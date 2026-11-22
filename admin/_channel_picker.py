"""Shared helper for the channel-slug picker used by observability screens.

Both the classic trace-list and the explorer want the same dropdown:
list of ``{slug, name}`` for every active channel. Wrapping
``core.channels.list_active()`` here keeps the call signature stable
even if the underlying source changes (cache busting, schema rename,
etc.) and gives the picker a single failure point.

The active-channels cache in ``core/channels.py`` already memoises the
DB query, so this helper is essentially free to call per request.

The fixed list of agent kinds lives here too — it's the same set the
channel-aware request middleware validates against. Surfacing it as a
constant here means the observability templates don't need to know
about ``core/channel_resolver.py``.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from core.deployment import active_scope

logger = logging.getLogger(__name__)


# The deployment's own agents, in the order its plugin lists them
# (`agent_catalog.picker_kinds()`); none in a build with no plugin.
from admin.agent_kinds import catalog_tuple as _catalog_tuple  # noqa: E402

AGENT_KINDS: tuple[str, ...] = _catalog_tuple("picker_kinds")


def visible_agent_kinds_for_picker() -> tuple[str, ...]:
    """Agent kinds the observability filters offer. Client-only (e.g.
    ``("tenant",)``) on any scoped build; otherwise the local WAJ list
    (unchanged behavior)."""
    return (client.agent_kind, *client.extra_kinds) if (client := active_scope()) else AGENT_KINDS


def fetch_channels_for_picker() -> List[Dict[str, str]]:
    """Return active channels as ``[{slug, name}, ...]`` ordered by slug.

    Empty list on any failure — the observability screens render the
    dropdown with just an "any channel" option instead of crashing.
    """
    try:
        from core.channels import list_active

        rows = list_active()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("channel picker: list_active failed (%s)", exc)
        return []

    # Scoped build: drop WAJ-product channels so the observability channel
    # filter doesn't advertise them (same narrow the /channels list applies).
    # No-op on the full WAJ engine.
    try:
        from admin.channel_handlers import visible_channels

        rows = visible_channels(list(rows))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("channel picker: visible_channels narrow failed (%s)", exc)

    out: List[Dict[str, str]] = []
    for row in rows:
        slug = (row.get("slug") or "").strip()
        if not slug:
            continue
        out.append({"slug": slug, "name": row.get("name") or slug})
    return out
