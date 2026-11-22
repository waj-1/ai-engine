"""Crash-safe reconcile for the two MCP membership joins.

Both ``agent_profile_mcp_servers`` (profile → attached MCPs) and
``agent_mcp_tools`` (bundle → member tools) are edited as "replace the whole
ordered set". The naive way — delete every row for the parent, then
re-insert the desired ones — leaves the join **empty** if the process dies
(or a single write errors) between the delete and the last insert, silently
stripping a live agent of its tools. That is the #1032 / #1380 incident
class, and MCP callers are automation, so they hit the window more often
than a human clicking a form.

These helpers instead **upsert the desired rows first** (on the composite
PK), **then delete only the now-stale rows**. At no point is the set empty:
a failure between the upsert and the delete leaves the prior wiring plus the
new rows (a superset — the agent has *more* tools, never zero); a failure
mid-upsert leaves the prior rows plus whatever committed. The one path that
does end empty is an explicit "clear all" (``desired`` empty), which is a
single delete and can't leave a worse-than-requested state.

Both join tables carry a composite primary key
(``agent_profile_mcp_servers`` → ``(profile_id, mcp_server_id)``,
``agent_mcp_tools`` → ``(mcp_server_id, tool_id)``; see
``supabase/migrations/20260705120000_mcp_foundations.sql``), so the upsert
conflict target is the PK and a reorder just rewrites ``position`` in place.

Callers own validation and audit; these helpers do the write and return the
prior membership (ordered) so the caller can record an honest audit
``before``. They are shared by the MCP tools (``admin_mcp/tools_configure``)
and the portal handlers (``profile_mcp_attach_handlers``,
``mcp_handlers``) so the two stay in lockstep on the safe pattern.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def _rows(resp: Any) -> List[Dict[str, Any]]:
    return getattr(resp, "data", None) or []


def reconcile_profile_mcp_attachments(
    client,
    profile_id: str,
    desired: Sequence[Tuple[str, Optional[List[str]]]],
) -> List[str]:
    """Reconcile a profile's ``agent_profile_mcp_servers`` rows.

    ``desired`` is an ordered sequence of ``(mcp_server_id, tool_subset)``
    pairs (``tool_subset`` a list of tool names, or ``None`` for "all"); the
    sequence index becomes ``position``. Assumes the caller has already
    validated attach-ability and tool_subset names.

    Returns the ordered ``mcp_server_id`` list present BEFORE the change,
    for the caller's audit ``before``.
    """
    before_rows = _rows(
        client.table("agent_profile_mcp_servers")
        .select("mcp_server_id, position")
        .eq("profile_id", profile_id)
        .execute()
    )
    before_ids = [
        r["mcp_server_id"]
        for r in sorted(before_rows, key=lambda r: r.get("position") or 0)
    ]

    new_rows = [
        {
            "profile_id": profile_id,
            "mcp_server_id": mid,
            "tool_subset": subset or None,
            "position": pos,
        }
        for pos, (mid, subset) in enumerate(desired)
    ]
    # Upsert desired FIRST — the join is never emptied by this write.
    if new_rows:
        client.table("agent_profile_mcp_servers").upsert(
            new_rows, on_conflict="profile_id,mcp_server_id"
        ).execute()

    # Then drop only rows no longer desired.
    keep = {mid for mid, _ in desired}
    stale = [mid for mid in before_ids if mid not in keep]
    if stale:
        client.table("agent_profile_mcp_servers").delete().eq(
            "profile_id", profile_id
        ).in_("mcp_server_id", stale).execute()

    return before_ids


def reconcile_bundle_members(
    client,
    mcp_server_id: str,
    tool_ids: Sequence[str],
) -> List[str]:
    """Reconcile a bundle's ``agent_mcp_tools`` membership.

    ``tool_ids`` is the ordered, deduped, already-validated tool id list;
    the index becomes ``position``. Returns the prior member ``tool_id``
    list (ordered) for the caller's audit ``before``.
    """
    before_rows = _rows(
        client.table("agent_mcp_tools")
        .select("tool_id, position")
        .eq("mcp_server_id", mcp_server_id)
        .execute()
    )
    before_ids = [
        r["tool_id"]
        for r in sorted(before_rows, key=lambda r: r.get("position") or 0)
    ]

    new_rows = [
        {"mcp_server_id": mcp_server_id, "tool_id": tid, "position": pos}
        for pos, tid in enumerate(tool_ids)
    ]
    if new_rows:
        client.table("agent_mcp_tools").upsert(
            new_rows, on_conflict="mcp_server_id,tool_id"
        ).execute()

    keep = set(tool_ids)
    stale = [tid for tid in before_ids if tid not in keep]
    if stale:
        client.table("agent_mcp_tools").delete().eq(
            "mcp_server_id", mcp_server_id
        ).in_("tool_id", stale).execute()

    return before_ids


def subset_names_for(attachable: Dict[str, Any]) -> Tuple[Optional[set], Optional[str]]:
    """The tool names an agent may pin on this MCP server, or why they could
    not be worked out: ``(names, None)`` or ``(None, reason)``.

    A bundle, or an external server registered with a subset of its own, has
    a stored list. An external server registered WITHOUT one exposes every
    tool it lists, so there is no stored list to check against — a pinned
    name was refused as "unknown" even when the server served it, and the
    only way through was to copy the server's whole tool list onto the server
    row first (2026-11-22). Those are checked against a live ``tools/list``.
    A server that cannot be reached is refused rather than waved through: a
    mistyped name would otherwise bind nothing and say nothing.
    """
    names = attachable.get("tool_names") or []
    if names or not attachable.get("exposes_all_tools"):
        return set(names), None
    import os

    from runtime.tools._http_adapter import _classify_endpoint
    from runtime.tools._mcp_adapter import list_tools_cached

    label = attachable.get("name") or attachable.get("mcp_server_id")
    endpoint = (attachable.get("endpoint") or "").strip()
    try:
        classification = _classify_endpoint(endpoint)
        allow_local = os.getenv("MCP_ALLOW_LOCAL_ENDPOINTS", "").strip().lower() \
            in {"1", "true", "yes", "on"}
        if classification == "blocked" and not allow_local:
            return None, (f"{label}: its address is local or internal and is "
                          "blocked, so its tools cannot be listed")
        env_var = attachable.get("auth_env_var")
        # Same rule as discover: the secret only goes to a public address.
        token = os.getenv(env_var) if (env_var and classification == "public") else None
        found = list_tools_cached(endpoint, token)
    except Exception as exc:  # noqa: BLE001 — reported to the caller, never raised
        return None, f"{label}: could not list its tools to check the names ({exc})"
    return {t.get("name") for t in (found or [])
            if isinstance(t, dict) and t.get("name")}, None
