"""The generic AI-agents demo portal — public, no login.

Four agents for a fictional provider (runtime/tools/agents_demo/), each with a
set of use cases a visitor plays: inbound support, business inquiries, an
outbound collection call and an outbound sales call. Served by a full build
(engine.company.example/agents, where the landing page links) and by a deployment
scope that owns these kinds (core/deployment.py); every other scoped build
404s it (the deployment gate blocks other scopes' prefixes, and each route
here re-checks `serves_agents_portal()`).

Routes:
  GET  /agents                    picker + workspace page (one template)
  GET  /agents/{agent}            same page, opened on that agent
  GET  /agents/api/catalog        brand, agents, voice availability, WS path
  GET  /agents/api/{agent}/cases  that agent's use-case story cards
  POST /agents/chat/stream        text chat with one agent (SSE); `case_id`
                                  picks the use case for EVERY agent (blank →
                                  the first) — the inbound agents answer from
                                  that case's pre-rendered case file

The landing page (company.example / staging.example) calls the two GETs and the chat POST
cross-origin; core.cors.FirstPartyCORSMiddleware grants those origins.
  GET  /agents/health             liveness

Anonymous callers: text chat is rate-limited per IP (per minute and per day);
voice goes through the shared duplex endpoint, whose per-IP connect limiter,
concurrency caps, daily audio budget and session ceiling are the posture (the
profiles seed the caps — admin/seed_agents_demo.ANONYMOUS_VOICE_KEYS).

Conversation state between text turns (who is verified, what was sent)
rides a signed token in the SSE stream (`state` event) and comes back on the
next request — HTTP turns are stateless and replicas share nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, AsyncGenerator, Deque, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from common.sse import sse_response
from core.channel_resolver import enforce_channel_kind, resolve_channel, root_span_attrs, tag_active_span
from core.deployment import mount_prefix, serves_agents_portal
from runtime.tools.agents_demo import data
from runtime.tools.agents_demo.brand import BRAND

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents-portal"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

PORTAL_CLIENT_TAG = "agents_portal"
_STATE_MAX_AGE = 60 * 60 * 3
_AGENT_PATTERN = r"^(support|inquiries|collection|sales)$"

# Story fields a card renders — everything else on a case (brief, expected
# tools, customer id) is internal. `summary_*` is the one-paragraph "who you
# are + what happened" the cards show; `who_*`/`happened_*` stay for older
# clients. `say_more_*` are extra caller lines a card may offer beyond the
# three `say_*` ones (the landing page's suggestion carousel). Story text
# carries no emoji (tests/agents_demo enforces it).
_STORY_KEYS = (
    "id", "title_en", "title_ar", "headline_en", "headline_ar", "summary_en", "summary_ar", "who_en", "who_ar",
    "happened_en", "happened_ar", "goal_en", "goal_ar", "say_en", "say_ar", "say_more_en", "say_more_ar",
    "twist_en", "twist_ar",
    "great_en", "great_ar", "facts",
)


def _require_portal() -> None:
    if not serves_agents_portal():
        raise HTTPException(status_code=404, detail="Not found")


def _voice_path() -> str:
    """Where this process mounts the duplex WS (see main._mount_prefix)."""
    return mount_prefix() + "/voice/live"


# ---------------------------------------------------------------------------
# Anonymous text-chat limits (per replica — same trade as the voice caps)
# ---------------------------------------------------------------------------
_PER_MINUTE = int(os.getenv("AGENTS_PORTAL_CHAT_PER_MINUTE", "20"))
_PER_DAY = int(os.getenv("AGENTS_PORTAL_CHAT_PER_DAY", "400"))
_minute: Dict[str, Deque[float]] = defaultdict(deque)
_day: Dict[str, Deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers, so request.client is the real caller.
    return (request.client.host if request.client else "") or "unknown"


def _rate_limited(ip: str, now: Optional[float] = None) -> bool:
    now = now or time.time()
    for store, window, cap in ((_minute, 60.0, _PER_MINUTE), (_day, 86400.0, _PER_DAY)):
        q = store[ip]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= cap:
            return True
    _minute[ip].append(now)
    _day[ip].append(now)
    return False


# ---------------------------------------------------------------------------
# Signed conversation state
# ---------------------------------------------------------------------------
_PROCESS_SECRET = os.urandom(32).hex()


def _state_secret() -> str:
    """AGENTS_PORTAL_SECRET when set; otherwise derived from a server-only
    secret every deployment already has, so no new env var is required. The
    last resort (nothing configured at all) is a per-process random key:
    state then simply does not survive a hop to another replica."""
    explicit = os.getenv("AGENTS_PORTAL_SECRET")
    if explicit:
        return explicit
    base = os.getenv("AI_SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_JWT_SECRET")
    if base:
        return hashlib.sha256(f"agents-portal-state:{base}".encode()).hexdigest()
    return _PROCESS_SECRET


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_state_secret(), salt="agents-portal-state")


def sign_state(agent: str, case_id: Optional[str], session_id: str, state: Dict[str, Any]) -> str:
    return _serializer().dumps({"a": agent, "c": case_id or "", "s": session_id, "st": state})


def read_state(token: Optional[str], agent: str, case_id: Optional[str], session_id: str) -> Optional[Dict[str, Any]]:
    """The state for THIS (agent, case, session), or None — a token minted for
    another conversation, tampered or expired is ignored, never an error."""
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=_STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("a") != agent or payload.get("c") != (case_id or "") or payload.get("s") != session_id:
        return None
    st = payload.get("st")
    return st if isinstance(st, dict) else None


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def public_cases(agent_key: str) -> list:
    return [{k: c[k] for k in _STORY_KEYS if k in c} for c in data.cases(agent_key)]


def _voice_enabled(agent_kind: str, channel: Optional[dict]) -> bool:
    """`voice_config.enabled` for the agent's active profile; fails closed."""
    from core.agent_profile import AgentProfileLoader
    from voice.live.config import resolve_live_config

    try:
        profile = AgentProfileLoader.load_active(agent_kind)
        return bool(resolve_live_config(profile_voice=getattr(profile, "voice_config", None),
                                        channel=channel).enabled)
    except Exception:  # noqa: BLE001
        logger.debug("agents portal: voice flag unavailable for %s", agent_kind, exc_info=True)
        return False


