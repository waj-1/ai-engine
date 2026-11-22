import copy

from runtime import agent as agent_mod
from core import model_resolver


class _Profile:
    model_main = {"provider": "google_genai", "model_string": "gemini-3-flash",
                  "temperature": 0.2, "extra_config": {}}


def test_build_llm_uses_override_when_present(monkeypatch):
    built = {}
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda m: built.update(m) or "LLM")
    model_resolver.set_model_override(
        {"provider": "anthropic", "model_string": "claude-sonnet-4",
         "temperature": 0.9, "extra_config": {"location": "us-east5"}}
    )
    try:
        out = agent_mod.RuntimeAgent._build_llm(_Profile())
    finally:
        model_resolver.clear_model_override()
    assert out == "LLM"
    assert built["model_string"] == "claude-sonnet-4"
    assert built["provider"] == "anthropic"


def test_build_llm_returns_none_without_main_model_or_override(monkeypatch):
    # No main model and no override → nothing to build.
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda m: "LLM")
    assert agent_mod.RuntimeAgent._build_llm(type("P", (), {"model_main": None})()) is None


def test_build_llm_does_not_mutate_profile_and_merges_extra_config(monkeypatch):
    # build_chat_model is monkeypatched to echo the merged dict so we can inspect it.
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda m: m)
    profile = type("P", (), {
        "model_main": {"provider": "google_genai", "model_string": "gemini-3-flash",
                       "temperature": 0.2, "extra_config": {"location": "us-central1"}},
    })()
    before = copy.deepcopy(profile.model_main)
    model_resolver.set_model_override({
        "provider": "anthropic", "model_string": "claude",
        "extra_config": {"location": "us-east5", "top_p": 0.8},
    })
    try:
        merged = agent_mod.RuntimeAgent._build_llm(profile)
    finally:
        model_resolver.clear_model_override()
    # profile.model_main must be untouched (cached profile immutability)
    assert profile.model_main == before
    # override wins on model_string; extra_config merges (location overridden, top_p added)
    assert merged["model_string"] == "claude"
    assert merged["extra_config"] == {"location": "us-east5", "top_p": 0.8}
