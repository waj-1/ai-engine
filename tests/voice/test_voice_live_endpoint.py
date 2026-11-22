import asyncio
import asyncio
import logging
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat, RealtimeEvent, ToolResultMode
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult
from voice.live.protocol import CLOSE_FORBIDDEN, CLOSE_RATE_LIMITED
from voice.live.testing import FakeRealtimeSession
from tests.scoped_build import full_build, make_scope, use_scope

_REAL_BUILD_AGENT_FOR_KIND = live_ep.build_agent_for_kind
_REAL_RESOLVE_PERSONA = live_ep.resolve_persona_prompt


@pytest.fixture
def harness(monkeypatch):
    state = {}

    # `_connect_limiter` is a module-level SlidingWindowLimiter shared across
    # every test in this file, and every test here connects with the same
    # mocked rate_key ("user-1"). Without a reset, the file's own connection
    # count eventually trips the 10/60s budget and a later test gets closed
    # with CLOSE_RATE_LIMITED instead of exercising what it's actually
    # testing -- same precedent as `reset_rate_limiters` in tests/conftest.py.
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
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    state["session"] = session

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state



def _read_until(ws, kind, limit=8):
    """Read frames until `kind` arrives. The exact frame SEQUENCE is not part
    of the contract — the holding line now emits its own transcript frame, and
    a test that counted frames closed the socket mid-turn and cancelled the
    delegate it was measuring."""
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")

def test_ready_frame_declares_the_audio_format(harness):
    client, state = harness
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ready = ws.receive_json()
    assert ready["type"] == "ready"
    assert ready["audio"]["input_sample_rate"] == 16000
    assert ready["audio"]["output_sample_rate"] == 24000


def test_the_handshake_forwards_the_configured_voice_profile_slug(harness, monkeypatch):
    """`voice_config.live.agent_profile_slug` must reach the agent build.

    The whole point of the key is that a call is served by the voice-optimized
    profile; resolving it into `cfg` and then not passing it on would look
    configured while serving the text agent — a silent no-op nobody notices
    until a caller gets a markdown table read out loud.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", agent_profile_slug="sample_voice_specialist",
    )
    seen = {}
    monkeypatch.setattr(
        live_ep, "build_agent_for_kind",
        lambda kind, params: (seen.update(params) or (object(), object())),
    )

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"

    assert seen["profile_slug"] == "sample_voice_specialist"


def test_no_configured_slug_forwards_none(harness, monkeypatch):
    """Unset stays unset — the params key exists but selects nothing, so the
    agent resolves its kind's active profile exactly as before."""
    client, state = harness
    seen = {}
    monkeypatch.setattr(
        live_ep, "build_agent_for_kind",
        lambda kind, params: (seen.update(params) or (object(), object())),
    )

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()

    assert seen["profile_slug"] is None


def test_binary_frames_reach_the_provider(harness):
    client, state = harness
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()               # ready
        ws.send_bytes(b"\x01\x02")
        ws.send_json({"type": "mute"})  # round-trip to let the pump run
    assert b"\x01\x02" in state["session"].sent_audio


def test_provider_audio_is_forwarded_as_binary(harness):
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=b"SPEECH"))
        assert ws.receive_bytes() == b"SPEECH"


def test_transcripts_are_forwarded(harness):
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="user", text="hi", final=True))
        frame = ws.receive_json()
    assert frame == {"type": "transcript", "role": "user", "text": "hi", "final": True}


def test_tool_call_delegates_and_answers_with_interrupt(harness, monkeypatch):
    client, state = harness
    session = state["session"]

    async def _fake_delegate(**kwargs):
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(
            RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                          arguments={"query": "net sales?"})
        )
        display = ws.receive_json()

    assert display == {"type": "display", "text": "AED 900"}
    assert session.tool_results == [("c1", {"spoken": "Nine hundred."}, ToolResultMode.INTERRUPT)]


def test_charts_are_forwarded_to_the_transcript_pane(harness, monkeypatch):
    client, state = harness
    session = state["session"]
    chart = {"type": "chart", "chart_id": "c1", "title": "Net", "caption": "", "spec": {"mark": "bar"}}

    async def _fake_delegate(**kwargs):
        return DelegateResult(spoken="See screen.", display="See screen.", charts=[chart])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        frames = [ws.receive_json(), ws.receive_json()]

    assert chart in frames


def test_scribe_extraction_turn_suppresses_the_display_frame(harness, monkeypatch):
    """text_only + proposals: the operator's payload is the proposal cards —
    the delegate's prose only narrates work already visible on screen, and
    posting it after every dictated sentence is the reply-to-every-utterance
    chatter the scribe mode exists to avoid."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", text_only=True
    )
    session = state["session"]
    card = {"type": "proposal", "kind": "order", "payload": {"item": "router upgrade"}}

    async def _extraction(**kwargs):
        return DelegateResult(
            spoken="Queued.", display="I queued the router upgrade for review.",
            charts=[], proposals=[card],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _extraction)

    # agentKind=sample_voice: a kind with no first-frame auth handshake, which is
    # not what this test is about (same choice as
    # tests/voice/test_voice_live_client_context.py) — the behaviour under
    # test is keyed on cfg.text_only alone.
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "router upgrade, two units"}))
        # The display frame is sent BEFORE the proposals by the same
        # coroutine, so the card arriving first proves the prose was
        # suppressed, not merely reordered.
        first = ws.receive_json()
        # And nothing trails the card either: a sentinel transcript is the
        # very next frame.
        session.push(RealtimeEvent(type="transcript", role="user", text="ping", final=True))
        second = ws.receive_json()

    assert first == card
    assert second["type"] == "transcript"


def test_scribe_turn_without_proposals_keeps_its_display_frame(harness, monkeypatch):
    """text_only + NO proposals is a genuine question/answer — the display
    frame is the answer's only delivery in this mode (nothing is spoken), so
    suppressing it here would make real answers vanish."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", text_only=True
    )
    session = state["session"]

    async def _answer(**kwargs):
        return DelegateResult(spoken="On screen.", display="Your last bill was 42.50.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _answer)

    # sample_voice, for the same auth-handshake reason as above.
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "last bill?"}))
        frame = ws.receive_json()

    assert frame == {"type": "display", "text": "Your last bill was 42.50."}


def test_audio_mode_keeps_the_display_frame_even_with_proposals(harness, monkeypatch):
    """The suppression is scoped to text_only — the phone agents' screens
    must stay byte-identical, proposals or not."""
    client, state = harness
    session = state["session"]
    card = {"type": "proposal", "kind": "order", "payload": {"item": "modem"}}

    async def _extraction(**kwargs):
        return DelegateResult(
            spoken="Queued.", display="Queued a modem for review.",
            charts=[], proposals=[card],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _extraction)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "order a modem"}))
        frames = [ws.receive_json(), ws.receive_json()]

    assert {"type": "display", "text": "Queued a modem for review."} in frames
    assert card in frames


def test_barge_in_during_delegation_delivers_silently(harness, monkeypatch):
    import asyncio

    client, state = harness
    session = state["session"]

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.05)
        return DelegateResult(spoken="Late answer.", display="Late answer.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        session.push(RealtimeEvent(type="interrupted"))
        # A REAL barge-in: the caller's overriding utterance is TRANSCRIBED as
        # real words. The commit alone (`user_turn_end`) is not the
        # confirmation — background noise commits too — so the substantive
        # transcript is what proves the caller took the floor. See
        # test_unconfirmed_interrupt_recovers_the_spoken_answer for the noise
        # half.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no wait, cancel that", final=True))
        flush = _read_until(ws, "interrupted")   # confirmation flush
        assert flush.get("reason") == "barge_in"
        _read_until(ws, "display")       # once the slow delegate lands

    assert session.tool_results == [("c1", {"spoken": "Late answer."}, ToolResultMode.SILENT)]


def test_disabled_tenant_is_closed_with_4403(harness):
    client, state = harness
    state["cfg"] = LiveVoiceConfig(enabled=False)
    with pytest.raises(Exception):
        with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
            ws.receive_json()


def test_exhausted_budget_is_closed_with_4429(harness, monkeypatch):
    from voice.live.budget import BudgetExhausted

    client, state = harness

    def _boom(*a, **kw):
        raise BudgetExhausted("spent")

    monkeypatch.setattr(live_ep, "reserve_audio_seconds", _boom)
    with pytest.raises(Exception):
        with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
            ws.receive_json()


def test_second_tool_call_does_not_re_invert_the_first_barge_in(harness, monkeypatch):
    """[tool_call A, interrupted, tool_call B]: A is the call the caller
    actually barged in on and must stay SILENT even though B's dispatch
    happens after the interruption. A single shared boolean reset at B's
    dispatch time used to flip A back to INTERRUPT retroactively — the
    monotonic epoch in `_handle_tool_call` must not have that bug."""
    import asyncio as _a

    client, state = harness
    session = state["session"]
    released = {"go": None}

    async def _fake_delegate(**kwargs):
        message = kwargs.get("message")
        if message == "first":
            # Held until the test has SEEN the interrupted frame: an instant
            # return can beat the pump's processing of the pushed interrupt
            # (a real delegate takes seconds), and call A would then deliver
            # before the epoch ever bumped.
            await released["go"].wait()
            return DelegateResult(spoken="First answer.", display="First answer.", charts=[])
        return DelegateResult(spoken="Second answer.", display="Second answer.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        released["go"] = _a.Event()
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="a", tool_name="ask_agent",
                                   arguments={"query": "first"}))
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="tool_call", call_id="b", tool_name="ask_agent",
                                   arguments={"query": "second"}))
        _read_until(ws, "interrupted")
        released["go"].set()
        _frames_until_turn_end(ws, session, results=2)

    modes = {call_id: mode for call_id, _payload, mode in session.tool_results}
    assert modes["a"] == ToolResultMode.SILENT
    assert modes["b"] == ToolResultMode.INTERRUPT


def test_agent_kind_gated_against_the_active_scope(harness, monkeypatch):
    """On one scope's build, ?agentKind=<another scope's kind> must be refused
    -- otherwise the WebSocket surface defeats the exact cross-scope isolation
    the HTTP deployment gate enforces on every other endpoint."""
    from tests.scoped_build import make_scope

    client, state = harness
    monkeypatch.setattr(live_ep, "active_scope", lambda: make_scope("sample_voice"))

    with pytest.raises(Exception):
        with client.websocket_connect("/voice/live?agentKind=other_scope&businessId=b1") as ws:
            ws.receive_json()


def test_agent_kind_allowed_for_the_active_scope_itself(harness, monkeypatch):
    """Sanity check for the fixture above: the active scope's OWN kind must
    still work, so the gate isn't accidentally refusing everything."""
    from tests.scoped_build import make_scope

    client, state = harness
    monkeypatch.setattr(live_ep, "active_scope", lambda: make_scope("sample_voice"))

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ready = ws.receive_json()
    assert ready["type"] == "ready"


async def _drive_raw_asgi_websocket(app, path: str, query_string: str):
    """Drive `app` as a raw ASGI websocket connection, bypassing Starlette's
    `TestClient`/`WebSocketTestSession` entirely.

    Why this exists: `WebSocketTestSession.__exit__` force-cancels the
    server-side ASGI task the moment the `with` block ends, regardless of
    what the handler's own code does. That makes `TestClient` unusable for
    proving the asyncio.gather-deadlock fix (CRITICAL 1/2): a test built on
    it can't distinguish "the handler cooperatively tore itself down when
    the client disconnected" from "the test harness just killed the task
    out from under it" -- and the bug this guards against is exactly a case
    where the handler's own code does NOT tear itself down on its own.

    Returns (app_task, send_queue, push_disconnect) — `push_disconnect()`
    enqueues a `websocket.disconnect` receive event (the one thing a real
    client disconnecting actually causes) without ever touching `app_task`.
    """
    send_queue: asyncio.Queue = asyncio.Queue()
    receive_queue: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await receive_queue.get()

    async def send(message):
        await send_queue.put(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string.encode(),
        "root_path": "",
        "headers": [],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "subprotocols": [],
        "state": {},
    }

    receive_queue.put_nowait({"type": "websocket.connect"})
    app_task = asyncio.create_task(app(scope, receive, send))

    accept_msg = await asyncio.wait_for(send_queue.get(), timeout=2.0)
    assert accept_msg["type"] == "websocket.accept", accept_msg

    async def push_disconnect():
        await receive_queue.put({"type": "websocket.disconnect", "code": 1000})

    return app_task, send_queue, push_disconnect


async def test_client_disconnect_does_not_orphan_the_provider_pump(harness):
    """Regression for the asyncio.gather deadlock: `gather` (default
    `return_exceptions=False`) does not cancel a sibling task when one of
    two gathered coroutines finishes first. An ordinary client disconnect
    used to end `_pump_client_to_provider` immediately while
    `_pump_provider_to_client` stayed blocked in `session.events()` for the
    full `_MAX_SESSION_SECONDS` ceiling -- `session.close()` (in the
    handler's `finally`) never ran until then, so the provider connection
    (and its billing) leaked for the ceiling's whole duration on every
    closed tab. Uses the raw-ASGI driver above (not TestClient) because
    TestClient force-cancels the server task on its own, which would make
    this pass whether or not the handler's own logic tears itself down.

    No further events are pushed into `session` here on purpose: if the
    provider pump isn't promptly cancelled on disconnect, it blocks forever
    on the FakeRealtimeSession's empty queue and `session.close()` is never
    reached."""
    client, state = harness
    session = state["session"]

    app_task, send_queue, push_disconnect = await _drive_raw_asgi_websocket(
        client.app, "/voice/live", "agentKind=sample_voice&businessId=b1"
    )
    ready = await asyncio.wait_for(send_queue.get(), timeout=2.0)
    assert ready["type"] == "websocket.send"

    await push_disconnect()

    # The handler's OWN logic -- not any test-harness cancellation -- must
    # tear itself down promptly. Give it a generous but finite budget; the
    # bug this guards against would otherwise hang for _MAX_SESSION_SECONDS
    # (900s), so anything well under that proves the fix.
    await asyncio.wait_for(app_task, timeout=2.0)
    assert session.closed, (
        "provider session was not closed promptly after the client "
        "disconnected -- the provider pump was left orphaned"
    )
    assert "b1" not in live_ep._active, "the concurrency slot was left held after disconnect"


async def test_provider_pump_failure_does_not_orphan_the_client_pump(harness):
    """CRITICAL 2: `gather`'s default `return_exceptions=False` propagates
    the first pump's exception but does not cancel the other one. If
    `_pump_provider_to_client` raises (a real provider dropping mid-
    session), `_pump_client_to_provider` must not be left running forever
    waiting on a client disconnect that, in a real hung/half-open
    connection, may never come. No disconnect is ever pushed in this test
    on purpose -- the client pump is just sitting in `websocket.receive()`,
    same as a live browser leaving the mic open; the handler must still
    notice the OTHER pump failed and tear the whole session down."""
    from voice.live.base import RealtimeProviderError

    client, state = harness
    session = state["session"]

    async def _boom_events():
        raise RealtimeProviderError("provider dropped")
        yield  # pragma: no cover - unreachable; keeps this an async generator

    session.events = _boom_events

    app_task, send_queue, push_disconnect = await _drive_raw_asgi_websocket(
        client.app, "/voice/live", "agentKind=sample_voice&businessId=b1"
    )
    ready = await asyncio.wait_for(send_queue.get(), timeout=2.0)
    assert ready["type"] == "websocket.send"

    await asyncio.wait_for(app_task, timeout=2.0)
    assert session.closed, "provider session was not closed after the provider pump failed"

    # `gather()` itself returns/raises as soon as the FIRST awaitable fails
    # -- it does not wait for or cancel the other one. So `app_task`
    # completing is not, on its own, proof the client pump was torn down;
    # without the explicit cancel-and-await fix, `_pump_client_to_provider`
    # keeps running in the background (blocked on `websocket.receive()`)
    # even after the handler above has already returned and closed
    # everything. Give the loop a couple of spins, then check directly.
    for _ in range(3):
        await asyncio.sleep(0)
    leaked = [
        t for t in asyncio.all_tasks()
        if not t.done() and "_pump_client_to_provider" in t.get_coro().__qualname__
    ]
    assert not leaked, "the client pump was left running after the provider pump failed"


def test_tool_call_failure_still_sends_a_tool_result(harness, monkeypatch):
    """A crash inside run_delegated_turn must not leave the realtime
    provider waiting forever for a function response to this call_id --
    _handle_tool_call is fire-and-forget, so nothing else will ever call
    send_tool_result for it if the body isn't guarded."""
    import time

    from voice.live.delegate import FAILURE_SPOKEN

    client, state = harness
    session = state["session"]

    async def _boom(**kwargs):
        raise RuntimeError("agent blew up")

    monkeypatch.setattr(live_ep, "run_delegated_turn", _boom)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        deadline = time.monotonic() + 2.0
        while not session.tool_results and time.monotonic() < deadline:
            time.sleep(0.02)

    assert session.tool_results == [("c1", {"spoken": FAILURE_SPOKEN}, ToolResultMode.INTERRUPT)]


def test_config_resolution_failure_is_closed_with_provider_error(harness, monkeypatch):
    """A non-LookupError failure in _live_config_for (e.g. a ValueError from
    get_service_client() when AI Supabase env vars are missing) must not
    escape the handler unguarded -- it's a provider-side failure, not a
    forbidden-tenant one, and it happens right after accept() where an
    unguarded raise would drop the socket with no close frame at all."""
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    client, state = harness

    def _boom(_conn, _kind):
        raise ValueError("AI_SUPABASE_URL is not set")

    monkeypatch.setattr(live_ep, "_live_config_for", _boom)

    with pytest.raises(WSDisconnect) as exc_info:
        with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
            ws.receive_json()
    assert exc_info.value.code == live_ep.CLOSE_PROVIDER_ERROR


