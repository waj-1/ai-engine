"""
Append-only audit log writer for the admin portal config plane.

Every CRUD endpoint in admin/router.py calls `record_change(...)`. The row
captures `{entity_type, entity_id, action, before, after, actor_user_id}`.
n8n doesn't have a dedicated audit table; we're closer to the
SAMA/PDPL-friendly pattern called out in PORTAL_PLAN.md §1.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

# Audit rows follow the env-switcher: a config write to the SELECTED branch
# must leave its audit row where that branch's /admin/audit read
# (`client_for(env)`) will find it. `admin_service_client()` reads the
# request-scoped `_ADMIN_ENV` (also re-set inside tracked job threads) and
# falls back to the local branch when no env is selected — e.g. /mcp calls,
# which never run the /admin env middleware (identical behavior there).
from core.auth import admin_service_client

logger = logging.getLogger(__name__)


def record_change(
    entity_type: str,
    entity_id: Optional[str],
    action: str,
    actor_user_id: Optional[str],
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort audit row insert.

    Failures are logged but never raised — the underlying admin operation
    has already succeeded, and we'd rather lose an audit row than fail the
    user's request after their config change landed.
    """
    try:
        client = admin_service_client()
        client.table("agent_audit_log").insert(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "action": action,
                "before": before,
                "after": after,
                "actor_user_id": actor_user_id,
            }
        ).execute()
    except Exception as exc:
        logger.warning("audit: insert failed for %s/%s (%s)", entity_type, entity_id, exc)
