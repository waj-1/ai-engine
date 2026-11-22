"""Tests for MCP server edit + delete handlers.

Asserts that:
  1. POST /admin/agent-mcp-servers/{id}    — updates the row (incl. tool_subset as list).
  2. POST /admin/agent-mcp-servers/{id}/delete — deletes the row AND removes the id
     from any agent_profiles.mcp_server_ids arrays, then redirects to /admin/agent-mcp-servers.

Uses the same _FakeClient / TestClient + dependency-override harness as test_kb_delete.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Supabase fake — mirrors test_kb_delete._FakeClient
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self.last_filter: Dict[str, Any] = {}
        self.last_update: Optional[Dict[str, Any]] = None
        self.deleted = False
        self.deleted_filter: Dict[str, Any] = {}
        # track update calls per eq-filter so we can assert per-profile
        self.update_calls: List[Dict[str, Any]] = []
        self._pending_update: Optional[Dict[str, Any]] = None
        # track insert calls (order matters — reconciliation re-inserts by position)
        self.inserted: List[Dict[str, Any]] = []
        self._pending_insert: Optional[Dict[str, Any]] = None
        # track upsert calls — membership reconcile now upserts-then-deletes-stale
        self.upserted: List[Dict[str, Any]] = []
        self._pending_upsert: Optional[Any] = None
        # when set, execute() of an update raises this (simulates a DB constraint
        # violation, e.g. UNIQUE(name) on a rename collision).
        self.update_raises: Optional[Exception] = None
        self._did_update = False

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self.last_filter[col] = val
        if self._pending_update is not None:
            self.update_calls.append({"payload": self._pending_update, "filter": {col: val}})
            self._pending_update = None
        if self.deleted:
            self.deleted_filter[col] = val
        return self

    def order(self, *_a, **_kw):
        return self

    def limit(self, _n):
        return self

    def in_(self, _col, _vals):
        return self

    def update(self, payload):
        self._pending_update = payload
        self._did_update = True
        return self

    def delete(self):
        self.deleted = True
        return self

    def insert(self, payload):
        self._pending_insert = payload
        self.inserted.append(payload)
        return self

    def upsert(self, payload, on_conflict=None):
        self._pending_upsert = payload
        items = payload if isinstance(payload, list) else [payload]
        self.upserted.extend(items)
        return self

    def execute(self):
        if self._did_update:
            self._did_update = False
            if self.update_raises is not None:
                raise self.update_raises
        result = MagicMock()
        if self._pending_insert is not None:
            result.data = [self._pending_insert]
            self._pending_insert = None
        elif self._pending_upsert is not None:
            up = self._pending_upsert
            result.data = up if isinstance(up, list) else [up]
            self._pending_upsert = None
        else:
            result.data = list(self._rows)
        return result


class _FakeClient:
    def __init__(self, rows_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self._rows = rows_by_table or {}
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name: str) -> _FakeQuery:
        if name not in self.queries:
            self.queries[name] = _FakeQuery(self._rows.get(name, []))
        return self.queries[name]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(fake_client: _FakeClient):
    from fastapi.testclient import TestClient
    from admin import auth, mcp_handlers
    from main import app

    def _fake_super():
        return {"user_token": "tok", "user_id": "u-1", "email": "a@waj.test"}

    app.dependency_overrides[auth.require_superuser] = _fake_super
    patches = [
        patch.object(mcp_handlers, "get_service_client", return_value=fake_client),
        patch.object(mcp_handlers, "record_change"),
    ]
    for p in patches:
        p.start()
    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


# ---------------------------------------------------------------------------
# UPDATE tests
# ---------------------------------------------------------------------------


def test_update_posts_parsed_fields_to_agent_mcp_servers():
    """POST /admin/agent-mcp-servers/{id} must update the row with parsed fields."""
    mcp_id = "mcp-upd-1"
    fake = _FakeClient(
        rows_by_table={
            "agent_mcp_servers": [
                {
                    "mcp_server_id": mcp_id,
                    "name": "old-name",
                    "transport": "sse",
                    "endpoint": "https://old.example.com",
                    "auth_env_var": "OLD_TOKEN",
                    "tool_subset": None,
                    "is_active": True,
                }
            ],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/agent-mcp-servers/{mcp_id}",
            data={
                "name": "new-name",
                "transport": "http",
                "endpoint": "https://new.example.com",
                "auth_env_var": "NEW_TOKEN",
                "tool_subset": "search_web,create_task",
                "is_active": "on",
                "_active_form": "1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:300]
        assert f"/admin/agent-mcp-servers/{mcp_id}" in r.headers["location"]

        q = fake.queries.get("agent_mcp_servers")
        assert q is not None, "agent_mcp_servers table was never touched"
        assert len(q.update_calls) >= 1, f"No update calls recorded: {q.update_calls}"

        update_call = q.update_calls[0]
        payload = update_call["payload"]
        assert payload["name"] == "new-name"
        assert payload["transport"] == "http"
        assert payload["endpoint"] == "https://new.example.com"
        assert payload["auth_env_var"] == "NEW_TOKEN"
        # tool_subset must be parsed into a list
        assert isinstance(payload["tool_subset"], list), (
            f"tool_subset should be a list, got {type(payload['tool_subset'])}: {payload['tool_subset']!r}"
        )
        assert "search_web" in payload["tool_subset"]
        assert "create_task" in payload["tool_subset"]

        assert update_call["filter"].get("mcp_server_id") == mcp_id
    finally:
        teardown()


def test_update_empty_tool_subset_becomes_none():
    """When tool_subset field is empty, it should be stored as None (all tools)."""
    mcp_id = "mcp-upd-2"
    fake = _FakeClient(
        rows_by_table={
            "agent_mcp_servers": [
                {
                    "mcp_server_id": mcp_id,
                    "name": "srv",
                    "transport": "sse",
                    "endpoint": "https://example.com",
                    "auth_env_var": None,
                    "tool_subset": ["old_tool"],
                    "is_active": True,
                }
            ],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/agent-mcp-servers/{mcp_id}",
            data={
                "name": "srv",
                "transport": "sse",
                "endpoint": "https://example.com",
                "tool_subset": "",  # empty => all tools => None
                "_active_form": "1",
                "is_active": "on",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302

        q = fake.queries.get("agent_mcp_servers")
        assert q is not None
        update_call = q.update_calls[0]
        assert update_call["payload"]["tool_subset"] is None, (
            f"Empty tool_subset should become None, got {update_call['payload']['tool_subset']!r}"
        )
    finally:
        teardown()


# ---------------------------------------------------------------------------
# DELETE tests
# ---------------------------------------------------------------------------


def test_delete_issues_delete_on_agent_mcp_servers():
    """Handler must call .delete().eq('mcp_server_id', ...) on agent_mcp_servers."""
    mcp_id = "mcp-del-1"
    fake = _FakeClient(
        rows_by_table={
            "agent_mcp_servers": [
                {
                    "mcp_server_id": mcp_id,
                    "name": "orchard-mock",
                    "transport": "sse",
                    "endpoint": "https://mcp.example.com",
                }
            ],
            "agent_profiles": [],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(f"/admin/agent-mcp-servers/{mcp_id}/delete", follow_redirects=False)
        assert r.status_code == 302, r.text[:300]
        assert r.headers["location"] == "/admin/agent-mcp-servers"

        q = fake.queries.get("agent_mcp_servers")
        assert q is not None, "agent_mcp_servers table was never touched"
        assert q.deleted, "DELETE was not called on agent_mcp_servers"
        assert q.deleted_filter.get("mcp_server_id") == mcp_id, (
            f"DELETE not filtered by mcp_server_id={mcp_id!r}, got {q.deleted_filter!r}"
        )
    finally:
        teardown()


def test_delete_removes_mcp_server_id_from_agent_profiles():
    """Profiles referencing the deleted mcp_server_id must have it removed."""
    mcp_id = "mcp-del-2"
    profile_a = {
        "profile_id": "prof-a",
        "mcp_server_ids": [mcp_id, "other-mcp"],
        "agent_kind": "business",
    }
    profile_b = {
        "profile_id": "prof-b",
        "mcp_server_ids": ["other-mcp"],  # does NOT contain mcp_id
        "agent_kind": "sales",
    }
    fake = _FakeClient(
        rows_by_table={
            "agent_mcp_servers": [
                {"mcp_server_id": mcp_id, "name": "orchard-mock", "transport": "sse", "endpoint": "https://x.com"}
            ],
            "agent_profiles": [profile_a, profile_b],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(f"/admin/agent-mcp-servers/{mcp_id}/delete", follow_redirects=False)
        assert r.status_code == 302

        q = fake.queries.get("agent_profiles")
        assert q is not None

        # Only profile_a should have been updated
        assert len(q.update_calls) == 1, (
            f"Expected 1 profile update, got {len(q.update_calls)}: {q.update_calls}"
        )
        call = q.update_calls[0]
        updated_ids = call["payload"].get("mcp_server_ids")
        assert updated_ids == ["other-mcp"], (
            f"Expected ['other-mcp'] after removing {mcp_id!r}, got {updated_ids!r}"
        )
        assert call["filter"].get("profile_id") == "prof-a", (
            f"Update was applied to wrong profile: {call['filter']!r}"
        )
    finally:
        teardown()


def test_delete_no_profiles_referencing_mcp_skips_profile_updates():
    """When no profiles reference the mcp_server_id, no profile updates are issued."""
    mcp_id = "mcp-del-3"
    fake = _FakeClient(
        rows_by_table={
            "agent_mcp_servers": [
                {"mcp_server_id": mcp_id, "name": "srv", "transport": "http", "endpoint": "https://x.com"}
            ],
            "agent_profiles": [
                {"profile_id": "prof-x", "mcp_server_ids": ["other-mcp"], "agent_kind": "support_bot"},
            ],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(f"/admin/agent-mcp-servers/{mcp_id}/delete", follow_redirects=False)
        assert r.status_code == 302

        q = fake.queries.get("agent_profiles")
        assert len(q.update_calls) == 0, f"Unexpected profile updates: {q.update_calls}"
    finally:
        teardown()


def test_delete_missing_server_still_redirects():
    """If the mcp_server row is already gone, handler redirects cleanly."""
    mcp_id = "mcp-del-gone"
    fake = _FakeClient(
        rows_by_table={
            "agent_mcp_servers": [],
            "agent_profiles": [],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(f"/admin/agent-mcp-servers/{mcp_id}/delete", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/admin/agent-mcp-servers"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# usage_guidance / context_fields persistence + built-in guard + cascade
# ---------------------------------------------------------------------------


def test_update_persists_usage_guidance_and_context_fields():
    fake = _FakeClient(rows_by_table={"agent_mcp_servers": [
        {"mcp_server_id": "m1", "name": "x", "is_builtin": False}]})
    client, teardown = _make_app(fake)
    try:
        r = client.post("/admin/agent-mcp-servers/m1", data={
            "name": "x", "usage_guidance": "when to use", "context_fields": '{"token":"$ctx.user_token"}',
            "_active_form": "1", "is_active": "on"}, follow_redirects=False)
        assert r.status_code == 302
        payload = fake.queries["agent_mcp_servers"].update_calls[0]["payload"]
        assert payload["usage_guidance"] == "when to use"
        assert payload["context_fields"] == {"token": "$ctx.user_token"}
    finally:
        teardown()


def test_builtin_server_update_is_refused():
    fake = _FakeClient(rows_by_table={"agent_mcp_servers": [
        {"mcp_server_id": "b1", "name": "__builtin__:business", "is_builtin": True}]})
    client, teardown = _make_app(fake)
    try:
        r = client.post("/admin/agent-mcp-servers/b1", data={"name": "hacked", "_active_form": "1"},
                        follow_redirects=False)
        # refused: redirect back with error (or 400) and NO update call
        assert not fake.queries["agent_mcp_servers"].update_calls
    finally:
        teardown()


def test_delete_cascades_join_tables():
    fake = _FakeClient(rows_by_table={
        "agent_mcp_servers": [{"mcp_server_id": "m1", "name": "x", "is_builtin": False}],
        "agent_profiles": [], "agent_mcp_tools": [{"mcp_server_id": "m1", "tool_id": "t1"}],
        "agent_profile_mcp_servers": [{"mcp_server_id": "m1", "profile_id": "p1"}]})
    client, teardown = _make_app(fake)
    try:
        r = client.post("/admin/agent-mcp-servers/m1/delete", follow_redirects=False)
        assert r.status_code == 302
        # both join tables saw a delete filtered by mcp_server_id
        assert fake.queries["agent_mcp_tools"].deleted
        assert fake.queries["agent_profile_mcp_servers"].deleted
    finally:
        teardown()


def test_builtin_server_delete_is_refused():
    """Delete must also refuse for built-in bundles, with no join-table cascade."""
    fake = _FakeClient(rows_by_table={
        "agent_mcp_servers": [{"mcp_server_id": "b1", "name": "__builtin__:business", "is_builtin": True}],
        "agent_profiles": [],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.post("/admin/agent-mcp-servers/b1/delete", follow_redirects=False)
        assert r.status_code in (302, 400)
        assert not fake.queries["agent_mcp_servers"].deleted
        assert "agent_mcp_tools" not in fake.queries or not fake.queries["agent_mcp_tools"].deleted
    finally:
        teardown()


# ---------------------------------------------------------------------------
# POST /agent-mcp-servers/{id}/members — membership reconciliation
# ---------------------------------------------------------------------------


def test_members_reconciles_agent_mcp_tools_in_order():
    """Posting member_tool_ids reconciles agent_mcp_tools: upsert the posted
    set in order (position 0,1,...) then delete only the now-stale rows —
    the bundle's tool set is never emptied mid-write."""
    fake = _FakeClient(rows_by_table={
        "agent_mcp_servers": [
            {"mcp_server_id": "m1", "name": "bundle-x", "is_builtin": False, "source": "bundle"}
        ],
        "agent_mcp_tools": [{"mcp_server_id": "m1", "tool_id": "old-tool"}],
        # Both posted ids must be current non-deprecated tools to pass the
        # pre-write validation guard.
        "agent_tools": [
            {"tool_id": "t1", "python_name": "tool_one", "is_deprecated": False},
            {"tool_id": "t2", "python_name": "tool_two", "is_deprecated": False},
        ],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            "/admin/agent-mcp-servers/m1/members",
            data={"member_tool_ids": ["t2", "t1"]},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:300]
        q = fake.queries["agent_mcp_tools"]
        assert q.deleted, "expected a delete of the now-stale membership"
        assert [row["tool_id"] for row in q.upserted] == ["t2", "t1"]
        assert [row["position"] for row in q.upserted] == [0, 1]
        assert all(row["mcp_server_id"] == "m1" for row in q.upserted)
    finally:
        teardown()


def test_builtin_server_members_is_refused():
    """Membership editing must refuse for built-in bundles (seeder-managed)."""
    fake = _FakeClient(rows_by_table={
        "agent_mcp_servers": [
            {"mcp_server_id": "b1", "name": "__builtin__:business", "is_builtin": True, "source": "bundle"}
        ],
        "agent_mcp_tools": [{"mcp_server_id": "b1", "tool_id": "t1"}],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            "/admin/agent-mcp-servers/b1/members",
            data={"member_tool_ids": ["t2"]},
            follow_redirects=False,
        )
        assert r.status_code in (302, 400)
        q = fake.queries.get("agent_mcp_tools")
        assert q is None or (not q.deleted and not q.inserted)
    finally:
        teardown()


def test_members_rejects_forged_id_without_wiping():
    """A forged/deprecated member id must be rejected BEFORE the destructive
    delete — existing membership stays intact and nothing is inserted."""
    fake = _FakeClient(rows_by_table={
        "agent_mcp_servers": [
            {"mcp_server_id": "m1", "name": "bundle-x", "is_builtin": False, "source": "bundle"}
        ],
        "agent_mcp_tools": [{"mcp_server_id": "m1", "tool_id": "t1", "position": 0}],
        # Picker only knows t1 — "forged" is not a current non-deprecated tool.
        "agent_tools": [{"tool_id": "t1", "python_name": "tool_one", "is_deprecated": False}],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            "/admin/agent-mcp-servers/m1/members",
            data={"member_tool_ids": ["t1", "forged"]},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "error=" in r.headers["location"], r.headers["location"]
        q = fake.queries.get("agent_mcp_tools")
        # No destructive delete, no insert of the forged id — membership preserved.
        assert q is None or not q.deleted, "existing membership must NOT be wiped"
        assert q is None or not q.inserted, "nothing should be inserted on rejection"
    finally:
        teardown()


def test_members_audit_logs_before_and_after():
    """record_change must log the REAL before (current membership) and after
    (the validated, persisted set)."""
    from admin import mcp_handlers

    fake = _FakeClient(rows_by_table={
        "agent_mcp_servers": [
            {"mcp_server_id": "m1", "name": "bundle-x", "is_builtin": False, "source": "bundle"}
        ],
        "agent_mcp_tools": [{"mcp_server_id": "m1", "tool_id": "t1", "position": 0}],
        "agent_tools": [
            {"tool_id": "t1", "python_name": "tool_one", "is_deprecated": False},
            {"tool_id": "t2", "python_name": "tool_two", "is_deprecated": False},
        ],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            "/admin/agent-mcp-servers/m1/members",
            data={"member_tool_ids": ["t2", "t1"]},
            follow_redirects=False,
        )
        assert r.status_code == 302
        # record_change is patched to a MagicMock by _make_app.
        assert mcp_handlers.record_change.called
        kwargs = mcp_handlers.record_change.call_args.kwargs
        assert kwargs["before"] == {"member_tool_ids": ["t1"]}
        assert kwargs["after"] == {"member_tool_ids": ["t2", "t1"]}
    finally:
        teardown()


def test_update_duplicate_name_rerenders_not_500():
    """Renaming an MCP to a name that collides with UNIQUE(name) must
    redirect-with-error, not 500 — matching the connect/create create-paths."""
    from admin import mcp_handlers

    fake = _FakeClient(rows_by_table={
        "agent_mcp_servers": [{"mcp_server_id": "m1", "name": "old", "is_builtin": False}],
    })
    # Simulate PostgREST raising a unique-violation on the rename update.
    fake.table("agent_mcp_servers").update_raises = Exception(
        "duplicate key value violates unique constraint \"agent_mcp_servers_name_key\""
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            "/admin/agent-mcp-servers/m1",
            data={"name": "taken", "_active_form": "1", "is_active": "on"},
            follow_redirects=False,
        )
        assert r.status_code == 302  # re-render (redirect-with-error), not a 500
        assert "error=" in r.headers["location"]
        # record_change must NOT fire on a failed update.
        assert not mcp_handlers.record_change.called
    finally:
        teardown()


def test_update_rejects_builtin_prefix_rename():
    """Renaming to the reserved __builtin__: prefix is rejected (no write),
    consistent with the create/connect guards."""
    from admin import mcp_handlers

    fake = _FakeClient(rows_by_table={
        "agent_mcp_servers": [{"mcp_server_id": "m1", "name": "ok", "is_builtin": False}],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            "/admin/agent-mcp-servers/m1",
            data={"name": "__builtin__:sneaky", "_active_form": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "error=" in r.headers["location"]
        # No update recorded, no audit.
        assert not fake.table("agent_mcp_servers").update_calls
        assert not mcp_handlers.record_change.called
    finally:
        teardown()
