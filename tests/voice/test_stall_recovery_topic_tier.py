"""Tier 2 of the stall recovery: a stall with no recognised announce phrase.

Tier 1 (`_ANNOUNCE_TOKENS`) covers the model that declares a lookup and then
stops. It cannot cover the model that simply stops — the persona is allowed to
"just answer", and a reply that ends a turn with the caller's question
unconsumed is, by its own text, indistinguishable from a goodbye.

Arming on every such turn was tried and measured WRONG: it recovers "no,
that's all thanks" and speaks over the persona's farewell on every call that
ends politely. So tier 2 inverts the reading — it keys on the CALLER's
utterance, which does distinguish them, and stays off unless the tenant has
configured topic terms.

These tests exist because the first attempt at this passed the whole existing
suite (97 tests) while broken: the older chitchat test sleeps 0.5s against a
longer grace and never observed the recovery fire. Every test here drives the
grace down to a value it can actually outwait.
"""
import time as _t

import pytest

import endpoints.voice_live as live_ep
from voice.live.base import RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

from tests.voice.test_voice_live_endpoint import harness  # noqa: F401

TOPIC_TERMS = ["sales", "terminal", "branch", "iban", "ticket",
               "مبيعات", "طرفية", "فرع", "ايبان", "تذكرة"]


def _run(harness, monkeypatch, caller: str, reply: str, terms=TOPIC_TERMS):
    """Push one caller utterance + one model reply that calls no tool.

    Returns the list of messages the delegate was asked to run — empty when
    the recovery correctly stood down.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", progress_after_sec=0,
        stall_recovery_sec=0.15,
        stall_recovery_unannounced_sec=0.15,
        stall_recovery_topic_terms=terms,
    )
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="RECOVERED", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user", text=caller, final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant", text=reply, final=True))
        session.push(RealtimeEvent(type="turn_end"))
        _t.sleep(0.6)
    return called


@pytest.mark.parametrize("caller,reply", [
    ("what were my sales last month", "Sure thing."),
    ("how did the Olaya branch do", "Okay."),
    ("مبيعات الشهر الماضي كم", "طيب."),
    ("أبي أغير الآيبان", "تمام."),
])
def test_a_topic_question_that_ends_with_no_tool_call_is_recovered(
    harness, monkeypatch, caller, reply  # noqa: F811
):
    """The residual stall: no announce phrase, so tier 1 is blind, but the
    caller asked about something only the agent can answer and the turn ended
    without a tool call. Nothing will prompt the model again — recover."""
    assert _run(harness, monkeypatch, caller, reply) == [caller]


@pytest.mark.parametrize("caller,reply", [
    ("no, that's all thanks", "Thank you for calling us. Goodbye!"),
    ("لا، هذا كل شي", "شكراً لاتصالك فينا، مع السلامة."),
])
def test_a_closure_is_never_recovered(harness, monkeypatch, caller, reply):  # noqa: F811
    """THE expensive false positive, and the reason tier 2 reads the caller
    rather than the model. A goodbye leaves the commit unconsumed exactly like
    a stall does. Recovering it delegates the caller's farewell and talks over
    the persona — on every call that ends politely. Measured, not theorised:
    an earlier version of this change did precisely that."""
    assert _run(harness, monkeypatch, caller, reply) == []


@pytest.mark.parametrize("caller,reply", [
    ("how are you today", "Doing great, thanks for asking!"),
    ("شكراً لك", "العفو، تحت أمرك."),
])
def test_chitchat_is_never_recovered(harness, monkeypatch, caller, reply):  # noqa: F811
    """Same hazard, smaller blast radius: the brain would answer social talk
    with "I only know this business", after the persona already answered it warmly."""
    assert _run(harness, monkeypatch, caller, reply) == []


def test_a_question_back_to_the_caller_is_never_recovered(harness, monkeypatch):  # noqa: F811
    """The persona asks ONE clarifying question on a vague problem, and offers
    ("want the breakdown?") instead of dumping detail. Both end the turn
    legitimately with the commit unconsumed. Recovering them answers a
    question the caller was never given the chance to answer."""
    assert _run(
        harness, monkeypatch,
        "my terminal has a problem",
        "Which terminal is it, and what's showing on the screen?",
    ) == []
    assert _run(
        harness, monkeypatch,
        "what were my sales last month",
        "‏هل تبي التفاصيل لكل فرع؟",
    ) == []


def test_the_tier_is_off_when_no_topic_terms_are_configured(harness, monkeypatch):  # noqa: F811
    """Opt-in by default: the engine is unbranded and cannot guess a tenant's
    vocabulary, so an existing deployment must not change behaviour by
    upgrading. With no terms, only tier 1 (announce phrase) can arm."""
    assert _run(harness, monkeypatch,
                "what were my sales last month", "Sure thing.", terms=[]) == []


def test_tier_one_still_arms_without_any_topic_terms(harness, monkeypatch):  # noqa: F811
    """...and tier 1 is untouched by tier 2 being disabled."""
    assert _run(harness, monkeypatch,
                "what were my sales last month", "One moment.", terms=[]) == [
        "what were my sales last month"
    ]
