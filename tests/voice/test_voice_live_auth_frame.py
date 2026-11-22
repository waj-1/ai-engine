"""First-frame auth for auth-required duplex kinds.

The harness mirrors tests/voice/test_voice_live_endpoint.py, with
`voice_agent_requires_auth` monkeypatched True and a made-up second
credential ("vault_token") and context keys standing in for what a kind's
registration declares, so the mechanism is tested against the same fake kind
the rest of the suite uses ("sample_voice") without coupling to any product
registration.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat
from voice.live.config import LiveVoiceConfig
from voice.live.protocol import CLOSE_FORBIDDEN
from voice.live.testing import FakeRealtimeSession
from runtime.agents.voice_registry import VoiceCredential, VoiceCredentialRefused


@pytest.fixture
def harness(monkeypatch):
    state = {}
    live_ep._connect_limiter._store.clear()

    monkeypatch.setattr(
        live_ep, "_live_config_for",
        lambda _conn, _kind: state.get(
            "cfg", LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")
        ),
    )
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    # Anonymous connect: the browser cannot set an Authorization header.
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: (None, None))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)

    def _capture_build(kind, params):
        state["params"] = params

        class _A:
            profile = object()

        return _A(), _A.profile

    monkeypatch.setattr(live_ep, "build_agent_for_kind", _capture_build)
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "persona")
    monkeypatch.setattr(live_ep, "voice_agent_requires_auth", lambda kind: True)
    # No second credential unless a test sets state["credential"] (VAULT).
    monkeypatch.setattr(live_ep, "voice_credential", lambda kind: state.get("credential"))
    monkeypatch.setattr(live_ep, "voice_client_context_keys",
                        lambda kind: ("record_id", "case_id", "screen_context"))
    monkeypatch.setattr(live_ep, "voice_session_metadata_keys",
                        lambda kind: ("record_id", "case_id"))
    # core.auth is the endpoint's (function-local import) verification seam.
    import core.auth as core_auth

    monkeypatch.setattr(
        core_auth, "validate_token_verified",
        lambda token: {"sub": "user-42"} if token == "good" else None,
    )

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    state["session"] = session

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


VAULT = VoiceCredential(name="vault_token", header="x-vault-token")


def _connect(client):
    return client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1")


def test_missing_auth_frame_times_out_to_forbidden(harness, monkeypatch):
    monkeypatch.setattr(live_ep, "_AUTH_FRAME_TIMEOUT_SEC", 0.2)
    client, state = harness
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client) as ws:
            ws.receive_json()  # blocks until the server closes
    assert exc.value.code == CLOSE_FORBIDDEN


def test_non_auth_first_frame_is_forbidden(harness):
    client, state = harness
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client) as ws:
            ws.send_json({"type": "mute"})
            ws.receive_json()
    assert exc.value.code == CLOSE_FORBIDDEN


def test_unverifiable_token_is_forbidden(harness):
    client, state = harness
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client) as ws:
            ws.send_json({"type": "auth", "token": "forged"})
            ws.receive_json()
    assert exc.value.code == CLOSE_FORBIDDEN


def test_verified_auth_frame_proceeds_and_threads_identity(harness, monkeypatch):
    client, state = harness
    state["credential"] = VAULT
    captured = {}
    real_identity = live_ep.TurnIdentity

    def _capture_identity(**kwargs):
        captured.update(kwargs)
        return real_identity(**kwargs)

    monkeypatch.setattr(live_ep, "TurnIdentity", _capture_identity)
    with _connect(client) as ws:
        ws.send_json({
            "type": "auth", "token": "good", "vault_token": "vt-1",
            "context": {"record_id": "r-9", "case_id": "c-3",
                        "screen_context": "detail", "ignored_key": "x"},
        })
        ready = ws.receive_json()

    assert ready["type"] == "ready"
    assert state["params"]["vault_token"] == "vt-1"
    assert state["params"]["user_token"] == "good"
    assert captured["user_id"] == "user-42"
    assert captured["session_metadata"]["record_id"] == "r-9"
    assert captured["session_metadata"]["case_id"] == "c-3"
    # A context key that is not a session-metadata key stays off the row.
    assert "screen_context" not in captured["session_metadata"]
    assert "ignored_key" not in captured["session_metadata"]


def test_identity_source_override_is_honored(harness, monkeypatch):
    client, state = harness
    monkeypatch.setattr(live_ep, "voice_identity_source", lambda kind: "records_app")
    captured = {}
    real_identity = live_ep.TurnIdentity

    def _capture_identity(**kwargs):
        captured.update(kwargs)
        return real_identity(**kwargs)

    monkeypatch.setattr(live_ep, "TurnIdentity", _capture_identity)
    with _connect(client) as ws:
        ws.send_json({"type": "auth", "token": "good"})
        ws.receive_json()

    assert captured["source"] == "records_app"


def test_kinds_without_requires_auth_are_untouched(harness, monkeypatch):
    """No auth frame expected: the handshake reaches ready immediately."""
    client, state = harness
    state["credential"] = VAULT
    monkeypatch.setattr(live_ep, "voice_agent_requires_auth", lambda kind: False)
    with _connect(client) as ws:
        assert ws.receive_json()["type"] == "ready"
    assert state["params"]["vault_token"] is None


def test_kind_without_a_credential_gets_no_credential_param(harness, monkeypatch):
    client, state = harness
    monkeypatch.setattr(live_ep, "voice_credential", lambda kind: None)
    with _connect(client) as ws:
        ws.send_json({"type": "auth", "token": "good", "vault_token": "vt-1"})
        assert ws.receive_json()["type"] == "ready"
    assert "vault_token" not in state["params"]


# ── Header-verified and sim-loopback callers skip the auth frame ──────────
# The frame exists only because a browser WebSocket cannot set headers. A
# server-to-server caller whose bearer header already SIGNATURE-VERIFIED
# (the voice eval driver's live mode, ops tooling) must not be asked to
# repeat the same credential as a frame; and the eval harness's trusted
# sim path (VOICE_LIVE_SIM_ENABLED + loopback + ?provider=simulated) runs
# on a replica the operator controls, where a JWT proves nothing.


def test_header_verified_caller_skips_the_auth_frame(harness, monkeypatch):
    client, state = harness
    monkeypatch.setattr(
        live_ep, "authenticate_optional", lambda authz: ("good", "user-42")
    )
    with _connect(client) as ws:
        assert ws.receive_json()["type"] == "ready"
    assert state["params"]["user_token"] == "good"


def test_header_verified_caller_credential_rides_the_header(harness, monkeypatch):
    client, state = harness
    state["credential"] = VAULT
    monkeypatch.setattr(
        live_ep, "authenticate_optional", lambda authz: ("good", "user-42")
    )
    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1",
        headers={"x-vault-token": "vt-hdr"},
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
    assert state["params"]["vault_token"] == "vt-hdr"


def test_unverified_header_still_requires_the_frame(harness, monkeypatch):
    """A merely-presented (forged/unverifiable) bearer is not identity —
    the frame gate stays."""
    client, state = harness
    monkeypatch.setattr(
        live_ep, "authenticate_optional", lambda authz: ("forged", "someone")
    )
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client) as ws:
            ws.send_json({"type": "mute"})
            ws.receive_json()
    assert exc.value.code == CLOSE_FORBIDDEN


def test_sim_loopback_eval_caller_skips_the_auth_frame(harness, monkeypatch):
    client, state = harness
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: True)
    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&provider=simulated"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"


def test_sim_param_without_the_gate_still_requires_auth(harness, monkeypatch):
    """?provider=simulated from a non-loopback peer (or with the env flag
    off) must not become an auth bypass — the param stays silently ignored
    and the frame gate stays."""
    client, state = harness
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: False)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/voice/live?agentKind=sample_voice&businessId=b1&provider=simulated"
        ) as ws:
            ws.send_json({"type": "mute"})
            ws.receive_json()
    assert exc.value.code == CLOSE_FORBIDDEN


def _read_until(ws, kind, limit=10):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


def test_header_authed_caller_auth_frame_context_reaches_the_delegate(harness, monkeypatch):
    """Header-auth skips the frame-auth branch, but the client still sends
    its opening `{"type":"auth", "context":{...}}` frame — which used to be
    silently discarded by the pump loop, leaving delegate_ctx empty for the
    whole call (the agent then re-searched for the record on screen).
    Only the context payload is rescued; identity stays the header's."""
    from voice.live.base import RealtimeEvent
    from voice.live.delegate import DelegateResult

    client, state = harness
    # Bearer header present and verifiable: the header-auth branch runs and
    # no first frame is consumed by the handshake.
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("good", "user-42"))
    seen = {}

    async def _fake_delegate(**kwargs):
        seen.update(kwargs)
        return DelegateResult(spoken="Done.", display="Done.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)
    session = state["session"]
    with _connect(client) as ws:
        ws.send_json({
            "type": "auth", "token": "good",
            "context": {"record_id": "r-9", "case_id": "c-3",
                        "ignored_key": "x"},
        })
        assert ws.receive_json()["type"] == "ready"
        session.push(RealtimeEvent(type="tool_call", call_id="c1",
                                   tool_name="ask_agent",
                                   arguments={"query": "status?"}))
        _read_until(ws, "display")
    extra = seen.get("extra_context") or {}
    assert extra.get("record_id") == "r-9"
    assert extra.get("case_id") == "c-3"
    assert "ignored_key" not in extra


def test_pump_loop_auth_frame_cannot_rebind_identity(harness, monkeypatch):
    """The rescued auth frame carries a token too — it must be ignored. A
    frame arriving mid-session (after the handshake settled identity) that
    could re-bind the user or their second credential would be an auth
    bypass."""
    from voice.live.base import RealtimeEvent
    from voice.live.delegate import DelegateResult

    client, state = harness
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("good", "user-42"))

    async def _fake_delegate(**kwargs):
        return DelegateResult(spoken="Done.", display="Done.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)
    session = state["session"]
    with _connect(client) as ws:
        # A hostile frame naming someone else's token: context is taken,
        # params/identity built at handshake time stay the header's.
        ws.send_json({"type": "auth", "token": "forged",
                      "vault_token": "vt-stolen",
                      "context": {"record_id": "r-1"}})
        assert ws.receive_json()["type"] == "ready"
        session.push(RealtimeEvent(type="tool_call", call_id="c1",
                                   tool_name="ask_agent",
                                   arguments={"query": "hi"}))
        _read_until(ws, "display")
    assert state["params"]["user_token"] == "good"
    assert state["params"].get("vault_token") != "vt-stolen"


# ── The kind's second credential (VoiceCredential) ────────────────────────


def test_auth_frame_without_the_credential_is_refused(harness):
    """A kind that reads its backend AS the caller: a verified auth frame
    with no credential is refused (never run on project-wide credentials)."""
    client, state = harness
    state["credential"] = VAULT
    state["credential"] = VAULT
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client) as ws:
            ws.send_json({"type": "auth", "token": "good"})
            ws.receive_json()
    assert exc.value.code == CLOSE_FORBIDDEN
    assert exc.value.reason == "vault_token_required"
    assert "params" not in state  # agent never built


