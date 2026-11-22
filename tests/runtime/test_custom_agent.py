"""The generic `custom` agent (runtime/agents/custom.py): profile resolution
by slug, the profile-driven tool set, the per-turn context and the loop."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

from core.agent_profile import MissingProfileError


def _profile(**over):
    base = dict(profile_id="p-1", slug="helpdesk", agent_kind="custom", environment="staging",
                data_source_ids=[], model_main={"model_string": "m"}, is_fallback=False)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_runtime_init(monkeypatch):
    """Skip the profile/LLM load; record what RuntimeAgent was asked for."""
    seen = {}

    def _init(self, agent_kind, environment=None, profile_slug=None):
        seen.update(agent_kind=agent_kind, profile_slug=profile_slug)
        self.agent_kind = agent_kind
        self.profile = seen.get("profile") or _profile()
        self.llm = object()

    monkeypatch.setattr("runtime.agent.RuntimeAgent.__init__", _init)
    return seen


class _Msg(SimpleNamespace):
    pass


class _FakeLLM:
    """Scripted chat model: one (content, tool_calls) pair per round."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.bound = None
        self.seen_messages = []

    def bind_tools(self, tools):
        self.bound = [t.name for t in tools]
        return self

    async def astream_events(self, messages, version="v2"):
        self.seen_messages.append(list(messages))
        content, calls = self.rounds.pop(0)
        if content:
            yield {"event": "on_chat_model_stream",
                   "data": {"chunk": _Msg(content=content, tool_call_chunks=[])}}
        yield {"event": "on_chat_model_end",
               "data": {"output": _Msg(content=content, tool_calls=calls, usage_metadata=None)}}


def _tool(name, fn):
    return StructuredTool.from_function(func=fn, name=name, description=name)


def _prepared(tools=()):
    return lambda ctx, all_tools: {"system_prompt": "SYS", "tools": list(tools),
                                   "tools_by_name": {t.name: t for t in tools}}


# ------------------------------------------------------------- construction
def test_builds_the_custom_kind_by_slug(stub_runtime_init):
    from runtime.agents.custom import create_custom_agent

    agent = create_custom_agent("helpdesk", locale="ar-SA")
    assert stub_runtime_init["agent_kind"] == "custom"
    assert stub_runtime_init["profile_slug"] == "helpdesk"
    assert agent.locale == "ar"  # region folded, language kept (never forced to "en")
    assert create_custom_agent("helpdesk", locale="fr").locale == "fr"
    assert create_custom_agent("helpdesk").locale == ""


def test_unknown_slug_fails_closed_instead_of_serving_another_agent(monkeypatch):
    """The base class falls back to the kind's ACTIVE profile on an unknown
    slug. For `custom` that would be a different agent answering — so the
    override must raise and must never consult load_active."""
    from core.agent_profile import AgentProfileLoader
    from runtime.agents.custom import CustomRuntimeAgent

    def _missing(kind, slug, env):
        raise MissingProfileError(f"{kind}/{slug}")

    def _active(*a, **k):  # pragma: no cover - the assertion is that this never runs
        raise AssertionError("load_active must not be consulted for a custom agent")

    monkeypatch.setattr(AgentProfileLoader, "load_delegate", staticmethod(_missing))
    monkeypatch.setattr(AgentProfileLoader, "load_active", staticmethod(_active))
    with pytest.raises(MissingProfileError):
        CustomRuntimeAgent._load_profile_by_slug("custom", "nope", "staging")


def test_slug_lookup_is_scoped_to_kind_and_environment(monkeypatch):
    from core.agent_profile import AgentProfileLoader
    from runtime.agents.custom import CustomRuntimeAgent

    seen = {}

    def _load(kind, slug, env):
        seen.update(kind=kind, slug=slug, env=env)
        return _profile()

    monkeypatch.setattr(AgentProfileLoader, "load_delegate", staticmethod(_load))
    CustomRuntimeAgent._load_profile_by_slug("custom", "helpdesk", "prod")
    assert seen == {"kind": "custom", "slug": "helpdesk", "env": "prod"}


