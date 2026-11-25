"""Talking to the builder assistant (demo_mode/assistant_voice.py,
``WS /admin/demo/assistant/voice``).

The calls here are real: the app, the demo gate, `demo_mode.voice.guard`, the
shared voice handler (endpoints/voice_live.py) on the simulated realtime
provider, the delegate, `run_turn` and the builder assistant's own tool loop
with its allow-list. Faked are only the edges: the profile and model rows,
the sign-in check, the chat model's rounds, the chat-session table and the
admin tools' bodies.

What these pin:

1. A spoken request runs the builder: it performs a tool step (shown as a
   step row in the panel's record of the turn) and the voice speaks a short
   summary, not the whole answer, after a holding line.
2. Typed and spoken turns share one conversation.
3. Every call runs under the demo's voice caps: the day's seconds, the call
   length, the lines open at once; and every spoken request under the
   builder's own message caps.
4. With the demo's voice off there is no microphone and no socket.
5. Only the signed-in visitor, from the demo's own page.
"""
from __future__ import annotations

import json

import pytest

from demo_mode import assistant, assistant_voice, limits, northwind, settings, voice
from tests.admin.test_demo_mode import _FakeServer
from tests.voice.test_custom_agent_voice import SIM_MODEL, _profile, _until, stack  # noqa: F401

URL = "/admin/demo/assistant/voice"
SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"
COOKIE = {"cookie": "waj_admin_token=good-token"}
ANSWER = ("I connected the Northwind tools to a new tool server. "
          "It can now check outages and order status. "
          "[Open Northwind 2](/admin/agent-mcp-servers/s1)")


def _raise(*_a, **_k):
    raise RuntimeError("no database in tests")


@pytest.fixture
def builder(stack, monkeypatch):  # noqa: F811
    """The demo with its voice on, and the builder assistant behind the
    panel's microphone, its admin tools faked."""
    client, state = stack
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_VOICE", "true")
    monkeypatch.setenv("DEMO_VOICE_ENDPOINT", "https://demo-voice.services.ai.azure.com/")
    monkeypatch.setenv(settings.VOICE_KEY_ENV, "test-voice-key")
    # What the demo's process lock-down sets (demo_mode.settings).
    monkeypatch.setenv("VOICE_RECORDING_ENABLED", "0")
    limits.reset_for_tests()
    voice.reset_for_tests()
    monkeypatch.setattr(limits, "_db_take", _raise)
    monkeypatch.setattr(limits, "_db_used", _raise)
    from admin import demo_handlers
    from core.agent_profile import AgentProfileLoader

    demo_handlers._LIVE.clear()
    # The portal's own routes see the same signed-in visitor as the socket.
    import main
    from admin import auth

    overrides = main.app.dependency_overrides
    before = overrides.get(auth.require_superuser)
    overrides[auth.require_superuser] = lambda: {"user_token": "good-token", "user_id": "u-1",
                                                 "email": "ops@example.com"}
    state["profiles"][assistant.BUILDER_SLUG] = _profile(assistant.BUILDER_SLUG,
                                                         profile_id="p-builder")
    monkeypatch.setattr(assistant_voice, "voice_model_id", lambda: "m-rt")
    monkeypatch.setattr(AgentProfileLoader, "fetch_model", classmethod(
        lambda cls, client, model_id, environment=None: SIM_MODEL if model_id == "m-rt" else None))
    server = _FakeServer()
    monkeypatch.setattr(assistant, "_admin_mcp", lambda: server)
    state["server"] = server
    state["rounds"] = [
        ("", [{"name": "mcp_servers", "id": "c1",
               "args": {"action": "create", "name": "Northwind 2",
                        "endpoint": northwind.northwind_endpoint()}}]),
        (ANSWER, []),
    ]
    yield client, state
    if before is None:
        overrides.pop(auth.require_superuser, None)
    else:
        overrides[auth.require_superuser] = before
    limits.reset_for_tests()
    voice.reset_for_tests()
    demo_handlers._LIVE.clear()


