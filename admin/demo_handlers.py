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


def _build_assistant(page: Optional[str]):
    """The Builder assistant agent. If a visitor deleted or switched it off,
    seed it back once and try again (blocking)."""
    from core.agent_profile import MissingProfileError
    from demo_mode import assistant

    try:
        return assistant.create_agent(page=page)
    except MissingProfileError:
        from core.agent_profile import AgentProfileLoader
        from demo_mode import seed

        seed.seed()
        AgentProfileLoader.invalidate("custom", seed._env())
        return assistant.create_agent(page=page)


#: The assistant's turns, by conversation id, so a visitor who navigates away
#: mid-turn finds the result when the panel loads on the next page: the turn
#: keeps running (it is not tied to the browser's connection) and the panel
#: reads it back from GET /admin/demo/assistant/history. Kept in this
#: process's memory for an hour; the saved conversation holds the text.
_LIVE: Dict[str, Dict[str, Any]] = {}
_TASKS: set = set()
_LIVE_TTL_SEC = 3600.0
_KEEPALIVE_SEC = 10.0


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
    import time

    from common.sse import sse_response
    from demo_mode import assistant, limits, settings

    if not settings.enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    from admin.first_run_handlers import same_origin

    if not same_origin(request):
        raise HTTPException(status_code=403, detail="The assistant only works from the demo's own page.")
    # The per-visitor message limit is the gate's ("assistant" bucket); the
    # daily cap is taken here, so a refusal is an answer the panel shows.
    if not await asyncio.to_thread(limits.take_turn):
        return sse_response(_one_answer(limits.CAP_MESSAGE))
    try:
        agent = await asyncio.to_thread(_build_assistant, body.page)
    except Exception as exc:  # noqa: BLE001
        logger.error("demo assistant: build failed (%s: %s)", type(exc).__name__, exc)
        return sse_response(_one_answer(
            "The builder assistant is not available right now. Please try again in a minute."))

    ip, session = assistant.visitor_of(request.scope)
    turn = assistant.Turn(actor=ctx, ip=ip, session=session)
    queue: asyncio.Queue = asyncio.Queue()
    holder: Dict[str, Any] = {"live": None}
    _prune_live()

    def _start_live(session_id: str) -> None:
        live = {"answer": "", "steps": [], "changed": False, "done": False,
                "at": time.monotonic(), "user_id": ctx.get("user_id")}
        _LIVE[session_id] = live
        holder["live"] = live

    async def _emit(event: Dict[str, Any]) -> None:
        _record(holder["live"], event)
        await queue.put(event)

    async def _produce() -> None:
        from common.chat_session import TurnIdentity, run_turn
        from runtime.agents.custom import CUSTOM_AGENT_KIND

        token = assistant.TURN.set(turn)
        if body.session_id:
            _start_live(body.session_id)
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

    return sse_response(_source, error_message="The assistant hit an error. Please try again.",
                        trace_name="demo.builder_assistant")


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
        out["live"] = {k: live[k] for k in ("answer", "steps", "changed", "done")}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})
