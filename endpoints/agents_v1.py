"""The generic agent API: `POST /v1/agents/{slug}/chat` and
`WS /v1/agents/{slug}/voice`.

One route for every `agent_kind='custom'` profile (runtime/agents/custom.py),
addressed by the profile's slug — so creating an agent in the portal or over
MCP gives it an endpoint with no code change and no deploy. Every other agent
in this service has a route written for it in `main.py`; this is the route the
open-source release is built around (docs/open-source/ROADMAP.md).

OFF by default. The route answers only where `ENABLE_GENERIC_AGENTS=1`, and
404s everywhere else, so merging this changes nothing on a running stack until
an operator turns it on. It is also 404'd on every scoped build by
`main.deployment_gate` (the `/v1/` entry, core.deployment.blocked_path_prefixes).

Auth: `Authorization: Bearer <token>`, where the token is one of

- a **portal-issued API key** (`wae_...`, Settings > API keys) with the `chat`
  or `admin` scope: `core/api_keys.py`. The turn runs AS THE KEY: its row id
  is the session's `user_id`, so a key can list, continue and read only the
  conversations it started, and two keys never see each other's;
- a signature-verified **superuser token**, the same trust boundary as the
  admin portal (`admin.auth.resolve_superuser`). Unchanged; it is how the
  portal's Test panel and the eval runners call this route.

A custom agent can be attached to any MCP server or tool an operator
configures, so "anyone holding any valid login may call any agent by slug" is
not a boundary worth shipping: a caller needs a key someone issued on purpose.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field

from common.sse import sse_response
from core.agent_profile import MissingProfileError, MissingSnapshotError
from core.channel_resolver import enforce_channel_kind, resolve_channel, root_span_attrs, tag_active_span

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["agents"])

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


def generic_agents_enabled() -> bool:
    """Read at call time, so a deployment flips it by env and tests can patch it."""
    return (os.getenv("ENABLE_GENERIC_AGENTS") or "").strip().lower() in ("1", "true", "yes")


class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000, description="The user's message.")
    session_id: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-fA-F-]{32,36}$",
        description="Continue this conversation. Omit to start a new one; its id comes back in "
                    "the stream's `session` event.")
    locale: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z]{2,3}([-_][A-Za-z]{2})?$",
        description="Language hint (e.g. `ar`, `en-GB`). The agent still replies in the language "
                    "the user writes in.")


async def _require_caller(authorization: Optional[str]) -> Dict[str, Any]:
    from core import api_keys

    is_bearer, token = api_keys.split_bearer(authorization)
    if not is_bearer:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if api_keys.looks_like_api_key(token):
        return await api_keys.authenticate(token, api_keys.SCOPE_CHAT)
    from admin.auth import resolve_superuser

    # Synchronous Supabase round trip: never inline on the event loop.
    return await asyncio.to_thread(resolve_superuser, token)


async def open_turn(
    slug: str,
    request: Request,
    authorization: Optional[str],
    *,
    message: str,
    session_id: Optional[str] = None,
    locale: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    session_for: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> Callable[[], AsyncGenerator[Dict[str, Any], None]]:
    """Everything one turn of the agent named `slug` needs before it streams.

    The flag, the caller check, the public demo's daily cap, the channel check
    and the agent build, in that order; raises `HTTPException` where one says
    no. Returns the factory of the turn's event stream (`run_turn`'s events).
    Shared by this module's chat route and the OpenAI-compatible one
    (endpoints/openai_compat.py), so the two cannot drift apart on who may
    call an agent or what guards a turn.

    `history` is passed to `run_turn` only when given (a caller that keeps the
    conversation itself); otherwise the session's stored turns are the history.
    `session_for` picks the session once the caller is known (it is handed the
    caller context), for a route whose session id depends on who is calling.
    """
    import re

    if not generic_agents_enabled() or not re.match(_SLUG_PATTERN, slug):
        raise HTTPException(status_code=404, detail="Not found")
    caller = await _require_caller(authorization)
    if session_for is not None:
        session_id = session_for(caller)

    from demo_mode import settings as demo_settings

    if demo_settings.enabled():
        # The public demo's hard daily cap (demo_mode/limits.py): once the
        # day's turns are used, answer with a plain message and call no model.
        from demo_mode import limits as demo_limits

        if not await asyncio.to_thread(demo_limits.take_turn):
            async def _capped() -> AsyncGenerator[Dict[str, Any], None]:
                yield {"type": "answer", "content": demo_limits.CAP_MESSAGE}
                yield {"type": "done"}

            return _capped

    from runtime.agents.custom import CUSTOM_AGENT_KIND, create_custom_agent

    resolve_channel(request)
    enforce_channel_kind(request, CUSTOM_AGENT_KIND, agent_slug=slug)
    tag_active_span(request, agent_kind=CUSTOM_AGENT_KIND)

    try:
        # Off the loop: the constructor loads the profile (blocking supabase-py).
        agent = await asyncio.to_thread(create_custom_agent, slug, locale=locale)
    except (MissingProfileError, MissingSnapshotError):
        # Unknown slug, an inactive agent, or one never published to this
        # environment: all "no such agent" to a caller.
        raise HTTPException(status_code=404, detail="Agent not found")
    except Exception as exc:  # noqa: BLE001
        logger.error("v1 agents: build failed for %s: %s", slug, exc, exc_info=True)
        raise HTTPException(status_code=503, detail="This agent is not available right now.")

    async def _source() -> AsyncGenerator[Dict[str, Any], None]:
        from common.chat_session import TurnIdentity, run_turn

        identity = TurnIdentity(
            agent_kind=CUSTOM_AGENT_KIND,
            session_id=session_id,
            user_id=caller.get("user_id"),
            business_id=None,
            session_type="custom",
            source="api",
            session_metadata={"agent_slug": slug},
        )
        supplied = {} if history is None else {"history": history}
        # The caller's id lets the agent read and save ITS memories about
        # this caller (off unless the agent's memory settings turn it on).
        async for event in run_turn(agent=agent, message=message, identity=identity,
                                    profile=agent.profile,
                                    extra_context={"user_id": caller.get("user_id")},
                                    **supplied):
            yield event

    return _source


def turn_trace_attributes(request: Request, slug: str, session_id: Optional[str]) -> Dict[str, Any]:
    """The root-span attributes of one custom-agent turn."""
    from demo_mode import settings as demo_settings
    from runtime.agents.custom import CUSTOM_AGENT_KIND

    attrs = root_span_attrs(request, agent_kind=CUSTOM_AGENT_KIND,
                            session_id=session_id, agent_slug=slug)
    if demo_settings.enabled():
        # The public demo's own sample traffic marks its turns from inside
        # this process (demo_mode/retention.py); no other request can.
        from demo_mode import retention

        attrs.update(retention.trace_mark(request.headers))
    return attrs


@router.post("/agents/{slug}/chat")
async def agent_chat(
    slug: str,
    body: AgentChatRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Stream one turn of the agent named `slug` as server-sent events."""
    source = await open_turn(slug, request, authorization, message=body.message,
                             session_id=body.session_id, locale=body.locale)
    return sse_response(
        source,
        error_message="An error occurred. Please try again.",
        trace_name="custom.agent",
        trace_attributes=turn_trace_attributes(request, slug, body.session_id),
    )


