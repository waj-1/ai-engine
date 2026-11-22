"""Per-(profile, environment) served-version pin — the service behind the
agent config screen's version bar (Phase P1) and any future MCP tool.

`agent_profiles.served_version_id` (see migration
`20260724120000_profile_served_version.sql`) lets an operator pin the exact
`agent_profile_versions` snapshot `AgentProfileLoader` serves for a profile,
overriding the default resolution (dev/staging = live row, prod = latest
promoted). This module is the only write path for that column outside raw
SQL, so it owns the one invariant the loader's own defense-in-depth check
(`core/agent_profile.py`) also re-verifies at read time: a pinned
`version_id` must belong to the profile being pinned. Rejecting a mismatched
pin here means an operator sees the error immediately instead of silently
falling back to default resolution on the next request.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from admin.audit import record_change
from core.agent_profile import AgentProfileLoader
from core.auth import admin_service_client
from runtime.compiler import _SHALLOW_FIELDS, compile_profile_snapshot

logger = logging.getLogger(__name__)

# `_SHALLOW_FIELDS` (runtime/compiler.py) is the config subset every
# snapshot carries. `restore_version` copies that subset back onto the
# live profile row, but must never touch profile *identity* — a restore
# is always same-profile, so overwriting how the profile is named/labeled
# would be a bug, not a restore. `agent_kind`/`slug`/`display_name` are
# unambiguous identity. `description` is grouped with them on the same
# line in `_SHALLOW_FIELDS` (and is human-authored profile metadata, not
# runtime behaviour) — treated as identity here too, so restoring an old
# version never silently reverts an operator's description edit.
_IDENTITY_FIELDS = frozenset({"agent_kind", "slug", "display_name", "description"})
_CONFIG_FIELDS = tuple(f for f in _SHALLOW_FIELDS if f not in _IDENTITY_FIELDS)

# Id-reference config fields that need a per-row existence probe before
# being written back: (snapshot field, table, PK column, dropped-bucket key).
# PK columns verified against supabase/migrations/20260603120000_baseline_
# agent_schema.sql — note agent_data_sources' PK is `source_id`, not
# `data_source_id` (the latter doesn't exist as a column at all; it's the
# `agent_profiles.data_source_ids` array column name, easy to confuse with
# the referenced table's own PK).
_REF_CATEGORIES = (
    ("tool_ids", "agent_tools", "tool_id", "tools"),
    ("data_source_ids", "agent_data_sources", "source_id", "data_sources"),
    ("mcp_server_ids", "agent_mcp_servers", "mcp_server_id", "mcp_servers"),
)
_ID_REF_FIELDS = frozenset(
    {field for field, _table, _pk, _key in _REF_CATEGORIES}
    | {"model_main_id", "prompt_version_ids"}
)
# The remaining config fields copy straight from the snapshot — no
# reference to filter, no existence to probe.
_PLAIN_CONFIG_FIELDS = tuple(f for f in _CONFIG_FIELDS if f not in _ID_REF_FIELDS)


def pin_served_version(
    profile_id: str,
    environment: str,
    version_id: Optional[str],
    *,
    actor_user_id: str,
) -> Dict[str, Any]:
    """Pin (or clear) the `agent_profile_versions` snapshot served for
    `profile_id` in `environment`.

    `version_id=None` (or `""`) clears the pin — `AgentProfileLoader` falls
    back to its default resolution for the environment. Otherwise
    `version_id` must reference a row in `agent_profile_versions` whose
    `profile_id` matches `profile_id`; a version_id that belongs to a
    different profile raises `ValueError` rather than being silently
    accepted (the loader would refuse it anyway — this is the write-side
    half of that same guard, so the operator sees the rejection at pin
    time, not on the next request).

    Invalidates the `AgentProfileLoader` cache for `(agent_kind,
    environment)` on every write so the pin (or its removal) takes effect
    on the very next request, and audits the change via `record_change`.

    Raises `ValueError` if `profile_id` doesn't exist, or if `version_id`
    doesn't belong to it. Returns `{"served_version_id": version_id}`.
    """
    client = admin_service_client()

    profile_rows = (
        client.table("agent_profiles")
        .select("profile_id, agent_kind, served_version_id")
        .eq("profile_id", profile_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not profile_rows:
        raise ValueError(f"profile not found: {profile_id}")
    profile = profile_rows[0]
    agent_kind = profile["agent_kind"]
    before_served_version_id = profile.get("served_version_id")

    if not version_id:
        client.table("agent_profiles").update(
            {"served_version_id": None}
        ).eq("profile_id", profile_id).execute()
        AgentProfileLoader.invalidate(agent_kind, environment)
        record_change(
            entity_type="agent_profiles",
            entity_id=profile_id,
            action="pin_served_version",
            actor_user_id=actor_user_id,
            before={"served_version_id": before_served_version_id},
            after={"served_version_id": None},
        )
        return {"served_version_id": None}

    version_rows = (
        client.table("agent_profile_versions")
        .select("version_id, profile_id, version_number")
        .eq("version_id", version_id)
        .eq("profile_id", profile_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not version_rows:
        raise ValueError(
            f"profile version {version_id} does not belong to profile {profile_id}"
        )

    client.table("agent_profiles").update(
        {"served_version_id": version_id}
    ).eq("profile_id", profile_id).execute()
    AgentProfileLoader.invalidate(agent_kind, environment)
    record_change(
        entity_type="agent_profiles",
        entity_id=profile_id,
        action="pin_served_version",
        actor_user_id=actor_user_id,
        before={"served_version_id": before_served_version_id},
        after={"served_version_id": version_id},
    )
    return {"served_version_id": version_id}


def save_version(
    profile_id: str,
    environment: str,
    *,
    actor_user_id: str,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Snapshot the profile's current (live/draft) config into a brand new
    `agent_profile_versions` row for `environment`, without touching
    `served_version_id` — this captures a checkpoint an operator can
    later inspect, diff, or pin; it does not change what's currently
    served.

    Delegates the actual snapshot work to
    `runtime.compiler.compile_profile_snapshot` — the same writer promote
    uses — so a manually-saved version has the identical self-contained
    shape (resolved model, prompt contents, tool python_names, data
    sources, MCP/skill attachments, delegate descriptors) as a promoted
    one.

    Note: `compile_profile_snapshot` returns only the new row's bare
    `version_id` (a `str`), not `version_number` — this reads the number
    back from the inserted row so the `{version_id, version_number}`
    shape this function promises callers is still satisfied.

    Saving never changes `served_version_id`, so a *pinned* serve (an
    operator-set pin, in any environment) is unaffected either way. But
    absent a pin, `AgentProfileLoader` (`core/agent_profile.py`) resolves
    differently per environment: staging serves the live/draft row (this
    call's snapshot is purely a checkpoint there — served config is
    unchanged), while PRODUCTION serves `load_active_snapshot`, i.e. the
    HIGHEST `version_number` row for `(profile_id, environment)`
    (`runtime/compiler.py`). This call just inserted exactly that — a new
    highest version_number — so in prod a save_version silently becomes the
    served config at the next cache TTL, behaving like a promote of the
    current draft. Calls `AgentProfileLoader.invalidate(agent_kind,
    environment)` after the write so pickup is deterministic (and
    consistent with promote's own invalidate) instead of accidental; this
    is a harmless no-op in staging, where the loader ignores the cache for
    the live-row path anyway. Audits the save via `record_change` (action
    `SAVE_VERSION`).

    Raises `runtime.compiler.SnapshotCompileError` if the snapshot write
    fails (e.g. profile not found, or contention on `version_number`
    exhausts its retries).
    """
    client = admin_service_client()

    version_id = compile_profile_snapshot(
        profile_id=profile_id,
        environment=environment,
        notes=notes,
        promoted_by=actor_user_id,
        client=client,
    )

    version_rows = (
        client.table("agent_profile_versions")
        .select("version_id, version_number")
        .eq("version_id", version_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    version_number = version_rows[0].get("version_number") if version_rows else None

    result = {"version_id": version_id, "version_number": version_number}

    profile_rows = (
        client.table("agent_profiles")
        .select("agent_kind")
        .eq("profile_id", profile_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    agent_kind = profile_rows[0].get("agent_kind") if profile_rows else None
    if agent_kind:
        AgentProfileLoader.invalidate(agent_kind, environment)
    else:
        logger.warning(
            "save_version: could not resolve agent_kind for profile_id=%s; "
            "skipping AgentProfileLoader.invalidate",
            profile_id,
        )

    record_change(
        entity_type="agent_profiles",
        entity_id=profile_id,
        action="SAVE_VERSION",
        actor_user_id=actor_user_id,
        before=None,
        after={"environment": environment, **result},
    )

    return result


def list_versions(profile_id: str, environment: str) -> List[Dict[str, Any]]:
    """List `agent_profile_versions` snapshots for `profile_id` in
    `environment`, newest first, decorated with `is_served` against the
    profile's current `served_version_id` pin.

    Read-only — backs the version bar's history list. Raises `ValueError`
    if `profile_id` doesn't exist. Each returned dict is
    `{version_id, version_number, promoted_at, notes, is_served}`.
    """
    client = admin_service_client()

    profile_rows = (
        client.table("agent_profiles")
        .select("profile_id, served_version_id")
        .eq("profile_id", profile_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not profile_rows:
        raise ValueError(f"profile not found: {profile_id}")
    served_version_id = profile_rows[0].get("served_version_id")

    version_rows = (
        client.table("agent_profile_versions")
        .select("version_id, version_number, promoted_at, notes")
        .eq("profile_id", profile_id)
        .eq("environment", environment)
        .order("version_number", desc=True)
        .execute()
        .data
        or []
    )

    return [
        {
            "version_id": v["version_id"],
            "version_number": v.get("version_number"),
            "promoted_at": v.get("promoted_at"),
            "notes": v.get("notes"),
            "is_served": v["version_id"] == served_version_id,
        }
        for v in version_rows
    ]


def _surviving_ids(
    client: Any, table: str, id_column: str, ids: List[str],
) -> set:
    """Which of `ids` still have a row in `table` (existence probe).

    One `.in_()` query per category — never a query per id. Best-effort: a
    probe failure (DB blip, unexpected response shape) is treated as
    "nothing survives" rather than raised, because a restore that can't
    positively confirm a reference still exists should drop + warn on it,
    not risk keeping a possibly-dangling id.
    """
    if not ids:
        return set()
    try:
        rows = (
            client.table(table).select(id_column)
            .in_(id_column, ids)
            .execute()
            .data
            or []
        )
        return {r[id_column] for r in rows if r.get(id_column)}
    except Exception as exc:
        logger.warning(
            "restore_version: existence probe against %s failed (%s); "
            "treating all %d referenced id(s) as missing",
            table, exc, len(ids),
        )
        return set()


def _recreate_prompt_version(
    client: Any, agent_kind: Optional[str], slug: str, content: str,
) -> Optional[str]:
    """Re-create a deleted `agent_prompt_versions` row from snapshot text.

    Mirrors the insert admin/router.py's `prompt_version_create` route
    performs: resolve `(agent_kind, slug)` -> the owning `agent_prompts`
    row's `prompt_id`, compute the next `version_number` for that prompt,
    insert, return the new `prompt_version_id`.

    Best-effort like `_surviving_ids`: returns `None` (never raises) if
    there's no matching `agent_prompts` row to hang the new version off
    of, or the insert itself fails — the caller records that slug as
    dropped rather than losing the whole restore over one prompt.
    """
    try:
        prompt_rows = (
            client.table("agent_prompts")
            .select("prompt_id")
            .eq("agent_kind", agent_kind)
            .eq("slug", slug)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not prompt_rows:
            logger.warning(
                "restore_version: no agent_prompts row for (%s, %s); cannot "
                "recreate its deleted prompt version",
                agent_kind, slug,
            )
            return None
        prompt_id = prompt_rows[0]["prompt_id"]

        last_rows = (
            client.table("agent_prompt_versions")
            .select("version_number")
            .eq("prompt_id", prompt_id)
            .order("version_number", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        next_number = (last_rows[0]["version_number"] + 1) if last_rows else 1

        inserted = (
            client.table("agent_prompt_versions")
            .insert({
                "prompt_id": prompt_id,
                "version_number": next_number,
                "content": content,
                "notes": "restored by profile-version restore "
                         "(original version no longer exists)",
            })
            .execute()
        )
        new_row = inserted.data[0] if inserted.data else {}
        return new_row.get("prompt_version_id")
    except Exception as exc:
        logger.warning(
            "restore_version: failed to recreate prompt version for slug "
            "%s (%s)",
            slug, exc,
        )
        return None


def restore_version(
    profile_id: str,
    environment: str,
    version_id: str,
    *,
    actor_user_id: str,
) -> Dict[str, Any]:
    """Copy `version_id`'s snapshot config back onto the live draft row.

    This is copy-on-write, not a checkout: it overwrites the live
    `agent_profiles` row's **config** fields (see `_CONFIG_FIELDS`, derived
    from `runtime.compiler._SHALLOW_FIELDS` minus identity) from the chosen
    version's snapshot, leaving `agent_kind`/`slug`/`display_name`/
    `description` untouched — restore always targets the *same* profile,
    so identity is never something a restore should change. The caller is
    expected to `save_version` afterwards to turn "editing a copy of vN"
    into a new version.

    **Every id-reference field is filtered to what still exists in this
    env** before being written back — a version can be old enough that a
    tool, KB source, MCP server, model, or prompt version it references
    has since been deleted. Filtering never fails the restore: a missing
    reference is dropped and reported back in `dropped`, so the caller
    (portal UI / MCP tool) can surface "restored vN — dropped 2 missing
    tools, 1 missing KB source" instead of the whole operation blowing up.

    Special-cased fields:
    - `model_main_id`: a profile MUST have a model, so a missing model is
      **never** nulled out — the live row's current model is left as-is
      and `dropped["model"]` is set `True` instead.
    - `prompt_version_ids` (`{slug: prompt_version_id}`): a surviving
      version id is kept as-is. A deleted one is re-created from the
      snapshot's `prompt_contents[slug]` via `_recreate_prompt_version` so
      restore never loses prompt text outright; the slug is still listed
      in `dropped["prompts"]` because its *original* version is gone (the
      content survives under a brand new version id).

    Raises `ValueError` if `version_id` doesn't exist, if it belongs to a
    different profile (same ownership guard as `pin_served_version`), or
    if `profile_id` itself doesn't exist. Invalidates the
    `AgentProfileLoader` cache for `(agent_kind, environment)` — a restore
    changes the draft config the loader may be serving. Audited via
    `record_change` (action `RESTORE_VERSION`).

    Returns `{"restored": {...filtered config fields written...},
    "dropped": {"tools": [...], "data_sources": [...], "mcp_servers":
    [...], "model": bool, "prompts": [...]}}`.
    """
    client = admin_service_client()

    version_rows = (
        client.table("agent_profile_versions")
        .select("version_id, profile_id, environment, snapshot")
        .eq("version_id", version_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not version_rows:
        raise ValueError(f"version not found: {version_id}")
    entry = version_rows[0]
    if entry.get("profile_id") != profile_id:
        raise ValueError(
            f"profile version {version_id} does not belong to profile {profile_id}"
        )
    snapshot = entry.get("snapshot") or {}

    profile_rows = (
        client.table("agent_profiles")
        .select("profile_id, agent_kind")
        .eq("profile_id", profile_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not profile_rows:
        raise ValueError(f"profile not found: {profile_id}")
    agent_kind = profile_rows[0].get("agent_kind")

    dropped: Dict[str, Any] = {
        "tools": [], "data_sources": [], "mcp_servers": [],
        "model": False, "prompts": [],
    }
    update: Dict[str, Any] = {}

    # --- Plain id-list references: tool_ids / data_source_ids / mcp_server_ids.
    for field, table, pk, dropped_key in _REF_CATEGORIES:
        if field not in snapshot:
            continue
        ids = list(snapshot.get(field) or [])
        surviving = _surviving_ids(client, table, pk, ids)
        update[field] = [i for i in ids if i in surviving]
        dropped[dropped_key] = [i for i in ids if i not in surviving]

    # --- Model: special-cased, never nulled.
    if "model_main_id" in snapshot:
        model_id = snapshot.get("model_main_id")
        surviving_models = _surviving_ids(
            client, "agent_models", "model_id", [model_id] if model_id else []
        )
        if model_id and model_id in surviving_models:
            update["model_main_id"] = model_id
        else:
            dropped["model"] = True
            logger.warning(
                "restore_version: model %s from version %s no longer "
                "exists; keeping profile %s's current model_main_id",
                model_id, version_id, profile_id,
            )

    # --- Prompts: keep surviving version ids, re-create deleted ones.
    if "prompt_version_ids" in snapshot:
        prompt_version_ids = dict(snapshot.get("prompt_version_ids") or {})
        prompt_contents = snapshot.get("prompt_contents") or {}
        candidate_ids = [v for v in prompt_version_ids.values() if v]
        surviving_prompts = _surviving_ids(
            client, "agent_prompt_versions", "prompt_version_id", candidate_ids
        )
        restored_prompt_ids: Dict[str, str] = {}
        dropped_prompt_slugs: List[str] = []
        for slug, pvid in prompt_version_ids.items():
            if pvid and pvid in surviving_prompts:
                restored_prompt_ids[slug] = pvid
                continue
            dropped_prompt_slugs.append(slug)
            content = prompt_contents.get(slug)
            if content is None:
                logger.warning(
                    "restore_version: prompt version %s (slug %s) is "
                    "missing and the snapshot carries no content to "
                    "recreate it from; dropping the slug",
                    pvid, slug,
                )
                continue
            new_id = _recreate_prompt_version(client, agent_kind, slug, content)
            if new_id:
                restored_prompt_ids[slug] = new_id
        update["prompt_version_ids"] = restored_prompt_ids
        dropped["prompts"] = dropped_prompt_slugs

    # --- Everything else: copy straight from the snapshot, no filtering.
    for field in _PLAIN_CONFIG_FIELDS:
        if field in snapshot:
            update[field] = snapshot[field]

    if update:
        client.table("agent_profiles").update(update).eq(
            "profile_id", profile_id
        ).execute()
        AgentProfileLoader.invalidate(agent_kind, environment)

    record_change(
        entity_type="agent_profiles",
        entity_id=profile_id,
        action="RESTORE_VERSION",
        actor_user_id=actor_user_id,
        before=None,
        after={
            "environment": environment,
            "version_id": version_id,
            "restored": update,
            "dropped": dropped,
        },
    )

    return {"restored": update, "dropped": dropped}


# Scalars compute_profile_diff compares that a version snapshot never freezes
# (profile-row bookkeeping, not config — see runtime.compiler._SHALLOW_FIELDS).
# Spliced from the live row so they compare equal instead of rendering a
# phantom "changed" row on every version diff. Same set the portal's
# GET /agents/{id}/versions/{vid}/diff route splices.
_NON_SNAPSHOT_SCALAR_FIELDS = ("status", "parent_profile_id", "model_light_id")


def diff_version(profile_id: str, version_id: str,
                 against_version_id: Optional[str] = None) -> Dict[str, Any]:
    """compute_profile_diff of a saved version snapshot (`a`) against the
    profile's live/draft row — or against a second saved version when
    `against_version_id` is given (`b`). Raises ValueError when a version
    doesn't exist or belongs to another profile, or the profile is gone.

    Mirrors admin/router.py::agents_version_diff (the portal's version-diff
    view): the snapshot is shaped into an agent_profiles-row-like dict and
    the non-snapshot bookkeeping scalars are spliced from the live row."""
    from admin.profile_fork_handlers import compute_profile_diff
    from runtime.compiler import load_snapshot_by_version

    client = admin_service_client()
    live_rows = (client.table("agent_profiles").select("*")
                 .eq("profile_id", profile_id).limit(1).execute().data or [])
    if not live_rows:
        raise ValueError(f"profile {profile_id} not found")
    live = live_rows[0]

    def _as_row(vid: str) -> Dict[str, Any]:
        entry = load_snapshot_by_version(vid)
        if not entry or entry.get("profile_id") != profile_id:
            raise ValueError(f"version {vid} not found for profile {profile_id}")
        row = {**(entry.get("snapshot") or {}),
               "profile_id": entry["profile_id"],
               "environment": entry.get("environment"),
               # toolsets/skills from the frozen copies, never a live re-read
               "_snapshot": True}
        for field in _NON_SNAPSHOT_SCALAR_FIELDS:
            row[field] = live.get(field)
        return row

    profile_a = _as_row(version_id)
    profile_b = _as_row(against_version_id) if against_version_id else live
    return compute_profile_diff(client, profile_a, profile_b)


def summarize_profile_diff(diff: Dict[str, Any]) -> List[str]:
    """compute_profile_diff output -> short plain lines ("field: a -> b",
    "tools: +x, -y"); unchanged parts are omitted."""
    out: List[str] = []
    for s in diff.get("scalars") or []:
        if not s.get("equal"):
            out.append(f"{s.get('field')}: {s.get('a')!r} -> {s.get('b')!r}")
    for key in ("tools", "data_sources", "mcp_servers", "sub_agents"):
        part = diff.get(key) or {}
        added = [x.get("label") or x.get("id") for x in part.get("added_in_b") or []]
        removed = [x.get("label") or x.get("id") for x in part.get("removed_in_b") or []]
        if added or removed:
            out.append(f"{key}: " + ", ".join(
                [f"+{a}" for a in added] + [f"-{r}" for r in removed]))
    ts = diff.get("toolsets") or {}
    added_ts = [x["name"] for x in ts.get("items") or [] if x.get("state") == "added"]
    removed_ts = [x["name"] for x in ts.get("items") or [] if x.get("state") == "removed"]
    if added_ts or removed_ts:
        out.append("toolsets: " + ", ".join(
            [f"+{a}" for a in added_ts] + [f"-{r}" for r in removed_ts]))
    for x in ts.get("items") or []:
        if x.get("state") != "changed":
            continue
        acts = x.get("actions") or []
        parts = ([f"+{a['label']}" for a in acts if a.get("state") == "added"]
                 + [f"-{a['label']}" for a in acts if a.get("state") == "removed"])
        out.append(f"toolset {x['name']} actions: "
                   + (", ".join(parts) if parts else f"{x.get('summary_a')} -> {x.get('summary_b')}"))
    sk = diff.get("skills") or {}
    added = [x.get("label") or x.get("id") for x in sk.get("added_in_b") or []]
    removed = [x.get("label") or x.get("id") for x in sk.get("removed_in_b") or []]
    if added or removed:
        out.append("skills: " + ", ".join([f"+{a}" for a in added] + [f"-{r}" for r in removed]))
    return out
