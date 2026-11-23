"""Voice for a generic (portal-built) agent: `WS /v1/agents/{slug}/voice`.

Everything here runs on the SIMULATED realtime provider (voice/live/simulated.py),
selected by the agent's own realtime model row, so the whole path is real
except the model's ears and mouth and the brain's LLM:

    socket -> gates -> auth frame -> ready -> greeting -> a caller turn ->
    ask_agent -> common.chat_session.run_turn -> CustomRuntimeAgent ->
    the answer spoken back as transcript + audio frames

What a real provider key would add is only the audio itself: speech
recognition, the realtime model deciding to call `ask_agent`, and synthesis.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import endpoints.voice_live as live_ep
from core.agent_profile import AgentProfileLoader, MissingProfileError, Profile
from tests.scoped_build import full_build, make_scope, use_scope
from voice.live.protocol import CLOSE_FORBIDDEN, CLOSE_PROVIDER_ERROR

SIM_MODEL = {"model_id": "m-rt", "provider": "simulated", "model_string": "sim", "extra_config": {}}
ANSWER = "We are open from nine to five, Sunday to Thursday."


def _profile(slug="helpdesk", *, profile_id="p-helpdesk", voice=None, voice_model=SIM_MODEL):
    return Profile(
        profile_id=profile_id, agent_kind="custom", slug=slug, environment="staging",
        model_main={"model_string": "m"},
        voice_config={"enabled": True, "model_id": "m-rt", **(voice or {})},
        voice_model=voice_model,
    )


class _Msg(SimpleNamespace):
    pass


class _FakeLLM:
    """A scripted chat model: one (content, tool_calls) pair per round."""

    def __init__(self, rounds):
        self.rounds = list(rounds)

    def bind_tools(self, tools):
        return self

    async def astream_events(self, messages, version="v2"):
        content, calls = self.rounds.pop(0)
        if content:
            yield {"event": "on_chat_model_stream",
                   "data": {"chunk": _Msg(content=content, tool_call_chunks=[])}}
        yield {"event": "on_chat_model_end",
               "data": {"output": _Msg(content=content, tool_calls=calls, usage_metadata=None)}}


@pytest.fixture
def stack(monkeypatch):
    """The real app with the flag on, one voice-enabled custom agent
    (`helpdesk`), and fakes only at the edges: the profile table, the
    superuser lookup, the brain's LLM, the chat-session table, the budget."""
    import main

    main.rate_limit_store.clear()
    live_ep._connect_limiter._store.clear()
    live_ep._active.clear()
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    monkeypatch.delenv("VOICE_LIVE_SIM_ENABLED", raising=False)
    full_build(monkeypatch)

    state = {
        "profiles": {"helpdesk": _profile()},
        "built": [], "identities": [], "budget": [], "tokens": [],
        "rounds": [(ANSWER, [])], "turn_contexts": [],
    }

    def _load_delegate(kind, slug, environment):
        profile = state["profiles"].get(slug)
        if kind != "custom" or profile is None:
            raise MissingProfileError(f"delegate {kind}/{slug} not found/inactive")
        return profile

    monkeypatch.setattr(AgentProfileLoader, "load_delegate", classmethod(
        lambda cls, kind, slug, environment: _load_delegate(kind, slug, environment)))
    monkeypatch.setattr(AgentProfileLoader, "load_active", classmethod(
        lambda cls, kind, environment=None: pytest.fail("a custom agent must never resolve via load_active")))

    def _resolve(token):
        state["tokens"].append(token)
        if token != "good-token":
            raise HTTPException(status_code=403, detail="Not a superuser")
        return {"user_token": token, "user_id": "u-1", "email": "ops@example.com"}

    import admin.auth as admin_auth

    monkeypatch.setattr(admin_auth, "resolve_superuser", _resolve)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "check_audio_budget",
                        lambda key, **kw: state["budget"].append(("check", key)))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds",
                        lambda key, seconds, **kw: state["budget"].append(("reserve", key)) or 0)

    # The brain: a real CustomRuntimeAgent whose profile and LLM are stubbed.
    def _init(self, agent_kind, environment=None, profile_slug=None):
        from core.profile_version_resolver import get_profile_pin

        self.agent_kind = agent_kind
        self.profile = _load_delegate(agent_kind, profile_slug, environment)
        self.llm = _FakeLLM(state["rounds"])
        state["built"].append({"slug": profile_slug, "pin": get_profile_pin()})

    # Patched on the objects the call path itself holds, never by dotted
    # name: another test in the session may have re-imported a module, and a
    # name then resolves to a copy the endpoint does not use.
    from runtime.agents.custom import CustomRuntimeAgent

    runtime_agent = next(c for c in CustomRuntimeAgent.__mro__ if c.__name__ == "RuntimeAgent")
    monkeypatch.setattr(runtime_agent, "__init__", _init)

    def _prepare(self, ctx, all_tools):
        state["turn_contexts"].append(ctx)
        tools = list(state.get("tools") or [])
        return {"system_prompt": "SYS", "tools": tools,
                "tools_by_name": {t.name: t for t in tools}}

    monkeypatch.setattr(CustomRuntimeAgent, "prepare", _prepare)

    def _for_identity(client, identity, **kw):
        state["identities"].append(identity)
        sess = MagicMock()
        sess.session_id = identity.session_id
        sess.resolve_or_create.return_value = (identity.session_id, True, "New Chat")
        sess.load_history.return_value = []
        return sess

    import voice.live.delegate as delegate_mod

    monkeypatch.setattr(inspect.getmodule(delegate_mod.run_turn).ChatSession,
                        "for_identity", _for_identity)
    return TestClient(main.app), state


