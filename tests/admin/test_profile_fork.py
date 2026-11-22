"""Tests for the Fork + Diff admin handlers (admin/profile_fork_handlers.py).

Covers:
  POST /admin/agents/{profile_id}/fork
    - happy path inserts a draft/dev row with copied arrays
    - cache invalidation fires for the source kind+env AND for dev
    - missing profile_id → 404

  GET /admin/agents/diff
    - 404 when either profile is missing
    - rendered page contains both profile ids
  compute_profile_diff()
    - self-diff: every scalar equal, every array fully shared
    - cross-fork diff: scalar diffs + added/removed/shared lists correct
    - kind_mismatch flag set when the two profiles have different agent_kinds

The supabase client is faked per-test (see _FakeClient) so the test
suite doesn't need Postgres; admin.services.profiles.record_change and
.invalidate_profile_loader are patched so we can assert they were called
without side-effects.
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
# A scriptable supabase fake that handles select/eq/in_/insert/update/order.
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self.filters: Dict[str, Any] = {}
        self.in_filters: Dict[str, List[Any]] = {}
        self.last_insert: Optional[Dict[str, Any]] = None
        self.last_update: Optional[Dict[str, Any]] = None
        self._select_cols: Optional[str] = None

    def select(self, cols):
        self._select_cols = cols
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def in_(self, col, vals):
        self.in_filters[col] = list(vals)
        return self

    def order(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    def insert(self, payload):
        self.last_insert = payload
        return _InsertResult(payload)

    def update(self, payload):
        self.last_update = payload
        return self

    def execute(self):
        # Filter rows against accumulated .eq/.in_ filters so a single
        # FakeClient can serve multiple lookups consistently.
        rows = list(self._rows)
        for col, val in self.filters.items():
            rows = [r for r in rows if r.get(col) == val]
        for col, vals in self.in_filters.items():
            rows = [r for r in rows if r.get(col) in vals]
        result = MagicMock()
        result.data = rows
        return result


class _InsertResult:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        result = MagicMock()
        result.data = [self._payload]
        return result


class _FakeClient:
    def __init__(self, rows_by_table=None):
        self._rows_by_table = rows_by_table or {}
        self.queries: Dict[str, List[_FakeQuery]] = {}

    def table(self, name):
        q = _FakeQuery(self._rows_by_table.get(name, []))
        self.queries.setdefault(name, []).append(q)
        return q

    # Helpers for assertions ------------------------------------------------
    def last_query(self, table: str) -> Optional[_FakeQuery]:
        return self.queries.get(table, [None])[-1]

    def insert_payloads(self, table: str) -> List[Dict[str, Any]]:
        return [q.last_insert for q in self.queries.get(table, []) if q.last_insert]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def profile_a() -> Dict[str, Any]:
    return {
        "profile_id": "p-aaaa",
        "agent_kind": "business",
        "slug": "business_default",
        "environment": "prod",
        "status": "active",
        "is_active": True,
        "display_name": "Business",
        "description": "ops agent",
        "model_main_id": "model-main-1",
        "model_light_id": "model-light-1",
        "tool_ids": ["tool-1", "tool-2"],
        "data_source_ids": ["src-1"],
        "mcp_server_ids": ["mcp-1"],
        "downstream_profile_ids": ["p-cccc"],
        "parent_profile_id": None,
        "endpoint_path": "/chat/stream",
        "prompt_version_ids": {},
        "escalation_policy": {},
        "additional_agent_kinds": [],
    }


@pytest.fixture
def profile_b(profile_a) -> Dict[str, Any]:
    # B shares the kind with A but drops tool-2, adds tool-3, swaps model.
    return {
        **profile_a,
        "profile_id": "p-bbbb",
        "environment": "dev",
        "status": "draft",
        "display_name": "Business (fork)",
        "tool_ids": ["tool-1", "tool-3"],
        "data_source_ids": ["src-1", "src-2"],
        "model_main_id": "model-main-2",
    }


def _make_app_with_fake_db(fake_client):
    """Boot the FastAPI app with require_superuser bypassed and the
    supabase client patched everywhere the fork+diff path reads from.

    Fork + diff logic now lives in admin/services/profiles.py
    (fork_profile_row / invalidate_profile_loader) — profile_fork_handlers
    only wires the routes. That module resolves its DB client via a
    function-local `from core.auth import admin_service_client`, and
    (with no admin-env override configured for this test env)
    `admin_service_client()` always bottoms out in the real
    `core.auth.get_service_client()`. Patching that one call intercepts
    every path — the fork service AND agents_diff's module-level
    `admin_service_client()` call — without needing to know each call
    site's import style.
    """
    from fastapi.testclient import TestClient
    from admin import auth, profile_fork_handlers
    from admin.services import profiles as profile_services
    from core import auth as core_auth
    from main import app

    def _fake_superuser():
        return {
            "user_token": "fake-jwt",
            "user_id": "u-1",
            "email": "test@waj.local",
        }

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(core_auth, "get_service_client", return_value=fake_client),
        patch.object(profile_services, "record_change"),
        patch.object(profile_services, "invalidate_profile_loader"),
    ]
    started = [p.start() for p in patches]

    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown, started


# ---------------------------------------------------------------------------
# Fork — happy path
# ---------------------------------------------------------------------------


def test_fork_inserts_draft_row_with_copied_arrays(profile_a):
    fake = _FakeClient(rows_by_table={"agent_profiles": [profile_a]})
    client, teardown, (_get_client, _audit, invalidate) = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            f"/admin/agents/{profile_a['profile_id']}/fork", follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:400]
        # Redirect lands on the new profile id so the operator can edit.
        loc = r.headers["location"]
        assert loc.startswith("/admin/agents?profile_id=")
        new_id = loc.split("=", 1)[1]
        assert new_id != profile_a["profile_id"]

        payloads = fake.insert_payloads("agent_profiles")
        assert len(payloads) == 1
        new_row = payloads[0]
        # Forced overrides win over copied values.
        assert new_row["status"] == "draft"
        assert new_row["environment"] == "staging"
        assert new_row["display_name"] == "Business (fork)"
        # Arrays are copied (deep-ish — same contents, different list object).
        assert new_row["tool_ids"] == ["tool-1", "tool-2"]
        assert new_row["tool_ids"] is not profile_a["tool_ids"]
        assert new_row["data_source_ids"] == ["src-1"]
        assert new_row["mcp_server_ids"] == ["mcp-1"]
        assert new_row["downstream_profile_ids"] == ["p-cccc"]
        # Scalar config (model, endpoint, parent) is preserved.
        assert new_row["model_main_id"] == "model-main-1"
        assert new_row["model_light_id"] == "model-light-1"
        assert new_row["endpoint_path"] == "/chat/stream"
        assert new_row["agent_kind"] == "business"
        # Slug must not collide with the source row on (kind, slug, env).
        assert new_row["slug"].startswith("business_default-fork-")
        assert new_row["slug"] != profile_a["slug"]
    finally:
        teardown()


def test_fork_invalidates_cache_for_source_and_dev(profile_a):
    fake = _FakeClient(rows_by_table={"agent_profiles": [profile_a]})
    client, teardown, (_get_client, _audit, invalidate) = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            f"/admin/agents/{profile_a['profile_id']}/fork", follow_redirects=False,
        )
        assert r.status_code == 302
        # invalidate is called twice: once for (kind, source_env) and
        # once for (kind, 'staging') so the new draft is visible immediately
        # in the admin's staging environment view.
        call_args = [c.args for c in invalidate.call_args_list]
        assert ("business", "prod") in call_args
        assert ("business", "staging") in call_args
    finally:
        teardown()


def test_fork_records_audit_entry(profile_a):
    fake = _FakeClient(rows_by_table={"agent_profiles": [profile_a]})
    client, teardown, (_get_client, audit, _invalidate) = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            f"/admin/agents/{profile_a['profile_id']}/fork", follow_redirects=False,
        )
        assert r.status_code == 302
        assert audit.called
        kwargs = audit.call_args.kwargs
        assert kwargs["entity_type"] == "agent_profiles"
        assert kwargs["action"] == "fork"
        assert kwargs["actor_user_id"] == "u-1"
        assert kwargs["before"]["profile_id"] == profile_a["profile_id"]
        assert kwargs["after"]["status"] == "draft"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Fork — error paths
# ---------------------------------------------------------------------------


def test_fork_missing_profile_returns_404():
    fake = _FakeClient(rows_by_table={"agent_profiles": []})
    client, teardown, _ = _make_app_with_fake_db(fake)
    try:
        r = client.post("/admin/agents/nope/fork", follow_redirects=False)
        assert r.status_code == 404
        assert "profile not found" in r.text.lower()
        # And we didn't write anything on the way out.
        assert not fake.insert_payloads("agent_profiles")
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Diff route — 404 + happy path render
# ---------------------------------------------------------------------------


def test_diff_missing_profile_returns_404(profile_a):
    fake = _FakeClient(rows_by_table={"agent_profiles": [profile_a]})
    client, teardown, _ = _make_app_with_fake_db(fake)
    try:
        r = client.get(
            f"/admin/agents/diff?a={profile_a['profile_id']}&b=does-not-exist",
            follow_redirects=False,
        )
        assert r.status_code == 404
        assert "does-not-exist" in r.text or "profile not found" in r.text.lower()
    finally:
        teardown()


def test_diff_renders_both_profiles(profile_a, profile_b):
    # Provide tools + data sources so label resolution doesn't 404.
    fake = _FakeClient(rows_by_table={
        "agent_profiles": [profile_a, profile_b],
        "agent_tools": [
            {"tool_id": "tool-1", "python_name": "fn_one"},
            {"tool_id": "tool-2", "python_name": "fn_two"},
            {"tool_id": "tool-3", "python_name": "fn_three"},
        ],
        "agent_data_sources": [
            {"source_id": "src-1", "name": "primary"},
            {"source_id": "src-2", "name": "secondary"},
        ],
        "agent_mcp_servers": [{"mcp_server_id": "mcp-1", "name": "mcp-alpha"}],
        "agent_prompts": [
            {"prompt_id": "pr-1", "agent_kind": "business", "slug": "system"},
        ],
        "agent_prompt_versions": [
            {"prompt_id": "pr-1", "version_number": 3, "created_at": "2026-01-01"},
        ],
    })
    client, teardown, _ = _make_app_with_fake_db(fake)
    try:
        r = client.get(
            f"/admin/agents/diff?a={profile_a['profile_id']}&b={profile_b['profile_id']}",
            follow_redirects=False,
        )
        assert r.status_code == 200, r.text[:400]
        body = r.text
        # Both ids surface in the header
        assert profile_a["profile_id"][:8] in body
        assert profile_b["profile_id"][:8] in body
        # Added / removed tool labels resolve to python_names in the body
        assert "fn_two" in body   # removed from B
        assert "fn_three" in body  # added in B
        # Scalar diff cell for model_main_id shows up
        assert "model_main_id" in body
    finally:
        teardown()


# ---------------------------------------------------------------------------
# compute_profile_diff — pure unit tests (no TestClient)
# ---------------------------------------------------------------------------


def test_compute_diff_self_diff_has_no_changes(profile_a):
    from admin.profile_fork_handlers import compute_profile_diff

    fake = _FakeClient(rows_by_table={
        "agent_tools": [
            {"tool_id": "tool-1", "python_name": "fn_one"},
            {"tool_id": "tool-2", "python_name": "fn_two"},
        ],
        "agent_data_sources": [{"source_id": "src-1", "name": "primary"}],
        "agent_mcp_servers": [{"mcp_server_id": "mcp-1", "name": "mcp-alpha"}],
        "agent_profiles": [{"profile_id": "p-cccc", "display_name": "child"}],
        "agent_prompts": [],
    })
    out = compute_profile_diff(fake, profile_a, profile_a)
    for scalar in out["scalars"]:
        assert scalar["equal"] is True, scalar
    for bucket_name in ("tools", "data_sources", "mcp_servers", "sub_agents"):
        bucket = out[bucket_name]
        assert bucket["added_in_b"] == []
        assert bucket["removed_in_b"] == []
        # Shared lists mirror the input arrays' contents.
    assert {it["id"] for it in out["tools"]["shared"]} == {"tool-1", "tool-2"}
    assert out["prompts"]["kind_mismatch"] is False


def test_compute_diff_cross_fork_classifies_arrays(profile_a, profile_b):
    from admin.profile_fork_handlers import compute_profile_diff

    fake = _FakeClient(rows_by_table={
        "agent_tools": [
            {"tool_id": "tool-1", "python_name": "fn_one"},
            {"tool_id": "tool-2", "python_name": "fn_two"},
            {"tool_id": "tool-3", "python_name": "fn_three"},
        ],
        "agent_data_sources": [
            {"source_id": "src-1", "name": "primary"},
            {"source_id": "src-2", "name": "secondary"},
        ],
        "agent_mcp_servers": [{"mcp_server_id": "mcp-1", "name": "mcp-alpha"}],
        "agent_profiles": [{"profile_id": "p-cccc", "display_name": "child"}],
        "agent_prompts": [],
    })
    out = compute_profile_diff(fake, profile_a, profile_b)

    # Tools: A=[1,2], B=[1,3] → removed=2, added=3, shared=1
    tools = out["tools"]
    assert [it["id"] for it in tools["added_in_b"]] == ["tool-3"]
    assert [it["id"] for it in tools["removed_in_b"]] == ["tool-2"]
    assert [it["id"] for it in tools["shared"]] == ["tool-1"]
    # Labels resolved
    assert tools["added_in_b"][0]["label"] == "fn_three"

    # Data sources: A=[src-1], B=[src-1, src-2] → added=src-2
    ds = out["data_sources"]
    assert [it["id"] for it in ds["added_in_b"]] == ["src-2"]
    assert ds["removed_in_b"] == []
    assert [it["id"] for it in ds["shared"]] == ["src-1"]

    # Scalars: at least the model_main_id and status + environment + display_name differ.
    by_field = {s["field"]: s for s in out["scalars"]}
    assert by_field["model_main_id"]["equal"] is False
    assert by_field["status"]["equal"] is False
    assert by_field["environment"]["equal"] is False
    assert by_field["display_name"]["equal"] is False
    # agent_kind is shared
    assert by_field["agent_kind"]["equal"] is True


def test_compute_diff_flags_kind_mismatch(profile_a):
    from admin.profile_fork_handlers import compute_profile_diff

    other_kind = {**profile_a, "profile_id": "p-zzzz", "agent_kind": "support_bot"}
    fake = _FakeClient(rows_by_table={"agent_prompts": []})
    out = compute_profile_diff(fake, profile_a, other_kind)
    assert out["prompts"]["kind_mismatch"] is True
    assert out["prompts"]["shared"] == []