def test_persona_resolution_db_failure_is_closed_with_provider_error(harness, monkeypatch):
    """Review finding MINOR 5: a transient DB/transport error while resolving
    the persona prompt (e.g. a Supabase blip, not a genuinely-missing slug)
    must not be treated as "misconfigured tenant" (CLOSE_FORBIDDEN) — only a
    real LookupError earns that. Mirrors the CLOSE_PROVIDER_ERROR split for
    `_live_config_for` just above this test."""
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    client, state = harness

    def _boom(cfg, kind, **kw):
        raise RuntimeError("network blip")

    monkeypatch.setattr(live_ep, "resolve_persona_prompt", _boom)

    with pytest.raises(WSDisconnect) as exc_info:
        with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
            ws.receive_json()
    assert exc_info.value.code == live_ep.CLOSE_PROVIDER_ERROR


def test_close_time_budget_error_does_not_skip_session_close(harness, monkeypatch):
    """A non-BudgetExhausted failure charging the close-time budget (e.g. a
    ValueError from get_service_client() when AI Supabase env vars are
    missing) must not skip session.close() in the finally -- that would
    leak the provider connection on every misconfigured-env deploy."""
    client, state = harness
    session = state["session"]
    calls = {"n": 0}

    def _reserve(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0  # the handshake reservation succeeds
        raise ValueError("AI Supabase env vars missing")

    monkeypatch.setattr(live_ep, "reserve_audio_seconds", _reserve)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready

    assert session.closed


def test_anonymous_session_still_gets_a_budget_key(harness, monkeypatch):
    """An anonymous kind (no businessId) -- reserve_audio_seconds must still
    be called with a real, non-empty key so the daily budget can actually
    meter it, rather than silently no-op through reserve_audio_seconds' own
    `if not business_id: return 0` guard."""
    client, state = harness
    seen = []

    def _reserve(business_id, seconds, **kw):
        seen.append(business_id)
        return 0

    monkeypatch.setattr(live_ep, "reserve_audio_seconds", _reserve)

    with client.websocket_connect("/voice/live?agentKind=sample_anonymous") as ws:  # no businessId
        ws.receive_json()

    assert seen, "reserve_audio_seconds was never called"
    assert all(seen), "an empty/falsy budget key was used for an anonymous session"


def test_turn_end_is_forwarded_so_the_client_can_close_its_bubble(harness):
    """The live Gemini API never sets Transcription.finished, so every
    transcript is non-final. turn_end is the only end-of-turn signal the
    client can use; if the endpoint swallowed it the transcript pane would
    merge every utterance of the session into one bubble."""
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user", text="hi", final=False))
        assert ws.receive_json()["final"] is False
        session.push(RealtimeEvent(type="turn_end"))
        assert ws.receive_json() == {"type": "turn_end"}


def test_turn_end_forwards_the_providers_reply_flag(harness):
    """The provider's `turn_reply` rides the turn_end frame as `reply`: true
    for the response answering the caller's speech, false for a greeting /
    continuation, and ABSENT when the provider cannot tell (legacy frame)."""
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="turn_end", turn_reply=False))  # greeting
        assert ws.receive_json() == {"type": "turn_end", "reply": False}
        session.push(RealtimeEvent(type="turn_end", turn_reply=True))  # a reply
        assert ws.receive_json() == {"type": "turn_end", "reply": True}
        session.push(RealtimeEvent(type="turn_end"))  # provider can't tell
        assert ws.receive_json() == {"type": "turn_end"}


def test_user_transcript_failed_closes_the_clients_user_transcript(harness):
    """A failed caller transcription reaches the client as an empty user
    final (closing any late-transcript window) — and is not run through the
    noise verdict, so it never cancels a reply."""
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="user_transcript_failed"))
        frame = ws.receive_json()
        assert frame["type"] == "transcript"
        assert (frame["role"], frame["text"], frame["final"]) == ("user", "", True)
        session.push(RealtimeEvent(type="turn_end"))
        assert ws.receive_json() == {"type": "turn_end"}


def test_greeting_trigger_is_sent_so_the_assistant_speaks_first(harness, monkeypatch):
    """A realtime session is silent until spoken to. Without this the caller
    opens the mic to dead air and concludes the feature is broken."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", greeting_trigger="Greet the caller."
    )
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
    assert state["session"].text_turns == ["Greet the caller."]


_BUSINESS_TRIGGER = ("The call has just connected — give your opening greeting now, in the call "
                     "language (Arabic: Levantine, e.g. «أهلين، معك وَج — شو بقدر أساعدك؟»): say "
                     "you are WAJ AI and ask what they need. One short sentence.")


def test_an_english_call_greets_without_the_arabic_example(harness):
    """User voice test 2026-10-03: an English app greeted in Arabic — the
    model recited the trigger's quoted Arabic example."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake",
                                   greeting_trigger=_BUSINESS_TRIGGER, language="ar-JO")
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1&locale=en") as ws:
        ws.receive_json()
    (sent,) = state["session"].text_turns
    assert "أهلين" not in sent and "«" not in sent
    assert "say you are WAJ AI" in sent.replace("\n", " ") and sent.endswith("Greet in English only.")


def test_an_arabic_call_keeps_the_levantine_example(harness):
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake",
                                   greeting_trigger=_BUSINESS_TRIGGER, language="ar-JO")
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1&locale=ar") as ws:
        ws.receive_json()
    (sent,) = state["session"].text_turns
    assert "أهلين" in sent and sent.endswith("Greet in Arabic, Levantine (Jordanian) dialect only.")


def test_no_greeting_trigger_means_no_text_turn(harness):
    client, state = harness
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
    assert state["session"].text_turns == []


def test_slow_delegation_speaks_a_holding_line_before_the_answer(harness, monkeypatch):
    """A delegated turn is ~10s even on flash-lite with tools bound. Without a
    holding line the caller sits in silence wondering if the call dropped."""
    import asyncio as _a
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]

    async def _slow(**kwargs):
        await _a.sleep(0.35)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")  # the answer; other frames may precede it

    # Out-of-band is PREFERRED when the provider offers it: the holding line
    # then costs nothing on the pending call id, so it can never eat the
    # answer (see the azure_voice_live one-result-per-call-id rule).
    assert session.out_of_band == ["Still working on that."], session.out_of_band
    modes = [(p["spoken"], m) for _cid, p, m in session.tool_results]
    assert ("Nine hundred.", ToolResultMode.INTERRUPT) in modes, modes


def test_holding_line_falls_back_to_a_tool_result_without_out_of_band(harness, monkeypatch):
    """A provider that takes several results per call id but has no
    out-of-band speech (gemini_live's shape) still gets a holding line — via
    the WHEN_IDLE tool-result path."""
    import asyncio as _a
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]
    session.supports_out_of_band_speech = False

    async def _slow(**kwargs):
        await _a.sleep(0.35)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        ws.receive_json()

    assert session.out_of_band == []
    modes = [(p["spoken"], m) for _cid, p, m in session.tool_results]
    assert ("Still working on that.", ToolResultMode.WHEN_IDLE) in modes, modes
    assert ("Nine hundred.", ToolResultMode.INTERRUPT) in modes, modes


def test_holding_line_spoken_out_of_band_when_results_are_single_use(harness, monkeypatch):
    """The azure_voice_live shape: ONE result per call id, but out-of-band
    speech available. The caller must still hear the holding line, and the
    answer must still arrive on its call id (the bug this pairing fixes was
    the holding line consuming the answer's only slot)."""
    import asyncio as _a
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]
    session.supports_streamed_tool_results = False
    session.supports_out_of_band_speech = True

    async def _slow(**kwargs):
        await _a.sleep(0.35)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")   # the answer; the holding line's own
                                     # transcript frame precedes it

    assert session.out_of_band == ["Still working on that."], session.out_of_band
    assert [(p["spoken"], m) for _cid, p, m in session.tool_results] == [
        ("Nine hundred.", ToolResultMode.INTERRUPT)
    ], session.tool_results


def test_no_holding_line_when_provider_lacks_streamed_tool_results(harness, monkeypatch):
    """Review finding: `_speak_progress` and the final answer both call
    send_tool_result on the SAME call_id. A provider that only accepts ONE
    result per call id (supports_streamed_tool_results=False) would have the
    holding line's WHEN_IDLE send succeed and then the final answer's send
    raise RealtimeProviderError -- the caller hears "Still working on that"
    and never gets the real answer. With NEITHER capability the progress task
    must not even be SCHEDULED; only the final answer, on its call_id, may be
    sent. (A provider with out-of-band speech instead gets the holding line
    that way -- see
    test_holding_line_spoken_out_of_band_when_results_are_single_use.)"""
    import asyncio as _a
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]
    session.supports_streamed_tool_results = False
    session.supports_out_of_band_speech = False

    async def _slow(**kwargs):
        await _a.sleep(0.35)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")  # the answer; other frames may precede it

    assert session.tool_results == [
        ("c1", {"spoken": "Nine hundred."}, ToolResultMode.INTERRUPT)
    ], session.tool_results


def test_fast_delegation_sends_no_holding_line(harness, monkeypatch):
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=5.0, progress_spoken="Still working on that.",
    )
    session = state["session"]

    async def _fast(**kwargs):
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fast)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        ws.receive_json()

    spoken = [p["spoken"] for _cid, p, _m in session.tool_results]
    assert "Still working on that." not in spoken, spoken


def test_streaming_speaks_each_sentence_and_does_not_repeat_the_answer(harness, monkeypatch):
    """Every sentence is delivered once, and the whole answer is NOT delivered
    again on top -- that would make the assistant repeat itself verbatim.

    `progress_after_sec=0` here, so no holding line was ever spoken: with
    nothing of ours on the floor the FIRST sentence goes WHEN_IDLE like the
    rest. INTERRUPT exists to displace a holding line, and firing it anyway
    cancelled the persona's own announce line mid-word -- the live "she
    interrupts herself" report. See the INTERRUPT case below."""
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0,
    )
    session = state["session"]

    async def _streaming(**kwargs):
        cb = kwargs.get("on_sentence")
        await cb("Sales were nine hundred.")
        await cb("Across twelve transactions.")
        return DelegateResult(
            spoken="Sales were nine hundred. Across twelve transactions.",
            display="AED 900", charts=[],
            streamed=["Sales were nine hundred.", "Across twelve transactions."],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _streaming)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        ws.receive_json()  # display frame

    sent = [(p["spoken"], m) for _c, p, m in session.tool_results]
    assert sent == [
        ("Sales were nine hundred.", ToolResultMode.WHEN_IDLE),
        ("Across twelve transactions.", ToolResultMode.WHEN_IDLE),
    ], sent


def _gemini_shaped(session):
    """The Gemini Live posture: several tool results per call id are accepted
    and SPOKEN, but the transcription of them is unusable as screen text."""
    session.supports_streamed_tool_results = True
    session.transcribes_streamed_tool_results = False
    return session


def _assistant_screen(frames):
    return [f["text"] for f in frames
            if f.get("type") == "transcript" and f.get("role") == "assistant"]


def _drain(ws, session, limit=24):
    """Every frame produced by the events pushed so far.

    `receive_json` BLOCKS, so there is no "read whatever is ready" here —
    the pump is asked to emit a sentinel it forwards verbatim, and everything
    ahead of that sentinel is the answer to the question being asked.
    """
    session.push(RealtimeEvent(type="error", message="__drained__"))
    out = []
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == "error" and frame.get("message") == "__drained__":
            return out
        out.append(frame)
    raise AssertionError(f"sentinel never arrived in {limit} frames: {out}")


def test_streamed_answer_screen_text_comes_from_the_server_not_the_provider(
    harness, monkeypatch,
):
    """The words the caller READS for a streamed answer are the ones we sent.

    Gemini Live speaks a streamed answer in full and transcribes only the
    leading fragment of each response — probed live 2026-08-19 with the exact
    answer from that morning's client call: delivered as its four chunker
    sentences, the transcript came back 'أبشر،' / 'ولا تشيل' / 'هل معك' /
    'ولا أفتحها'. The caller's words for it were "it speaks more than it
    writes". The endpoint therefore substitutes the sentence IT delivered for
    the provider's truncated echo of that sentence.

    Paced, not flushed: every sentence is delivered within the same
    millisecond but spoken over the following seconds, so a sentence goes out
    when the provider's transcription shows her STARTING it. That is what the
    truncated fragment is good for.
    """
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0,
    )
    session = _gemini_shaped(state["session"])

    async def _streaming(**kwargs):
        cb = kwargs.get("on_sentence")
        assert cb is not None, "streaming must stay ON — this is a screen fix"
        await cb("هل معك رقم الطرفية للمشكلة،")
        await cb("ولا أفتحها على أي فرع؟")
        return DelegateResult(
            spoken="هل معك رقم الطرفية للمشكلة، ولا أفتحها على أي فرع؟",
            display="…", charts=[],
            streamed=["هل معك رقم الطرفية للمشكلة،", "ولا أفتحها على أي فرع؟"],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _streaming)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        _read_until(ws, "display")   # both sentences are delivered by now
        # …and now the provider starts speaking them, truncated exactly as it
        # does live: the first two words of each response and nothing more.
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="هل معك", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="ولا أفتحها", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        screen = _assistant_screen(_drain(ws, session))

    assert screen == [
        "هل معك رقم الطرفية للمشكلة،",
        "ولا أفتحها على أي فرع؟",
    ], screen


def test_the_models_own_speech_still_comes_from_the_provider(harness, monkeypatch):
    """Only the delegated ANSWER switches source.

    The greeting, the announce line ("خلني أشوف لك") and any chit-chat are
    ordinary responses, not streamed tool results, and the provider transcribes
    those perfectly well — we have no copy of them and must not suppress them.
    The announce line in particular is still being transcribed when the answer
    is delivered, so this is the common interleaving, not an edge case.
    """
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0,
    )
    session = _gemini_shaped(state["session"])

    async def _streaming(**kwargs):
        await kwargs["on_sentence"]("ولا تشيل هم.")
        return DelegateResult(spoken="ولا تشيل هم.", display="…", charts=[],
                              streamed=["ولا تشيل هم."])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _streaming)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        _read_until(ws, "display")
        # The announce line's transcription is still arriving…
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text=" ثانية وحدة بس.", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        # …then the answer's own response starts.
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="ولا تشيل", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        screen = _assistant_screen(_drain(ws, session))

    assert screen == [" ثانية وحدة بس.", "ولا تشيل هم."], screen


def test_a_silent_sentence_is_never_put_on_screen(harness, monkeypatch):
    """A SILENT landing is context repair, not speech.

    Once the caller has interrupted, the rest of the abandoned answer is
    delivered SILENT so the model's history stays coherent — she never hears
    it. Showing it would put words in her mouth the recording does not
    contain, on the one screen whose entire promise is that it matches what
    she said.
    """
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0,
    )
    session = _gemini_shaped(state["session"])
    unheard = "وهذي الجملة اللي ما سمعتها."

    async def _streaming(**kwargs):
        cb = kwargs["on_sentence"]
        await cb("أبشر، هذي أول جملة.")
        # Detection plus the substantive transcript that confirms it — the
        # same barge-in shape the streamed-modes test above scripts.
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="لا لحظة، الغِ هذا", final=True))
        await asyncio.sleep(0.1)
        await cb(unheard)
        return DelegateResult(spoken="…", display="…", charts=[],
                              streamed=["أبشر، هذي أول جملة.", unheard])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _streaming)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        _read_until(ws, "display")
        # Whatever the provider says next, the unheard sentence must not be
        # what the screen expands it into.
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="وهذي الجملة", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        screen = _assistant_screen(_drain(ws, session))

    modes = {p["spoken"]: m for _c, p, m in session.tool_results}
    assert modes[unheard] is ToolResultMode.SILENT, modes
    assert unheard not in screen, (
        f"a sentence the caller never heard reached the screen: {screen}"
    )
    # The provider's own words still come through — this suppresses nothing,
    # it only declines to CLAIM the unspoken sentence.
    assert "وهذي الجملة" in screen, screen


def test_provider_transcription_is_kept_when_the_provider_can_be_trusted(
    harness, monkeypatch,
):
    """The other half of the flag, and the one that keeps it honest.

    A provider that transcribes streamed results properly is the BETTER
    source — it is the transcript of the audio, not of our intent — so
    nothing is substituted and nothing is suppressed there. Pinned together
    with the substituting case because one condition decides both, and a test
    of either alone passes with that condition inverted.
    """
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0,
    )
    session = state["session"]
    session.supports_streamed_tool_results = True
    session.transcribes_streamed_tool_results = True

    async def _streaming(**kwargs):
        await kwargs["on_sentence"]("هل معك رقم الطرفية للمشكلة،")
        return DelegateResult(spoken="هل معك رقم الطرفية للمشكلة،",
                              display="…", charts=[],
                              streamed=["هل معك رقم الطرفية للمشكلة،"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _streaming)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="هل معك", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        screen = _assistant_screen(_drain(ws, session))

    assert screen == ["هل معك"], screen


def test_first_sentence_interrupts_only_the_holding_line_it_displaces(harness, monkeypatch):
    """The other half of the rule: when a holding line WAS put on the floor,
    the answer's first sentence must cut it off — otherwise the caller waits
    out "still working on that" before hearing the answer it was masking.

    Pinning both halves together because the mode is decided by ONE condition
    and a test of either alone passes with that condition inverted."""
    import asyncio as _a
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]

    async def _slow_then_streams(**kwargs):
        cb = kwargs.get("on_sentence")
        await _a.sleep(0.3)          # long enough for the holding line to land
        await cb("Sales were nine hundred.")
        await cb("Across twelve transactions.")
        return DelegateResult(
            spoken="Sales were nine hundred. Across twelve transactions.",
            display="AED 900", charts=[],
            streamed=["Sales were nine hundred.", "Across twelve transactions."],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_then_streams)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        _frames_until_turn_end(ws, session, results=2)

    # The holding line itself went out-of-band on this session (it declares
    # supports_out_of_band_speech), so it is not among the tool results —
    # what matters is that having spoken it flips the answer's first sentence
    # to INTERRUPT.
    sent = [(p["spoken"], m) for _c, p, m in session.tool_results]
    assert sent == [
        ("Sales were nine hundred.", ToolResultMode.INTERRUPT),
        ("Across twelve transactions.", ToolResultMode.WHEN_IDLE),
    ], sent


