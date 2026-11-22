"""Tests for the "architecture mold" template catalog + idempotent seeder.

Templates are agent_profiles rows with status='template', environment=
'staging' — inert at runtime, forkable via the (later) templates surface.
"""
from __future__ import annotations

from admin import seed_templates as st


def test_catalog_shape():
    slugs = {m["slug"] for m in st.TEMPLATE_CATALOG}
    assert {"mold:blank", "mold:rag", "mold:orchestrator", "mold:data-analyst",
            "mold:mcp-connected", "mold:worker-subagent", "mold:chart-subagent"} <= slugs
    for m in st.TEMPLATE_CATALOG:
        assert m["agent_kind"] == st.TEMPLATE_AGENT_KIND
        assert m["status"] == "template"
        assert m["environment"] == "staging"


def test_seeder_idempotent(monkeypatch):
    from tests.admin_mcp.conftest import FakeClient   # reuse the fake
    client = FakeClient({"agent_profiles": [], "agent_prompt_versions": []})
    monkeypatch.setattr("core.auth.get_service_client", lambda: client)
    st.run()
    n1 = len(client.table("agent_profiles")._data)
    st.run()                       # second run must not duplicate
    n2 = len(client.table("agent_profiles")._data)
    assert n1 == n2 == len(st.TEMPLATE_CATALOG)


def test_composite_wiring(monkeypatch):
    from tests.admin_mcp.conftest import FakeClient
    client = FakeClient({"agent_profiles": [], "agent_prompt_versions": []})
    monkeypatch.setattr("core.auth.get_service_client", lambda: client)
    st.run()
    rows = {r["slug"]: r for r in client.table("agent_profiles")._data}
    da = rows["mold:data-analyst"]
    chart_id = rows["mold:chart-subagent"]["profile_id"]
    assert da["downstream_profile_ids"] == [chart_id]


# ---------------------------------------------------------------------------
# Stale model-reference repair (Azure migration, 2026-07-26).
#
# _default_model_id picks "first active staging model ORDERED BY NAME", so each
# mold inherits whatever sorted first the day it was created — and that pointer
# never updates, because this seeder is insert-only. When the model is later
# deactivated, an agent FORKED from the mold inherits a dead reference and fails
# at build time. Templates are read-only through the admin API/MCP by design, so
# this seeder is the only supported place to repair them.
# ---------------------------------------------------------------------------


def _client_with_models(active_id: str, stale_id: str):
    from tests.admin_mcp.conftest import FakeClient
    return FakeClient({
        "agent_profiles": [],
        "agent_prompt_versions": [],
        "agent_models": [
            # sorts first by name, so _default_model_id picks it
            {"model_id": active_id, "name": "AAA Active", "environment": "staging",
             "is_active": True},
            {"model_id": stale_id, "name": "ZZZ Retired", "environment": "staging",
             "is_active": False},
        ],
    })


def test_repairs_mold_pointing_at_deactivated_model(monkeypatch):
    client = _client_with_models("model-active", "model-stale")
    monkeypatch.setattr("core.auth.get_service_client", lambda: client)
    st.run()   # seeds molds against the active model

    # Simulate the real situation: the molds were created when a now-retired
    # model sorted first, so they all point at it.
    for r in client.table("agent_profiles")._data:
        r["model_main_id"] = "model-stale"

    out = st.run()

    assert out["repaired"] == len(st.TEMPLATE_CATALOG)
    assert all(r["model_main_id"] == "model-active"
               for r in client.table("agent_profiles")._data)


def test_repair_is_idempotent(monkeypatch):
    client = _client_with_models("model-active", "model-stale")
    monkeypatch.setattr("core.auth.get_service_client", lambda: client)
    st.run()
    out = st.run()   # already healthy -> nothing to repair
    assert out["repaired"] == 0


def test_repair_leaves_healthy_refs_alone(monkeypatch):
    """A mold on a DIFFERENT but still-active model must not be rewritten."""
    client = _client_with_models("model-active", "model-stale")
    client.table("agent_models")._data.append(
        {"model_id": "model-other", "name": "MMM Other", "environment": "staging",
         "is_active": True})
    monkeypatch.setattr("core.auth.get_service_client", lambda: client)
    st.run()
    for r in client.table("agent_profiles")._data:
        r["model_main_id"] = "model-other"
    out = st.run()
    assert out["repaired"] == 0
    assert all(r["model_main_id"] == "model-other"
               for r in client.table("agent_profiles")._data)


