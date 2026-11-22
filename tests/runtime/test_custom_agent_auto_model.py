"""A custom agent pinned to the Automatic model: it runs on whatever model
the install can use, and says plainly when there is none."""
from __future__ import annotations

import pytest

from core import model_resolver as mr
from core.agent_profile import AgentProfileLoader, Profile

AUTO = {"model_id": "m-auto", "name": mr.AUTO_MODEL_NAME, "provider": mr.AUTO_PROVIDER,
        "model_string": mr.AUTO_MODEL_STRING, "environment": "staging", "is_active": True,
        "extra_config": {"auto": True}}
LOCAL = {"model_id": "m-local", "name": "Local", "provider": "ollama", "model_string": "qwen2.5:3b",
         "environment": "staging", "is_active": True, "extra_config": {}}


@pytest.fixture
def demo(monkeypatch):
    profile = Profile(profile_id="p-demo", agent_kind="custom", slug="demo",
                      environment="staging", model_main=dict(AUTO))
    monkeypatch.setattr(AgentProfileLoader, "load_delegate",
                        classmethod(lambda cls, kind, slug, env: profile))
    return profile


async def _events(agent, message="hello"):
    return [e async for e in agent.run_stream(message, {})]


async def test_no_usable_model_is_a_plain_message_not_an_error_dump(demo, monkeypatch):
    from runtime.agents.custom import create_custom_agent

    monkeypatch.setattr(mr, "load_usable_model", lambda env=None, client=None: None)
    agent = create_custom_agent("demo")
    assert agent.llm is None and agent.model_unresolved is True
    events = await _events(agent)
    assert events == [{"type": "error", "message": mr.NO_MODEL_MESSAGE}]
    text = events[0]["message"]
    assert "No model is configured" in text and "Models" in text and "Model keys" in text


async def test_it_runs_on_the_model_added_later_without_an_edit(demo, monkeypatch):
    from runtime import llm_factory
    from runtime.agents.custom import create_custom_agent

    usable = {"row": None}
    monkeypatch.setattr(mr, "load_usable_model", lambda env=None, client=None: usable["row"])
    monkeypatch.setitem(llm_factory._BUILDERS, "ollama", lambda m: ("client-for", m["model_string"]))
    llm_factory.clear_llm_cache()

    assert create_custom_agent("demo").llm is None
    usable["row"] = dict(LOCAL)  # the operator adds a local model; the agent is not touched
    agent = create_custom_agent("demo")
    assert agent.llm == ("client-for", "qwen2.5:3b")
    assert agent.model_unresolved is False
    # The request runs on (and reports) the real model; the cached profile keeps its pin.
    assert agent.profile.model_main["model_string"] == "qwen2.5:3b"
    assert demo.model_main["provider"] == mr.AUTO_PROVIDER
    llm_factory.clear_llm_cache()


async def test_an_agent_with_a_real_model_and_no_client_keeps_the_old_message(monkeypatch):
    from runtime.agents.custom import create_custom_agent

    profile = Profile(profile_id="p", agent_kind="custom", slug="plain", environment="staging",
                      model_main=None)
    monkeypatch.setattr(AgentProfileLoader, "load_delegate",
                        classmethod(lambda cls, kind, slug, env: profile))
    agent = create_custom_agent("plain")
    assert agent.model_unresolved is False
    assert await _events(agent) == [{"type": "error", "message": "This agent has no model configured."}]
