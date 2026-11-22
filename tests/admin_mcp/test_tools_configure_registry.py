"""Tests for the Task 11 multiplexer tools: models, tools, mcp_servers,
kb_sources. Same FakeClient pattern as test_tools_configure.py."""
from __future__ import annotations

import pytest

import admin_mcp.tools_configure as tc
from tests.admin_mcp.conftest import FakeClient

MODEL = {"model_id": "m1", "name": "flash", "provider": "google_genai",
         "model_string": "gemini-3-flash", "environment": "staging",
         "temperature": 0.2, "max_output_tokens": 1024, "extra_config": {},
         "is_active": True}

HTTP_TOOL = {"tool_id": "t1", "python_name": "get_weather",
             "display_name": "Get weather", "description": "desc",
             "category": "http", "scope": "read", "agent_kind": "sample_agent",
             "additional_agent_kinds": [], "kind": "http",
             "endpoint_url": "https://api.example.com/weather",
             "http_method": "POST", "auth_env_var": None,
             "timeout_seconds": 30, "argument_schema": {"type": "object"},
             "source_file": "__portal__", "is_deprecated": False}

MCP_SERVER = {"mcp_server_id": "s1", "name": "search", "source": "external",
              "is_builtin": False, "transport": "http",
              "endpoint": "https://mcp.example.com", "auth_env_var": None,
              "tool_subset": None, "context_fields": {}, "usage_guidance": None,
              "is_active": True, "timeout_seconds": 30}

BUNDLE_SERVER = {"mcp_server_id": "b1", "name": "toolkit", "source": "bundle",
                 "is_builtin": False, "transport": None, "endpoint": None,
                 "auth_env_var": None, "tool_subset": None,
                 "context_fields": {}, "usage_guidance": None,
                 "is_active": True, "timeout_seconds": 30}

TOOL_A = {"tool_id": "ta", "python_name": "tool_a", "is_deprecated": False}
TOOL_B = {"tool_id": "tb", "python_name": "tool_b", "is_deprecated": False}

KB_SOURCE = {"source_id": "k1", "name": "docs", "kind": "pgvector",
             "agent_kind": "sample_agent", "config": {"table": "ai_kb_embeddings",
             "pages_table": "ai_kb_pages"}, "is_active": True}

PROFILE = {"profile_id": "p1", "agent_kind": "sample_agent", "slug": "main",
           "environment": "staging", "mcp_server_ids": ["s1"],
           "data_source_ids": ["k1"]}


def _fake(monkeypatch, tables):
    client = FakeClient(tables)
    monkeypatch.setattr(tc, "client_for_env", lambda env: client)
    return client


