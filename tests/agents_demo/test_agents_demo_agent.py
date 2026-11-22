"""The four demo agent classes: kind/case resolution, the voice hooks, the
per-turn context (language pin, call block, conversation state) and the
streaming loop's tool dispatch."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def stub_runtime_init(monkeypatch):
    """Skip the profile/LLM load; record the agent_kind RuntimeAgent got."""
    seen = {}

    def _init(self, agent_kind, environment=None, profile_slug=None):
        seen["agent_kind"] = agent_kind
        self.profile = SimpleNamespace(profile_id="p-test", slug="s-test", data_source_ids=["kb-1"],
                                       model_main={}, is_fallback=True)

    monkeypatch.setattr("runtime.agent.RuntimeAgent.__init__", _init)
    return seen


@pytest.mark.parametrize("key,kind", [("support", "agents_support"), ("inquiries", "agents_inquiries"),
                                      ("collection", "agents_collection"), ("sales", "agents_sales")])
def test_factory_builds_each_kind(stub_runtime_init, key, kind):
    from runtime.agents.agents_demo import create_demo_agent

    agent = create_demo_agent(key, locale="en")
    assert stub_runtime_init["agent_kind"] == kind
    assert agent.kb_source_id == "kb-1"
    names = [t.name for t in agent._build_tools({"call_state": agent.state, "locale": "en"})]
    assert names[-1] == "agents_kb_search"


def test_unknown_agent_and_case(stub_runtime_init):
    from runtime.agents.agents_demo import create_demo_agent

    with pytest.raises(LookupError):
        create_demo_agent("billing")
    with pytest.raises(LookupError):
        create_demo_agent("collection", call_case="C99")
    assert "agent_kind" not in stub_runtime_init  # failed before any profile load


def test_outbound_persona_variables_and_direct_tools(stub_runtime_init):
    from runtime.agents.agents_demo import create_demo_agent

    agent = create_demo_agent("collection", call_case="C02", locale="ar-AE")
    assert agent.persona_variables["callee_name"] == "رنا حداد"
    assert "303 riyals" in agent.persona_variables["case_file"]
    names = [d["name"] for d, _ in agent.direct_voice_tools()]
    assert "send_payment_link" in names
    agent.note_assistant_turn("Can I send the link to the mobile ending 9032?")
    assert agent.state["asked_send"] is True


@pytest.mark.parametrize("locale,dialect,accent", [
    ("ar-EG", "Egyptian", "neutral"), ("ar-JO", "Levantine", "neutral"), ("ar", "Gulf", "neutral"),
    ("ar-ZZ", "Gulf", "neutral"), ("en-GB", "Gulf", "British"), ("en_au", "Gulf", "Australian"),
    ("en", "Gulf", "neutral"), (None, "Gulf", "neutral"),
])
def test_accent_variables_from_the_picker_tag(locale, dialect, accent):
    from runtime.agents.agents_demo import accent_variables

    assert accent_variables(locale) == {"arabic_dialect": dialect, "english_accent": accent}


def test_picked_accent_reaches_persona_and_turn_context(stub_runtime_init):
    """The language & accent picker's tag: content still folds to `ar`, the
    region becomes the persona's dialect and the chat reply rule's."""
    from runtime.agents.agents_demo import create_demo_agent

    out = create_demo_agent("sales", locale="ar-EG")
    assert out.locale == "ar"
    assert out.persona_variables["arabic_dialect"] == "Egyptian"
    assert out.persona_variables["callee_name"]  # merged, not replaced
    inbound = create_demo_agent("support", locale="ar-JO")
    assert inbound.persona_variables["arabic_dialect"] == "Levantine"
    assert "Levantine dialect" in inbound._turn_context("مرحبا", False, None)
    assert "Gulf" not in inbound._turn_context("مرحبا", False, None)


def test_inbound_agent_on_a_delegated_profile_keeps_its_tools(stub_runtime_init):
    """No prefetched_context on the profile (an un-migrated stack, or a
    rollback): the old tool-backed brain, no case-file persona variables."""
    from runtime.agents.agents_demo import create_demo_agent
    from runtime.prompts.agents_demo import SUPPORT_SYSTEM_PROMPT

    agent = create_demo_agent("support")
    assert agent.prompt_only is False
    assert agent.persona_variables == {"arabic_dialect": "Gulf", "english_accent": "neutral"}
    assert agent._fallback_system_prompt() == SUPPORT_SYSTEM_PROMPT
    assert "support_verify_customer" in [t.name for t in agent._build_tools({"call_state": agent.state})]


def test_state_is_carried_in_and_summarised(stub_runtime_init):
    from runtime.agents.agents_demo import create_demo_agent

    st = {"verified": "41209908", "verify_failures": 0, "refunds": [{"ref": "REF-1", "amount": 349, "to": "card"}],
          "bookings": [], "changes": [], "tickets": [], "handoffs": []}
    agent = create_demo_agent("support", state=st)
    ctx = agent._turn_context("ok", voice_turn=False, prefetched=None)
    assert "VERIFIED as Khalid Bakr" in ctx and "Refund raised: REF-1" in ctx


def test_turn_context_language_pin_and_voice_rules(stub_runtime_init):
    from runtime.agents.agents_demo import create_demo_agent

    agent = create_demo_agent("sales", call_case="O09", locale="ar")
    ar = agent._turn_context("كم السعر؟", voice_turn=True, prefetched="### Plans\nx")
    assert "reply ONLY in Arabic" in ar and "LIVE PHONE CALL" in ar and "Plans, prices" in ar
    assert "Home Fiber 1 Gig" in ar  # the offer is named in the call block
    # Digits alone carry no language: an Arabic call stays Arabic.
    assert "reply ONLY in Arabic" in agent._turn_context("5594", voice_turn=False, prefetched=None)
    en = agent._turn_context("How much does it cost per month?", voice_turn=False, prefetched=None)
    assert "reply ONLY in English" in en


def test_text_turn_feeds_the_yes_gate_from_history(stub_runtime_init):
    from runtime.agents.agents_demo import create_demo_agent

    agent = create_demo_agent("collection", call_case="C01")
    agent._before_turn("yes please", [{"role": "assistant", "content": "Shall I text the link to 1441?"}])
    assert agent.state["asked_send"] is True and agent.state["reply_has_words"] is True
    agent._before_turn("1441", [{"role": "assistant", "content": "Your balance is 723 dirhams."}])
    assert agent.state["asked_send"] is False


class _Msg(SimpleNamespace):
    pass


class _FakeLLM:
    """Scripted chat model: first round calls a tool, second round answers."""

    def __init__(self, rounds):
        self.rounds = list(rounds)

    def bind_tools(self, tools):
        return self

    async def astream_events(self, messages, version="v2"):
        content, calls = self.rounds.pop(0)
        if content:
            yield {"event": "on_chat_model_stream", "data": {"chunk": _Msg(content=content, tool_call_chunks=[])}}
        yield {"event": "on_chat_model_end", "data": {"output": _Msg(content=content, tool_calls=calls,
                                                                         usage_metadata=None)}}


async def test_stream_runs_tools_against_the_state(stub_runtime_init, monkeypatch):
    from runtime.agents.agents_demo import create_demo_agent

    agent = create_demo_agent("support")
    agent.llm = _FakeLLM([
        ("", [{"name": "support_verify_customer", "args": {"account_number": "41203317",
                                                           "date_of_birth": "1991-03-14"}, "id": "c1"}]),
        ("Thanks Layla — your line is healthy.", []),
    ])
    monkeypatch.setattr(agent, "prepare", lambda ctx, all_tools: {
        "system_prompt": "SYS", "tools": all_tools, "tools_by_name": {t.name: t for t in all_tools}})
    events = [e async for e in agent.run_stream("my account 41203317 dob 14 March 1991", {})]
    kinds = [e["type"] for e in events]
    assert "tool_activity" in kinds and "usage" in kinds and "error" not in kinds
    assert next(e for e in events if e["type"] == "tool_activity")["tools"] == ["support_verify_customer"]
    assert "".join(e["content"] for e in events if e["type"] == "answer") == "Thanks Layla — your line is healthy."
    assert agent.state["verified"] == "41203317"
    json.dumps(agent.state)  # state stays plain JSON (it rides the signed token)