def test_streaming_gated_off_when_provider_lacks_the_capability(harness, monkeypatch):
    """cfg.stream_sentences=True alone is not enough -- the session must ALSO
    declare a delivery capability: several tool results per call id, or
    out-of-band speech (the azure_voice_live path). A provider with NEITHER
    must fall back to whole-turn delivery regardless of the operator's
    config, or streaming would duplicate/lose sentences at runtime."""
    import time

    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0,
    )
    session = state["session"]
    session.supports_streamed_tool_results = False
    session.supports_out_of_band_speech = False

    async def _plain(**kwargs):
        assert kwargs.get("on_sentence") is None, "streaming must be opt-in"
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _plain)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        # Polling wait, not a second receive_json(): before the endpoint gate
        # is implemented, _plain's assertion fails INSIDE run_delegated_turn,
        # so the try block never reaches the display_frame send and a second
        # receive_json() would hang forever waiting for a message that never
        # arrives. _handle_tool_call's `finally` always sends a tool result
        # regardless (see test_tool_call_failure_still_sends_a_tool_result
        # above, same pattern), so poll that instead -- bounded, and correct
        # both before and after the fix.
        deadline = time.monotonic() + 2.0
        while not session.tool_results and time.monotonic() < deadline:
            time.sleep(0.02)

    assert [p["spoken"] for _c, p, _m in session.tool_results] == ["Nine hundred."]


def test_streaming_defaults_on_for_a_capable_provider(harness, monkeypatch):
    # stream_sentences now defaults ON (the measured 10s -> ~3s
    # time-to-first-speech win); a capable provider must get the
    # per-sentence callback without any operator opt-in.
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")
    session = state["session"]

    async def _expects_streaming(**kwargs):
        assert kwargs.get("on_sentence") is not None, "default must stream"
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _expects_streaming)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        ws.receive_json()

    assert [p["spoken"] for _c, p, _m in session.tool_results] == ["Nine hundred."]


def test_streaming_explicit_false_sends_one_whole_answer(harness, monkeypatch):
    # The operator opt-out: an explicit stream_sentences=false must keep
    # delivering the whole answer as a single tool result.
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=False,
    )
    session = state["session"]

    async def _plain(**kwargs):
        assert kwargs.get("on_sentence") is None, "explicit false must not stream"
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _plain)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        ws.receive_json()

    assert [p["spoken"] for _c, p, _m in session.tool_results] == ["Nine hundred."]


def test_barge_in_mid_stream_silences_remaining_sentences(harness, monkeypatch):
    """Review finding: streamed sentences ignored the barge-in epoch, so Q1's
    sentences audibly interrupted or queued behind the caller's NEW question.
    After a barge-in they must land SILENT (context only)."""
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0,
    )
    session = state["session"]

    async def _streams_around_a_barge_in(**kwargs):
        cb = kwargs.get("on_sentence")
        await cb("First sentence.")
        # Caller barges in while the turn is still streaming — detection
        # (`interrupted`) plus the substantive transcript that confirms it.
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no wait, cancel that", final=True))
        import asyncio as _a
        await _a.sleep(0.1)  # let the pump process the interrupt
        await cb("Second sentence.")
        return DelegateResult(
            spoken="First sentence. Second sentence.", display="d", charts=[],
            streamed=["First sentence.", "Second sentence."],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _streams_around_a_barge_in)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        # Sentinel-driven read: both sentences must have been delivered
        # before the socket closes (the first sentence's delivery-flush frame
        # made positional receive_json() counts lie).
        _frames_until_turn_end(ws, session, results=2)

    modes = {p["spoken"]: m for _c, p, m in session.tool_results}
    # WHEN_IDLE, not INTERRUPT: no holding line was spoken (progress_after_sec
    # is 0), so the first sentence has nothing of ours to displace.
    assert modes["First sentence."] == ToolResultMode.WHEN_IDLE
    assert modes["Second sentence."] == ToolResultMode.SILENT, modes


def test_holding_line_is_skipped_after_a_barge_in(harness, monkeypatch):
    """Review finding: the holding line ('still working on that') fired even
    after the caller had barged in and asked something else -- a spoken
    non-sequitur on the DEFAULT config path."""
    import asyncio as _a
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.15, progress_spoken="Still working on that.",
    )
    session = state["session"]

    async def _slow_with_barge_in(**kwargs):
        session.push(RealtimeEvent(type="interrupted"))   # barge-in at t~0
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no wait, cancel that", final=True))
        await _a.sleep(0.4)                                # holding line due at 0.15
        return DelegateResult(spoken="Late answer.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_with_barge_in)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        _read_until(ws, "interrupted")   # confirmation flush
        _read_until(ws, "display")

    spoken = [p["spoken"] for _c, p, _m in session.tool_results]
    assert "Still working on that." not in spoken, spoken


def test_holding_line_is_written_to_the_conversation_log(harness, monkeypatch):
    """The holding line is spoken AUDIO-ONLY (native models reject text+audio
    on a response), so it produces no transcript of its own. Without an
    explicit frame the log had a silent gap exactly where the caller heard
    "one moment" — the record has to show everything she said."""
    import asyncio as _a
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]

    async def _slow(**kwargs):
        await _a.sleep(0.35)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    frames = []
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        for _ in range(4):
            frames.append(ws.receive_json())
            if frames[-1].get("type") == "display":
                break
    spoken_log = [f for f in frames
                  if f.get("type") == "transcript" and f.get("role") == "assistant"]
    assert spoken_log, f"holding line not logged: {frames}"
    assert spoken_log[0]["text"] == "Still working on that."
    assert spoken_log[0]["final"] is True


def test_latency_metrics_spans_are_emitted(harness, monkeypatch):
    """The endpoint feeds every provider event to the collector, times the
    delegated turn, and emits turn/tool/session point spans — and the
    `user_turn_end` event exists ONLY for the collector: nothing is forwarded
    to the client for it (the first client frame after it is the audio)."""
    import voice.live.metrics as metrics_mod

    spans = []
    monkeypatch.setattr(
        metrics_mod, "_emit_span", lambda name, attrs: spans.append((name, attrs))
    )

    async def _fake_delegate(**kwargs):
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="audio", audio=b"A"))
        assert ws.receive_bytes() == b"A"  # nothing was sent for user_turn_end
        session.push(
            RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                          arguments={"query": "net sales?"})
        )
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="turn_end"))
        _read_until(ws, "turn_end")

    by_name = {}
    for name, attrs in spans:
        by_name.setdefault(name, []).append(attrs)

    turn = by_name["voice.live.turn"][0]
    assert turn["waj.voice.live.eot_to_first_audio_ms"] >= 0
    assert turn["waj.voice.live.had_tool"] == 1

    tool = by_name["voice.live.tool"][0]
    assert tool["waj.voice.live.delegate_ms"] >= 0
    assert tool["waj.voice.live.dispatch_to_result_ms"] >= 0
    assert tool["waj.voice.live.eot_to_result_ms"] >= 0
    assert tool["waj.voice.live.error"] == 0

    session_attrs = by_name["voice.live.session"][0]
    assert session_attrs["waj.voice.live.tool_calls"] == 1
    assert session_attrs["waj.voice.live.user_turns"] == 1
    assert session_attrs["waj.voice.live.assistant_turns"] == 1
# --- who gets charged, and who gets a concurrency slot ---------------------


def test_a_spoofed_business_id_cannot_pick_the_budget_row(monkeypatch, harness):
    """`businessId` is an unauthenticated query param. An anonymous caller
    naming someone else's business must not charge (or drain) their day."""
    client, _state = harness
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: (None, None))
    monkeypatch.setattr(live_ep, "_resolve_portal_session", lambda kind, conn: None)
    charged = []
    monkeypatch.setattr(live_ep, "check_audio_budget", lambda key, *a, **kw: charged.append(key))

    for biz in ("victim-biz", "another-biz"):
        with client.websocket_connect(f"/voice/live?agentKind=sample_voice&businessId={biz}") as ws:
            ws.receive_json()

    assert len(charged) == 2, "the budget was never consulted"
    # `_budget_key` uuid5-hashes its input, so the raw string never appears
    # either way. The property that matters is that the caller cannot MOVE
    # between budget rows: rotating the param must land on the same key, and
    # that key must not be the one the param would have chosen.
    assert charged[0] == charged[1], "rotating businessId still mints a fresh budget"
    assert charged[0] != live_ep._budget_key("victim-biz", "irrelevant")


def test_the_portal_session_merchant_wins_over_the_query_param(monkeypatch, harness):
    client, _state = harness
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: (None, None))
    monkeypatch.setattr(
        live_ep, "_resolve_portal_session",
        lambda kind, conn: {"merchantId": "3f8c4c5e-0d1a-4f2b-8c7d-9e0a1b2c3d4e"},
    )
    charged = []
    monkeypatch.setattr(live_ep, "check_audio_budget", lambda key, *a, **kw: charged.append(key))

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=someone-else") as ws:
        ws.receive_json()

    assert charged[0] == "3f8c4c5e-0d1a-4f2b-8c7d-9e0a1b2c3d4e"


def test_rotating_the_business_id_does_not_mint_concurrency_slots(monkeypatch, harness):
    """The cap keyed on `businessId` bound only callers who didn't think to
    change it."""
    client, _state = harness
    live_ep._active.clear()
    monkeypatch.setattr(live_ep._connect_limiter, "check", lambda key: None)

    # Snapshot from INSIDE the handler, at the first call that happens after the
    # counters are bumped. Sampling `_active` from the test body instead raced
    # the handler's own teardown — the fake session's event stream can end
    # before the client's `with` block does, so CI saw [{'user-1'}, set(),
    # set()] and the test failed for a reason that had nothing to do with keys.
    seen_keys = []
    monkeypatch.setattr(
        live_ep, "_tag_live_span",
        lambda **kw: seen_keys.append(set(live_ep._active)),
    )

    for i in range(3):
        with client.websocket_connect(f"/voice/live?agentKind=sample_voice&businessId=b{i}") as ws:
            ws.receive_json()

    assert len(seen_keys) == 3, seen_keys
    # Same caller, three different businessIds => the SAME buckets every time.
    # Keyed on businessId (the old behaviour) these were {'b0'}, {'b1'}, {'b2'}
    # — a fresh slot per rotation, so the cap only bound callers who didn't
    # think to change the param.
    assert all(k == seen_keys[0] for k in seen_keys), f"businessId still picks the bucket: {seen_keys}"
    # Two counters per connection: the per-identity one and the per-IP ceiling
    # that bounds a caller who can present a new identity each time.
    assert len(seen_keys[0]) == 2, seen_keys[0]
    assert sum(1 for k in seen_keys[0] if k.startswith("ip:")) == 1, seen_keys[0]
    live_ep._active.clear()

def test_the_handshake_is_charged_only_after_the_session_starts(monkeypatch, harness):
    """Charging before the provider was up let a reconnect loop against a
    broken provider drain the tenant's day without ever speaking."""
    client, _state = harness
    reserved = []
    monkeypatch.setattr(live_ep, "check_audio_budget", lambda *a, **kw: None)
    monkeypatch.setattr(
        live_ep, "reserve_audio_seconds",
        lambda key, seconds, *a, **kw: reserved.append(seconds),
    )

    class _DeadSession:
        audio_format = AudioFormat(16000, 24000)

        async def start(self):
            raise live_ep.RealtimeProviderError("provider down")

        async def close(self):
            pass

    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: _DeadSession())

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        with pytest.raises(Exception):
            while True:
                ws.receive_json()

    assert reserved == [], f"a failed setup still charged {reserved}"


def test_a_forged_bearer_token_cannot_vouch_for_the_business_id(monkeypatch, harness):
    """The first fix only closed the ANONYMOUS path. `authenticate_optional`
    decodes without verifying the signature, so a self-minted
    `{"sub": "..."}` made the caller look authenticated and handed the
    unauthenticated `businessId` param straight back to them — reopening both
    the fresh-budget-per-connection and the aim-a-victim's-row holes."""
    client, _state = harness
    # Looks authenticated to the unverified decode...
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("forged-tok", "attacker-sub"))
    monkeypatch.setattr(live_ep, "_resolve_portal_session", lambda kind, conn: None)
    # ...but the signature does not verify.
    import core.auth as core_auth
    monkeypatch.setattr(
        core_auth, "validate_token_verified",
        lambda tok: (_ for _ in ()).throw(ValueError("bad signature")),
    )
    charged = []
    monkeypatch.setattr(live_ep, "check_audio_budget", lambda key, *a, **kw: charged.append(key))

    for biz in ("victim-biz", "another-biz"):
        with client.websocket_connect(f"/voice/live?agentKind=sample_voice&businessId={biz}") as ws:
            ws.receive_json()

    assert charged[0] == charged[1], "a forged bearer still mints a fresh budget per connection"
    assert charged[0] != live_ep._budget_key("victim-biz", "irrelevant")


def test_a_verified_bearer_may_still_name_its_business(monkeypatch, harness):
    """The legitimate case must keep working: an operator or direct-API caller
    whose token actually verifies is trusted with the param."""
    client, _state = harness
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("real-tok", "op-sub"))
    monkeypatch.setattr(live_ep, "_resolve_portal_session", lambda kind, conn: None)
    import core.auth as core_auth
    monkeypatch.setattr(core_auth, "validate_token_verified", lambda tok: {"sub": "op-sub"})
    charged = []
    monkeypatch.setattr(live_ep, "check_audio_budget", lambda key, *a, **kw: charged.append(key))

    biz = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    with client.websocket_connect(f"/voice/live?agentKind=sample_voice&businessId={biz}") as ws:
        ws.receive_json()

    assert charged[0] == biz


# ---------------------------------------------------------------------------
# Out-of-band sentence streaming (one-result-per-call-id providers)
# ---------------------------------------------------------------------------


def _oob_session(state):
    """Shape the fake like azure_voice_live: one tool result per call id,
    out-of-band speech available."""
    session = state["session"]
    session.supports_streamed_tool_results = False
    session.supports_out_of_band_speech = True
    return session


def test_single_use_provider_gets_the_whole_answer_as_one_interrupt_result(harness, monkeypatch):
    """A provider that can't take several tool results per call id
    (azure_voice_live) is NOT streamed to — not via tool results, and not via
    the out-of-band queue either. Answers spoken out-of-band never became
    conversation items: the model's history never showed it had spoken, every
    later response looked like a first reply, and the persona re-greeted the
    caller mid-call (live incident, 2026-07-31). The whole answer must land
    as its single closing tool result, audibly (INTERRUPT)."""
    client, state = harness
    session = _oob_session(state)

    async def _fake_delegate(**kwargs):
        assert kwargs.get("on_sentence") is None, (
            "single-use-result providers must never be sentence-streamed"
        )
        return DelegateResult(
            spoken="Nine hundred riyals. Mada leads the schemes.",
            display="SAR 900", charts=[], streamed=[],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")

    # Nothing went out-of-band; the answer is one audible tool result.
    assert session.out_of_band == []
    assert session.tool_results == [(
        "c1",
        {"spoken": "Nine hundred riyals. Mada leads the schemes."},
        ToolResultMode.INTERRUPT,
    )]


def test_single_result_delivery_lands_silently_after_barge_in(harness, monkeypatch):
    import asyncio as _a

    client, state = harness
    session = _oob_session(state)
    released = {"go": None}

    async def _fake_delegate(**kwargs):
        await released["go"].wait()          # hold until the barge-in landed
        return DelegateResult(
            spoken="The whole answer.", display="d", charts=[], streamed=[],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        released["go"] = _a.Event()
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        session.push(RealtimeEvent(type="interrupted"))
        # Confirmed barge-in (the caller's new utterance is transcribed as
        # real words) — an unconfirmed one recovers the answer instead.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no wait, cancel that", final=True))
        _read_until(ws, "interrupted")
        released["go"].set()
        _read_until(ws, "display")

    # The abandoned answer lands in context without hijacking the caller's
    # new question.
    assert session.out_of_band == []
    assert session.tool_results == [(
        "c1", {"spoken": "The whole answer."}, ToolResultMode.SILENT,
    )]


def test_delegate_failure_still_speaks_the_apology(harness, monkeypatch):
    """Nothing streamed (delegate error) -> the closing result must be the
    audible INTERRUPT failure line, exactly like the non-streaming path."""
    from voice.live.delegate import FAILURE_SPOKEN

    client, state = harness
    session = _oob_session(state)

    async def _fake_delegate(**kwargs):
        return DelegateResult(spoken=FAILURE_SPOKEN, display="err",
                              charts=[], error=True)

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        _read_until(ws, "display")

    assert session.out_of_band == []
    assert session.tool_results == [
        ("c1", {"spoken": FAILURE_SPOKEN}, ToolResultMode.INTERRUPT)
    ]


def test_played_frame_routes_to_report_playback(harness):
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "played", "ms": 1234})
        ws.send_json({"type": "mute"})           # ignored, exercises the parser
        ws.send_text("not json at all")          # malformed, must not kill the pump
        ws.send_bytes(b"\x01")                   # audio still flows after all of it
    assert session.playback_reports == [1234.0]
    assert b"\x01" in session.sent_audio


