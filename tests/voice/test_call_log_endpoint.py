"""A call with a generic agent is saved as a conversation when it ends
(`WS /v1/agents/{slug}/voice`, voice/live/call_log.py), on the simulated
provider. See tests/voice/test_call_log.py for which turns are written.
"""
from __future__ import annotations

import threading
import time

import pytest

import endpoints.voice_live as live_ep
from tests.voice.test_custom_agent_voice import ANSWER, _open, _until, stack  # noqa: F401
from voice.live import call_log as call_log_mod


@pytest.fixture
def saved(monkeypatch):
    """What the calls of a test wrote: (identity, rows, thread id)."""
    calls = []
    monkeypatch.setattr(
        call_log_mod, "_write",
        lambda identity, rows: calls.append((identity, rows, threading.get_ident())))
    return calls


def _written(saved, n=1):
    """The write is handed to a worker thread as the call ends."""
    deadline = time.monotonic() + 10
    while len(saved) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(saved) == n, saved
    return saved


def _greeting(ws):
    _open(ws)
    frame, _ = _until(ws, lambda f: f["type"] == "transcript" and f["role"] == "assistant")
    _until(ws, lambda f: f["type"] == "turn_end")
    return frame["text"]


def test_a_call_with_no_lookup_is_still_saved(stack, saved):  # noqa: F811
    """The caller hears the greeting and hangs up: no delegated turn ran, so
    nothing else writes this conversation."""
    client, state = stack
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        greeting = _greeting(ws)
    assert state["identities"] == []          # no delegated turn
    (identity, rows, _thread), = _written(saved)
    assert identity.agent_kind == "custom"
    assert identity.session_metadata["agent_slug"] == "helpdesk"
    assert identity.session_metadata["channel"] == "voice"
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", greeting)]
    assert rows[0]["metadata"]["voice"]["spoken"] is True


def test_a_delegated_turn_is_not_saved_a_second_time(stack, saved):  # noqa: F811
    client, state = stack
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        greeting = _greeting(ws)
        ws.send_json({"type": "text", "text": "What are your opening hours?"})
        _until(ws, lambda f: f["type"] == "transcript" and f["role"] == "assistant"
               and not f.get("out_of_band") and f["text"] == ANSWER)
        _until(ws, lambda f: f["type"] == "turn_end")
    # `run_turn` saved the question and the answer under the same session.
    (identity, rows, _thread), = _written(saved)
    assert identity.session_id == state["identities"][-1].session_id
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", greeting)]


def test_the_transcript_is_written_off_the_event_loop(stack, saved, monkeypatch):  # noqa: F811
    """Thread identity against a function the handler calls inline on the
    loop (see tests/voice/test_voice_handshake_off_loop.py for why a timing
    test cannot show this)."""
    client, _state = stack
    loop_thread = {}
    monkeypatch.setattr(live_ep, "enforce_channel_kind",
                        lambda conn, kind: loop_thread.setdefault("id", threading.get_ident()))
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        _greeting(ws)
    assert _written(saved)[0][2] != loop_thread["id"]


def test_a_database_failure_does_not_break_the_call(stack, monkeypatch):  # noqa: F811
    client, _state = stack

    def _boom(identity, rows):
        raise RuntimeError("the database is away")

    monkeypatch.setattr(call_log_mod, "_write", _boom)
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        _greeting(ws)
        ws.send_json({"type": "text", "text": "What are your opening hours?"})
        spoken, _ = _until(ws, lambda f: f["type"] == "transcript" and f["role"] == "assistant"
                           and not f.get("out_of_band"))
    assert spoken["text"] == ANSWER
    # And the next call is served.
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        assert _greeting(ws)