def _until(ws, pred, limit=30):
    seen = []
    for _ in range(limit):
        message = ws.receive()
        if message.get("type") == "websocket.close":
            raise AssertionError(f"socket closed: {message} after {seen}")
        if message.get("text") is None:
            seen.append({"type": "<audio>", "bytes": len(message.get("bytes") or b"")})
            continue
        import json

        frame = json.loads(message["text"])
        seen.append(frame)
        if pred(frame):
            return frame, seen
    raise AssertionError(f"frame not seen in {limit}: {seen}")


def _open(ws, token="good-token"):
    ws.send_json({"type": "auth", "token": token})
    ready, _ = _until(ws, lambda f: f["type"] == "ready")
    return ready


def _closes_with(client, url, *, send=None):
    """The close frame the server answers with. A call that is let in
    instead fails here, at once, rather than waiting on a socket that will
    never close."""
    import json

    with client.websocket_connect(url) as ws:
        if send is not None:
            ws.send_json(send)
        for _ in range(5):
            message = ws.receive()
            if message.get("type") == "websocket.close":
                return SimpleNamespace(code=message.get("code"), reason=message.get("reason"))
            if message.get("text") and json.loads(message["text"]).get("type") == "ready":
                raise AssertionError("the call was let in")
    raise AssertionError("the socket did not close")


# --------------------------------------------------------------- the whole path
def test_a_call_reaches_the_custom_agent_and_its_answer_is_spoken(stack):
    client, state = stack
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        ready = _open(ws)
        assert ready["audio"]["encoding"] == "pcm16" and ready["greeting"] is True
        greeting, _ = _until(ws, lambda f: f["type"] == "transcript" and f["role"] == "assistant")
        assert greeting["text"]
        _until(ws, lambda f: f["type"] == "turn_end")

        ws.send_json({"type": "text", "text": "What are your opening hours?"})
        spoken, seen = _until(
            ws, lambda f: f["type"] == "transcript" and f["role"] == "assistant"
            and not f.get("out_of_band"))
        _, tail = _until(ws, lambda f: f["type"] == "turn_end")

    assert spoken["text"] == ANSWER
    assert {"type": "transcript", "role": "user", "text": "What are your opening hours?",
            "final": True} in seen
    # The answer was spoken: audio frames followed the transcript.
    assert any(f["type"] == "<audio>" and f["bytes"] for f in seen + tail)
    # The brain is the addressed agent, and the turn was a voice turn.
    assert state["built"] == [{"slug": "helpdesk", "pin": None}]
    assert state["turn_contexts"][-1]["voice_turn"] is True
    # The session belongs to this agent, exactly as the chat route stamps it.
    identity = state["identities"][-1]
    assert identity.agent_kind == "custom" and identity.session_type == "custom"
    assert identity.source == "api" and identity.user_id == "u-1"
    assert identity.business_id is None
    assert identity.session_metadata["agent_slug"] == "helpdesk"
    assert identity.session_metadata["channel"] == "voice"


