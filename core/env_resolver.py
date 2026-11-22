"""
Env-var resolver — read order: ``os.environ`` first, ``agent_env_vars`` second.

Background
----------
The agent service has historically read configuration from two places:

1. Local dev — ``ai_agent/.env`` (loaded via python-dotenv at startup).
2. Staging / prod Cloud Run — GCP Secret Manager, mounted into the service
   yaml via ``valueFrom: secretKeyRef``.

The admin portal (``/admin/env-vars``) now offers a third location: a row
in ``wajagents.agent_env_vars`` keyed by ``(environment, name)``. This module
is the single read-path that lets runtime code transparently consume a
value that may live in any of those three places.

Lookup order
------------
``resolve_env(name, environment=None)`` resolves a value in this order:

1. ``os.environ[name]`` — the existing Cloud Run / .env behaviour. We keep
   this first so a deploy can still override a DB value without a UI edit.
2. ``agent_env_vars`` row matching ``environment`` (or ``ENVIRONMENT`` env
   var when not passed) and ``name``. Only rows with ``source='db'`` are
   used inline today; ``source='secret_manager:…'`` rows are recognised
   but currently surface ``None`` because the Secret Manager integration
   follows in a separate PR.

This module is intentionally NOT yet wired into the agent runtime — it is
shipped alongside ``/admin/env-vars`` so ops can populate values and tests
can exercise the resolver. Wiring will land in a follow-up.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _current_environment() -> str:
    """Mirror the convention used elsewhere in ai_agent: ENVIRONMENT env
    var, falling back to 'dev'. Values are normalised to lowercase and
    mapped to the agent_environment enum (``dev``/``staging``/``prod``).
    """
    raw = (os.getenv("ENVIRONMENT") or "staging").lower()
    if raw in ("production", "prod"):
        return "prod"
    # 'dev' / development / local collapse to the live staging env (dev retired).
    return "staging"


def resolve_env(name: str, environment: Optional[str] = None) -> Optional[str]:
    """Return the value of env var ``name``, or ``None`` if unset.

    Resolution order:
      1. ``os.environ[name]`` — wins so a Cloud Run deploy override is
         respected.
      2. ``agent_env_vars`` row matching (``environment``, ``name``) with
         ``source='db'``. ``environment`` defaults to the current process
         environment.

    Rows with ``source`` starting with ``secret_manager:`` or
    ``supabase_vault:`` are recognised but the actual fetch is NOT YET
    wired — those return ``None`` today. Secret Manager integration
    follows in a separate PR; for now use ``source='db'`` to store the
    value inline.
    """
    direct = os.environ.get(name)
    if direct is not None and direct != "":
        return direct

    env = environment or _current_environment()
    try:
        # Imported lazily so tests can run without a live Supabase client.
        from core.auth import get_service_client  # type: ignore  # noqa: WPS433

        client = get_service_client()
        rows = (
            client.table("agent_env_vars")
            .select("value, source")
            .eq("environment", env)
            .eq("name", name)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("env_resolver: db lookup failed for %s (%s)", name, exc)
        return None

    if not rows:
        return None
    row = rows[0]
    source = (row.get("source") or "db").strip()
    if source == "db":
        return row.get("value")
    # secret_manager:<proj>/<secret> and supabase_vault:<key> are
    # placeholders that haven't been wired yet.
    logger.debug(
        "env_resolver: %s has source=%s but external resolution is not wired yet", name, source
    )
    return None


__all__ = ["resolve_env"]
