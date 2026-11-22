"""Which voice-persona version a caller actually hears — for the Agents screen.

The duplex persona is the one prompt whose *served* version is not visible
anywhere an operator looks. Since 20260817121000 the persona is pinned on the
PARENT profile's ``prompt_version_ids`` and frozen into its promote snapshot,
which is the same governance the brain's system prompt has — and the same
consequence: **on a snapshot-served profile, saving a persona version in the
portal does not reach callers until the parent is promoted.** The portal showed
the new version either way, so "saved" read as "live" when it was not.
`docs/superpowers/specs/2026-08-17-voice-config-unification-design.md`
(Decision 2) calls this display mandatory for exactly that reason.

**This module MUST mirror `endpoints/voice_live.py::resolve_persona_prompt`.**
A display that disagrees with the resolver about which version is served is
worse than no display — it is the same class of defect it exists to remove
(the portal asserting a value the session will not use). The tier order is
therefore duplicated here as PURE functions over already-fetched rows, so the
mirror is unit-testable against the resolver's four branches without a DB:

  1. ``snapshot``        — the parent's promoted snapshot froze the content
                           (``Profile.resolved_prompts[slug]``, fed from
                           ``snapshot["prompt_contents"]``).
  2. ``parent_pin``      — the parent's ``prompt_version_ids[slug]``.
  3. ``specialist_pin``  — a legacy pin on the specialist. Honoured by the
                           resolver, but it warns on every connect.
  4. ``global_latest``   — nothing pins it: the highest ``version_number`` for
                           ``(agent_kind, slug)``. Ungoverned — promote and
                           rollback do not control it.

Which of ``resolved_prompts`` / ``prompt_version_ids`` a *Profile* carries is
decided by `core.agent_profile.AgentProfileLoader._build_profile`: a
``served_version_id`` pin (any env) or ``environment == "prod"`` builds from a
snapshot, everything else builds from the live row. `_profile_view` below
reproduces that branch — which is also where the dev-vs-prod semantic
difference comes from, rather than being hardcoded per environment name.

Not modelled: the precompiled prod bundle (`core.agent_profile._PROD_BUNDLE`),
an in-memory registry loaded from GCS at boot that would shadow the DB snapshot
for a kind it carries. It is empty in the current single-DB deploy (see
DEPLOY.md's 2026-07-07 pivot) and unreadable from the portal process, so the
snapshot tier is reported by profile version rather than by bundle entry.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from voice.live.config import promote_legacy_voice_config

logger = logging.getLogger(__name__)

TIER_SNAPSHOT = "snapshot"
TIER_PARENT_PIN = "parent_pin"
TIER_SPECIALIST_PIN = "specialist_pin"
TIER_GLOBAL_LATEST = "global_latest"


# --------------------------------------------------------------------------- #
# Pure half: the tier order, mirrored from resolve_persona_prompt.
# --------------------------------------------------------------------------- #
def _prompt_maps(view: Optional[Dict[str, Any]]) -> tuple:
    """``(resolved_prompts, prompt_version_ids)`` as the loader would build them.

    Mirrors `AgentProfileLoader._profile_from_snapshot` (snapshot path: contents
    AND pins both come off the snapshot, never the live row) vs the dev path
    (no resolved content at all; pins come off the row).
    """
    if not view:
        return ({}, {})
    snapshot = view.get("snapshot")
    if snapshot is not None:
        return (snapshot.get("prompt_contents") or {},
                snapshot.get("prompt_version_ids") or {})
    return ({}, (view.get("row") or {}).get("prompt_version_ids") or {})


def resolve_persona_tier(
    slug: str,
    parent: Optional[Dict[str, Any]],
    specialist: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Which tier serves `slug`, and which version id that tier names.

    `parent` / `specialist` are the ``{"row": …, "snapshot": …}`` views built by
    `_profile_view`. Returns ``{tier, version_id, from_snapshot}``;
    ``version_id`` is None only when a snapshot froze content for the slug but
    carries no id for it (a hand-built snapshot), and for `TIER_GLOBAL_LATEST`,
    whose id the caller supplies from the latest-version lookup.
    """
    p_resolved, p_pins = _prompt_maps(parent)
    if parent is not None:
        if p_resolved.get(slug):
            return {"tier": TIER_SNAPSHOT, "version_id": p_pins.get(slug),
                    "from_snapshot": True}
        if p_pins.get(slug):
            # A pin read off the SNAPSHOT is still frozen config even though its
            # content is resolved live — repointing the live row's pin does not
            # move it. `from_snapshot` is what the screen turns into
            # "promote to publish" vs "saved is live".
            return {"tier": TIER_PARENT_PIN, "version_id": p_pins.get(slug),
                    "from_snapshot": parent.get("snapshot") is not None}

    # The resolver skips this branch when the specialist IS the parent row.
    same_row = (
        parent is not None
        and specialist is not None
        and (specialist.get("row") or {}).get("profile_id")
        == (parent.get("row") or {}).get("profile_id")
    )
    if specialist is not None and not same_row:
        s_resolved, s_pins = _prompt_maps(specialist)
        if s_resolved.get(slug) or s_pins.get(slug):
            return {"tier": TIER_SPECIALIST_PIN, "version_id": s_pins.get(slug),
                    "from_snapshot": bool(s_resolved.get(slug))}

    return {"tier": TIER_GLOBAL_LATEST, "version_id": None, "from_snapshot": False}


