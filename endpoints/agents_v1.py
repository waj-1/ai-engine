"""The generic agent API: `POST /v1/agents/{slug}/chat`.

One route for every `agent_kind='custom'` profile (runtime/agents/custom.py),
addressed by the profile's slug — so creating an agent in the portal or over
MCP gives it an endpoint with no code change and no deploy. Every other agent
in this service has a route written for it in `main.py`; this is the route the
open-source release is built around (docs/open-source/ROADMAP.md).

OFF by default. The route answers only where `ENABLE_GENERIC_AGENTS=1`, and
404s everywhere else, so merging this changes nothing on a running stack until
an operator turns it on. It is also 404'd on every scoped build by
`main.deployment_gate` (the `/v1/` entry, core.deployment.blocked_path_prefixes).

Auth, for now: a signature-verified superuser token — the same trust boundary
as the admin portal and the admin MCP server (`admin.auth.resolve_superuser`).
A custom agent can be attached to any MCP server or tool an operator
configures, and portal-issued API keys with their own scopes do not exist yet;
until they do, "anyone holding any valid login may call any agent by slug" is
not a boundary worth shipping. API keys replace this check.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request
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
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    from admin.auth import resolve_superuser

    # Synchronous Supabase round trip: never inline on the event loop.
    return await asyncio.to_thread(resolve_superuser, authorization.split(" ", 1)[1].strip())


@router.post("/agents/{slug}/chat")
async def agent_chat(
    slug: str,
    body: AgentChatRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Stream one turn of the agent named `slug` as server-sent events."""
    import re

    if not generic_agents_enabled() or not re.match(_SLUG_PATTERN, slug):
        raise HTTPException(status_code=404, detail="Not found")
    caller = await _require_caller(authorization)

    from runtime.agents.custom import CUSTOM_AGENT_KIND, create_custom_agent

    resolve_channel(request)
    enforce_channel_kind(request, CUSTOM_AGENT_KIND)
    tag_active_span(request, agent_kind=CUSTOM_AGENT_KIND)

    try:
        # Off the loop: the constructor loads the profile (blocking supabase-py).
        agent = await asyncio.to_thread(create_custom_agent, slug, locale=body.locale)
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
            session_id=body.session_id,
            user_id=caller.get("user_id"),
            business_id=None,
            session_type="custom",
            source="api",
            session_metadata={"agent_slug": slug},
        )
        async for event in run_turn(agent=agent, message=body.message, identity=identity,
                                    profile=agent.profile):
            yield event

    return sse_response(
        _source,
        error_message="An error occurred. Please try again.",
        trace_name="custom.agent",
        trace_attributes=root_span_attrs(request, agent_kind=CUSTOM_AGENT_KIND,
                                         session_id=body.session_id),
    )
