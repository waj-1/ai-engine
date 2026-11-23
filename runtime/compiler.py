"""Compile-on-promote: snapshot a profile into agent_profile_versions.

When an admin promotes a profile from dev → staging or staging → prod,
we snapshot the source row's **fully resolved** configuration into
`agent_profile_versions`. The runtime in the target environment reads
the snapshot in non-dev environments instead of joining live tables, so
a developer editing dev (or even the target DB's `agent_models` /
`agent_tools` / `agent_prompt_versions` rows) post-promotion can't
drift staging/prod.

**Snapshot is self-contained.** It carries the resolved `model_main`
row, prompt *contents* (not just version ids), tool
`python_name`s, and the data source / MCP server rows — everything the
loader needs to instantiate the agent without touching any other
table. This is what makes true cross-environment promotion possible:
target Supabase projects no longer need duplicate `agent_models` /
`agent_tools` rows for the runtime to work.

Public API:
- `compile_profile_snapshot(profile_id, environment, ..., source_client,
   target_client)` resolves the profile + dependencies from
   `source_client`, then writes the snapshot into `target_client`.
   Both default to the local service client when omitted, preserving
   single-DB behaviour for callers that don't opt in.
- `load_active_snapshot(profile_id, environment)` returns the latest
  snapshot for that profile+env from the runtime's local DB.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.auth import get_service_client
from voice.live.config import promote_legacy_voice_config

logger = logging.getLogger(__name__)


class SnapshotCompileError(RuntimeError):
    """Raised when compile_profile_snapshot cannot insert a version row."""


# Shallow fields copied verbatim from the live profile row. Kept for
# audit / display purposes — the loader uses the resolved fields below
# for actual runtime behaviour.
# `agent_type_id` (the user-defined category, admin/services/agent_types.py)
# is deliberately NOT here. It is organisational metadata the runtime never
# reads: freezing it would make a category rename or a move between
# categories wait for a re-promote, and restoring an older version would
# move the agent back to a category that may no longer exist.
_SHALLOW_FIELDS = (
    "agent_kind", "slug", "display_name", "description",
    "model_main_id",
    "prompt_version_ids",
    "tool_ids", "data_source_ids", "mcp_server_ids",
    "downstream_profile_ids", "endpoint_path",
    "escalation_policy",
    "voice_config",
    "memory_config",
    "delegatable",
)

_MAX_VERSION_RETRIES = 5
_SNAPSHOT_SCHEMA_VERSION = 6  # v6: freeze downstream_delegates + delegatable (delegation)


def compile_profile_snapshot(
    *,
    profile_id: str,
    environment: str,
    promoted_by: Optional[str] = None,
    notes: Optional[str] = None,
    source_client: Optional[Any] = None,
    target_client: Optional[Any] = None,
    # Backwards-compat alias for the previous single-client parameter.
    client: Optional[Any] = None,
    # When source and target are different Supabase projects (a true
    # cross-env promote), the source profile is read by this id while the
    # written version row is keyed by `profile_id` (the freshly-inserted
    # target row). Defaults to `profile_id` for same-DB promotes.
    source_profile_id: Optional[str] = None,
) -> str:
    """Resolve + write a fully-denormalized snapshot for `profile_id`.

    - `source_client`: where to read the profile + its dependencies
      (model rows, prompt contents, tool python_names, etc.). Defaults
      to the local service client (typically dev).
    - `target_client`: where to write `agent_profile_versions`. Defaults
      to `source_client`. Set this to a remote-env client to write
      across Supabase projects.
    - `client`: legacy single-client alias used by older tests.

    Returns the new `version_id`. Raises `SnapshotCompileError` on
    missing profile, retry exhaustion, or any other failure.
    """
    source = source_client or client or get_service_client()
    target = target_client or source
    read_id = source_profile_id or profile_id

    rows = (
        source.table("agent_profiles").select("*")
        .eq("profile_id", read_id).limit(1).execute().data or []
    )
    if not rows:
        raise SnapshotCompileError(f"profile {read_id} not found")
    profile_row = rows[0]

    snapshot: Dict[str, Any] = {
        "_schema_version": _SNAPSHOT_SCHEMA_VERSION,
        **{k: profile_row.get(k) for k in _SHALLOW_FIELDS},
    }

    # Resolve and embed the heavy fields. Each is best-effort: a missing
    # dependency logs a warning but doesn't fail the promote, because
    # falling back to None / [] for that field is preferable to blocking
    # the whole promote — the admin can fix the dependency and re-promote.
    snapshot["model_main"] = _fetch_model(source, profile_row.get("model_main_id"))
    # The realtime (ears-and-mouth) model, frozen for the same reason
    # `model_main` is: the profile's voice_config.model_id was already part of
    # the snapshot, but the ROW it names was read live at call time, so the
    # provider endpoint, voice_type, turn_detection and credential-var name
    # escaped promote and rollback. Best-effort: a profile with no duplex voice
    # has no model_id and simply gets None.
    # Read through `promote_legacy_voice_config` rather than off the raw column:
    # `voice_config` is flat since 20260817120000, but a profile on a stack that
    # has not run that migration still stores `{"live": {...}}`, and a flat-only
    # `.get("model_id")` returns None there — freezing `voice_model = None` into
    # the snapshot. That does not break the call (`hydrate_live_model` falls back
    # to a live `fetch_model` of the id resolved by the read shim), which is
    # exactly why it would go unnoticed: `endpoint`/`voice_type`/`turn_detection`
    # would silently STOP being promote-governed for that version, the
    # invisible-config failure this whole change exists to remove. The stored
    # `voice_config` field itself is deliberately left verbatim — the loader's
    # read shim resolves a nested block — so this only affects which row is
    # frozen.
    snapshot["voice_model"] = _fetch_model(
        source, promote_legacy_voice_config(profile_row.get("voice_config")).get("model_id")
    )
    snapshot["tool_python_names"] = _fetch_tool_python_names(
        source, profile_row.get("tool_ids") or []
    )
    snapshot["prompt_contents"] = _fetch_prompt_contents(
        source, profile_row.get("prompt_version_ids") or {}
    )
    snapshot["data_sources"] = _fetch_rows(
        # agent_data_sources' PK is `source_id` (not `data_source_id`); probing
        # the wrong column silently returned [] and dropped every data source
        # from the snapshot's denormalized rows.
        source, "agent_data_sources", "source_id",
        profile_row.get("data_source_ids") or [],
    )
    snapshot["mcp_servers"] = _fetch_rows(
        source, "agent_mcp_servers", "mcp_server_id",
        profile_row.get("mcp_server_ids") or [],
    )

    # Phase 1a: freeze the profile's MCP attachments (bundle membership +
    # attached servers, denormalized) so the prod loader re-hydrates them
    # without a live join. Same shape core.agent_profile._fetch_mcp_attachments
    # returns and _profile_from_snapshot consumes. Best-effort: [] on failure.
    try:
        from core.agent_profile import _fetch_mcp_attachments
        snapshot["mcp_attachments"] = _fetch_mcp_attachments(source, read_id)
    except Exception as exc:  # never block a promote on this
        logger.warning("compile_profile_snapshot: mcp_attachments freeze failed (%s)", exc)
        snapshot["mcp_attachments"] = []

    # v5: freeze the profile's platform-skill attachments (skills + latest
    # version bodies + resources, denormalized) so the prod loader re-hydrates
    # them without a live join. Same shape core.skills.fetch_skill_attachments
    # returns and _profile_from_snapshot consumes. Best-effort: [] on failure.
    try:
        from core.skills import fetch_skill_attachments
        snapshot["skill_attachments"] = fetch_skill_attachments(source, read_id)
    except Exception as exc:  # never block a promote on this
        logger.warning("compile_profile_snapshot: skill_attachments freeze failed (%s)", exc)
        snapshot["skill_attachments"] = []

    # v6: freeze the delegate descriptors so prod builds ask_<slug> tools
    # without a live join. Same shape _fetch_downstream_delegates returns and
    # _profile_from_snapshot consumes. Best-effort: [] on failure.
    try:
        from core.agent_profile import _fetch_downstream_delegates
        snapshot["downstream_delegates"] = _fetch_downstream_delegates(
            source, profile_row.get("downstream_profile_ids"))
    except Exception as exc:  # never block a promote on this
        logger.warning("compile_profile_snapshot: downstream_delegates freeze failed (%s)", exc)
        snapshot["downstream_delegates"] = []

    return _insert_version(
        target=target,
        profile_id=profile_id,
        environment=environment,
        snapshot=snapshot,
        promoted_by=promoted_by,
        notes=notes,
    )


def _insert_version(
    *,
    target: Any,
    profile_id: str,
    environment: str,
    snapshot: Dict[str, Any],
    promoted_by: Optional[str],
    notes: Optional[str],
) -> str:
    """Contention-safe insert into agent_profile_versions on the target client."""
    last_error: Optional[Exception] = None
    for attempt in range(_MAX_VERSION_RETRIES):
        existing = (
            target.table("agent_profile_versions").select("version_number")
            .eq("profile_id", profile_id).eq("environment", environment)
            .order("version_number", desc=True).limit(1)
            .execute().data or []
        )
        next_num = (existing[0]["version_number"] + 1) if existing else 1
        try:
            inserted = target.table("agent_profile_versions").insert({
                "profile_id": profile_id,
                "environment": environment,
                "version_number": next_num,
                "snapshot": snapshot,
                "promoted_by": promoted_by,
                "notes": notes,
            }).execute()
        except Exception as exc:
            if _is_unique_violation(exc):
                last_error = exc
                logger.info(
                    "compile_profile_snapshot: version_number=%s collided "
                    "(attempt %s/%s); re-reading max and retrying",
                    next_num, attempt + 1, _MAX_VERSION_RETRIES,
                )
                continue
            raise SnapshotCompileError(
                f"insert into agent_profile_versions failed: {exc}"
            ) from exc
        if not inserted.data:
            raise SnapshotCompileError(
                "insert into agent_profile_versions returned no rows"
            )
        return inserted.data[0]["version_id"]

    raise SnapshotCompileError(
        f"contention exceeded {_MAX_VERSION_RETRIES} retries "
        f"writing snapshot for profile {profile_id}/{environment}: "
        f"{last_error}"
    )


def _fetch_model(client: Any, model_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not model_id:
        return None
    try:
        rows = (
            client.table("agent_models").select("*")
            .eq("model_id", model_id).limit(1).execute().data or []
        )
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning("compile: model %s fetch failed (%s)", model_id, exc)
        return None


def _fetch_tool_python_names(client: Any, tool_ids: List[str]) -> List[str]:
    if not tool_ids:
        return []
    try:
        rows = (
            client.table("agent_tools").select("python_name")
            .in_("tool_id", tool_ids).execute().data or []
        )
        return [r["python_name"] for r in rows if r.get("python_name")]
    except Exception as exc:
        logger.warning("compile: tool python_names fetch failed (%s)", exc)
        return []


def _fetch_prompt_contents(client: Any, version_ids: Dict[str, str]) -> Dict[str, str]:
    """Resolve {role: prompt_version_id} to {role: rendered content}.

    The loader uses these in non-dev so PromptResolver never has to hit
    the target DB's `agent_prompt_versions` table — which is exactly
    what allows cross-env promotion to write to a target Supabase that
    doesn't have those prompt rows replicated.
    """
    if not version_ids:
        return {}
    out: Dict[str, str] = {}
    ids = [v for v in version_ids.values() if v]
    if not ids:
        return {}
    try:
        rows = (
            client.table("agent_prompt_versions")
            .select("prompt_version_id, content")
            .in_("prompt_version_id", ids).execute().data or []
        )
        by_id = {r["prompt_version_id"]: r.get("content", "") for r in rows}
        for role, vid in version_ids.items():
            if vid and vid in by_id:
                out[role] = by_id[vid]
    except Exception as exc:
        logger.warning("compile: prompt contents fetch failed (%s)", exc)
    return out


def _fetch_rows(
    client: Any, table: str, id_column: str, ids: List[str],
) -> List[Dict[str, Any]]:
    if not ids:
        return []
    try:
        return (
            client.table(table).select("*")
            .in_(id_column, ids).execute().data or []
        )
    except Exception as exc:
        logger.warning("compile: %s fetch failed (%s)", table, exc)
        return []


def _is_unique_violation(exc: Exception) -> bool:
    """Detect a Postgres unique-violation surfaced via supabase-py."""
    code = getattr(exc, "code", None) or getattr(exc, "pgcode", None)
    if code == "23505":
        return True
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text or "unique constraint" in text


def load_active_snapshot(profile_id: str, environment: str) -> Optional[Dict[str, Any]]:
    """Fetch the latest snapshot for a profile+env, or None if none exist.

    Reads from the runtime's local DB (never cross-env): each environment
    serves its own pinned config. Does NOT raise on DB errors — a
    Supabase blip at agent boot should not crash every running
    container. The loader at `agent_profile.py` decides whether falling
    back is acceptable (dev: yes; non-dev: no).
    """
    try:
        client = get_service_client()
        rows = (
            client.table("agent_profile_versions").select("snapshot, version_number, promoted_at")
            .eq("profile_id", profile_id).eq("environment", environment)
            .order("version_number", desc=True).limit(1)
            .execute().data or []
        )
        if not rows:
            return None
        return rows[0]["snapshot"]
    except Exception as exc:
        logger.warning("load_active_snapshot: failed (%s)", exc)
        return None


def load_snapshot_by_version(version_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a specific `agent_profile_versions` row by its `version_id`.

    Unlike `load_active_snapshot` (latest per profile+env), this pins an
    exact version — used by eval/bake-off requests that want to run a
    specific historical snapshot rather than whatever is currently active.
    Returns `{profile_id, environment, snapshot}` or `None` if the version
    doesn't exist.
    """
    rows = (
        get_service_client().table("agent_profile_versions")
        .select("version_id, profile_id, environment, snapshot")
        .eq("version_id", version_id).limit(1)
        .execute().data or []
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "profile_id": r["profile_id"],
        "environment": r["environment"],
        "snapshot": r.get("snapshot") or {},
    }
