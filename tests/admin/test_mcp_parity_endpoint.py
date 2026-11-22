"""Tests for GET /admin/api/mcp-flag-parity (MCP-resolution health report).

For each active agent profile in the selected env the handler resolves the tool
set the runtime way (resolve_mcp_tools_for_profile) and reports the bound tool
names, attachment count, and any resolution error. These tests mock the pool
builder, the MCP resolver, and the profile loader so they are deterministic and
need no DB.

Note: the handler resolves the pool builder + resolver via *local* imports inside
the request, so we monkeypatch them at their SOURCE modules (admin.sync_tools,
runtime.tools._mcp_resolver); the module-level `admin_service_client` /
`AgentProfileLoader` are patched on the handler module itself.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


class _Tool:
    def __init__(self, name: str):
        self.name = name


class _Profile:
    def __init__(self, agent_kind: str, mcp_attachments: Optional[List[Any]] = None):
        self.agent_kind = agent_kind
        self.mcp_attachments = mcp_attachments or []


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self._env: Optional[str] = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        if col == "environment":
            self._env = val
        return self

    def execute(self):
        return _Result([r for r in self._rows if self._env is None or r.get("environment") == self._env])


class _FakeClient:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def table(self, name: str) -> _Query:
        return _Query(self._rows if name == "agent_profiles" else [])


def _app(
    monkeypatch,
    rows: List[Dict[str, Any]],
    mcp_by_kind: Dict[str, List[str]],
    attachments_by_kind: Optional[Dict[str, List[Any]]] = None,
    bad_kinds: tuple = (),
) -> TestClient:
    import admin.mcp_parity_handlers as h
    from admin.router import router
    from admin import auth

    attachments_by_kind = attachments_by_kind or {}

    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)

    # env-aware client + profile loader are imported at module level → patch here.
    monkeypatch.setattr(h, "admin_service_client", lambda: _FakeClient(rows))

    class _FakeLoader:
        @staticmethod
        def load_active(agent_kind, env):
            if agent_kind in bad_kinds:
                raise RuntimeError(f"load blew up for {agent_kind}")
            return _Profile(agent_kind, attachments_by_kind.get(agent_kind))

    monkeypatch.setattr(h, "AgentProfileLoader", _FakeLoader)

    # Resolver + pool builder are imported LOCALLY in the handler → patch at source.
    monkeypatch.setattr("admin.sync_tools.pool_for_kind", lambda kind: [])
    monkeypatch.setattr(
        "runtime.tools._mcp_resolver.resolve_mcp_tools_for_profile",
        lambda profile, ctx, pool: [_Tool(n) for n in mcp_by_kind.get(profile.agent_kind, [])],
    )
    return TestClient(app)


def test_health_endpoint_reports_bound_tools(monkeypatch):
    rows = [{"profile_id": "p1", "agent_kind": "sales", "status": "active", "environment": "staging"}]
    client = _app(
        monkeypatch, rows,
        mcp_by_kind={"sales": ["search_docs", "get_clients"]},  # unsorted input
        attachments_by_kind={"sales": [{"x": 1}, {"y": 2}]},
    )
    r = client.get("/admin/api/mcp-flag-parity")
    assert r.status_code == 200
    body = r.json()
    assert body["env"] == "staging"
    assert body["total"] == 1
    assert body["errors"] == 0
    row = body["results"][0]
    assert row["error"] is None
    assert row["attachments"] == 2
    assert row["tools"] == ["get_clients", "search_docs"]  # name-sorted


def test_health_endpoint_reports_zero_tools_when_unattached(monkeypatch):
    # A profile with no attachments resolves to zero tools — not an error.
    rows = [{"profile_id": "p1", "agent_kind": "business", "status": "active", "environment": "staging"}]
    client = _app(
        monkeypatch, rows,
        mcp_by_kind={"business": []},
    )
    r = client.get("/admin/api/mcp-flag-parity")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["errors"] == 0
    row = body["results"][0]
    assert row["error"] is None
    assert row["tools"] == []


def test_health_endpoint_degrades_on_bad_profile(monkeypatch):
    # p_ok resolves cleanly; p_bad raises during load → reported with error,
    # endpoint still 200s and includes both.
    rows = [
        {"profile_id": "p_ok", "agent_kind": "sales", "status": "active", "environment": "staging"},
        {"profile_id": "p_bad", "agent_kind": "support_bot", "status": "active", "environment": "staging"},
    ]
    client = _app(
        monkeypatch, rows,
        mcp_by_kind={"sales": ["a"]},
        bad_kinds=("support_bot",),
    )
    r = client.get("/admin/api/mcp-flag-parity")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["errors"] == 1
    by_id = {row["profile_id"]: row for row in body["results"]}
    assert by_id["p_ok"]["error"] is None
    assert by_id["p_ok"]["tools"] == ["a"]
    bad = by_id["p_bad"]
    assert bad["error"]
    assert bad["tools"] == []


def test_health_endpoint_sorts_errored_first(monkeypatch):
    rows = [
        {"profile_id": "p_ok", "agent_kind": "sales", "status": "active", "environment": "staging"},
        {"profile_id": "p_bad", "agent_kind": "support_bot", "status": "active", "environment": "staging"},
    ]
    client = _app(
        monkeypatch, rows,
        mcp_by_kind={"sales": ["a"]},
        bad_kinds=("support_bot",),
    )
    r = client.get("/admin/api/mcp-flag-parity")
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == 1
    # Errored profile floats to the top.
    assert body["results"][0]["profile_id"] == "p_bad"


def test_health_endpoint_requires_superuser():
    from admin.router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    r = client.get("/admin/api/mcp-flag-parity")
    assert r.status_code in (401, 403)
