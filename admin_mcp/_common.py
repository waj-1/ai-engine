"""Shared plumbing for admin MCP tools: env resolution, audit attribution,
confirm gating, and thin table access. Every tool module uses these so the
behavior (audit shape, env fallback, error payloads) stays uniform."""
from __future__ import annotations

import functools
import inspect
from contextlib import contextmanager
from typing import Annotated, Any, Dict, List, Literal, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from admin.audit import record_change
from admin_mcp.context import current_actor

ALLOWED_ENVS = ("staging", "prod")

# Closed-set signature aliases: FastMCP turns Literal into a JSON-schema
# enum, so the MCP client rejects a bad value before it costs a round-trip.
# Runtime checks (validate_env, the per-action err() paths) stay as the
# backstop for direct Python callers, which bypass schema validation.
#
# Naming contract (one meaning per name, across every tool):
#   env             — the CONFIG BRANCH: which `environment` rows of the AI
#                     config DB the tool reads/writes. Always this meaning.
#   row_environment — a row's own `environment` COLUMN (update_profile,
#                     deployment_targets, env_vars). Old name: `environment`.
#   trace_env       — the Laminar `waj.env` TAG on traces (live_analytics,
#                     caching_analytics, voice_insights). Old name:
#                     `environment`.
# The old names stay accepted as deprecated aliases (resolve_alias below).
Env = Annotated[Literal["staging", "prod"], Field(
    description="Config branch: which `environment` rows of the AI config DB "
                "to read/write. staging = the editable working set (default); "
                "prod = what production serves.")]
# Row-environment filters where "" means "default / no filter".
EnvFilter = Literal["", "staging", "prod"]
RowEnv = Annotated[EnvFilter, Field(
    description="The row's own `environment` column (staging|prod). '' = "
                "default to `env` (create) or leave unchanged (update).")]
# Laminar waj.env tags can carry "dev" too — wider than the config-DB Env.
TraceEnv = Annotated[Literal["", "dev", "staging", "prod"], Field(
    description="Laminar `waj.env` trace tag to scope telemetry to "
                "(dev|staging|prod); '' = all environments.")]
Confirm = Annotated[bool, Field(
    description="false (default) = dry run: report what WOULD change and "
                "change nothing. true = execute.")]

# Shared tool annotations — hosts key permission UX off these (a read-only
# tool can be auto-approved; destructive ones get a confirm dialog). Only
# set where truthful for EVERY action a tool multiplexes: mixed read/write
# tools stay unannotated, which hosts already treat as worst-case.
READ_ONLY = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)


def resolve_alias(new_value: Any, old_value: Any, *, new_name: str,
                  old_name: str) -> Any:
    """Return whichever of a renamed parameter and its deprecated alias was
    supplied. Both supplied with DIFFERENT values is a caller error (we
    cannot know which one they meant), raised as ValueError so tool_errors
    turns it into the standard err() payload."""
    if new_value and old_value and new_value != old_value:
        raise ValueError(f"pass {new_name} (or its deprecated alias "
                         f"{old_name}), not both with different values")
    return new_value or old_value


def page(items: List[Any], limit: int, offset: int = 0,
         max_limit: int = 200) -> Dict[str, Any]:
    """Slice `items` for an MCP response and say what was cut, so a caller
    never mistakes a capped list for the whole set."""
    limit = max(1, min(int(limit or 1), max_limit))
    offset = max(0, int(offset or 0))
    window = items[offset:offset + limit]
    return {"items": window, "total": len(items), "offset": offset,
            "limit": limit, "has_more": offset + len(window) < len(items)}


def validate_env(env: str) -> str:
    if env not in ALLOWED_ENVS:
        raise ValueError(f"env must be one of {ALLOWED_ENVS}, got {env!r}")
    return env


def client_for_env(env: str):
    """Service client for the env branch; local single-DB fallback (same
    semantics as admin.router.client_for, without importing the router)."""
    from core.auth import CrossEnvNotConfiguredError, get_service_client, \
        get_service_client_for_env
    validate_env(env)
    try:
        return get_service_client_for_env(env)
    except CrossEnvNotConfiguredError:
        return get_service_client()


@contextmanager
def use_env(env: str):
    """Pin _ADMIN_ENV for the duration so reused portal code that calls
    core.auth.admin_service_client() resolves the same env branch."""
    from core.auth import reset_admin_env, set_admin_env
    token = set_admin_env(validate_env(env))
    try:
        yield
    finally:
        reset_admin_env(token)


def actor_audit(entity_type: str, entity_id: Optional[str], action: str,
                before: Optional[Dict[str, Any]] = None,
                after: Optional[Dict[str, Any]] = None) -> None:
    actor = current_actor()
    record_change(entity_type=entity_type, entity_id=entity_id, action=action,
                  actor_user_id=actor["user_id"], before=before, after=after)


def not_confirmed(description: str, **extra) -> Dict[str, Any]:
    return {
        "confirmed": False,
        "would": description,
        "hint": "No change was made. Re-call with confirm=true to execute.",
        **extra,
    }


def err(message: str, hint: Optional[str] = None, **extra) -> Dict[str, Any]:
    return {"error": message, "hint": hint, **extra}


def tool_errors(fn):
    """Convert domain validation failures (ValueError) into the standard
    {"error", "hint"} payload instead of raising through the MCP protocol
    layer. Decorate tool functions BELOW @mcp.tool() (i.e. @mcp.tool() is
    outermost, @tool_errors is innermost) so FastMCP still builds its input
    schema from the wrapped function's real signature — functools.wraps
    sets __wrapped__, and inspect.signature() (which FastMCP uses to derive
    the tool schema) follows it through automatically.

    Works on both sync and async tools: an `async def` tool must be wrapped
    in an `async def` wrapper that awaits it (returning the coroutine
    unawaited from a sync wrapper would hand FastMCP a coroutine object
    instead of the tool's result), so coroutine functions get their own
    async wrapper branch. The sync path is unchanged."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ValueError as exc:
                return err(str(exc))
        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return err(str(exc))
    return wrapper


def rows(resp: Any) -> List[Dict[str, Any]]:
    return getattr(resp, "data", None) or []


def list_rows(client, table: str, *, select: str = "*",
              order_by: str = "created_at", desc: bool = True,
              limit: int = 100,
              filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    q = client.table(table).select(select)
    for col, val in (filters or {}).items():
        if val is not None:
            q = q.eq(col, val)
    q = q.order(order_by, desc=desc).limit(limit)
    return rows(q.execute())


def get_row(client, table: str, id_column: str, item_id: str) -> Optional[Dict[str, Any]]:
    found = rows(client.table(table).select("*").eq(id_column, item_id)
                 .limit(1).execute())
    return found[0] if found else None
