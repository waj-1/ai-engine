"""The voice handshake must not park the event loop on its Supabase reads.

supabase-py is synchronous. Called straight from the async WS handler, each
of the handshake's reads (active profile + realtime model row, budget
ceiling, daily usage, the voice specialist, the persona's prompt version)
stops the whole replica for its round trip — other callers' handshakes and
the audio pumps of calls already in progress.

Live consequence, company.example 2026-09-03: a caller got "the live voice service
can't be reached right now" while the same replica served a burst of
`/recommendations` traffic. The engine was fine — its own reads finished at
~14s, each queued behind other requests' blocking calls, and the browser had
given up at 12s.

HOW THIS IS MEASURED, and why the obvious ways don't work.

`TestClient` gives each `websocket_connect` its own portal thread, so two
concurrent connections overlap whether or not the handler blocks — a timing
test there passes against the broken code. For the same reason the loop does
NOT run on the main thread, so "is this the main thread?" is answered False
either way. Both of those were written first and both passed against the
unfixed code, which is exactly the trap `CLAUDE.md` warns about.

What is unambiguous is a thread IDENTITY comparison against an anchor that
is known to run ON the loop: `enforce_channel_kind` is called inline in the
handler a few lines above the config read. A blocking call that reports the
same thread as that anchor is holding the loop; one that reports a different
thread is not. No timing, no mechanism assumptions.
"""
from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat
from voice.live.config import LiveVoiceConfig
from voice.live.testing import FakeRealtimeSession

# Every handshake call that reaches Supabase, in the order the handler runs
# them. `_live_config_for` covers the active profile + the realtime model
# row; `resolve_persona_prompt` covers the parent profile read AND the
# prompt-version read it makes inside.
BLOCKING_CALLS = (
    "_live_config_for",
    "check_audio_budget",
    "build_agent_for_kind",
    "resolve_persona_prompt",
    "reserve_audio_seconds",
)


@pytest.fixture
def threads_seen(monkeypatch):
    """Wire the endpoint so every blocking handshake call records the thread
    it ran on, alongside the on-loop anchor."""
    live_ep._connect_limiter._store.clear()
    seen: dict = {}

    def _anchor(conn, kind):
        # Runs inline in the handler (never wrapped) — this IS the loop.
        seen["loop"] = threading.get_ident()

    monkeypatch.setattr(live_ep, "enforce_channel_kind", _anchor)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(
        live_ep, "authenticate_optional", lambda authz: ("tok", "user-1")
    )

    def _record(name, result):
        def _fn(*_a, **_kw):
            # setdefault: FIRST call wins. `reserve_audio_seconds` is called
            # again at teardown, and that one is deliberately left
            # synchronous (an await in the handler's `finally` would raise
            # CancelledError on a torn-down task and skip session.close(),
            # leaking the provider connection — see its comment). Recording
            # the last call would measure that instead of the handshake
            # charge this file is about.
            seen.setdefault(name, threading.get_ident())
            return result() if callable(result) else result
        return _fn

    monkeypatch.setattr(
        live_ep, "_live_config_for",
        _record("_live_config_for", lambda: LiveVoiceConfig(
            enabled=True, model_id="m-1", provider="fake", handshake_charge_sec=1,
        )),
    )
    monkeypatch.setattr(
        live_ep, "check_audio_budget", _record("check_audio_budget", None)
    )
    monkeypatch.setattr(
        live_ep, "build_agent_for_kind",
        _record("build_agent_for_kind", lambda: (object(), object())),
    )
    monkeypatch.setattr(
        live_ep, "resolve_persona_prompt",
        _record("resolve_persona_prompt", "You are a voice agent."),
    )
    monkeypatch.setattr(
        live_ep, "reserve_audio_seconds", _record("reserve_audio_seconds", 1)
    )

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)

    app = FastAPI()
    app.include_router(live_ep.router)
    client = TestClient(app)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # `ready` — the whole handshake completed
    return seen


def test_the_anchor_really_ran_on_the_loop(threads_seen):
    """Guards the measurement itself: if the anchor stopped running inline,
    every assertion below would compare against a missing value and could
    pass vacuously."""
    assert "loop" in threads_seen, (
        "the on-loop anchor never ran — the rest of this file measures "
        "nothing until it does"
    )


@pytest.mark.parametrize("call_name", BLOCKING_CALLS)
def test_each_blocking_handshake_call_runs_off_the_event_loop(
    threads_seen, call_name
):
    assert call_name in threads_seen, f"{call_name} never ran in the handshake"
    assert threads_seen[call_name] != threads_seen["loop"], (
        f"{call_name} ran on the event loop's own thread. supabase-py is "
        f"synchronous, so for the length of that round trip this replica "
        f"serves nobody: other callers' handshakes queue behind it and the "
        f"audio pumps of calls already in progress stop. This is the "
        f"company.example 2026-09-03 failure — a ~14s handshake against a 12s "
        f"browser deadline, on an engine that was working fine."
    )
