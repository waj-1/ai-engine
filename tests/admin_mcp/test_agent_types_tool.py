"""The `agent_types` admin MCP tool, and the category arguments on
`update_profile`, `templates(action="use")` and `list_profiles`.

Parity with the portal: both go through admin/services/agent_types.py, so the
rules (stable slug, fixed palette, delete leaves agents uncategorised) are the
same. The route-to-tool map and the docs row are pinned in
test_surface_parity.py.
"""
from __future__ import annotations

import pytest

import admin_mcp.tools_configure as tc
from admin.services import agent_types as svc
from admin.services import profiles
from admin.services import templates as tpl
from tests.admin_mcp.conftest import ACTOR, FakeClient, rpc


@pytest.fixture
def db(monkeypatch):
    """An install with no categories yet and three agents."""
    fake = FakeClient({
        "agent_types": [],
        "agent_profiles": [
            {"profile_id": "p-desk", "agent_kind": "custom", "slug": "front-desk",
             "environment": "staging", "status": "active", "display_name": "Front desk"},
            {"profile_id": "p-desk-live", "agent_kind": "custom", "slug": "front-desk",
             "environment": "prod", "status": "active", "display_name": "Front desk"},
            {"profile_id": "p-notes", "agent_kind": "custom", "slug": "notes",
             "environment": "staging", "status": "draft", "display_name": "Notes"},
            {"profile_id": "t-blank", "agent_kind": "custom", "slug": "mold:blank",
             "environment": "staging", "status": "template", "display_name": "Blank",
             "downstream_profile_ids": []},
        ],
    })
    monkeypatch.setattr(tc, "client_for_env", lambda env: fake)
    monkeypatch.setattr("core.auth.admin_service_client", lambda: fake)
    monkeypatch.setattr(svc, "record_change", lambda **kw: None)
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    monkeypatch.setattr(profiles, "invalidate_profile_loader", lambda *a: None)
    monkeypatch.setattr(tpl, "record_change", lambda **kw: None)
    return fake


def _profile(db, profile_id):
    return next(r for r in db.table("agent_profiles")._data if r["profile_id"] == profile_id)


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def test_a_fresh_install_lists_no_categories(db):
    out = tc.agent_types(action="list")
    assert out["count"] == 0 and out["agent_types"] == []
    assert out["colors"] == list(svc.PALETTE)


def test_create_list_update_delete(db):
    made = tc.agent_types(action="create", display_name="Support", color="teal",
                          description="Answers customers")
    assert made["created"] is True
    assert made["agent_type"]["slug"] == "support"
    assert made["agent_type"]["created_by"] == ACTOR["user_id"]     # audited as the caller
    tc.agent_types(action="create", display_name="Sales")

    listed = tc.agent_types(action="list")
    assert [(t["slug"], t["agent_count"]) for t in listed["agent_types"]] == [
        ("support", 0), ("sales", 0)]

    out = tc.agent_types(action="update", agent_type="support",
                         display_name="Customer care", color="blue", sort_order=5)
    assert out["updated"] is True
    assert out["changed"] == ["color", "display_name", "sort_order"]
    assert out["agent_type"]["slug"] == "support"                    # the handle is stable
    assert [t["slug"] for t in tc.agent_types(action="list")["agent_types"]] == [
        "sales", "support"]                                          # sort_order moved it

    out = tc.agent_types(action="delete", agent_type="sales", confirm=True)
    assert out == {"deleted": True, "agent_type": "sales", "agents_uncategorised": 0}
    assert [t["slug"] for t in tc.agent_types(action="list")["agent_types"]] == ["support"]


def test_delete_dry_runs_first_and_reports_the_agents_affected(db):
    tc.agent_types(action="create", display_name="Support")
    tc.update_profile(profile_id="p-desk", agent_type="support")
    tc.update_profile(profile_id="p-notes", agent_type="support")

    dry = tc.agent_types(action="delete", agent_type="support")
    assert dry["confirmed"] is False and dry["agents_uncategorised"] == 2
    assert "2 agents" in dry["would"] and "no agent is deleted" in dry["would"]
    assert len(db.table("agent_types")._data) == 1                   # nothing happened

    out = tc.agent_types(action="delete", agent_type="support", confirm=True)
    assert out["agents_uncategorised"] == 2
    assert db.table("agent_types")._data == []
    assert len(db.table("agent_profiles")._data) == 4                # the agents stay
    assert _profile(db, "p-desk")["agent_type_id"] is None
    assert _profile(db, "p-desk-live")["agent_type_id"] is None
    assert _profile(db, "p-notes")["agent_type_id"] is None


