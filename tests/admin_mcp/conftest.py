"""Fixtures for MCP server tests: a minimal FastAPI host app (so we don't
boot main.py) and an authenticated-actor bypass."""
from __future__ import annotations

import contextlib

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

ACTOR = {"user_id": "00000000-0000-0000-0000-000000000001",
         "email": "admin@waj.test", "user_token": "test-token"}

MCP_HEADERS = {
    "Authorization": "Bearer test-token",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture(scope="session")
def mcp_client():
    """TestClient for the mounted MCP app with resolve_superuser stubbed.

    Session-scoped (not the usual function scope) because the SDK's
    StreamableHTTPSessionManager.run() may be entered exactly once per
    FastMCP instance for the life of the process — a second .run() call
    raises RuntimeError even after the first has exited (see
    mcp/server/streamable_http_manager.py). admin_mcp.server.mcp is a
    module-level singleton shared by every test file under
    tests/admin_mcp/, so the host app + its lifespan are built once for the
    whole test session rather than once per test function. Uses
    MonkeyPatch.context() instead of the function-scoped monkeypatch
    fixture for the same scope reason.
    """
    import admin_mcp.auth as mcp_auth
    from admin_mcp.server import build_mcp_asgi_app, mcp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp_auth, "resolve_superuser", lambda tok: dict(ACTOR))
        # The deploy-workflow tools are only offered where a deploy
        # repository is configured (admin_mcp.tools_ship.
        # drop_unconfigured_deploy_tools). These tests describe the whole
        # surface, so configure one for the build, in every build.
        mp.setenv("GITHUB_DEPLOY_REPO", "example-org/engine")

        @contextlib.asynccontextmanager
        async def lifespan(app):
            async with mcp.session_manager.run():
                yield

        host = FastAPI(lifespan=lifespan)
        # Route (not Mount), matching main.py's registration: proves the
        # tests exercise the same no-redirect path as production.
        host.add_route("/mcp", build_mcp_asgi_app(), methods=["GET", "POST", "DELETE"])
        with TestClient(host) as client:
            yield client


def rpc(client, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=MCP_HEADERS)


@pytest.fixture(autouse=True)
def mcp_actor():
    """Every tool test runs with an authenticated actor set."""
    from admin_mcp.context import MCP_ACTOR
    token = MCP_ACTOR.set(dict(ACTOR))
    yield ACTOR
    MCP_ACTOR.reset(token)


class FakeTable:
    """Fluent postgrest fake: seed rows per table, apply recorded predicates
    on read, and mutate `_data` in place so reads reflect prior writes."""

    def __init__(self, data):
        self._data = data
        self.inserted, self.updated, self.deleted = [], [], []
        self.upserted: list = []
        self._predicates: list = []
        self._limit = None
        self._order = None
        self._insert_payload = self._update_payload = None
        self._upsert_payload = None
        self._on_conflict = None
        self._deleting = False

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._predicates.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._predicates.append(("in", col, list(vals)))
        return self

    def gte(self, col, val):
        self._predicates.append(("gte", col, val))
        return self

    def is_(self, col, val):
        assert val == "null", val
        self._predicates.append(("is_null", col, None))
        return self

    def order(self, *a, **k):
        """Record the sort so `execute()` can apply it before `limit`.

        This used to be a no-op returning self, which silently made any test
        about ordering vacuous — a `.order(...).limit(1)` query returned
        whatever happened to be inserted first, so the test passed regardless of
        the column or direction the code asked for. Production code DOES depend
        on this (e.g. seed_templates._default_model_id picks the oldest active
        model via `.order("created_at").limit(1)`), so the fake has to model it.

        Accepts postgrest's shapes: `order("col")`, `order("col", desc=True)`,
        and `order(column="col", desc=...)`. Rows missing the sort key sort last
        so a partially-populated fixture can't crash the comparison.
        """
        col = a[0] if a else k.get("column")
        if col:
            self._order = (col, bool(k.get("desc", False)))
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    def range(self, *a, **k):
        return self

    def insert(self, payload):
        self._insert_payload = payload
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._upsert_payload = payload
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._deleting = True
        return self

    def _matches(self, row):
        for kind, col, val in self._predicates:
            if kind == "eq" and row.get(col) != val:
                return False
            if kind == "in" and row.get(col) not in val:
                return False
            if kind == "gte" and row.get(col) < val:
                return False
            if kind == "is_null" and row.get(col) is not None:
                return False
        return True

    def _reset_query_state(self):
        self._predicates = []
        self._limit = None
        # Must be cleared too, or a sort leaks into the next query on this table
        # and silently reorders an unrelated result set.
        self._order = None

    def execute(self):
        result = type("R", (), {})()
        try:
            if self._insert_payload is not None:
                self.inserted.append(self._insert_payload)
                row = dict(self._insert_payload)
                row.setdefault("id", f"fake-id-{len(self._data)}")
                self._data.append(row)
                result.data = [row]
                self._insert_payload = None
            elif self._update_payload is not None:
                self.updated.append(self._update_payload)
                matched = [row for row in self._data if self._matches(row)]
                for row in matched:
                    row.update(self._update_payload)
                result.data = matched
                self._update_payload = None
            elif self._upsert_payload is not None:
                # Merge-on-conflict over the composite key columns named in
                # on_conflict, mirroring PostgREST upsert. Join tables have no
                # synthetic id column, so (unlike insert) none is added.
                items = (self._upsert_payload if isinstance(self._upsert_payload, list)
                         else [self._upsert_payload])
                conflict_cols = [c.strip() for c in (self._on_conflict or "").split(",")
                                 if c.strip()]
                out = []
                for item in items:
                    existing = None
                    if conflict_cols:
                        for row in self._data:
                            if all(row.get(c) == item.get(c) for c in conflict_cols):
                                existing = row
                                break
                    if existing is not None:
                        existing.update(item)
                        out.append(existing)
                    else:
                        row = dict(item)
                        self._data.append(row)
                        out.append(row)
                self.upserted.extend(items)
                result.data = out
                self._upsert_payload = None
                self._on_conflict = None
            elif self._deleting:
                matched = [row for row in self._data if self._matches(row)]
                matched_ids = {id(row) for row in matched}
                self._data[:] = [row for row in self._data
                                  if id(row) not in matched_ids]
                self.deleted.extend(matched)
                result.data = matched
                self._deleting = False
            else:
                matched = [row for row in self._data if self._matches(row)]
                if self._order is not None:
                    col, desc = self._order
                    # Rows missing the key sort last regardless of direction, so a
                    # partially-populated fixture can't blow up the comparison.
                    matched = sorted(
                        matched,
                        key=lambda r: (r.get(col) is None, r.get(col)),
                        reverse=desc,
                    )
                if self._limit is not None:
                    matched = matched[:self._limit]
                result.data = matched
        finally:
            self._reset_query_state()
        result.count = len(result.data)
        return result


class FakeClient:
    def __init__(self, tables: dict):
        self.tables = {name: FakeTable(data) for name, data in tables.items()}

    def table(self, name):
        return self.tables.setdefault(name, FakeTable([]))
