"""The demo agents have no personal names (user feedback 2026-10-08): not in
what they are told to say, not in the greeting, not in the story cards — and
scripts/agents_demo_sync_prompts.py carries that to already-seeded stacks."""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict

import pytest

from admin import seed_agents_demo as seeder
from runtime.prompts import agents_demo as prompts
from runtime.tools.agents_demo import data
from scripts import agents_demo_sync_prompts as sync
from tests.agents_demo._fakedb import _Client

_NAMES = re.compile(r"\b(Lina|Adam|Salma|Rami)\b|لينا|آدم|سلمى|رامي")


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


@pytest.mark.parametrize("kind,slug", [(k, s) for k, s, _d, _c in prompts.prompt_specs()])
def test_no_prompt_names_the_agent(kind, slug):
    content = next(c for k, s, _d, c in prompts.prompt_specs() if (k, s) == (kind, slug))
    assert not _NAMES.search(content), (kind, slug, _NAMES.search(content).group(0))
    assert "never give or invent one" in content  # the no-name rule rides the gender line


@pytest.mark.parametrize("key", list(data.AGENTS))
def test_no_greeting_names_the_agent(key):
    assert not _NAMES.search(prompts.GREETING_TRIGGERS[key])


@pytest.mark.parametrize("key", list(data.AGENTS))
def test_no_story_text_names_the_agent(key):
    for case in data.cases(key):
        hits = [s for s in _strings(case) if _NAMES.search(s)]
        assert not hits, (key, case["id"], hits[:1])


@pytest.fixture
def seeded_db(monkeypatch):
    """A stack seeded BEFORE the change: every prompt version and greeting
    still carries the old named text."""
    store: Dict[str, Any] = {"agent_models": [
        {"model_id": "m-brain", "name": "Gemini 3 Flash", "model_string": "gemini-3-flash-preview",
         "provider": "google_genai", "extra_config": {}, "is_active": True, "environment": "prod"},
        {"model_id": "m-live", "name": "Gemini Live", "model_string": "gemini-3.1-flash-live-preview",
         "provider": "google_genai", "extra_config": {"realtime": True}, "is_active": True,
         "environment": "staging"},
    ]}
    monkeypatch.setattr(seeder, "get_service_client", lambda: _Client(store))
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    seeder.run()
    for v in store["agent_prompt_versions"]:
        v["content"] = "You are Lina. " + v["content"]
    for p in store["agent_profiles"]:
        p["voice_config"] = {**p["voice_config"], "greeting_trigger": "say you are Lina", "max_session_sec": 300}
    return store


def test_dry_run_writes_nothing(seeded_db):
    before = copy.deepcopy(seeded_db)
    report = sync.run(_Client(seeded_db), "prod", dry_run=True, out=lambda *_: None)
    assert seeded_db == before
    assert all(any("publish" in c for c in changes) and any("greeting" in c for c in changes)
               for changes in report.values())


def test_converges_every_profile_and_is_idempotent(seeded_db):
    sync.run(_Client(seeded_db), "prod", dry_run=False, out=lambda *_: None)
    versions = {v["prompt_version_id"]: v["content"] for v in seeded_db["agent_prompt_versions"]}
    expected = {(k, s): c for k, s, _d, c in prompts.prompt_specs()}
    for prof in seeded_db["agent_profiles"]:
        for slug, vid in prof["prompt_version_ids"].items():
            assert versions[vid] == expected[(prof["agent_kind"], slug)]
        key = data.AGENT_BY_KIND[prof["agent_kind"]].key
        assert prof["voice_config"]["greeting_trigger"] == prompts.GREETING_TRIGGERS[key]
        assert prof["voice_config"]["max_session_sec"] == 300  # other keys untouched
    # old versions are kept for rollback
    assert any(c.startswith("You are Lina. ") for c in versions.values())
    again = sync.run(_Client(seeded_db), "prod", dry_run=False, out=lambda *_: None)
    assert all(changes == [] for changes in again.values())
