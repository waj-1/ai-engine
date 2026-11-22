"""User voice test 2026-09-30, the doubled holding line.

The call recording (right channel = assistant) shows the persona's own
announce «لحظة. خلني أتأكد لك.» playing 66.1–68.5s, the delegate starting at
~67.0s, and the endpoint's out-of-band holding line starting at 69.9s —
`progress_after_sec` (2.5s) after DISPATCH, i.e. 1.4s after the announce the
caller was still hearing had ended. Two holding lines back to back, then no
answer for a while (the brain's stall re-ask had made the turn 6.8s).

The holding line exists to fill DEAD AIR. Its clock now starts when the
caller stops hearing her, not when the tool call landed.
"""
from __future__ import annotations

import asyncio

from voice.live.base import RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

import endpoints.voice_live as live_ep

from tests.voice.test_voice_live_endpoint import harness  # noqa: F401 — fixture

_OUT_RATE = 24000       # the harness's output sample rate (pcm16 mono)


def _audio(seconds: float) -> bytes:
    return b"\x00\x00" * int(_OUT_RATE * seconds)


def _drain_until_display(ws, limit=40):
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("text") and '"display"' in msg["text"]:
            return
    raise AssertionError("no display frame")


def _run(harness, monkeypatch, *, announce_sec: float, delegate_sec: float,  # noqa: F811
         out_of_band: bool = True):
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.3, progress_spoken="لحظة، أشيك لك.",
    )
    session = state["session"]
    # Out-of-band (azure_voice_live's shape) or, without it, several results
    # per call id with the holding line as a WHEN_IDLE tool result (gemini's).
    session.supports_streamed_tool_results = not out_of_band
    session.supports_out_of_band_speech = out_of_band

    async def _slow(**kwargs):
        await asyncio.sleep(delegate_sec)
        return DelegateResult(spoken="ما أقدر أحجز مواعيد العملاء من هنا.", display="…", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        if announce_sec:
            # The persona's announce, streamed faster than real time as the
            # service does: the caller hears it for `announce_sec` from now.
            session.push(RealtimeEvent(type="audio", audio=_audio(announce_sec)))
            ws.receive_bytes()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "موعد مع عمر الساعة 6"}))
        _drain_until_display(ws)
    return session


def test_no_holding_line_right_after_the_personas_own_announce(harness, monkeypatch):  # noqa: F811
    # Dispatch + 0.3s falls inside the announce (0.5s); the answer lands at
    # 0.6s, before 0.3s of silence has passed after the announce.
    session = _run(harness, monkeypatch, announce_sec=0.5, delegate_sec=0.6)
    assert session.out_of_band == [], session.out_of_band


def test_a_long_silence_after_the_announce_still_gets_the_holding_line(harness, monkeypatch):  # noqa: F811
    session = _run(harness, monkeypatch, announce_sec=0.3, delegate_sec=1.3)
    assert session.out_of_band == ["لحظة، أشيك لك."], session.out_of_band


def test_without_an_announce_the_line_still_comes_on_time(harness, monkeypatch):  # noqa: F811
    session = _run(harness, monkeypatch, announce_sec=0, delegate_sec=0.6)
    assert session.out_of_band == ["لحظة، أشيك لك."], session.out_of_band


# ---- the WHEN_IDLE tool-result path (review minor: only out-of-band was tested)
def _when_idle_lines(session):
    from voice.live.base import ToolResultMode
    return [p.get("spoken") for _cid, p, m in session.tool_results if m == ToolResultMode.WHEN_IDLE]


def test_when_idle_no_holding_line_right_after_the_announce(harness, monkeypatch):  # noqa: F811
    session = _run(harness, monkeypatch, announce_sec=0.5, delegate_sec=0.6, out_of_band=False)
    assert _when_idle_lines(session) == [], session.tool_results


def test_when_idle_a_long_silence_still_gets_the_holding_line(harness, monkeypatch):  # noqa: F811
    session = _run(harness, monkeypatch, announce_sec=0.3, delegate_sec=1.3, out_of_band=False)
    assert _when_idle_lines(session) == ["لحظة، أشيك لك."], session.tool_results


# ---- the caller's own words reach the delegate ----------------------------------
# Same call: «أعطيني اليوم» ("give me today") reached the brain only as the
# persona's paraphrase «أبغى أحدث الرصيد الحالي» ("the latest current balance").
def test_the_callers_committed_words_reach_the_delegate(harness, monkeypatch):  # noqa: F811
    client, state = harness
    session = state["session"]
    seen = {}

    async def _capture(**kwargs):
        seen.update(kwargs.get("extra_context") or {})
        return DelegateResult(spoken="…", display="…", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _capture)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user", text="أعطيني اليوم.", final=True))
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "أبغى أحدث الرصيد الحالي"}))
        _drain_until_display(ws)
    assert seen.get("caller_heard") == "أعطيني اليوم."