def test_an_authorization_header_needs_no_auth_frame(stack):
    """A server-to-server caller sets the header; the frame exists only
    because a browser cannot."""
    client, state = stack
    with client.websocket_connect("/v1/agents/helpdesk/voice",
                                  headers={"Authorization": "Bearer good-token"}) as ws:
        ready, _ = _until(ws, lambda f: f["type"] == "ready")
    assert ready["type"] == "ready" and state["tokens"] == ["good-token"]


# ------------------------------------------------------------------------ gates
def test_off_unless_the_generic_agents_flag_is_on(stack, monkeypatch):
    client, state = stack
    monkeypatch.delenv("ENABLE_GENERIC_AGENTS")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/agents/helpdesk/voice",
                                      headers={"Authorization": "Bearer good-token"}):
            pass
    assert state["tokens"] == [] and state["built"] == []


def test_never_served_on_a_scoped_build(stack, monkeypatch):
    client, state = stack
    use_scope(monkeypatch, make_scope("orchard"), blocked=("/v1/",))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/agents/helpdesk/voice",
                                      headers={"Authorization": "Bearer good-token"}):
            pass
    assert state["built"] == []


@pytest.mark.parametrize("slug", ["Helpdesk", "-lead", "a" * 65, "x.y"])
def test_a_malformed_slug_is_refused_before_any_lookup(stack, slug):
    client, state = stack
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/agents/{slug}/voice",
                                      headers={"Authorization": "Bearer good-token"}):
            pass
    assert state["tokens"] == [] and state["built"] == []


@pytest.mark.parametrize("frame", [
    {"type": "auth", "token": "someone-elses-login"},
    {"type": "auth"},
    {"type": "mute"},
])
def test_a_caller_the_chat_route_would_refuse_is_refused(stack, frame):
    client, state = stack
    closed = _closes_with(client, "/v1/agents/helpdesk/voice", send=frame)
    assert closed.code == CLOSE_FORBIDDEN and closed.reason == "Authentication required."
    assert state["built"] == [] and state["budget"] == []


def test_no_auth_frame_times_out_to_forbidden(stack, monkeypatch):
    client, state = stack
    monkeypatch.setattr(live_ep, "_AUTH_FRAME_TIMEOUT_SEC", 0.2)
    closed = _closes_with(client, "/v1/agents/helpdesk/voice")
    assert closed.code == CLOSE_FORBIDDEN and state["built"] == []


def test_the_caller_check_is_the_chat_routes(stack, monkeypatch):
    """API keys arrive through `agents_v1._require_caller`. The socket must
    use that function, so a caller it admits is admitted here too — even one
    with no user id."""
    from endpoints import agents_v1

    client, state = stack
    seen = []

    async def _require(authorization):
        seen.append(authorization)
        if authorization != "Bearer key-123":
            raise HTTPException(status_code=401, detail="no")
        return {"api_key_id": "k-9"}

    monkeypatch.setattr(agents_v1, "_require_caller", _require)
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        assert _open(ws, token="key-123")["type"] == "ready"
        _until(ws, lambda f: f["type"] == "turn_end")
        ws.send_json({"type": "text", "text": "hello"})
        _until(ws, lambda f: f["type"] == "turn_end")
    assert seen == ["Bearer key-123"] and state["tokens"] == []
    assert state["identities"][-1].user_id is None