def _noaudit(monkeypatch):
    monkeypatch.setattr(tc, "actor_audit", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def test_models_list(monkeypatch):
    _fake(monkeypatch, {"agent_models": [MODEL]})
    out = tc.models(action="list", env="staging")
    assert out["models"][0]["model_id"] == "m1"


def test_models_create_rejects_unsupported_provider(monkeypatch):
    _fake(monkeypatch, {"agent_models": []})
    out = tc.models(action="create", name="x", provider="bedrock",
                    model_string="foo")
    assert "error" in out
    assert "bedrock" in out["error"]


def test_models_create_success(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_models": []})
    out = tc.models(action="create", name="new-model", provider="google_genai",
                    model_string="gemini-3-flash", env="staging")
    assert out["created"] is True
    assert client.table("agent_models").inserted[0]["environment"] == "staging"


def test_models_promote_idempotent_when_exists(monkeypatch):
    existing = {"model_id": "m2", "name": "flash", "environment": "prod"}
    _fake(monkeypatch, {"agent_models": [MODEL, existing]})
    out = tc.models(action="promote", model_id="m1", target_env="prod")
    assert out["promoted"] is False
    assert out["already_exists"] is True
    assert out["model_id"] == "m2"


def test_models_promote_creates_new_row(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_models": [MODEL]})
    out = tc.models(action="promote", model_id="m1", target_env="prod")
    assert out["promoted"] is True
    inserted = client.table("agent_models").inserted[-1]
    assert inserted["environment"] == "prod"
    assert inserted["name"] == "flash"


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def test_tools_create_rejects_bad_python_name(monkeypatch):
    _fake(monkeypatch, {"agent_tools": []})
    out = tc.tools(action="create", python_name="Bad Name!", description="x",
                   endpoint_url="https://x.com")
    assert "error" in out
    assert "python_name" in out["error"]


def test_tools_create_requires_agent_kind(monkeypatch):
    # Portal parity: agent_kind is required and must be a known kind (an empty
    # agent_kind is rejected, not silently stored as NULL).
    _fake(monkeypatch, {"agent_tools": []})
    out = tc.tools(action="create", python_name="get_weather", description="x",
                   endpoint_url="https://x.com",
                   argument_schema={"type": "object"})
    assert "error" in out and "agent_kind" in out["error"]


def test_tools_create_requires_argument_schema(monkeypatch):
    # Portal parity: an argument_schema must be supplied ({} is fine; omission
    # is not).
    _fake(monkeypatch, {"agent_tools": []})
    out = tc.tools(action="create", python_name="get_weather", description="x",
                   endpoint_url="https://x.com", agent_kind="sample_agent")
    assert "error" in out and "argument_schema" in out["error"]


def test_tools_create_success_sets_http_defaults(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_tools": []})
    monkeypatch.setattr("core.agent_profile.AgentProfileLoader.invalidate",
                        staticmethod(lambda *a, **k: None))
    out = tc.tools(action="create", python_name="get_weather",
                   description="fetches weather", endpoint_url="https://api.x.com",
                   agent_kind="sample_agent",
                   argument_schema={"type": "object", "properties": {}})
    assert out["created"] is True
    inserted = client.table("agent_tools").inserted[0]
    assert inserted["kind"] == "http"
    assert inserted["source_file"] == "__portal__"
    assert inserted["http_method"] == "POST"
    assert inserted["timeout_seconds"] == 30
    assert inserted["scope"] == "read"
    assert inserted["category"] == "http"


def test_tools_delete_without_confirm_returns_not_confirmed(monkeypatch):
    _fake(monkeypatch, {"agent_tools": [HTTP_TOOL]})
    out = tc.tools(action="delete", tool_id="t1")
    assert out["confirmed"] is False
    assert "would" in out


def test_tools_delete_with_confirm_deprecates(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_tools": [dict(HTTP_TOOL)]})
    monkeypatch.setattr("core.agent_profile.AgentProfileLoader.invalidate",
                        staticmethod(lambda *a, **k: None))
    out = tc.tools(action="delete", tool_id="t1", confirm=True)
    assert out["deleted"] is True
    row = client.table("agent_tools")._data[0]
    assert row["is_deprecated"] is True


def test_tools_update_metadata_only(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_tools": [dict(HTTP_TOOL)]})
    monkeypatch.setattr("core.agent_profile.AgentProfileLoader.invalidate",
                        staticmethod(lambda *a, **k: None))
    out = tc.tools(action="update", tool_id="t1", display_name="New name")
    assert out["updated"] is True
    assert client.table("agent_tools")._data[0]["display_name"] == "New name"


def test_tools_dry_run_calls_shared_helper(monkeypatch):
    _fake(monkeypatch, {"agent_tools": [HTTP_TOOL]})
    seen = {}

    def fake_dry_run(payload, *, actor_user_id=None):
        seen["payload"] = payload
        seen["actor_user_id"] = actor_user_id
        return {"ok": True, "http_status": 200}

    import admin.tool_handlers as th
    monkeypatch.setattr(th, "run_tool_dry_run", fake_dry_run)
    out = tc.tools(action="dry_run", tool_id="t1", args={"q": "hi"})
    assert out == {"ok": True, "http_status": 200}
    assert seen["payload"]["endpoint_url"] == HTTP_TOOL["endpoint_url"]
    assert seen["payload"]["args"] == {"q": "hi"}
    assert seen["actor_user_id"]


# ---------------------------------------------------------------------------
# mcp_servers
# ---------------------------------------------------------------------------


def test_mcp_servers_create_validates_transport(monkeypatch):
    _fake(monkeypatch, {"agent_mcp_servers": []})
    out = tc.mcp_servers(action="create", source="external", name="s",
                         transport="carrier-pigeon", endpoint="https://x.com")
    assert "error" in out
    assert "transport" in out["error"]


def test_mcp_servers_create_requires_valid_source(monkeypatch):
    _fake(monkeypatch, {"agent_mcp_servers": []})
    out = tc.mcp_servers(action="create", name="s", endpoint="https://x.com")
    assert "error" in out
    assert "source" in out["error"]


def test_mcp_servers_create_external_success(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_mcp_servers": []})
    out = tc.mcp_servers(action="create", source="external", name="search",
                         transport="http", endpoint="https://mcp.example.com")
    assert out["created"] is True
    assert out["source"] == "external"
    inserted = client.table("agent_mcp_servers").inserted[0]
    assert inserted["source"] == "external"
    assert inserted["is_builtin"] is False
    assert inserted["transport"] == "http"


def test_mcp_servers_create_rejects_builtin_prefix(monkeypatch):
    _fake(monkeypatch, {"agent_mcp_servers": []})
    out = tc.mcp_servers(action="create", source="external",
                         name="__builtin__:reserved", endpoint="https://x.com")
    assert "error" in out
    assert "__builtin__" in out["error"]


def test_mcp_servers_create_bundle_with_members(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_mcp_servers": [],
        "agent_mcp_tools": [],
        "agent_tools": [dict(TOOL_A), dict(TOOL_B)],
    })
    out = tc.mcp_servers(action="create", source="bundle", name="toolkit",
                         member_tool_ids=["ta", "tb"])
    assert out["created"] is True
    assert out["mcp_server"]["source"] == "bundle"
    assert "endpoint" not in client.table("agent_mcp_servers").inserted[0]
    # Members are seeded through reconcile_bundle_members (upsert), not a
    # per-row insert loop.
    members = client.table("agent_mcp_tools").upserted
    assert len(members) == 2
    assert members[0]["tool_id"] == "ta" and members[0]["position"] == 0
    assert members[1]["tool_id"] == "tb" and members[1]["position"] == 1


def test_mcp_servers_create_bundle_bad_member_id_leaves_no_orphan_row(monkeypatch):
    """A bad member id must be caught BEFORE the server row is inserted —
    otherwise a retry after fixing the id hits UNIQUE(name) on an orphaned,
    un-audited row (the bug in commit 785a73ddb)."""
    audit_calls = []
    monkeypatch.setattr(tc, "actor_audit", lambda *a, **k: audit_calls.append((a, k)))
    client = _fake(monkeypatch, {
        "agent_mcp_servers": [],
        "agent_mcp_tools": [],
        "agent_tools": [dict(TOOL_A)],
    })
    out = tc.mcp_servers(action="create", source="bundle", name="toolkit",
                         member_tool_ids=["ta", "bogus"])
    assert "error" in out
    assert "bogus" in out["error"]
    # No orphaned server row for the rejected bundle name.
    assert client.table("agent_mcp_servers")._data == []
    assert client.table("agent_mcp_tools")._data == []
    # No audit entry recorded for a create that never happened.
    assert audit_calls == []


def test_mcp_servers_create_duplicate_name_returns_friendly_error(monkeypatch):
    """A UNIQUE(name) violation from PostgREST must come back as a clean
    err(), not raise through the MCP call (mirrors the portal's
    mcp_connect_create/mcp_create_bundle try/except)."""
    client = _fake(monkeypatch, {"agent_mcp_servers": []})
    table = client.table("agent_mcp_servers")

    def _raise(*a, **k):
        raise Exception('duplicate key value violates unique constraint '
                        '"agent_mcp_servers_name_key"')
    monkeypatch.setattr(table, "execute", _raise)

    out = tc.mcp_servers(action="create", source="external", name="search",
                         transport="http", endpoint="https://mcp.example.com")
    assert "error" in out
    assert "already exists" in out["error"]


def test_mcp_servers_update_refuses_builtin(monkeypatch):
    _noaudit(monkeypatch)
    builtin = dict(MCP_SERVER, mcp_server_id="s1", is_builtin=True)
    client = _fake(monkeypatch, {"agent_mcp_servers": [builtin]})
    out = tc.mcp_servers(action="update", mcp_server_id="s1", name="renamed")
    assert "error" in out
    assert "built-in" in out["error"]
    assert client.table("agent_mcp_servers").updated == []


def test_mcp_servers_delete_without_confirm(monkeypatch):
    _fake(monkeypatch, {"agent_mcp_servers": [MCP_SERVER]})
    out = tc.mcp_servers(action="delete", mcp_server_id="s1")
    assert out["confirmed"] is False


def test_mcp_servers_delete_cascades(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_mcp_servers": [dict(MCP_SERVER)],
        "agent_mcp_tools": [{"mcp_server_id": "s1", "tool_id": "ta", "position": 0}],
        "agent_profile_mcp_servers": [{"profile_id": "p1", "mcp_server_id": "s1",
                                       "tool_subset": None, "position": 0}],
        "agent_profiles": [dict(PROFILE)],
    })
    out = tc.mcp_servers(action="delete", mcp_server_id="s1", confirm=True)
    assert out["deleted"] is True
    assert client.table("agent_mcp_servers")._data == []
    assert client.table("agent_mcp_tools")._data == []
    assert client.table("agent_profile_mcp_servers")._data == []
    assert client.table("agent_profiles")._data[0]["mcp_server_ids"] == []


def test_mcp_servers_members_reinserts_in_order(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_mcp_servers": [dict(BUNDLE_SERVER)],
        "agent_mcp_tools": [],
        "agent_tools": [dict(TOOL_A), dict(TOOL_B)],
    })
    out = tc.mcp_servers(action="members", mcp_server_id="b1",
                         member_tool_ids=["tb", "ta"])
    assert out["updated"] is True
    assert out["members"] == ["tb", "ta"]
    final = client.table("agent_mcp_tools")._data
    assert [(r["tool_id"], r["position"]) for r in final] == [("tb", 0), ("ta", 1)]


def test_mcp_servers_members_refuses_external(monkeypatch):
    _fake(monkeypatch, {
        "agent_mcp_servers": [dict(MCP_SERVER)],
        "agent_tools": [dict(TOOL_A)],
    })
    out = tc.mcp_servers(action="members", mcp_server_id="s1",
                         member_tool_ids=["ta"])
    assert "error" in out
    assert "bundle" in out["error"]


def test_mcp_servers_get_returns_members_and_attached_count(monkeypatch):
    _fake(monkeypatch, {
        "agent_mcp_servers": [dict(BUNDLE_SERVER)],
        "agent_mcp_tools": [{"mcp_server_id": "b1", "tool_id": "ta", "position": 0},
                            {"mcp_server_id": "b1", "tool_id": "tb", "position": 1}],
        "agent_tools": [dict(TOOL_A), dict(TOOL_B)],
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "b1", "position": 0},
            {"profile_id": "p2", "mcp_server_id": "b1", "position": 0},
        ],
    })
    out = tc.mcp_servers(action="get", mcp_server_id="b1")
    assert out["mcp_server"]["mcp_server_id"] == "b1"
    assert out["attached_count"] == 2
    assert out["members"] == [
        {"tool_id": "ta", "python_name": "tool_a", "position": 0},
        {"tool_id": "tb", "python_name": "tool_b", "position": 1},
    ]


def test_mcp_servers_discover_success(monkeypatch):
    import runtime.tools._http_adapter as http_adapter
    import runtime.tools._mcp_adapter as mcp_adapter
    monkeypatch.setattr(http_adapter, "_classify_endpoint", lambda ep: "public")
    monkeypatch.setattr(mcp_adapter, "list_tools_cached",
                        lambda ep, token: [{"name": "get_weather",
                                            "description": "d",
                                            "inputSchema": {"type": "object"}}])
    out = tc.mcp_servers(action="discover", endpoint="https://mcp.example.com")
    assert out["ok"] is True
    assert out["tools"] == [{"name": "get_weather", "description": "d",
                             "inputSchema": {"type": "object"}}]


def test_mcp_servers_discover_blocks_local_endpoint(monkeypatch):
    import runtime.tools._http_adapter as http_adapter
    monkeypatch.delenv("MCP_ALLOW_LOCAL_ENDPOINTS", raising=False)
    monkeypatch.setattr(http_adapter, "_classify_endpoint", lambda ep: "blocked")
    out = tc.mcp_servers(action="discover", endpoint="http://127.0.0.1:9999")
    assert out["ok"] is False
    assert "error" in out


# ---------------------------------------------------------------------------
# get_profile — mcp_attachments (#1222 join, alongside the legacy mcp_servers
# label list)
# ---------------------------------------------------------------------------


def test_get_profile_surfaces_mcp_attachments(monkeypatch):
    _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE)],
        "agent_mcp_servers": [dict(MCP_SERVER)],
    })
    fake_attachments = [{
        "mcp_server": {"mcp_server_id": "b1", "name": "toolkit",
                       "source": "bundle", "is_builtin": False},
        "tool_subset": None,
        "position": 0,
        "member_tools": [{"tool": {"python_name": "get_weather"},
                          "context_fields": {}, "position": 0}],
    }]
    import core.agent_profile as agent_profile
    monkeypatch.setattr(agent_profile, "_fetch_mcp_attachments",
                        lambda client, profile_id: fake_attachments)
    out = tc.get_profile(profile_id="p1")
    assert out["mcp_attachments"] == [{
        "mcp_server_id": "b1", "name": "toolkit", "source": "bundle",
        "is_builtin": False, "tool_subset": None, "position": 0,
        "member_tools": ["get_weather"],
    }]
    # Legacy label list still present (coexistence during dark-launch).
    assert out["mcp_servers"] == [{"id": "s1", "label": "search"}]


