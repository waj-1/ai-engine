"""The duplex endpoint's dictation-capture seam (voice/live/dictation.py).

The engine owns the delivery; a plugin supplies the collector through the
``voice_capture`` hook. These tests pin both halves of that split with no
product code: a build with no collector never opens one, and a collector a
plugin hands in is opened on ITS signal and spoken through the say-exactly
path. The product's own collector is tested in its plugin's tests.
"""
import logging
import time
from dataclasses import dataclass

import endpoints.voice_live as live_ep
from voice.live import dictation
from voice.live.base import RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult
from tests.voice.test_voice_live_endpoint import _read_until, harness  # noqa: F401


@dataclass
class _Decision:
    action: str
    spoken: str = ""
    digits: str = ""
    message: str = ""


class _FakeState(dictation.ClosedCaptureState):
    pass


class _FakeCollector:
    """Collects digits until four are held; tallies every group."""

    name = "code"
    open_signal = "code_flow_open"

    def new_state(self):
        return _FakeState()

    def open_capture(self, state, *, now):
        state.open = True
        state.digits = ""

    def feed(self, state, utterance, *, language, is_backchannel, now):
        digits = "".join(ch for ch in utterance if ch.isdigit())
        if not digits:
            state.reset()
            return _Decision("close")
        state.digits += digits
        return _Decision("tally", spoken=f"Holding {state.digits}.")


def _cfg():
    return LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        server_side_iban_capture=True, language="en", relay_mode=False,
    )


def _delegate_reporting(signal):
    async def _fake_delegate(**kwargs):
        return DelegateResult(spoken="Verified.", display="d", charts=[],
                              flow_signals=[signal])
    return _fake_delegate


def _call(client, session, utterance):
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "verify my code"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text=utterance, final=True))
        deadline = time.time() + 2.0
        while not session.text_turns and time.time() < deadline:
            ws.send_json({"type": "mute"})
            time.sleep(0.02)


def test_a_build_with_no_plugin_has_no_collector(monkeypatch):
    import engine_plugins

    monkeypatch.setattr(engine_plugins, "PLUGINS", ())
    assert dictation.dictation_capture() is None


def test_no_collector_never_opens_and_warns_once(harness, monkeypatch, caplog):
    """The flag on a build with no collector is inert: the model keeps its
    own tally, nothing is cancelled, and the operator is told once why."""
    client, state = harness
    state["cfg"] = _cfg()
    session = state["session"]
    monkeypatch.setattr(live_ep, "dictation_capture", lambda: None)
    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate_reporting("code_flow_open"))

    with caplog.at_level(logging.WARNING, logger=live_ep.logger.name):
        _call(client, session, "44 20")

    assert session.text_turns == [], session.text_turns
    assert session.cancelled == 0
    warnings = [r for r in caplog.records if "no dictation collector" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


def test_a_plugin_collector_opens_on_its_own_signal_and_is_spoken(harness, monkeypatch):
    client, state = harness
    state["cfg"] = _cfg()
    session = state["session"]
    monkeypatch.setattr(live_ep, "dictation_capture", lambda: _FakeCollector())
    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate_reporting("code_flow_open"))

    _call(client, session, "44 20")

    assert session.cancelled >= 1, "the model's own reply was never cancelled"
    assert any(
        t.startswith("Say exactly this to the caller") and "Holding 4420." in t
        for t in session.text_turns
    ), session.text_turns


def test_another_flow_signal_does_not_open_the_collector(harness, monkeypatch):
    client, state = harness
    state["cfg"] = _cfg()
    session = state["session"]
    monkeypatch.setattr(live_ep, "dictation_capture", lambda: _FakeCollector())
    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate_reporting("send_otp_ok"))

    _call(client, session, "44 20")

    assert session.text_turns == [], session.text_turns
