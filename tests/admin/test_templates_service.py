import pytest
from tests.admin_mcp.conftest import FakeClient
from admin.services import templates as tpl
from admin.services.profiles import ProfileNotFoundError

CATALOG = {
    "t_root": {"profile_id": "t_root", "slug": "mold:data-analyst", "status": "template",
               "agent_kind": "business", "environment": "staging", "display_name": "Data Analyst",
               "downstream_profile_ids": ["t_chart"]},
    "t_chart": {"profile_id": "t_chart", "slug": "mold:chart-subagent", "status": "template",
                "agent_kind": "business", "environment": "staging", "display_name": "Chart Sub-agent",
                "downstream_profile_ids": []},
}


def _client():
    return FakeClient({"agent_profiles": [dict(CATALOG["t_root"]), dict(CATALOG["t_chart"])]})


def test_use_composite_forks_tree(monkeypatch):
    client = _client()
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    monkeypatch.setattr(tpl, "record_change", lambda **k: None)
    monkeypatch.setattr("admin.services.profiles.invalidate_profile_loader", lambda *a: None)
    out = tpl.use_template(template_id="t_root", display_name="Revenue Analyst", actor_user_id="u")
    assert len(out["created"]) == 2                    # parent + chart sub-agent
    rows = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    root = rows[out["root"]]
    assert root["status"] == "draft" and root["display_name"] == "Revenue Analyst"
    # parent draft points at the NEW chart draft, not the template
    assert root["downstream_profile_ids"] and root["downstream_profile_ids"][0] != "t_chart"


def test_use_non_template_errors(monkeypatch):
    client = FakeClient({"agent_profiles": [
        {"profile_id": "p1", "slug": "main", "status": "active", "agent_kind": "business",
         "environment": "staging", "downstream_profile_ids": []}]})
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    with pytest.raises(tpl.TemplateNotFoundError):
        tpl.use_template(template_id="p1", display_name="x", actor_user_id="u")


def test_use_template_missing_parent_leaves_no_orphan_drafts(monkeypatch):
    client = _client()
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    monkeypatch.setattr(tpl, "record_change", lambda **k: None)
    monkeypatch.setattr("admin.services.profiles.invalidate_profile_loader", lambda *a: None)
    rows_before = len(client.table("agent_profiles")._data)
    with pytest.raises(ProfileNotFoundError):
        tpl.use_template(template_id="t_root", display_name="Revenue Analyst",
                         as_subagent_of="nonexistent", actor_user_id="u")
    rows_after = client.table("agent_profiles")._data
    assert len(rows_after) == rows_before        # no orphan rows inserted
    assert not any(r.get("status") == "draft" for r in rows_after)


def test_use_template_non_staging_parent_leaves_no_orphan_drafts(monkeypatch):
    client = FakeClient({"agent_profiles": [
        dict(CATALOG["t_root"]), dict(CATALOG["t_chart"]),
        {"profile_id": "prod_parent", "slug": "main", "status": "active",
         "agent_kind": "business", "environment": "prod",
         "downstream_profile_ids": []}]})
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    monkeypatch.setattr(tpl, "record_change", lambda **k: None)
    monkeypatch.setattr("admin.services.profiles.invalidate_profile_loader", lambda *a: None)
    rows_before = len(client.table("agent_profiles")._data)
    with pytest.raises(ProfileNotFoundError):
        tpl.use_template(template_id="t_root", display_name="Revenue Analyst",
                         as_subagent_of="prod_parent", actor_user_id="u")
    rows_after = client.table("agent_profiles")._data
    assert len(rows_after) == rows_before        # no orphan rows inserted
    assert not any(r.get("status") == "draft" for r in rows_after)


def test_as_subagent_of_wires_parent(monkeypatch):
    client = FakeClient({"agent_profiles": [
        dict(CATALOG["t_chart"]),
        {"profile_id": "parent1", "slug": "main", "status": "draft", "agent_kind": "business",
         "environment": "staging", "downstream_profile_ids": []}]})
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    monkeypatch.setattr(tpl, "record_change", lambda **k: None)
    monkeypatch.setattr("admin.services.profiles.invalidate_profile_loader", lambda *a: None)
    out = tpl.use_template(template_id="t_chart", display_name="My Chart",
                           as_subagent_of="parent1", actor_user_id="u")
    parent = next(r for r in client.table("agent_profiles")._data if r["profile_id"] == "parent1")
    assert out["root"] in parent["downstream_profile_ids"]