# ---------------------------------------------------------------------------
# kb_sources
# ---------------------------------------------------------------------------


def test_kb_sources_create_defaults_pgvector_config(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_data_sources": []})
    out = tc.kb_sources(action="create", name="docs", kind="pgvector")
    assert out["created"] is True
    inserted = client.table("agent_data_sources").inserted[0]
    assert inserted["config"] == {"table": "ai_kb_embeddings",
                                  "pages_table": "ai_kb_pages"}


def test_kb_sources_delete_detaches_from_profiles(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_data_sources": [dict(KB_SOURCE)],
        "agent_profiles": [dict(PROFILE)],
    })
    out = tc.kb_sources(action="delete", source_id="k1", confirm=True)
    assert out["deleted"] is True
    assert client.table("agent_data_sources")._data == []
    assert client.table("agent_profiles")._data[0]["data_source_ids"] == []


def test_kb_sources_upload_document_rejects_non_pgvector(monkeypatch):
    other = dict(KB_SOURCE, source_id="k2", kind="gitbook")
    _fake(monkeypatch, {"agent_data_sources": [other]})
    out = tc.kb_sources(action="upload_document", source_id="k2",
                        filename="a.md", content="hello")
    assert "error" in out


def test_kb_sources_upload_document_queues_tracked_job(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_data_sources": [dict(KB_SOURCE)],
        "ai_kb_pages": [],
    })

    seen = {}

    def fake_start_tracked_job(fn, *, kind, label, actor_user_id,
                               related_entity_type, related_entity_id,
                               metadata, **fn_kwargs):
        seen["kind"] = kind
        seen["fn_kwargs"] = fn_kwargs
        return "job-1"

    import admin._jobs as jobs
    monkeypatch.setattr(jobs, "start_tracked_job", fake_start_tracked_job)

    out = tc.kb_sources(action="upload_document", source_id="k1",
                        filename="guide.md", content="# hello world")
    assert out["queued"] is True
    assert out["job_id"] == "job-1"
    assert seen["kind"] == "kb_upload"
    assert seen["fn_kwargs"]["table_name"] == "ai_kb_embeddings"
    # Shared KB (config.table == ai_kb_embeddings) stamps source_id onto the
    # page row — replicates kb_handlers._is_shared_kb's condition exactly.
    page_row = client.table("ai_kb_pages")._data[0]
    assert page_row["source_id"] == "k1"


