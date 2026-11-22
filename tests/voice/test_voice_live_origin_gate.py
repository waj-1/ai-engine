"""Regression tests for review finding IMPORTANT 4 (Task 13 follow-up
review, corrected in a second pass): before the portal-session-cookie fix
(Defect B), `/voice/live` carried no ambient credential, so an `Origin`
header matching no configured channel was harmless — `enforce_channel_kind`
deliberately SKIPS enforcement in that case, on the reasoning that a
non-browser/server-to-server caller just has no `Origin` header at all.

Now a portal's session cookie rides along on the handshake. A WebSocket
handshake also gets NO CORS preflight (unlike fetch/XHR), so a browser will
happily open one to an arbitrary origin regardless of this server's CORS
allow-list. The Origin gate is deliberately OPT-IN, not "reject any
unmatched Origin" (see `endpoints/voice_live.py`'s module-level comment):
today's demo-stack channel seeders leave `allowed_origins` empty, so rejecting
unconditionally would reject every real browser voice connection on every
current deployment in exchange for closing a risk `SameSite=Lax` already
mitigates — the wrong trade. The gate only rejects once an operator has
opted in by giving SOME channel a non-empty `allowed_origins`
(`core.channel_resolver.any_channel_has_origin_allowlist()`); until then an
unmatched Origin is allowed (with a warning logged either way, so an
operator can tell from the logs which branch fired). A caller with no
Origin header at all is unaffected either way (the existing, intentionally-
permitted non-browser path).

Strategy: unlike the other `test_voice_live_*` files, this one does NOT
monkeypatch `resolve_channel` — it needs the REAL channel-matching logic
(`core.channel_resolver.resolve_channel` -> `core.channels.get_active_channels`)
so the Origin-vs-channel match actually runs. Only `core.channels.
get_active_channels` is stubbed (the DB boundary), same seam
`core/channel_resolver.py`'s own tests use.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect as WSDisconnect

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat
from voice.live.config import LiveVoiceConfig
from voice.live.testing import FakeRealtimeSession

# A channel that HAS opted into origin restriction (non-empty
# allowed_origins) -- the "configured" state.
_CONFIGURED_CHANNEL = {
    "channel_id": "chan-1",
    "slug": "partner_portal",
    "allowed_origins": ["https://portal.partner.example"],
    "allowed_agent_kinds": [],
}

# The channel shape today's real demo-stack seeders actually produce --
# allowed_origins left empty. The
# "unconfigured" state the gate must stay dormant for.
_UNCONFIGURED_CHANNEL = {
    "channel_id": "chan-2",
    "slug": "sample_voice",
    "allowed_origins": [],
    "allowed_agent_kinds": [],
}


def _make_harness(monkeypatch, channels: dict):
    state = {"build_calls": []}

    live_ep._connect_limiter._store.clear()
    live_ep._active.clear()

    # Real resolve_channel/enforce_channel_kind (NOT monkeypatched to a
    # no-op, unlike the other test files here) -- stub only the DB-backed
    # channel snapshot underneath them.
    monkeypatch.setattr("core.channels.get_active_channels", lambda: dict(channels))

    def _cfg(_conn, _kind):
        return LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")

    def _build_agent(kind, params):
        state["build_calls"].append((kind, dict(params)))
        return object(), object()

    monkeypatch.setattr(live_ep, "_live_config_for", _cfg)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: (None, None))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(live_ep, "build_agent_for_kind", _build_agent)
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "persona")

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


@pytest.fixture
def harness(monkeypatch):
    """Default harness: a channel WITH a configured allow-list (the gate
    is active) -- most tests in this file want the gate switched on."""
    return _make_harness(monkeypatch, {"partner_portal": _CONFIGURED_CHANNEL})


# ---------------------------------------------------------------------------
# Branch 1: a configured allow-list rejects a foreign origin.
# ---------------------------------------------------------------------------


def test_configured_allowlist_rejects_a_foreign_origin(harness):
    """Once an operator has opted in (some channel has a non-empty
    allowed_origins), an Origin that matches none of them is genuinely not
    permitted and must be rejected -- there is no CORS preflight to have
    stopped it earlier."""
    client, _state = harness

    with pytest.raises(WSDisconnect) as exc_info:
        with client.websocket_connect(
            "/voice/live?agentKind=sample_voice",
            headers={"origin": "https://evil.example"},
        ) as ws:
            ws.receive_json()
    assert exc_info.value.code == live_ep.CLOSE_FORBIDDEN


def test_origin_matching_an_active_channel_still_connects(harness):
    """The fix must not reject legitimate browser traffic — an Origin that
    DOES match a configured channel connects exactly as before."""
    client, _state = harness

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice",
        headers={"origin": "https://portal.partner.example"},
    ) as ws:
        ready = ws.receive_json()
    assert ready["type"] == "ready"


def test_no_origin_header_still_connects(harness):
    """The existing, intentionally-permitted non-browser/server-to-server
    path must not regress: a caller with no Origin header at all (the
    normal case for `enforce_channel_kind`'s skip) still connects,
    regardless of whether the allow-list is configured."""
    client, _state = harness

    with client.websocket_connect("/voice/live?agentKind=sample_voice") as ws:
        ready = ws.receive_json()
    assert ready["type"] == "ready"


# ---------------------------------------------------------------------------
# Branch 2: an unconfigured deployment (no channel has any allowed_origins)
# allows an unmatched origin through -- the gate is opt-in, not "reject any
# unmatched Origin".
# ---------------------------------------------------------------------------


def test_unconfigured_deployment_allows_a_foreign_origin(monkeypatch):
    """The real, current state of a fresh demo-stack deployment: the seeded
    channel row has `allowed_origins: []`. Rejecting here would break every
    real browser voice connection on every such deployment (each one sends a
    same-origin Origin header) in exchange for closing a risk `SameSite=Lax`
    already mitigates -- the wrong trade. Must still connect."""
    client, _state = _make_harness(monkeypatch, {"sample_voice": _UNCONFIGURED_CHANNEL})

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice",
        headers={"origin": "https://partner-demo.example"},
    ) as ws:
        ready = ws.receive_json()
    assert ready["type"] == "ready"


def test_no_channels_at_all_allows_a_foreign_origin(monkeypatch):
    """Same as above but for a deployment with NO channel row at all yet
    (before the stack's channel seeder has ever been run) --
    `any_channel_has_origin_allowlist()` must treat "no channels" the same
    as "channels with empty allow-lists": nothing configured, gate stays
    dormant."""
    client, _state = _make_harness(monkeypatch, {})

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice",
        headers={"origin": "https://partner-demo.example"},
    ) as ws:
        ready = ws.receive_json()
    assert ready["type"] == "ready"


def test_unconfigured_deployment_logs_a_warning_that_the_gate_is_inactive(monkeypatch, caplog):
    """A silent allow here is exactly how this gets forgotten -- the log
    must say plainly that the gate is inactive, so an operator scanning
    logs (or investigating why a partner-embed exploit didn't get caught)
    can find it."""
    client, _state = _make_harness(monkeypatch, {"sample_voice": _UNCONFIGURED_CHANNEL})

    with caplog.at_level(logging.WARNING, logger="endpoints.voice_live"):
        with client.websocket_connect(
            "/voice/live?agentKind=sample_voice",
            headers={"origin": "https://partner-demo.example"},
        ) as ws:
            ws.receive_json()

    assert any(
        "inactive" in record.message.lower() for record in caplog.records
    ), "expected a warning naming the Origin gate as inactive"