@router.websocket("/agents/{slug}/voice")
async def agent_voice(
    websocket: WebSocket,
    slug: str,
    sessionId: Optional[str] = Query(None),
    locale: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    simRun: Optional[str] = Query(None),
    profileId: Optional[str] = Query(None),
):
    """A duplex voice call with the agent named `slug`.

    The wire protocol and the whole call are the shared voice handler's
    (`endpoints/voice_live.py`, `voice/live/protocol.py`); this route only
    says WHICH agent and WHO may call. Same gates as the chat route: off
    unless `ENABLE_GENERIC_AGENTS=1`, never on a scoped build, and the caller
    check is `_require_caller`. A browser cannot set a header on a WebSocket,
    so a caller without an `Authorization` header sends
    `{"type": "auth", "token": "..."}` as its first frame.

    `profileId` asks for a draft copy of this agent (the portal's test call);
    it is honoured for an operator only.
    """
    import re

    from core.deployment import active_scope

    if (not generic_agents_enabled() or not re.match(_SLUG_PATTERN, slug)
            or active_scope() is not None):
        # Closed before accept: the handshake fails as if no such route.
        await websocket.close(code=1008)
        return

    from endpoints.voice_live import VoiceRoute, serve_voice_route
    from runtime.agents.custom import CUSTOM_AGENT_KIND

    pin = (profileId or "").strip()

    async def _serve() -> None:
        await serve_voice_route(
            websocket,
            VoiceRoute(
                agent_slug=slug,
                # Looked up at call time so the caller check stays the chat route's.
                authenticate=lambda authorization: _require_caller(authorization),
                profile_pin=pin if re.fullmatch(r"[0-9a-fA-F-]{32,36}", pin) else None,
            ),
            agent_kind=CUSTOM_AGENT_KIND,
            session_id=sessionId, locale=locale, provider=provider, sim_run=simRun,
        )

    from demo_mode import settings as demo_settings

    if demo_settings.enabled():
        # The public demo's voice caps (demo_mode/voice.py): call length,
        # calls at once and the day's seconds, whatever the agent's own
        # voice settings say.
        from demo_mode import voice as demo_voice

        await demo_voice.guard(websocket, _serve)
        return
    await _serve()