def _locale(request: Request) -> str:
    q = (request.query_params.get("lang") or request.cookies.get("agents_locale") or "en").lower()
    return "ar" if q.startswith("ar") else "en"


def _page(request: Request, agent_key: Optional[str]):
    _require_portal()
    if agent_key is not None and agent_key not in data.AGENTS:
        raise HTTPException(status_code=404, detail="Not found")
    locale = _locale(request)
    response = templates.TemplateResponse(request, "agents/portal.html", {
        "locale": locale, "agent_key": agent_key or "", "brand": BRAND,
    })
    response.set_cookie("agents_locale", locale, samesite="lax", path="/", max_age=60 * 60 * 24 * 30)
    return response


@router.get("/agents/health")
async def agents_health():
    _require_portal()
    return {"ok": True}


@router.get("/agents/api/catalog")
async def agents_catalog(request: Request):
    _require_portal()
    resolve_channel(request)
    channel = getattr(request.state, "channel", None)
    flags = await asyncio.gather(*(asyncio.to_thread(_voice_enabled, a.kind, channel)
                                   for a in data.AGENTS.values()))
    agents = []
    for agent, voice in zip(data.AGENTS.values(), flags):
        agents.append({"key": agent.key, "kind": agent.kind, "direction": agent.direction,
                       "persona_en": agent.persona_en, "persona_ar": agent.persona_ar, "gender": agent.gender,
                       "voice": voice, "cases": len(data.cases(agent.key))})
    return {
        "brand": {k: BRAND[k] for k in ("name_en", "name_ar", "tagline_en", "tagline_ar")},
        "data_note": data.catalog()["data_note"],
        "voice_path": _voice_path(),
        "agents": agents,
    }


@router.get("/agents/api/{agent_key}/cases")
async def agents_cases(agent_key: str):
    _require_portal()
    if agent_key not in data.AGENTS:
        raise HTTPException(status_code=404, detail="Not found")
    return {"agent": agent_key, "direction": data.AGENTS[agent_key].direction,
            "cases": public_cases(agent_key)}


class AgentsChatRequest(BaseModel):
    agent: str = Field(..., pattern=_AGENT_PATTERN)
    message: str = Field(..., min_length=1, max_length=1000)
    session_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{32,36}$")
    locale: str = Field("en", pattern=r"^(en|ar)(-[A-Za-z]{2})?$")
    case_id: Optional[str] = Field(None, pattern=r"^[A-Za-z][0-9]{2}$")
    state: Optional[str] = Field(None, max_length=8000)


@router.post("/agents/chat/stream")
async def agents_chat_stream(body: AgentsChatRequest, request: Request):
    _require_portal()
    agent_meta = data.AGENTS[body.agent]
    kind = agent_meta.kind
    resolve_channel(request)
    enforce_channel_kind(request, kind)
    tag_active_span(request, agent_kind=kind)
    if _rate_limited(_client_ip(request)):
        return JSONResponse({"detail": "Too many messages — please wait a moment."}, status_code=429,
                            headers={"Retry-After": "60"})
    # Every agent plays a use case — inbound too, since the prompt-only
    # inbound agents answer from that case's pre-rendered case file. Blank →
    # the agent's first case.
    case_id = (body.case_id or "").upper() or data.cases(body.agent)[0]["id"]
    state = read_state(body.state, body.agent, case_id, body.session_id)

    from runtime.agents.agents_demo import create_demo_agent

    try:
        # Off the loop: the constructor loads the profile (blocking supabase-py).
        agent = await asyncio.to_thread(
            create_demo_agent, body.agent, locale=body.locale, call_case=case_id, state=state,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Unknown use case")
    except Exception as exc:  # noqa: BLE001
        logger.error("agents portal: agent build failed for %s: %s", kind, exc, exc_info=True)
        return JSONResponse({"detail": "This agent is not available right now."}, status_code=503)

    async def _source() -> AsyncGenerator[Dict[str, Any], None]:
        from common.chat_session import TurnIdentity, run_turn

        identity = TurnIdentity(
            agent_kind=kind, session_id=body.session_id, user_id=None, business_id=None,
            session_type="agents_demo", source="waj",
            session_metadata={"client": PORTAL_CLIENT_TAG, "case": case_id or ""},
        )
        async for event in run_turn(agent=agent, message=body.message, identity=identity,
                                    profile=agent.profile):
            if isinstance(event, dict) and event.get("type") == "done":
                yield {"type": "state",
                       "token": sign_state(body.agent, case_id, body.session_id, agent.state)}
            yield event

    return sse_response(
        _source,
        error_message="An error occurred. Please try again.",
        trace_name="agents_demo.agent",
        trace_attributes=root_span_attrs(request, agent_kind=kind, session_id=body.session_id),
    )


@router.get("/agents")
async def agents_portal(request: Request):
    return _page(request, None)


@router.get("/agents/{agent_key}")
async def agents_workspace(agent_key: str, request: Request):
    return _page(request, agent_key)
