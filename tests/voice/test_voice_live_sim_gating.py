"""Gating for the eval-only simulated-provider override and text frame.

?provider=simulated must be honored ONLY when VOICE_LIVE_SIM_ENABLED=1 AND
the peer is loopback; otherwise silently ignored. The {"type":"text"} client
frame must reach send_text_turn ONLY on simulated sessions.
"""
import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat
from voice.live.config import LiveVoiceConfig
from voice.live.simulated import SimulatedRealtimeSession
from voice.live.testing import FakeRealtimeSession
from tests.product import requires_product


def _ws(host, headers=None):
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=host), headers=headers or {}
    )


def test_sim_override_needs_flag_and_loopback(monkeypatch):
    monkeypatch.delenv("VOICE_LIVE_SIM_ENABLED", raising=False)
    assert live_ep._sim_override_allowed(_ws("127.0.0.1")) is False
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "1")
    assert live_ep._sim_override_allowed(_ws("127.0.0.1")) is True
    assert live_ep._sim_override_allowed(_ws("::1")) is True
    assert live_ep._sim_override_allowed(_ws("203.0.113.9")) is False
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "0")
    assert live_ep._sim_override_allowed(_ws("127.0.0.1")) is False


def test_sim_override_missing_client_is_denied(monkeypatch):
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "1")
    assert live_ep._sim_override_allowed(
        types.SimpleNamespace(client=None, headers={})
    ) is False


def test_sim_override_denied_when_forwarded_for_present(monkeypatch):
    # Finding 1: uvicorn runs with --proxy-headers --forwarded-allow-ips="*"
    # (Dockerfile), so ProxyHeadersMiddleware rewrites scope["client"] from
    # a client-supplied X-Forwarded-For — a remote caller sending
    # "X-Forwarded-For: 127.0.0.1" would otherwise pass the loopback check.
    # A genuine loopback connection (the eval driver) never sends any
    # forwarding header, so this must deny only when one is present.
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "1")
    assert live_ep._sim_override_allowed(
        _ws("127.0.0.1", headers={"x-forwarded-for": "203.0.113.9"})
    ) is False


@pytest.mark.parametrize(
    "header_name",
    ["x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded"],
)
def test_sim_override_denied_for_any_forwarding_header(monkeypatch, header_name):
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "1")
    assert live_ep._sim_override_allowed(
        _ws("127.0.0.1", headers={header_name: "x"})
    ) is False


def test_sim_override_allowed_with_empty_headers(monkeypatch):
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "1")
    assert live_ep._sim_override_allowed(_ws("127.0.0.1", headers={})) is True


def test_sim_override_allowed_when_headers_attr_missing(monkeypatch):
    # Defensive: a fake/mocked websocket that doesn't set `.headers` at all
    # must not raise — treat it the same as "no forwarding headers".
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "1")
    ws = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
    assert live_ep._sim_override_allowed(ws) is True


@pytest.fixture
def harness(monkeypatch):
    state = {}

    # Same rate-limiter reset precedent as tests/voice/test_voice_live_endpoint.py.
    live_ep._connect_limiter._store.clear()

    def _cfg(_conn, _kind):
        return state.get("cfg", LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake"))

    monkeypatch.setattr(live_ep, "_live_config_for", _cfg)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("tok", "user-1"))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(live_ep, "build_agent_for_kind", lambda kind, params: (object(), object()))
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "You are a voice agent.")

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    state["session"] = session

    def _session_for(cfg, **kw):
        if cfg.provider == "simulated":
            sim = SimulatedRealtimeSession(None, persona="p")
            state["session"] = sim
            return sim
        return session

    monkeypatch.setattr(live_ep, "_session_for", _session_for)

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _read_until(ws, kind, limit=8):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


def test_provider_param_ignored_when_gate_denied(harness, monkeypatch):
    client, state = harness
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: False)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&provider=simulated"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"

    assert isinstance(state["session"], FakeRealtimeSession)


def test_text_frame_dropped_on_non_simulated_session(harness, monkeypatch):
    client, state = harness
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: False)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&provider=simulated"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_text(json.dumps({"type": "text", "text": "hello"}))
        ws.send_text(json.dumps({"type": "played", "ms": 123}))

    session = state["session"]
    assert isinstance(session, FakeRealtimeSession)
    assert "hello" not in session.text_turns
    assert session.playback_reports == [123.0]


