"""The eval driver must be the client the engine is written against.

The 2026-08-06 mid-word truncation lived on the
flush -> `played` -> `conversation.item.truncate` path, and that path starts
in the BROWSER (`static/shared/voice-live.js` reports its playback offset on
every flush). A driver that swallows audio at wire speed and never reports
playback cannot reach it — so the entire defect class was unreachable from a
voice eval, live mode included.

These tests pin the client contract with a stub voice edge: no engine, no
provider, just the frames a real one sends.
"""
import asyncio
import json
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket

from admin.voice_eval_driver import run_voice_eval_scenario

# 24kHz mono pcm16: 48 bytes per ms.
_ONE_SECOND = b"\x00\x00" * 24000


@pytest.fixture()
def stub_edge():
    """A /voice/live-shaped server that speaks a greeting, streams 1s of
    audio far faster than realtime (as the real service does), then flushes."""
    received: list = []
    app = FastAPI()

    @app.websocket("/voice/live")
    async def _edge(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"type": "ready", "provider": "simulated",
                            "audio": {"encoding": "pcm16",
                                      "input_sample_rate": 24000,
                                      "output_sample_rate": 24000}})
        await ws.send_json({"type": "transcript", "role": "assistant",
                            "text": "Hello there.", "final": True})
        await ws.send_bytes(_ONE_SECOND)
        # Barely any of that second can have played yet.
        await asyncio.sleep(0.05)
        await ws.send_json({"type": "interrupted"})
        await ws.send_json({"type": "turn_end"})
        try:
            async with asyncio.timeout(3):
                while True:
                    received.append(json.loads(await ws.receive_text()))
        except (Exception, asyncio.TimeoutError):
            pass

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}", received
    server.should_exit = True
    thread.join(timeout=5)


class _NoCallerLLM:
    def invoke(self, messages):  # pragma: no cover - never reached
        raise AssertionError("the caller must not speak in this test")


async def test_driver_reports_playback_on_a_flush_like_the_browser(stub_edge):
    target, received = stub_edge
    scenario = {"id": "s", "caller": {"persona": "p", "goal": "g",
                                      "max_turns": 0}, "locale": "en"}
    await run_voice_eval_scenario(
        scenario, {"mode": "sim", "agent_kind": "orchard",
                   "ws_path": "/voice/live"},
        caller_llm=_NoCallerLLM(), target=target, timeout_sec=20,
    )
    played = [f for f in received if f.get("type") == "played"]
    assert played, "no `played` frame — the truncate path is unreachable"
    # Received a full second, played ~50ms of it: the engine must be told the
    # small number, not the big one. Reporting the whole second is what makes
    # the server believe the caller heard everything.
    assert 0 <= played[0]["ms"] < 400, played[0]
