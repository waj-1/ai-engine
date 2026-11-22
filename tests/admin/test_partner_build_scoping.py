"""A partner build (the AI-agents demo as a scope) shows only its own agents.

Docs writers on the agents build found the WAJ catalog leaking through:
Connections › Create bundle listed all 147 tools (internal ones included),
the bundle agent filter and the channel "Answered by" picker offered internal
agents, Testing › Test suites listed the internal WAJ suites — and the Tool
catalog said "No tools match these filters" over 29 tools. Every screen now
asks `admin.agent_kinds` (build_kinds / scope_to_build / build_agent_kinds /
channel_in_build / mcp_server_in_build) what the build serves.

Each partner-build test has a full-build twin proving the narrowing is a
partner-only behaviour, not a general filter.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.admin.test_draft_copy_isolation import FakeSupabase
from tests.scoped_build import full_build, use_agents_demo

DEMO = "agents_sales"


@pytest.fixture
def partner(monkeypatch):
    use_agents_demo(monkeypatch)


@pytest.fixture
def full(monkeypatch):
    full_build(monkeypatch)


def _catalog():
    return {
        "agent_tools": [
            {"tool_id": "t-rec", "python_name": "get_case_history", "kind": "python",
             "agent_kind": "records", "additional_agent_kinds": [], "is_deprecated": False},
            {"tool_id": "t-biz", "python_name": "get_bookings", "kind": "python",
             "agent_kind": "business", "additional_agent_kinds": [], "is_deprecated": False},
            {"tool_id": "t-demo", "python_name": "send_offer_link", "kind": "python",
             "agent_kind": DEMO, "additional_agent_kinds": [], "is_deprecated": False},
            {"tool_id": "t-shared", "python_name": "lookup_plan", "kind": "python",
             "agent_kind": "sales", "additional_agent_kinds": [DEMO], "is_deprecated": False},
        ],
        "agent_profiles": [
            {"profile_id": "p-demo", "agent_kind": DEMO, "slug": "agents_sales_default",
             "display_name": "Rami — Outbound sales", "environment": "staging",
             "status": "active", "is_active": True, "mcp_server_ids": []},
            {"profile_id": "p-orchard", "agent_kind": "orchard", "slug": "orchard_default",
             "display_name": "Orchard Assistant", "environment": "staging",
             "status": "active", "is_active": True, "mcp_server_ids": ["s-orchard-mock"]},
            {"profile_id": "p-rec", "agent_kind": "records", "slug": "records_default",
             "display_name": "Records", "environment": "staging",
             "status": "active", "is_active": True, "mcp_server_ids": []},
        ],
        "agent_mcp_servers": [
            {"mcp_server_id": "s-demo", "name": f"__builtin__:{DEMO}", "source": "bundle",
             "is_builtin": True},
            {"mcp_server_id": "s-rec", "name": "__builtin__:records", "source": "bundle",
             "is_builtin": True},
            {"mcp_server_id": "s-orchard-mock", "name": "orchard-mock", "source": "external",
             "is_builtin": False},
        ],
        "agent_mcp_tools": [
            {"mcp_server_id": "s-demo", "tool_id": "t-demo", "position": 0},
            {"mcp_server_id": "s-rec", "tool_id": "t-rec", "position": 0},
            # A demo-visible tool that ALSO sits in a hidden server.
            {"mcp_server_id": "s-rec", "tool_id": "t-shared", "position": 1},
        ],
        # Demo profiles attach their bundle through the junction table; the
        # legacy mcp_server_ids column is empty for them.
        "agent_profile_mcp_servers": [
            {"profile_id": "p-demo", "mcp_server_id": "s-demo"},
            {"profile_id": "p-rec", "mcp_server_id": "s-rec"},
        ],
    }


# --------------------------------------------------------------------------
# 2a — Connections › Create bundle: the tool picker
# --------------------------------------------------------------------------

def test_bundle_picker_partner_build_lists_only_its_agents_tools(partner):
    from admin.mcp_manage_handlers import _load_tools_for_picker

    ids = {t["tool_id"] for t in _load_tools_for_picker(FakeSupabase(_catalog()))}
    assert ids == {"t-demo", "t-shared"}


def test_bundle_picker_full_build_lists_every_tool(full):
    from admin.mcp_manage_handlers import _load_tools_for_picker

    ids = {t["tool_id"] for t in _load_tools_for_picker(FakeSupabase(_catalog()))}
    assert ids == {"t-rec", "t-biz", "t-demo", "t-shared"}


def test_bundle_edit_picker_keeps_existing_members(partner):
    """Editing a bundle never silently drops a member the build doesn't own."""
    from admin.mcp_handlers import _picker_tools_for_build

    tools = _catalog()["agent_tools"]
    ids = {t["tool_id"] for t in _picker_tools_for_build(tools, {"t-rec"})}
    assert ids == {"t-demo", "t-shared", "t-rec"}


