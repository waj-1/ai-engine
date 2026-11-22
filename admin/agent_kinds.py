"""One live list of agents (agent kinds) for every Build-area picker.

The Tools, Channels, Knowledge and Skills screens each used to carry their
own hard-coded tuple of agent kinds, so an operator could not create or
filter a knowledge base, channel or tool for an agent added later, even
though rows scoped to it already existed.

This module is the single source:

* ``BASE_AGENT_KINDS`` — the deployment's curated kinds, always offered (in
  this order) even when the selected env has no profile for one yet. They
  come from the plugins' ``agent_catalog`` hook; the engine names none;
* plus every kind that has a live (non-template) ``agent_profiles`` row,
  appended in alphabetical order;
* minus *system* agents (conversation-title / follow-up-suggestion
  generators, and any kind a plugin marks as a utility) unless
  ``include_system=True`` — partners do not know those are agents.

Labels come from the agent's default profile ``display_name`` (``Lina —
Customer support``) so the UI can say which agent a kind is; the kind
itself stays the stored value. Any DB failure degrades to the base list —
a picker must never block a screen.

Scoped builds keep their existing, client-scoped behaviour in the callers
(a tenant never sees the full catalog); this module only replaces the
full-engine branch.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def catalog_tuple(fn: str) -> Tuple[str, ...]:
    """The plugins' ``agent_catalog.<fn>()`` tuples, joined in plugin order
    without repeats. Empty in a build with no plugin."""
    from core.plugins import call_hook, load_hook

    out: List[str] = []
    for found in call_hook(load_hook("agent_catalog"), fn):
        out.extend(k for k in found if k not in out)
    return tuple(out)


def catalog_dict(fn: str) -> Dict[str, Any]:
    """The plugins' ``agent_catalog.<fn>()`` dicts, merged (an earlier
    plugin wins). Empty in a build with no plugin."""
    from core.plugins import call_hook, load_hook

    out: Dict[str, Any] = {}
    for found in call_hook(load_hook("agent_catalog"), fn):
        for k, v in found.items():
            out.setdefault(k, v)
    return out


# The deployment's curated kinds, in the order its pickers show them.
BASE_AGENT_KINDS: Tuple[str, ...] = catalog_tuple("catalog_kinds")

# Plain names for kinds whose profile may not exist in the selected env.
_FALLBACK_LABELS: Dict[str, str] = catalog_dict("kind_labels")

_SYSTEM_SUFFIXES = ("_title", "_suggestions")
_SYSTEM_KINDS = frozenset(catalog_tuple("system_kinds"))

_CACHE_TTL_S = 60.0
_cache: Dict[str, object] = {"at": 0.0, "rows": None}


def is_system_kind(kind: str) -> bool:
    """Utility agents (titles, follow-up suggestions, plugin-marked ones)."""
    return kind in _SYSTEM_KINDS or kind.endswith(_SYSTEM_SUFFIXES)


def _clean_label(name: str) -> str:
    # "Salma — Outbound collection (AI agents demo)" -> "Salma — Outbound collection"
    return re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip()) or name


def _humanize(kind: str) -> str:
    return kind.replace("_", " ").strip().capitalize() or kind


def _fetch_profiles() -> List[dict]:
    """Live agent_profiles rows (kind + name), cached briefly per process."""
    now = time.monotonic()
    rows = _cache.get("rows")
    if rows is not None and now - float(_cache.get("at") or 0) < _CACHE_TTL_S:
        return rows  # type: ignore[return-value]
    try:
        from core.auth import admin_service_client

        rows = (
            admin_service_client().table("agent_profiles")
            .select("agent_kind, slug, display_name, status, parent_profile_id, is_active")
            .execute().data or []
        )
    except Exception as exc:  # noqa: BLE001 - never block a picker
        logger.warning("agent_kinds: profile read failed (%s)", exc)
        rows = []
    _cache.update(at=now, rows=rows)
    return rows


def clear_cache() -> None:
    _cache.update(at=0.0, rows=None)


def _live_index() -> Dict[str, str]:
    """kind -> best display name among its live (non-template) profiles."""
    best: Dict[str, Tuple[int, str]] = {}
    for r in _fetch_profiles():
        kind = (r.get("agent_kind") or "").strip()
        if not kind or (r.get("status") == "template"):
            continue
        name = r.get("display_name") or ""
        # Prefer the kind's default profile, then any top-level profile.
        rank = 0 if r.get("slug") == f"{kind}_default" else (1 if not r.get("parent_profile_id") else 2)
        if kind not in best or rank < best[kind][0]:
            best[kind] = (rank, name)
    return {k: _clean_label(v[1]) for k, v in best.items()}


def _seeded_kinds() -> Tuple[str, ...]:
    try:
        from admin.seed_agent_profiles import _AGENT_KINDS
        return tuple(_AGENT_KINDS)
    except Exception:  # noqa: BLE001 - never block a picker
        return ()


def live_agent_kinds(*, include_system: bool = False) -> Tuple[str, ...]:
    """Base kinds first, then every other kind with a live profile."""
    live = _live_index()
    # Seeded kinds are defined in code, so a picker never depends on the
    # profile read succeeding (CI, cold start, AI project briefly down).
    extra = sorted((set(live) | set(_seeded_kinds())) - set(BASE_AGENT_KINDS))
    kinds = list(BASE_AGENT_KINDS) + extra
    if not include_system:
        kinds = [k for k in kinds if not is_system_kind(k)]
    return tuple(kinds)


def agent_label(kind: Optional[str]) -> str:
    """Plain name for an agent kind ("Lina — Customer support")."""
    if not kind:
        return ""
    live = _live_index()
    return live.get(kind) or _FALLBACK_LABELS.get(kind) or _humanize(kind)


def agent_options(kinds) -> List[Dict[str, str]]:
    """``[{value, label}]`` for a picker over ``kinds``."""
    return [{"value": k, "label": agent_label(k)} for k in kinds]


# ---------------------------------------------------------------------------
# Build scope — what THIS deployment's portal may show
# ---------------------------------------------------------------------------
# A partner build (a deployment scope, core/deployment.py) serves only its
# agents, but its AI database can still hold rows for every other agent (a
# shared local stack, a box seeded before the split, the WAJ seeders' rows).
# Every list and picker filters through the helpers below, so "which agents
# does this build serve" is answered in exactly one place. On the full engine
# (no scope) they are all no-ops.


def build_kinds() -> Optional[frozenset]:
    """Agent kinds this build serves (client main + extras + title +
    suggestions), or None on the full engine — None means "no scoping"."""
    from core.deployment import active_scope, scope_kinds

    client = active_scope()
    return scope_kinds(client) if client is not None else None


def in_build(kind: Optional[str]) -> bool:
    """True when ``kind`` belongs to this build (always True on the full engine)."""
    kinds = build_kinds()
    return kinds is None or (kind or "") in kinds


def row_in_build(row: Dict, *, key: str = "agent_kind",
                 also: Tuple[str, ...] = ("additional_agent_kinds",),
                 keep_unkinded: bool = False) -> bool:
    """True when a catalog row (tool, suite, profile, KB source, scenario…)
    belongs to this build: its ``key`` kind, or any kind in its ``also``
    list columns, is served here. A row with no kind at all is kept only when
    ``keep_unkinded`` — un-kinded rows are usually shared WAJ catalog."""
    kinds = build_kinds()
    if kinds is None:
        return True
    primary = row.get(key)
    extra = set()
    for col in also:
        extra.update(row.get(col) or [])
    if not primary and not extra:
        return keep_unkinded
    return primary in kinds or bool(extra & kinds)


def scope_to_build(rows, **kwargs) -> List[Dict]:
    """``rows`` narrowed to this build (``row_in_build`` per row)."""
    if build_kinds() is None:
        return list(rows or [])
    return [r for r in (rows or []) if row_in_build(r, **kwargs)]


def build_agent_kinds(*, include_system: bool = False) -> Tuple[str, ...]:
    """The agent kinds a Build-area picker offers on this deployment.

    Partner build: the client's own agents (main first, then extras) — never
    its reserved title/suggestions kinds unless ``include_system``, since the
    agents demo has no profile for them. Full engine: ``live_agent_kinds``.
    """
    from core.deployment import active_scope

    client = active_scope()
    if client is None:
        return live_agent_kinds(include_system=include_system)
    kinds = (client.agent_kind, *client.extra_kinds)
    if include_system:
        kinds = kinds + (client.title_kind, client.suggestions_kind)
    return kinds


def channel_in_build(row: Dict, *, profile_kinds: Optional[Dict[str, str]] = None) -> bool:
    """True when a channel (``agent_channels`` row) belongs to this build.

    A channel restricted to agents (``allowed_agent_kinds``) must name one of
    ours; one pinned to a default profile must pin one of ours
    (``profile_kinds`` maps profile_id → agent_kind when the caller has it);
    one named after ANOTHER partner (slug ``tenant``, ``tenant_…``) is that
    partner's. The full build's own channel kinds (core/channel_kinds.py)
    are never a partner's.
    """
    kinds = build_kinds()
    if kinds is None:
        return True
    from core.deployment import all_scopes, active_scope

    if row.get("kind") in _WAJ_ONLY_CHANNEL_KINDS:
        return False
    allowed = set(row.get("allowed_agent_kinds") or [])
    if allowed and not (allowed & kinds):
        return False
    pid = row.get("default_profile_id")
    if pid and profile_kinds is not None and pid in profile_kinds \
            and profile_kinds[pid] not in kinds:
        return False
    mine = active_scope().key
    slug = (row.get("slug") or "").lower()
    for other in all_scopes():
        if other != mine and (slug == other or slug.startswith(other + "_")
                              or slug.startswith(other + "-")):
            return False
    return True


# Channel kinds that are a full build's own surfaces, never a partner's
# (the plugins' `full_build_channel_kinds`, core/channel_kinds.py).
from core.channel_kinds import full_build_channel_kinds as _full_build_channel_kinds  # noqa: E402

_WAJ_ONLY_CHANNEL_KINDS: Tuple[str, ...] = _full_build_channel_kinds()


def mcp_server_in_build(server: Dict, *, attached_kinds: Dict[str, set]) -> bool:
    """True when an MCP server row belongs to this build.

    A built-in bundle is named ``__builtin__:<agent_kind>`` — it is ours when
    that kind is. Any other server (an operator bundle, an external
    connection) is ours when a profile of one of our agents uses it;
    ``attached_kinds`` maps mcp_server_id → the agent kinds of the profiles
    that attach it (junction table AND the legacy ``mcp_server_ids`` column).
    A server nobody uses yet is shown too — an operator's freshly created
    bundle must not vanish before it is attached.
    """
    kinds = build_kinds()
    if kinds is None:
        return True
    name = server.get("name") or ""
    if name.startswith("__builtin__:"):
        return name[len("__builtin__:"):] in kinds
    used = attached_kinds.get(server.get("mcp_server_id")) or set()
    if not used:
        return not server.get("is_builtin")
    return bool(used & kinds)
