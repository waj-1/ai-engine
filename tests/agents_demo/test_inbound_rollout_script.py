"""scripts/agents_demo_inbound_prompt_only.py against a fake AI database seeded
the way staging and prod were on 2026-10-07 (inbound = delegated, with tools)."""
from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from admin import seed_agents_demo as seeder
from scripts import agents_demo_inbound_prompt_only as rollout
from tests.agents_demo._fakedb import _Client

INBOUND_KINDS = ("agents_support", "agents_inquiries")


@pytest.fixture
def legacy_db(monkeypatch):
    """Seed with today's seeder, then put the inbound rows back in the shape
    the first (delegated) seeder left them in."""
    store: Dict[str, Any] = {"agent_models": [
        {"model_id": "m-brain", "name": "Gemini 3 Flash", "model_string": "gemini-3-flash-preview",
         "provider": "google_genai", "extra_config": {}, "is_active": True, "environment": "prod"},
        {"model_id": rollout.EXPECTED_REALTIME_MODEL_ID, "name": "Gemini Live",
         "model_string": "gemini-3.1-flash-live-preview", "provider": "google_genai",
         "extra_config": {"realtime": True}, "is_active": True, "environment": "staging"},
    ]}
    monkeypatch.setattr(seeder, "get_service_client", lambda: _Client(store))
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    seeder.run()
    tools = {t["python_name"]: t["tool_id"] for t in store["agent_tools"]}
    for prof in store["agent_profiles"]:
        if prof["agent_kind"] not in INBOUND_KINDS:
            continue
        # v1 of each prompt is the delegated text (what the first seeder wrote).
        for slug, vid in prof["prompt_version_ids"].items():
            row = next(v for v in store["agent_prompt_versions"] if v["prompt_version_id"] == vid)
            row.update(content=f"LEGACY {slug} — call `ask_agent` immediately", legacy=True)
        prof["tool_ids"] = [tid for name, tid in tools.items()
                            if name.startswith(("support_", "inquiry_")) or name == "agents_kb_search"][:3]
        prof["voice_config"] = {
            "enabled": True, "persona_prompt_slug": "voice_persona", "model_id": rollout.EXPECTED_REALTIME_MODEL_ID,
            "barge_in_enabled": True, "local_barge_in_sec": 0.9, "kb_prefetch_enabled": True,
            "greeting_trigger": "greet", "max_session_sec": 300,
        }
        store.setdefault("agent_profile_mcp_servers", []).append(
            {"profile_id": prof["profile_id"], "mcp_server_id": "bundle-x", "tool_subset": [], "position": 0})
    return store


def _inbound(db):
    return {p["agent_kind"]: p for p in db["agent_profiles"] if p["agent_kind"] in INBOUND_KINDS}


def _content(db, vid):
    return next(v["content"] for v in db["agent_prompt_versions"] if v["prompt_version_id"] == vid)


def test_dry_run_reports_everything_and_writes_nothing(legacy_db):
    before = copy.deepcopy(legacy_db)
    lines = []
    report = rollout.run(_Client(legacy_db), "prod", dry_run=True, out=lines.append)
    assert legacy_db == before
    for key in ("support", "inquiries"):
        text = " | ".join(report[key])
        assert "publish" in text and "voice_config" in text and "detach tools" in text
    assert any(line.startswith("# Before") for line in lines)
    assert any("WOULD publish" in line for line in lines)


