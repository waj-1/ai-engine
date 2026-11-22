"""SimulatedRealtimeSession: the deterministic provider brain for voice evals.

It models what a real realtime provider does with the engine's stimuli —
greeting trigger, caller turns, tool results — without any network, so the
whole WS endpoint + agent stack can run for free in eval sim mode.
"""
import asyncio

from voice.live.base import RealtimeEvent, ToolResultMode
from voice.live.simulated import SimulatedRealtimeSession
from voice.live.testing import assert_realtime_contract


async def _collect(session, n, timeout=1.0):
    out = []
    it = session.events()
    for _ in range(n):
        out.append(await asyncio.wait_for(it.__anext__(), timeout=timeout))
    return out


def _make():
    return SimulatedRealtimeSession(None, persona="test persona")


async def test_passes_realtime_contract():
    await assert_realtime_contract(_make)


async def test_first_text_turn_is_greeting():
    s = _make()
    await s.start()
    await s.send_text_turn("Greet the caller.")
    events = await _collect(s, 2)
    assert events[0].type == "transcript"
    assert events[0].role == "assistant"
    assert events[0].text == SimulatedRealtimeSession.GREETING
    assert events[0].final is True
    assert events[1].type == "turn_end"
    await s.close()


async def test_subsequent_text_turn_becomes_ask_agent_call():
    s = _make()
    await s.start()
    await s.send_text_turn("Greet the caller.")
    await _collect(s, 2)  # drain greeting
    await s.send_text_turn("What were my sales yesterday?")
    events = await _collect(s, 2)
    assert events[0].type == "transcript" and events[0].role == "user"
    assert events[0].text == "What were my sales yesterday?"
    assert events[1].type == "tool_call"
    assert events[1].tool_name == "ask_agent"
    assert events[1].arguments == {"query": "What were my sales yesterday?"}
    assert events[1].call_id  # non-empty, unique per call
    await s.close()


async def test_tool_result_speaks_and_ends_turn():
    s = _make()
    await s.start()
    await s.send_text_turn("Greet the caller.")
    await _collect(s, 2)
    await s.send_text_turn("q")
    events = await _collect(s, 2)
    call_id = events[1].call_id
    await s.send_tool_result(
        call_id, {"spoken": "Your sales were 500 riyals."},
        mode=ToolResultMode.INTERRUPT,
    )
    out = await _collect(s, 3)
    assert out[0].type == "transcript" and out[0].role == "assistant"
    assert out[0].text == "Your sales were 500 riyals."
    assert out[1].type == "audio" and out[1].reply is True and out[1].audio
    assert out[2].type == "turn_end"
    await s.close()


async def test_empty_spoken_still_ends_turn():
    s = _make()
    await s.start()
    await s.send_text_turn("Greet the caller.")
    await _collect(s, 2)
    await s.send_text_turn("q")
    events = await _collect(s, 2)
    await s.send_tool_result(events[1].call_id, {}, mode=ToolResultMode.WHEN_IDLE)
    out = await _collect(s, 1)
    assert out[0].type == "turn_end"  # no transcript/audio for empty spoken
    await s.close()


async def test_out_of_band_is_spoken_as_transcript():
    s = _make()
    await s.start()
    await s.speak_out_of_band("Still working on that.")
    out = await _collect(s, 1)
    assert out[0].type == "transcript" and out[0].role == "assistant"
    assert out[0].text == "Still working on that."
    # Marked out_of_band so the endpoint can stamp the WS frame — without
    # this a sim holding line is indistinguishable from the answer and the
    # eval driver's answer_ms clocks the filler (see admin/voice_eval_driver).
    assert out[0].out_of_band is True
    assert s.out_of_band == ["Still working on that."]
    await s.close()


async def test_registered_in_factory():
    from voice.live.config import LiveVoiceConfig
    from voice.live.factory import build_realtime_session
    import voice.live.simulated  # noqa: F401 — side-effect registration

    cfg = LiveVoiceConfig(enabled=True)
    cfg.provider = "simulated"
    session = build_realtime_session(cfg, persona="p")
    assert isinstance(session, SimulatedRealtimeSession)
    assert session.persona == "p"
