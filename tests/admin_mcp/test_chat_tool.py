"""`chat` — one test-chat turn through the profile's own endpoint, the way
the portal widget does it. The HTTP hop (`_post_chat`) and the post-turn
DB lookup (`_lookup_turn`) are patched; everything between them (body,
headers, SSE collapse, draft warning, version pin) is the real code."""
from __future__ import annotations

import json

import admin_mcp.tools_dispatch as td
from tests.admin_mcp.conftest import ACTOR, FakeClient

PROFILE = {"profile_id": "p1", "agent_kind": "business", "slug": "main",
           "environment": "staging", "status": "active", "is_active": True,
           "endpoint_path": "/chat/stream"}


def _sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def _setup(monkeypatch, profile=PROFILE, body=None, status=200, lookup=None):
    client = FakeClient({"agent_profiles": [dict(profile)]})
    monkeypatch.setattr(td, "client_for_env", lambda env: client)
    sent = {}

    async def fake_post(endpoint, payload, headers, env, timeout):
        sent.update(endpoint=endpoint, payload=payload, headers=headers, env=env)
        return status, "text/event-stream", body or _sse(
            {"type": "session", "id": payload["session_id"]},
            {"type": "answer", "content": "You have "},
            {"type": "answer", "content": "3 bookings today."},
            {"type": "done"}), "in-process"

    monkeypatch.setattr(td, "_post_chat", fake_post)
    monkeypatch.setattr(td, "_lookup_turn", lambda sid, env: lookup or {
        "trace_id": "tr-1",
        "tool_calls": [{"name": "get_bookings", "input": "{}", "output": "[3]",
                        "is_error": False}]})
    return sent


async def test_chat_returns_reply_tool_calls_trace(monkeypatch):
    sent = _setup(monkeypatch)

    out = await td.chat(profile_id="p1", message="how many bookings?",
                        business_id="b1")

    assert out["reply"] == "You have 3 bookings today."
    assert out["tool_calls"][0]["name"] == "get_bookings"
    assert out["trace_id"] == "tr-1"
    assert out["session_id"] == sent["payload"]["session_id"]
    assert "warning" not in out
    # Same request the portal widget sends: operator JWT + widget body.
    assert sent["endpoint"] == "/chat/stream"
    assert sent["headers"]["Authorization"] == f"Bearer {ACTOR['user_token']}"
    assert sent["payload"]["message"] == "how many bookings?"
    assert sent["payload"]["business_id"] == "b1"
    assert sent["payload"]["agent_kind"] == "business"


async def test_chat_continues_a_session(monkeypatch):
    sent = _setup(monkeypatch)
    out = await td.chat(profile_id="p1", message="and tomorrow?", session_id="s-42")
    assert sent["payload"]["session_id"] == "s-42"
    assert out["session_id"] == "s-42"


async def test_chat_draft_answers_as_itself_and_version_pin_sends_header(monkeypatch):
    # A draft copy / specialist answers its own test chat: the tool pins the
    # profile (X-WAJ-Profile-Id) instead of warning that the active one answers.
    sent = _setup(monkeypatch, profile={**PROFILE, "status": "draft"})
    out = await td.chat(profile_id="p1", message="hi")
    assert sent["headers"]["X-WAJ-Profile-Id"] == "p1"
    assert "X-WAJ-Profile-Version" not in sent["headers"]
    assert "warning" not in out and "p1" in out["served_by"]

    sent = _setup(monkeypatch, profile={**PROFILE, "status": "draft"})
    out = await td.chat(profile_id="p1", message="hi", profile_version_id="v9")
    assert sent["headers"]["X-WAJ-Profile-Version"] == "v9"
    assert "X-WAJ-Profile-Id" not in sent["headers"]
    assert "warning" not in out and "v9" in out["served_by"]


async def test_chat_switched_off_profile_warns(monkeypatch):
    _setup(monkeypatch, profile={**PROFILE, "is_active": False})
    out = await td.chat(profile_id="p1", message="hi")
    assert "ACTIVE profile" in out["warning"]


DEMO = {"profile_id": "d1", "agent_kind": "agents_sales", "slug": "agents_sales_default",
        "environment": "staging", "status": "active", "is_active": True,
        "endpoint_path": "/agents/chat/stream"}