def test_switches_the_inbound_profiles_and_is_idempotent(legacy_db):
    from runtime.prompts import agents_demo as p

    outbound_before = copy.deepcopy([r for r in legacy_db["agent_profiles"] if r["agent_kind"] not in INBOUND_KINDS])
    lines = []
    rollout.run(_Client(legacy_db), "prod", dry_run=False, out=lines.append)
    profs = _inbound(legacy_db)
    expected = {"agents_support": (p.SUPPORT_DIRECT_SYSTEM_PROMPT, p.SUPPORT_DIRECT_VOICE_PERSONA),
                "agents_inquiries": (p.INQUIRIES_DIRECT_SYSTEM_PROMPT, p.INQUIRIES_DIRECT_VOICE_PERSONA)}
    for kind, prof in profs.items():
        system, persona = expected[kind]
        assert _content(legacy_db, prof["prompt_version_ids"]["system"]) == system
        assert _content(legacy_db, prof["prompt_version_ids"]["voice_persona"]) == persona
        cfg = prof["voice_config"]
        assert cfg["prefetched_context"] is True and cfg["persona_prompt_slug"] == "voice_persona"
        # Kept exactly: the model, the barge-in settings already on the row, the rest.
        assert cfg["model_id"] == rollout.EXPECTED_REALTIME_MODEL_ID
        assert cfg["barge_in_enabled"] is True and cfg["local_barge_in_sec"] == 0.9
        assert cfg["greeting_trigger"] == "greet" and cfg["max_session_sec"] == 300
        # Added only because the row had none.
        assert cfg["local_barge_in_dbfs"] == -28.0 and cfg["greeting_barge_in_guard_sec"] == 4.0
        assert prof["tool_ids"] == []
        assert not [a for a in legacy_db["agent_profile_mcp_servers"] if a["profile_id"] == prof["profile_id"]]
    # The legacy versions stay for rollback; the new ones are v2.
    assert sum(1 for v in legacy_db["agent_prompt_versions"] if v.get("legacy")) == 4
    for prof in profs.values():
        for vid in prof["prompt_version_ids"].values():
            row = next(v for v in legacy_db["agent_prompt_versions"] if v["prompt_version_id"] == vid)
            assert row["version_number"] == 2 and row["notes"] == rollout.NOTES
    assert [r for r in legacy_db["agent_profiles"] if r["agent_kind"] not in INBOUND_KINDS] == outbound_before
    assert any("self-promote" in line for line in lines)

    # Second run: nothing to do, nothing written.
    snapshot = copy.deepcopy(legacy_db)
    report = rollout.run(_Client(legacy_db), "prod", dry_run=False, out=lambda *_: None)
    assert report == {"support": [], "inquiries": []}
    assert legacy_db == snapshot


def test_a_fresh_stack_needs_nothing(monkeypatch):
    store: Dict[str, Any] = {"agent_models": [
        {"model_id": "m-brain", "name": "B", "model_string": "gemini-3-flash-preview", "provider": "google_genai",
         "extra_config": {}, "is_active": True, "environment": "staging"},
        {"model_id": rollout.EXPECTED_REALTIME_MODEL_ID, "name": "L", "model_string": "gemini-3.1-flash-live-preview",
         "provider": "google_genai", "extra_config": {"realtime": True}, "is_active": True,
         "environment": "staging"},
    ]}
    monkeypatch.setattr(seeder, "get_service_client", lambda: _Client(store))
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    seeder.run()
    report = rollout.run(_Client(store), "staging", dry_run=False, out=lambda *_: None)
    assert report == {"support": [], "inquiries": []}


def test_other_environment_rows_are_untouched(legacy_db):
    staging_copy = copy.deepcopy(next(p for p in legacy_db["agent_profiles"]
                                      if p["agent_kind"] == "agents_support"))
    staging_copy.update(profile_id="staging-support", environment="staging")
    legacy_db["agent_profiles"].append(staging_copy)
    rollout.run(_Client(legacy_db), "prod", dry_run=False, out=lambda *_: None)
    assert next(p for p in legacy_db["agent_profiles"] if p["profile_id"] == "staging-support") == staging_copy


@pytest.mark.parametrize("value,env", [("production", "prod"), ("prod", "prod"), ("staging", "staging")])
def test_env_names(value, env):
    assert rollout.normalize_env(value) == env


def test_env_is_required():
    with pytest.raises(SystemExit):
        rollout.normalize_env(None)


def test_legacy_nested_voice_config_is_flattened():
    cfg = rollout.target_voice_config({"live": {"enabled": True, "model_id": "x"}, "voice_id": "Kore"})
    assert "live" not in cfg and cfg["model_id"] == "x" and cfg["prefetched_context"] is True
