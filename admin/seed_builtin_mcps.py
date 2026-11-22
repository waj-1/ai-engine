"""Seed one system-managed "built-in" bundle MCP per factory agent kind, and
attach each profile to its kind's bundle with a pinned tool_subset.

Runs in the lifespan AFTER sync_tools (agent_tools populated) and
seed_agent_profiles (profiles exist with resolved tool_ids). Idempotent:
upserts the bundle by (is_builtin, name), reconciles membership to match the
current tool set, and only creates a profile attachment when none exists
(so admin edits survive — mirrors seed_agent_profiles._apply_defaults).

Byte parity is proven separately by tests/runtime/test_mcp_parity.py; this
module just writes rows the parity harness then validates.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from admin.builtin_mcp_membership import builtin_mcp_name, ordered_membership
from admin.seed_agent_profiles import (
    _ALL_SCOPE_KINDS,
    _TOOL_KIND_ALIAS,
    visible_agent_kinds,
)
from admin.sync_tools import _FACTORIES, _call_factory
from core.auth import get_service_client
from core.deployment import scoped_build

logger = logging.getLogger(__name__)

# The kinds that own a Python tool factory (subset of _FACTORIES). Order is
# the _FACTORIES order.
_FACTORY_BY_KIND = {kind: (module, factory, kwargs)
                    for (kind, _src, module, factory, kwargs) in _FACTORIES}


def _seedable_kinds() -> List[str]:
    """Factory kinds visible in the current (full vs scoped) build."""
    visible = set(visible_agent_kinds())
    in_scope = scoped_build()
    out = []
    for kind in _FACTORY_BY_KIND:
        if kind not in visible:
            continue
        if kind in _ALL_SCOPE_KINDS and not in_scope:
            continue  # a full build never seeds a scope's kinds
        out.append(kind)
    return out


def _pool_names_for_kind(kind: str) -> List[str]:
    """Ordered tool names the kind's factory produces (the pool order)."""
    module_name, factory_name, kwargs = _FACTORY_BY_KIND[kind]
    module = __import__(module_name, fromlist=[factory_name])
    factory = getattr(module, factory_name)
    tools = _call_factory(factory, kwargs)
    return [t.name for t in tools]


def _tool_rows_for_kind(client, kind: str) -> List[Dict[str, Any]]:
    """Non-deprecated agent_tools rows for a kind, mirroring
    seed_agent_profiles._collect_tool_ids (primary agent_kind + additional)."""
    lookup = _TOOL_KIND_ALIAS.get(kind, kind)
    if lookup is None:
        return []
    primary = (
        client.table("agent_tools").select("tool_id, python_name, kind")
        .eq("agent_kind", lookup).eq("is_deprecated", False).execute().data or []
    )
    extra = (
        client.table("agent_tools").select("tool_id, python_name, kind")
        .contains("additional_agent_kinds", [lookup]).eq("is_deprecated", False)
        .execute().data or []
    )
    seen, rows = set(), []
    for r in (*primary, *extra):
        tid = r.get("tool_id")
        if tid and tid not in seen:
            seen.add(tid)
            rows.append(r)
    return rows


def _upsert_builtin_bundle(client, kind: str) -> str:
    """Return the mcp_server_id of kind's built-in bundle, creating it if absent."""
    name = builtin_mcp_name(kind)
    existing = (
        client.table("agent_mcp_servers").select("mcp_server_id")
        .eq("name", name).eq("is_builtin", True).limit(1).execute().data or []
    )
    if existing:
        return existing[0]["mcp_server_id"]
    inserted = client.table("agent_mcp_servers").insert({
        "name": name,
        "source": "bundle",
        "is_builtin": True,
        "usage_guidance": f"Built-in tools for the {kind} agent.",
        # bundle rows have no endpoint/transport (nullable post-Phase-0).
    }).execute().data or []
    return inserted[0]["mcp_server_id"]


