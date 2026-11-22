"""Tests for GET /admin/api/profiles/{id}/mentionables (Phase 3c Task 2).

Feeds the prompt-editor's @-dropdown: tools/mcps/kbs available to a profile,
derived from its MCP attachments (`_fetch_mcp_attachments`) and its
`data_source_ids` (looked up in `agent_data_sources`). Uses a minimal fake
Supabase client shaped to the two queries the handler actually issues:
  - agent_profiles.select(...).eq("profile_id", pid).limit(1).execute()
  - agent_data_sources.select(...).in_("source_id", ds_ids).execute()
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
# Minimal fake Supabase client — only the two tables the handler queries.
# The fake profile row is always keyed "p1"; querying any other profile_id
# (e.g. "nope") naturally yields no match, which is how the 404 test works
# without a separate "profile missing" flag.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table: str, profile_row: Optional[Dict[str, Any]], ds_rows: List[Dict[str, Any]]):
        self._table = table
        self._profile_row = profile_row
        self._ds_rows = ds_rows
        self._eq: Dict[str, Any] = {}
        self._in: Optional[tuple] = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def limit(self, *_a, **_k):
        return self

    def in_(self, col, vals):
        self._in = (col, list(vals))
        return self

    def execute(self):
        if self._table == "agent_profiles":
            if self._profile_row is not None and self._profile_row.get("profile_id") == self._eq.get("profile_id"):
                return _Result([self._profile_row])
            return _Result([])
        if self._table == "agent_data_sources":
            if self._in is not None:
                col, vals = self._in
                vals = set(vals)
                return _Result([r for r in self._ds_rows if r.get(col) in vals])
            return _Result(list(self._ds_rows))
        return _Result([])


class _FakeClient:
    def __init__(self, profile_row: Optional[Dict[str, Any]], ds_rows: List[Dict[str, Any]]):
        self._profile_row = profile_row
        self._ds_rows = ds_rows

    def table(self, name: str) -> _Query:
        return _Query(name, self._profile_row, self._ds_rows)


def _app(monkeypatch, attachments, data_source_ids, ds_rows):
    import admin.mentionables_handlers as h
    from admin.router import router
    from admin import auth

    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)

    monkeypatch.setattr(h, "_fetch_mcp_attachments", lambda c, pid: attachments)
    profile_row = {"profile_id": "p1", "data_source_ids": data_source_ids}
    monkeypatch.setattr(h, "admin_service_client", lambda: _FakeClient(profile_row, ds_rows))
    return TestClient(app)


def test_mentionables_lists_tools_mcps_kbs(monkeypatch):
    attachments = [{
        "mcp_server": {"mcp_server_id": "m1", "name": "Sales Docs", "source": "bundle"},
        "tool_subset": None, "position": 0,
        "member_tools": [{"tool": {"python_name": "get_clients"}, "context_fields": None, "position": 0},
                          {"tool": {"python_name": "search_docs"}, "context_fields": None, "position": 1}],
    }]
    client = _app(monkeypatch, attachments, ["ds1"], [{"source_id": "ds1", "name": "Onboarding Guide"}])
    r = client.get("/admin/api/profiles/p1/mentionables")
    assert r.status_code == 200
    body = r.json()
    assert {t["name"] for t in body["tools"]} == {"get_clients", "search_docs"}
    assert all(t["name"] == t["label"] for t in body["tools"])
    assert [m["name"] for m in body["mcps"]] == ["Sales Docs"]
    assert body["mcps"][0]["label"] == "Sales Docs"
    assert [k["name"] for k in body["kbs"]] == ["Onboarding Guide"]
    assert body["kbs"][0]["label"] == "Onboarding Guide"


def test_mentionables_exclude_tools_outside_the_attachment_subset(monkeypatch):
    """The dropdown must offer only tools that will actually BIND.

    An attachment's `tool_subset` narrows what the runtime resolver binds
    (runtime/tools/_mcp_resolver.py); listing every member tool regardless
    offered names that never reach the LLM — and the promote gate then blocked
    the profile with prompt_tool_mismatch for mentioning exactly what this
    dropdown suggested. Discriminates: the unfiltered handler returns
    {get_clients, search_docs, export_csv} here and this fails.
    """
    attachments = [{
        "mcp_server": {"mcp_server_id": "m1", "name": "Sales Docs", "source": "bundle"},
        "tool_subset": ["get_clients", "search_docs"], "position": 0,
        "member_tools": [
            {"tool": {"python_name": "get_clients"}, "context_fields": None, "position": 0},
            {"tool": {"python_name": "search_docs"}, "context_fields": None, "position": 1},
            {"tool": {"python_name": "export_csv"}, "context_fields": None, "position": 2},
        ],
    }]
    client = _app(monkeypatch, attachments, [], [])
    r = client.get("/admin/api/profiles/p1/mentionables")
    assert r.status_code == 200
    body = r.json()
    assert {t["name"] for t in body["tools"]} == {"get_clients", "search_docs"}
    # The MCP itself is still mentionable — only its unbound tools are not.
    assert [m["name"] for m in body["mcps"]] == ["Sales Docs"]


def test_mentionables_dedupes_tools_across_attachments(monkeypatch):
    attachments = [
        {
            "mcp_server": {"mcp_server_id": "m1", "name": "Sales Docs", "source": "bundle"},
            "tool_subset": None, "position": 0,
            "member_tools": [{"tool": {"python_name": "search_docs"}, "context_fields": None, "position": 0}],
        },
        {
            "mcp_server": {"mcp_server_id": "m2", "name": "Support Docs", "source": "bundle"},
            "tool_subset": None, "position": 1,
            "member_tools": [{"tool": {"python_name": "search_docs"}, "context_fields": None, "position": 0}],
        },
    ]
    client = _app(monkeypatch, attachments, [], [])
    r = client.get("/admin/api/profiles/p1/mentionables")
    assert r.status_code == 200
    body = r.json()
    assert [t["name"] for t in body["tools"]] == ["search_docs"]
    assert [m["name"] for m in body["mcps"]] == ["Sales Docs", "Support Docs"]
    assert body["kbs"] == []


def test_mentionables_404_missing_profile(monkeypatch):
    client = _app(monkeypatch, [], [], [])  # fake profile row is "p1"; "nope" won't match
    r = client.get("/admin/api/profiles/nope/mentionables")
    assert r.status_code == 404


def test_mentionables_attachment_failure_degrades_to_empty(monkeypatch):
    import admin.mentionables_handlers as h
    from admin.router import router
    from admin import auth

    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)

    def _boom(client, pid):
        raise RuntimeError("db down")

    monkeypatch.setattr(h, "_fetch_mcp_attachments", _boom)
    profile_row = {"profile_id": "p1", "data_source_ids": ["ds1"]}
    monkeypatch.setattr(
        h, "admin_service_client",
        lambda: _FakeClient(profile_row, [{"source_id": "ds1", "name": "Onboarding Guide"}]),
    )
    client = TestClient(app)
    r = client.get("/admin/api/profiles/p1/mentionables")
    assert r.status_code == 200
    body = r.json()
    assert body["tools"] == []
    assert body["mcps"] == []
    # kb lookup is independent of attachment lookup — still succeeds.
    assert [k["name"] for k in body["kbs"]] == ["Onboarding Guide"]


def test_mentionables_requires_superuser():
    from admin.router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    r = client.get("/admin/api/profiles/p1/mentionables")
    assert r.status_code in (401, 403)
