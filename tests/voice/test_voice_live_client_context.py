"""Client context frames reach the delegated turn; proposals reach the client.

Harness mirrors tests/voice/test_voice_live_endpoint.py. Which context keys a
client may set is the kind's registration (`client_context_keys`); the keys
here ("record_id", "case_id") are test data.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat, RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult
from voice.live.testing import FakeRealtimeSession


@pytest.fixture
def harness(monkeypatch):
    state = {}
    live_ep._connect_limiter._store.clear()

    monkeypatch.setattr(
        live_ep, "_live_config_for",
        lambda _conn, _kind: LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake"),
    )
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("tok", "user-1"))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(live_ep, "build_agent_for_kind", lambda kind, params: (object(), object()))
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "persona")
    monkeypatch.setattr(live_ep, "voice_client_context_keys", lambda kind: ("record_id", "case_id"))

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    state["session"] = session

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _read_until(ws, kind, limit=10):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


def test_proposals_are_forwarded_after_a_tool_call(harness, monkeypatch):
    client, state = harness
    proposal_event = {
        "type": "proposal",
        "proposal": {"type": "address_change", "value": "12 Main St",
                     "confidence": 0.92, "source_text": "I moved to 12 Main St"},
    }

    async def _fake_delegate(**kwargs):
        return DelegateResult(
            spoken="Queued for your review.", display="Queued an address change.",
            charts=[], proposals=[proposal_event],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
        session.push(RealtimeEvent(type="tool_call", call_id="c1",
                                   tool_name="ask_agent", arguments={"query": "note the new address"}))
        frame = _read_until(ws, "proposal")
    assert frame == proposal_event


def test_context_frame_reaches_the_next_delegated_turn(harness, monkeypatch):
    client, state = harness
    seen = {}

    async def _fake_delegate(**kwargs):
        seen.update(kwargs)
        return DelegateResult(spoken="Done.", display="Done.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "context", "record_id": "r-7",
                      "case_id": "c-2", "not_allowed": "x"})
        session.push(RealtimeEvent(type="tool_call", call_id="c1",
                                   tool_name="ask_agent", arguments={"query": "status?"}))
        _read_until(ws, "display")
    assert seen["extra_context"]["record_id"] == "r-7"
    assert seen["extra_context"]["case_id"] == "c-2"
    assert "not_allowed" not in seen["extra_context"]


def test_a_later_context_frame_replaces_the_earlier_one(harness, monkeypatch):
    """The user navigated to another record: full replacement, no merge --
    a stale record_id merged under a new case describes the wrong record."""
    client, state = harness
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs.get("extra_context"))
        return DelegateResult(spoken="Done.", display="Done.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "context", "record_id": "r-1", "case_id": "c-1"})
        session.push(RealtimeEvent(type="tool_call", call_id="c1",
                                   tool_name="ask_agent", arguments={"query": "one"}))
        _read_until(ws, "display")
        ws.send_json({"type": "context", "record_id": "r-2"})
        session.push(RealtimeEvent(type="tool_call", call_id="c2",
                                   tool_name="ask_agent", arguments={"query": "two"}))
        _read_until(ws, "display")
    assert calls[0]["record_id"] == "r-1"
    assert calls[1]["record_id"] == "r-2"
    assert "case_id" not in calls[1]


def test_no_context_means_none_extra_context(harness, monkeypatch):
    client, state = harness
    seen = {}

    async def _fake_delegate(**kwargs):
        seen.update(kwargs)
        return DelegateResult(spoken="Done.", display="Done.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
        session.push(RealtimeEvent(type="tool_call", call_id="c1",
                                   tool_name="ask_agent", arguments={"query": "hello"}))
        _read_until(ws, "display")
    assert seen["extra_context"] is None
