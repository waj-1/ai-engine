"""The unconfirmed-interrupt silent-answer class on the LEGACY barge-in arm.

Providers without barge-in confirmation (Gemini Live) treat every provider
`interrupted` as a caller barge-in immediately: the supersede epoch bumps
UNCONFIRMED, with no later transcript verdict to clear it. Two defects rode
that arm, both reproduced live on the landing sales tenant on 2026-08-30/31:

1. **Sentence-streamed answers were eaten whole.** A noise-tripped (or
   self-inflicted, see 2) unconfirmed bump made `_speak_sentence` deliver
   every remaining sentence SILENT — and each silent sentence still counted
   as "streamed", which short-circuited the whole-answer delivery AND its
   false-barge-in recovery (`_resolve_barge_in_mode`). Session log signature:
   `mode=silent streamed=3 recovered_turns=0`; the caller asked, waited, and
   heard nothing. The fix halts streaming on an unconfirmed divergence
   (halted sentences are never recorded as streamed) and routes the unspoken
   tail through the same confirm-window recovery the non-streamed path
   already had.

2. **Our own delivery armed the barge-in.** An INTERRUPT-mode tool result
   displaces the holding line server-side, and the provider reports that
   displacement as `interrupted` — indistinguishable on the wire from the
   caller speaking. The legacy arm bumped the epoch and painted a phantom
   "you took the floor" chip on every streamed answer that displaced a
   holding line. The fix opens a short absorb window at each deliberate
   floor cut (`_SELF_CUT_ABSORB_SEC`); one `interrupted` inside it is
   absorbed — no bump, no frame.

These are deliberately NOT parametrized into
test_voice_live_turn_invariants.py: that file's orders all model CALLER
events, while both cases here hinge on events the ENDPOINT itself caused.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat, RealtimeEvent, ToolResultMode
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult
from voice.live.testing import FakeRealtimeSession

QUESTION = "how much does it cost"
S1 = "It starts at ninety-nine riyals."
S2 = "The annual plan is cheaper."


class LegacySession(FakeRealtimeSession):
    """A Gemini-shaped provider: no transcript verdict for barge-ins, so the
    endpoint runs the legacy (bump-on-detection) arm."""

    supports_barge_in_confirmation = False


@pytest.fixture(params=[0.0, 0.05], ids=["sustain-off", "sustain-on"])
def harness(monkeypatch, request):
    """Same wiring as test_voice_live_turn_invariants.py's fixture (and
    duplicated for the same reason: sharing it would couple the files' rate
    budgets), except the session is a LegacySession and the config enables
    sentence streaming with a short confirm window so the recovery wait
    doesn't slow the suite."""
    live_ep._connect_limiter._store.clear()
    state = {}

    def _cfg(_conn, _kind):
        return state["cfg"]

    monkeypatch.setattr(live_ep, "_live_config_for", _cfg)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(
        live_ep, "authenticate_optional", lambda authz: ("tok", "user-1")
    )
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(
        live_ep, "build_agent_for_kind", lambda kind, params: (object(), object())
    )
    monkeypatch.setattr(
        live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "You are a voice agent."
    )

    session = LegacySession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    state["session"] = session
    state["cfg"] = LiveVoiceConfig(
        enabled=True,
        model_id="m-1",
        provider="fake",
        stream_sentences=True,
        interrupt_confirm_window_sec=0.3,
        # Also with the sustained cut configured: on a provider without
        # confirmation it must stay inert (eval review 2026-09-30).
        barge_in_sustain_sec=request.param,
    )

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _tool_call_event(call_id="c1", query=QUESTION):
    return RealtimeEvent(
        type="tool_call",
        call_id=call_id,
        tool_name="ask_agent",
        arguments={"query": query},
    )


def _read_until(ws, kind, limit=16):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