def test_default_model_is_stable_against_new_alphabetically_earlier_models(monkeypatch):
    """Adding a model must NEVER steal the mold default.

    Ordering by name meant the winner changed whenever the catalog grew — which
    is how the molds first landed on an anthropic_vertex row, and then jumped
    onto a newly-added "Azure …" row after that one was retired. Ordering by
    created_at keeps the established default.
    """
    from tests.admin_mcp.conftest import FakeClient
    client = FakeClient({
        "agent_profiles": [],
        "agent_prompt_versions": [],
        "agent_models": [
            {"model_id": "established", "name": "Gemini 3.5 Flash-Lite",
             "environment": "staging", "is_active": True,
             "created_at": "2026-01-01T00:00:00+00:00"},
            # sorts FIRST by name, but is newer — must not win
            {"model_id": "newcomer", "name": "AAA Azure GPT-5-mini",
             "environment": "staging", "is_active": True,
             "created_at": "2026-07-26T00:00:00+00:00"},
        ],
    })
    monkeypatch.setattr("core.auth.get_service_client", lambda: client)
    assert st._default_model_id(client) == "established"
    st.run()
    assert all(r["model_main_id"] == "established"
               for r in client.table("agent_profiles")._data)


# ── a template keeps exactly what its catalog entry names ────────────────────

def test_backfilled_molds_are_reset_to_their_catalog_entry(monkeypatch):
    """The profile seeder used to fill every empty array on a kind's profiles,
    molds included: each mold picked up the kind's whole tool set and its
    knowledge sources, and every agent made from it inherited them."""
    from tests.admin_mcp.conftest import FakeClient

    client = FakeClient({
        "agent_tools": [{"tool_id": "t-search", "python_name": "search_documentation",
                         "is_deprecated": False}],
        "agent_profiles": [
            {"profile_id": "m1", "slug": "mold:blank", "agent_kind": st.TEMPLATE_AGENT_KIND,
             "environment": "staging", "status": "template",
             "tool_ids": ["t-a", "t-b"], "data_source_ids": ["s1", "s2", "s3"]},
            {"profile_id": "m2", "slug": "mold:rag", "agent_kind": st.TEMPLATE_AGENT_KIND,
             "environment": "staging", "status": "template",
             "tool_ids": ["t-a", "t-search"], "data_source_ids": ["s1"]},
            {"profile_id": "real", "slug": "sample_default", "agent_kind": "sample_kind",
             "environment": "staging", "status": "active",
             "tool_ids": ["t-a"], "data_source_ids": ["s1"]},
        ],
    })
    assert st._repair_backfilled_attachments(client) == 2
    rows = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    assert rows["m1"]["tool_ids"] == [] and rows["m1"]["data_source_ids"] == []
    assert rows["m2"]["tool_ids"] == ["t-search"] and rows["m2"]["data_source_ids"] == []
    assert rows["real"]["tool_ids"] == ["t-a"] and rows["real"]["data_source_ids"] == ["s1"]
    assert st._repair_backfilled_attachments(client) == 0  # nothing left to correct


def test_profile_seeder_does_not_backfill_a_template(monkeypatch):
    from admin import seed_agent_profiles as sp
    from tests.admin_mcp.conftest import FakeClient

    client = FakeClient({"agent_profiles": [
        {"profile_id": "m1", "slug": "mold:blank", "agent_kind": st.TEMPLATE_AGENT_KIND, "status": "template",
         "tool_ids": [], "data_source_ids": [], "prompt_version_ids": {"system": "v"}},
        {"profile_id": "real", "slug": "sample_default", "agent_kind": "sample_kind", "status": "active",
         "tool_ids": [], "data_source_ids": [], "prompt_version_ids": {"system": "v"}},
    ]})
    touched = []
    monkeypatch.setattr(sp, "get_service_client", lambda: client)
    monkeypatch.setattr(sp, "_is_local", lambda: False)
    monkeypatch.setattr(sp, "_link_orphan_subagents", lambda c: None)
    monkeypatch.setattr(sp, "visible_agent_kinds", lambda: ("sample_kind", st.TEMPLATE_AGENT_KIND))
    monkeypatch.setattr(sp, "_apply_defaults", lambda c, p, k: touched.append(p["profile_id"]) or True)
    sp.run()
    assert touched == ["real"]