def test_a_blank_ask_agent_query_is_not_delegated(harness, monkeypatch):
    """A noise-triggered turn looks like this: the VAD commits a non-speech
    utterance, the model calls ask_agent with nothing, and the caller then
    hears "I couldn't pull that up" for a question they never asked (the
    persisted transcript showed an empty user message on a client demo)."""
    client, state = harness
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="should not happen", display="x", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c-blank",
                                   tool_name="ask_agent", arguments={"query": "   "}))
        session.push(RealtimeEvent(type="turn_end"))
        _read_until(ws, "turn_end")

    assert called == [], "a blank query still ran a full agent turn"
    # The call id is still closed, SILENTly, so the provider isn't left waiting
    # and nothing is spoken.
    assert session.tool_results == [("c-blank", {"spoken": ""}, ToolResultMode.SILENT)]


def test_a_blank_ask_agent_after_a_committed_question_recovers_it(harness, monkeypatch):
    """The dropped-question race (observed live 3x on 2026-08-03, incl. the
    12:23:48Z call): a barge-in/custody cancel lands while the model is
    mid-way through STREAMING its ask_agent arguments — the provider parses
    the truncated JSON ("malformed tool arguments"), emits arguments={}, and
    the blank-query guard swallows the call. The caller's just-asked question
    gets no answer, ever.

    A blank call that follows a COMMITTED substantive utterance nothing else
    has answered is that race, not noise: the endpoint must delegate the
    committed words directly and deliver relay-style (the cancelled response
    took the function_call item with it — a tool result has nothing left to
    attach to)."""
    client, state = harness
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="Here is your answer.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="how were sales yesterday", final=True))
        session.push(RealtimeEvent(type="tool_call", call_id="c-cut",
                                   tool_name="ask_agent", arguments={}))
        _read_until(ws, "display")

    assert called == ["how were sales yesterday"]
    # The truncated call id is still closed SILENTly so the provider is not
    # left waiting...
    assert session.tool_results == [("c-cut", {"spoken": ""}, ToolResultMode.SILENT)]
    # ...and the answer arrives as a say-exactly text turn, like a relay
    # delivery.
    assert any(
        t.startswith("Say exactly this to the caller") and "Here is your answer." in t
        for t in session.text_turns
    ), session.text_turns


def test_recovered_turn_in_text_only_skips_the_say_exactly_turn(harness, monkeypatch):
    """Same dropped-question race as the test above, but on a text_only
    session: nothing is ever spoken, so the recovery must not fall back to
    the relay-style `send_text_turn("Say exactly this...")` delivery — the
    display frame the endpoint already sent is the whole delivery. Before the
    fix this handler ignored `cfg.text_only` and issued the say-exactly turn
    unconditionally, which would have rendered the literal instruction
    wording as a second turn."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", text_only=True,
    )
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="Here is your answer.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="how were sales yesterday", final=True))
        session.push(RealtimeEvent(type="tool_call", call_id="c-cut",
                                   tool_name="ask_agent", arguments={}))
        _read_until(ws, "display")

    assert called == ["how were sales yesterday"]
    # The truncated call id is still closed SILENTly, same as the spoken case.
    assert session.tool_results == [("c-cut", {"spoken": ""}, ToolResultMode.SILENT)]
    # ...but nothing is ever spoken in text_only mode: no say-exactly turn.
    assert session.text_turns == [], session.text_turns


def test_a_blank_ask_agent_after_an_answered_question_stays_swallowed(harness, monkeypatch):
    """The recovery must not resurrect noise. Once the model's own usable
    ask_agent has dispatched for the last committed utterance, a later blank
    call is the old noise shape again (VAD committed non-speech) — close it
    silently, no second answer."""
    client, state = harness
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="answered", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="how were sales yesterday", final=True))
        session.push(RealtimeEvent(type="tool_call", call_id="c-real",
                                   tool_name="ask_agent",
                                   arguments={"query": "sales yesterday"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="tool_call", call_id="c-blank",
                                   tool_name="ask_agent", arguments={"query": ""}))
        session.push(RealtimeEvent(type="turn_end"))
        _read_until(ws, "turn_end")

    assert called == ["sales yesterday"], "the blank call re-ran a full agent turn"
    assert ("c-blank", {"spoken": ""}, ToolResultMode.SILENT) in session.tool_results
    assert not any(
        t.startswith("Say exactly this to the caller") for t in session.text_turns
    ), session.text_turns


def _wait_for(predicate, timeout=3.0):
    """Poll `predicate` from the test thread while the app loop runs."""
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if predicate():
            return True
        _t.sleep(0.02)
    return predicate()


def test_undelivered_answer_is_re_delivered_as_a_text_turn(harness, monkeypatch):
    """The post-delivery mute race (tid_shorthand ~1/3, live 2026-08-03
    15:12 session): the INTERRUPT tool result lands, but its response.create
    is rejected by a response racing it, and the provider's retry declines
    because a commit interleaved — a NOISE commit also declines it, so the
    result item sits in the conversation with nothing speaking it. 74s of
    dead air with no error, until the caller hangs up.

    The endpoint knows what the provider can't judge: whether the caller
    actually superseded the answer (interrupt_epoch). If a delivered answer
    produces no assistant audio within the watchdog window and the epoch is
    unchanged, re-deliver ONCE via the relay discipline (cancel, then a
    say-exactly text turn)."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0.25,
    )
    session = state["session"]

    async def _delegate(**kwargs):
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")
        # No audio ever arrives after the delivery — the mute race. The
        # watchdog must re-deliver the same answer as a say-exactly turn.
        assert _wait_for(lambda: any(
            t.startswith("Say exactly this to the caller") and "Nine hundred." in t
            for t in session.text_turns
        )), session.text_turns
        assert session.cancelled >= 1, "re-delivery must cancel before speaking"


def test_answer_watchdog_stands_down_when_audio_flows(harness, monkeypatch):
    """The watchdog exists for the MUTE shape only: assistant audio after the
    delivery proves the answer is being spoken — no re-delivery, or the
    caller would hear the answer twice."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0.25,
    )
    session = state["session"]

    async def _delegate(**kwargs):
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")
        # The answer starts speaking right after the delivery.
        session.push(RealtimeEvent(type="audio", audio=b"\x00\x01", reply=True))
        import time as _t
        _t.sleep(0.5)  # give the watchdog window time to elapse
        assert not any(
            t.startswith("Say exactly this to the caller") for t in session.text_turns
        ), session.text_turns


def test_answer_watchdog_stands_down_while_a_response_is_in_flight(harness, monkeypatch):
    """Live 2026-08-05 17:46, the duplicate answer: the watchdog fired 4.0s
    after delivery while the answer's own response was still in flight behind
    a holding line the cancel had failed to stop, re-issued it as a
    say-exactly turn, and the caller got the SAME sentence twice — on screen
    and in their ear («طيب — مبيعات شهر يوليو كانت 405,341.84 ريال …»), with
    two "Conversation already has an active response" rejections between the
    two creates.

    Audio is not the only ground truth: a response in flight is an answer
    about to speak. The watchdog must defer to it — and must still rescue the
    genuinely mute answer the moment the floor goes idle."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0.4,
    )
    session = state["session"]

    async def _delegate(**kwargs):
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        # The provider has a response on the wire for the whole delivery.
        session.response_in_flight = True
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")
        import time as _t
        # Two full windows with no audio at all: the pre-fix watchdog fires at
        # the first one.
        _t.sleep(0.9)
        assert not any(
            t.startswith("Say exactly this to the caller") for t in session.text_turns
        ), session.text_turns
        # The response ends having said nothing — the mute race the watchdog
        # was built for. It must still fire.
        session.response_in_flight = False
        assert _wait_for(lambda: any(
            t.startswith("Say exactly this to the caller") and "Nine hundred." in t
            for t in session.text_turns
        )), session.text_turns


def test_answer_watchdog_fires_at_the_ceiling_on_a_wedged_floor(harness, monkeypatch):
    """Deferring to `response_in_flight` must not become deferring forever: a
    response that has held the line for three windows without a syllable is
    wedged, not slow, and the answer still has to reach the caller."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0.2,
    )
    session = state["session"]

    async def _delegate(**kwargs):
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.response_in_flight = True   # and never clears
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")
        assert _wait_for(lambda: any(
            t.startswith("Say exactly this to the caller") and "Nine hundred." in t
            for t in session.text_turns
        )), session.text_turns


def test_answer_watchdog_stands_down_after_a_new_question(harness, monkeypatch):
    """A caller who moved on (confirmed barge-in bumps the epoch) must not
    hear the superseded answer re-delivered behind their new question."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0.25,
    )
    session = state["session"]
    calls = []

    async def _delegate(**kwargs):
        calls.append(kwargs.get("message"))
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")
        # The caller asks something NEW right after the delivery — epoch bump.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="actually, forget it — the fees instead", final=True))
        import time as _t
        _t.sleep(0.5)
        assert not any(
            t.startswith("Say exactly this to the caller") for t in session.text_turns
        ), session.text_turns


def test_announce_then_stall_recovers_the_question(harness, monkeypatch):
    """The 2026-08-04 live-client stall: the native model completes its reply
    after the looking-it-up line ("one moment" / «ثانية وحدة») WITHOUT ever
    emitting the ask_agent call — clean wire, no cancels, tool_calls=0 — and
    in server-VAD mode nothing prompts it again, so the caller sits in
    silence forever. Three live sessions in a row; a persona rule making the
    announce and the call one reply did NOT hold.

    The announce phrase is the perfect trigger: there is no legitimate
    reply that says "let me check" and ends with no tool call behind it.
    A model turn that completes containing an announce phrase while the
    caller's committed question is still unanswered gets the question
    dispatched by the ENDPOINT via the recovery path."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, stall_recovery_sec=0.15,
    )
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="Last month was SAR 90,000.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="what were my sales last month", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="One moment, let me pull that up.", final=True))
        session.push(RealtimeEvent(type="turn_end"))
        # No tool_call ever arrives — the stall. The endpoint must dispatch
        # the committed question itself and deliver relay-style.
        assert _wait_for(lambda: called == ["what were my sales last month"]), called
        assert _wait_for(lambda: any(
            t.startswith("Say exactly this to the caller") and "90,000" in t
            for t in session.text_turns
        )), session.text_turns


def test_a_late_ask_agent_after_a_stall_recovery_is_suppressed(harness, monkeypatch):
    """Live 07:09 session on the recovery build: the model's own ask_agent
    DID arrive — 12s after it announced, 5s after the recovery had already
    spoken the answer. Both delegated the same question (two full LLM turns)
    and the provider only saved the caller from hearing it twice by
    rejecting the late duplicate create ("already has an active response",
    then "Tool call ID not found" x3).

    Once the recovery has answered a commit, the model's late call for that
    same commit is a duplicate by definition: close its call id SILENTLY,
    exactly as relay custody does with the calls it supersedes."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, stall_recovery_sec=0.15,
    )
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="Last month was SAR 90,000.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="what were my sales last month", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="One moment, let me pull that up.", final=True))
        session.push(RealtimeEvent(type="turn_end"))
        assert _wait_for(lambda: called == ["what were my sales last month"]), called
        # …and NOW the model's own call finally shows up.
        session.push(RealtimeEvent(type="tool_call", call_id="c-late",
                                   tool_name="ask_agent",
                                   arguments={"query": "sales last month"}))
        assert _wait_for(lambda: any(
            cid == "c-late" for cid, _p, _m in session.tool_results
        )), session.tool_results

    assert called == ["what were my sales last month"], \
        f"the late call re-delegated an already-recovered question: {called}"
    assert ("c-late", {"spoken": ""}, ToolResultMode.SILENT) in session.tool_results


def test_a_chitchat_reply_with_no_announce_is_not_recovered(harness, monkeypatch):
    """The persona answers small talk directly, with no tool — that is not a
    stall. Only a reply that DECLARED a lookup (announce phrase) and then
    never called qualifies; recovering chitchat would double-answer it."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, stall_recovery_sec=0.15,
    )
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="x", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="how are you today", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="Doing great, thanks for asking!", final=True))
        session.push(RealtimeEvent(type="turn_end"))
        import time as _t
        _t.sleep(0.5)
        assert called == [], "chitchat was recovered into a delegated turn"
        assert not session.text_turns


def test_an_announce_followed_by_its_tool_call_is_not_recovered(harness, monkeypatch):
    """The healthy shape: announce + ask_agent in one reply. The tool call
    consumes the commit, so the stall timer must stand down — one delegated
    turn, one answer, no say-exactly duplicate."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, stall_recovery_sec=0.15,
    )
    session = state["session"]
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="SAR 90,000.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="what were my sales last month", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="One moment, let me pull that up.", final=True))
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "sales last month"}))
        session.push(RealtimeEvent(type="turn_end"))
        _read_until(ws, "display")
        import time as _t
        _t.sleep(0.5)
        assert called == ["sales last month"], called
        assert not any(
            t.startswith("Say exactly this to the caller") for t in session.text_turns
        ), session.text_turns


# ---------------------------------------------------------------------------
# Relay mode — server-side custody of protected flows
# ---------------------------------------------------------------------------


def test_relay_mode_takes_custody_of_a_protected_flow(harness, monkeypatch):
    """The live failure this exists for: the realtime model delegates the
    FIRST beat of an IBAN change then role-plays the rest (asked for a code
    it never sent; wrote a fabricated IBAN; stalled holding digits). Once a
    delegated turn's tools open the flow (flow_signals), every committed
    caller utterance goes STRAIGHT to the delegate — no ask_agent required —
    with the model's own reply cancelled and the relayed answer delivered as
    a say-exactly text turn."""
    client, state = harness
    session = state["session"]
    calls = []
    results = [
        DelegateResult(spoken="Code sent, read it to me.", display="d1",
                       charts=[], flow_signals=["send_otp"]),
        DelegateResult(spoken="Verified — read the new IBAN.", display="d2",
                       charts=[], flow_signals=["verify_otp_ok"]),
    ]

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        return results[min(len(calls), len(results)) - 1]

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "change my iban"}))
        _read_until(ws, "display")
        # Custody: the caller's next utterance never produces a tool_call —
        # the endpoint relays it directly.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="the code is one two three", final=True))
        _read_until(ws, "display")

    assert calls == ["change my iban", "the code is one two three"]
    assert session.cancelled >= 1, "the model's own reply was never cancelled"
    assert any(
        t.startswith("Say exactly this to the caller") and "Verified" in t
        for t in session.text_turns
    ), session.text_turns


def test_relay_custody_in_text_only_skips_the_say_exactly_turn(harness, monkeypatch):
    """Same flow-custody relay as the test above, but on a text_only session:
    nothing is ever spoken, so the relayed answer must land purely as a
    display frame — no `send_text_turn("Say exactly this...")` fallback.
    Before the fix this handler ignored `cfg.text_only` and always issued the
    say-exactly turn, which would have rendered a duplicate, literally-worded
    second turn on top of the display frame."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", text_only=True,
    )
    session = state["session"]
    calls = []
    results = [
        DelegateResult(spoken="Code sent, read it to me.", display="d1",
                       charts=[], flow_signals=["send_otp"]),
        DelegateResult(spoken="Verified — read the new IBAN.", display="d2",
                       charts=[], flow_signals=["verify_otp_ok"]),
    ]

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        return results[min(len(calls), len(results)) - 1]

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "change my iban"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="the code is one two three", final=True))
        _read_until(ws, "display")

    assert calls == ["change my iban", "the code is one two three"]
    assert session.text_turns == [], session.text_turns


def test_relay_custody_releases_after_two_flow_quiet_turns(harness, monkeypatch):
    """The read-back beat legitimately calls no tool, so ONE quiet turn keeps
    custody; a SECOND quiet turn releases it — after that the caller's
    utterances are the model's business again."""
    client, state = harness
    session = state["session"]
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        # Only the opening turn shows flow activity; everything after is quiet.
        signals = ["send_otp"] if len(calls) == 1 else []
        return DelegateResult(spoken=f"s{len(calls)}", display=f"d{len(calls)}",
                              charts=[], flow_signals=signals)

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "change my iban"}))
        _read_until(ws, "display")
        for utterance in ("beat two", "beat three"):
            session.push(RealtimeEvent(type="transcript", role="user",
                                       text=utterance, final=True))
            _read_until(ws, "display")
        # Custody released (two quiet turns) — this one must NOT be relayed.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="unrelated question", final=True))
        session.push(RealtimeEvent(type="turn_end"))
        _read_until(ws, "turn_end")

    assert calls == ["change my iban", "beat two", "beat three"]