# ------------------------------------------------------------------ fail closed
def test_an_unknown_slug_fails_closed(stack):
    client, state = stack
    closed = _closes_with(client, "/v1/agents/nobody/voice",
                          send={"type": "auth", "token": "good-token"})
    assert closed.code == CLOSE_FORBIDDEN and closed.reason == "Agent not found."
    assert state["built"] == []


def test_the_plain_socket_does_not_serve_the_custom_kind(stack):
    """`/voice/live?agentKind=custom` names no agent, and has no caller check
    of the generic agents' own."""
    client, state = stack
    closed = _closes_with(client, "/voice/live?agentKind=custom")
    assert closed.code == CLOSE_FORBIDDEN and "/v1/agents/" in closed.reason
    assert state["built"] == [] and state["tokens"] == []


def test_a_brain_slug_that_does_not_exist_fails_closed(stack):
    client, state = stack
    state["profiles"]["helpdesk"] = _profile(voice={"agent_profile_slug": "ghost"})
    closed = _closes_with(client, "/v1/agents/helpdesk/voice",
                          send={"type": "auth", "token": "good-token"})
    assert closed.code == CLOSE_FORBIDDEN and closed.reason == "Agent not found."


# ----------------------------------------------------------------------- config
def test_voice_off_is_said_plainly(stack):
    client, state = stack
    state["profiles"]["helpdesk"] = _profile(voice={"enabled": False})
    closed = _closes_with(client, "/v1/agents/helpdesk/voice",
                          send={"type": "auth", "token": "good-token"})
    assert closed.code == CLOSE_FORBIDDEN
    assert closed.reason == "Voice is not turned on for this agent."


def test_no_realtime_model_is_said_plainly(stack, monkeypatch):
    client, state = stack
    state["profiles"]["helpdesk"] = _profile(voice={"model_id": ""}, voice_model=None)
    closed = _closes_with(client, "/v1/agents/helpdesk/voice",
                          send={"type": "auth", "token": "good-token"})
    assert closed.code == CLOSE_FORBIDDEN
    assert closed.reason.startswith("No realtime voice model is set for this agent.")


@pytest.mark.parametrize("row,envs,expected", [
    ({"provider": "google_genai", "extra_config": {}}, ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
     "Realtime model key missing: set GOOGLE_API_KEY on the server."),
    ({"provider": "azure_voice_live",
      "extra_config": {"endpoint": "https://voice.example", "auth_env_var": "MY_VOICE_KEY"}},
     ("MY_VOICE_KEY",), "Realtime model key missing: set MY_VOICE_KEY on the server."),
])
def test_a_missing_provider_key_is_a_clear_refusal_not_a_crash(stack, monkeypatch, row, envs, expected):
    """The fresh-stack case: voice is configured but the server holds no
    realtime key. The caller is told which key, before any provider is
    dialled and before anything is charged."""
    client, state = stack
    for name in envs:
        monkeypatch.delenv(name, raising=False)
    state["profiles"]["helpdesk"] = _profile(voice_model={"model_id": "m-rt", "model_string": "x", **row})
    monkeypatch.setattr(live_ep, "_session_for",
                        lambda cfg, **kw: pytest.fail("no provider session may be built"))
    closed = _closes_with(client, "/v1/agents/helpdesk/voice",
                          send={"type": "auth", "token": "good-token"})
    assert closed.code == CLOSE_PROVIDER_ERROR and closed.reason == expected
    assert state["built"] == [] and state["budget"] == []


def test_the_greeting_and_persona_are_built_in_when_the_agent_sets_none(stack, monkeypatch):
    client, state = stack
    seen = {}
    real = live_ep._session_for

    def _capture(cfg, **kw):
        seen.update(persona=kw["persona"], greeting=cfg.greeting_trigger, language=cfg.language)
        return real(cfg, **kw)

    monkeypatch.setattr(live_ep, "_session_for", _capture)
    monkeypatch.setattr(live_ep, "resolve_persona_prompt",
                        lambda *a, **kw: pytest.fail("no persona prompt is named"))
    with client.websocket_connect("/v1/agents/helpdesk/voice?locale=ar") as ws:
        _open(ws)
    assert "`ask_agent`" in seen["persona"] and "Arabic" in seen["persona"]
    assert seen["greeting"] and seen["language"].startswith("ar")