async def test_chat_demo_kind_sends_agent_key_case_and_state(monkeypatch):
    # The AI-agents demo endpoint requires `agent` (its key, not the kind);
    # without it every turn was a 422 `body.agent: Field required`.
    sent = _setup(monkeypatch, profile=DEMO, body=_sse(
        {"type": "answer", "content": "Hi"}, {"type": "state", "token": "tok-1"},
        {"type": "done"}))
    out = await td.chat(profile_id="d1", message="hi")
    assert sent["endpoint"] == "/agents/chat/stream"
    assert sent["payload"]["agent"] == "sales"
    assert sent["payload"]["case_id"] == "O01"
    assert "business_id" not in sent["payload"]
    assert out["state"] == "tok-1"

    sent = _setup(monkeypatch, profile=DEMO)
    await td.chat(profile_id="d1", message="yes", session_id="s-1",
                  case_id="O02", state="tok-1")
    assert sent["payload"]["case_id"] == "O02"
    assert sent["payload"]["state"] == "tok-1"


async def test_chat_refuses_non_chat_endpoints(monkeypatch):
    from admin.services import test_chat
    monkeypatch.setattr(test_chat, "_NOT_CHAT_ENDPOINTS", {"/sample/suggest"})
    _setup(monkeypatch, profile={**PROFILE, "endpoint_path": "/sample/suggest"})
    out = await td.chat(profile_id="p1", message="hi")
    assert "isn't a chat agent" in out["error"]


async def test_chat_surfaces_stream_errors_and_http_errors(monkeypatch):
    _setup(monkeypatch, body=_sse({"type": "error", "message": "model down"}))
    out = await td.chat(profile_id="p1", message="hi")
    assert out["errors"] == ["model down"] and out["reply"] == ""

    _setup(monkeypatch, status=422, body='{"detail":"business_id required"}')
    out = await td.chat(profile_id="p1", message="hi")
    assert "HTTP 422" in out["error"] and "business_id" in out["detail"]


async def test_chat_needs_an_endpoint_and_a_profile(monkeypatch):
    _setup(monkeypatch, profile={**PROFILE, "endpoint_path": None})
    out = await td.chat(profile_id="p1", message="hi")
    assert "no chat address" in out["error"] and "endpoint_path" in out["hint"]
    _setup(monkeypatch)
    assert "not found" in (await td.chat(profile_id="nope", message="hi"))["error"]
    assert "empty" in (await td.chat(profile_id="p1", message="  "))["error"]


async def test_chat_falls_back_to_stream_tool_events(monkeypatch):
    _setup(monkeypatch,
           body=_sse({"type": "tool_call", "name": "edit_section", "args": {"a": 1}},
                     {"type": "answer", "content": "done"}),
           lookup={"trace_id": None, "tool_calls": []})
    monkeypatch.setattr(td.asyncio, "sleep", _no_sleep)
    out = await td.chat(profile_id="p1", message="edit")
    assert out["tool_calls"] == [{"type": "tool_call", "name": "edit_section",
                                  "args": {"a": 1}}]
    assert out["trace_id"] is None


async def _no_sleep(_s):
    return None


def test_parse_sse_handles_plain_json_body():
    out = td.parse_sse('{"response": "plain answer"}')
    assert out["reply"] == "plain answer"
    out = td.parse_sse(_sse({"type": "final", "content": "single-shot"}))
    assert out["reply"] == "single-shot"


def test_lookup_turn_reads_trace_and_persisted_tool_calls(monkeypatch):
    import admin.screens.observability as obs
    client = FakeClient({
        "agent_traces": [{"session_id": "s1", "lmnr_trace_id": "tr-9",
                          "created_at": "2"}],
        "ai_chat_messages": [
            {"session_id": "s1", "role": "user", "created_at": "1", "metadata": {}},
            {"session_id": "s1", "role": "assistant", "created_at": "2",
             "metadata": {"tool_calls": [{"name": "get_sales", "args": {"d": 1},
                                          "result": "x" * 5000}]}}],
    })
    monkeypatch.setattr(td, "client_for_env", lambda env: client)
    monkeypatch.setattr(obs, "_coerce_session_id_for_lookup", lambda s: s)
    out = td._lookup_turn("s1", "staging")
    assert out["trace_id"] == "tr-9"
    call = out["tool_calls"][0]
    assert call["name"] == "get_sales" and call["output_truncated"] is True
    assert len(call["output"]) <= td._TOOL_OUTPUT_CAP + 1


async def test_post_chat_goes_in_process_for_own_env(monkeypatch):
    """The real transport: a tiny ASGI app stands in for main.app."""
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    app = FastAPI()

    @app.post("/chat/stream")
    async def stream(payload: dict):
        async def gen():
            yield f"data: {json.dumps({'type': 'answer', 'content': payload['message']})}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    monkeypatch.setattr(td, "_app_for_in_process", lambda: app)
    monkeypatch.setattr(td, "_process_env", lambda: "staging")
    status, ctype, text, target = await td._post_chat(
        "/chat/stream", {"message": "echo"}, {}, "staging", 10)
    assert status == 200 and target == "in-process"
    assert td.parse_sse(text)["reply"] == "echo"
