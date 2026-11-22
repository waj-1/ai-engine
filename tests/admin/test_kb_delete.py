"""Tests for POST /admin/kb/{source_id}/delete.

Asserts that the delete handler:
  1. Issues a DELETE against agent_data_sources filtered by source_id.
  2. Updates any agent_profiles whose data_source_ids contained the deleted id
     so the id is removed from the array.
  3. Returns a 302 redirect to /admin/kb.

Uses the same _FakeClient / TestClient + dependency-override harness as
test_kb_upload.py.
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
# Supabase fake — same shape as test_kb_upload._FakeClient
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

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self.last_filter[col] = val
        if self._pending_update is not None:
            # record the update + filter together
            self.update_calls.append({"payload": self._pending_update, "filter": {col: val}})
            self._pending_update = None
        if self.deleted:
            self.deleted_filter[col] = val
        return self

    def limit(self, _n):
        return self

    def update(self, payload):
        self._pending_update = payload
        return self

    def delete(self):
        self.deleted = True
        return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _FakeClient:
    def __init__(self, rows_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self._rows = rows_by_table or {}
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name: str) -> _FakeQuery:
        # Each call to table() with the same name returns the SAME query
        # object so we can inspect accumulated state after the handler runs.
        if name not in self.queries:
            self.queries[name] = _FakeQuery(self._rows.get(name, []))
        return self.queries[name]


# ---------------------------------------------------------------------------
# App factory — mirrors test_kb_upload._make_app
# ---------------------------------------------------------------------------


def _make_app(fake_client: _FakeClient):
    from fastapi.testclient import TestClient
    from admin import auth, kb_handlers
    from main import app

    def _fake_super():
        return {"user_token": "tok", "user_id": "u-1", "email": "a@waj.test"}

    app.dependency_overrides[auth.require_superuser] = _fake_super
    patches = [
        patch.object(kb_handlers, "get_service_client", return_value=fake_client),
        patch.object(kb_handlers, "record_change"),
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
# Tests
# ---------------------------------------------------------------------------


def test_delete_issues_delete_on_agent_data_sources():
    """Handler must call .delete().eq('source_id', ...) on agent_data_sources."""
    source_id = "src-del-1"
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [
                {"source_id": source_id, "name": "My KB", "kind": "pgvector", "config": {}}
            ],
            "agent_profiles": [],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(f"/admin/kb/{source_id}/delete", follow_redirects=False)
        assert r.status_code == 302, r.text[:300]
        assert r.headers["location"] == "/admin/kb"

        q = fake.queries.get("agent_data_sources")
        assert q is not None, "agent_data_sources table was never touched"
        assert q.deleted, "DELETE was not called on agent_data_sources"
        assert q.deleted_filter.get("source_id") == source_id, (
            f"DELETE was not filtered by source_id={source_id!r}, "
            f"got {q.deleted_filter!r}"
        )
    finally:
        teardown()


def test_delete_removes_source_id_from_agent_profiles():
    """Profiles referencing the deleted source_id must have it removed."""
    source_id = "src-del-2"
    profile_a = {
        "profile_id": "prof-a",
        "data_source_ids": [source_id, "src-other"],
        "agent_kind": "business",
    }
    profile_b = {
        "profile_id": "prof-b",
        "data_source_ids": ["src-other"],  # does NOT contain source_id
        "agent_kind": "sales",
    }
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [
                {"source_id": source_id, "name": "KB2", "kind": "pgvector", "config": {}}
            ],
            "agent_profiles": [profile_a, profile_b],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(f"/admin/kb/{source_id}/delete", follow_redirects=False)
        assert r.status_code == 302

        q = fake.queries.get("agent_profiles")
        assert q is not None

        # Only profile_a should have been updated (profile_b doesn't reference source_id)
        assert len(q.update_calls) == 1, (
            f"Expected 1 profile update, got {len(q.update_calls)}: {q.update_calls}"
        )
        call = q.update_calls[0]
        updated_ids = call["payload"].get("data_source_ids")
        assert updated_ids == ["src-other"], (
            f"Expected ['src-other'] after removing {source_id!r}, got {updated_ids!r}"
        )
        assert call["filter"].get("profile_id") == "prof-a", (
            f"Update was applied to wrong profile: {call['filter']!r}"
        )
    finally:
        teardown()


def test_delete_no_profiles_referencing_source_skips_profile_updates():
    """When no profiles reference the source, no profile updates are issued."""
    source_id = "src-del-3"
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [
                {"source_id": source_id, "name": "KB3", "kind": "pgvector", "config": {}}
            ],
            "agent_profiles": [
                {"profile_id": "prof-x", "data_source_ids": ["src-other"], "agent_kind": "support_bot"},
            ],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(f"/admin/kb/{source_id}/delete", follow_redirects=False)
        assert r.status_code == 302

        q = fake.queries.get("agent_profiles")
        # select was called to fetch profiles, but no updates issued
        assert len(q.update_calls) == 0, f"Unexpected profile updates: {q.update_calls}"
    finally:
        teardown()


def test_delete_missing_source_still_redirects():
    """If the source row is already gone, handler redirects cleanly (no 404/500)."""
    source_id = "src-del-gone"
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [],
            "agent_profiles": [],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(f"/admin/kb/{source_id}/delete", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/admin/kb"
    finally:
        teardown()
