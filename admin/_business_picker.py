"""Shared helper for the business-id picker used across the admin portal.

Every screen that asks an operator for a `business_id` (eval trigger,
session filter, suite default context, profile test-chat widget) reads
the same shape: list of `{id, name, subdomain}` ordered by name. Keep
that fetch in one place so the columns + ordering stay consistent and
the admin uses ONE query against the `business` table on each page.

The `business` table lives in the MAIN project, so this helper builds its
own MAIN service-role client via `get_main_client()` (the AI-project
`get_service_client()` cannot see `business`). The `client` parameter is
retained only for backward/test compatibility and is otherwise ignored.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.auth import get_main_client

logger = logging.getLogger(__name__)

# Cap so a tenant explosion doesn't blow up the page. 500 is well above
# WAJ's current count; the picker is a `<select>`, not autocomplete, so
# huge lists would become unusable anyway — when we cross this we add a
# search box client-side.
_MAX_BUSINESSES = 500


def fetch_businesses_for_picker(client: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Return businesses as picker rows. Empty list on any failure
    (the picker just renders empty + a 'no businesses' hint) so the
    page never crashes on a `business` table outage.

    `business` is a MAIN-project table, so we always query through a fresh
    MAIN service-role client built here. The `client` argument is accepted
    for backward compatibility with existing call sites but ignored.
    """
    del client  # legacy parameter — the `business` table is MAIN-resident
    try:
        main_client = get_main_client()
        result = (
            main_client.table("business")
            .select("id, business_name, subdomain_name")
            .order("business_name")
            .limit(_MAX_BUSINESSES)
            .execute()
        )
        rows = result.data or []
    except Exception as exc:
        logger.warning("business picker: fetch failed (%s)", exc)
        return []
    return [
        {
            "id": r["id"],
            "name": r.get("business_name") or r["id"],
            "subdomain": r.get("subdomain_name") or "",
        }
        for r in rows
        if r.get("id")
    ]
