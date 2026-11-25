"""The showcase's public routes (demo_mode/showcase.py). 404 unless
``DEMO_MODE`` and ``DEMO_SHOWCASE`` are on.

    GET  /showcase/api/catalog              the four agents, what voice and chat can do now
    GET  /showcase/api/{key}/cases          that agent's curated story cards
    POST /showcase/api/chat                 one chat turn (server-sent events)
    WS   /showcase/api/{key}/voice          a voice call (first frame: {"type": "auth", "token": "showcase"})

The two GETs are public data (a product page prefetches them on its server).
A chat turn or a call needs an Origin from ``DEMO_SHOWCASE_ORIGINS``, a
curated case, and room under the showcase's caps. A refusal is a 403 / 429
with ``{"detail": "[code] sentence", "code": ...}``, or for a call a close
4429 with that sentence as its reason.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from demo_mode import settings, showcase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/showcase/api", tags=["showcase"])

_KEY = r"^(collection|sales|support|inquiries)$"
_CASE = r"^[A-Z][0-9]{2}$"
_LOCALE = r"^(en|ar)(-[A-Za-z]{2})?$"


def _require_on() -> None:
    if not settings.showcase_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _refusal(status: int, message: str) -> JSONResponse:
    code = message[1:message.index("]")] if message.startswith("[") and "]" in message else "refused"
    return JSONResponse({"detail": message, "code": code}, status_code=status,
                        headers={"Cache-Control": "no-store",
                                 **({"Retry-After": "300"} if status == 429 else {})})


def _ip(scope: Dict[str, Any]) -> str:
    from demo_mode.gate import visitor

    return visitor(scope)[0]


@router.get("/catalog")
async def showcase_catalog():
    _require_on()
    from runtime.tools.agents_demo import data
    from runtime.tools.agents_demo.brand import BRAND

    try:
        pages = await asyncio.to_thread(showcase.agent_page_ids)
    except Exception:  # noqa: BLE001 (the links fall back to the portal's home)
        pages = {}
    base = settings.public_url()
    voice_state = await asyncio.to_thread(showcase.voice_state)
    chat_state = await asyncio.to_thread(showcase.chat_state)
    agents = []
    for key in ("collection", "sales", "support", "inquiries"):
        agent = showcase.AGENTS[key]
        page = (f"{base}/admin/agents?env=staging&profile_id={pages[key]}" if key in pages
                else f"{base}/admin/agents?env=staging")
        agents.append({"key": key, "slug": agent.slug, "name": agent.display_name,
                       "direction": agent.direction, "voice": voice_state != "off",
                       "cases": len(showcase.CASES[key]), "platform_url": page})
    body = {
        "brand": {k: BRAND[k] for k in ("name_en", "name_ar", "tagline_en", "tagline_ar")},
        "data_note": showcase._no_em_dash(data.catalog()["data_note"]),
        "agents": agents,
        "speech_tags": list(showcase.SPEECH_TAGS),
        "voice": {"state": voice_state,
                  "max_call_seconds": settings.showcase_voice_max_call_seconds()},
        "chat": {"state": chat_state},
        "platform": {"url": base, "repo_url": settings.repo_url()},
        "privacy": {"en": "Conversations are fictional and visible in the public demo until its nightly reset. "
                          "Calls are not recorded.",
                    "ar": "المحادثات تجريبية وتظهر في العرض التجريبي العام حتى يُمسح كل ليلة. "
                          "المكالمات لا تُسجَّل."},
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get("/{key}/cases")
async def showcase_cases(key: str):
    _require_on()
    if key not in showcase.AGENTS:
        raise HTTPException(status_code=404, detail="Not Found")
    agent = showcase.AGENTS[key]
    return {"agent": key, "direction": agent.direction, "cases": showcase.stories(key)}


class ShowcaseChat(BaseModel):
    agent: str = Field(..., pattern=_KEY)
    case_id: str = Field(..., pattern=_CASE)
    message: str = Field(..., min_length=1, max_length=1000)
    session_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{32,36}$")
    locale: str = Field("en-US", pattern=_LOCALE)


@router.post("/chat")
async def showcase_chat(body: ShowcaseChat, request: Request):
    _require_on()
    if not showcase.origin_allowed(request.headers.get("origin")):
        return _refusal(403, showcase.MSG_ORIGIN)
    try:
        brief = showcase.brief(body.agent, body.case_id, body.locale)
    except LookupError:
        raise HTTPException(status_code=404, detail="Unknown use case")
    problem = await asyncio.to_thread(showcase.take_chat_turn, _ip(request.scope))
    if problem:
        return _refusal(429, problem)

    from common.sse import sse_response
    from core.channel_resolver import resolve_channel, root_span_attrs, tag_active_span
    from runtime.agents.custom import CUSTOM_AGENT_KIND, create_custom_agent

    slug = showcase.AGENTS[body.agent].slug
    resolve_channel(request)
    tag_active_span(request, agent_kind=CUSTOM_AGENT_KIND)
    try:
        agent = await asyncio.to_thread(create_custom_agent, slug, locale=body.locale, brief=brief)
    except Exception as exc:  # noqa: BLE001
        logger.error("showcase: agent build failed for %s: %s", slug, exc)
        return _refusal(503, "[off] This agent is not available right now.")

    async def _source() -> AsyncGenerator[Dict[str, Any], None]:
        from common.chat_session import TurnIdentity, run_turn

        identity = TurnIdentity(
            agent_kind=CUSTOM_AGENT_KIND, session_id=body.session_id, user_id=None,
            business_id=None, session_type="custom", source="api",
            session_metadata={"agent_slug": slug, "client": "showcase", "case": body.case_id},
        )
        async for event in run_turn(agent=agent, message=body.message, identity=identity,
                                    profile=agent.profile):
            yield event

    return sse_response(
        _source, error_message="An error occurred. Please try again.",
        trace_name="custom.agent",
        trace_attributes=root_span_attrs(request, agent_kind=CUSTOM_AGENT_KIND,
                                         session_id=body.session_id, agent_slug=slug),
    )


async def _admit(authorization: Optional[str]) -> Dict[str, Any]:
    """The showcase call's caller: anonymous, admitted by the route's own
    checks (origin, case, caps) before the handler ever asks."""
    token = (authorization or "").split(" ", 1)[-1].strip()
    if token != showcase.CALL_TOKEN:
        raise HTTPException(status_code=401, detail="Not a showcase call")
    return {"user_id": None, "id": "showcase"}


@router.websocket("/{key}/voice")
async def showcase_voice(
    websocket: WebSocket,
    key: str,
    case: str = Query(..., pattern=_CASE),
    locale: str = Query("en-US", pattern=_LOCALE),
    sessionId: Optional[str] = Query(None, pattern=r"^[0-9a-fA-F-]{32,36}$"),
):
    if not settings.showcase_enabled() or key not in showcase.AGENTS:
        await websocket.close(code=1008)
        return
    if not showcase.origin_allowed(websocket.headers.get("origin")):
        # Accepted first so the browser sees the reason, like every refusal here.
        await showcase._refuse(websocket, showcase.MSG_ORIGIN)
        return
    try:
        brief = showcase.brief(key, case, locale)
        greeting = showcase.greeting(key, case, locale)
    except LookupError:
        await showcase._refuse(websocket, "Unknown use case.")
        return

    from endpoints.voice_live import VoiceRoute, serve_voice_route
    from runtime.agents.custom import CUSTOM_AGENT_KIND

    async def _serve() -> None:
        await serve_voice_route(
            websocket,
            VoiceRoute(agent_slug=showcase.AGENTS[key].slug, authenticate=_admit,
                       brief=brief, greeting=greeting),
            agent_kind=CUSTOM_AGENT_KIND, session_id=sessionId,
            locale=showcase.normalize_tag(locale),
        )

    await showcase.guard(websocket, _ip(websocket.scope), _serve)
