"""Populate agent_env_vars for the dev environment from local `.env`.

Idempotent on (environment, name). Runs once at boot via the FastAPI
lifespan alongside the other seeders. After running, every key/value
pair from `ai_agent/.env` is visible at /admin/env?tab=variables under the dev
column — admins can edit them and the next service restart picks up
the new values via core.env_resolver.

Sensitive-looking keys (containing api_key / secret / token / password
case-insensitively) are flagged `is_secret=true` so the UI masks them.
The plaintext value is still stored in the row; rotating to GCP Secret
Manager + setting `source='secret_manager:...'` is a follow-up.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from core.auth import get_service_client

logger = logging.getLogger(__name__)

# Heuristic — any key containing one of these tokens is masked in the UI.
_SECRET_PATTERNS = (
    "key", "secret", "token", "password", "credential", "private",
)

# Keys to NEVER upload — internal-only or already managed elsewhere.
_SKIP = {
    # Pulled from the dotenv file but irrelevant to the agent runtime.
    "PYTHONPATH",
}


def _looks_secret(name: str) -> bool:
    n = name.lower()
    return any(token in n for token in _SECRET_PATTERNS)


def _parse_dotenv(path: Path) -> Dict[str, str]:
    """Minimal .env parser — handles `KEY=value`, ignores comments + blanks.

    Strips matching single/double quotes off values. Doesn't expand $vars
    (matches python-dotenv's `interpolate=False` mode well enough for the
    seed use case).
    """
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


# Vars mirrored from the deployed service's process env (the names bound on the
# ACA apps). Secrets are mirrored as a "(set)" placeholder — never
# their value — so /admin/env?tab=variables shows the effective config of
# each environment without copying credentials into the DB.
_DEPLOYED_VARS = (
    "ENVIRONMENT", "SUPABASE_URL", "AI_SUPABASE_URL",
    "ALLOWED_ORIGINS", "LOG_LEVEL", "RATE_LIMIT_REQUESTS",
    # VERTEX_PROJECT_ID dropped 2026-08-16 — the Vertex providers that read it
    # were deleted, so mirroring it only showed operators a variable that no
    # longer affects anything.
    "CLOUD_STORAGE_BACKEND", "AZURE_STORAGE_ACCOUNT_URL", "AZURE_STORAGE_CONTAINER",
    "GCS_CSV_BUCKET", "CSV_EXPIRATION_HOURS", "GCS_EXPORT_ENABLED", "EMBED_PROVIDER",
    # LMNR_DASHBOARD_URL stays even though core/lmnr_client.py is gone: it is
    # still read at admin/router.py's lmnr_diagnose to build the "sign in to
    # Laminar" link, so dropping it here would hide a LIVE variable from the
    # portal's env screen — the same invisible-config failure this mirror exists
    # to prevent. The CLICKHOUSE trio is what the observability read path uses now.
    "LMNR_BASE_URL", "LMNR_DASHBOARD_URL", "LMNR_CLICKHOUSE_URL",
    "LMNR_CLICKHOUSE_USER", "LMNR_CLICKHOUSE_DB",
    # The LANGCHAIN_* quartet (TRACING_V2/PROJECT/ENDPOINT/API_KEY) was dropped
    # 2026-08-18 along with the bindings on the deployed apps. Laminar replaced
    # LangSmith in code on 2026-05-19, but the deployed services kept their env
    # vars, and langchain-core turns the LangSmith tracer on from THOSE ALONE —
    # no repo code required — so prod kept shipping every LLM call to LangSmith
    # EU for three months, invisibly, until the quota ran out and the 429s
    # showed up in the logs. Do not re-add these names to a deployed app: unlike
    # the vars above, setting them does not describe behaviour, it CREATES it.
    # secrets — mirrored as "(set)"/absent, never the value:
    "SUPABASE_SERVICE_KEY", "AI_SUPABASE_SERVICE_KEY", "GOOGLE_API_KEY",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "LMNR_PROJECT_API_KEY", "LMNR_CLICKHOUSE_PASSWORD",
)


def _deployed_var_names() -> tuple:
    """_DEPLOYED_VARS plus the names a product plugin mirrors for its own
    systems (its ``integrations`` hook, ``deployed_env_vars()``)."""
    from core.plugins import call_hook, load_hook

    names = list(_DEPLOYED_VARS)
    for extra in call_hook(load_hook("integrations"), "deployed_env_vars"):
        names.extend(n for n in (extra or ()) if n not in names)
    return tuple(names)


def _deployed_env_pairs() -> tuple[str, Dict[str, str]]:
    """(environment, pairs) mirrored from os.environ on a deployed service.

    Returns ("", {}) outside staging/production so local dev keeps the
    .env-file path and the control-plane (ENVIRONMENT=dev) doesn't
    double-write another service's column.
    """
    raw = (os.getenv("ENVIRONMENT") or "").strip().lower()
    env = {"staging": "staging", "production": "prod", "prod": "prod"}.get(raw, "")
    if not env:
        return "", {}
    pairs: Dict[str, str] = {}
    for name in _deployed_var_names():
        value = os.getenv(name)
        if value is None or value == "":
            continue
        pairs[name] = "(set)" if _looks_secret(name) else value
    return env, pairs


def run() -> Dict[str, int]:
    """Upsert the environment's effective vars into agent_env_vars.

    Locally: every pair from `ai_agent/.env` (under the staging column).
    Deployed (ENVIRONMENT=staging|production): a curated mirror of the
    process env, with secret values replaced by "(set)".
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    pairs = _parse_dotenv(env_path)
    target_env = "staging"
    if not pairs:
        target_env, pairs = _deployed_env_pairs()
        if not pairs:
            logger.info("seed_env_vars: %s not found and not a deployed env, skipping", env_path)
            return {"inserted": 0, "updated": 0, "skipped": 0}

    client = get_service_client()
    inserted = 0
    updated = 0
    skipped = 0
    for name, value in pairs.items():
        if name in _SKIP:
            skipped += 1
            continue
        try:
            existing = (
                client.table("agent_env_vars").select("var_id, value, is_secret")
                .eq("environment", target_env).eq("name", name)
                .limit(1).execute().data or []
            )
            payload: Dict[str, Any] = {
                "environment": target_env,
                "name": name,
                "value": value,
                "is_secret": _looks_secret(name),
                "source": "db",
                "description": "Seeded from .env",
            }
            if existing:
                # Don't clobber values an admin edited via the portal —
                # only update if the row was originally seeded and is
                # still in its initial state. Use the description as a
                # cheap "is-seeded" tag.
                if (existing[0].get("value") or "") != value:
                    # Value drifted: leave the row alone, but log it.
                    logger.debug("seed_env_vars: %s drifted; not overwriting", name)
                    skipped += 1
                    continue
                client.table("agent_env_vars").update(payload).eq(
                    "var_id", existing[0]["var_id"]
                ).execute()
                updated += 1
            else:
                client.table("agent_env_vars").insert(payload).execute()
                inserted += 1
        except Exception as exc:
            logger.warning("seed_env_vars: %s skipped (%s)", name, exc)
            skipped += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}