def _ready(ws):
    frame, _ = _until(ws, lambda f: f["type"] == "ready")
    _until(ws, lambda f: f["type"] == "turn_end")  # the greeting
    return frame


def _ask(ws, text):
    """Say `text`; return every frame up to the end of the answer."""
    ws.send_json({"type": "text", "text": text})
    _, seen = _until(ws, lambda f: f["type"] == "display", limit=60)
    _, tail = _until(ws, lambda f: f["type"] == "turn_end", limit=60)
    return seen + tail


def _closed(client, url=URL, headers=COOKIE):
    with client.websocket_connect(url, headers=headers) as ws:
        for _ in range(10):
            message = ws.receive()
            if message.get("type") == "websocket.close":
                return message
            if message.get("text") and json.loads(message["text"]).get("type") == "ready":
                raise AssertionError("the call was let in")
    raise AssertionError("the socket did not close")


# --------------------------------------------------------------------------
# 1. A spoken request runs the builder
# --------------------------------------------------------------------------


def test_a_spoken_request_runs_a_tool_step_and_speaks_a_summary(builder):
    from admin import demo_handlers

    client, state = builder
    with client.websocket_connect(f"{URL}?sessionId={SESSION}", headers=COOKIE) as ws:
        ready = _ready(ws)
        frames = _ask(ws, "Connect the Northwind tools")

    assert ready["session_id"] == SESSION
    spoken = [f["text"] for f in frames if f["type"] == "transcript"
              and f["role"] == "assistant" and not f.get("out_of_band")]
    holding = [f["text"] for f in frames if f["type"] == "transcript" and f.get("out_of_band")]
    display = next(f for f in frames if f["type"] == "display")
    # A holding line first, then one short sentence, not the whole answer.
    assert holding and holding[0] in assistant_voice.PROGRESS
    assert spoken == [assistant_voice.spoken_summary(ANSWER)]
    assert spoken[0].startswith("I connected the Northwind tools to a new tool server.")
    assert "/admin/" not in spoken[0] and "[" not in spoken[0]
    assert spoken[0].endswith(assistant_voice.PANEL_POINTER)
    # The panel gets the whole answer, links included.
    assert "(/admin/agent-mcp-servers/s1)" in display["text"]
    # The tool ran through the allow-list, as the signed-in visitor.
    call = state["server"].tools["mcp_servers"].calls[-1]
    assert call["action"] == "create" and call["env"] == "staging"
    # The panel's record of the turn: the step, its link, the visitor's words.
    live = demo_handlers._LIVE[SESSION]
    assert live["done"] is True and live["voice"] is True and live["changed"] is True
    assert live["message"] == "Connect the Northwind tools"
    assert [(s["label"], s["ok"]) for s in live["steps"]] == [("Connected the Northwind tools", True)]
    assert live["steps"][0]["links"] == [{"text": "Open Northwind 2",
                                          "href": "/admin/agent-mcp-servers/s1"}]
    # The brain is the builder, on a voice turn, under the builder's caps.
    assert state["built"][-1]["slug"] == assistant.BUILDER_SLUG
    assert state["turn_contexts"][-1]["voice_turn"] is True
    assert limits.turns_left() == settings.daily_turns() - 1


def test_the_builder_gets_its_own_voice_note_not_the_generic_rules(builder, monkeypatch):
    from runtime.agents.custom import VOICE_TURN_RULES

    client, state = builder
    seen = []
    import langchain_core.messages as messages

    real = messages.SystemMessage

    from runtime.agents import custom

    monkeypatch.setattr(custom, "SystemMessage",
                        lambda content: seen.append(content) or real(content=content))
    with client.websocket_connect(URL, headers=COOKIE) as ws:
        _ready(ws)
        _ask(ws, "Connect the Northwind tools")
    context = "\n".join(seen)
    assert assistant_voice.VOICE_NOTE in context and VOICE_TURN_RULES not in context