def test_bundle_create_rejects_a_foreign_tool(partner):
    """Server-side: a forged internal member id is refused on the partner build."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    import admin.mcp_manage_handlers as h
    from admin.auth import require_superuser

    fake = FakeSupabase(_catalog())
    app = FastAPI()
    app.include_router(h.router)
    app.dependency_overrides[require_superuser] = lambda: {"user_id": "u", "email": "x"}
    with patch.object(h, "admin_service_client", return_value=fake):
        resp = TestClient(app).post("/admin/mcp/create", data={
            "name": "Demo bundle", "member_tool_ids": ["t-rec"]})
    assert resp.status_code == 200
    assert "unavailable" in resp.text
    assert not [w for w in fake.writes if w[1] == "agent_mcp_servers"]


# --------------------------------------------------------------------------
# 2b — the agent lists: bundle filter options + channel "Answered by"
# --------------------------------------------------------------------------

def test_channel_answered_by_partner_build_offers_only_its_agents(partner):
    from admin.channel_handlers import _fetch_profiles_for_picker

    kinds = {p["agent_kind"] for p in _fetch_profiles_for_picker(FakeSupabase(_catalog()))}
    assert kinds == {DEMO}


def test_channel_answered_by_full_build_offers_every_agent(full):
    from admin.channel_handlers import _fetch_profiles_for_picker

    kinds = {p["agent_kind"] for p in _fetch_profiles_for_picker(FakeSupabase(_catalog()))}
    assert kinds == {DEMO, "orchard", "records"}


def test_channels_list_hides_other_partners_and_full_build_surfaces(partner, monkeypatch):
    from admin import agent_kinds
    from admin.channel_handlers import visible_channels

    # A kind a plugin says only a full build has (`full_build_channel_kinds`).
    monkeypatch.setattr(agent_kinds, "_WAJ_ONLY_CHANNEL_KINDS", ("app_surface",))
    rows = [
        {"slug": "orchard", "kind": "other", "allowed_agent_kinds": []},
        {"slug": "waj_app", "kind": "app_surface", "allowed_agent_kinds": []},
        {"slug": "admin_portal", "kind": "other", "allowed_agent_kinds": []},
        {"slug": "demo-site", "kind": "embed", "allowed_agent_kinds": [DEMO]},
        {"slug": "records-site", "kind": "embed", "allowed_agent_kinds": ["records"]},
        {"slug": "pinned", "kind": "embed", "allowed_agent_kinds": [],
         "default_profile_id": "p-rec"},
    ]
    with patch("core.auth.admin_service_client", return_value=FakeSupabase(_catalog())):
        slugs = {r["slug"] for r in visible_channels(rows)}
    assert slugs == {"admin_portal", "demo-site"}


def test_channel_agent_kinds_partner_build(partner):
    from admin.channel_handlers import _channel_agent_kinds

    assert _channel_agent_kinds() == (
        "agents_support", "agents_inquiries", "agents_collection", "agents_sales")


# --------------------------------------------------------------------------
# 2c — Testing › Test suites / runs / schedules
# --------------------------------------------------------------------------

def _eval_rows():
    profiles = _catalog()["agent_profiles"]
    suites = [
        {"suite_id": "su-demo", "agent_kind": DEMO, "profile_id": "p-demo"},
        {"suite_id": "su-rec", "agent_kind": "records", "profile_id": None},
        {"suite_id": "su-orchard", "agent_kind": "orchard", "profile_id": "p-orchard"},
    ]
    runs = [
        {"run_id": "r-demo", "suite_id": "su-demo", "profile_id": "p-demo"},
        {"run_id": "r-rec", "suite_id": "su-rec", "profile_id": "p-rec"},
        {"run_id": "r-live-demo", "suite_id": None, "profile_id": "p-demo"},
        {"run_id": "r-live-orchard", "suite_id": None, "profile_id": "p-orchard"},
    ]
    schedules = [
        {"schedule_id": "sc-demo", "profile_id": "p-demo"},
        {"schedule_id": "sc-rec", "profile_id": "p-rec"},
    ]
    return profiles, suites, runs, schedules


def test_eval_rows_partner_build_scoped(partner):
    from admin.eval_runs_handlers import _scope_eval_rows

    profiles, suites, runs, schedules = _eval_rows()
    out = _scope_eval_rows(all_profiles=profiles, suites=suites, runs=runs, schedules=schedules)
    assert [s["suite_id"] for s in out["suites"]] == ["su-demo"]
    assert {r["run_id"] for r in out["runs"]} == {"r-demo", "r-live-demo"}
    assert [s["schedule_id"] for s in out["schedules"]] == ["sc-demo"]


def test_eval_rows_full_build_untouched(full):
    from admin.eval_runs_handlers import _scope_eval_rows

    profiles, suites, runs, schedules = _eval_rows()
    out = _scope_eval_rows(all_profiles=profiles, suites=suites, runs=runs, schedules=schedules)
    assert len(out["suites"]) == 3 and len(out["runs"]) == 4 and len(out["schedules"]) == 2


def test_test_suites_screen_partner_build(partner, monkeypatch):
    """The Test suites tab itself (build_evals_page_context), not just the helper."""
    import admin.eval_runs_handlers as h

    tables = _catalog()
    tables.update({
        "agent_eval_suites": [
            {"suite_id": "su-demo", "name": "Demo suite", "agent_kind": DEMO,
             "profile_id": "p-demo", "scenarios": []},
            {"suite_id": "su-rec", "name": "records_core", "agent_kind": "records",
             "profile_id": None, "scenarios": []},
        ],
        "agent_eval_runs": [], "agent_eval_schedules": [], "agent_models": [],
        "agent_profile_versions": [],
    })
    fake = FakeSupabase(tables)
    monkeypatch.setattr(h, "admin_service_client", lambda: fake)
    monkeypatch.setattr("admin._business_picker.fetch_businesses_for_picker", lambda c: [])
    ctx = h.build_evals_page_context()
    assert [s["name"] for s in ctx["suites"]] == ["Demo suite"]
    assert set(ctx["default_expectations_by_kind"]) <= {
        "agents_support", "agents_inquiries", "agents_collection", "agents_sales"}


# --------------------------------------------------------------------------
# 2d — the sweep: Knowledge / Memory / Skills pickers, Models "Used by",
# Conversations filters
# --------------------------------------------------------------------------

def test_picker_kinds_partner_build_drop_reserved_kinds(partner):
    """KB / Memory / Skills offered "Agents title" / "Agents suggestions" —
    reserved kinds with no profile."""
    from admin.agent_kinds import build_agent_kinds
    from admin.kb_handlers import _kb_agent_kinds

    expected = ("agents_support", "agents_inquiries", "agents_collection", "agents_sales")
    assert build_agent_kinds() == expected
    assert _kb_agent_kinds() == expected


def test_picker_kinds_full_build_is_live_list(full):
    from admin.agent_kinds import build_agent_kinds, live_agent_kinds

    assert build_agent_kinds() == live_agent_kinds()


def test_models_used_by_partner_build(partner, monkeypatch):
    import admin.router as r

    tables = {"agent_models": [{"model_id": "m1", "name": "Gemini", "environment": "staging"}],
              "agent_profiles": [
                  {"profile_id": "p-demo", "agent_kind": DEMO, "display_name": "Rami",
                   "environment": "staging", "status": "active", "model_main_id": "m1"},
                  {"profile_id": "p-orchard", "agent_kind": "orchard", "display_name": "Orchard",
                   "environment": "staging", "status": "active", "model_main_id": "m1"}]}
    monkeypatch.setattr(r, "client_for", lambda env: FakeSupabase(tables))
    usage = r.build_models_context("staging")["model_usage"]
    assert [u["name"] for u in usage["m1"]] == ["Rami"]


def test_conversation_tool_filter_partner_build(partner, monkeypatch):
    from admin.screens import observability as obs

    monkeypatch.setattr(obs, "admin_service_client", lambda: FakeSupabase(_catalog()))
    assert obs.list_tool_names_from_db() == ["lookup_plan", "send_offer_link"]


def test_mcp_server_in_build_rules(partner):
    from admin.agent_kinds import mcp_server_in_build

    used = {"s-orchard-mock": {"orchard"}, "s-mine": {DEMO}}
    assert mcp_server_in_build({"name": f"__builtin__:{DEMO}"}, attached_kinds={})
    assert not mcp_server_in_build({"name": "__builtin__:records"}, attached_kinds={})
    assert not mcp_server_in_build({"mcp_server_id": "s-orchard-mock", "name": "orchard-mock"},
                                   attached_kinds=used)
    assert mcp_server_in_build({"mcp_server_id": "s-mine", "name": "Demo"}, attached_kinds=used)
    # A freshly created, not-yet-attached operator bundle stays visible.
    assert mcp_server_in_build({"mcp_server_id": "s-new", "name": "Demo new"}, attached_kinds={})


# --------------------------------------------------------------------------
# 3 — Tool catalog: "No tools match these filters" over 29 tools
# --------------------------------------------------------------------------

def test_tool_catalog_partner_build_groups_its_tools(partner, monkeypatch):
    """The partner filter read only the legacy mcp_server_ids column (empty
    for demo profiles, which attach via the junction table), so every server
    was hidden — and the catalog, grouped per server, rendered no rows."""
    import admin.router as r

    monkeypatch.setattr(r, "client_for", lambda env: FakeSupabase(_catalog()))
    ctx = r.build_integrations_context("staging")
    assert [s["name"] for s in ctx["mcp_internal"]] == [f"__builtin__:{DEMO}"]
    grouped = {t["tool_id"] for g in ctx["tool_groups"] for t in g["tools"]}
    assert {t["tool_id"] for t in ctx["all_tools"]} == {"t-demo", "t-shared"}
    # Every catalog row is reachable — t-shared's only server is hidden, so
    # it falls through to "Not in any server" instead of vanishing.
    assert grouped == {"t-demo", "t-shared"}


def test_tool_catalog_full_build_unchanged(full, monkeypatch):
    import admin.router as r

    monkeypatch.setattr(r, "client_for", lambda env: FakeSupabase(_catalog()))
    ctx = r.build_integrations_context("staging")
    assert {s["mcp_server_id"] for s in ctx["mcp_internal"]} == {"s-demo", "s-rec"}
    grouped = {t["tool_id"] for g in ctx["tool_groups"] for t in g["tools"]}
    assert grouped == {"t-rec", "t-biz", "t-demo", "t-shared"}
