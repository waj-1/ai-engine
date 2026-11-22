"""Request-scoped agent_profile_versions pin (mirrors core/model_resolver).

The eval runner sends `X-WAJ-Profile-Version: <version_id>`; a middleware parks
it here; `core.agent_profile.AgentProfileLoader` (via runtime.agent) loads that
immutable snapshot instead of the live/active profile — for that one request.
Operator-gated in the middleware, exactly like the model/prompt overrides.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

_PROFILE_VERSION: ContextVar[Optional[str]] = ContextVar(
    "_PROFILE_VERSION", default=None,
)


def set_profile_version(v: Optional[str]) -> Any:
    return _PROFILE_VERSION.set(v)


def clear_profile_version() -> None:
    _PROFILE_VERSION.set(None)


def get_profile_version() -> Optional[str]:
    return _PROFILE_VERSION.get()


# Request-scoped PROFILE pin (X-WAJ-Profile-Id) — the portal's Test drawer and
# the admin MCP `chat` tool send the profile the operator is LOOKING AT, so a
# draft copy or a specialist answers its own test chat instead of the kind's
# active profile. Unlike the version pin it needs no saved snapshot: the row is
# built exactly the way its own environment builds it (live in dev/staging,
# promoted snapshot in prod). Operator-gated in the same middleware, and the
# runtime only honours it for a profile of the SAME agent_kind as the endpoint
# being called — it swaps config within a kind, never the kind or tenancy.
_PROFILE_ID: ContextVar[Optional[str]] = ContextVar("_PROFILE_ID", default=None)


def set_profile_pin(profile_id: Optional[str]) -> Any:
    return _PROFILE_ID.set(profile_id)


def clear_profile_pin() -> None:
    _PROFILE_ID.set(None)


def get_profile_pin() -> Optional[str]:
    return _PROFILE_ID.get()
