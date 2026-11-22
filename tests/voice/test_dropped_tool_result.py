"""A tool result the provider's upstream refused must not be re-said.

Reproduces the 2026-08-05 live client call. Three tool results were rejected
with "Tool call ID '...' not found in conversation" while `streamed=0`, so
nothing had reached the caller. The answer watchdog then re-issued each answer
as a say-exactly turn against a conversation that no longer held the matching
function_call — the model improvised around the instruction and replayed a
mangled splice of the previous answer and the holding line, twice, and never
answered the question the caller had actually just asked.

The provider had been swallowing that rejection on the reasoning that "the
caller already heard the streamed answer; only the model's context copy is
lost". That is true only when the answer streamed. When it did not, the
rejection is the whole story: the answer is unrecoverable by re-saying it,
and the watchdog's rescue makes the call worse rather than better.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat, RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult
from voice.live.providers.azure_voice_live import AzureVoiceLiveSession
from voice.live.testing import FakeRealtimeSession


def test_the_provider_reports_a_refused_tool_result_with_its_call_id():
    """The rejection names the call; parse it instead of only logging it."""
    session = AzureVoiceLiveSession.__new__(AzureVoiceLiveSession)
    events = AzureVoiceLiveSession._translate_nosend(session, {
        "type": "error",
        "error": {
            "message": "Tool call ID 'call_uY2gQznpi3GS1rcJ' not found in conversation."
        },
    })
    dropped = [e for e in events if e.type == "tool_result_dropped"]
    assert len(dropped) == 1
    assert dropped[0].call_id == "call_uY2gQznpi3GS1rcJ"
    # ...and it must NOT also surface as a caller-visible error frame: this is
    # bookkeeping the endpoint acts on, not something to read out loud.
    assert not [e for e in events if e.type == "error"]


@pytest.fixture
def harness(monkeypatch):
    live_ep._connect_limiter._store.clear()
    state = {}

    def _cfg(_conn, _kind):
        return state.get(
            "cfg",
            LiveVoiceConfig(
                enabled=True, model_id="m-1", provider="fake",
                # Short, so the test does not wait the production 4s.
                answer_watchdog_sec=0.3,
            ),
        )

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
        live_ep, "resolve_persona_prompt",
        lambda cfg, kind, **kw: "You are a voice agent.",
    )

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    state["session"] = session

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _read_until(ws, kind, limit=12):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


def test_a_dropped_tool_result_stands_the_answer_watchdog_down(harness, monkeypatch):
    """The regression: no say-exactly re-delivery for a refused call."""
    client, state = harness
    session = state["session"]

    async def _answer(**kwargs):
        return DelegateResult(spoken="Your July sales were 405,341.", display="x", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _answer)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()
        session.push(RealtimeEvent(
            type="tool_call", call_id="c1", tool_name="ask_agent",
            arguments={"query": "my sales last month"},
        ))
        _read_until(ws, "display")
        # The service refuses the result: its function_call is already gone.
        session.push(RealtimeEvent(type="tool_result_dropped", call_id="c1"))
        # Well past answer_watchdog_sec: the watchdog would have fired by now.
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="ignored", final=True))
        import time as _t
        _t.sleep(0.6)

    said = [t for t in session.text_turns if "Say exactly this" in t]
    assert not said, (
        "the answer for a REFUSED tool call was re-issued as a say-exactly "
        f"turn: {said!r}. The conversation no longer holds that call, so the "
        "model improvises around the instruction instead of obeying it — the "
        "2026-08-05 mangled-replay failure."
    )


def test_an_unrelated_call_keeps_its_watchdog(harness, monkeypatch):
    """The stand-down must be surgical.

    Cancelling every watchdog on any drop would silently give up the rescue
    for calls that are still perfectly deliverable — trading one live defect
    for a quieter one.
    """
    client, state = harness
    session = state["session"]

    async def _answer(**kwargs):
        return DelegateResult(spoken="Twelve branches.", display="x", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _answer)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()
        session.push(RealtimeEvent(
            type="tool_call", call_id="c1", tool_name="ask_agent",
            arguments={"query": "how many branches"},
        ))
        _read_until(ws, "display")
        # A DIFFERENT call was refused; c1 is untouched and produced no audio,
        # so c1's watchdog must still rescue it.
        session.push(RealtimeEvent(type="tool_result_dropped", call_id="other"))
        import time as _t
        _t.sleep(0.6)

    said = [t for t in session.text_turns if "Say exactly this" in t]
    assert said, (
        "an unrelated drop cancelled a healthy call's watchdog — the "
        "stand-down must key on the call_id it was told about."
    )


def test_a_queued_holding_line_that_is_never_spoken_is_never_displayed(harness, monkeypatch):
    """The 2026-08-05 'printed but not spoken' report.

    Out-of-band speech is QUEUED, not spoken: the provider drops the queue on a
    barge-in, a committed utterance or a playback flush. The endpoint used to
    emit the transcript at enqueue time, so the caller read two lines
    ("إيه، ثانية وحدة." / "لحظة، أتأكد لك الحين.") they never heard. The frame
    must come from the provider REPORTING the line committed to speech.
    """
    import endpoints.voice_live as live_ep_mod
    from voice.live.base import RealtimeEvent as _RE

    client, state = harness
    session = state["session"]

    # Model a provider that accepts the line into its queue and then drops it
    # (the barge-in / commit path) -- no out_of_band_started is ever reported.
    async def _queued_then_dropped(text):
        session.out_of_band.append(text)

    monkeypatch.setattr(session, "speak_out_of_band", _queued_then_dropped)

    async def _slow(**kwargs):
        import asyncio as _a
        await _a.sleep(0.25)
        return DelegateResult(spoken="Answer.", display="Answer.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)

    frames = []
    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()
        session.push(_RE(type="tool_call", call_id="c1", tool_name="ask_agent",
                         arguments={"query": "my sales last month"}))
        for _ in range(10):
            f = ws.receive_json()
            frames.append(f)
            if f.get("type") == "display":
                break

    spoken_texts = [f.get("text") for f in frames if f.get("type") == "transcript"]
    assert not any(t in session.out_of_band for t in spoken_texts if t), (
        "a holding line the provider never committed to speech was shown to "
        f"the caller: transcripts={spoken_texts!r} queued={session.out_of_band!r}"
    )