# -------------------------------------------------------------------- tools
def test_knowledge_tool_only_when_the_profile_attaches_a_source(stub_runtime_init):
    from runtime.agents.custom import create_custom_agent

    assert create_custom_agent("helpdesk")._knowledge_tools() == []
    stub_runtime_init["profile"] = _profile(data_source_ids=["kb-1", "kb-2"])
    names = [t.name for t in create_custom_agent("helpdesk")._knowledge_tools()]
    assert names == ["search_knowledge"]


async def test_no_code_tool_pool_and_knowledge_added_after_resolution(stub_runtime_init, monkeypatch):
    """What binds is the profile's attachments plus knowledge search — the
    agent hands `prepare` an EMPTY pool, so no tool can come from code."""
    from runtime.agents.custom import create_custom_agent

    stub_runtime_init["profile"] = _profile(data_source_ids=["kb-1"])
    agent = create_custom_agent("helpdesk")
    pools = []
    attached = _tool("lookup_order", lambda order_id: "{}")

    def _prepare(ctx, all_tools):
        pools.append(list(all_tools))
        return {"system_prompt": "SYS", "tools": [attached], "tools_by_name": {"lookup_order": attached}}

    monkeypatch.setattr(agent, "prepare", _prepare)
    agent.llm = _FakeLLM([("Hello.", [])])
    events = [e async for e in agent.run_stream("hi", {})]
    assert pools == [[]]
    assert agent.llm.bound == ["lookup_order", "search_knowledge"]
    assert "error" not in [e["type"] for e in events]


async def test_operator_tool_named_search_knowledge_wins(stub_runtime_init, monkeypatch):
    from runtime.agents.custom import create_custom_agent

    stub_runtime_init["profile"] = _profile(data_source_ids=["kb-1"])
    agent = create_custom_agent("helpdesk")
    theirs = _tool("search_knowledge", lambda query: "theirs")
    monkeypatch.setattr(agent, "prepare", _prepared([theirs]))
    agent.llm = _FakeLLM([("Hello.", [])])
    [e async for e in agent.run_stream("hi", {})]
    assert agent.llm.bound == ["search_knowledge"]


# --------------------------------------------------------------------- loop
async def test_tool_round_then_answer_and_provenance_recorded(stub_runtime_init, monkeypatch):
    from runtime.agents import custom
    from runtime.agents.custom import create_custom_agent

    agent = create_custom_agent("helpdesk")
    calls = []
    tool = _tool("lookup_order", lambda order_id: calls.append(order_id) or json.dumps({"status": "shipped"}))
    monkeypatch.setattr(agent, "prepare", _prepared([tool]))
    recorded = []
    monkeypatch.setattr(custom, "record_tool_result",
                        lambda payload, tool_name="": recorded.append((tool_name, payload)))
    agent.llm = _FakeLLM([
        ("", [{"name": "lookup_order", "args": {"order_id": "A17"}, "id": "c1"}]),
        ("Your order has shipped.", []),
    ])
    events = [e async for e in agent.run_stream("where is order A17?", {})]
    kinds = [e["type"] for e in events]
    assert calls == ["A17"]
    assert "error" not in kinds and kinds[-2:] == ["usage", "turn_meta"]
    assert next(e for e in events if e["type"] == "tool_activity")["tools"] == ["lookup_order"]
    assert "".join(e["content"] for e in events if e["type"] == "answer") == "Your order has shipped."
    assert recorded == [("lookup_order", json.dumps({"status": "shipped"}))]
    usage = next(e for e in events if e["type"] == "usage")
    assert usage["iterations"] == 2 and usage["tool_count"] == 1


async def test_failing_tool_is_reported_to_the_model_not_raised(stub_runtime_init, monkeypatch):
    from runtime.agents.custom import create_custom_agent

    agent = create_custom_agent("helpdesk")

    def _boom(order_id):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(agent, "prepare", _prepared([_tool("lookup_order", _boom)]))
    agent.llm = _FakeLLM([
        ("", [{"name": "lookup_order", "args": {"order_id": "A17"}, "id": "c1"},
              {"name": "no_such_tool", "args": {}, "id": "c2"}]),
        ("I could not check that right now.", []),
    ])
    events = [e async for e in agent.run_stream("where is order A17?", {})]
    assert "error" not in [e["type"] for e in events]
    tool_msgs = [m.content for m in agent.llm.seen_messages[1] if type(m).__name__ == "ToolMessage"]
    assert "tool_failed" in tool_msgs[0] and "unknown tool no_such_tool" in tool_msgs[1]


