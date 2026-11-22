"""Precompiled prod config bundle — the cloud-object-storage "middle path".

Instead of resolving production agent config from the DB at request time,
we compile every active `environment=prod` profile's self-contained
snapshot into ONE immutable JSON bundle, push it to a cloud object, and have
the prod service load it into an in-memory registry
(`core.agent_profile._PROD_BUNDLE`) at boot. The prod read path then serves
config from that registry with no config-DB reads.

Safe + reversible by design:
- Until a bundle exists in object storage, `load_bundle()` returns ``None``
  and prod falls back to the DB-snapshot path — byte-identical to today.
- An empty / partial bundle simply yields registry misses, which also fall
  back to the DB-snapshot path per kind. Nothing prod serves changes until
  a populated bundle is pushed.
- Editing / promoting still happens on the control-plane; a prod promote
  regenerates + re-pushes the bundle (see `admin/router.py::env_promote`).

Staging / dev never read the bundle — they serve live, editable rows.

Object layout (inside the active backend's container/bucket):
    ai-agent/prod-config/latest.json          <- the active bundle
    ai-agent/prod-config/history/<version>.json  <- optional rollback copies

This module is BACKEND-AGNOSTIC: the paths below are relative object paths and
every read/write goes through `shared.cloud_storage`, which resolves
`CLOUD_STORAGE_BACKEND` (Azure Blob since 2026-08-16 — the GCS backend was
removed with the rest of the dead GCP infrastructure). Nothing here needs a
`gs://` URI; `push_bundle` returns whatever URI the active backend hands back
(`azure-blob://<container>/<path>`).

Bundle shape:
    {
      "schema_version": 1,
      "profiles": {
        "<agent_kind>": {"row": <agent_profiles row>, "snapshot": <snapshot>},
        ...
      }
    }
Both `row` and `snapshot` are stored per kind because the loader's
`AgentProfileLoader._profile_from_snapshot(row, snapshot, environment)`
needs both.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Relative object paths, resolved against the active backend's container.
BUNDLE_LATEST_PATH = "ai-agent/prod-config/latest.json"
BUNDLE_HISTORY_PREFIX = "ai-agent/prod-config/history"


def compile_prod_bundle(
    client: Any, *, generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile every active prod profile + its snapshot into one dict.

    Reads the active `environment=prod` rows from `client` (the
    prod-targeting Supabase client) and, for each, its latest snapshot via
    `runtime.compiler.load_active_snapshot`. An active profile with NO
    snapshot is skipped with a warning (it stays on the DB-snapshot path
    at runtime) rather than aborting the whole bundle.

    `generated_at` is an optional caller-supplied stamp; we never call
    wall-clock time here so the output stays deterministic for tests.
    """
    from runtime.compiler import load_active_snapshot

    rows = (
        client.table("agent_profiles").select("*")
        .eq("environment", "prod")
        .eq("status", "active")
        .execute()
        .data
        or []
    )

    profiles: Dict[str, Any] = {}
    for row in rows:
        profile_id = row.get("profile_id")
        agent_kind = row.get("agent_kind")
        if not profile_id or not agent_kind:
            logger.warning(
                "compile_prod_bundle: skipping prod profile with missing "
                "profile_id/agent_kind (%r)", row.get("slug"),
            )
            continue
        snapshot = load_active_snapshot(profile_id, "prod")
        if not snapshot:
            logger.warning(
                "compile_prod_bundle: no snapshot for active prod profile "
                "kind=%s profile_id=%s; skipping (bundle omits this kind, "
                "runtime keeps the DB-snapshot fallback)",
                agent_kind, profile_id,
            )
            continue
        profiles[agent_kind] = {"row": row, "snapshot": snapshot}

    bundle: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "profiles": profiles,
    }
    if generated_at:
        bundle["generated_at"] = generated_at
    return bundle


def push_bundle(bundle: Dict[str, Any], *, version: Optional[str] = None) -> str:
    """Write `bundle` to the latest object and return its backend-native URI.

    When `version` is a stable id, also writes a rollback copy at
    ``ai-agent/prod-config/history/<version>.json``. With no stable id we
    skip the history copy rather than invent a wall-clock timestamp. The
    history write is best-effort: its failure never fails the latest push.
    """
    from shared.cloud_storage import upload_json

    uri = upload_json(BUNDLE_LATEST_PATH, bundle)
    if version:
        try:
            upload_json(f"{BUNDLE_HISTORY_PREFIX}/{version}.json", bundle)
        except Exception as exc:
            logger.warning(
                "push_bundle: rollback history copy for version=%s failed "
                "(%s); latest.json was written", version, exc,
            )
    return uri


def load_bundle() -> Optional[Dict[str, Any]]:
    """Read the active bundle from object storage, or ``None`` on miss / any error.

    Never raises — the caller (prod boot loader) decides what to do with a
    missing bundle (today: warn + fall back to DB snapshots).
    """
    try:
        from shared.cloud_storage import download_json

        return download_json(BUNDLE_LATEST_PATH)
    except Exception as exc:  # defensive: download_json already swallows
        logger.warning("load_bundle: unexpected error (%s)", exc)
        return None
