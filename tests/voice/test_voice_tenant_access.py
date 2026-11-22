"""A kind registered with VoiceTenantAccess (runtime/agents/voice_registry.py):
the duplex handshake admits only a member of the tenant the call names, and
hands the kind's auth-frame reader's params to its builder. A fictional kind,
so this holds in a build with no plugin."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import endpoints.voice_live as live_ep
from runtime.agents import voice_registry
from voice.live.base import AudioFormat
from voice.live.config import LiveVoiceConfig
from voice.live.protocol import CLOSE_FORBIDDEN
from voice.live.testing import FakeRealtimeSession

TENANT = "7af7fb72-3995-410c-93e3-b7c6b9521d35"


@pytest.fixture
def harness(monkeypatch):
    state = {"member": True, "admit_calls": [], "live_config_calls": 0}
    live_ep._connect_limiter._store.clear()

    def _admit(token, tenant_id):
        state["admit_calls"].append((token, tenant_id))
        return state["member"]

    def _frame_params(frame):
        return ({"level": str((frame or {}).get("level") or "none")},
                {"desk": (frame or {}).get("desk")})

    monkeypatch.setitem(voice_registry._BUILDERS, "sample_tenant_voice", lambda params: None)
    monkeypatch.setitem(voice_registry._OPTIONS, "sample_tenant_voice", {
        "requires_auth": True, "identity_source": None, "session_reader": None,
        "default_phrase_list": None, "credential": None,
        "tenant_access": voice_registry.VoiceTenantAccess(
            admit=_admit, frame_params=_frame_params, refused_reason="Not a member."),
        "client_context_keys": ("screen_context",), "session_metadata_keys": (),
    })

    def _live_config_for(_c, _k):
        state["live_config_calls"] += 1
        return LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")

    monkeypatch.setattr(live_ep, "_live_config_for", _live_config_for)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: (None, None))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)

    def _capture_build(kind, params):
        state["params"] = params

        class _A:
            profile = object()

        return _A(), _A.profile

    monkeypatch.setattr(live_ep, "build_agent_for_kind", _capture_build)
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "persona")
    import core.auth as core_auth
    monkeypatch.setattr(core_auth, "validate_token_verified",
                        lambda token: {"sub": "user-42"} if token == "good" else None)
    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _connect(client):
    return client.websocket_connect(f"/voice/live?agentKind=sample_tenant_voice&businessId={TENANT}")


def test_a_non_member_is_refused_before_any_config_work(harness):
    client, state = harness
    state["member"] = False
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client) as ws:
            ws.send_json({"type": "auth", "token": "good"})
            ws.receive_json()
    assert exc.value.code == CLOSE_FORBIDDEN
    assert state["admit_calls"] == [("good", TENANT)]
    assert "params" not in state and state["live_config_calls"] == 0


def test_a_member_gets_the_frame_params(harness):
    client, state = harness
    with _connect(client) as ws:
        ws.send_json({"type": "auth", "token": "good", "level": "full", "desk": "front"})
        assert ws.receive_json()["type"] == "ready"
    assert state["params"]["level"] == "full"
    assert state["params"]["user_token"] == "good"
