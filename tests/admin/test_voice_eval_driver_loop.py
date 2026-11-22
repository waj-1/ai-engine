"""run_voice_eval_scenario against the real WS endpoint + simulated provider.

Spins an in-process uvicorn on an ephemeral port; all external seams
(_live_config_for, auth, budget, agent build, delegation) are monkeypatched
exactly like tests/voice/test_voice_live_endpoint.py's harness.
"""
import asyncio
import threading
import time
import types

import pytest
import uvicorn
from fastapi import FastAPI

import endpoints.voice_live as live_ep
from admin.voice_eval_driver import run_voice_eval_scenario
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult
from voice.live.simulated import SimulatedRealtimeSession


class FakeCallerLLM:
    """Scripted caller: .invoke returns the next canned reply."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else "[HANGUP]"
        return types.SimpleNamespace(content=text)


@pytest.fixture()
def live_server(monkeypatch):
    # Same rate-limiter reset precedent as tests/voice/test_voice_live_endpoint.py's
    # `harness` fixture — every test here connects from the same loopback peer,
    # and without a reset the file's own connection count eventually trips the
    # 10/60s budget and a later test gets closed with CLOSE_RATE_LIMITED.
    live_ep._connect_limiter._store.clear()

    cfg = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="simulated",
        greeting_trigger="Greet the caller.",
    )

    monkeypatch.setattr(live_ep, "_live_config_for", lambda _conn, _kind: cfg)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("tok", "user-1"))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(live_ep, "build_agent_for_kind", lambda kind, params: (object(), object()))
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "You are a voice agent.")
    monkeypatch.setattr(
        live_ep, "_session_for",
        lambda cfg, **kw: SimulatedRealtimeSession(cfg, persona=kw.get("persona", "")),
    )
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: True)

    async def _fake_run_delegated_turn(*, message, **kwargs):
        return DelegateResult(
            spoken=f"You said: {message}", display=f"You said: {message}",
            charts=[], error=False, streamed=[], flow_signals=[],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_run_delegated_turn)

    app = FastAPI()
    app.include_router(live_ep.router)
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
    yield f"ws://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


SCENARIO = {
    "id": "s1",
    "caller": {"persona": "p", "goal": "ask about sales, then hang up",
               "facts": {}, "max_turns": 5},
    "locale": "en",
}
VOICE_CTX = {"mode": "sim", "agent_kind": "orchard", "ws_path": "/voice/live"}


async def test_happy_two_turn_conversation(live_server):
    llm = FakeCallerLLM(["What were my sales yesterday?", "[HANGUP]"])
    out = await run_voice_eval_scenario(
        SCENARIO, VOICE_CTX, caller_llm=llm, target=live_server,
    )
    assert out["status"] == "completed"
    assert out["termination"] == "hangup"
    roles = [t["role"] for t in out["turns"]]
    texts = [t["text"] for t in out["turns"]]
    # greeting, caller utterance, delegated answer
    assert roles[0] == "assistant"
    assert "What were my sales yesterday?" in texts
    assert any("You said: What were my sales yesterday?" in t for t in texts)
    assert len(out["per_turn"]) == 1
    assert out["per_turn"][0]["latency_ms"] >= 0


async def test_holding_line_then_answer_splits_the_two_latencies(live_server, monkeypatch):
    """The metric fix this branch exists for: on a tool turn the FIRST
    assistant transcript is usually the out-of-band holding line the endpoint
    speaks at `progress_after_sec` — so the old single `latency_ms` pegged
    there no matter how slow the real answer was. The driver must clock them
    separately: `first_reply_ms` (any transcript — the holding line here) and
    `answer_ms` (first NON-out-of-band transcript — the delegate's answer).

    Discriminator (verified by reverting): drop the `out_of_band=True` marker
    from SimulatedRealtimeSession.speak_out_of_band and this test fails with
    `answer_ms` clocking the ~0.3s holding line instead of the ~1.2s answer,
    and `holding_line` False — exactly the old blindness.
    """
    cfg = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="simulated",
        greeting_trigger="Greet the caller.",
        # Holding line well before the delegate returns, as on the live
        # stack (2.5s progress vs ~5-7s p50 answer) — just faster.
        progress_after_sec=0.3,
    )
    monkeypatch.setattr(live_ep, "_live_config_for", lambda _conn, _kind: cfg)

    async def _slow_delegated_turn(*, message, **kwargs):
        await asyncio.sleep(1.2)
        return DelegateResult(
            spoken=f"You said: {message}", display=f"You said: {message}",
            charts=[], error=False, streamed=[], flow_signals=[],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegated_turn)

    llm = FakeCallerLLM(["What were my sales yesterday?", "[HANGUP]"])
    out = await run_voice_eval_scenario(
        SCENARIO, VOICE_CTX, caller_llm=llm, target=live_server,
    )
    assert out["status"] == "completed"
    (turn,) = out["per_turn"]
    # The holding line is the first reply, and is recorded as such...
    assert turn["holding_line"] is True
    assert turn["first_reply_ms"] is not None
    assert turn["latency_ms"] == turn["first_reply_ms"]  # legacy alias
    # ...while the ANSWER is measured at the delegate's real pace. The two
    # must be far apart (0.3s holding vs 1.2s delegate; generous margins so
    # a loaded CI box cannot blur them together).
    assert turn["answer_ms"] is not None
    assert turn["answer_ms"] >= 1100
    assert turn["answer_ms"] - turn["first_reply_ms"] >= 500
    # The caller still HEARD the holding line — it stays in the judged
    # transcript; only the latency accounting tells it apart.
    texts = [t["text"] for t in out["turns"]]
    assert any("Still working on that" in t for t in texts)
    assert any("You said: What were my sales yesterday?" in t for t in texts)


async def test_turn_without_holding_line_has_equal_latencies(live_server):
    """Fast delegate, no holding line: `answer_ms` and `first_reply_ms` are
    the same frame, and `holding_line` stays False."""
    llm = FakeCallerLLM(["What were my sales yesterday?", "[HANGUP]"])
    out = await run_voice_eval_scenario(
        SCENARIO, VOICE_CTX, caller_llm=llm, target=live_server,
    )
    assert out["status"] == "completed"
    (turn,) = out["per_turn"]
    assert turn["holding_line"] is False
    assert turn["answer_ms"] == turn["first_reply_ms"] == turn["latency_ms"]
    assert turn["answer_ms"] is not None


async def test_turn_cap_terminates(live_server):
    llm = FakeCallerLLM(["q1", "q2", "q3", "q4", "q5", "q6", "q7"])
    scenario = {**SCENARIO, "caller": {**SCENARIO["caller"], "max_turns": 2}}
    out = await run_voice_eval_scenario(
        scenario, VOICE_CTX, caller_llm=llm, target=live_server,
    )
    assert out["status"] == "completed"
    assert out["termination"] == "turn_cap"
    assert len([t for t in out["turns"] if t["role"] == "caller"]) == 2


async def test_fixed_opening_skips_llm_for_first_turn(live_server):
    llm = FakeCallerLLM(["[HANGUP]"])
    scenario = {**SCENARIO, "caller": {**SCENARIO["caller"],
                                       "opening": "Hi, I need help with my terminal."}}
    out = await run_voice_eval_scenario(
        scenario, VOICE_CTX, caller_llm=llm, target=live_server,
    )
    assert out["status"] == "completed"
    caller_turns = [t["text"] for t in out["turns"] if t["role"] == "caller"]
    assert caller_turns[0] == "Hi, I need help with my terminal."
    # The LLM was consulted only for the SECOND turn (which hung up).
    assert len(llm.calls) == 1


async def test_malformed_max_turns_does_not_raise(live_server):
    # "never raises" contract: a scenario with a non-numeric max_turns must
    # not blow up int(...) before the try/except-guarded WS session even
    # opens -- it should fall back to the default turn cap and complete
    # normally.
    llm = FakeCallerLLM(["[HANGUP]"])
    scenario = {**SCENARIO, "caller": {**SCENARIO["caller"], "max_turns": "five"}}
    out = await run_voice_eval_scenario(
        scenario, VOICE_CTX, caller_llm=llm, target=live_server,
    )
    assert isinstance(out, dict)
    assert out["status"] in ("completed", "error")


async def test_sim_mode_aborts_when_gate_denies_the_override(live_server, monkeypatch):
    # Finding 4: by design the endpoint silently falls back to the real
    # provider when the sim gate denies (VOICE_LIVE_SIM_ENABLED unset, or
    # the connection isn't recognized as loopback) -- so without this
    # check, a "sim" eval run would silently open a real, billable
    # provider session. Deny the gate here (same effect as the flag being
    # off / the peer not being loopback) and assert the driver aborts
    # before any caller turn or LLM call, instead of driving a real
    # session for up to the full turn deadline.
    monkeypatch.setattr(live_ep, "_sim_override_allowed", lambda ws: False)
    llm = FakeCallerLLM(["What were my sales yesterday?", "[HANGUP]"])
    out = await run_voice_eval_scenario(
        SCENARIO, VOICE_CTX, caller_llm=llm, target=live_server,
    )
    assert out["status"] == "error"
    assert out["termination"] == "ws_closed"
    assert "simulated provider" in (out["reason"] or "")
    assert out["turns"] == []
    assert llm.calls == []


async def test_unreachable_target_is_error_not_raise():
    llm = FakeCallerLLM([])
    out = await run_voice_eval_scenario(
        SCENARIO, VOICE_CTX, caller_llm=llm, target="ws://127.0.0.1:1",
    )
    assert out["status"] == "error"
    assert out["reason"]