def test_relay_mode_off_never_takes_custody(harness, monkeypatch):
    """The rollback lever: relay_mode false leaves every utterance to the
    model even while a flow is open."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", relay_mode=False,
    )
    session = state["session"]
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        return DelegateResult(spoken="s", display="d", charts=[],
                              flow_signals=["send_otp"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "change my iban"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="the code is one two three", final=True))
        session.push(RealtimeEvent(type="turn_end"))
        _read_until(ws, "turn_end")

    assert calls == ["change my iban"]


def test_relay_custody_opens_on_caller_intent_before_any_tool(harness, monkeypatch):
    """Live acceptance 1/3: the model sometimes refuses to delegate the
    change request itself (tool_calls=0) or bundles an invented code into
    beat one — tool-signal custody starts too late. The caller saying
    "iban" opens custody on the spot and relays that very utterance."""
    client, state = harness
    session = state["session"]
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        return DelegateResult(spoken="Code sent.", display="d",
                              charts=[], flow_signals=["send_otp"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="I want to change my IBAN please",
                                   final=True))
        _read_until(ws, "display")

    assert calls == ["I want to change my IBAN please"]
    assert session.cancelled >= 1


def test_relay_custody_opens_on_garbled_iban_intent(harness, monkeypatch):
    """ASR writes "IBAN" as "E-band" / "i ban" / "آي بان" on real calls (the
    prompts enumerate these) — the intent match must survive the garble, or
    custody never opens and the realtime model runs the protected flow
    itself (observed live 2026-07-31: it asked for the new IBAN, no OTP).
    Matching is normalized (case/space/hyphen-insensitive) over the term
    list, so "i-ban" hits "iban" and "E-band" hits "e band"."""
    client, state = harness
    session = state["session"]
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        return DelegateResult(spoken="Code sent.", display="d",
                              charts=[], flow_signals=["send_otp"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="I want to change my E-band on my account",
                                   final=True))
        _read_until(ws, "display")

    assert calls == ["I want to change my E-band on my account"]


def test_relayed_turn_speaks_the_holding_line_during_a_slow_delegate(harness, monkeypatch):
    """Relay custody covers the demo's most sensitive beats (IBAN digits,
    OTP read-back) — and it CANCELS the model's own reply before delegating,
    so without its own holding line the caller reads 16 digits into 6-17s of
    pure dead air (measured live 2026-08-03: relay delegate_ms up to 16.8s,
    no filler at all). The ask_agent path holds the caller after
    progress_after_sec; the relay path must do the same."""
    import time as _t

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        if len(calls) > 1:
            await asyncio.sleep(0.4)
        return DelegateResult(spoken=f"s{len(calls)}", display=f"d{len(calls)}",
                              charts=[], flow_signals=["send_otp"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "change my iban"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="the code is one two three", final=True))
        # Mute frames keep the app loop awake on a quiet runner (same
        # precedent as _frames_until_turn_end) while the slow delegate and
        # the holding-line timer run.
        deadline = _t.time() + 5
        while _t.time() < deadline and not (
            session.out_of_band
            and any(t.startswith("Say exactly this") for t in session.text_turns)
        ):
            ws.send_json({"type": "mute"})
            _t.sleep(0.02)
        session.push(RealtimeEvent(type="turn_end"))
        ws.send_json({"type": "mute"})
        frames = []
        for _ in range(16):
            frame = ws.receive_json()
            if frame.get("type") == "turn_end":
                break
            frames.append(frame)

    assert session.out_of_band == ["Still working on that."]
    # The line the caller heard is also on the conversation record.
    assert any(
        f.get("type") == "transcript" and f.get("role") == "assistant"
        and f.get("text") == "Still working on that."
        for f in frames
    ), frames
    # And it held the caller — it never replaced the actual answer.
    assert any("s2" in t for t in session.text_turns), session.text_turns


def test_relay_delivery_cancels_the_racing_response_first(harness, monkeypatch):
    """The relayed answer rides send_text_turn -> response.create, which has
    NO collision retry (deliberate — a bare retry re-read the last answer).
    If the holding line (or the model catching up) is mid-response when the
    answer lands, the create is rejected and the caller hears silence
    (observed live 2026-08-03, "already has an active response" at a relay
    delivery). Custody semantics already say the model's replies must never
    play — so the delivery must cancel whatever is active first: one cancel
    at turn start, one right before the answer."""
    import time as _t

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        if len(calls) > 1:
            await asyncio.sleep(0.3)
        return DelegateResult(spoken=f"s{len(calls)}", display=f"d{len(calls)}",
                              charts=[], flow_signals=["send_otp"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "change my iban"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="the code is one two three", final=True))
        deadline = _t.time() + 5
        while _t.time() < deadline and not any("s2" in t for t in session.text_turns):
            ws.send_json({"type": "mute"})
            _t.sleep(0.02)

    assert any("s2" in t for t in session.text_turns), session.text_turns
    # Turn-start cancel + pre-delivery cancel. The ask_agent beat (c1)
    # contributes none.
    assert session.cancelled == 2, session.cancelled


def test_relayed_holding_line_defers_to_interim_response(harness, monkeypatch):
    """Same exemption as the ask_agent path: a session whose provider fills
    the wait itself (handles_interim_response) must not get a second holding
    line stacked on top by the relay path."""
    import time as _t

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]
    session.handles_interim_response = True
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        if len(calls) > 1:
            await asyncio.sleep(0.2)
        return DelegateResult(spoken=f"s{len(calls)}", display=f"d{len(calls)}",
                              charts=[], flow_signals=["send_otp"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "change my iban"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="the code is one two three", final=True))
        deadline = _t.time() + 5
        while _t.time() < deadline and not any(
            t.startswith("Say exactly this") for t in session.text_turns
        ):
            ws.send_json({"type": "mute"})
            _t.sleep(0.02)

    assert session.out_of_band == []


def test_model_ask_agent_during_relay_custody_is_closed_silently(harness, monkeypatch):
    """While custody is open the endpoint already relays every committed
    utterance to the delegate — the model's own ask_agent for that same
    utterance is a duplicate. Live 2026-08-03: "نعم صحيح" ran TWO concurrent
    ~12s delegates (relay-4 + the model's call), racing two answers at the
    caller. The duplicate call id must be closed silently, not delegated."""
    client, state = harness
    session = state["session"]
    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        return DelegateResult(spoken=f"s{len(calls)}", display=f"d{len(calls)}",
                              charts=[], flow_signals=["send_otp"])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "change my iban"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="the code is one two three", final=True))
        _read_until(ws, "display")
        # The model catches up and delegates the very utterance the endpoint
        # just relayed.
        session.push(RealtimeEvent(type="tool_call", call_id="c2", tool_name="ask_agent",
                                   arguments={"query": "the code is one two three"}))
        _frames_until_turn_end(ws, session, results=2)

    assert calls == ["change my iban", "the code is one two three"], calls
    assert ("c2", {"spoken": ""}, ToolResultMode.SILENT) in session.tool_results, (
        session.tool_results
    )


# ---------------------------------------------------------------------------
# Fill-the-silence fixes: delivery flushes stale audio, interim-response
# providers own the wait, and false barge-ins recover the answer
# ---------------------------------------------------------------------------


def _frames_until_turn_end(ws, session, *, results=1, settle=0.1):
    """Collect frames up to a pushed turn_end sentinel — hang-proof.

    Waits until `results` tool results are recorded (the delivery, and
    anything the endpoint sends right after it, has then been scheduled),
    lets the loop settle, then pushes turn_end and reads up to it. A missing
    expected frame surfaces as an assertion on the returned list, never as a
    receive_json() that blocks forever.

    The benign "mute" frames are load-bearing on a quiet runner: pushes and
    this poll cross threads, and without inbound socket traffic an IDLE app
    event loop never wakes to process the pushed events — the first CI run
    of these tests stalled exactly that way (passing locally on incidental
    wakeups). "mute" is documented, parsed, and ignored by the endpoint.
    """
    import time as _t

    deadline = _t.time() + 5
    while len(session.tool_results) < results and _t.time() < deadline:
        ws.send_json({"type": "mute"})
        _t.sleep(0.02)
    _t.sleep(settle)
    session.push(RealtimeEvent(type="turn_end"))
    ws.send_json({"type": "mute"})
    seen = []
    for _ in range(16):
        frame = ws.receive_json()
        if frame.get("type") == "turn_end":
            return seen
        seen.append(frame)
    raise AssertionError(f"no turn_end sentinel; saw {seen!r}")


def test_interrupt_answer_delivery_flushes_stale_playback(harness, monkeypatch):
    """Microsoft's own tool-result recipe: cancel -> truncate -> FLUSH LOCAL
    PLAYBACK -> item -> create. The provider cancels generation server-side,
    but the browser still holds buffered filler audio (the service streams
    faster than realtime) — without a flush frame the caller hears the rest
    of the holding line before the answer, i.e. the filler ADDS latency."""
    client, state = harness
    session = state["session"]

    async def _fake_delegate(**kwargs):
        return DelegateResult(spoken="Answer.", display="Answer.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        frames = _frames_until_turn_end(ws, session)

    assert {"type": "interrupted", "reason": "answer_ready"} in frames, frames  # the flush
    assert {"type": "display", "text": "Answer."} in frames
    assert session.tool_results == [("c1", {"spoken": "Answer."}, ToolResultMode.INTERRUPT)]


def test_first_streamed_sentence_flushes_stale_holding_line_playback(harness, monkeypatch):
    """The browser holds audio the caller has not heard yet — on Gemini
    SECONDS of it (speech is generated ~3x faster than it plays: measured on
    one turn, generation done at +3.9s, turn_complete at +10.9s). When the
    first sentence INTERRUPTS a holding line, that queue must be dropped or
    the caller waits out the filler the answer just cancelled server-side.
    Exactly once: flushing on every WHEN_IDLE sentence would cut each one off
    with the next."""
    import asyncio as _a
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]

    async def _fake_delegate(**kwargs):
        on_sentence = kwargs["on_sentence"]
        await _a.sleep(0.3)
        await on_sentence("One.")
        await on_sentence("Two.")
        return DelegateResult(spoken="One. Two.", display="One. Two.", charts=[],
                              streamed=["One.", "Two."])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        frames = _frames_until_turn_end(ws, session, results=2)

    flushes = [f for f in frames if f == {"type": "interrupted", "reason": "answer_ready"}]
    assert len(flushes) == 1, frames
    modes = [m for _cid, _p, m in session.tool_results]
    assert modes == [ToolResultMode.INTERRUPT, ToolResultMode.WHEN_IDLE]


def test_no_playback_flush_when_no_holding_line_was_spoken(harness, monkeypatch):
    """The discriminating half. That flush THROWS AWAY whatever the browser
    still holds — and on a fast turn what it holds is the persona's own
    announce line, which the prompt requires in the same breath as the tool
    call. Flushing it is heard as the assistant cutting itself off mid-word
    (reported live 2026-08-19 as "she interrupts herself", alongside 159ms
    and 531ms assistant turns). With no holding line there is nothing stale
    to drop, so no flush frame may be sent at all."""
    from voice.live.config import LiveVoiceConfig

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", stream_sentences=True,
        progress_after_sec=0,
    )
    session = state["session"]

    async def _fake_delegate(**kwargs):
        on_sentence = kwargs["on_sentence"]
        await on_sentence("One.")
        await on_sentence("Two.")
        return DelegateResult(spoken="One. Two.", display="One. Two.", charts=[],
                              streamed=["One.", "Two."])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        frames = _frames_until_turn_end(ws, session, results=2)

    assert [f for f in frames
            if f == {"type": "interrupted", "reason": "answer_ready"}] == [], frames
    modes = [m for _cid, _p, m in session.tool_results]
    assert modes == [ToolResultMode.WHEN_IDLE, ToolResultMode.WHEN_IDLE]


def test_no_holding_line_when_the_provider_handles_interim_responses(harness, monkeypatch):
    """A session that declared handles_interim_response (Azure's server-side
    interim_response is configured on the model row) owns the wait itself —
    the endpoint's holding line on top would double-fill the same silence."""
    import asyncio as _a

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="Still working on that.",
    )
    session = state["session"]
    session.handles_interim_response = True

    async def _slow(**kwargs):
        await _a.sleep(0.3)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "net sales?"}))
        _read_until(ws, "display")

    assert session.out_of_band == []
    spoken = [p["spoken"] for _cid, p, _m in session.tool_results]
    assert "Still working on that." not in spoken, spoken


def test_unconfirmed_interrupt_recovers_the_spoken_answer(harness, monkeypatch):
    """Background noise trips speech_started mid-response ('interrupted') but
    no utterance ever commits. Downgrading the pending answer to SILENT on
    the interrupt alone meant the caller asked, waited through the filler,
    and heard nothing — the reported "answer received but never spoken". The
    industry pattern (LiveKit's resume_false_interruption) waits a short
    confirm window: unconfirmed means false alarm, speak the answer.

    Since confirmed barge-in landed, this case no longer even reaches the
    window: an unconfirmed `interrupted` does not bump the epoch at all, so
    the answer is INTERRUPT on the first check with nothing to wait out."""
    import asyncio as _a

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=0.2,
    )
    session = state["session"]

    async def _slow(**kwargs):
        await _a.sleep(0.05)
        return DelegateResult(spoken="Recovered.", display="Recovered.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        session.push(RealtimeEvent(type="interrupted"))
        _frames_until_turn_end(ws, session)

    assert session.tool_results == [
        ("c1", {"spoken": "Recovered."}, ToolResultMode.INTERRUPT)
    ], session.tool_results


def test_confirmed_interrupt_still_lands_silently(harness, monkeypatch):
    """The confirm window must NOT soften a real barge-in: the caller's
    overriding utterance is transcribed as real words, so the abandoned
    answer stays SILENT exactly as before."""
    import asyncio as _a

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=0.2,
    )
    session = state["session"]

    async def _slow(**kwargs):
        await _a.sleep(0.05)
        return DelegateResult(spoken="Late answer.", display="Late answer.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no wait, cancel that", final=True))
        _frames_until_turn_end(ws, session)

    assert session.tool_results == [
        ("c1", {"spoken": "Late answer."}, ToolResultMode.SILENT)
    ], session.tool_results


def test_zero_confirm_window_keeps_legacy_immediate_silence(harness, monkeypatch):
    """interrupt_confirm_window_sec=0 is the rollback lever: any interrupt,
    confirmed or not, lands the answer SILENT immediately."""
    import asyncio as _a

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=0,
    )
    session = state["session"]

    async def _slow(**kwargs):
        await _a.sleep(0.05)
        return DelegateResult(spoken="Late answer.", display="Late answer.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        session.push(RealtimeEvent(type="interrupted"))
        _frames_until_turn_end(ws, session)

    assert session.tool_results == [
        ("c1", {"spoken": "Late answer."}, ToolResultMode.SILENT)
    ], session.tool_results




def test_committed_utterance_supersedes_an_answer_dispatched_before_it(harness, monkeypatch):
    """The barge-in that actually happens on a call gets NO `interrupted`.

    azure_voice_live raises `interrupted` only while she is speaking
    (`_response_active`), but the common barge-in lands in the SILENT
    tool-wait of a delegated turn — the provider classifies that
    `speech_started_idle` and nothing moves. Reproduced live 2026-08-01:
    caller asked for last month's sales, then cut in with "no, the best
    branch instead"; session metrics showed barge_ins=0,
    speech_started_idle=2, and BOTH answers mode=interrupt — so she read out
    the full superseded answer before the one they asked for.

    A committed, substantive utterance is therefore the signal: it supersedes
    anything dispatched before it, with no `interrupted` needed.

    The `speech_started_idle` below is not decoration — it is the second of
    the two this session's own metrics recorded, and since #72 it is what
    makes the utterance NEW. A `final` that arrives with no onset since the
    dispatch is the transcript of the utterance that dispatch is already
    answering (Azure's ASR pipeline lags its model events by design), and
    superseding on that delivered the caller's own answer as dead air. The
    script omitted the onset while it was behaviourally inert; with the
    distinction load-bearing, omitting it would script a DIFFERENT call than
    the one this test is about.
    """
    import asyncio

    client, state = harness
    session = state["session"]

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.05)
        return DelegateResult(spoken="Superseded answer.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "sales last month"}))
        # No `interrupted` at all — she is silent, mid tool-wait. The caller
        # starting to speak is reported as `speech_started_idle`, and then
        # their new question commits.
        session.push(RealtimeEvent(type="speech_started_idle"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no, the best branch instead", final=True))
        ws.receive_json()   # user transcript frame
        ws.receive_json()   # display frame once the slow delegate lands

    assert session.tool_results == [
        ("c1", {"spoken": "Superseded answer."}, ToolResultMode.SILENT)
    ]


def test_a_backchannel_does_not_supersede_the_pending_answer(harness, monkeypatch):
    """The other half: a caller saying "mm-hm" / "أيوه" while she fetches
    their answer must still HEAR that answer. Superseding on every committed
    utterance would silently drop it — the caller asks, waits, and gets
    nothing, which is worse than the bug being fixed."""
    import asyncio

    client, state = harness
    session = state["session"]

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.05)
        return DelegateResult(spoken="Wanted answer.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "sales last month"}))
        session.push(RealtimeEvent(type="transcript", role="user", text="أيوه", final=True))
        ws.receive_json()   # user transcript frame
        ws.receive_json()   # display frame

    assert session.tool_results == [
        ("c1", {"spoken": "Wanted answer."}, ToolResultMode.INTERRUPT)
    ]


def test_an_availability_probe_does_not_supersede_the_pending_answer(harness, monkeypatch):
    """"هلو؟" ten seconds into a slow lookup is the caller checking the line,
    not a new question — yet it committed as a substantive utterance and the
    answer landed mode=silent (observed live 2026-08-03 after a 14.6s
    delegate; the caller re-asked the same question across three redials).
    Availability probes must ride the backchannel exemption."""
    import asyncio

    client, state = harness
    session = state["session"]

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.05)
        return DelegateResult(spoken="Wanted answer.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "sales last month"}))
        session.push(RealtimeEvent(type="transcript", role="user", text="هلو؟", final=True))
        ws.receive_json()   # user transcript frame
        ws.receive_json()   # display frame

    assert session.tool_results == [
        ("c1", {"spoken": "Wanted answer."}, ToolResultMode.INTERRUPT)
    ]


def test_the_current_questions_own_answer_is_never_superseded(harness, monkeypatch):
    """Bumping on a committed utterance must not eat the answer to THAT
    utterance: its `tool_call` is dispatched afterwards and so captures the
    new epoch. Pinned because the obvious implementation (bump, then reuse a
    stale dispatch epoch) silences every answer on the call."""
    import asyncio

    client, state = harness
    session = state["session"]

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.05)
        return DelegateResult(spoken="Its own answer.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="how were my sales last month", final=True))
        ws.receive_json()   # user transcript frame
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "sales"}))
        ws.receive_json()   # display frame

    assert session.tool_results == [
        ("c1", {"spoken": "Its own answer."}, ToolResultMode.INTERRUPT)
    ]