def test_kb_sources_unknown_action(monkeypatch):
    _fake(monkeypatch, {})
    out = tc.kb_sources(action="bogus")
    assert "error" in out


# ---------------------------------------------------------------------------
# tools — usage_guidance / context_fields (new agent_tools columns)
# ---------------------------------------------------------------------------


def test_tools_create_includes_usage_guidance_and_context_fields(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_tools": []})
    monkeypatch.setattr("core.agent_profile.AgentProfileLoader.invalidate",
                        staticmethod(lambda *a, **k: None))
    out = tc.tools(action="create", python_name="get_weather",
                   description="fetches weather", endpoint_url="https://api.x.com",
                   agent_kind="sample_agent",
                   argument_schema={"type": "object", "properties": {}},
                   usage_guidance="Call this before quoting a forecast.",
                   context_fields={"unit": "celsius"})
    assert out["created"] is True
    inserted = client.table("agent_tools").inserted[0]
    assert inserted["usage_guidance"] == "Call this before quoting a forecast."
    assert inserted["context_fields"] == {"unit": "celsius"}


def test_tools_create_omits_usage_guidance_and_context_fields_when_unset(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_tools": []})
    monkeypatch.setattr("core.agent_profile.AgentProfileLoader.invalidate",
                        staticmethod(lambda *a, **k: None))
    out = tc.tools(action="create", python_name="get_weather",
                   description="fetches weather", endpoint_url="https://api.x.com",
                   agent_kind="sample_agent",
                   argument_schema={"type": "object", "properties": {}})
    assert out["created"] is True
    inserted = client.table("agent_tools").inserted[0]
    assert "usage_guidance" not in inserted
    assert "context_fields" not in inserted


def test_tools_update_sets_usage_guidance_and_context_fields(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_tools": [dict(HTTP_TOOL)]})
    monkeypatch.setattr("core.agent_profile.AgentProfileLoader.invalidate",
                        staticmethod(lambda *a, **k: None))
    out = tc.tools(action="update", tool_id="t1",
                   usage_guidance="Only for logged-in users.",
                   context_fields={"scope": "session"})
    assert out["updated"] is True
    row = client.table("agent_tools")._data[0]
    assert row["usage_guidance"] == "Only for logged-in users."
    assert row["context_fields"] == {"scope": "session"}


# ---------------------------------------------------------------------------
# profile_mcp_attachments
# ---------------------------------------------------------------------------

ATTACHABLE_M1 = {"mcp_server_id": "m1", "name": "search-mcp", "source": "external",
                 "is_builtin": False, "tool_names": ["ta", "tb"]}
ATTACHABLE_M2 = {"mcp_server_id": "m2", "name": "toolkit-mcp", "source": "bundle",
                 "is_builtin": False, "tool_names": ["tc"]}


def _not_scoped(monkeypatch):
    import core.deployment as dep
    monkeypatch.setattr(dep, "scoped_build", lambda: False)


def test_profile_mcp_attachments_list(monkeypatch):
    _not_scoped(monkeypatch)
    _fake(monkeypatch, {"agent_profiles": [dict(PROFILE)]})

    import admin.screens.agents as agents_mod
    monkeypatch.setattr(agents_mod, "_attachable_mcps",
                        lambda client, visible_profiles=None: [ATTACHABLE_M1, ATTACHABLE_M2])

    fake_current = [{
        "mcp_server": {"mcp_server_id": "m1", "name": "search-mcp",
                       "source": "external", "is_builtin": False},
        "tool_subset": ["ta"], "position": 0, "member_tools": [],
    }]
    import core.agent_profile as agent_profile
    monkeypatch.setattr(agent_profile, "_fetch_mcp_attachments",
                        lambda client, profile_id: fake_current)

    out = tc.profile_mcp_attachments(profile_id="p1", action="list")
    assert out["attachable"] == [ATTACHABLE_M1, ATTACHABLE_M2]
    assert out["attachments"] == [{
        "mcp_server_id": "m1", "name": "search-mcp", "source": "external",
        "is_builtin": False, "tool_subset": ["ta"], "position": 0,
        "member_tools": [],
    }]


def test_profile_mcp_attachments_set_happy_path(monkeypatch):
    _not_scoped(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE)],
        "agent_profile_mcp_servers": [],
    })
    import admin.screens.agents as agents_mod
    monkeypatch.setattr(agents_mod, "_attachable_mcps",
                        lambda client, visible_profiles=None: [ATTACHABLE_M1])

    invalidated = []
    import admin.services.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "invalidate_profile_loader",
                        lambda agent_kind, environment: invalidated.append(
                            (agent_kind, environment)))

    audit_calls = []
    monkeypatch.setattr(tc, "actor_audit",
                        lambda *a, **k: audit_calls.append((a, k)))

    out = tc.profile_mcp_attachments(
        profile_id="p1", action="set",
        attachments=[{"mcp_server_id": "m1", "tool_subset": ["ta"]}])

    assert out["updated"] is True
    assert out["attachments"] == ["m1"]
    # Written via reconcile_profile_mcp_attachments (upsert), not insert.
    upserted = client.table("agent_profile_mcp_servers").upserted
    assert upserted == [{"profile_id": "p1", "mcp_server_id": "m1",
                         "tool_subset": ["ta"], "position": 0}]
    assert invalidated == [("sample_agent", "staging")]
    assert audit_calls
    (args, kwargs) = audit_calls[0]
    assert args[0] == "agent_profile_mcp_servers"
    assert args[2] == "update"


