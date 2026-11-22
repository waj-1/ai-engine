"""A recovered answer must still be SPOKEN after an unconfirmed interrupt.

The stall recovery (the model announces a lookup, then ends its turn without
calling `ask_agent`) delegates the question itself, pushes the answer to the
screen, and then asks the realtime model to say it. That last step used to be
gated on a bare `interrupt_epoch["n"] == dispatch_epoch`.

An epoch bump is not evidence the caller took the floor. On a provider
without barge-in confirmation (Gemini Live) EVERY provider `interrupted`
bumps it unconfirmed: room noise, the caller's own echo, the provider
reporting our own cancel back at us. So the recovery answer was dropped on
any bump at all — while the `display` frame had already gone out. The caller
read the answer on screen and heard nothing, and nothing was logged.

Live on company.example 2026-09-03: two calls, three stall recoveries,
`barge_ins=8` / `barge_ins=2` with `false_barge_ins_suppressed=0`. The
ask_agent path would have spoken all three — it waits the interrupt out
(`_resolve_barge_in_mode`) and treats an unconfirmed one as a false alarm.
This pins that the recovery path now does the same.
"""
from __future__ import annotations

import time as _t

import pytest

import endpoints.voice_live as live_ep
from voice.live.base import RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

from tests.voice.test_voice_live_unconfirmed_interrupt import (  # noqa: F401
    LegacySession,
    harness,
)

QUESTION = "how does the whatsapp integration work"
ANSWER = "Waj connects your WhatsApp Business profile under Marketing."


def _drive(harness, monkeypatch, *, interrupt: bool, confirm: bool):
    """Announce-then-stall (so the recovery fires), then optionally an
    `interrupted` — and, when `confirm`, a substantive utterance that
    confirms it the way a real barge-in does."""
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

    async def _delegate(**kwargs):
        # Slow on purpose: the interrupt has to land while the recovery's
        # delegate is still in flight, which is the window the live defect
        # lives in. An instant delegate finishes before the interrupt and
        # tests nothing (the first draft of this file did exactly that and
        # the confirmed-barge-in case passed vacuously).
        import asyncio as _a
        await _a.sleep(0.5)
        return DelegateResult(spoken=ANSWER, display=ANSWER, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(
            RealtimeEvent(type="transcript", role="user", text=QUESTION, final=True)
        )
        session.push(
            RealtimeEvent(
                type="transcript", role="assistant",
                text="One moment, let me check.", final=True,
            )
        )
        session.push(RealtimeEvent(type="turn_end"))
        if interrupt:
            # Lands while the recovery's delegate is in flight: the tripwire
            # fires at stall_recovery_sec (0.15s) and the delegate then takes
            # 0.5s, so 0.35s is comfortably inside that window.
            _t.sleep(0.35)
            session.push(RealtimeEvent(type="interrupted"))
            if confirm:
                session.push(
                    RealtimeEvent(
                        type="transcript", role="user",
                        text="actually forget that, what about pricing",
                        final=True,
                    )
                )
        _t.sleep(1.6)
    return session


def test_a_recovered_answer_is_spoken_when_nothing_interrupts(harness, monkeypatch):
    """Baseline: the undisturbed path still speaks."""
    session = _drive(harness, monkeypatch, interrupt=False, confirm=False)
    assert session.text_turns, "the recovered answer was never spoken"
    assert ANSWER in session.text_turns[-1]


def test_a_recovered_answer_survives_an_unconfirmed_interrupt(harness, monkeypatch):
    """The reported defect: noise (or our own echo) bumps the epoch, nothing
    ever confirms it, and the answer must still be said."""
    session = _drive(harness, monkeypatch, interrupt=True, confirm=False)
    assert session.text_turns, (
        "the recovered answer went to the screen and was never spoken. An "
        "unconfirmed interrupt is not the caller taking the floor — on a "
        "provider without barge-in confirmation it is the NORMAL case, so "
        "this drops the answer to a doc lookup on an ordinary call"
    )
    assert ANSWER in session.text_turns[-1]


def test_a_confirmed_barge_in_still_silences_the_recovered_answer(
    harness, monkeypatch
):
    """The scope boundary: when the caller really did move on, speaking the
    old answer would talk over their new question."""
    session = _drive(harness, monkeypatch, interrupt=True, confirm=True)
    spoken = " ".join(session.text_turns)
    assert ANSWER not in spoken, (
        "a confirmed barge-in must not be spoken over — the suppression is "
        "over-broad in the other direction now"
    )
