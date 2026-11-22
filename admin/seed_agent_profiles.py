"""Backfill agent_profiles with sensible defaults.

A migration creates one profile row per agent_kind in the dev env, but
leaves `tool_ids`, `data_source_ids`, and `prompt_version_ids` empty.
Without those arrays the AgentProfileLoader resolves to an empty tool
whitelist, so a profile is only useful once these arrays are populated.

This seeder fills those arrays from the already-synced
`agent_tools` / `agent_data_sources` / `agent_prompt_versions` rows so
the admin portal becomes the single source of truth out of the box.

**Idempotency rule:** we only overwrite arrays that are currently empty,
so admin edits made through `/admin/agents` survive subsequent restarts.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from core.auth import get_service_client
from core.deployment import active_scope, all_scope_kinds, scoped_build

logger = logging.getLogger(__name__)

# The kinds whose migration-seeded profile rows this seeder fills in. They
# are the deployment's own agents, so they come from its plugin (the
# `agent_catalog` hook); a build with no plugin fills none.
from admin.agent_kinds import catalog_dict as _catalog_dict, catalog_tuple as _catalog_tuple  # noqa: E402

_AGENT_KINDS = _catalog_tuple("seeded_kinds")

# Every agent kind owned by ANY deployment scope (core/deployment.py). A full
# build treats all of them as "not part of the catalog".
_ALL_SCOPE_KINDS = all_scope_kinds()


def visible_agent_kinds() -> Tuple[str, ...]:
    """The agent_kinds the admin portal + boot seeders should operate on.

    On a scoped build this collapses to the active client's main agent and
    its own sub-agents (title / suggestions) — they're the client's title /
    suggestion generators, so they must seed + show alongside the parent.
    Otherwise the full ``_AGENT_KINDS`` tuple.
    """
    client = active_scope()
    if client is not None:
        return (client.agent_kind, client.title_kind, client.suggestions_kind, *client.extra_kinds)
    return _AGENT_KINDS


def filter_trace_rows_to_client(rows, *, key: str = "agent_kind", client=None):
    """Restrict a list of trace/telemetry rows to a deployment scope's main
    agent; pass them through unchanged when no client applies.

    Used by the observability trace explorer + the Live Monitor analytics,
    which both read raw spans from Laminar's ClickHouse and would otherwise
    surface EVERY agent's traces. When
    we ship a client as a standalone service those screens must show only it.

    ``client`` defaults to ``core.deployment.active_scope()``; pass a
    ``DeploymentScope`` explicitly to filter regardless of env. Each ``row``
    is a per-trace dict carrying its ``agent_kind`` under ``key`` (the
    attribute read from the trace's tagged span). A row is kept iff
    ``row[key]`` is the client's main kind or one of its ``extra_kinds``; rows
    with a missing / ``None`` / other kind are dropped. This is a presentational narrow — nothing is mutated or
    deleted.

    No-op (returns the list unchanged) when there is no active/explicit
    client, so the full multi-agent dashboards are untouched with the flag off.
    """
    client = client if client is not None else active_scope()
    if client is None:
        return list(rows)
    keep = {client.agent_kind, *client.extra_kinds}
    return [r for r in rows if r.get(key) in keep]


def filter_rows_by_visible_kinds(rows):
    """Keep only catalog rows (tools / data sources / channels) whose agent
    kind is visible under the current mode.

    A row passes when ANY of these is true:
      * the flag is OFF — the full catalog is always visible (no-op), or
      * the row has no ``agent_kind`` AND no ``allowed_agent_kinds`` /
        ``additional_agent_kinds`` restriction — generic/un-kinded rows
        stay visible, or
      * the row's primary ``agent_kind`` is in ``visible_agent_kinds()``, or
      * the row's ``additional_agent_kinds`` intersects the visible set, or
      * the row's ``allowed_agent_kinds`` (channels) intersects the visible
        set, OR is empty/missing (an unrestricted channel serves any kind).

    Purely presentational: it hides rows the operator shouldn't see in
    scoped-build mode without deleting anything. Behaviour is identical (the
    full list passes through unchanged) when no client is active.
    """
    if not scoped_build():
        return list(rows)

    visible = set(visible_agent_kinds())
    out = []
    for row in rows:
        primary = row.get("agent_kind")
        additional = set(row.get("additional_agent_kinds") or [])
        # Channels gate via allowed_agent_kinds (an empty list = unrestricted).
        allowed = row.get("allowed_agent_kinds")
        has_allowed_key = "allowed_agent_kinds" in row

        if has_allowed_key:
            allowed_set = set(allowed or [])
            # Unrestricted channel (no kind list) → visible; otherwise it must
            # intersect the visible kinds.
            keep = (not allowed_set) or bool(allowed_set & visible)
        else:
            # Tool / data-source row: generic (un-kinded, no extras) stays;
            # otherwise primary or any additional kind must be visible.
            if primary is None and not additional:
                keep = True
            else:
                keep = (primary in visible) or bool(additional & visible)
        if keep:
            out.append(row)
    return out

# Tools-by-kind aliasing: a kind that shares another kind's tool registry, or
# (None) has no tools at all. Maps "what tools to look up" → "real agent_kind
# in agent_tools". The deployment's own aliases come from its plugin
# (`agent_catalog.tool_kind_aliases()`).
_TOOL_KIND_ALIAS: Dict[str, Optional[str]] = _catalog_dict("tool_kind_aliases")


def _is_local() -> bool:
    """True when running on a developer machine (not deployed staging/prod).

    The migration seeds profile rows in the ``dev`` environment, but the
    runtime collapses local ``ENVIRONMENT=development`` to the ``staging`` env
    (see ``core.agent_profile._default_environment`` — ``dev`` was retired as a
    runtime env). On deployed staging/prod the matching rows are created by the
    promote flow, but locally nothing ever creates them, so every chat 500s
    with ``MissingProfileError``. We only bridge that gap locally; deployed
    envs set ENVIRONMENT explicitly and must not be auto-seeded from ``dev``.
    """
    import os

    return os.getenv("ENVIRONMENT", "development").lower() in ("development", "dev", "")


def _clone_dev_profiles_into_runtime_env(client) -> int:
    """Locally, copy active ``dev`` profile rows into the runtime's resolved
    environment so the agent can actually serve.

    Idempotent: skips any (agent_kind, slug) that already has a row in the
    target env. FK arrays (model/prompt/tool/source ids) are env-agnostic UUIDs
    resolved by id, so the clone references the same rows the dev profile does —
    which is why a plain row copy is sufficient (verified end-to-end).
    """
    from core.agent_profile import _default_environment

    target_env = _default_environment()
    if target_env == "dev":
        # Nothing to bridge — the runtime is already reading the seeded rows.
        return 0

    dev_rows = (
        client.table("agent_profiles").select("*")
        .eq("environment", "dev").eq("is_active", True)
        .execute().data or []
    )
    # On a scoped build, only bridge the active client's profiles into the
    # runtime env so a fresh DB stays single-client (don't clone WAJ's catalog).
    if scoped_build():
        visible = set(visible_agent_kinds())
        dev_rows = [r for r in dev_rows if r.get("agent_kind") in visible]
    existing = (
        client.table("agent_profiles").select("agent_kind, slug")
        .eq("environment", target_env)
        .execute().data or []
    )
    have = {(r.get("agent_kind"), r.get("slug")) for r in existing}

    cloned = 0
    for row in dev_rows:
        key = (row.get("agent_kind"), row.get("slug"))
        if key in have:
            continue
        clone = {
            k: v for k, v in row.items()
            if k not in ("profile_id", "created_at", "updated_at")
        }
        clone["environment"] = target_env
        try:
            client.table("agent_profiles").insert(clone).execute()
            cloned += 1
        except Exception as exc:  # unique-violation race etc. — non-fatal
            logger.warning(
                "seed_agent_profiles: clone of %s/%s into %s failed: %s",
                key[0], key[1], target_env, exc,
            )
    if cloned:
        logger.info(
            "seed_agent_profiles: cloned %d dev profile(s) into '%s' for local serving",
            cloned, target_env,
        )
    return cloned


_SUBAGENT_SUFFIXES = ("_title", "_suggestions", "_field_edit")


def _link_orphan_subagents(client) -> int:
    """Attach parentless sub-agents to their main agent, per environment.

    Sub-agent kinds follow the ``<parent>_title|_suggestions|_field_edit``
    naming convention, but some seeds (a partner's title/suggestions kinds)
    landed without ``parent_profile_id`` — they rendered as top-level
    rows in the Agents rail, looking like duplicate agents. Sets the
    parent to the same-env active row of the base kind and appends the
    sub's id to the parent's ``downstream_profile_ids``. Idempotent.
    """
    rows = (
        client.table("agent_profiles")
        .select("profile_id, agent_kind, environment, status, parent_profile_id, downstream_profile_ids")
        .execute().data or []
    )
    by_env_kind: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        key = (r["environment"], r["agent_kind"])
        if key not in by_env_kind or (r.get("status") or "active") == "active":
            by_env_kind[key] = r
    linked = 0
    for r in rows:
        if r.get("parent_profile_id"):
            continue
        kind = r["agent_kind"]
        base = next((kind[: -len(sfx)] for sfx in _SUBAGENT_SUFFIXES if kind.endswith(sfx)), None)
        if not base:
            continue
        parent = by_env_kind.get((r["environment"], base))
        if not parent:
            continue
        try:
            client.table("agent_profiles").update(
                {"parent_profile_id": parent["profile_id"]}
            ).eq("profile_id", r["profile_id"]).execute()
            ds = list(parent.get("downstream_profile_ids") or [])
            if r["profile_id"] not in ds:
                ds.append(r["profile_id"])
                parent["downstream_profile_ids"] = ds
                client.table("agent_profiles").update(
                    {"downstream_profile_ids": ds}
                ).eq("profile_id", parent["profile_id"]).execute()
            linked += 1
        except Exception as exc:
            logger.warning("seed_agent_profiles: link %s failed: %s", kind, exc)
    if linked:
        logger.info("seed_agent_profiles: linked %d orphan sub-agent(s)", linked)
    return linked


def run() -> Dict[str, int]:
    client = get_service_client()
    cloned = 0
    if _is_local():
        cloned = _clone_dev_profiles_into_runtime_env(client)
    _link_orphan_subagents(client)
    patched = 0
    skipped = 0
    in_scope = scoped_build()
    for kind in visible_agent_kinds():
        # On a full build no deployment scope's kinds are part of the
        # catalog, so their profile rows are absent by design — don't emit a
        # spurious "migration seed missing" warning for them.
        # (visible_agent_kinds() still returns the full tuple when off; the
        # warning is the only thing we suppress here.)
        if kind in _ALL_SCOPE_KINDS and not in_scope:
            continue
        profiles = (
            client.table("agent_profiles").select("*")
            .eq("agent_kind", kind)
            .execute().data or []
        )
        if not profiles:
            logger.warning("seed_agent_profiles: no profile row for %s (migration seed missing?)", kind)
            continue
        for profile in profiles:
            if profile.get("status") == "template":
                # A template is complete as seeded (admin/seed_templates.py):
                # its empty arrays are its design. Filling them gave every
                # template the kind's whole tool set and knowledge sources,
                # which each agent made from it then inherited.
                skipped += 1
                continue
            if _apply_defaults(client, profile, kind):
                patched += 1
            else:
                skipped += 1
    return {"cloned": cloned, "patched": patched, "skipped": skipped}


def _apply_defaults(client, profile: Dict[str, Any], kind: str) -> bool:
    """Fill empty arrays on `profile`. Returns True iff an UPDATE happened."""
    patch: Dict[str, Any] = {}

    if not (profile.get("tool_ids") or []):
        tool_ids = _collect_tool_ids(client, kind)
        if tool_ids:
            patch["tool_ids"] = tool_ids

    if not (profile.get("data_source_ids") or []):
        source_ids = _collect_source_ids(client, kind)
        if source_ids:
            patch["data_source_ids"] = source_ids

    if not (profile.get("prompt_version_ids") or {}):
        version_map = _collect_prompt_version_ids(client, kind)
        if version_map:
            patch["prompt_version_ids"] = version_map

    if not patch:
        return False

    client.table("agent_profiles").update(patch).eq(
        "profile_id", profile["profile_id"]
    ).execute()
    return True


def _collect_tool_ids(client, kind: str) -> List[str]:
    # Honor the alias: a kind may look up another kind's tools, or have none.
    lookup_kind = _TOOL_KIND_ALIAS.get(kind, kind)
    if lookup_kind is None:
        return []
    # Primary agent_kind hit + every row where this kind is listed in
    # additional_agent_kinds (tools exposed by more than one agent, e.g.
    # one tool built by two agents' factories).
    # Mirrors _collect_source_ids so shared tools aren't dropped.
    primary = (
        client.table("agent_tools").select("tool_id")
        .eq("agent_kind", lookup_kind).eq("is_deprecated", False)
        .execute().data or []
    )
    extra = (
        client.table("agent_tools").select("tool_id")
        .contains("additional_agent_kinds", [lookup_kind]).eq("is_deprecated", False)
        .execute().data or []
    )
    seen = set()
    out: List[str] = []
    for row in (*primary, *extra):
        tid = row.get("tool_id")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _collect_source_ids(client, kind: str) -> List[str]:
    # Same aliasing rule as tools.
    lookup_kind = _TOOL_KIND_ALIAS.get(kind, kind)
    if lookup_kind is None:
        return []
    # Primary agent_kind hit + every row where this kind is listed in
    # additional_agent_kinds (data sources that serve multiple agents).
    primary = (
        client.table("agent_data_sources").select("source_id")
        .eq("agent_kind", lookup_kind).eq("is_active", True)
        .execute().data or []
    )
    extra = (
        client.table("agent_data_sources").select("source_id")
        .contains("additional_agent_kinds", [lookup_kind]).eq("is_active", True)
        .execute().data or []
    )
    seen = set()
    out: List[str] = []
    for row in (*primary, *extra):
        sid = row.get("source_id")
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _collect_prompt_version_ids(client, kind: str) -> Dict[str, str]:
    prompts = (
        client.table("agent_prompts").select("prompt_id, slug")
        .eq("agent_kind", kind).execute().data or []
    )
    out: Dict[str, str] = {}
    for p in prompts:
        latest = (
            client.table("agent_prompt_versions").select("prompt_version_id, version_number")
            .eq("prompt_id", p["prompt_id"])
            .order("version_number", desc=True).limit(1)
            .execute().data or []
        )
        if latest:
            out[p["slug"]] = latest[0]["prompt_version_id"]
    return out
