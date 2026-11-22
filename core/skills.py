"""Agent skills — data access + request-time resolution.

A skill is an operator/tenant-authored, versioned instruction bundle
(spec: docs/superpowers/specs/2026-07-22-agent-skills-design.md).
This module is the ONE owner of skill data access: the profile loader,
the compile-on-promote snapshot, and RuntimeAgent.prepare() all consume
the SKILL_ENTRY shape produced here:

{
  "skill": {...agent_skills row...},
  "version": {"version_number": int, "content": str} | None,
  "resources": [{"filename", "content_type", "content"}],
  "linked_data_sources": [{"source_id", "name"}],
  "position": int,
  "scope": "platform" | "tenant",
}

Platform entries (business_id NULL, attached via agent_profile_skills)
are frozen into prod snapshots at promote time. Tenant entries
(business_id set) are ALWAYS resolved live — they belong to the tenant
and must not be pinned by WAJ's promote cycle — behind a 60s in-process
TTL cache (house config-caching rule; invalidated on every admin write).

Failure posture mirrors _fetch_mcp_attachments: any DB failure returns
[] (logged) — skills must degrade, never crash an agent turn.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)

# Engine limits (belt-and-braces vs context blowout) — not operator config.
MAX_BODY_BYTES = 32 * 1024
MAX_RESOURCE_BYTES = 64 * 1024
MAX_ALWAYS_ON = 10

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")

_TENANT_CACHE_TTL_SEC = 60.0
# (agent_kind, business_id) -> (monotonic_ts, entries)
_tenant_cache: Dict[tuple, tuple] = {}


def validate_skill_metadata(
    slug: Optional[str], display_name: Optional[str], description: Optional[str],
) -> Optional[str]:
    """Shared write-time validation (portal + admin MCP). Returns an error
    message, or None when valid. display_name/description are injected into
    the system prompt (catalog line), so braces are banned there — they'd
    collide with downstream `.format()` passes some agents still run."""
    if not _SLUG_RE.match(slug or ""):
        return "slug must match ^[a-z][a-z0-9_-]{1,62}$"
    for label, value in (("display_name", display_name), ("description", description)):
        if not (value or "").strip():
            return f"{label} is required"
        if "{" in value or "}" in value:
            return f"{label} must not contain '{{' or '}}' (injected into the system prompt)"
    return None


def invalidate_skills_cache() -> None:
    """Drop the tenant-skill cache. Called by every admin write path."""
    _tenant_cache.clear()


def _hydrate_entries(
    client: Any, skill_rows: List[Dict[str, Any]], scope: str,
) -> List[Dict[str, Any]]:
    """Join latest version + its resources + linked data-source names onto
    each skill row, producing SKILL_ENTRY dicts (position filled by caller)."""
    if not skill_rows:
        return []
    skill_ids = [r["skill_id"] for r in skill_rows]
    version_rows = (
        client.table("agent_skill_versions")
        .select("skill_version_id, skill_id, version_number, content")
        .in_("skill_id", skill_ids)
        .order("version_number", desc=True)
        .execute().data or []
    )
    # Pick the highest version_number per skill explicitly — don't rely on the
    # query's ORDER BY for correctness (ties / NULLs / a dropped .order() clause
    # would silently pick the wrong version). The desc order above is kept as a
    # belt-and-braces nicety for real DB reads.
    latest_by_skill: Dict[str, Dict[str, Any]] = {}
    for v in version_rows:
        cur = latest_by_skill.get(v["skill_id"])
        if cur is None or v["version_number"] > cur["version_number"]:
            latest_by_skill[v["skill_id"]] = v

    version_ids = [v["skill_version_id"] for v in latest_by_skill.values()]
    resource_rows = (
        client.table("agent_skill_resources")
        .select("skill_version_id, filename, content_type, content, position")
        .in_("skill_version_id", version_ids)
        .order("position", desc=False)
        .execute().data or []
    ) if version_ids else []
    resources_by_version: Dict[str, List[Dict[str, Any]]] = {}
    for r in resource_rows:
        resources_by_version.setdefault(r["skill_version_id"], []).append({
            "filename": r.get("filename"),
            "content_type": r.get("content_type"),
            "content": r.get("content"),
        })

    ds_ids = sorted({
        str(d) for row in skill_rows for d in (row.get("linked_data_source_ids") or [])
    })
    ds_names: Dict[str, str] = {}
    if ds_ids:
        ds_rows = (
            client.table("agent_data_sources").select("source_id, name")
            .in_("source_id", ds_ids).execute().data or []
        )
        ds_names = {str(d["source_id"]): d.get("name") or "" for d in ds_rows}

    entries: List[Dict[str, Any]] = []
    for row in skill_rows:
        latest = latest_by_skill.get(row["skill_id"])
        entries.append({
            "skill": row,
            "version": (
                {"version_number": latest["version_number"], "content": latest.get("content") or ""}
                if latest else None
            ),
            "resources": resources_by_version.get(
                (latest or {}).get("skill_version_id"), []
            ),
            "linked_data_sources": [
                {"source_id": str(d), "name": ds_names.get(str(d), str(d))}
                for d in (row.get("linked_data_source_ids") or [])
            ],
            "position": 0,
            "scope": scope,
        })
    return entries


def fetch_skill_attachments(client: Any, profile_id: str) -> List[Dict[str, Any]]:
    """agent_profile_skills -> ordered platform SKILL_ENTRYs.

    Used by the live (staging) profile loader AND compile_profile_snapshot
    (which freezes the result into the prod snapshot, schema v5).
    """
    try:
        att_rows = (
            client.table("agent_profile_skills").select("*")
            .eq("profile_id", profile_id).execute().data or []
        )
        if not att_rows:
            return []
        position_by_id = {r["skill_id"]: r.get("position") or 0 for r in att_rows}
        skill_rows = (
            client.table("agent_skills").select("*")
            .in_("skill_id", list(position_by_id))
            .eq("is_active", True).eq("status", "active")
            .execute().data or []
        )
        entries = _hydrate_entries(client, skill_rows, scope="platform")
        for e in entries:
            e["position"] = position_by_id.get(e["skill"]["skill_id"], 0)
        entries.sort(key=lambda e: e["position"])
        return entries
    except Exception as exc:
        logger.warning("skills: attachment lookup failed for %s (%s)", profile_id, exc)
        return []


def resolve_tenant_skills(agent_kind: str, business_id: str) -> List[Dict[str, Any]]:
    """Live tenant-skill resolution by (agent_kind, business_id), 60s TTL.

    SECURITY: business_id MUST be the authenticated business identity the
    agent already uses for chat-persistence scoping — never a raw,
    unauthenticated request value (same bug class as the run_turn session
    IDOR, PR #1351). Callers own that guarantee; this function trusts its
    argument.
    """
    if not business_id:
        return []
    key = (agent_kind, str(business_id))
    now = time.monotonic()
    cached = _tenant_cache.get(key)
    if cached is not None and (now - cached[0]) < _TENANT_CACHE_TTL_SEC:
        return cached[1]
    try:
        client = get_service_client()
        skill_rows = (
            client.table("agent_skills").select("*")
            .eq("agent_kind", agent_kind).eq("business_id", str(business_id))
            .eq("is_active", True).eq("status", "active")
            .execute().data or []
        )
        entries = _hydrate_entries(client, skill_rows, scope="tenant")
        for i, e in enumerate(entries):
            e["position"] = 1000 + i  # tenant skills list after platform ones
    except Exception as exc:
        logger.warning("skills: tenant lookup failed for %s/%s (%s)",
                       agent_kind, business_id, exc)
        return []
    _tenant_cache[key] = (now, entries)
    return entries


def resolve_skills_for_request(profile: Any, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The skill set for one turn: platform attachments ⊕ tenant skills,
    deduped by slug (platform wins — a tenant must not shadow platform
    behaviour). Empty list when the profile has no attachments and the
    request has no business_id — the zero-cost path for skill-less agents.
    """
    platform = list(getattr(profile, "skill_attachments", None) or [])
    business_id = ctx.get("business_id")
    tenant = (
        resolve_tenant_skills(getattr(profile, "agent_kind", ""), str(business_id))
        if business_id else []
    )
    seen = {
        (e.get("skill") or {}).get("slug") for e in platform
        if (e.get("skill") or {}).get("slug")
    }
    merged = platform[:]
    for e in tenant:
        slug = (e.get("skill") or {}).get("slug")
        if not slug:
            continue
        if slug in seen:
            logger.warning("skills: tenant skill %r shadowed by platform skill; skipping", slug)
            continue
        seen.add(slug)
        merged.append(e)
    return merged