def test_noise_interrupt_never_reaches_the_client(harness):
    """A bare `interrupted` (speech-start detection, no words ever) must not
    flush the browser: no `interrupted` frame goes out. The marker transcript
    proves frame ordering — if a flush had been sent it would arrive first."""
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frame = ws.receive_json()
    assert frame["type"] == "transcript" and frame["text"] == "marker"


def test_noise_commit_holds_the_flush_and_suppresses_the_reply(harness):
    """Noise loud enough to COMMIT *over her voice*: the flush is held, the
    transcript verdict comes back empty, the held flush is dropped (she keeps
    playing) and the spurious auto-reply is suppressed. The leading
    `interrupted` is what makes this barge-in context — see
    test_idle_noise_commit_drops_the_flush_but_spares_the_reply for the
    idle-line case, where the reply must survive."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,  # valve must not fire mid-test
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="interrupted"))   # noise over her voice
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        session.push(RealtimeEvent(type="transcript", role="user", text="", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frames = [ws.receive_json(), ws.receive_json()]
    # The empty-final user frame and the marker — but NO interrupted frame.
    assert [f["type"] for f in frames] == ["transcript", "transcript"]
    assert session.spurious_suppressions == 1
    assert session.barge_in_cancels == 0


def test_backchannel_commit_does_not_flush_playback(harness):
    """An 'mhm' while she talks: committed, transcribed, dismissed."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="interrupted"))   # 'mhm' over her voice
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        session.push(RealtimeEvent(type="transcript", role="user", text="mhm", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frames = [ws.receive_json(), ws.receive_json()]
    assert all(f["type"] == "transcript" for f in frames)
    assert session.spurious_suppressions == 1


def test_idle_noise_commit_drops_the_flush_but_spares_the_reply(harness):
    """Same empty-verdict commit, but with NO speech onset over her voice —
    the line was idle. The held flush is still dropped (an empty verdict
    never flushes), but the auto-reply must NOT be cancelled: on the native
    row the model hears the audio directly, so an empty transcript here can
    be real-but-quiet speech it understood, and suppressing its answer is
    exactly the silent-answer bug this branch exists to avoid."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,  # valve must not fire mid-test
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        # No `interrupted` push: nothing pending, so no barge-in context.
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        session.push(RealtimeEvent(type="transcript", role="user", text="", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frames = [ws.receive_json(), ws.receive_json()]
    # Marker-transcript trick: a flush would have arrived before the marker.
    assert [f["type"] for f in frames] == ["transcript", "transcript"]
    assert frames[-1]["text"] == "marker"
    assert session.spurious_suppressions == 0
    assert session.barge_in_cancels == 0


def test_substantive_delta_confirms_before_the_final(harness):
    """Real barge-in cut latency rides the DELTAS: words accumulate mid-
    utterance -> epoch bumps, the talked-over response is cancelled, and the
    flush frame goes out — all before any `final` transcript."""
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="what about the jeddah branch", final=False))
        frame = _read_until(ws, "interrupted")
    assert frame == {"type": "interrupted", "reason": "barge_in"}
    assert session.barge_in_cancels == 1


def test_confirmation_bumps_the_epoch_once_per_utterance(harness, monkeypatch):
    """Delta-confirm then final-with-text: ONE epoch bump. A double bump
    would advance the epoch past the tool_call dispatched for that same
    utterance and wrongly downgrade the caller's own answer to SILENT."""
    client, state = harness
    session = state["session"]

    async def _fake_delegate(**kwargs):
        return DelegateResult(spoken="Answer.", display="Answer.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="interrupted"))   # speech onset over her voice
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="how many terminals", final=False))
        _read_until(ws, "interrupted")  # confirmation flush for the utterance
        session.push(RealtimeEvent(type="transcript", role="user", text="", final=True))
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "how many terminals"}))
        _read_until(ws, "display")
    assert session.tool_results == [("c1", {"spoken": "Answer."}, ToolResultMode.INTERRUPT)]
    # The latch itself, not just its epoch side effect: without the
    # once-per-utterance guard the final-with-text would confirm a SECOND
    # time and cancel twice.
    assert session.barge_in_cancels == 1


def test_tool_call_backstop_then_lagging_final_bumps_the_epoch_once(harness, monkeypatch):
    """The other transcript shape of the once-per-utterance rule: on Azure the
    user transcript rides a separate, slower ASR pipeline than model events,
    so the utterance's `tool_call` routinely arrives BEFORE its substantive
    `final`. The confirm-mode backstop fires on the tool_call (epoch bump,
    dispatch captures the new epoch) — and the lagging final must then hit
    the latch and confirm NOTHING. Clearing the latch after the backstop let
    that final bump the epoch a second time, past the utterance's own
    dispatch, and the caller's own answer went out SILENT: dead air for a
    question they actually asked."""
    import asyncio as _a

    client, state = harness
    session = state["session"]

    async def _slow(**kwargs):
        await _a.sleep(0.05)
        return DelegateResult(spoken="Answer.", display="Answer.", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        # Speech onset over her voice: detection only, pending interrupt set.
        session.push(RealtimeEvent(type="interrupted"))
        # No transcript yet — the model understood the audio first. The
        # backstop confirms the barge-in here; dispatch captures the epoch.
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "how many terminals"}))
        _read_until(ws, "interrupted")  # the backstop's confirmation flush
        # The lagging, substantive final for the SAME utterance lands while
        # the delegate is still in flight. It must not re-confirm.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="how many terminals", final=True))
        _read_until(ws, "display")
    # The answer to the question the caller just asked is SPOKEN, not
    # silently dropped as its own barge-in victim.
    assert session.tool_results == [("c1", {"spoken": "Answer."}, ToolResultMode.INTERRUPT)]
    # And she was cancelled exactly once — the lagging final confirmed nothing.
    assert session.barge_in_cancels == 1


def test_flush_valve_releases_when_transcription_goes_dark(harness):
    """No verdict within interrupt_confirm_window_sec of a commit -> flush
    anyway. Fail toward today's behavior, never toward stale audio playing
    over a real reply."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=0.05,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        frame = _read_until(ws, "interrupted")   # valve fires at ~50ms
    assert frame == {"type": "interrupted", "reason": "speech_detected"}


def test_reply_audio_releases_the_held_flush_first(harness):
    """Reply audio racing the transcript verdict: the held flush is released
    BEFORE the reply bytes are forwarded, so the flush can never eat the
    start of the answer it was protecting."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        session.push(RealtimeEvent(type="audio", audio=b"REPLY", reply=True))
        frame = ws.receive_json()          # the released flush, first
        assert frame == {"type": "interrupted", "reason": "input_end"}
        assert ws.receive_bytes() == b"REPLY"


def test_provider_without_confirmation_keeps_the_legacy_barge_in(harness):
    """gemini_live never finalizes user transcription, so it declares
    supports_barge_in_confirmation=False — its `interrupted` must keep
    flushing immediately (tagged barge_in: on this path the endpoint treats
    every provider interrupt as one)."""
    client, state = harness
    session = state["session"]
    session.supports_barge_in_confirmation = False   # instance shadow of the class attr
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="interrupted"))
        frame = ws.receive_json()
    assert frame == {"type": "interrupted", "reason": "barge_in"}


def test_zero_window_legacy_never_reaches_for_a_provider_cancel(harness, monkeypatch):
    """window 0 is the rollback lever, so it must be byte-identical legacy
    even on a provider that CAN confirm. A substantive final still supersedes
    the in-flight answer (epoch bump -> SILENT), but the endpoint must not
    call cancel_barged_in_response — an upstream call the pre-confirmation
    code never made, and a rollback that adds one isn't a rollback."""
    import asyncio as _a

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=0,
    )
    session = state["session"]
    assert session.supports_barge_in_confirmation is True  # the window is the lever

    async def _slow(**kwargs):
        await _a.sleep(0.05)
        return DelegateResult(spoken="Superseded.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no wait, cancel that", final=True))
        _frames_until_turn_end(ws, session)

    assert session.tool_results == [
        ("c1", {"spoken": "Superseded."}, ToolResultMode.SILENT)
    ], session.tool_results
    assert session.barge_in_cancels == 0
    assert session.spurious_suppressions == 0


# ── The caller is still HEARING an answer the provider already "delivered" ──
# The service streams a reply far faster than realtime (28s of speech in 6s,
# measured live 2026-08-04), so for most of an answer the caller is listening
# to audio the provider considers finished. A flush in that window destroys
# the unheard remainder: the answer stops mid-sentence and the model, believing
# it said everything, has nothing to add. These three pin the gate.

# 2s of 24 kHz pcm16 mono — the fixture's declared output rate.
_TWO_SECONDS = b"\x00" * (2 * 24000 * 2)


def test_reply_audio_does_not_flush_an_answer_the_caller_is_still_hearing(harness):
    """The leak that reproduced live: a backchannel commits, the flush is
    correctly held for the verdict, and then the provider's auto-reply to that
    backchannel releases it BEFORE the verdict lands — throwing away every
    still-queued second of the real answer."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,   # valve must not fire mid-test
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        session.push(RealtimeEvent(type="audio", audio=b"REPLY", reply=True))
        # No flush frame ahead of the reply bytes: ~2s of answer is still
        # queued in the browser and the commit is not confirmed.
        assert ws.receive_bytes() == b"REPLY"


def test_flush_valve_spares_an_answer_the_caller_is_still_hearing(harness):
    """The valve fails toward flushing, which is right when the only thing
    queued is a stale holding line — and wrong when it is the answer."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=0.05,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        # Well past the 50ms window, so an ungated valve has certainly fired
        # by the time the marker is pushed — the marker arriving FIRST is the
        # proof no flush went out.
        time.sleep(0.4)
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frame = ws.receive_json()
    assert frame["type"] == "transcript" and frame["text"] == "marker"


def test_confirmed_barge_in_still_flushes_mid_playback(harness):
    """The gate must not defeat a REAL barge-in: substantive words while the
    answer plays still cut it off immediately."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="playback_flush"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no, the best branch instead", final=True))
        frame = _read_until(ws, "interrupted")
    assert frame == {"type": "interrupted", "reason": "barge_in"}
    assert session.barge_in_cancels == 1


def test_playback_protection_counts_answers_not_audio_chunks(harness, caplog):
    """One protected answer must be ONE protection, however many audio chunks
    the reply arrives in. The first live call logged 11 lines in half a second
    and reported playback_protected=76 for a single protected answer, because
    the reply-audio path runs the guard per chunk and the guard did not check
    whether anything was actually held."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,   # valve must not fire mid-test
    )
    session = state["session"]
    with caplog.at_level(logging.INFO, logger="endpoints.voice_live"):
        with client.websocket_connect(
            "/voice/live?agentKind=sample_voice&businessId=b1"
        ) as ws:
            ws.receive_json()  # ready
            session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
            assert ws.receive_bytes() == _TWO_SECONDS
            session.push(RealtimeEvent(type="user_turn_end"))
            session.push(RealtimeEvent(type="playback_flush"))
            # One reply, eight chunks — the shape that inflated the counter.
            for _ in range(8):
                session.push(RealtimeEvent(type="audio", audio=b"REPLY", reply=True))
                assert ws.receive_bytes() == b"REPLY"
    protections = [
        r for r in caplog.records if "still hearing the answer" in r.getMessage()
    ]
    assert len(protections) == 1, [r.getMessage() for r in protections]


# --- Who ended the call? -----------------------------------------------------
# Both teardown paths used to be completely silent: a clean upstream close ends
# events() by returning None, and a client disconnect just returns from its
# pump. Neither logged anything, so "the conversation keeps ending" was
# unfalsifiable from the logs and had to be chased by reproduction instead.


def test_provider_ending_the_stream_is_logged(harness, caplog):
    client, state = harness
    session = state["session"]
    # Queued BEFORE the socket opens, deliberately. The provider pump then
    # finds end-of-stream already sitting in the queue and completes the moment
    # it is scheduled, so "the provider won the teardown race" is a fact rather
    # than a coin flip. Poking the queue mid-call instead is a cross-thread
    # `put_nowait` against the TestClient portal's loop, which does not reliably
    # wake a pump parked in `queue.get()`.
    #
    # Not asserted here: that the endpoint then closes our socket. It does, but
    # only after awaiting the cancelled CLIENT pump, and TestClient's
    # server-side `receive()` cannot be cancelled without client traffic — so
    # reading for the close deadlocks the test rather than observing it. Both
    # log lines are emitted before that await, which is what makes them
    # assertable at all.
    session.end_stream()
    with caplog.at_level(logging.INFO, logger="endpoints.voice_live"):
        with client.websocket_connect(
            "/voice/live?agentKind=sample_voice&businessId=b1"
        ) as ws:
            ws.receive_json()   # ready
    messages = [r.getMessage() for r in caplog.records]
    assert any("provider ended the event stream" in m for m in messages), messages
    assert any("session ended by the provider" in m for m in messages), messages


def test_client_disconnect_is_logged(harness, caplog):
    client, state = harness
    with caplog.at_level(logging.INFO, logger="endpoints.voice_live"):
        with client.websocket_connect(
            "/voice/live?agentKind=sample_voice&businessId=b1"
        ) as ws:
            ws.receive_json()  # ready
    messages = [r.getMessage() for r in caplog.records]
    assert any("client disconnected" in m for m in messages), messages
    assert any("session ended by the client" in m for m in messages), messages


# ── She must not cut herself off on a hallucinated transcript ────────────────
# The delta-confirm exists for CUT LATENCY: confirm the barge-in from the
# caller's partial transcript instead of waiting for the commit. But a
# transcript delta is not evidence the caller spoke — on a live mic the
# recognizer invents words from room noise and from her own voice echoing back
# ("مبيعانش", "I'm doing a tout blurs" — real examples from one client call),
# and each one cancelled her mid-word ~1.4s into every reply.


def test_hallucinated_delta_does_not_cancel_her_mid_sentence(harness):
    """No speech-onset over her voice = not barge-in context = no cancel."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="audio", audio=b"HER-REPLY"))
        assert ws.receive_bytes() == b"HER-REPLY"
        # A substantive-looking delta with NO preceding `interrupted`.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="مبيعانش", final=False))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frames = [ws.receive_json(), ws.receive_json()]
    assert [f["type"] for f in frames] == ["transcript", "transcript"]
    assert frames[-1]["text"] == "marker"          # no flush frame between
    assert session.barge_in_cancels == 0, "she cancelled herself on noise"


def test_real_barge_in_delta_still_cuts_immediately(harness):
    """The latency win this branch exists for, preserved: speech detected over
    her voice, then words -> cut on the DELTA, not at the commit."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=b"HER-REPLY"))
        assert ws.receive_bytes() == b"HER-REPLY"
        session.push(RealtimeEvent(type="interrupted"))      # onset over her voice
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no, the best branch instead", final=False))
        frame = _read_until(ws, "interrupted")
    assert frame == {"type": "interrupted", "reason": "barge_in"}
    assert session.barge_in_cancels == 1


def test_confirmed_barge_in_is_counted(harness, caplog):
    """`barge_ins` only ever counted the provider's `interrupted` EVENT, so a
    confirm-driven cancel — the one that actually stops her — was invisible.
    Every affected session read `barge_ins=0` while she was cut on every turn,
    which is why the logs looked healthy through a live call that was not."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    with caplog.at_level(logging.INFO, logger="voice.live.metrics"):
        with client.websocket_connect(
            "/voice/live?agentKind=sample_voice&businessId=b1"
        ) as ws:
            ws.receive_json()
            session.push(RealtimeEvent(type="audio", audio=b"HER-REPLY"))
            assert ws.receive_bytes() == b"HER-REPLY"
            session.push(RealtimeEvent(type="interrupted"))
            session.push(RealtimeEvent(type="transcript", role="user",
                                       text="actually wait", final=False))
            _read_until(ws, "interrupted")
    session_line = [
        r.getMessage() for r in caplog.records if "metrics: session" in r.getMessage()
    ]
    assert session_line, "no session metrics line"
    assert "confirmed_cuts=1" in session_line[0], session_line[0]


# ── Onset audibility: an interruption vs. an ordinary turn ──────────────────
# `interrupted` is raised only while the provider is still GENERATING, and it
# generates ~4x realtime. So the provider's own label answers "was she talking
# over the assistant?" for one shape and gets the other backwards:
#
#   * onset with audio still queued in the browser  -> interruption, even
#     though the provider calls it `speech_started_idle`;
#   * onset with an EMPTY queue                     -> ordinary turn-taking,
#     and anything queued by the time her (slower) transcript comes back was
#     forwarded AFTERWARDS — her holding line, or the reply to this very
#     utterance.
#
# The confirmed path used to flush unconditionally, so the second shape threw
# away the answer to the caller's own question mid-word. Live 2026-08-05: the
# filler «إيه، ثانية وحدة» never played and the last line of the call ended at
# «تمام — فتحت».


def test_confirmed_turn_does_not_flush_audio_that_began_after_her_onset(harness):
    """Ordinary turn-taking: silence at onset, then the holding line starts,
    then Azure's slower ASR finally returns her words. The confirmation must
    not destroy the line she is hearing right now."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,   # valve must not fire mid-test
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        # Nothing queued: she is taking a normal turn, not interrupting.
        session.push(RealtimeEvent(type="speech_started_idle"))
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        # The holding line is out-of-band, so it carries reply=False and the
        # reply-audio guard above never sees it.
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="ابغى افتح تذكرة صيانة", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frames = [ws.receive_json(), ws.receive_json()]
    kinds = [f["type"] for f in frames]
    assert "interrupted" not in kinds, (
        "the confirmation flushed the holding line she was still hearing: %s" % kinds
    )