def _wait_for(predicate, timeout=2.0):
    """Server tasks run in the TestClient's portal thread — a plain sleep in
    the test thread lets them progress."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_unconfirmed_interrupt_halts_streaming_and_recovers_the_tail(
    harness, monkeypatch
):
    """A noise `interrupted` mid-answer must not eat the rest of the answer.

    Sentence 1 streams audibly, then an unconfirmed interrupt lands, then
    sentence 2 arrives. Old behaviour: sentence 2 delivered SILENT, marked
    streamed, no recovery — dead air for half the answer. Required: the
    halted tail is re-delivered AUDIBLY once the interrupt stays unconfirmed
    past the window.
    """
    client, state = harness
    session = state["session"]

    import asyncio

    async def _delegate(**kwargs):
        on_sentence = kwargs["on_sentence"]
        assert on_sentence is not None, (
            "stream_sentences=True on a streaming-capable session must hand "
            "the delegate an on_sentence callback"
        )
        streamed = []
        for i, s in enumerate((S1, S2)):
            if i == 1:
                # Noise trips speech-start detection while the answer is
                # mid-stream; let the pump process it before the next
                # sentence — the exact live interleaving.
                session.push(RealtimeEvent(type="interrupted"))
                await asyncio.sleep(0.15)
            try:
                await on_sentence(s)
            except Exception:
                continue  # the delegate contract: log-and-continue, not streamed
            streamed.append(s)
        return DelegateResult(
            spoken=f"{S1} {S2}", display=f"{S1} {S2}", charts=[], streamed=streamed
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(_tool_call_event())
        _read_until(ws, "display")
        # The tail delivery waits out the 0.3s confirm window in the tool
        # task's finally block — poll the session ledger, not the wire.
        assert _wait_for(
            lambda: len([r for r in session.tool_results if r[0] == "c1"]) >= 2
        ), (
            "the halted tail was never delivered: after an unconfirmed "
            "interrupt the caller got sentence 1 and then dead air"
        )

    deliveries = [r for r in session.tool_results if r[0] == "c1"]
    assert len(deliveries) == 2, (
        f"expected the audible first sentence plus one tail delivery, got "
        f"{[(p.get('spoken'), m.value) for _, p, m in deliveries]!r}"
    )
    (_, p1, m1), (_, p2, m2) = deliveries
    assert p1.get("spoken") == S1 and m1 is not ToolResultMode.SILENT
    assert p2.get("spoken") == S2, (
        f"the tail delivery must carry exactly the unspoken sentences, got "
        f"{p2.get('spoken')!r} — re-saying the streamed prefix makes the "
        f"assistant repeat itself"
    )
    assert m2 is not ToolResultMode.SILENT, (
        "the tail was delivered SILENT: an interrupt nothing ever confirmed "
        "silently superseded the caller's own answer — the live "
        "'asked, waited, heard nothing' defect"
    )


def test_provider_interrupted_after_our_own_cut_is_absorbed(harness, monkeypatch):
    """The provider reporting OUR INTERRUPT delivery back must not become a
    phantom barge-in — no epoch bump, no 'you took the floor' chip. A later
    `interrupted` outside the absorb window is still a real barge-in."""
    client, state = harness
    session = state["session"]
    state["cfg"] = LiveVoiceConfig(
        enabled=True,
        model_id="m-1",
        provider="fake",
        stream_sentences=False,  # whole-answer delivery → INTERRUPT mode
        interrupt_confirm_window_sec=0.3,
    )
    # raising=False so the discrimination run (fix stashed, constant absent)
    # reaches the assertion below and fails on the SYMPTOM, not the patch.
    monkeypatch.setattr(live_ep, "_SELF_CUT_ABSORB_SEC", 0.25, raising=False)

    async def _delegate(**kwargs):
        return DelegateResult(spoken=S1, display=S1, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(_tool_call_event())
        _read_until(ws, "display")
        assert _wait_for(
            lambda: any(r[0] == "c1" for r in session.tool_results)
        ), "the answer was never delivered"
        # The service reports our own cut back to us, inside the window.
        session.push(RealtimeEvent(type="interrupted"))
        # …and a real barge-in later, once the window has closed.
        time.sleep(0.4)
        session.push(RealtimeEvent(type="interrupted"))
        time.sleep(0.1)
        # Sentinel: turn_end forwards to the client, bounding the read below
        # (receive_json has no timeout of its own).
        session.push(RealtimeEvent(type="turn_end"))

        barge_frames = 0
        for _ in range(24):
            frame = ws.receive_json()
            if (
                frame.get("type") == "interrupted"
                and frame.get("reason") == "barge_in"
            ):
                barge_frames += 1
            if frame.get("type") == "turn_end":
                break

    assert barge_frames == 1, (
        f"expected exactly one barge_in frame (the post-window interrupt); "
        f"got {barge_frames}. Two means the provider's report of our "
        f"own delivery was painted as the caller taking the floor; zero "
        f"means a real barge-in was swallowed."
    )