def test_the_history_endpoint_shows_a_spoken_turn(builder):
    client, _ = builder
    with client.websocket_connect(f"{URL}?sessionId={SESSION}", headers=COOKIE) as ws:
        _ready(ws)
        _ask(ws, "Connect the Northwind tools")
    live = client.get(f"/admin/demo/assistant/history?session_id={SESSION}",
                      headers=COOKIE).json()["live"]
    assert live["voice"] is True and live["done"] is True and live["id"]
    assert live["message"] == "Connect the Northwind tools" and live["steps"]


@pytest.mark.parametrize("text", [
    "Found two agents. **Support desk** and the [Sales assistant](/admin/agents?x=1) are live.",
    "- Created it\n- Tested it",
    "",
])
def test_the_spoken_summary_is_plain_and_short(text):
    said = assistant_voice.spoken_summary(text)
    assert "*" not in said and "/admin" not in said and "[" not in said
    assert len(said) <= 220 + len(assistant_voice.PANEL_POINTER) + 1
    assert chr(0x2014) not in said


# --------------------------------------------------------------------------
# 2. One conversation
# --------------------------------------------------------------------------


def test_typed_and_spoken_turns_share_one_conversation(builder):
    client, state = builder
    resp = client.post("/admin/demo/assistant/chat",
                       json={"message": "Connect the Northwind tools", "session_id": SESSION},
                       headers=COOKIE)
    assert resp.status_code == 200 and '"type": "step"' in resp.text
    typed = state["identities"][-1]
    # What the typed turn made is told to the spoken one.
    assert "Connected the Northwind tools" in assistant.earlier_note(SESSION)
    state["rounds"] = [("Yes, the Northwind tools are attached.", [])]
    with client.websocket_connect(f"{URL}?sessionId={SESSION}", headers=COOKIE) as ws:
        _ready(ws)
        _ask(ws, "Is it done?")
    spoken = state["identities"][-1]
    assert typed.session_id == spoken.session_id == SESSION
    assert typed.user_id == spoken.user_id == "u-1"
    assert typed.session_metadata["agent_slug"] == spoken.session_metadata["agent_slug"] \
        == assistant.BUILDER_SLUG


def test_a_spoken_request_waits_for_a_typed_turn_still_running(builder):
    from admin import demo_handlers

    client, state = builder
    demo_handlers._new_live(SESSION, "u-1")  # a typed turn, not done
    with client.websocket_connect(f"{URL}?sessionId={SESSION}", headers=COOKIE) as ws:
        _ready(ws)
        frames = _ask(ws, "And test it")
    spoken = [f["text"] for f in frames if f["type"] == "transcript"
              and f["role"] == "assistant" and not f.get("out_of_band")]
    assert spoken == [demo_handlers.STILL_WORKING]
    assert not state["server"].tools.get("mcp_servers")  # nothing ran


# --------------------------------------------------------------------------
# 3. The caps
# --------------------------------------------------------------------------


def test_every_call_runs_under_the_demo_voice_guard(builder, monkeypatch):
    client, _ = builder
    seen = []

    async def fake_guard(websocket, run):
        seen.append(websocket.scope["path"])
        await websocket.accept()
        await websocket.close(code=4429, reason="guarded")

    monkeypatch.setattr(voice, "guard", fake_guard)
    closed = _closed(client)
    assert closed["code"] == 4429 and seen == [URL]


def test_a_call_is_refused_when_the_days_voice_seconds_are_spent(builder, monkeypatch):
    client, state = builder
    monkeypatch.setenv("DEMO_VOICE_DAILY_SECONDS", "10")
    closed = _closed(client)
    assert closed["code"] == voice.CLOSE_CODE and closed["reason"] == voice.CAP_MESSAGE
    assert state["built"] == []