def test_an_operator_greeting_and_persona_win(stack, monkeypatch):
    client, state = stack
    state["profiles"]["helpdesk"] = _profile(voice={
        "greeting_trigger": "Say hello from the help desk.",
        "persona_prompt_slug": "helpdesk:voice_persona"})
    seen = {}
    real = live_ep._session_for

    def _capture(cfg, **kw):
        seen.update(persona=kw["persona"], greeting=cfg.greeting_trigger)
        return real(cfg, **kw)

    def _persona(cfg, kind, **kw):
        seen["owner"] = kw["profile"].slug
        return "OPERATOR PERSONA"

    monkeypatch.setattr(live_ep, "_session_for", _capture)
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", _persona)
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        _open(ws)
    assert seen == {"persona": "OPERATOR PERSONA", "greeting": "Say hello from the help desk.",
                    "owner": "helpdesk"}


def test_the_brain_can_be_another_custom_agent(stack):
    """`voice_config.agent_profile_slug`, as on every other kind: the
    addressed agent owns the voice settings, the named one answers. The
    session still belongs to the agent that was called."""
    client, state = stack
    state["profiles"]["helpdesk"] = _profile(voice={"agent_profile_slug": "helpdesk-voice"})
    state["profiles"]["helpdesk-voice"] = _profile("helpdesk-voice", profile_id="p-brain", voice={"enabled": False})
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        _open(ws)
        _until(ws, lambda f: f["type"] == "turn_end")
        ws.send_json({"type": "text", "text": "hello"})
        _until(ws, lambda f: f["type"] == "turn_end")
    assert [b["slug"] for b in state["built"]] == ["helpdesk-voice"]
    assert state["identities"][-1].session_metadata["agent_slug"] == "helpdesk"


# -------------------------------------------------------- identifier provenance
REFERENCE = "4829103756281947"


def _digits(text):
    return "".join(ch for ch in text if ch.isdigit())


def _reference_turn(client, state, *, tool_returns):
    from langchain_core.tools import StructuredTool

    state["tools"] = [StructuredTool.from_function(
        func=lambda: tool_returns, name="lookup_order", description="Look up the order.")]
    state["rounds"][:] = [
        ("", [{"name": "lookup_order", "args": {}, "id": "c-1"}]),
        (f"Your order reference is {REFERENCE}.", []),
    ]
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        _open(ws)
        _until(ws, lambda f: f["type"] == "turn_end")
        ws.send_json({"type": "text", "text": "What is my order reference?"})
        spoken, _ = _until(
            ws, lambda f: f["type"] == "transcript" and f["role"] == "assistant"
            and not f.get("out_of_band"))
        _until(ws, lambda f: f["type"] == "turn_end")
    return spoken["text"]


def test_a_reference_number_a_tool_returned_is_spoken_in_full(stack):
    """The provenance guard withholds any long number no tool result vouches
    for. The custom agent reports what its tools return, so a number it
    quotes correctly reaches the caller."""
    client, state = stack
    spoken = _reference_turn(client, state, tool_returns=f'{{"reference": "{REFERENCE}"}}')
    assert REFERENCE in _digits(spoken)


def test_a_reference_number_no_tool_returned_is_withheld(stack):
    client, state = stack
    spoken = _reference_turn(client, state, tool_returns='{"reference": "unknown"}')
    assert REFERENCE not in _digits(spoken)


