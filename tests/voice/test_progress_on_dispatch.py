"""Fix 2: acknowledge a slow lookup immediately instead of after 4 seconds.

The product ask: when a lookup is going to take a while, the caller should
hear "understood, one moment" right after they finish talking, not
`progress_after_sec` seconds into silence. The endpoint cannot predict a
tool's duration up front (the realtime model just calls `ask_agent` with a
query), so `LiveVoiceConfig.progress_on_dispatch` implements this as a MODE:
speak the (already-existing, operator-worded) holding line at DISPATCH
instead of waiting out the timer.

The path this has to work on unmodified is Gemini's: it has no out-of-band
speech (`supports_out_of_band_speech = False`), so the holding line rides a
WHEN_IDLE tool result instead of being spoken independently -- see
CLAUDE.md's Duplex voice section. These tests drive exactly that shape
(`test_holding_line_falls_back_to_a_tool_result_without_out_of_band` in
test_voice_live_endpoint.py is the pre-existing test for the WHEN_IDLE
mechanism itself; these pin the NEW timing on top of it).
"""
import asyncio as _a

import endpoints.voice_live as live_ep
from voice.live.base import RealtimeEvent, ToolResultMode
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

from tests.voice.test_voice_live_endpoint import _read_until, harness  # noqa: F401


def test_progress_on_dispatch_speaks_immediately_on_the_gemini_when_idle_path(
    harness, monkeypatch  # noqa: F811
):
    """With the mode ON, the WHEN_IDLE holding line is sent at dispatch --
    even on a turn far shorter than `progress_after_sec` -- instead of never
    firing at all (today's behaviour would never speak it on a 0.1s turn
    against a 4.0s threshold)."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=4.0, progress_spoken="Understood, one moment.",
        progress_on_dispatch=True,
    )
    session = state["session"]
    session.supports_out_of_band_speech = False  # the Gemini shape

    async def _fast(**kwargs):
        await _a.sleep(0.1)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fast)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")

    assert session.out_of_band == [], (
        "Gemini has no out-of-band speech -- the line must ride the tool "
        "result, not speak_out_of_band"
    )
    modes = [(p["spoken"], m) for _cid, p, m in session.tool_results]
    assert ("Understood, one moment.", ToolResultMode.WHEN_IDLE) in modes, modes


def test_progress_on_dispatch_off_is_byte_identical_to_the_timer(
    harness, monkeypatch  # noqa: F811
):
    """Default OFF: a fast turn (well under `progress_after_sec`) must get
    NO holding line at all -- today's exact behaviour, unchanged."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=4.0, progress_spoken="Understood, one moment.",
        progress_on_dispatch=False,
    )
    session = state["session"]
    session.supports_out_of_band_speech = False

    async def _fast(**kwargs):
        await _a.sleep(0.1)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fast)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")

    modes = [(p["spoken"], m) for _cid, p, m in session.tool_results]
    assert ("Understood, one moment.", ToolResultMode.WHEN_IDLE) not in modes, modes


def test_progress_on_dispatch_still_requires_a_nonzero_threshold_to_exist_at_all(
    harness, monkeypatch  # noqa: F811
):
    """`progress_after_sec=0` remains the "no holding line at all" switch --
    `progress_on_dispatch` only changes WHEN a line that would fire is said,
    not WHETHER one exists."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, progress_spoken="Understood, one moment.",
        progress_on_dispatch=True,
    )
    session = state["session"]
    session.supports_out_of_band_speech = False

    async def _fast(**kwargs):
        await _a.sleep(0.1)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fast)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")

    modes = [(p["spoken"], m) for _cid, p, m in session.tool_results]
    assert ("Understood, one moment.", ToolResultMode.WHEN_IDLE) not in modes, modes


def test_progress_on_dispatch_relay_path_still_only_uses_out_of_band(
    harness, monkeypatch  # noqa: F811
):
    """The relay (custody) holding line has no call id to ride a WHEN_IDLE
    result on -- it is out-of-band only, gated on `supports_out_of_band_
    speech`. On a provider without it (Gemini), `progress_on_dispatch` must
    not invent a delivery path that does not exist: no line, same as today.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=4.0, progress_spoken="Understood, one moment.",
        progress_on_dispatch=True,
        relay_intent_terms=["iban"],
    )
    session = state["session"]
    session.supports_out_of_band_speech = False
    session.finalizes_user_transcripts = True

    async def _fast(**kwargs):
        await _a.sleep(0.1)
        return DelegateResult(spoken="Done.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fast)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="I need to change my iban", final=True))
        _read_until(ws, "display")

    assert session.out_of_band == []


def test_progress_on_dispatch_speaks_the_relay_holding_line_immediately(
    harness, monkeypatch  # noqa: F811
):
    """Positive coverage for `_speak_relay_progress`, on a provider that DOES
    support out-of-band speech (the fake's default -- the Azure shape): the
    negative test elsewhere only proves Gemini gets no line on this path,
    which is silent about whether the line fires at all when it CAN.
    progress_on_dispatch=True must speak it right at dispatch, before a
    delegate far faster than the 4.0s timer even has a chance to complete."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=4.0, progress_spoken="Still working on that.",
        progress_on_dispatch=True,
    )
    session = state["session"]
    assert session.supports_out_of_band_speech is True

    async def _fast(**kwargs):
        await _a.sleep(0.1)
        return DelegateResult(spoken="Code sent.", display="d", charts=[],
                              flow_signals=["send_otp"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fast)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        # "iban" is in the built-in relay_intent_terms default -- opens
        # custody on the caller's own words, before any tool runs, and
        # dispatches _handle_relayed_turn.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="I want to change my IBAN please",
                                   final=True))
        _read_until(ws, "display")

    assert session.out_of_band == ["Still working on that."], session.out_of_band


def test_progress_on_dispatch_speaks_the_recovery_holding_line_immediately(
    harness, monkeypatch  # noqa: F811
):
    """Positive coverage for `_speak_recovery_progress`, same reasoning as
    the relay test above -- the custody path has incident history (a
    fabricated IBAN write, an unsent-code request) and deserves proof the
    line actually fires, not just that Gemini is correctly denied it."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=4.0, progress_spoken="Still working on that.",
        progress_on_dispatch=True,
    )
    session = state["session"]
    assert session.supports_out_of_band_speech is True

    async def _fast(**kwargs):
        await _a.sleep(0.1)
        return DelegateResult(spoken="Here is your answer.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fast)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        # The dropped-question race: a committed utterance followed by a
        # blank ask_agent call (truncated arguments) triggers the recovery
        # path directly (see test_a_blank_ask_agent_after_a_committed_
        # question_recovers_it in test_voice_live_endpoint.py).
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="how were sales yesterday", final=True))
        session.push(RealtimeEvent(type="tool_call", call_id="c-cut",
                                   tool_name="ask_agent", arguments={}))
        _read_until(ws, "display")

    assert session.out_of_band == ["Still working on that."], session.out_of_band
