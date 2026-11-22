"""The late-duplicate guard must survive an epoch bump.

When the stall recovery answers a committed question and the model's own
ask_agent for it lands late, the call is closed unspoken — keyed, before this
file's fix, on `last_commit["epoch"] == dispatch["epoch"]` alone. Exact, and
brittle: any unconfirmed `interrupted` between the commit and the late call
(echo, room noise, the provider reporting our own recovery-delivery cancel)
moves the epoch and let the duplicate through.

Live 2026-08-31 13:44 on the landing sales tenant: the recovery answered the
caller's booking details, the model's own call landed 1.2s later past a
bumped epoch, the caller got their phone number read back TWICE — and both
turns ran the brain, so `book_demo_call` fired twice (two demo-request emails
for one lead, the first before the caller had confirmed the digits).

The guard now has a second prong: normalized token containment between the
late call's query and the recovered utterance, which survives the epoch bump
and the model's paraphrasing. The scope test pins that a genuinely NEW
question is still answered.
"""
from __future__ import annotations

import time as _t

import endpoints.voice_live as live_ep
from voice.live.base import RealtimeEvent, ToolResultMode
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

# The legacy-arm (no barge-in confirmation) session and harness — the
# provider shape the incident happened on.
from tests.voice.test_voice_live_unconfirmed_interrupt import (  # noqa: F401
    LegacySession,
    harness,
)

BOOKING = "I want to book a demo. My name is Aasim. My phone number is 0503288400."
PARAPHRASED = "my name is aasim, my phone number 0503288400, book a demo"
NEW_QUESTION = "what does a Waj subscription cost per month"


def _drive(harness, monkeypatch, late_query: str):
    """One committed utterance -> announce-then-stall -> recovery answers ->
    epoch bump -> the model's late ask_agent with `late_query`.

    Returns (delegated_messages, session) for the assertions.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True,
        model_id="m-1",
        provider="fake",
        progress_after_sec=0,
        stall_recovery_sec=0.15,
        interrupt_confirm_window_sec=0.3,
    )
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="RECOVERED", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(
            RealtimeEvent(type="transcript", role="user", text=BOOKING, final=True)
        )
        # Tier-1 announce with no tool call, then turn_end: the stall
        # tripwire fires after stall_recovery_sec and the recovery answers.
        session.push(
            RealtimeEvent(
                type="transcript",
                role="assistant",
                text="One moment, let me check.",
                final=True,
            )
        )
        session.push(RealtimeEvent(type="turn_end"))
        _t.sleep(0.6)
        # Noise bumps the epoch on the legacy arm. TWO events on purpose:
        # the recovery's own pre-delivery cancel opened the self-cut absorb
        # window, so the first `interrupted` is (correctly) absorbed — the
        # second is a genuine bump, the exact state the incident died in.
        session.push(RealtimeEvent(type="interrupted"))
        _t.sleep(0.05)
        session.push(RealtimeEvent(type="interrupted"))
        _t.sleep(0.1)
        session.push(
            RealtimeEvent(
                type="tool_call",
                call_id="late1",
                tool_name="ask_agent",
                arguments={"query": late_query},
            )
        )
        _t.sleep(0.5)
    return called, session


def test_the_models_late_call_is_suppressed_across_an_epoch_bump(
    harness, monkeypatch
):
    called, session = _drive(harness, monkeypatch, PARAPHRASED)

    assert called == [BOOKING], (
        f"expected exactly the recovery's delegation; the model's late "
        f"paraphrase of the same question ran the brain again: {called!r}. "
        f"This is the double read-back / double book_demo_call defect."
    )
    closes = [r for r in session.tool_results if r[0] == "late1"]
    assert closes, "the late call id was never closed — the provider would wait forever"
    _cid, payload, mode = closes[0]
    assert mode is ToolResultMode.SILENT and payload.get("spoken") == "", (
        f"the late duplicate must be closed unspoken, got {payload!r} ({mode})"
    )


def test_a_genuinely_new_question_after_a_recovery_is_still_answered(
    harness, monkeypatch
):
    """The scope boundary: the text prong must not swallow a NEW question
    whose tool_call happens to land while `recovered` is still set."""
    called, session = _drive(harness, monkeypatch, NEW_QUESTION)

    assert called == [BOOKING, NEW_QUESTION], (
        f"the new question was not delegated: {called!r}. The duplicate "
        f"suppression is over-broad."
    )
    deliveries = [r for r in session.tool_results if r[0] == "late1"]
    assert deliveries and deliveries[0][1].get("spoken") == "RECOVERED", (
        "the new question's answer was not delivered on its own call id"
    )