async def test_announce_without_a_tool_call_gets_one_forced_round(stub_runtime_init, monkeypatch):
    from runtime.agents.custom import create_custom_agent

    agent = create_custom_agent("helpdesk")
    tool = _tool("lookup_order", lambda order_id: "{}")
    monkeypatch.setattr(agent, "prepare", _prepared([tool]))
    agent.llm = _FakeLLM([
        ("One moment, let me check.", []),
        ("", [{"name": "lookup_order", "args": {"order_id": "A17"}, "id": "c1"}]),
        ("It has shipped.", []),
    ])
    events = [e async for e in agent.run_stream("where is order A17?", {})]
    assert next(e for e in events if e["type"] == "usage")["tool_count"] == 1
    nudge = agent.llm.seen_messages[1][-1].content
    assert "called no tool" in nudge


async def test_no_forced_round_for_a_tool_less_agent(stub_runtime_init, monkeypatch):
    """With nothing to call, "let me check" is just a sentence: forcing a
    round would loop a model that has no tool to satisfy it."""
    from runtime.agents.custom import create_custom_agent

    agent = create_custom_agent("helpdesk")
    monkeypatch.setattr(agent, "prepare", _prepared([]))
    agent.llm = _FakeLLM([("One moment, let me check.", [])])
    events = [e async for e in agent.run_stream("hi", {})]
    assert next(e for e in events if e["type"] == "usage")["iterations"] == 1
    assert agent.llm.bound is None  # bind_tools never called with an empty set


async def test_profile_without_a_model_reports_instead_of_crashing(stub_runtime_init):
    from runtime.agents.custom import create_custom_agent

    agent = create_custom_agent("helpdesk")
    agent.llm = None
    events = [e async for e in agent.run_stream("hi", {})]
    assert [e["type"] for e in events] == ["error"]
    assert "no model" in events[0]["message"]


# ------------------------------------------------------------- turn context
def test_turn_context_language_rule_and_voice_rules(stub_runtime_init):
    from runtime.agents.custom import VOICE_TURN_RULES, create_custom_agent

    text = create_custom_agent("helpdesk", locale="ar")._turn_context(False, None)
    assert "language of the user's latest message" in text and "`ar`" in text
    assert VOICE_TURN_RULES not in text and "already retrieved" not in text
    plain = create_custom_agent("helpdesk")._turn_context(False, None)
    assert "no clear language" not in plain  # no locale hint -> no fallback clause
    voice = create_custom_agent("helpdesk")._turn_context(True, "### Refunds\n14 days.")
    assert VOICE_TURN_RULES in voice and "14 days." in voice


async def test_voice_prefetch_uses_the_profile_sources_and_is_silent_on_failure(stub_runtime_init, monkeypatch):
    from runtime.agents.custom import create_custom_agent
    from runtime.tools import knowledge

    assert await create_custom_agent("helpdesk").prefetch_voice_context("refunds?", match_count=3) is None

    stub_runtime_init["profile"] = _profile(data_source_ids=["kb-1"])
    agent = create_custom_agent("helpdesk")
    seen = {}

    def _retrieve(query, *, source_ids, match_count):
        seen.update(query=query, source_ids=list(source_ids), match_count=match_count)
        return "### Refunds\n14 days."

    monkeypatch.setattr(knowledge, "retrieve", _retrieve)
    assert await agent.prefetch_voice_context("refunds?", match_count=3) == "### Refunds\n14 days."
    assert seen == {"query": "refunds?", "source_ids": ["kb-1"], "match_count": 3}

    def _boom(*a, **k):
        raise RuntimeError("embed down")

    monkeypatch.setattr(knowledge, "retrieve", _boom)
    assert await agent.prefetch_voice_context("refunds?", match_count=3) is None
