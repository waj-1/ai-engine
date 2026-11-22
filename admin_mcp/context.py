"""Per-request actor identity for MCP tool calls.

The auth middleware sets MCP_ACTOR before the protocol layer runs; tools
read it via current_actor() to attribute audit rows to the real user.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Dict, Optional

MCP_ACTOR: ContextVar[Optional[Dict[str, str]]] = ContextVar("MCP_ACTOR", default=None)


def current_actor() -> Dict[str, str]:
    actor = MCP_ACTOR.get()
    if actor is None:
        raise RuntimeError(
            "MCP actor is not set — tool called outside an authenticated request"
        )
    return actor
