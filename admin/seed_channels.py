"""Seed the default channel registry (agent_channels).

The Channels screen read an always-empty table ("No channels connected")
because nothing ever wrote to it — channels were designed as an opt-in
gating/tagging layer. A plugin's default channels (the ``channels`` hook,
``core/channel_kinds.py``) register its real request sources so the screen
reflects reality and traces get `waj.channel.*` tagging via
``core.channel_resolver`` (which matches the inbound Origin header against
``allowed_origins``).

SAFETY: every seeded row has ``allowed_agent_kinds = []`` — per the column
contract an empty list means "no restriction", so seeding can never 403
live traffic. Operators tighten the whitelists from the portal.

Idempotent on slug and INSERT-ONLY: rows an operator has edited (or
deleted… which resurrects on next boot — delete is_active=false instead)
are never overwritten.
"""
from __future__ import annotations

import logging
from typing import Dict

from core.auth import get_service_client
from core.channel_kinds import default_channels

logger = logging.getLogger(__name__)


def run() -> Dict[str, int]:
    """Insert any missing default channel rows. Never updates existing ones.

    The rows come from the plugins (``core.channel_kinds.default_channels``);
    with none, nothing is read or written."""
    channels = default_channels()
    if not channels:
        logger.info("seed_channels: no default channels in this build")
        return {"inserted": 0, "skipped": 0}

    client = get_service_client()
    try:
        existing = {
            r["slug"]
            for r in (
                client.table("agent_channels").select("slug").execute().data or []
            )
        }
    except Exception as exc:  # table missing on an old branch — skip quietly
        logger.warning("seed_channels: cannot read agent_channels (%s); skipping", exc)
        return {"inserted": 0, "skipped": 0}

    inserted = 0
    skipped = 0
    for ch in channels:
        if ch["slug"] in existing:
            skipped += 1
            continue
        try:
            client.table("agent_channels").insert(dict(ch)).execute()
            inserted += 1
        except Exception as exc:
            logger.warning("seed_channels: insert %s failed: %s", ch["slug"], exc)
    if inserted:
        logger.info("seed_channels: inserted %d default channel(s)", inserted)
    return {"inserted": inserted, "skipped": skipped}