def test_a_running_call_ends_when_the_days_voice_seconds_run_out(builder, monkeypatch):
    client, _ = builder
    monkeypatch.setattr(voice, "STEP_SEC", 1)
    monkeypatch.setenv("DEMO_VOICE_DAILY_SECONDS", "2")
    closed = None
    with client.websocket_connect(URL, headers=COOKIE) as ws:
        _until(ws, lambda f: f["type"] == "ready")
        for _ in range(400):
            message = ws.receive()
            if message.get("type") == "websocket.close":
                closed = message
                break
    assert closed is not None and closed["reason"] == voice.CAP_MESSAGE
    assert limits.used(voice.VOICE_BUCKET) == 2 and voice.open_calls() == 0


def test_a_call_ends_at_the_demo_call_length(builder, monkeypatch):
    client, _ = builder
    monkeypatch.setattr(voice, "STEP_SEC", 1)
    monkeypatch.setattr(settings, "voice_max_call_seconds", lambda: 2)
    closed = None
    with client.websocket_connect(URL, headers=COOKIE) as ws:
        _until(ws, lambda f: f["type"] == "ready")
        for _ in range(400):
            message = ws.receive()
            if message.get("type") == "websocket.close":
                closed = message
                break
    assert closed is not None and closed["reason"] == voice.length_message()
    assert voice.open_calls() == 0


def test_the_lines_open_at_once_are_shared_with_every_demo_call(builder, monkeypatch):
    client, state = builder
    monkeypatch.setenv("DEMO_VOICE_MAX_CALLS", "1")
    monkeypatch.setattr(voice, "_open_calls", 1)  # the Support desk's call
    closed = _closed(client)
    assert closed["reason"] == voice.BUSY_MESSAGE and state["built"] == []


def test_calls_per_visitor_are_limited(builder, monkeypatch):
    client, _ = builder
    monkeypatch.setenv("DEMO_VOICE_CALLS_PER_HOUR", "1")
    monkeypatch.setenv("DEMO_IP_MULTIPLIER", "1")
    with client.websocket_connect(URL, headers=COOKIE) as ws:
        _ready(ws)
    closed = _closed(client)
    assert closed["reason"] == voice.RATE_MESSAGE


def test_a_spoken_request_counts_against_the_builders_message_limit(builder, monkeypatch):
    client, state = builder
    monkeypatch.setenv("DEMO_ASSISTANT_PER_HOUR", "1")
    monkeypatch.setenv("DEMO_IP_MULTIPLIER", "1")
    with client.websocket_connect(URL, headers=COOKIE) as ws:
        _ready(ws)
        _ask(ws, "Connect the Northwind tools")
        frames = _ask(ws, "Now test it")
    spoken = [f["text"] for f in frames if f["type"] == "transcript"
              and f["role"] == "assistant" and not f.get("out_of_band")]
    assert spoken == [limits.RATE_MESSAGE]
    assert len(state["server"].tools["mcp_servers"].calls) == 1


def test_a_spoken_request_takes_a_turn_from_the_days_cap(builder, monkeypatch):
    client, state = builder
    monkeypatch.setattr(limits, "take_turn", lambda amount=1: False)
    with client.websocket_connect(URL, headers=COOKIE) as ws:
        _ready(ws)
        frames = _ask(ws, "Connect the Northwind tools")
    display = next(f for f in frames if f["type"] == "display")
    assert display["text"] == limits.CAP_MESSAGE
    assert not state["server"].tools.get("mcp_servers")


