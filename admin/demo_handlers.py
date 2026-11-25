"""The public demo's one-click entry: ``POST /admin/demo/enter``.

Only in demo mode (`demo_mode`, ``DEMO_MODE``); a 404 otherwise. The sign-in
page shows a "Try the demo" button that posts here; this signs the visitor in
as the demo admin (`demo_mode.account`) with a session of their own and sends
them to the portal. No email or password is typed or shown.

Same protections as the other sign-in forms: a request a browser marks as
coming from another site is refused (`first_run_handlers.same_origin`), and
`demo_mode.gate` limits how often one address may enter.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from admin.router import router, templates

logger = logging.getLogger(__name__)

#: Where a visitor lands: the agents, in staging (the environment the demo's
#: agents are built and tested in; Overview opens on production and is mostly
#: empty while tracing is off).
PORTAL = "/admin/agents?env=staging"


def _sign_in_sync() -> str:
    from core.auth import SignInError
    from demo_mode import account

    try:
        return account.sign_in()
    except SignInError:
        # The account is made at start; make it again if it went missing
        # (a database restored from elsewhere), then try once more.
        account.ensure_demo_user()
        return account.sign_in()


@router.post("/demo/enter")
async def demo_enter(request: Request) -> Response:
    from admin.first_run_handlers import same_origin
    from demo_mode import settings

    if not settings.enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if not same_origin(request):
        return HTMLResponse("This button only works from the demo's own page.", status_code=403)
    try:
        token = await asyncio.to_thread(_sign_in_sync)
    except Exception as exc:  # noqa: BLE001 — never echo internals to a visitor
        logger.error("demo: sign-in failed (%s)", type(exc).__name__)
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "The demo could not start just now. Please try again in a minute."},
            status_code=503)
    redirect = RedirectResponse(url=PORTAL, status_code=303)
    from admin.router import ADMIN_TOKEN_COOKIE, ENV_COOKIE

    # Served over TLS (behind the proxy): keep the session cookie off plain http.
    secure = request.url.scheme == "https"
    redirect.set_cookie(ADMIN_TOKEN_COOKIE, token.strip(), httponly=True, secure=secure,
                        samesite="lax", max_age=60 * 60 * 8)
    # The demo is built and tested in staging.
    redirect.set_cookie(ENV_COOKIE, "staging", max_age=30 * 24 * 3600, secure=secure,
                        samesite="lax", path="/")
    return redirect


def _call_page_sync() -> str:
    """The Voice tab of the agent a visitor can call (demo_mode.voice.CALL_SLUG)."""
    from core.auth import get_service_client
    from demo_mode import voice

    rows = (get_service_client().table("agent_profiles").select("profile_id")
            .eq("agent_kind", "custom").eq("slug", voice.CALL_SLUG).eq("environment", "staging")
            .limit(1).execute().data or [])
    if not rows:
        return PORTAL
    return f"/admin/agents/{rows[0]['profile_id']}/voice?env=staging#voice-call"


@router.get("/demo/call")
async def demo_call() -> Response:
    """The banner's "place a voice call" link: straight to the Call button."""
    from demo_mode import settings

    if not settings.voice_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        target = await asyncio.to_thread(_call_page_sync)
    except Exception as exc:  # noqa: BLE001 — the agents list is a fine fallback
        logger.warning("demo: call page lookup failed (%s)", type(exc).__name__)
        target = PORTAL
    return RedirectResponse(url=target, status_code=303)


# ---------------------------------------------------------------------------
# The builder assistant: POST /admin/demo/assistant/chat
# ---------------------------------------------------------------------------

from typing import Any, AsyncGenerator, Dict, Optional  # noqa: E402

from fastapi import Depends, Query  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from admin.auth import require_superuser  # noqa: E402


class AssistantMessage(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-fA-F-]{32,36}$")
    page: Optional[str] = Field(default=None, max_length=300)


