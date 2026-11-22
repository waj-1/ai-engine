"""admin/seed_agents_demo.py: insert-only, idempotent, safe on every build."""
from __future__ import annotations

import logging
from typing import Any, Dict

import pytest

from admin import seed_agents_demo as seeder
from tests.agents_demo._fakedb import _Client, _Q
from tests.scoped_build import use_agents_demo, use_orchard

@pytest.fixture
def db(monkeypatch):
    store: Dict[str, Any] = {"agent_models": [
        {"model_id": "m-brain", "name": "Gemini 3 Flash", "model_string": "gemini-3-flash-preview",
         "provider": "google_genai", "extra_config": {}, "is_active": True, "environment": "staging"},
        {"model_id": "m-live", "name": "Gemini Live", "model_string": "gemini-3.1-flash-live-preview",
         "provider": "google_genai", "extra_config": {"realtime": True}, "is_active": True, "environment": "staging"},
        {"model_id": "m-prod", "name": "Gemini 3 Flash", "model_string": "gemini-3-flash-preview",
         "provider": "google_genai", "extra_config": {}, "is_active": True, "environment": "prod"},
    ]}
    monkeypatch.setattr(seeder, "get_service_client", lambda: _Client(store))
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")  # the demo seeds only where it can run
    monkeypatch.setenv("ENVIRONMENT", "staging")
    return store


def _profiles(db):
    return [r for r in db.get("agent_profiles", []) if r["slug"].startswith("agents_")]


def test_seeds_everything_on_the_full_engine_and_is_idempotent(db, caplog):
    caplog.set_level(logging.WARNING)
    seeder.run()
    profiles = _profiles(db)
    assert sorted(p["agent_kind"] for p in profiles) == ["agents_collection", "agents_inquiries",
                                                         "agents_sales", "agents_support"]
    assert all(p["environment"] == "staging" and p["status"] == "active" for p in profiles)
    assert all(p["model_main_id"] == "m-brain" for p in profiles)
    assert all(set(p["prompt_version_ids"]) == {"system", "voice_persona"} for p in profiles)
    assert all(p["data_source_ids"] for p in profiles)
    # Outbound kinds carry their action tools; the inbound kinds are prompt-only.
    tools = {p["agent_kind"]: p["tool_ids"] for p in profiles}
    assert tools["agents_collection"] and tools["agents_sales"]
    assert tools["agents_support"] == [] and tools["agents_inquiries"] == []
    assert len(db["agent_data_sources"]) == 1 and len(db["agent_prompt_versions"]) == 8
    assert len(db["agent_mcp_servers"]) == 2 and len(db["agent_profile_mcp_servers"]) == 2
    attached = {p["profile_id"] for p in profiles if p["agent_kind"] in ("agents_collection", "agents_sales")}
    assert {a["profile_id"] for a in db["agent_profile_mcp_servers"]} == attached
    counts = {t: len(db[t]) for t in ("agent_tools", "agent_prompts", "agent_prompt_versions", "agent_profiles",
                                      "agent_mcp_servers", "agent_profile_mcp_servers", "agent_data_sources")}
    seeder.run()
    assert {t: len(db[t]) for t in counts} == counts  # second boot inserts nothing
    assert "skipped" not in caplog.text


def test_inbound_profiles_are_seeded_with_the_prompt_only_prompts(db):
    from runtime.prompts.agents_demo import (
        INQUIRIES_DIRECT_SYSTEM_PROMPT,
        INQUIRIES_DIRECT_VOICE_PERSONA,
        SUPPORT_DIRECT_SYSTEM_PROMPT,
        SUPPORT_DIRECT_VOICE_PERSONA,
    )

    seeder.run()
    content = {v["prompt_version_id"]: v["content"] for v in db["agent_prompt_versions"]}
    by_kind = {p["agent_kind"]: p["prompt_version_ids"] for p in _profiles(db)}
    assert content[by_kind["agents_support"]["system"]] == SUPPORT_DIRECT_SYSTEM_PROMPT
    assert content[by_kind["agents_support"]["voice_persona"]] == SUPPORT_DIRECT_VOICE_PERSONA
    assert content[by_kind["agents_inquiries"]["system"]] == INQUIRIES_DIRECT_SYSTEM_PROMPT
    assert content[by_kind["agents_inquiries"]["voice_persona"]] == INQUIRIES_DIRECT_VOICE_PERSONA


def test_a_reboot_never_reattaches_a_bundle_to_an_inbound_profile(db):
    """The rollout script detaches the inbound tools; a later boot must not
    undo it (the bundle attach is attach-when-none)."""
    seeder.run()
    seeder.run()
    inbound = {p["profile_id"] for p in _profiles(db) if p["agent_kind"] in ("agents_support", "agents_inquiries")}
    assert not [a for a in db["agent_profile_mcp_servers"] if a["profile_id"] in inbound]


def test_operator_edits_survive_a_reboot(db):
    seeder.run()
    p = next(p for p in _profiles(db) if p["agent_kind"] == "agents_support")
    p["voice_config"]["enabled"] = False
    p["model_main_id"] = "m-other"
    version = db["agent_prompt_versions"][0]
    version["content"] = "operator edit"
    seeder.run()
    assert p["voice_config"]["enabled"] is False and p["model_main_id"] == "m-other"
    assert version["content"] == "operator edit"