def test_profile_mcp_attachments_set_rejects_non_attachable(monkeypatch):
    _not_scoped(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE)],
        "agent_profile_mcp_servers": [],
    })
    import admin.screens.agents as agents_mod
    monkeypatch.setattr(agents_mod, "_attachable_mcps",
                        lambda client, visible_profiles=None: [ATTACHABLE_M1])

    out = tc.profile_mcp_attachments(
        profile_id="p1", action="set",
        attachments=[{"mcp_server_id": "not-attachable"}])
    assert "error" in out
    assert "not-attachable" in out["error"]
    assert client.table("agent_profile_mcp_servers")._data == []
    assert client.table("agent_profile_mcp_servers").inserted == []


def test_profile_mcp_attachments_set_rejects_bad_tool_subset(monkeypatch):
    _not_scoped(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE)],
        "agent_profile_mcp_servers": [],
    })
    import admin.screens.agents as agents_mod
    monkeypatch.setattr(agents_mod, "_attachable_mcps",
                        lambda client, visible_profiles=None: [ATTACHABLE_M1])

    out = tc.profile_mcp_attachments(
        profile_id="p1", action="set",
        attachments=[{"mcp_server_id": "m1", "tool_subset": ["bogus"]}])
    assert "error" in out
    assert "bogus" in out["error"]
    assert client.table("agent_profile_mcp_servers")._data == []
    assert client.table("agent_profile_mcp_servers").inserted == []