def _one_answer(text: str):
    async def _gen() -> AsyncGenerator[Dict[str, Any], None]:
        yield {"type": "answer", "content": text}
        yield {"type": "done"}
    return _gen


def _build_assistant(page: Optional[str], voice: bool = False):
    """The Builder assistant agent. If a visitor deleted or switched it off,
    seed it back once and try again (blocking)."""
    from core.agent_profile import MissingProfileError
    from demo_mode import assistant

    try:
        return assistant.create_agent(page=page, voice=voice)
    except MissingProfileError:
        from core.agent_profile import AgentProfileLoader
        from demo_mode import seed

        seed.seed()
        AgentProfileLoader.invalidate("custom", seed._env())
        return assistant.create_agent(page=page, voice=voice)


#: The assistant's turns, by conversation id, so a visitor who navigates away
#: mid-turn finds the result when the panel loads on the next page: the turn
#: keeps running (it is not tied to the browser's connection) and the panel
#: reads it back from GET /admin/demo/assistant/history. Kept in this
#: process's memory for an hour; the saved conversation holds the text.
_LIVE: Dict[str, Dict[str, Any]] = {}
_TASKS: set = set()
_LIVE_TTL_SEC = 3600.0
_KEEPALIVE_SEC = 10.0
#: The top span of an assistant turn's trace. Listed among the background top
#: spans (admin/screens/observability.py), so the assistant's turns stay out
#: of Conversations and Overview: it is a helper in the portal, not one of
#: the agents' conversations.
TRACE_NAME = "demo.builder_assistant"
STILL_WORKING = ("I am still working on your last message. Give me a moment, then ask again.")


def _new_live(session_id: str, user_id: Any, **extra: Any) -> Dict[str, Any]:
    """Start the record of a turn of conversation `session_id`. `id` tells
    one turn from the next (the panel polls for a spoken turn's record)."""
    import time
    import uuid

    live = {"id": uuid.uuid4().hex, "answer": "", "steps": [], "changed": False,
            "done": False, "at": time.monotonic(), "user_id": user_id, **extra}
    _LIVE[session_id] = live
    return live


def _prune_live() -> None:
    import time

    cutoff = time.monotonic() - _LIVE_TTL_SEC
    for key in [k for k, v in _LIVE.items() if v.get("at", 0) < cutoff]:
        _LIVE.pop(key, None)


def _record(live: Optional[Dict[str, Any]], event: Dict[str, Any]) -> None:
    if live is None:
        return
    kind = event.get("type")
    if kind == "answer" and event.get("content"):
        live["answer"] += str(event["content"])
    elif kind == "step":
        live["steps"].append({k: v for k, v in event.items() if k != "type"})
    elif kind == "changed":
        live["changed"] = True
    elif kind == "error":
        live["answer"] += ("\n\n" if live["answer"] else "") + str(event.get("message") or "")
    elif kind == "done":
        live["done"] = True