def test_every_kind_seeds_pre_fetched_context_voice(db):
    """Outbound (direct action tools) and inbound (prompt-only) alike: no
    delegated ask_agent call anywhere, on the barge-in values tuned live."""
    seeder.run()
    by_kind = {p["agent_kind"]: p["voice_config"] for p in _profiles(db)}
    for cfg in by_kind.values():
        assert cfg["prefetched_context"] is True and cfg["local_barge_in_sec"] == 0.6
        assert cfg["local_barge_in_dbfs"] == -28.0 and cfg["greeting_barge_in_guard_sec"] == 4.0
        assert cfg["greeting_input_mute_sec"] == 12.0
        assert "kb_prefetch_enabled" not in cfg and "progress_spoken_by_language" not in cfg
    for cfg in by_kind.values():
        assert cfg["enabled"] is True and cfg["model_id"] == "m-live"
        assert cfg["persona_prompt_slug"] == "voice_persona" and cfg["greeting_trigger"]
        assert cfg["max_session_sec"] == 300 and cfg["max_concurrent_per_ip"] == 4
        assert "live" not in cfg  # flat shape


def test_no_realtime_row_seeds_voice_disabled(db):
    db["agent_models"] = [m for m in db["agent_models"] if not m["extra_config"].get("realtime")]
    seeder.run()
    assert all(p["voice_config"]["enabled"] is False for p in _profiles(db))


def test_prod_rows_use_prod_models(db, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    seeder.run()
    assert {p["environment"] for p in _profiles(db)} == {"prod"}
    assert all(p["model_main_id"] == "m-prod" for p in _profiles(db))


def test_runs_on_the_agents_build_and_never_on_another_clients(db, monkeypatch):
    use_orchard(monkeypatch)
    seeder.run()
    assert "_inserts" not in db
    use_agents_demo(monkeypatch)
    seeder.run()
    assert len(_profiles(db)) == 4


def test_never_raises(monkeypatch):
    def _down():
        raise RuntimeError("db down")

    monkeypatch.setattr(seeder, "get_service_client", _down)
    seeder.run()


def test_tool_rows_share_the_kb_tool_across_kinds():
    rows = seeder.tool_rows()
    kb = rows["agents_kb_search"]
    assert kb["agent_kind"] == "agents_support"
    assert set(kb["additional_agent_kinds"]) == {"agents_inquiries", "agents_collection", "agents_sales"}
    assert rows["collection_send_payment_link"]["scope"] == "write"
    assert rows["support_check_outage"]["scope"] == "read"


def test_brain_prefers_in_region_azure_only_when_its_key_is_set(monkeypatch):
    rows = [{"model_id": "az", "provider": "azure_openai", "model_string": "gpt-5-mini", "extra_config": {}},
            {"model_id": "g", "provider": "google_genai", "model_string": "gemini-3-flash-preview", "extra_config": {}}]
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    assert seeder.pick_brain_model(rows) == "g"
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "x")
    assert seeder.pick_brain_model(rows) == "az"


def test_prod_seeds_voice_on_the_staging_tagged_realtime_row(db, monkeypatch):
    # Mirrors the real AI database: prod has chat rows but NO prod realtime
    # row — prod's live voice runs on staging-tagged rows. Scoping the
    # realtime pick to prod seeded voice OFF on the first prod rollout.
    monkeypatch.setenv("ENVIRONMENT", "production")
    seeder.run()
    profiles = _profiles(db)
    assert all(p["voice_config"]["enabled"] is True for p in profiles)
    assert all(p["voice_config"]["model_id"] == "m-live" for p in profiles)
    assert all(p["model_main_id"] == "m-prod" for p in profiles)


def test_realtime_prefers_the_tuned_flash_live_row():
    rows = [
        {"model_id": "old", "provider": "google_genai", "model_string": "gemini-2.5-flash-native-audio",
         "extra_config": {"realtime": True}},
        {"model_id": "tuned", "provider": "google_genai", "model_string": seeder.PREFERRED_REALTIME,
         "extra_config": {"realtime": True}},
    ]
    assert seeder.pick_realtime_model(rows) == "tuned"
    assert seeder.pick_realtime_model(rows[:1]) == "old"


def test_tool_insert_race_with_a_sibling_boot_still_seeds(db, monkeypatch):
    # Staging, prod and the control-plane boot together on one database: a
    # sibling inserts a tool between our select and insert (23505). That
    # used to abort the whole seed for that environment.
    real_execute = _Q.execute
    raced = {"done": False}

    def racing_execute(self):
        if self.table == "agent_tools" and self.op == "insert" and not raced["done"]:
            raced["done"] = True
            real_execute(self)  # the sibling's insert lands first
            raise RuntimeError("{'code': '23505', 'message': 'duplicate key value'}")
        return real_execute(self)

    monkeypatch.setattr(_Q, "execute", racing_execute)
    seeder.run()
    assert raced["done"]
    assert len(_profiles(db)) == 4


def test_nothing_is_seeded_without_a_key_the_demo_can_run_on(db, monkeypatch):
    """The demo's brain is a Gemini or Azure OpenAI model. Seeded without
    either key, a fresh install listed four Active agents that could not
    answer. They appear on the first boot after a key is set."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    seeder.run()
    assert _profiles(db) == [] and "agent_prompts" not in db
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    seeder.run()
    assert len(_profiles(db)) == 4
