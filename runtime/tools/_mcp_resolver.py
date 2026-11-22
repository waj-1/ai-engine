"""Profile MCP attachments -> the request's bound tool list.

This is THE tool-resolution path: the agent's Python factory output is a
tool *pool* and profile MCP membership decides what binds. (The resolution
flag is retired; the legacy per-profile filter/merge path it once gated has
been removed.)

Attachment shape (built by core.agent_profile._fetch_mcp_attachments):
{
  "mcp_server": {...agent_mcp_servers row...},
  "tool_subset": Optional[List[str]],   # None = all
  "position": int,
  "member_tools": [{"tool": {...agent_tools row...},
                    "context_fields": Optional[dict], "position": int}],
}
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from langchain_core.tools import BaseTool

from core.agent_profile import Profile
from runtime.tools._context import effective_context_fields
from runtime.tools._external_adapter import build_mcp_tools
from runtime.tools._http_adapter import build_http_tool

logger = logging.getLogger(__name__)


def _attach_guidance(tool, guidance) -> None:
    """Stamp usage_guidance onto the resolved tool's metadata (rendered by
    render_tool_catalog). Never overwrites an existing key.

    Mutates the tool in place. SAFE only because every agent tool factory
    rebuilds fresh tool objects per request (none memoize). If a factory
    ever caches its pool, guidance from the first profile would freeze onto
    the shared object via setdefault and leak across requests — make this
    a copy-on-write stamp before adding any such cache.
    """
    if not guidance:
        return
    md = dict(getattr(tool, "metadata", None) or {})
    md.setdefault("usage_guidance", guidance)
    tool.metadata = md


def _bundle_tools(
    attachment: Dict[str, Any],
    ctx: Dict[str, Any],
    pool_by_name: Dict[str, BaseTool],
) -> List[BaseTool]:
    server = attachment["mcp_server"]
    subset = attachment.get("tool_subset")
    allowed = set(subset) if subset else None
    out: List[BaseTool] = []
    for member in attachment.get("member_tools") or []:
        row = member["tool"]
        name = row.get("python_name")
        if not name or (allowed is not None and name not in allowed):
            continue
        kind = row.get("kind") or "python"
        if kind == "python":
            tool = pool_by_name.get(name)
            if tool is None:
                logger.warning("mcp bundle %r: python tool %r not in this agent's pool; skipping",
                               server.get("name"), name)
                continue
            _attach_guidance(tool, row.get("usage_guidance"))
            out.append(tool)
        elif kind == "http":
            cf = effective_context_fields(
                server.get("context_fields"), member.get("context_fields"), row.get("context_fields"))
            try:
                http_tool = build_http_tool(row, context_fields=cf or None, ctx=ctx)
                _attach_guidance(http_tool, row.get("usage_guidance"))
                out.append(http_tool)
            except ValueError as exc:
                logger.warning("mcp bundle %r: broken http tool %r (%s); skipping",
                               server.get("name"), name, exc)
        else:
            logger.warning("mcp bundle %r: unknown tool kind %r for %r; skipping",
                           server.get("name"), kind, name)
    return out


def bound_tool_names_from_attachments(mcp_attachments: Iterable[Dict[str, Any]]) -> set:
    """Python tool names that WOULD bind from frozen `mcp_attachments`.

    Mirrors `_bundle_tools`' subset filter without needing the live tool pool,
    so promote-time validation can predict the bound set from a snapshot alone.
    External (`source != "bundle"`) MCP servers expose tools only at runtime, so
    they can't be enumerated statically and are omitted. Keep this in lockstep
    with `_bundle_tools`.
    """
    names: set = set()
    for attachment in mcp_attachments or []:
        server = attachment.get("mcp_server") or {}
        if server.get("endpoint_only") or server.get("source") != "bundle":
            continue
        subset = attachment.get("tool_subset")
        allowed = set(subset) if subset else None
        for member in attachment.get("member_tools") or []:
            name = (member.get("tool") or {}).get("python_name")
            if not name or (allowed is not None and name not in allowed):
                continue
            names.add(name)
    return names


def resolve_mcp_tools_for_profile(
    profile: Profile, ctx: Dict[str, Any], tool_pool: Iterable[BaseTool],
) -> List[BaseTool]:
    pool = list(tool_pool)
    if profile.is_fallback:
        # No DB row: keep serving the agent's full hand-coded list (the
        # fallback semantics for an unconfigured profile).
        return pool

    pool_by_name = {t.name: t for t in pool}
    resolved: List[BaseTool] = []
    seen: set = set()
    for attachment in profile.mcp_attachments or []:
        server = attachment.get("mcp_server") or {}
        if server.get("endpoint_only"):
            # Endpoint-only external MCP (a partner server): supplies the endpoint URL
            # for external_mcp_endpoint_for_kind but contributes NO tools here —
            # the agent's curated bundle provides the real tools.
            continue
        try:
            if server.get("source") == "bundle":
                batch = _bundle_tools(attachment, ctx, pool_by_name)
            else:
                batch = build_mcp_tools(server, ctx, tool_subset=attachment.get("tool_subset"))
        except Exception as exc:
            logger.warning("mcp resolution failed for server %r (%s); skipping",
                           server.get("name"), exc)
            continue
        server_guidance = server.get("usage_guidance") if server.get("source") != "bundle" else None
        for tool in batch:
            if tool.name in seen:
                logger.warning("mcp resolution: duplicate tool name %r (server %r); first wins",
                               tool.name, server.get("name"))
                continue
            _attach_guidance(tool, server_guidance)
            seen.add(tool.name)
            resolved.append(tool)
    if not resolved:
        # A non-fallback (real DB) profile that resolves to zero tools is almost
        # always a misconfiguration — no MCP bundles attached, or every
        # attachment points at an inactive/deleted server. Post-#1245 there is
        # no legacy fallback, so the agent would run tool-less and silently
        # can't do anything. Name the profile so the misconfig is diagnosable.
        logger.warning(
            "mcp resolution: profile %r (kind=%s) resolved to ZERO tools — "
            "no active MCP attachments? the agent will have no tools",
            getattr(profile, "profile_id", "?"), getattr(profile, "agent_kind", "?"),
        )
    return resolved
