"""Gaps found when re-checking the public build (no plugin, tracing off).

* Conversations: the Agent filter lists every agent from the agent profiles,
  also on the production view where an agent made from a template has no row
  yet, and also when tracing is off (the saved-chats list).
* Connections: the Tool catalog's agent filter lists each agent, and a server
  group (an external server's tools included) matches the agents it is
  attached to.
* Memory: the bare ``custom`` kind is neither offered nor opened when the
  generic agents are listed one by one; the save note names the picked agent.
* Settings overview and the knowledge base's "Kind of document" read neutrally
  when no plugin provides businesses.
* Admin MCP ``memory add`` resolves an email like ``list`` does.
"""
from __future__ import annotations

import pytest

from admin import agent_kinds as ak
from tests.admin.test_agent_pickers_per_agent import KB_ID, FakeDB, PROFILES
from tests.scoped_build import full_build

USER = "00000000-0000-0000-0000-0000000000aa"
SERVER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def db(monkeypatch):
    """Full build, the generic agents of the shared fixture (all in staging)."""
    full_build(monkeypatch)
    fake = FakeDB({
        "agent_profiles": PROFILES,
        "ai_user_memory": [],
        "user": [{"id": USER, "email": "admin@example.com"}],
        "agent_data_sources": [{"source_id": KB_ID, "name": "help_center", "kind": "pgvector",
                                "agent_kind": None, "additional_agent_kinds": [],
                                "config": {}, "is_active": True}],
        "ai_chat_sessions": [
            {"id": "s1", "agent_kind": "custom", "metadata": {"agent_slug": "support-desk"},
             "deleted_at": None, "updated_at": "2099-01-01T00:00:00Z"},
            {"id": "s2", "agent_kind": "custom", "metadata": {"agent_slug": "sales-assistant"},
             "deleted_at": None, "updated_at": "2099-01-01T00:00:00Z"},
        ],
        "agent_mcp_servers": [{"mcp_server_id": SERVER, "name": "helpdesk", "source": "external",
                               "is_builtin": False, "is_active": True}],
        "agent_mcp_tools": [],
        "agent_profile_mcp_servers": [{"mcp_server_id": SERVER, "profile_id": "p-support"}],
        "agent_tools": [],
    })
    monkeypatch.setattr(ak, "_fetch_profiles", lambda: fake.tables["agent_profiles"])
    import core.auth
    from admin import kb_handlers, memory_handlers, router

    monkeypatch.setattr(router, "client_for", lambda env: fake)
    monkeypatch.setattr(router, "admin_service_client", lambda: fake)
    monkeypatch.setattr(core.auth, "admin_service_client", lambda: fake)
    monkeypatch.setattr(core.auth, "get_main_client", lambda: fake)
    monkeypatch.setattr(kb_handlers, "admin_service_client", lambda: fake)
    monkeypatch.setattr(memory_handlers, "client_for", lambda env: fake)
    monkeypatch.setattr(memory_handlers, "record_change", lambda **_k: None)
    monkeypatch.setattr(memory_handlers, "embed_text", lambda _t: None)
    return fake


def _no_businesses(monkeypatch):
    """A build whose plugins provide no businesses (the public build)."""
    import admin.eval_context as ec
    from admin.router import templates

    monkeypatch.setattr(ec, "business_field_label", lambda: "")
    # In place: a template compiled earlier holds this very dict.
    monkeypatch.setitem(templates.env.globals, "business_field_label", lambda: "")


# --- 1. Conversations ---------------------------------------------------------


def test_conversations_filter_lists_staging_agents_on_production(db):
    from admin.screens.conversations import agent_filter_options

    values = {o["kind"]: o["name"] for o in agent_filter_options([], {}, "prod")}
    assert values.get("custom:support-desk") == "Support desk"
    assert values.get("custom:sales-assistant") == "Sales assistant"
    assert values.get("custom:demo") == "Demo agent"
    assert "custom" not in values


def test_conversations_with_tracing_off_filters_saved_chats_by_agent(db, monkeypatch):
    from admin.screens import conversations, saved_chats

    monkeypatch.setattr("core.lmnr_clickhouse.tracing_configured", lambda: False)
    monkeypatch.setattr(saved_chats, "_client", lambda env: db)
    ctx = conversations.build_conversations_context(
        env="prod", hours=24, search=None, status=None, outcome=None, tool=None,
        channel_slug=None, agent_kind="custom:support-desk", group="session", background=0)
    assert ctx["saved_chats"] is True
    assert {"kind": "custom:support-desk", "name": "Support desk"} in ctx["agent_options"]
    assert [g["session_id"] for g in ctx["groups"]] == ["s1"]