# --------------------------------------------------------------------------- #
# DB half: build the views + look up version numbers.
# --------------------------------------------------------------------------- #
def _profile_view(
    client: Any, environment: str, row: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """``{row, snapshot, snapshot_version_number, snapshot_source}`` for `row`.

    Reproduces `AgentProfileLoader._build_profile`'s snapshot-vs-live branch:
    a ``served_version_id`` pin wins in EVERY environment (and is ignored when
    it belongs to another profile, exactly as the loader ignores it), then
    ``prod`` reads the newest snapshot for the profile+env, and everything else
    serves the live row. `snapshot=None` means "live row".
    """
    if not row:
        return None
    view: Dict[str, Any] = {
        "row": row, "snapshot": None,
        "snapshot_version_number": None, "snapshot_source": None,
    }
    served_vid = row.get("served_version_id")
    if served_vid:
        pinned = _fetch_version(client, version_id=served_vid)
        # Ownership mismatch = a dangling pin to the loader: it logs and falls
        # through to the default resolution. Mirror that, don't report it.
        if pinned and pinned.get("profile_id") == row.get("profile_id"):
            view["snapshot"] = pinned.get("snapshot") or {}
            view["snapshot_version_number"] = pinned.get("version_number")
            view["snapshot_source"] = "pin"
            return view
    if environment == "prod":
        latest = _fetch_version(
            client, profile_id=row.get("profile_id"), environment=environment,
        )
        if latest:
            view["snapshot"] = latest.get("snapshot") or {}
            view["snapshot_version_number"] = latest.get("version_number")
            view["snapshot_source"] = "active"
    return view


def _fetch_version(
    client: Any,
    *,
    version_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    environment: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """One `agent_profile_versions` row — by id, or newest for profile+env."""
    try:
        q = (client.table("agent_profile_versions")
             .select("version_id, profile_id, version_number, promoted_at, snapshot"))
        if version_id:
            q = q.eq("version_id", version_id).limit(1)
        else:
            q = (q.eq("profile_id", profile_id).eq("environment", environment)
                 .order("version_number", desc=True).limit(1))
        rows = q.execute().data or []
        return rows[0] if rows else None
    except Exception as exc:  # never 500 the Agents page over this display
        logger.warning("persona_versions: version fetch failed (%s)", exc)
        return None


def _fetch_prompt_versions(
    client: Any, agent_kind: str, slug: str, limit: int = 200,
) -> List[Dict[str, Any]]:
    """Every version of ``(agent_kind, slug)``, newest first.

    One extra round-trip over "just the latest" buys the served version's
    NUMBER — a bare uuid tells an operator nothing about whether it is behind.
    Scoping by `(agent_kind, slug)` is `PromptResolver._latest_version_id_for_slug`'s
    own scope, so "latest" here means the same row the ungoverned tier serves.
    """
    try:
        prompt_rows = (
            client.table("agent_prompts").select("prompt_id")
            .eq("agent_kind", agent_kind).eq("slug", slug).limit(1).execute().data or []
        )
        if not prompt_rows:
            return []
        return (
            client.table("agent_prompt_versions")
            .select("prompt_version_id, version_number, created_at, notes")
            .eq("prompt_id", prompt_rows[0]["prompt_id"])
            .order("version_number", desc=True).limit(limit).execute().data or []
        )
    except Exception as exc:
        logger.warning("persona_versions: prompt version fetch failed (%s)", exc)
        return []


def _version_entry(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "version_id": row.get("prompt_version_id"),
        "version_number": row.get("version_number"),
        "created_at": row.get("created_at"),
        "notes": row.get("notes"),
    }


def collect_persona_version_status(
    client: Any,
    *,
    environment: str,
    parent_row: Optional[Dict[str, Any]],
    specialist_row: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """What the Agents screen renders for the persona: served vs latest.

    `parent_row` is the profile that OWNS the voice config (it names the slug
    through ``voice_config.persona_prompt_slug``); `specialist_row` is the
    profile it serves calls through, needed only to detect the legacy
    misplaced pin. Returns None when there is no persona to talk about.

    Every field is derived, never assumed: `save_reaches_callers` comes from
    which row the serving tier read, not from the environment's name, because
    an ungoverned persona (tier 4) IS live-on-save even in prod while a
    snapshot-frozen one is not, even in staging when a version is pinned.
    """
    voice = _voice_block(parent_row)
    slug = (voice.get("persona_prompt_slug") or "").strip()
    if not slug or not parent_row:
        return None
    agent_kind = parent_row.get("agent_kind")

    parent_view = _profile_view(client, environment, parent_row)
    specialist_view = _profile_view(client, environment, specialist_row)
    tier = resolve_persona_tier(slug, parent_view, specialist_view)

    versions = _fetch_prompt_versions(client, agent_kind, slug)
    latest = _version_entry(versions[0] if versions else None)
    by_id = {v.get("prompt_version_id"): v for v in versions}

    if tier["tier"] == TIER_GLOBAL_LATEST:
        # By definition the same row `resolve_by_slug` picks — served IS latest,
        # which is exactly why this tier reads as fine and is not.
        served = latest
    else:
        served = _version_entry(by_id.get(tier["version_id"]))

    served_num = (served or {}).get("version_number")
    latest_num = (latest or {}).get("version_number")
    diverged = (
        served_num is not None and latest_num is not None and served_num != latest_num
    )

    owner_view = specialist_view if tier["tier"] == TIER_SPECIALIST_PIN else parent_view
    return {
        "slug": slug,
        "agent_kind": agent_kind,
        "environment": environment,
        "tier": tier["tier"],
        "served": served,
        "latest": latest,
        "diverged": diverged,
        # None when the served version could not be named (a snapshot with
        # frozen content but no id, or a pin whose version row is gone). The
        # template must not render "unchanged" over an unknown.
        "served_known": served is not None,
        "behind_by": (latest_num - served_num) if diverged else 0,
        # Which row the SERVING tier read — the honest answer to "is my save
        # live?", and the only place dev/staging and prod differ.
        "from_snapshot": bool(tier["from_snapshot"]),
        "save_reaches_callers": not tier["from_snapshot"],
        "snapshot_version_number": (owner_view or {}).get("snapshot_version_number"),
        "snapshot_source": (owner_view or {}).get("snapshot_source"),
        # tiers 3 and 4 resolve, but the resolver warns on every connect.
        "governed": tier["tier"] in (TIER_SNAPSHOT, TIER_PARENT_PIN),
        "warns_at_connect": tier["tier"] in (TIER_SPECIALIST_PIN, TIER_GLOBAL_LATEST),
        "parent": _profile_ref(parent_row),
        "specialist": _profile_ref(specialist_row),
        # A draft/archived row is not what `_active_profile_for` loads, so its
        # numbers describe a config no caller is on. Said out loud rather than
        # silently rendered as if it were live.
        "parent_is_active": (parent_row.get("status") or "active") == "active",
    }


def _voice_block(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The parent's FLAT voice_config, tolerating the shapes that 500'd before."""
    stored = (row or {}).get("voice_config")
    if stored is not None and not isinstance(stored, dict):
        return {}
    return promote_legacy_voice_config(stored)


def _profile_ref(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "profile_id": row.get("profile_id"),
        "slug": row.get("slug"),
        "display_name": row.get("display_name") or row.get("slug") or row.get("agent_kind"),
    }
