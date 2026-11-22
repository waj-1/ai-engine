"""ask_agent's description names who handles the request. A build with no
plugin says "the agent"; a plugin may name its own agent through the
voice_builders hook's ask_agent_subject() (see
test_ask_agent_description_plugin.py for this deployment's wording)."""
from types import SimpleNamespace

import pytest

from voice.live import base
from voice.live.config import LiveVoiceConfig
from voice.live.providers import azure_voice_live, gemini_live


def _gemini_description() -> str:
    (decl,) = gemini_live._function_declarations(LiveVoiceConfig())
    return decl["description"]


def _azure_description() -> str:
    cfg = LiveVoiceConfig()
    cfg.model_string = "gpt-4.1-mini"
    cfg.extra_config = {"endpoint": "https://voice.example.invalid"}
    (decl,) = azure_voice_live._session_payload(cfg, "p")["session"]["tools"]
    return decl["description"]


@pytest.fixture
def no_plugin(monkeypatch):
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [])


@pytest.mark.parametrize("describe", [_gemini_description, _azure_description])
def test_no_plugin_asks_the_agent(no_plugin, describe):
    text = describe()
    assert text.startswith("Ask the agent to handle the caller's request. Use this for ANY question")
    assert "business" not in text


@pytest.mark.parametrize("describe", [_gemini_description, _azure_description])
def test_a_plugin_names_its_agent(monkeypatch, describe):
    hook = SimpleNamespace(ask_agent_subject=lambda: "the front-desk agent")
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [hook] if name == "voice_builders" else [])
    assert describe().startswith("Ask the front-desk agent to handle the caller's request. ")


def test_the_subject_is_the_only_difference(no_plugin, monkeypatch):
    neutral = _gemini_description()
    hook = SimpleNamespace(ask_agent_subject=lambda: "the front-desk agent")
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [hook])
    assert _gemini_description() == neutral.replace("the agent to handle", "the front-desk agent to handle", 1)


def test_default_subject(no_plugin):
    assert base.ask_agent_subject() == "the agent"