def test_profile_mcp_attachments_set_detaches_omitted(monkeypatch):
    """set REPLACES the attachment set — an MCP not in the posted list gets
    detached, even though it isn't rejected by either guard."""
    _not_scoped(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE)],
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "m2",
             "tool_subset": None, "position": 0},
        ],
    })
    import admin.screens.agents as agents_mod
    monkeypatch.setattr(agents_mod, "_attachable_mcps",
                        lambda client, visible_profiles=None: [ATTACHABLE_M1, ATTACHABLE_M2])
    import admin.services.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "invalidate_profile_loader", lambda *a, **k: None)
    _noaudit(monkeypatch)

    out = tc.profile_mcp_attachments(
        profile_id="p1", action="set",
        attachments=[{"mcp_server_id": "m1"}])
    assert out["updated"] is True
    rows_ = client.table("agent_profile_mcp_servers")._data
    assert [r["mcp_server_id"] for r in rows_] == ["m1"]


def test_profile_mcp_attachments_profile_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_profiles": []})
    out = tc.profile_mcp_attachments(profile_id="missing", action="list")
    assert "error" in out


def test_profile_mcp_attachments_scope_gate_refuses_non_visible(monkeypatch):
    """On a scoped build, an MCP not referenced by any visible profile
    (and not already in agent_profile_mcp_servers for one) must be refused,
    mirroring profile_mcp_attachments_update's Guard 1."""
    client = _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE)],
        "agent_profile_mcp_servers": [],
    })
    import core.deployment as dep
    monkeypatch.setattr(dep, "scoped_build", lambda: True)
    import admin.seed_agent_profiles as seed_mod
    monkeypatch.setattr(seed_mod, "visible_agent_kinds", lambda: ("sample_agent",))

    # _attachable_mcps itself is not monkeypatched here — it is exercised
    # for real, gated on the (real) visible_profiles list this test builds
    # via agent_profiles. Only ATTACHABLE_M1 (not referenced by "p1") is
    # NOT in the gated set, so `set` on it must be refused.
    import admin.screens.agents as agents_mod
    monkeypatch.setattr(agents_mod, "_attachable_mcps",
                        lambda client, visible_profiles=None:
                        [] if visible_profiles is not None else [ATTACHABLE_M1])

    out = tc.profile_mcp_attachments(
        profile_id="p1", action="set",
        attachments=[{"mcp_server_id": "m1"}])
    assert "error" in out
    assert client.table("agent_profile_mcp_servers")._data == []


