"""A sessionId-less duplex call must get its OWN conversation.

`endpoints/voice_live.py` used to fall back to `f"voice:{agentKind}:{tenant_key}"`
when the client sent no `sessionId`, and `tenant_key` is
`businessId or rate_key or "anonymous"`. That string is CONSTANT per tenant, and
`coerce_session_id` maps it to a deterministic UUIDv5 — so every anonymous call
for a given agent kind landed on ONE `ai_chat_sessions` row, one trace group in
the observability explorer, and one recording bucket. On the WAJ landing-page
sales demo, where every visitor shares the one businessId, that meant every
caller's transcript accumulating in a single thread.

The clients that DO send a sessionId (a portal's chat.js mints one at page load so
a spoken turn and a typed turn share history) are unaffected: an explicit
sessionId still wins.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat
from voice.live.config import LiveVoiceConfig
from voice.live.testing import FakeRealtimeSession


@pytest.fixture
def harness(monkeypatch):
    """Anonymous connect against the fake `sample_voice` kind, no auth frame."""
    state = {"identities": []}
    live_ep._connect_limiter._store.clear()

    monkeypatch.setattr(
        live_ep, "_live_config_for",
        lambda _conn, _kind: LiveVoiceConfig(
            enabled=True, model_id="m-1", provider="fake"),
    )
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: (None, None))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(live_ep, "voice_agent_requires_auth", lambda kind: False)
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "persona")
    monkeypatch.setattr(live_ep, "recording_enabled", lambda: False)

    def _build(kind, params):
        class _A:
            profile = object()
        return _A(), _A.profile

    monkeypatch.setattr(live_ep, "build_agent_for_kind", _build)
    monkeypatch.setattr(
        live_ep, "_session_for",
        lambda cfg, **kw: FakeRealtimeSession(audio_format=AudioFormat(16000, 24000)),
    )

    real_identity = live_ep.TurnIdentity

    def _capture(**kwargs):
        state["identities"].append(kwargs)
        return real_identity(**kwargs)

    monkeypatch.setattr(live_ep, "TurnIdentity", _capture)

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _session_ids(state):
    return [i["session_id"] for i in state["identities"]]


def test_two_sessionless_calls_from_one_tenant_get_different_sessions(harness):
    """The reported bug: every voice call grouped into one thread."""
    client, state = harness
    url = "/voice/live?agentKind=sample_voice&businessId=b1"
    for _ in range(2):
        with client.websocket_connect(url) as ws:
            ws.receive_json()  # ready

    ids = _session_ids(state)
    assert len(ids) == 2, state["identities"]
    assert ids[0] != ids[1], (
        f"both calls share session {ids[0]!r} — every anonymous caller for this "
        "tenant would land in one conversation"
    )
    for sid in ids:
        assert "voice:sample_voice" not in sid, (
            f"{sid!r} is the tenant-keyed constant, not a per-call id"
        )


def test_an_explicit_session_id_is_still_honoured(harness):
    """Clients that share one id between typed and spoken turns must keep working."""
    client, state = harness
    url = "/voice/live?agentKind=sample_voice&businessId=b1&sessionId=abc-123"
    with client.websocket_connect(url) as ws:
        ws.receive_json()

    assert _session_ids(state) == ["abc-123"]


def test_the_same_explicit_id_reconnects_to_the_same_session(harness):
    """A second call in the same tab reuses the conversation, as before."""
    client, state = harness
    url = "/voice/live?agentKind=sample_voice&businessId=b1&sessionId=abc-123"
    for _ in range(2):
        with client.websocket_connect(url) as ws:
            ws.receive_json()

    ids = _session_ids(state)
    assert ids == ["abc-123", "abc-123"]