def test_use_template_honors_explicit_slug_verbatim(monkeypatch):
    """An explicit slug is used verbatim (no -fork- suffix) so a delegation
    specialist's ask_<slug> tool name is predictable — the whole point of
    creating one via `templates use slug=...` instead of a raw fork."""
    client = _client()
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    monkeypatch.setattr(tpl, "record_change", lambda **k: None)
    monkeypatch.setattr("admin.services.profiles.invalidate_profile_loader", lambda *a: None)
    out = tpl.use_template(template_id="t_chart", display_name="Reporting specialist",
                           slug="business_reporting", actor_user_id="u")
    root = next(r for r in client.table("agent_profiles")._data
                if r["profile_id"] == out["root"])
    assert root["slug"] == "business_reporting"


def _use(monkeypatch, client):
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    monkeypatch.setattr(tpl, "record_change", lambda **k: None)
    monkeypatch.setattr("admin.services.profiles.invalidate_profile_loader", lambda *a: None)
    return tpl.use_template(template_id="t_root", display_name="Revenue Analyst", actor_user_id="u")


def test_fork_keeps_the_template_kind_when_generic_agents_are_off(monkeypatch):
    """Flag off (every deployment today): a fork is exactly what it was."""
    monkeypatch.delenv("ENABLE_GENERIC_AGENTS", raising=False)
    client = _client()
    out = _use(monkeypatch, client)
    rows = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    assert {rows[i]["agent_kind"] for i in out["created"]} == {"business"}


def test_fork_makes_generic_agents_when_the_flag_is_on(monkeypatch):
    """Flag on: the new agent AND its forked sub-agents are `custom`, so they
    run on the generic runtime and answer at /v1/agents/{slug}/chat. The
    templates themselves are never re-kinded."""
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    client = _client()
    out = _use(monkeypatch, client)
    rows = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    assert len(out["created"]) == 2
    assert {rows[i]["agent_kind"] for i in out["created"]} == {"custom"}
    assert rows["t_root"]["agent_kind"] == "business" and rows["t_chart"]["agent_kind"] == "business"
    # Each new agent owns its instructions from the start (one prompt row each).
    own = [p["slug"] for p in client.table("agent_prompts")._data if p.get("agent_kind") == "custom"]
    assert sorted(own) == sorted(f"{rows[i]['slug']}:system" for i in out["created"])


def test_new_generic_agent_does_not_keep_the_templates_description(monkeypatch):
    """The fork copied the template's own blurb ("Start here to build up")
    onto the new agent, where the agents list showed it under the agent's
    name. A forked sub-agent keeps its description: the parent reads it to
    decide when to hand off."""
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    client = _client()
    for row in client.table("agent_profiles")._data:
        row["description"] = f"about the {row['profile_id']} template"
    out = _use(monkeypatch, client)
    rows = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    root, sub = out["root"], [i for i in out["created"] if i != out["root"]][0]
    assert rows[root]["description"] is None
    assert rows[sub]["description"] == "about the t_chart template"
    assert rows["t_root"]["description"] == "about the t_root template"


def test_new_generic_agent_gets_an_address_made_from_its_name(monkeypatch):
    """Its slug is public (/v1/agents/<slug>/chat, ask_<slug>); the fork's
    default made it `mold:data-analyst-fork-1a2b3c4d`."""
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    client = _client()
    out = _use(monkeypatch, client)
    rows = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    assert rows[out["root"]]["slug"] == "revenue-analyst"
    sub = [i for i in out["created"] if i != out["root"]][0]
    assert rows[sub]["slug"].startswith("revenue-analyst-") and "mold" not in rows[sub]["slug"]
    # A second agent with the same name does not collide.
    again = _use(monkeypatch, client)
    rows = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    assert rows[again["root"]]["slug"] == "revenue-analyst-2"


def test_slug_from_name_handles_names_with_no_latin_letters():
    client = _client()
    assert tpl.slug_from_name(client, "المساعد") == "agent-1"
    assert tpl.slug_from_name(client, "24/7 Helpdesk!") == "agent-24-7-helpdesk"
    assert tpl.slug_from_name(client, "  Sales   Desk ") == "sales-desk"


def test_wizard_model_choice_is_applied_and_checked_first(monkeypatch):
    import pytest

    from admin.services.profiles import ProfileNotFoundError

    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    client = _client()
    client.table("agent_models")._data.extend([
        {"model_id": "m-local", "is_active": True, "extra_config": {}},
        {"model_id": "m-voice", "is_active": True, "extra_config": {"realtime": True}},
    ])
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    before = len(client.table("agent_profiles")._data)
    with pytest.raises(ProfileNotFoundError, match="not an active chat model"):
        tpl.use_template(template_id="t_root", display_name="X", model_id="m-voice", actor_user_id="u")
    assert len(client.table("agent_profiles")._data) == before  # nothing was forked
    out = tpl.use_template(template_id="t_root", display_name="Y", model_id="m-local", actor_user_id="u")
    rows = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    assert {rows[i]["model_main_id"] for i in out["created"]} == {"m-local"}
