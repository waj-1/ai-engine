"""Tier 3: the model answers the caller with NOTHING AT ALL.

Tiers 1 and 2 both hang off `turn_end` and read the model's reply, so neither
can see a model that never speaks and never ends a turn. Measured on a cold
session 2026-08-05, right after the harness stopped sharing one session across
every scenario:

    metrics: session sec=85 turns=1 user_turns=1 tool_calls=0 ...

The caller asked for last month's sales and got 85 seconds of silence. No
recovery armed, because there was no reply to inspect — `turn_end`'s arming
block is gated on `and reply_text`.

So this tier hangs off the CALLER's commit, the one trigger a silent model
cannot blind. Every test here drives `stall_recovery_silent_sec` low enough to
actually outwait — the older suite's habit of sleeping less than the grace is
exactly how a broken recovery passed 97 tests on 2026-08-04.
"""
import time as _t

import pytest

import endpoints.voice_live as live_ep
from voice.live.base import RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

from tests.voice.test_voice_live_endpoint import harness  # noqa: F401

TOPIC_TERMS = ["sales", "terminal", "branch", "iban", "مبيعات", "طرفية"]


def _silence(harness, monkeypatch, caller, *, terms=TOPIC_TERMS, silent_sec=0.2,
             then=None):
    """Caller speaks; the model does NOTHING unless `then` pushes something."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", progress_after_sec=0,
        stall_recovery_sec=0.15,
        stall_recovery_unannounced_sec=0.15,
        stall_recovery_silent_sec=silent_sec,
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
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text=caller, final=True))
        if then is not None:
            then(session)
        _t.sleep(0.7)
    return called


@pytest.mark.parametrize("caller", [
    "what were my sales last month",
    "how did the Olaya branch do",
    "مبيعات الشهر الماضي كم",
])
def test_total_silence_after_a_topic_question_is_recovered(harness, monkeypatch, caller):  # noqa: F811
    """The measured failure: no reply, no turn_end, no tool call, no audio."""
    assert _silence(harness, monkeypatch, caller) == [caller]


@pytest.mark.parametrize("caller", [
    "no, that's all thanks",
    "how are you today",
    "لا، هذا كل شي",
])
def test_silence_after_a_non_topic_utterance_is_never_recovered(harness, monkeypatch, caller):  # noqa: F811
    """A farewell that the model simply does not answer must not be turned
    into a delegated turn. Same gate as tier 2, same reason — recovering a
    goodbye speaks over the persona on every call that ends politely."""
    assert _silence(harness, monkeypatch, caller) == []


def test_a_model_that_speaks_stands_the_silent_tier_down(harness, monkeypatch):  # noqa: F811
    """ANY assistant audio after the commit means the model replied, so this
    is chitchat, a clarifying question or a closure — the turn_end tiers judge
    those on the reply text, which is strictly better than a timer."""
    def _speak(session):
        session.push(RealtimeEvent(type="audio", role="assistant", audio=b"\x00" * 640))

    assert _silence(harness, monkeypatch,
                    "what were my sales last month", then=_speak) == []


def test_a_tool_call_stands_the_silent_tier_down(harness, monkeypatch):  # noqa: F811
    """The healthy shape: the model went quiet because it was calling the
    tool. Consuming the commit must cancel the timer, or the caller hears the
    answer twice."""
    def _tool(session):
        session.push(RealtimeEvent(type="tool_call", call_id="c1",
                                   tool_name="ask_agent",
                                   arguments={"query": "what were my sales last month"}))

    called = _silence(harness, monkeypatch,
                      "what were my sales last month", then=_tool)
    assert called == ["what were my sales last month"], called
    assert len(called) == 1, "the silent tier re-delegated a question already answered"


def test_a_newer_caller_utterance_stands_the_silent_tier_down(harness, monkeypatch):  # noqa: F811
    """The caller moved on. Recovering the superseded question is the goodbye
    bug in miniature — answering something nobody is waiting for any more."""
    def _newer(session):
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="actually, my terminal instead", final=True))

    called = _silence(harness, monkeypatch,
                      "what were my sales last month", then=_newer)
    assert "what were my sales last month" not in called, called


def test_a_late_tool_call_after_a_silent_recovery_is_suppressed(harness, monkeypatch):  # noqa: F811
    """A model silent for the whole grace is LATE, not dead — its own
    ask_agent can still land after the recovery has answered.

    The turn_end tiers mark `last_commit["recovered"]` BEFORE delegating so
    the #32 suppression catches that late call on arrival. Tier 3 shipped
    without it and produced two turns for one question: live 2026-08-05
    07:04:10, tier 3 fired at 12.0s on `data_question` and the harness caught
    "MULTIPLE unprompted responses after the answer".
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", progress_after_sec=0,
        stall_recovery_sec=0.15, stall_recovery_unannounced_sec=0.15,
        stall_recovery_silent_sec=0.2, stall_recovery_topic_terms=TOPIC_TERMS,
    )
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="RECOVERED", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)
    q = "what were my sales last month"
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user", text=q, final=True))
        _t.sleep(0.5)                      # let tier 3 fire and answer
        assert called == [q], called
        # ...and NOW the model's own call finally arrives for the same commit.
        session.push(RealtimeEvent(type="tool_call", call_id="late-1",
                                   tool_name="ask_agent", arguments={"query": q}))
        _t.sleep(0.4)
    assert called == [q], f"the late call re-delegated an answered question: {called}"


def test_the_tier_is_off_by_default_and_by_config(harness, monkeypatch):  # noqa: F811
    """0 disables tier 3 alone; empty topic terms disable it too. Both are
    rollback levers that leave tiers 1 and 2 untouched."""
    assert _silence(harness, monkeypatch,
                    "what were my sales last month", silent_sec=0) == []
    assert _silence(harness, monkeypatch,
                    "what were my sales last month", terms=[]) == []
