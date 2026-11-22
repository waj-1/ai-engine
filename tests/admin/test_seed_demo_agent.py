"""admin/seed_demo_agent.py: the demo agent is seeded once, on the builds
that want it, and is never brought back or overwritten."""
from __future__ import annotations

import pytest

from admin import seed_demo_agent as seeder
from core import model_resolver as mr
from tests.fake_pg import Client


@pytest.fixture
def db(monkeypatch):
    store = {}
    monkeypatch.setattr(seeder, "get_service_client", lambda: Client(store))
    monkeypatch.setenv("SEED_DEMO_AGENT", "true")
    monkeypatch.setenv("ENVIRONMENT", "development")
    for key in ("GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return store


def _demo(db):
    return [p for p in db.get("agent_profiles", []) if p["slug"] == "demo"]


def test_seeds_one_active_custom_agent_with_its_own_instructions_and_no_tools(db):
    assert seeder.run()["seeded"] is True
    (p,) = _demo(db)
    assert (p["agent_kind"], p["status"], p["is_active"], p["environment"]) == ("custom", "active", True, "staging")
    assert p["tool_ids"] == [] and p["mcp_server_ids"] == [] and p["data_source_ids"] == []
    assert p["endpoint_path"] == "/v1/agents/demo/chat"
    assert "demo" in p["display_name"].lower() and "demo" in p["description"].lower()
    (prompt,) = db["agent_prompts"]
    assert (prompt["agent_kind"], prompt["slug"]) == ("custom", "demo:system")  # its own row
    (version,) = db["agent_prompt_versions"]
    assert p["prompt_version_ids"] == {"system": version["prompt_version_id"]}
    assert "you are a demo" in version["content"].lower()
    assert "Agents > New agent" in version["content"]


def test_it_is_pinned_to_the_automatic_model_even_with_no_usable_model(db):
    """No key and no local model: the agent still exists."""
    seeder.run()
    (model,) = db["agent_models"]
    assert model["provider"] == mr.AUTO_PROVIDER and model["name"] == mr.AUTO_MODEL_NAME
    assert _demo(db)[0]["model_main_id"] == model["model_id"]


def test_a_second_boot_inserts_nothing(db):
    seeder.run()
    counts = {t: len(rows) for t, rows in db.items() if t != "_calls"}
    assert seeder.run() == {"seeded": False, "reason": "already seeded"}
    assert {t: len(rows) for t, rows in db.items() if t != "_calls"} == counts


def test_operator_edits_survive_a_reboot(db):
    seeder.run()
    p = _demo(db)[0]
    p.update(display_name="Front desk", description="Ours now", model_main_id="m-other", status="archived",
             is_active=False)
    db["agent_prompt_versions"][0]["content"] = "edited"
    seeder.run()
    (again,) = _demo(db)
    assert (again["display_name"], again["model_main_id"], again["status"]) == ("Front desk", "m-other", "archived")
    assert db["agent_prompt_versions"][0]["content"] == "edited" and len(db["agent_prompt_versions"]) == 1


def test_a_deleted_demo_agent_does_not_come_back(db):
    seeder.run()
    db["agent_profiles"].clear()
    assert seeder.run()["seeded"] is False
    assert _demo(db) == []


def test_a_renamed_demo_agent_is_not_seeded_a_second_time(db):
    seeder.run()
    db["agent_profiles"][0]["slug"] = "front-desk"
    seeder.run()
    assert [p["slug"] for p in db["agent_profiles"]] == ["front-desk"]


def test_an_existing_agent_at_that_address_is_left_alone(db):
    db["agent_profiles"] = [{"profile_id": "mine", "agent_kind": "custom", "slug": "demo",
                             "environment": "staging", "display_name": "Mine", "status": "draft"}]
    assert seeder.run()["seeded"] is False
    assert db["agent_profiles"] == [{"profile_id": "mine", "agent_kind": "custom", "slug": "demo",
                                     "environment": "staging", "display_name": "Mine", "status": "draft"}]
    assert not db.get("agent_prompts") and not db.get("agent_models")
    db["agent_profiles"].clear()
    assert seeder.run()["seeded"] is False  # the marker was written: still never seeded


def test_deleting_the_marker_seeds_it_again(db):
    seeder.run()
    db["agent_profiles"].clear()
    db["agent_env_vars"].clear()
    assert seeder.run()["seeded"] is True
    assert len(_demo(db)) == 1 and len(db["agent_models"]) == 1 and len(db["agent_prompt_versions"]) == 1


def test_the_marker_is_a_visible_settings_row(db):
    seeder.run()
    (marker,) = db["agent_env_vars"]
    assert (marker["environment"], marker["name"], marker["is_secret"]) == ("staging", "DEMO_AGENT_SEEDED", False)
    assert "Delete this row" in marker["description"]


def test_default_is_on_only_with_no_product_and_the_agents_api(db, monkeypatch):
    monkeypatch.delenv("SEED_DEMO_AGENT")
    monkeypatch.setattr("core.plugins.has_product", lambda: False)
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    assert seeder.enabled() is True
    monkeypatch.setattr("core.plugins.has_product", lambda: True)
    assert seeder.enabled() is False  # a product build has its own agents
    assert seeder.run() == {"seeded": False, "reason": "off"} and not db.get("agent_profiles")
    monkeypatch.setenv("SEED_DEMO_AGENT", "true")
    assert seeder.enabled() is True  # ... unless it asks for one
    monkeypatch.setattr("core.plugins.has_product", lambda: False)
    monkeypatch.setenv("SEED_DEMO_AGENT", "false")
    assert seeder.enabled() is False
    monkeypatch.delenv("SEED_DEMO_AGENT")
    monkeypatch.delenv("ENABLE_GENERIC_AGENTS")
    assert seeder.enabled() is False  # nothing would serve it


def test_a_database_failure_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("down")

    monkeypatch.setenv("SEED_DEMO_AGENT", "true")
    monkeypatch.setattr(seeder, "get_service_client", boom)
    assert seeder.run() == {"seeded": False, "reason": "error"}


def test_it_runs_at_boot_after_the_models():
    import main

    order = list(main._BOOT_SEEDERS)
    assert order.index("admin.seed_demo_agent") > order.index("admin.seed_agent_models")


def test_the_agents_list_labels_it_as_a_demo():
    from pathlib import Path

    html = (Path(seeder.__file__).parent / "templates" / "agents_list.html").read_text()
    assert "r.kind == 'custom' and r.slug == 'demo'" in html and ">Demo</span>" in html