@router.post("/demo/assistant/chat")
async def demo_assistant_chat(
    request: Request,
    body: AssistantMessage,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Stream one turn of the builder assistant (demo_mode/assistant.py) as
    server-sent events: the agent's usual events plus a `step` event per
    tool call ({tool, action, label, ok, note, links}), `working` while a
    long step runs (also a keep-alive), and `changed` when something in the
    portal was created or changed."""
    from common.sse import sse_response
    from demo_mode import assistant, limits, settings

    if not settings.enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    from admin.first_run_handlers import same_origin

    if not same_origin(request):
        raise HTTPException(status_code=403, detail="The assistant only works from the demo's own page.")
    # The per-visitor message limit is the gate's ("assistant" bucket); the
    # daily cap is taken here, so a refusal is an answer the panel shows.
    _prune_live()
    running = _LIVE.get(body.session_id or "")
    if running and not running.get("done") and running.get("user_id") == ctx.get("user_id"):
        # The last message's turn is still running (its stream may have
        # dropped). A second turn now would not know what the first made.
        return sse_response(_one_answer(STILL_WORKING))
    if not await asyncio.to_thread(limits.take_turn):
        return sse_response(_one_answer(limits.CAP_MESSAGE))
    try:
        agent = await asyncio.to_thread(_build_assistant, body.page)
    except Exception as exc:  # noqa: BLE001
        logger.error("demo assistant: build failed (%s: %s)", type(exc).__name__, exc)
        return sse_response(_one_answer(
            "The builder assistant is not available right now. Please try again in a minute."))

    ip, session = assistant.visitor_of(request.scope)
    turn = assistant.Turn(actor=ctx, ip=ip, session=session, conversation=body.session_id)
    queue: asyncio.Queue = asyncio.Queue()
    holder: Dict[str, Any] = {"live": None, "session": body.session_id}

    def _start_live(session_id: str) -> None:
        live = _new_live(session_id, ctx.get("user_id"))
        holder["live"] = live
        holder["session"] = session_id

    async def _emit(event: Dict[str, Any]) -> None:
        _record(holder["live"], event)
        await queue.put(event)

    async def _produce() -> None:
        from common.chat_session import TurnIdentity, run_turn
        from runtime.agents.custom import CUSTOM_AGENT_KIND

        token = assistant.TURN.set(turn)
        if body.session_id:
            _start_live(body.session_id)
        # The turn's ONE trace. It is opened here, inside the task that makes
        # the model calls: the task is created before the response starts to
        # stream, so a root span opened around the stream (sse_response's
        # `trace_name`) was not in this task's context, and each model call
        # became a trace of its own (three traces a turn).
        from core.observability import root_span

        span_cm = root_span(TRACE_NAME)
        span_cm.__enter__()
        try:
            identity = TurnIdentity(
                agent_kind=CUSTOM_AGENT_KIND, session_id=body.session_id,
                user_id=ctx.get("user_id"), business_id=None, session_type="custom",
                source="portal", session_metadata={"agent_slug": assistant.BUILDER_SLUG})
            async for event in run_turn(agent=agent, message=body.message, identity=identity,
                                        profile=agent.profile,
                                        extra_context={"user_id": ctx.get("user_id")}):
                kind = event.get("type")
                if kind == "session" and event.get("id") and holder["live"] is None:
                    _start_live(str(event["id"]))
                if kind == "done":
                    for step in turn.drain():
                        await _emit({"type": "step", **step})
                    if turn.wrote:
                        await _emit({"type": "changed"})
                await _emit(event)
                if kind in ("tool_activity", "thought"):
                    for step in turn.drain():
                        await _emit({"type": "step", **step})
        except Exception as exc:  # noqa: BLE001 — the panel shows a plain message
            logger.error("demo assistant: turn failed (%s: %s)", type(exc).__name__, exc)
            await _emit({"type": "error", "message": "The assistant hit an error. Please try again."})
            await _emit({"type": "done"})
        finally:
            # For the conversation's next turn: what this one made or changed.
            try:
                span_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 — tracing never breaks the turn
                pass
            assistant.remember(holder["session"], turn.steps)
            if holder["live"] is not None:
                holder["live"]["done"] = True
            try:
                assistant.TURN.reset(token)
            except ValueError:
                pass
            await queue.put(None)

    # Not tied to this response: a visitor who leaves the page mid-turn gets
    # the result on the next page (GET /admin/demo/assistant/history).
    task = asyncio.create_task(_produce())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    async def _source() -> AsyncGenerator[Dict[str, Any], None]:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SEC)
            except asyncio.TimeoutError:
                # A keep-alive, so a proxy does not close a quiet stream while
                # a long step (a test run) runs.
                yield {"type": "working", "label": turn.working or ""}
                continue
            if event is None:
                return
            yield event

    return sse_response(_source, error_message="The assistant hit an error. Please try again.")


@router.get("/demo/assistant/history")
async def demo_assistant_history(
    session_id: str = Query(..., pattern=r"^[0-9a-fA-F-]{32,36}$"),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """The assistant conversation `session_id`: its saved messages, and the
    latest turn's steps and state while it is still in memory ({answer,
    steps, changed, done}). The panel reads it when a page loads in the
    middle of a turn."""
    from fastapi.responses import JSONResponse

    from demo_mode import settings

    if not settings.enabled():
        raise HTTPException(status_code=404, detail="Not Found")

    def _read() -> Dict[str, Any]:
        from core.auth import get_service_client

        client = get_service_client()
        owned = (client.table("ai_chat_sessions").select("id")
                 .eq("id", session_id).eq("user_id", ctx.get("user_id")).limit(1)
                 .execute().data or [])
        if not owned:
            return {"messages": []}
        rows = (client.table("ai_chat_messages").select("role, content, created_at")
                .eq("session_id", session_id).order("created_at").limit(200)
                .execute().data or [])
        return {"messages": [{"role": r.get("role"), "content": r.get("content")}
                             for r in rows if r.get("role") in ("user", "assistant")]}

    try:
        out = await asyncio.to_thread(_read)
    except Exception as exc:  # noqa: BLE001 — the panel keeps what it has
        logger.warning("demo assistant: history unavailable (%s)", type(exc).__name__)
        out = {"messages": []}
    live = _LIVE.get(session_id)
    if live and live.get("user_id") == ctx.get("user_id"):
        keys = ("id", "answer", "steps", "changed", "done", "message", "voice")
        out["live"] = {k: live.get(k) for k in keys}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Talking to the builder assistant: WS /admin/demo/assistant/voice
# ---------------------------------------------------------------------------

import re  # noqa: E402

from fastapi import WebSocket  # noqa: E402

_SESSION_ID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
NOT_SIGNED_IN = "Open the demo first, then press the microphone again."
NOT_SET_UP = "Voice is not set up on this demo."
CROSS_SITE = "The microphone only works from the demo's own page."


async def _close_with(websocket: WebSocket, reason: str, code: int = 4403) -> None:
    """Accept, then close with the reason: a refused handshake reaches a
    browser as a bare failure with no words."""
    try:
        await websocket.accept()
        await websocket.close(code=code, reason=reason[:120])
    except Exception:  # noqa: BLE001 (the caller has already gone)
        pass


def _answer(text: str) -> Dict[str, Any]:
    return {"type": "answer", "content": text}


async def _voice_turn(inner, message: str, context: Dict[str, Any], *, ctx: Dict[str, Any],
                      ip: str, session: Optional[str]) -> AsyncGenerator[Dict[str, Any], None]:
    """One spoken request, run as a builder message: the same caps, the same
    step events and the same conversation record as a typed one
    (`demo_assistant_chat`). Ends with the `spoken_summary` the voice reads."""
    from demo_mode import assistant, assistant_voice, limits

    session_id = str(context.get("session_id") or "")
    _prune_live()
    running = _LIVE.get(session_id)
    if running and not running.get("done"):
        yield _answer(STILL_WORKING)
        return
    # The gate counts a typed message against the visitor's "assistant"
    # bucket; a spoken one is counted here, then the daily turn.
    if not limits.hit("assistant", ip=ip, session=session):
        yield _answer(limits.RATE_MESSAGE)
        return
    if not await asyncio.to_thread(limits.take_turn):
        yield _answer(limits.CAP_MESSAGE)
        return
    turn = assistant.Turn(actor=ctx, ip=ip, session=session, conversation=session_id or None)
    live = _new_live(session_id, ctx.get("user_id"), message=message, voice=True) if session_id else None
    token = assistant.TURN.set(turn)
    answer = []
    try:
        async for event in inner(message, context):
            kind = event.get("type")
            if kind == "answer" and event.get("content"):
                answer.append(str(event["content"]))
            _record(live, event)
            yield event
            if kind in ("tool_activity", "thought"):
                for step in turn.drain():
                    _record(live, {"type": "step", **step})
        for step in turn.drain():
            _record(live, {"type": "step", **step})
        if turn.wrote:
            _record(live, {"type": "changed"})
        summary = assistant_voice.spoken_summary("".join(answer))
        if summary:
            yield {"type": "spoken_summary", "content": summary}
    finally:
        assistant.remember(session_id or None, turn.steps)
        if live is not None:
            live["done"] = True
        try:
            assistant.TURN.reset(token)
        except ValueError:
            pass


def _voice_brain(ctx: Dict[str, Any], ip: str, session: Optional[str], page: Optional[str]):
    """`VoiceRoute.build_brain`: the builder assistant, each spoken turn of
    it run through `_voice_turn`. Blocking (profile load)."""
    def build(_params: Dict[str, Any]):
        agent = _build_assistant(page, voice=True)
        inner = agent.run_stream

        async def run_stream(message: str, context: Dict[str, Any]):
            async for event in _voice_turn(inner, message, context, ctx=ctx, ip=ip, session=session):
                yield event

        agent.run_stream = run_stream
        return agent
    return build


@router.websocket("/demo/assistant/voice")
async def demo_assistant_voice(
    websocket: WebSocket,
    sessionId: Optional[str] = Query(None),
    page: Optional[str] = Query(None),
) -> None:
    """A voice call with the builder assistant (demo_mode/assistant_voice.py):
    the panel's microphone. Only where the demo's voice is on (`DEMO_VOICE`);
    the visitor's portal sign-in is the caller; every call runs under the
    demo's voice caps (`demo_mode.voice.guard`)."""
    from admin.auth import resolve_superuser
    from admin.first_run_handlers import same_origin
    from demo_mode import assistant, assistant_voice, gate, settings
    from demo_mode import voice as demo_voice

    if not settings.voice_enabled():
        await websocket.close(code=1008)
        return
    if not same_origin(websocket):
        return await _close_with(websocket, CROSS_SITE)
    headers = gate._headers(websocket.scope)
    token = (gate._cookie(headers, gate.ADMIN_COOKIE) or "").strip()
    try:
        ctx = await asyncio.to_thread(resolve_superuser, token) if token else None
    except Exception:  # noqa: BLE001 — an expired or foreign sign-in
        ctx = None
    if not ctx:
        return await _close_with(websocket, NOT_SIGNED_IN)
    try:
        model_id = await asyncio.to_thread(assistant_voice.voice_model_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("demo assistant voice: model lookup failed (%s)", type(exc).__name__)
        model_id = None
    if not model_id:
        return await _close_with(websocket, NOT_SET_UP)
    session_id = sessionId if sessionId and _SESSION_ID.match(sessionId) else None
    page = page if page and page.startswith("/admin") and len(page) <= 300 else None
    ip, session = gate.visitor(websocket.scope)

    # The shared handler takes its caller from an Authorization header (a
    # browser socket cannot set one, so the cookie's token is handed over);
    # the route's caller check is that this is the same signed-in visitor.
    websocket.scope["headers"] = list(websocket.scope.get("headers") or []) + [
        (b"authorization", f"Bearer {token}".encode("latin-1"))]
    websocket.__dict__.pop("_headers", None)

    async def _authenticate(authorization: Optional[str]) -> Dict[str, Any]:
        if (authorization or "") != f"Bearer {token}":
            raise HTTPException(status_code=403, detail="Not the signed-in visitor")
        return ctx

    from endpoints.voice_live import VoiceRoute, serve_voice_route
    from runtime.agents.custom import CUSTOM_AGENT_KIND

    route = VoiceRoute(
        agent_slug=assistant.BUILDER_SLUG,
        authenticate=_authenticate,
        voice_config=assistant_voice.live_config(model_id),
        build_brain=_voice_brain(ctx, ip, session, page),
        own_page=True,
    )

    async def _serve() -> None:
        await serve_voice_route(websocket, route, agent_kind=CUSTOM_AGENT_KIND,
                                session_id=session_id)

    await demo_voice.guard(websocket, _serve)