@pytest.mark.parametrize("kwargs, message", [
    ({"action": "create"}, "create needs display_name"),
    ({"action": "create", "display_name": "Sales", "color": "neon"}, "colour must be one of"),
    ({"action": "update", "display_name": "x"}, "update needs agent_type"),
    ({"action": "update", "agent_type": "nope", "color": "red"}, "no category 'nope'"),
    ({"action": "update", "agent_type": "support", "slug": "care"}, "slug cannot be changed"),
    ({"action": "update", "agent_type": "support"}, "nothing to change"),
    ({"action": "delete"}, "delete needs agent_type"),
    ({"action": "delete", "agent_type": "nope", "confirm": True}, "no category 'nope'"),
    ({"action": "create", "display_name": "SUPPORT"}, "already exists"),
    ({"action": "archive"}, "unknown action"),
])
def test_refusals_come_back_as_error_payloads(db, kwargs, message):
    tc.agent_types(action="create", display_name="Support")
    out = tc.agent_types(**kwargs)
    assert message in out["error"], out
    assert [t["slug"] for t in db.table("agent_types")._data] == ["support"]


# --------------------------------------------------------------------------- #
# Organising agents
# --------------------------------------------------------------------------- #
def test_update_profile_files_an_agent_by_category_slug(db):
    made = tc.agent_types(action="create", display_name="Support")["agent_type"]

    out = tc.update_profile(profile_id="p-desk", agent_type="support")

    assert out["updated"] is True and out["changed"] == ["agent_type_id"]
    assert _profile(db, "p-desk")["agent_type_id"] == made["agent_type_id"]
    # The live row of the same agent follows: no promote needed.
    assert _profile(db, "p-desk-live")["agent_type_id"] == made["agent_type_id"]
    assert tc.agent_types(action="list")["agent_types"][0]["agent_count"] == 1
    assert tc.get_profile(profile_id="p-desk")["agent_type"] == {
        "slug": "support", "display_name": "Support", "color": "slate"}


@pytest.mark.parametrize("clear", ["", "none"])
def test_update_profile_takes_an_agent_out_of_its_category(db, clear):
    tc.agent_types(action="create", display_name="Support")
    tc.update_profile(profile_id="p-desk", agent_type="support")

    out = tc.update_profile(profile_id="p-desk", agent_type=clear)

    assert out["updated"] is True
    assert _profile(db, "p-desk")["agent_type_id"] is None
    assert tc.get_profile(profile_id="p-desk")["agent_type"] is None


def test_update_profile_without_the_argument_leaves_the_category_alone(db):
    made = tc.agent_types(action="create", display_name="Support")["agent_type"]
    tc.update_profile(profile_id="p-desk", agent_type="support")

    out = tc.update_profile(profile_id="p-desk", description="Greets visitors")

    assert out["changed"] == ["description"]
    assert _profile(db, "p-desk")["agent_type_id"] == made["agent_type_id"]


def test_update_profile_refuses_an_unknown_category(db):
    out = tc.update_profile(profile_id="p-desk", agent_type="no-such-category")
    assert "no category 'no-such-category'" in out["error"]
    assert _profile(db, "p-desk").get("agent_type_id") is None


def test_templates_use_files_the_new_agent(db):
    made = tc.agent_types(action="create", display_name="Support")["agent_type"]

    out = tc.templates(action="use", template_id="t-blank", display_name="Help desk",
                       agent_type="support")

    assert "error" not in out, out
    assert _profile(db, out["root"])["agent_type_id"] == made["agent_type_id"]

    refused = tc.templates(action="use", template_id="t-blank", display_name="Other",
                           agent_type="no-such-category")
    assert "no category" in refused["error"]
    assert len(db.table("agent_profiles")._data) == 5          # only the first was created


def test_list_profiles_shows_and_filters_by_category(db):
    tc.agent_types(action="create", display_name="Support")
    tc.update_profile(profile_id="p-desk", agent_type="support")

    everyone = tc.list_profiles(env="staging")
    assert {p["profile_id"]: p["agent_type"] for p in everyone["profiles"]} == {
        "p-desk": "support", "p-notes": None, "t-blank": None}

    assert [p["profile_id"] for p in
            tc.list_profiles(env="staging", agent_type="support")["profiles"]] == ["p-desk"]
    assert sorted(p["profile_id"] for p in
                  tc.list_profiles(env="staging", agent_type="none")["profiles"]) == [
        "p-notes", "t-blank"]
    assert "no category" in tc.list_profiles(env="staging", agent_type="nope")["error"]


# --------------------------------------------------------------------------- #
# On the wire
# --------------------------------------------------------------------------- #
def test_the_tool_is_registered_with_its_actions_and_palette(mcp_client):
    tools = {t["name"]: t for t in rpc(mcp_client, "tools/list").json()["result"]["tools"]}
    props = tools["agent_types"]["inputSchema"]["properties"]
    assert props["action"]["enum"] == ["list", "create", "update", "delete"]
    for colour in svc.PALETTE:
        assert colour in props["color"]["description"]
    # A mixed read/write tool must not claim to be read-only.
    assert not (tools["agent_types"].get("annotations") or {}).get("readOnlyHint")
    # The category arguments exist where an assistant organises agents.
    assert "agent_type" in tools["update_profile"]["inputSchema"]["properties"]
    assert "agent_type" in tools["templates"]["inputSchema"]["properties"]
    assert "agent_type" in tools["list_profiles"]["inputSchema"]["properties"]