def test_barge_in_over_queued_audio_cuts_even_though_the_provider_is_idle(harness):
    """The other direction, which must not be traded away: she is still
    HEARING the answer when the caller starts speaking. The provider reports
    `speech_started_idle` (it finished generating seconds ago) — the cut has
    to happen anyway."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS      # ~2s still unplayed
        session.push(RealtimeEvent(type="speech_started_idle"))
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="لا، خلاص، الفرع الثاني", final=True))
        # Marker last, and read WITHOUT blocking on the flush: a fix that
        # over-corrects (gating the cut on the provider's `interrupted` label
        # instead of on what the caller could hear) emits no flush at all, and
        # a `_read_until` would hang here rather than fail.
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        # [0] is the echo of her own transcript, which the pump forwards
        # before it confirms anything.
        frames = [ws.receive_json(), ws.receive_json()]
    assert frames[1] == {"type": "interrupted", "reason": "barge_in"}, frames


def test_an_ordinary_turn_is_a_confirmation_not_a_cut(harness, caplog):
    """`confirmed_cuts` counted every confirmation, i.e. every substantive
    caller turn — a 185s live call read confirmed_cuts=8 across 9 user turns
    and was read as "she is stopped on every turn". The two numbers have to be
    separable."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    with caplog.at_level(logging.INFO, logger="voice.live.metrics"):
        with client.websocket_connect(
            "/voice/live?agentKind=sample_voice&businessId=b1"
        ) as ws:
            ws.receive_json()  # ready
            session.push(RealtimeEvent(type="speech_started_idle"))
            session.push(RealtimeEvent(type="user_turn_end"))
            session.push(RealtimeEvent(type="playback_flush"))
            session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
            assert ws.receive_bytes() == _TWO_SECONDS
            session.push(RealtimeEvent(type="transcript", role="user",
                                       text="ابغى افتح تذكرة صيانة", final=True))
            session.push(RealtimeEvent(type="transcript", role="assistant",
                                       text="marker", final=False))
            assert ws.receive_json()["type"] == "transcript"
    session_line = [
        r.getMessage() for r in caplog.records if "metrics: session" in r.getMessage()
    ]
    assert session_line, "no session metrics line"
    assert "confirmed_cuts=0" in session_line[0], session_line[0]
    assert "confirmations=1" in session_line[0], session_line[0]


# ── barge_in_enabled = False: she keeps the floor ───────────────────────────
# A scripted demo does not need interruption, and every mid-sentence cut,
# silently-dropped answer and rapid-utterance stall rides the same cancel.


def test_barge_in_disabled_never_cancels_her(harness):
    """Substantive words over her voice, with the onset the gate requires —
    and she still is not cut."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0, barge_in_enabled=False,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="audio", audio=b"HER-REPLY"))
        assert ws.receive_bytes() == b"HER-REPLY"
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no, the best branch instead", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frames = [ws.receive_json(), ws.receive_json()]
    # The user transcript and the marker — and NO interrupted/flush frame.
    assert [f["type"] for f in frames] == ["transcript", "transcript"]
    assert frames[-1]["text"] == "marker"
    assert session.barge_in_cancels == 0


def test_barge_in_disabled_keeps_the_legacy_path_quiet_too(harness):
    """Window 0 (legacy) must honour the switch as well — that path flushes on
    the raw `interrupted` event without waiting for any verdict."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=0, barge_in_enabled=False,
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frame = ws.receive_json()
    assert frame["type"] == "transcript" and frame["text"] == "marker"


def test_barge_in_enabled_is_the_default(harness):
    """The switch is opt-out: every existing tenant keeps interruption."""
    assert LiveVoiceConfig().barge_in_enabled is True


# ── Periodic in-session budget charging ─────────────────────────────────────
# The handshake advance + teardown charge alone left a RUNNING session
# contributing only its 30s advance to the persisted daily tally however long
# it had actually run — concurrent sessions could accrue hours against a
# 1-hour cap before any of it was recorded, and a replica killed mid-session
# recorded 30s for a 15-minute call. These tests drive the periodic charger
# with a controllable clock so none of them sleeps for a real minute.


class _AdvancingClock:
    """`time`-module stand-in: real monotonic plus a test-advanced offset.

    Patched in as `live_ep.time`, so only the endpoint module's own reads
    shift; asyncio's loop clock (which paces the charger's sleep and the
    session ceiling) stays real.
    """

    def __init__(self):
        self._offset = 0.0

    def advance(self, seconds):
        self._offset += seconds

    def monotonic(self):
        return time.monotonic() + self._offset


def _budget_harness(state, monkeypatch, reserve, interval=0.05):
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        handshake_charge_sec=30.0, budget_charge_interval_sec=interval,
    )
    # The prod floor (5s) exists so raw operator JSON can't turn the charger
    # into a per-tick DB write; shrink it here so the tests run in real time.
    monkeypatch.setattr(live_ep, "_BUDGET_CHARGE_INTERVAL_FLOOR_SEC", 0.01)
    clock = _AdvancingClock()
    monkeypatch.setattr(live_ep, "time", clock)
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", reserve)
    return clock


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert predicate(), "condition not reached within the wait budget"


def test_a_running_session_charges_the_budget_periodically(harness, monkeypatch):
    """Mid-session, the charger books the elapsed-but-uncharged delta: 90s
    into a call whose handshake advanced 30s, the tally must gain the
    missing 60 — not stay frozen at 30 until teardown."""
    client, state = harness
    charges = []

    def _reserve(key, seconds, **kw):
        charges.append(seconds)
        return sum(charges)  # truthy new total, the RPC path's shape

    clock = _budget_harness(state, monkeypatch, _reserve)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready — the handshake advance is already booked
        assert charges == [30]
        clock.advance(90)
        _wait_until(lambda: sum(charges) >= 90)
    assert 60 in charges, charges


def test_teardown_books_exactly_the_uncharged_remainder(harness, monkeypatch):
    """No double-counting: the finally charges `elapsed - charged_so_far`
    (handshake advance + every periodic delta), so the session's recorded
    total is exactly its elapsed seconds — the same number the legacy
    two-charge scheme recorded. And the charger is cancelled BEFORE that
    final charge, so nothing can land after it."""
    client, state = harness
    charges = []

    def _reserve(key, seconds, **kw):
        charges.append(seconds)
        return sum(charges)

    clock = _budget_harness(state, monkeypatch, _reserve)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        clock.advance(100)
        _wait_until(lambda: sum(charges) >= 100)  # at least one periodic delta
        clock.advance(25)  # a tail the charger may or may not tick for
    # The with-block exit waits for the app teardown (same guarantee
    # test_close_time_budget_error_does_not_skip_session_close relies on),
    # so the final charge has happened by here. A few seconds of slack
    # because the REAL time the test itself took also elapses on the clock.
    assert 125 <= sum(charges) <= 130, charges
    assert len(charges) >= 3, charges  # handshake, >=1 periodic delta, final

    # Cancellation ordering: the charger must never fire past teardown's
    # final charge — advance the clock well past several intervals and
    # require the ledger to stay put.
    booked = len(charges)
    clock.advance(500)
    time.sleep(0.2)
    assert len(charges) == booked, "the periodic charger fired after the final charge"


def test_an_over_cap_periodic_charge_is_not_rebooked_at_teardown(harness, monkeypatch):
    """reserve_audio_seconds RECORDS an over-cap charge (clamped) before
    raising BudgetExhausted, so those seconds count as charged — the
    teardown must not book them a second time."""
    client, state = harness
    from voice.live.budget import BudgetExhausted

    charges = []

    def _reserve(key, seconds, **kw):
        charges.append(seconds)
        if len(charges) == 2:  # the first periodic delta crosses the cap
            raise BudgetExhausted("spent")
        return sum(charges)

    clock = _budget_harness(state, monkeypatch, _reserve)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        clock.advance(100)
        _wait_until(lambda: len(charges) >= 2)
    # charges[1] is the 70s over-cap delta — recorded clamped, so charged
    # advanced past it. The teardown remainder is only the few REAL seconds
    # the test itself took, never the 70 again.
    assert charges[1] == 70, charges
    assert all(c <= 5 for c in charges[2:]), charges