def test_header_verified_caller_without_the_credential_is_refused(harness, monkeypatch):
    client, state = harness
    state["credential"] = VAULT
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("good", "user-42"))
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client) as ws:
            ws.receive_json()
    assert exc.value.code == CLOSE_FORBIDDEN
    assert exc.value.reason == "vault_token_required"
    assert "params" not in state


def test_simulated_eval_path_reaches_ready_without_the_credential(harness, monkeypatch):
    """The trusted loopback simulated-eval path carries no auth frame and no
    credential and must keep working."""
    client, state = harness
    state["credential"] = VAULT
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: True)
    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&provider=simulated"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
    assert state["params"]["vault_token"] is None


def test_credential_is_verified_with_the_callers_identity(harness):
    client, state = harness
    seen = {}

    def _verify(value, user_id):
        seen.update(value=value, user_id=user_id)

    state["credential"] = VoiceCredential(name="vault_token", header="x-vault-token", verify=_verify)
    with _connect(client) as ws:
        ws.send_json({"type": "auth", "token": "good", "vault_token": "vt-1"})
        assert ws.receive_json()["type"] == "ready"
    assert seen == {"value": "vt-1", "user_id": "user-42"}


def test_refused_credential_closes_with_its_detail(harness):
    client, state = harness

    def _verify(value, user_id):
        raise VoiceCredentialRefused("vault_token_expired", "test")

    state["credential"] = VoiceCredential(name="vault_token", header="x-vault-token", verify=_verify)
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client) as ws:
            ws.send_json({"type": "auth", "token": "good", "vault_token": "vt-old"})
            ws.receive_json()
    assert exc.value.code == CLOSE_FORBIDDEN
    assert exc.value.reason == "vault_token_expired"
    assert "params" not in state
