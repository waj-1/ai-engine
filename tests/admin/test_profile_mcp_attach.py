"""Tests for POST /admin/profiles/{id}/mcp-attachments (Phase 3b Task 2).

Reconciles the agent_profile_mcp_servers join with a validated per-attachment
tool_subset + a scoped-build attach-ability guard, both applied before any write.
Uses a stateful fake Supabase client so we can assert the exact rows
inserted/deleted.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Stateful fake Supabase client
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table: str, store: Dict[str, List[Dict[str, Any]]], log: Dict[str, Any]):
        self._table = table
        self._store = store
        self._log = log
        self._eq: Dict[str, Any] = {}
        self._in: Optional[tuple] = None
        self._insert: Optional[Any] = None
        self._upsert: Optional[Any] = None
        self._on_conflict: Optional[str] = None
        self._delete = False

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def in_(self, col, vals):
        self._in = (col, list(vals))
        return self

    def insert(self, payload):
        self._insert = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._upsert = payload
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._delete = True
        return self

    def _matches(self, row) -> bool:
        for c, v in self._eq.items():
            if row.get(c) != v:
                return False
        if self._in is not None:
            col, vals = self._in
            if row.get(col) not in set(vals):
                return False
        return True

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._insert is not None:
            items = self._insert if isinstance(self._insert, list) else [self._insert]
            stored = []
            for it in items:
                r = dict(it)
                rows.append(r)
                stored.append(r)
            self._log.setdefault("inserts", []).append({"table": self._table, "rows": stored})
            return _Result(stored)
        if self._upsert is not None:
            items = self._upsert if isinstance(self._upsert, list) else [self._upsert]
            conflict_cols = [c.strip() for c in (self._on_conflict or "").split(",")
                             if c.strip()]
            stored = []
            for it in items:
                existing = None
                if conflict_cols:
                    for r in rows:
                        if all(r.get(c) == it.get(c) for c in conflict_cols):
                            existing = r
                            break
                if existing is not None:
                    existing.update(it)
                    stored.append(existing)
                else:
                    r = dict(it)
                    rows.append(r)
                    stored.append(r)
            self._log.setdefault("upserts", []).append({"table": self._table, "rows": stored})
            return _Result(stored)
        if self._delete:
            kept, removed = [], []
            for r in rows:
                (removed if self._matches(r) else kept).append(r)
            self._store[self._table] = kept
            self._log.setdefault("deletes", []).append(
                {"table": self._table, "filter": dict(self._eq), "removed": removed}
            )
            return _Result(removed)
        return _Result([r for r in rows if self._matches(r)])


class _Client:
    def __init__(self, store: Dict[str, List[Dict[str, Any]]]):
        self.store = store
        self.log: Dict[str, Any] = {}

    def table(self, name: str) -> _Query:
        return _Query(name, self.store, self.log)


def _make_app(store, monkeypatch):
    from admin import auth, profile_mcp_attach_handlers
    from admin.screens import agents as agents_screen
    from main import app

    client_obj = _Client(store)
    app.dependency_overrides[auth.require_superuser] = lambda: {
        "user_token": "t", "user_id": "u-1", "email": "a@waj.test"
    }
    # Both the handler and _attachable_mcps call get_service_client / client I/O.
    monkeypatch.setattr(profile_mcp_attach_handlers, "admin_service_client", lambda: client_obj)
    monkeypatch.setattr(profile_mcp_attach_handlers, "record_change", lambda **k: None)
    monkeypatch.setattr(
        profile_mcp_attach_handlers.AgentProfileLoader, "invalidate",
        classmethod(lambda cls, *a, **k: None),
    )
    tc = TestClient(app)

    def _teardown():
        app.dependency_overrides.pop(auth.require_superuser, None)

    return tc, client_obj, _teardown


def _base_store():
    return {
        "agent_profiles": [
            {"profile_id": "p1", "agent_kind": "business", "environment": "staging",
             "mcp_server_ids": []},
        ],
        "agent_mcp_servers": [
            {"mcp_server_id": "u1", "name": "Sales Docs", "source": "bundle",
             "is_builtin": False, "is_active": True},
        ],
        "agent_mcp_tools": [
            {"mcp_server_id": "u1", "tool_id": "t1", "position": 0},
        ],
        "agent_tools": [
            {"tool_id": "t1", "python_name": "search_docs", "is_deprecated": False},
        ],
        "agent_profile_mcp_servers": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_attach_writes_join_row_with_subset(monkeypatch):
    store = _base_store()
    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = tc.post("/admin/profiles/p1/mcp-attachments",
                    data={"attach_mcp_ids": ["u1"], "tool_subset__u1": "search_docs"},
                    follow_redirects=False)
        assert r.status_code == 302
        rows = store["agent_profile_mcp_servers"]
        assert len(rows) == 1
        assert rows[0]["profile_id"] == "p1"
        assert rows[0]["mcp_server_id"] == "u1"
        assert rows[0]["tool_subset"] == ["search_docs"]
        assert rows[0]["position"] == 0
    finally:
        teardown()


def test_empty_subset_is_null(monkeypatch):
    store = _base_store()
    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = tc.post("/admin/profiles/p1/mcp-attachments",
                    data={"attach_mcp_ids": ["u1"], "tool_subset__u1": ""},
                    follow_redirects=False)
        assert r.status_code == 302
        assert store["agent_profile_mcp_servers"][0]["tool_subset"] is None
    finally:
        teardown()


def test_detach_removes_row(monkeypatch):
    store = _base_store()
    store["agent_profile_mcp_servers"] = [
        {"profile_id": "p1", "mcp_server_id": "u1", "tool_subset": None, "position": 0}]
    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        # POST with u1 NOT in attach_mcp_ids → detach.
        r = tc.post("/admin/profiles/p1/mcp-attachments", data={}, follow_redirects=False)
        assert r.status_code == 302
        assert store["agent_profile_mcp_servers"] == []
    finally:
        teardown()


def test_redirect_percent_encodes_profile_id_and_uses_row_env(monkeypatch):
    """A path segment can legally contain `&` — the redirect must keep the
    whole profile_id as ONE percent-encoded query value (no smuggled params),
    and the env param must come from the profile ROW, not a cookie."""
    store = _base_store()
    store["agent_profiles"].append(
        {"profile_id": "p&x=1", "agent_kind": "business", "environment": "prod",
         "mcp_server_ids": []},
    )
    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = tc.post("/admin/profiles/p&x=1/mcp-attachments", data={},
                    follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "profile_id=p%26x%3D1" in loc, loc
        assert "&x=1" not in loc, loc
        assert "env=prod" in loc, loc
    finally:
        teardown()


def test_error_redirect_percent_encodes_profile_id(monkeypatch):
    store = _base_store()
    store["agent_profiles"].append(
        {"profile_id": "p&x=1", "agent_kind": "business", "environment": "staging",
         "mcp_server_ids": []},
    )
    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = tc.post("/admin/profiles/p&x=1/mcp-attachments",
                    data={"attach_mcp_ids": ["unknown-mcp"]},  # not attachable -> refused
                    follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "error=" in loc
        assert "profile_id=p%26x%3D1" in loc, loc
        assert "&x=1" not in loc, loc
    finally:
        teardown()


def test_subset_validated_against_mcp_tools(monkeypatch):
    store = _base_store()
    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = tc.post("/admin/profiles/p1/mcp-attachments",
                    data={"attach_mcp_ids": ["u1"], "tool_subset__u1": "search_docs,forged_tool"},
                    follow_redirects=False)
        assert r.status_code == 302
        assert "error=" in r.headers["location"]
        # No write on validation failure.
        assert store["agent_profile_mcp_servers"] == []
        assert "inserts" not in client_obj.log and "deletes" not in client_obj.log
    finally:
        teardown()


def test_attach_refuses_non_visible_mcp_on_a_scoped_build(monkeypatch):
    # A WAJ-only MCP (u_waj) not referenced by any visible orchard profile must be
    # refused server-side on a scoped build, even via a hand-crafted POST.
    from core import deployment as dep
    from admin import seed_agent_profiles as sap

    store = _base_store()
    store["agent_profiles"] = [
        {"profile_id": "pg", "agent_kind": "orchard", "environment": "staging", "mcp_server_ids": []},
    ]
    store["agent_mcp_servers"] = [
        {"mcp_server_id": "u_waj", "name": "WAJ Only", "source": "bundle",
         "is_builtin": False, "is_active": True},
    ]
    store["agent_mcp_tools"] = [{"mcp_server_id": "u_waj", "tool_id": "t1", "position": 0}]
    # agent_tools t1 already present from _base_store.
    monkeypatch.setattr(dep, "scoped_build", lambda: True)
    monkeypatch.setattr(sap, "visible_agent_kinds", lambda: ("orchard", "orchard_title", "orchard_suggestions"))

    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = tc.post("/admin/profiles/pg/mcp-attachments",
                    data={"attach_mcp_ids": ["u_waj"]}, follow_redirects=False)
        assert r.status_code == 302
        assert "error=" in r.headers["location"]
        # Refused: nothing attached.
        assert store["agent_profile_mcp_servers"] == []
    finally:
        teardown()


def test_attach_allows_mcp_referenced_by_visible_profile_via_legacy_array(monkeypatch):
    """On a scoped build, an MCP referenced by a visible profile's legacy
    mcp_server_ids array IS attachable — proves the visible-profile lookup
    fetches mcp_server_ids (not just profile_id/agent_kind) so the union half
    of _attachable_mcps's allow-set works during dark-launch coexistence."""
    from core import deployment as dep
    from admin import seed_agent_profiles as sap

    store = _base_store()
    # A visible orchard profile that references u1 via the LEGACY array (not the
    # new join table) — u1 must therefore be attachable.
    store["agent_profiles"] = [
        {"profile_id": "pg", "agent_kind": "orchard", "environment": "staging",
         "mcp_server_ids": ["u1"]},
    ]
    monkeypatch.setattr(dep, "scoped_build", lambda: True)
    monkeypatch.setattr(sap, "visible_agent_kinds", lambda: ("orchard", "orchard_title", "orchard_suggestions"))

    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = tc.post("/admin/profiles/pg/mcp-attachments",
                    data={"attach_mcp_ids": ["u1"], "tool_subset__u1": "search_docs"},
                    follow_redirects=False)
        assert r.status_code == 302
        assert "error=" not in r.headers["location"]  # allowed, not refused
        rows = store["agent_profile_mcp_servers"]
        assert len(rows) == 1 and rows[0]["mcp_server_id"] == "u1"
    finally:
        teardown()