def test_ready_frame_declares_simulated_provider_when_override_applied(harness, monkeypatch):
    # Finding 4: the driver's sim-mode abort check depends on this marker
    # being present ONLY when the sim override actually landed.
    client, state = harness
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: True)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&provider=simulated"
    ) as ws:
        ready = ws.receive_json()

    assert ready["type"] == "ready"
    assert ready.get("provider") == "simulated"


def test_ready_frame_omits_provider_key_on_normal_session(harness, monkeypatch):
    # No probing surface: a caller that never asked for (or wasn't granted)
    # the simulated provider must not see the key at all.
    client, state = harness
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: False)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ready = ws.receive_json()

    assert ready["type"] == "ready"
    assert "provider" not in ready
    assert isinstance(state["session"], FakeRealtimeSession)


def test_text_frame_reaches_simulated_session(harness, monkeypatch):
    client, state = harness
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: True)
    # SimulatedRealtimeSession treats its FIRST send_text_turn as the
    # greeting trigger (voice/live/simulated.py's module docstring) -- a
    # profile with no configured greeting_trigger would have the caller's
    # own "hi there" swallowed as that greeting instead of producing a user
    # transcript. Configure one, matching how the client-demo profiles actually
    # run, so the endpoint's own pre-pump greeting send consumes that slot.
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        greeting_trigger="Hello, thanks for calling.",
    )

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&provider=simulated"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_text(json.dumps({"type": "text", "text": "hi there"}))
        found = None
        for _ in range(16):
            frame = ws.receive_json()
            if (
                frame.get("type") == "transcript"
                and frame.get("role") == "user"
                and frame.get("text") == "hi there"
            ):
                found = frame
                break
        assert found is not None, "no matching user transcript frame received"

    session = state["session"]
    assert isinstance(session, SimulatedRealtimeSession)
    assert "hi there" in session.text_turns


@requires_product
def test_business_sim_run_still_takes_the_auth_frame(harness, monkeypatch):
    """A business sim eval run: the merchant's JWT and ai_access ride the auth
    frame exactly as from the app (the brain reads data AS the caller). The
    sim branch used to skip the frame for every auth-gated kind, so a
    business sim call was closed 4403 before it started."""
    client, state = harness
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: True)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: (None, None))
    monkeypatch.setattr("core.auth.validate_token_verified", lambda tok: {"sub": "user-1"})
    monkeypatch.setattr("waj.voice_builders._is_business_member", lambda tok, biz: tok == "jwt")
    seen = {}
    monkeypatch.setattr(live_ep, "build_agent_for_kind",
                        lambda kind, params: (seen.update(params) or object(), object()))
    with client.websocket_connect(
        "/voice/live?agentKind=business&businessId=11111111-1111-1111-1111-111111111111&provider=simulated"
    ) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "jwt", "ai_access": {"data": True}}))
        ready = ws.receive_json()
    assert ready["type"] == "ready" and ready.get("provider") == "simulated"
    assert seen["user_token"] == "jwt" and seen["ai_access"]["data"] is True


def test_simulated_calls_never_bill_the_tenants_budget(harness, monkeypatch):
    """One local eval pass billed one tenant's real day (3584 of 3600 s) and
    its owner's next real call was refused. Sim calls are unmetered."""
    client, state = harness
    keys = []
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda key, sec: keys.append(key) or 0)
    monkeypatch.setattr(live_ep, "check_audio_budget", lambda key, *a, **k: keys.append(key))
    biz = "11111111-1111-1111-1111-111111111111"
    for sim in (False, True):
        monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws, sim=sim: sim)
        with client.websocket_connect(f"/voice/live?agentKind=sample_voice&businessId={biz}&provider=simulated") as ws:
            assert ws.receive_json()["type"] == "ready"
        keys.append("|")
    split = keys.index("|")
    real_keys, sim_keys = set(keys[:split]), set(keys[split + 1:-1])
    assert len(real_keys) == 1
    assert not sim_keys, "a sim call touched the audio budget"