# ---------------------------------------------------------- budget and recording
def test_calls_are_metered_per_agent_whoever_calls(stack, monkeypatch):
    """No tenant: the daily audio budget is one bucket per agent, the same
    for an operator and for a caller with no user id at all."""
    from endpoints import agents_v1

    client, state = stack
    bucket = live_ep.agent_budget_id("custom", "helpdesk")
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        _open(ws)
    first = list(state["budget"])
    state["budget"].clear()

    async def _key_caller(authorization):
        return {"api_key_id": "k-9"}

    monkeypatch.setattr(agents_v1, "_require_caller", _key_caller)
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        _open(ws, token="key-123")
    assert first and {key for _, key in first} == {bucket}
    assert {key for _, key in state["budget"]} == {bucket}
    assert bucket != live_ep.agent_budget_id("custom", "other-agent")


def test_an_exhausted_agent_budget_refuses_the_call(stack, monkeypatch):
    from voice.live.budget import BudgetExhausted
    from voice.live.protocol import CLOSE_RATE_LIMITED

    client, state = stack

    def _spent(key, **kw):
        raise BudgetExhausted("Daily voice budget reached.")

    monkeypatch.setattr(live_ep, "check_audio_budget", _spent)
    closed = _closes_with(client, "/v1/agents/helpdesk/voice",
                          send={"type": "auth", "token": "good-token"})
    assert closed.code == CLOSE_RATE_LIMITED and state["built"] == []


def test_a_recorded_call_is_filed_under_the_agent_bucket(stack, monkeypatch):
    client, state = stack
    made = {}

    class _Recorder:
        def __init__(self, **kw):
            made.update(kw)

        def __getattr__(self, name):
            return lambda *a, **kw: None

    monkeypatch.setattr(live_ep, "recording_enabled", lambda: True)
    monkeypatch.setattr(live_ep, "VoiceCallRecorder", _Recorder)
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        _open(ws)
    assert made["agent_kind"] == "custom"
    assert made["business_id"] == live_ep.agent_budget_id("custom", "helpdesk")


# ------------------------------------------------------------- draft (the pin)
def test_an_operator_can_call_a_draft_copy_of_the_agent(stack, monkeypatch):
    client, state = stack
    draft = _profile(profile_id="11111111-2222-3333-4444-555555555555",
                     voice={"greeting_trigger": "Draft greeting."})
    monkeypatch.setattr(AgentProfileLoader, "load_pinned", classmethod(
        lambda cls, profile_id, kind: draft if profile_id == draft.profile_id else None))
    seen = {}
    real = live_ep._session_for
    monkeypatch.setattr(live_ep, "_session_for",
                        lambda cfg, **kw: seen.update(greeting=cfg.greeting_trigger) or real(cfg, **kw))
    with client.websocket_connect(
            f"/v1/agents/helpdesk/voice?profileId={draft.profile_id}") as ws:
        _open(ws)
    assert seen["greeting"] == "Draft greeting."
    assert state["built"] == [{"slug": "helpdesk", "pin": draft.profile_id}]


def test_a_pin_is_ignored_for_a_caller_who_is_not_an_operator(stack, monkeypatch):
    from endpoints import agents_v1

    client, state = stack
    monkeypatch.setattr(AgentProfileLoader, "load_pinned", classmethod(
        lambda cls, profile_id, kind: pytest.fail("the pin must not be read")))

    async def _key_caller(authorization):
        return {"api_key_id": "k-9"}

    monkeypatch.setattr(agents_v1, "_require_caller", _key_caller)
    with client.websocket_connect(
            "/v1/agents/helpdesk/voice?profileId=11111111-2222-3333-4444-555555555555") as ws:
        _open(ws, token="key-123")
    assert state["built"] == [{"slug": "helpdesk", "pin": None}]


def test_a_pin_cannot_swap_in_a_different_agent(stack, monkeypatch):
    client, state = stack
    other = _profile("billing", profile_id="11111111-2222-3333-4444-555555555555",
                     voice={"greeting_trigger": "Billing."})
    monkeypatch.setattr(AgentProfileLoader, "load_pinned", classmethod(
        lambda cls, profile_id, kind: other))
    with client.websocket_connect(
            f"/v1/agents/helpdesk/voice?profileId={other.profile_id}") as ws:
        _open(ws)
    assert state["built"] == [{"slug": "helpdesk", "pin": None}]