def test_a_periodic_charge_failure_does_not_kill_the_call(harness, monkeypatch):
    """The budget layer is fail-open by design: a DB blip inside the charger
    must not take down a live call, and teardown must still close the
    provider session."""
    client, state = harness
    session = state["session"]
    calls = {"n": 0}

    def _reserve(key, seconds, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 30  # the handshake advance succeeds
        raise ValueError("AI Supabase env vars missing")

    clock = _budget_harness(state, monkeypatch, _reserve)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        clock.advance(90)
        _wait_until(lambda: calls["n"] >= 2)  # the charger tried, and blew up
        # The call is still alive: provider audio still reaches the client.
        session.push(RealtimeEvent(type="audio", audio=b"STILL-ALIVE"))
        assert ws.receive_bytes() == b"STILL-ALIVE"
    assert session.closed


def test_budget_interval_zero_disables_the_periodic_charge(harness, monkeypatch):
    """<= 0 is the rollback lever: exactly the legacy two-charge scheme —
    the handshake advance and one teardown remainder, nothing in between."""
    client, state = harness
    charges = []

    def _reserve(key, seconds, **kw):
        charges.append(seconds)
        return sum(charges)

    clock = _budget_harness(state, monkeypatch, _reserve, interval=0)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        clock.advance(90)
        time.sleep(0.15)  # several would-be intervals
    # Handshake + teardown only; the teardown books the whole remainder
    # (90 elapsed - 30 advance, ± the test's own real sub-seconds).
    assert len(charges) == 2, charges
    assert 60 <= charges[1] <= 65, charges


# ---------------------------------------------------------------------------
# Call recording wiring (voice/live/recorder.py)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.inbound = []
        self.outbound = []
        self.flushes = 0
        self.finalized = False
        _FakeRecorder.instances.append(self)

    def add_inbound(self, pcm):
        self.inbound.append(pcm)

    def add_outbound(self, pcm):
        self.outbound.append(pcm)

    def mark_playback_flush(self):
        self.flushes += 1

    async def finalize_and_store(self, **kw):
        self.finalized = True
        return None


@pytest.fixture
def recorder_harness(harness, monkeypatch):
    _FakeRecorder.instances = []
    monkeypatch.setattr(live_ep, "VoiceCallRecorder", _FakeRecorder)
    monkeypatch.setattr(live_ep, "recording_enabled", lambda: True)
    return harness


def test_both_audio_directions_reach_the_recorder(recorder_harness):
    """Caller mic frames and forwarded provider audio both land in the
    recorder, and teardown finalizes it — the whole capture contract."""
    client, state = recorder_harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x01\x02" * 160)
        session.push(RealtimeEvent(type="audio", audio=b"REPLY-AUDIO"))
        assert ws.receive_bytes() == b"REPLY-AUDIO"
    _wait_until(lambda: _FakeRecorder.instances
                and _FakeRecorder.instances[0].finalized)
    rec = _FakeRecorder.instances[0]
    assert rec.inbound == [b"\x01\x02" * 160]
    assert rec.outbound == [b"REPLY-AUDIO"]
    # The call's span linkage key is threaded into the recorder.
    assert rec.kw["call_id"]


def test_recording_can_be_disabled(recorder_harness, monkeypatch):
    monkeypatch.setattr(live_ep, "recording_enabled", lambda: False)
    client, state = recorder_harness
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x01\x02" * 160)
    assert _FakeRecorder.instances == []


def test_recorder_failure_at_init_does_not_break_the_call(recorder_harness, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("recorder init failed")

    monkeypatch.setattr(live_ep, "VoiceCallRecorder", _boom)
    client, state = recorder_harness
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
        state["session"].push(RealtimeEvent(type="audio", audio=b"OK"))
        assert ws.receive_bytes() == b"OK"


def test_a_real_call_is_not_marked_simulated(recorder_harness):
    client, state = recorder_harness
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
    _wait_until(lambda: _FakeRecorder.instances
                and _FakeRecorder.instances[0].finalized)
    assert _FakeRecorder.instances[0].kw["simulated"] is False


def test_sim_run_flag_marks_the_recording_simulated(recorder_harness, monkeypatch):
    """A live-mode eval/scenario run self-declares with ?simRun=1 — its
    recording must carry the flag so the UI can badge the session. (The
    TestClient peer isn't loopback, so trust is stubbed; the gate's own
    logic is covered below.)"""
    monkeypatch.setattr(live_ep, "_trusted_loopback", lambda ws: True)
    client, state = recorder_harness
    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&simRun=1"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
    _wait_until(lambda: _FakeRecorder.instances
                and _FakeRecorder.instances[0].finalized)
    assert _FakeRecorder.instances[0].kw["simulated"] is True


def test_sim_run_flag_is_ignored_from_a_non_loopback_peer(recorder_harness):
    """A public caller must not be able to self-badge 'sim' and get its
    session dismissed as harness noise — the tag is honoured only from the
    trusted loopback the in-process harnesses connect over. The TestClient
    peer ('testclient') is exactly such an untrusted remote."""
    client, state = recorder_harness
    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&simRun=1"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
    _wait_until(lambda: _FakeRecorder.instances
                and _FakeRecorder.instances[0].finalized)
    assert _FakeRecorder.instances[0].kw["simulated"] is False


def test_sim_run_zero_means_not_simulated(recorder_harness, monkeypatch):
    """bool('0') is True in Python — ?simRun=0 must parse as OFF."""
    monkeypatch.setattr(live_ep, "_trusted_loopback", lambda ws: True)
    client, state = recorder_harness
    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1&simRun=0"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
    _wait_until(lambda: _FakeRecorder.instances
                and _FakeRecorder.instances[0].finalized)
    assert _FakeRecorder.instances[0].kw["simulated"] is False


def test_non_uuid_business_id_is_dropped_from_the_recording(recorder_harness):
    """ai_voice_recordings.business_id is a uuid column, but a tenant's
    merchant id can be any format — passing it raw would 22P02 the insert
    and silently lose the whole recording for that tenant."""
    client, state = recorder_harness
    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=demo-merchant-7"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
    _wait_until(lambda: _FakeRecorder.instances
                and _FakeRecorder.instances[0].finalized)
    assert _FakeRecorder.instances[0].kw["business_id"] is None


def test_uuid_business_id_reaches_the_recording(recorder_harness):
    client, state = recorder_harness
    bid = "9f8e7d6c-5b4a-3210-9f8e-7d6c5b4a3210"
    with client.websocket_connect(
        f"/voice/live?agentKind=sample_voice&businessId={bid}"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
    _wait_until(lambda: _FakeRecorder.instances
                and _FakeRecorder.instances[0].finalized)
    assert _FakeRecorder.instances[0].kw["business_id"] == bid


# ---- outbound simulated call: callCase / {callee_name} ----------------------
def test_outbound_call_case_reaches_the_agent_builder(harness, monkeypatch):
    """`?callCase=C06` must arrive as params["call_case"] for an outbound kind,
    and the kind must pass its build's deployment-scope gate (it is an EXTRA
    kind of that scope, not its main one)."""
    use_scope(monkeypatch, make_scope("tenant_a", extra_kinds=("tenant_a_outbound", "tenant_a_sales")))
    client, _ = harness
    seen = {}

    def _build(kind, params):
        seen["kind"], seen["params"] = kind, params
        return object(), object()

    monkeypatch.setattr(live_ep, "build_agent_for_kind", _build)
    with client.websocket_connect(
        "/voice/live?agentKind=tenant_a_outbound&businessId=b1&callCase=C06"
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
    assert seen["kind"] == "tenant_a_outbound"
    assert seen["params"]["call_case"] == "C06"


def test_outbound_persona_receives_callee_name(harness, monkeypatch):
    """The agent's persona_variables must be rendered into the persona handed
    to the provider session."""
    from types import SimpleNamespace

    full_build(monkeypatch)
    client, state = harness
    personas = []
    agent = SimpleNamespace(persona_variables={"callee_name": "أستاذ فهد"})
    monkeypatch.setattr(live_ep, "build_agent_for_kind", lambda kind, params: (agent, object()))
    monkeypatch.setattr(
        live_ep, "_active_profile_for",
        lambda kind: SimpleNamespace(resolved_prompts={"sample_outbound_persona": "Hello {callee_name}."}),
    )
    # Use the REAL persona resolver (the harness stubs it).
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", _REAL_RESOLVE_PERSONA)
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", persona_prompt_slug="sample_outbound_persona",
    )
    session = state["session"]
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: personas.append(kw["persona"]) or session)
    with client.websocket_connect("/voice/live?agentKind=sample_outbound&businessId=b1") as ws:
        assert ws.receive_json()["type"] == "ready"
    assert personas and "أستاذ فهد" in personas[0] and "{callee_name}" not in personas[0]


# ---- (c) + V3, voice E2E 2026-09-28 ----------------------------------------
def test_greeting_turn_end_is_marked_once(harness):
    """App contract: `{"type":"turn_end","reply":false,"greeting":true}` on the
    greeting response's turn_end, the key present only then. The app keeps
    that turn's assistant text out of the chat (one greeting bubble per call
    otherwise)."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", greeting_trigger="Greet the caller."
    )
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="turn_end"))  # Gemini: no verdict of its own
        assert ws.receive_json() == {"type": "turn_end", "reply": False, "greeting": True}
        session.push(RealtimeEvent(type="turn_end", turn_reply=True))
        assert ws.receive_json() == {"type": "turn_end", "reply": True}
        session.push(RealtimeEvent(type="turn_end", turn_reply=False))
        assert ws.receive_json() == {"type": "turn_end", "reply": False}


def test_no_greeting_key_without_a_greeting(harness):
    client, state = harness
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="turn_end", turn_reply=False))
        assert ws.receive_json() == {"type": "turn_end", "reply": False}


def test_gemini_holding_line_transcript_is_marked_out_of_band(harness, monkeypatch):
    """Gemini has no out-of-band speech: the holding line rides a WHEN_IDLE
    tool result and came back as an ordinary transcript — its own chat bubble
    («ثواني بس.», 2026-09-28). The fragments spelling OUR line are labelled
    out_of_band like Azure's; the model's own words after it are not."""
    import asyncio as _a

    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0.05, progress_spoken="ثواني بس.",
    )
    session = state["session"]
    session.supports_out_of_band_speech = False

    async def _slow(**kwargs):
        await _a.sleep(0.6)
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    frames = []
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "revenue?"}))
        for _ in range(50):
            if any(p.get("spoken") == "ثواني بس." for _c, p, _m in session.tool_results):
                break
            time.sleep(0.02)
        # How Gemini transcribed it live: split, with its own punctuation.
        session.push(RealtimeEvent(type="transcript", role="assistant", text="ثواني", final=False))
        session.push(RealtimeEvent(type="transcript", role="assistant", text=" بس.", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        session.push(RealtimeEvent(type="transcript", role="assistant", text="Nine hundred.", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        seen_turn_ends = 0
        for _ in range(20):
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "turn_end":
                seen_turn_ends += 1
                if seen_turn_ends == 2:
                    break

    captions = [f for f in frames if f.get("type") == "transcript"]
    holding = [f for f in captions if f["text"] in ("ثواني", " بس.")]
    assert holding and all(f.get("out_of_band") is True for f in holding), captions
    own = [f for f in captions if f["text"] == "Nine hundred."]
    assert own and "out_of_band" not in own[0], captions


def test_provider_start_failure_does_not_leak_upstream_text(harness):
    """Retest 2026-09-28: the 4502 close carried Azure's raw session.update
    error to the browser. The client gets a generic reason; the detail is
    logged server-side."""
    from starlette.websockets import WebSocketDisconnect as WSDisconnect
    from voice.live.base import RealtimeProviderError

    client, state = harness

    async def _fail():
        raise RealtimeProviderError(
            "azure voice live: session.update rejected: 'session.voice' should be 'alloy', …")

    state["session"].start = _fail
    with pytest.raises(WSDisconnect) as exc_info:
        with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
            ws.receive_json()
    assert exc_info.value.code == live_ep.CLOSE_PROVIDER_ERROR
    assert "session.update" not in (exc_info.value.reason or "")
    assert exc_info.value.reason == "Voice provider unavailable."


# ── Talk-over that the provider labels `speech_started_idle` ────────────────
# Live 2026-09-29 (a deployment's voice, azure_voice_live + azure-speech): the
# session summary read `speech_started_idle=6 barge_ins=0 confirmed_cuts=6
# confirmations=6` and the caller said "interrupting doesn't work at all".
# Three engine-side causes, pinned below:
#   1. an audible idle onset armed nothing, so the cut waited for the commit
#      AND the transcript — i.e. until after the caller had stopped talking;
#   2. azure-speech delivers the caller's words only after the commit, so
#      even an armed delta-confirm cannot cut DURING the talk-over — hence the
#      sustained-speech cut (`barge_in_sustain_sec`);
#   3. the formality flush of an ordinary turn (empty queue) was counted and
#      labelled as a barge-in cut, so confirmed_cuts == user turns.


def _azure_like(state):
    """azure_voice_live posture: one tool result per answer (no sentence
    streaming) — the only shape the sustained cut arms on."""
    state["session"].supports_streamed_tool_results = False
    return state["session"]


def _sustain_cfg(sustain: float) -> LiveVoiceConfig:
    return LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        interrupt_confirm_window_sec=5.0, barge_in_sustain_sec=sustain,
    )


def test_sustained_talk_over_cuts_before_any_transcript(harness, caplog):
    import time as _time

    client, state = harness
    state["cfg"] = _sustain_cfg(0.1)
    session = _azure_like(state)
    with caplog.at_level(logging.INFO, logger="voice.live.metrics"):
        with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
            ws.receive_json()  # ready
            session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
            assert ws.receive_bytes() == _TWO_SECONDS      # she is audible
            session.push(RealtimeEvent(type="speech_started_idle"))
            _time.sleep(0.4)                                 # caller keeps talking
            session.push(RealtimeEvent(type="transcript", role="assistant",
                                       text="marker", final=False))
            first = ws.receive_json()
            assert first == {"type": "interrupted", "reason": "barge_in"}, first
            assert session.barge_in_cancels == 1
            # Her words arrive after the commit: bookkeeping only, no 2nd cut.
            session.push(RealtimeEvent(type="speech_stopped"))
            session.push(RealtimeEvent(type="user_turn_end"))
            session.push(RealtimeEvent(type="playback_flush"))
            session.push(RealtimeEvent(type="transcript", role="user",
                                       text="stop, the other branch", final=True))
            _read_until(ws, "transcript")
    line = [r.getMessage() for r in caplog.records if "metrics: session" in r.getMessage()][0]
    assert "sustained_cuts=1" in line and "confirmed_cuts=0" in line, line


def test_a_blip_that_stops_inside_the_window_does_not_cut(harness):
    import time as _time

    client, state = harness
    state["cfg"] = _sustain_cfg(0.2)
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="speech_started_idle"))
        session.push(RealtimeEvent(type="speech_stopped"))
        _time.sleep(0.4)
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frame = ws.receive_json()
    assert frame["type"] == "transcript" and frame["text"] == "marker", frame
    assert session.barge_in_cancels == 0


def test_sustain_off_by_default_keeps_transcript_confirmed_cuts(harness):
    import time as _time

    client, state = harness
    assert LiveVoiceConfig().barge_in_sustain_sec == 0
    state["cfg"] = _sustain_cfg(0.0)
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="speech_started_idle"))
        _time.sleep(0.3)
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frame = ws.receive_json()
    assert frame["type"] == "transcript" and frame["text"] == "marker", frame


@pytest.mark.parametrize("sustain,cuts", [(0.0, False), (5.0, True)])
def test_audible_idle_onset_confirms_on_the_first_delta_only_under_the_lever(harness, sustain, cuts):
    """Cause 1, gated (eval review I-1): with `barge_in_sustain_sec` > 0 an
    audible `speech_started_idle` is barge-in context and the first
    substantive delta cuts her. With the lever at 0 (every non-business
    tenant) nothing changes: no cut until the transcript final."""
    client, state = harness
    state["cfg"] = _sustain_cfg(sustain)
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="speech_started_idle"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="no wait, the other one", final=False))
        session.push(RealtimeEvent(type="transcript", role="assistant",
                                   text="marker", final=False))
        frames = [ws.receive_json(), ws.receive_json()]
    if cuts:
        assert frames[1] == {"type": "interrupted", "reason": "barge_in"}, frames
        assert session.barge_in_cancels == 1
    else:
        assert [f["type"] for f in frames] == ["transcript", "transcript"], frames
        assert session.barge_in_cancels == 0


def _bytes(ws, limit=10):
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("bytes") is not None:
            return msg["bytes"]
    raise AssertionError("no audio frame")


def test_reply_queued_after_a_sustained_cut_is_not_flushed_by_the_verdict(harness):
    """Eval review I-2: after the sustained cut the caller no longer hears
    her; the reply to the caller's own question, queued afterwards, must not
    be thrown away when the (late) transcript confirms."""
    import time as _time

    client, state = harness
    state["cfg"] = _sustain_cfg(0.1)
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="speech_started_idle"))
        _time.sleep(0.4)
        session.push(RealtimeEvent(type="transcript", role="assistant", text="m1", final=False))
        assert _read_until(ws, "interrupted") == {"type": "interrupted", "reason": "barge_in"}
        session.push(RealtimeEvent(type="speech_stopped"))
        session.push(RealtimeEvent(type="user_turn_end"))
        session.push(RealtimeEvent(type="playback_flush"))
        # The reply to THIS utterance starts arriving before its words do.
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS, reply=True))
        # (A formality `input_end` may precede it: nothing was queued then.)
        assert _bytes(ws) == _TWO_SECONDS
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="what about tomorrow", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant", text="marker", final=False))
        seen = [ws.receive_json(), ws.receive_json()]
    assert all(f["type"] != "interrupted" for f in seen), seen


_ANSWER3 = "Revenue is up this month. Omar did best of all. Sara is second overall."
_ONE_SECOND = b"\x00" * (24000 * 2)


def _say_exactly_turns(session):
    return [t for t in session.text_turns if "Revenue" in t or "Omar" in t or "Sara" in t]


def _deliver_answer(client_ws, session, monkeypatch, answer=_ANSWER3, seconds=3):
    async def _answer(**kwargs):
        return DelegateResult(spoken=answer, display=answer, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _answer)
    session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                               arguments={"query": "how am I doing"}))
    _read_until(client_ws, "display")
    for _ in range(seconds):
        session.push(RealtimeEvent(type="audio", audio=_ONE_SECOND))
        assert _bytes(client_ws) == _ONE_SECOND
    session.push(RealtimeEvent(type="turn_end"))       # the answer's response is generated


def _cut_then_noise(ws, session, played_ms=None):
    import json as _json
    import time as _time

    session.push(RealtimeEvent(type="speech_started_idle"))
    _time.sleep(0.4)
    session.push(RealtimeEvent(type="transcript", role="assistant", text="m1", final=False))
    _read_until(ws, "interrupted")
    if played_ms is not None:
        ws.send_text(_json.dumps({"type": "played", "ms": played_ms}))
        _time.sleep(0.1)
    session.push(RealtimeEvent(type="user_turn_end"))
    session.push(RealtimeEvent(type="transcript", role="user", text="", final=True))
    _time.sleep(0.5)


def test_a_cut_mid_answer_re_says_the_whole_unheard_remainder_once(harness, monkeypatch):
    """Re-review R-1: the caller heard ~1.3s of a 3s, three-sentence answer
    when a noise burst cut her — sentences 2 AND 3 are re-said, once."""
    client, state = harness
    state["cfg"] = _sustain_cfg(0.1)
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        _deliver_answer(ws, session, monkeypatch)
        _cut_then_noise(ws, session, played_ms=1300)
    said = _say_exactly_turns(session)
    assert len(said) == 1, said
    assert "Omar did best of all" in said[0] and "Sara is second overall" in said[0]
    assert "Revenue is up" not in said[0]


def test_an_announce_line_ahead_of_the_answer_is_not_the_answer(harness, monkeypatch):
    """Re-review minor: the answer was delivered while an announce line held
    the floor. Its span must start at the answer's own first audio, or a cut
    during the real answer reads as "fully heard" and is lost."""
    client, state = harness
    state["cfg"] = _sustain_cfg(0.1)
    session = _azure_like(state)

    async def _answer(**kwargs):
        return DelegateResult(spoken=_ANSWER3, display=_ANSWER3, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _answer)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.response_in_flight = True                   # the announce line is speaking
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "how am I doing"}))
        _read_until(ws, "display")
        session.push(RealtimeEvent(type="audio", audio=_ONE_SECOND))     # announce line
        assert _bytes(ws) == _ONE_SECOND
        session.push(RealtimeEvent(type="turn_end"))
        session.response_in_flight = False
        for _ in range(3):                                               # the answer
            session.push(RealtimeEvent(type="audio", audio=_ONE_SECOND))
            assert _bytes(ws) == _ONE_SECOND
        session.push(RealtimeEvent(type="turn_end"))
        _cut_then_noise(ws, session, played_ms=1000 + 1300)
    said = _say_exactly_turns(session)
    assert len(said) == 1 and "Omar did best of all" in said[0], said


def test_late_audio_of_the_cut_response_is_not_forwarded(harness, monkeypatch):
    """No double-speak: audio the cancelled response still emits after the
    cut is dropped until that response ends."""
    import time as _time

    client, state = harness
    state["cfg"] = _sustain_cfg(0.1)
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert _bytes(ws) == _TWO_SECONDS
        session.response_in_flight = True               # still generating
        session.push(RealtimeEvent(type="speech_started_idle"))
        _time.sleep(0.4)
        session.push(RealtimeEvent(type="transcript", role="assistant", text="m1", final=False))
        _read_until(ws, "interrupted")
        session.push(RealtimeEvent(type="audio", audio=b"LATE"))        # cancelled response's tail
        session.push(RealtimeEvent(type="transcript", role="assistant", text="marker", final=False))
        got = []
        while True:
            msg = ws.receive()
            got.append(msg)
            if "marker" in (msg.get("text") or ""):
                break
    assert not any(m.get("bytes") for m in got), got


def test_a_cut_over_model_only_speech_re_says_nothing_stale(harness, monkeypatch):
    """The answer was fully heard; the cut lands on the model's own speech
    afterwards (a farewell). Nothing of the old answer is re-said."""
    client, state = harness
    state["cfg"] = _sustain_cfg(0.1)
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        _deliver_answer(ws, session, monkeypatch, seconds=1)
        # The model's own line, after the answer's response ended.
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert _bytes(ws) == _TWO_SECONDS
        _cut_then_noise(ws, session, played_ms=1500)     # past the answer's 1s of audio
    assert _say_exactly_turns(session) == []


def test_the_answer_watchdog_does_not_duplicate_the_re_say(harness, monkeypatch):
    import time as _time

    client, state = harness
    cfg = _sustain_cfg(0.1)
    cfg.answer_watchdog_sec = 0.2
    state["cfg"] = cfg
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        _deliver_answer(ws, session, monkeypatch)
        _cut_then_noise(ws, session, played_ms=1300)
        _time.sleep(0.8)
    assert len(_say_exactly_turns(session)) == 1, session.text_turns


def test_no_recovery_while_a_new_answer_is_in_flight(harness, monkeypatch):
    """The recovery never cancels a delegated answer (or its holding line)
    that is on its way."""
    client, state = harness
    state["cfg"] = _sustain_cfg(0.1)
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        _deliver_answer(ws, session, monkeypatch)

        async def _slow(**kwargs):
            await asyncio.sleep(3)
            return DelegateResult(spoken="x", display="x", charts=[])

        monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
        session.push(RealtimeEvent(type="tool_call", call_id="c2", tool_name="ask_agent",
                                   arguments={"query": "and tomorrow?"}))
        _cut_then_noise(ws, session, played_ms=1300)
    assert _say_exactly_turns(session) == []


def test_the_sustained_cut_never_fires_during_the_greeting_guard(harness):
    """Eval review I-4: a speaker echoing her own greeting is continuous
    "speech" — the greeting guard must cover the sustained cut too."""
    import time as _time

    client, state = harness
    cfg = _sustain_cfg(0.1)
    cfg.greeting_trigger = "Welcome"
    cfg.greeting_barge_in_guard_sec = 8.0
    state["cfg"] = cfg
    session = _azure_like(state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert _bytes(ws) == _TWO_SECONDS
        session.push(RealtimeEvent(type="speech_started_idle"))
        _time.sleep(0.4)
        session.push(RealtimeEvent(type="transcript", role="assistant", text="marker", final=False))
        frame = ws.receive_json()
    assert frame["type"] == "transcript", frame
    assert session.barge_in_cancels == 0


def test_ordinary_turn_formality_flush_is_not_a_cut(harness, caplog):
    """Cause 3: nothing queued at onset, nothing queued at the verdict — the
    held flush is a formality: `input_end`, not counted as a cut."""
    client, state = harness
    state["cfg"] = _sustain_cfg(0.0)
    session = state["session"]
    with caplog.at_level(logging.INFO, logger="voice.live.metrics"):
        with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
            ws.receive_json()
            session.push(RealtimeEvent(type="speech_started_idle"))
            session.push(RealtimeEvent(type="user_turn_end"))
            session.push(RealtimeEvent(type="playback_flush"))
            session.push(RealtimeEvent(type="transcript", role="user",
                                       text="how many bookings today", final=True))
            frame = _read_until(ws, "interrupted")
    assert frame == {"type": "interrupted", "reason": "input_end"}, frame
    line = [r.getMessage() for r in caplog.records if "metrics: session" in r.getMessage()][0]
    assert "confirmed_cuts=0" in line and "confirmations=1" in line, line


def test_gemini_tool_call_tokens_in_the_output_transcription_never_reach_the_browser(harness):
    """Gemini Live's output transcription carried its raw tool call —
    "startcall:{query:…}" — split across deltas and, once, cut off mid-token.
    None of it was audible (the recording proved it); it existed only in the
    transcript frames the portal's call log is built from. The prose around
    it must survive."""
    client, state = harness
    session = _gemini_shaped(state["session"])
    deltas = [
        "let me check", " for you start", "call:{query:", "How much do I owe",
        " and for which months?}", "let me check, how much you owe…",
        " one sec startcall:{query:Please send me the payment link to",
    ]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        for delta in deltas:
            session.push(RealtimeEvent(type="transcript", role="assistant", text=delta, final=False))
        session.push(RealtimeEvent(type="turn_end", turn_reply=True))
        frames = []
        for _ in range(20):
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "turn_end":
                break
    caption = "".join(_assistant_screen(frames))
    assert "startcall" not in caption and "query" not in caption and "{" not in caption, caption
    assert " ".join(caption.split()) == "let me check for you let me check, how much you owe… one sec", caption