def test_profile_mcp_attachments_gate_lookup_failure_fails_closed(monkeypatch):
    """If _profile_visible_mcp_gate's own agent_profiles lookup raises (DB
    hiccup, not a normal "not visible" result), it must fail CLOSED — an
    empty visible-profiles list, exactly like the scoped-build handler this
    mirrors — rather than let the exception propagate or fail open. With
    nothing visible, nothing is attachable, so `set` must refuse every MCP
    and write nothing to agent_profile_mcp_servers."""
    client = _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE)],
        "agent_mcp_servers": [dict(MCP_SERVER, mcp_server_id="m1", name="search-mcp")],
        "agent_profile_mcp_servers": [],
    })
    import core.deployment as dep
    monkeypatch.setattr(dep, "scoped_build", lambda: True)
    import admin.seed_agent_profiles as seed_mod
    monkeypatch.setattr(seed_mod, "visible_agent_kinds", lambda: ("sample_agent",))

    # Make ONLY the gate's unfiltered "select every profile" call raise —
    # the initial profile_id lookup (which sets an eq/limit predicate) must
    # still succeed, or we'd never reach the gate at all.
    profiles_table = client.table("agent_profiles")
    real_execute = profiles_table.execute

    def _maybe_raise():
        if not profiles_table._predicates and profiles_table._limit is None:
            raise RuntimeError("boom: agent_profiles lookup failed")
        return real_execute()

    monkeypatch.setattr(profiles_table, "execute", _maybe_raise)

    out = tc.profile_mcp_attachments(
        profile_id="p1", action="set",
        attachments=[{"mcp_server_id": "m1"}])

    # Refused, not a 500/raise: the exception was swallowed inside the gate.
    assert "error" in out
    assert "m1" in out["error"]
    assert client.table("agent_profile_mcp_servers")._data == []
    assert client.table("agent_profile_mcp_servers").inserted == []


