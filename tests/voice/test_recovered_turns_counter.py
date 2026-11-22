"""The stall-recovery blind spot: eleven recoveries, a clean summary.

Live 2026-08-27, one long call: the model stopped calling ask_agent entirely
and ELEVEN consecutive turns were answered by _handle_recovered_turn's direct
delegation — while the session summary read `reprompts=0` (that counter is
the bare response.create nudge only) and nothing else counted the recoveries.
The collapse was invisible outside WARNING log lines. These tests pin the
counter that makes it visible: `recovered_turns` on the session summary span.
"""
import time as _t

import endpoints.voice_live as live_ep
import voice.live.metrics as metrics_mod
from voice.live.base import RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

from tests.voice.test_voice_live_endpoint import harness  # noqa: F401

TOPIC_TERMS = ["sales", "terminal", "مبيعات"]


def _run(harness, monkeypatch, caller: str, reply: str):  # noqa: F811
    """Drive one caller utterance + one no-tool model reply through the real
    endpoint; return the captured session-summary span attrs."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", progress_after_sec=0,
        stall_recovery_sec=0.15,
        stall_recovery_unannounced_sec=0.15,
        stall_recovery_topic_terms=TOPIC_TERMS,
    )
    session = state["session"]

    async def _delegate(**kwargs):
        return DelegateResult(spoken="RECOVERED", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    sessions = []
    real_emit = metrics_mod._emit_span

    def _capture(span_name, attrs):
        if span_name == metrics_mod.SESSION_SPAN:
            sessions.append(dict(attrs))
        return real_emit(span_name, attrs)

    monkeypatch.setattr(metrics_mod, "_emit_span", _capture)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user", text=caller, final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant", text=reply, final=True))
        session.push(RealtimeEvent(type="turn_end"))
        _t.sleep(0.6)
    return sessions


def test_a_recovered_turn_is_counted_in_the_session_summary(harness, monkeypatch):  # noqa: F811
    sessions = _run(harness, monkeypatch,
                    "what were my sales last month", "Sure thing.")
    assert sessions, "session summary span was never emitted"
    assert sessions[-1]["waj.voice.live.recovered_turns"] == 1


def test_a_clean_goodbye_counts_no_recovered_turns(harness, monkeypatch):  # noqa: F811
    """The recovery correctly stands down on a closure — and the counter must
    agree, or every polite call ends with a phantom collapse signal."""
    sessions = _run(harness, monkeypatch,
                    "no, that's all thanks", "Thank you for calling us. Goodbye!")
    assert sessions, "session summary span was never emitted"
    assert sessions[-1]["waj.voice.live.recovered_turns"] == 0