# --- 2. Tool catalog -----------------------------------------------------------


def test_tool_catalog_agent_filter_lists_agents_and_tags_server_groups(client, db, monkeypatch):
    import admin.external_mcp_tools as ext

    monkeypatch.setattr(ext, "listed_tools", lambda servers: {
        SERVER: [{"name": "open_ticket", "description": "Open a ticket"}]})
    from admin.router import build_integrations_context

    ctx = build_integrations_context("staging")
    assert {"custom:support-desk", "custom:sales-assistant", "custom:demo"} <= set(
        ctx["tool_agent_kinds"])
    ext_group = next(g for g in ctx["tool_groups"] if g.get("external"))
    assert ext_group["agent_values"] == ["custom:support-desk"]

    page = client.get("/admin/tools?env=staging")
    assert page.status_code == 200
    assert '<option value="custom:support-desk">Support desk</option>' in page.text
    assert '<details data-group data-agent="custom:support-desk">' in page.text


# --- 3. Memory ------------------------------------------------------------------


def test_memory_settings_open_on_an_agent_and_never_offer_the_bare_kind(client, db):
    page = client.get("/admin/agents/memory?agent_kind=custom&env=staging")
    assert page.status_code == 200
    assert 'value="custom"' not in page.text
    assert "Custom agent" not in page.text
    assert '<option value="custom:demo" selected>' in page.text
    assert "What Demo agent remembers" in page.text
    assert "Saves to Demo agent" in page.text and "until you release Demo agent" in page.text


def test_memory_settings_note_names_the_picked_agent(client, db):
    page = client.get("/admin/agents/memory?agent_kind=custom:support-desk&env=staging")
    assert "Saves to Support desk" in page.text
    assert '<span class="technical">support-desk</span> in staging' in page.text


def test_memory_lookup_never_offers_the_bare_kind(client, db):
    page = client.get("/admin/memory?agent_kind=custom&env=staging")
    assert page.status_code == 200
    assert 'value="custom"' not in page.text
    assert "Custom agent" not in page.text


# --- 4. Settings overview ------------------------------------------------------


def test_settings_voice_budgets_entry_is_neutral_without_businesses(client, db, monkeypatch):
    from admin import nav

    _no_businesses(monkeypatch)
    entry = next(e for e in nav.settings_entries() if e.key == "voice_budgets")
    assert entry.description == "How much voice-call time each agent can use per day."
    page = client.get("/admin/settings")
    assert "each business" not in page.text

    monkeypatch.setattr("admin.eval_context.business_field_label", lambda: "Business")
    entry = next(e for e in nav.settings_entries() if e.key == "voice_budgets")
    assert entry.description == "How much voice-call time each business can use per day."


# --- 5. Knowledge base ---------------------------------------------------------


def test_kb_kind_of_document_is_neutral_without_businesses(client, db, monkeypatch):
    _no_businesses(monkeypatch)
    page = client.get(f"/admin/kb/{KB_ID}?env=staging")
    assert page.status_code == 200
    assert '<option value="business_concept">Concept</option>' in page.text
    assert "Business concept" not in page.text


# --- Admin MCP memory add by email ---------------------------------------------


def test_mcp_memory_add_resolves_an_email(db, monkeypatch):
    import admin_mcp.tools_govern as tg

    monkeypatch.setattr(tg, "client_for_env", lambda env: db)
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: None)
    monkeypatch.setattr("core.memory.embeddings.embed_text", lambda _t: None)
    out = tg.memory(action="add", agent_kind="custom:support-desk",
                    user_id="admin@example.com", content="Prefers email")
    # Saved under the user's id, never the email (Postgres: 22P02 on a uuid).
    assert db.inserted["ai_user_memory"][0]["user_id"] == USER
    assert out["added"] is True and out["user_id"] == USER

    missing = tg.memory(action="add", agent_kind="custom:support-desk",
                        user_id="nobody@example.com", content="x")
    assert "no user" in missing["error"]
    assert len(db.inserted["ai_user_memory"]) == 1


def test_portal_memory_add_resolves_an_email(client, db):
    r = client.post("/admin/memory/add?env=staging", data={
        "user_id": "admin@example.com", "content": "Budget is 5k",
        "agent_kind": "custom:sales-assistant"}, follow_redirects=False)
    assert r.status_code == 302
    assert db.inserted["ai_user_memory"][0]["user_id"] == USER
    client.post("/admin/memory/add?env=staging", data={
        "user_id": "nobody@example.com", "content": "x",
        "agent_kind": "custom:sales-assistant"}, follow_redirects=False)
    assert len(db.inserted["ai_user_memory"]) == 1
