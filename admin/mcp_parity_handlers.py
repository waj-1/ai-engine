"""GET /admin/api/mcp-flag-parity — read-only MCP-resolution health report.

For every active agent profile in the selected env, resolves the tool set the
way the runtime does — `resolve_mcp_tools_for_profile(profile, ctx, pool)` — and
reports the bound tool names, how many MCP attachments the profile carries, and
whether resolution raised.

`pool` is the agent kind's factory tool pool (built via admin.sync_tools.pool_for_kind).

Read-only, superuser-gated, env-scoped. NEVER writes. Degrades per-profile: one
bad profile is reported with an `error`, never crashes the whole report.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends
from fastapi.responses import JSONResponse

from admin.auth import require_superuser
from admin.router import router, _safe_list, current_env
from core.agent_profile import AgentProfileLoader
from core.auth import admin_service_client

logger = logging.getLogger(__name__)


def compute_mcp_flag_parity(env: str) -> Dict[str, Any]:
    """Resolve every active profile's tool set the way the runtime does and
    report what actually binds.

    Factored out of the route handler below so `admin_mcp/tools_ops.py`'s
    `mcp_flag_parity` MCP tool can call the exact same logic instead of
    duplicating it — this function is now the shared implementation; the
    route is a thin JSON wrapper. `tests/admin/test_mcp_parity_endpoint.py`
    exercises this via the route (patching module-level `admin_service_client`
    / `AgentProfileLoader`, which this function reads as free variables so
    that monkeypatching keeps working unchanged).
    """
    # Local imports keep this cheap to import and mirror how the runtime
    # resolves tools (see runtime/agent.py prepare()).
    from admin.sync_tools import pool_for_kind
    from runtime.tools._mcp_resolver import resolve_mcp_tools_for_profile

    client = admin_service_client()  # env-aware — the switcher-selected branch.
    rows = _safe_list(
        client.table("agent_profiles")
        .select("profile_id, agent_kind, status, environment")
        .eq("environment", env)
        .execute()
    )
    active_rows = [r for r in rows if (r.get("status") or "active") == "active"]

    # Stub request context: read-only resolution needs no real user token.
    resolve_ctx = {
        "user_token": None,
        "business_id": "00000000-0000-0000-0000-000000000000",
        "locale": "en",
        "session_id": None,
    }

    results: List[Dict[str, Any]] = []
    errors = 0
    for row in active_rows:
        agent_kind = row.get("agent_kind")
        profile_id = row.get("profile_id")
        try:
            pool = pool_for_kind(agent_kind)
            profile = AgentProfileLoader.load_active(agent_kind, env)
            # NOTE: resolve_mcp_tools_for_profile may call the network for
            # source='external' MCP attachments (a client's own MCP). The resolver
            # wraps that in try/except (logs + skips on failure), so this never
            # crashes — but an unreachable external MCP shows as FEWER tools.
            # External-MCP agents need a reachable endpoint for an accurate count.
            mcp = resolve_mcp_tools_for_profile(profile, resolve_ctx, pool)
            results.append({
                "agent_kind": agent_kind,
                "profile_id": profile_id,
                "attachments": len(profile.mcp_attachments or []),
                "tools": sorted(getattr(t, "name", "?") for t in mcp),
                "error": None,
            })
        except Exception as exc:  # noqa: BLE001 — degrade per-profile, never crash the report.
            logger.warning("mcp-flag-parity: profile %s (%s) failed (%s)", profile_id, agent_kind, exc)
            errors += 1
            results.append({
                "agent_kind": agent_kind,
                "profile_id": profile_id,
                "attachments": 0,
                "tools": [],
                "error": str(exc),
            })

    # Errored profiles first so an operator sees problems at the top.
    results.sort(key=lambda r: 0 if r.get("error") else 1)
    return {
        "env": env,
        "total": len(results),
        "errors": errors,
        "results": results,
    }


@router.get("/api/mcp-flag-parity")
async def mcp_flag_parity(
    ctx: dict = Depends(require_superuser),
    env: str = Depends(current_env),
) -> JSONResponse:
    return JSONResponse(compute_mcp_flag_parity(env))