def _reconcile_membership(client, mcp_server_id: str, members: List[Dict[str, Any]]) -> int:
    """Make agent_mcp_tools for this bundle exactly `members`. Returns count."""
    want = {m["tool_id"]: m["position"] for m in members}
    have_rows = (
        client.table("agent_mcp_tools").select("tool_id, position")
        .eq("mcp_server_id", mcp_server_id).execute().data or []
    )
    have = {r["tool_id"]: r.get("position") for r in have_rows}
    # delete rows no longer wanted
    for tid in have:
        if tid not in want:
            client.table("agent_mcp_tools").delete().eq(
                "mcp_server_id", mcp_server_id).eq("tool_id", tid).execute()
    # insert/refresh wanted rows
    for m in members:
        tid = m["tool_id"]
        if tid in have:
            if have[tid] != m["position"]:
                client.table("agent_mcp_tools").update({"position": m["position"]}).eq(
                    "mcp_server_id", mcp_server_id).eq("tool_id", tid).execute()
        else:
            client.table("agent_mcp_tools").insert({
                "mcp_server_id": mcp_server_id, "tool_id": tid, "position": m["position"],
            }).execute()
    return len(members)


def _tool_python_names(client, tool_ids: List[str]) -> List[str]:
    """python_names for a profile's tool_ids, in the id list's order."""
    if not tool_ids:
        return []
    rows = (
        client.table("agent_tools").select("tool_id, python_name")
        .in_("tool_id", tool_ids).execute().data or []
    )
    by_id = {r["tool_id"]: r.get("python_name") for r in rows}
    return [by_id[t] for t in tool_ids if by_id.get(t)]


def _attach_profiles(client, kind: str, mcp_server_id: str) -> int:
    """Attach every profile of `kind` (and of every kind that borrows its
    tools, ``seed_agent_profiles._TOOL_KIND_ALIAS``) to the bundle with
    tool_subset = the profile's current tool_python_names. Only creates an
    attachment when the profile has none (idempotent, and admin edits
    survive)."""
    from admin.seed_agent_profiles import _TOOL_KIND_ALIAS

    kinds = [kind] + [k for k, target in _TOOL_KIND_ALIAS.items() if target == kind and k != kind]
    attached = 0
    for k in kinds:
        profiles = (
            client.table("agent_profiles").select("profile_id, tool_ids")
            .eq("agent_kind", k).execute().data or []
        )
        for p in profiles:
            existing = (
                client.table("agent_profile_mcp_servers").select("mcp_server_id")
                .eq("profile_id", p["profile_id"]).execute().data or []
            )
            if existing:
                # A real (tool-providing) attachment means an admin/prior choice
                # we must not override. Endpoint-only external attachments don't
                # count — the bundle still needs to attach to supply tools.
                server_ids = [e["mcp_server_id"] for e in existing]
                servers = (
                    client.table("agent_mcp_servers")
                    .select("mcp_server_id, endpoint_only")
                    .in_("mcp_server_id", server_ids).execute().data or []
                )
                if any(not s.get("endpoint_only") for s in servers):
                    continue  # leave admin/prior tool attachments untouched
            subset = _tool_python_names(client, p.get("tool_ids") or [])
            client.table("agent_profile_mcp_servers").insert({
                "profile_id": p["profile_id"],
                "mcp_server_id": mcp_server_id,
                "tool_subset": subset,
                "position": 0,
            }).execute()
            attached += 1
    return attached


def run() -> Dict[str, int]:
    client = get_service_client()
    bundles = members = attachments = 0
    for kind in _seedable_kinds():
        try:
            pool = _pool_names_for_kind(kind)
            tool_rows = _tool_rows_for_kind(client, kind)
            if not tool_rows:
                continue
            mcp_id = _upsert_builtin_bundle(client, kind)
            bundles += 1
            members += _reconcile_membership(client, mcp_id, ordered_membership(pool, tool_rows))
            attachments += _attach_profiles(client, kind, mcp_id)
        except Exception as exc:  # never let one kind abort the boot seed
            logger.warning("seed_builtin_mcps: kind %s failed (%s)", kind, exc)
    logger.info("seed_builtin_mcps: %d bundles, %d members, %d attachments",
                bundles, members, attachments)
    return {"bundles": bundles, "members": members, "attachments": attachments}