def test_tool_steps_per_spoken_request_are_capped(builder, monkeypatch):
    from admin import demo_handlers

    client, state = builder
    monkeypatch.setenv("DEMO_ASSISTANT_TOOLS_PER_TURN", "1")
    call = {"name": "mcp_servers", "args": {"action": "list"}}
    state["rounds"] = [("", [dict(call, id="c1"), dict(call, id="c2")]), ("Done.", [])]
    with client.websocket_connect(f"{URL}?sessionId={SESSION}", headers=COOKIE) as ws:
        _ready(ws)
        _ask(ws, "List the servers twice")
    steps = demo_handlers._LIVE[SESSION]["steps"]
    assert sorted(s["ok"] for s in steps) == [False, True]
    assert next(s for s in steps if not s["ok"])["note"] == assistant.STEP_CAP_FOR_VISITOR
    assert len(state["server"].tools["mcp_servers"].calls) == 1


def test_the_call_is_never_recorded(builder, monkeypatch):
    import endpoints.voice_live as live_ep

    client, _ = builder
    monkeypatch.setattr(live_ep, "VoiceCallRecorder",
                        lambda *a, **k: pytest.fail("a builder call was recorded"))
    env = {"DEMO_MODE": "1", "VOICE_RECORDING_ENABLED": "1"}
    settings.apply_process_lockdown(env)
    monkeypatch.setenv("VOICE_RECORDING_ENABLED", env["VOICE_RECORDING_ENABLED"])
    with client.websocket_connect(URL, headers=COOKIE) as ws:
        _ready(ws)


# --------------------------------------------------------------------------
# 4. Voice off
# --------------------------------------------------------------------------


def test_no_microphone_and_no_socket_when_the_demo_voice_is_off(builder, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    client, state = builder
    page = client.get("/admin/settings", headers=COOKIE).text
    assert 'id="ba-mic"' in page
    monkeypatch.delenv("DEMO_VOICE")
    page = client.get("/admin/settings", headers=COOKIE).text
    assert 'id="ba-panel"' in page and 'id="ba-mic"' not in page
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(URL, headers=COOKIE):
            pass
    assert closed.value.code == 1008 and state["built"] == []


def test_the_route_is_gone_outside_demo_mode(builder, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    client, state = builder
    monkeypatch.delenv("DEMO_MODE")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(URL, headers=COOKIE):
            pass
    assert state["built"] == []


# --------------------------------------------------------------------------
# 5. Who may call
# --------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"cookie": "waj_admin_token=someone-else"}])
def test_only_a_signed_in_visitor_may_call(builder, headers):
    from admin import demo_handlers

    client, state = builder
    closed = _closed(client, headers=headers)
    assert closed["reason"] == demo_handlers.NOT_SIGNED_IN and state["built"] == []


def test_only_from_the_demos_own_page(builder):
    from admin import demo_handlers

    client, state = builder
    closed = _closed(client, headers={**COOKIE, "origin": "https://elsewhere.example",
                                      "sec-fetch-site": "cross-site"})
    assert closed["reason"] == demo_handlers.CROSS_SITE and state["built"] == []


def test_a_demo_without_the_voice_model_says_so(builder, monkeypatch):
    from admin import demo_handlers

    client, _ = builder
    monkeypatch.setattr(assistant_voice, "voice_model_id", lambda: None)
    assert _closed(client)["reason"] == demo_handlers.NOT_SET_UP


def test_the_demo_channel_does_not_refuse_the_portals_own_page(builder, monkeypatch):
    """The demo's public channel lists the public agents only; the builder is
    not one, and the panel is the portal's own page, not a channel caller."""
    import endpoints.voice_live as live_ep
    from fastapi import HTTPException

    client, _ = builder

    def _resolve(conn):
        conn.state.channel = {"slug": "demo", "allowed_agent_kinds": ["custom:support-desk"]}

    def _enforce(conn, kind):
        if getattr(conn.state, "channel", None):
            raise HTTPException(status_code=403, detail="channel refuses")

    monkeypatch.setattr(live_ep, "resolve_channel", _resolve)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", _enforce)
    with client.websocket_connect(URL, headers=COOKIE) as ws:
        assert _ready(ws)["type"] == "ready"