# ---------------------------------------------------------------------------
# mentionables
# ---------------------------------------------------------------------------


def test_mentionables_populates_tools_mcps_kbs(monkeypatch):
    _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE, data_source_ids=["k1"])],
        "agent_data_sources": [dict(KB_SOURCE)],
    })
    fake_attachments = [{
        "mcp_server": {"mcp_server_id": "m1", "name": "search-mcp"},
        "tool_subset": None, "position": 0,
        "member_tools": [{"tool": {"python_name": "get_weather"},
                          "context_fields": {}, "position": 0},
                         {"tool": {"python_name": "get_forecast"},
                          "context_fields": {}, "position": 1}],
    }]
    import core.agent_profile as agent_profile
    monkeypatch.setattr(agent_profile, "_fetch_mcp_attachments",
                        lambda client, profile_id: fake_attachments)

    out = tc.mentionables(profile_id="p1")
    assert out["mcps"] == [{"name": "search-mcp"}]
    assert out["tools"] == [{"name": "get_weather"}, {"name": "get_forecast"}]
    assert out["kbs"] == [{"name": "docs"}]


def test_mentionables_degrades_to_empty_on_attachment_failure(monkeypatch):
    _fake(monkeypatch, {
        "agent_profiles": [dict(PROFILE, data_source_ids=[])],
    })
    import core.agent_profile as agent_profile

    def _raise(client, profile_id):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_profile, "_fetch_mcp_attachments", _raise)

    out = tc.mentionables(profile_id="p1")
    assert out == {"tools": [], "mcps": [], "kbs": []}


def test_mentionables_profile_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_profiles": []})
    out = tc.mentionables(profile_id="missing")
    assert "error" in out


@pytest.fixture(autouse=True)
def _sample_kinds(product_build):
    """Generic logic, fictional agent kinds (tests/conftest.py::product_build)."""
