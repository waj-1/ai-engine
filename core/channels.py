"""Runtime accessor for ``wajagents.agent_channels``.

A *channel* names a request source (an app, a marketing landing
page, a tenant's own website, a partner app, the public API, …) and carries the
policy the runtime should apply to it: CORS allow-list, allowed
agent_kinds, default profile, branding, auth + rate-limit knobs. The
admin portal (`/admin/channels`, served by
`admin/channel_handlers.py`) owns the CRUD; this module provides a
small in-process cache so the runtime doesn't hit Postgres on every
request.

Typical use::

    from core.channels import get_active_channels

    channels = get_active_channels()
    site = channels.get("website_chat")
    if site and origin in site["allowed_origins"]:
        ...  # accept the request

Readers: the request-time channel resolver (``core.channel_resolver``) and
the CORS middleware (``core.cors.FirstPartyCORSMiddleware``), which allows a
channel's origins as soon as the cache sees the channel.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)


# Module-level cache. Threading lock guards both the dict and the
# loaded-flag so a concurrent invalidate() during a load() doesn't
# return a half-populated dict.
_lock = threading.Lock()
_cache: Optional[Dict[str, Dict[str, Any]]] = None
_loaded_at = 0.0


def _ttl_seconds() -> float:
    """How long a loaded map is served before it is read again.

    ``invalidate()`` makes a portal edit take effect at once in the process
    that served the edit; the TTL is what lets every OTHER process (another
    replica, a separate chat service) see it too. ``CHANNELS_CACHE_TTL_SEC``,
    default 60; 0 or less = never expire (the old behaviour)."""
    try:
        return float(os.getenv("CHANNELS_CACHE_TTL_SEC", "60"))
    except ValueError:
        return 60.0


# Columns we surface to callers. Pinned so a downstream schema addition
# doesn't accidentally leak (or remove) fields the runtime depends on.
_FIELDS: tuple[str, ...] = (
    "channel_id",
    "slug",
    "name",
    "kind",
    "is_active",
    "allowed_origins",
    "allowed_agent_kinds",
    "default_profile_id",
    "business_id",
    "auth_required",
    "rate_limit_rpm",
    "config",
    "description",
)


def _normalize(row: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a row from PostgREST into a plain dict with the runtime
    fields. Defaults `None` array columns to `[]` so callers can `in`-test
    without a None-guard."""
    out: Dict[str, Any] = {f: row.get(f) for f in _FIELDS}
    if out.get("allowed_origins") is None:
        out["allowed_origins"] = []
    if out.get("allowed_agent_kinds") is None:
        out["allowed_agent_kinds"] = []
    if out.get("config") is None:
        out["config"] = {}
    return out


def _load_from_db() -> Dict[str, Dict[str, Any]]:
    """Fetch every active channel and key by slug.

    Failures (Supabase down, table not yet migrated in a stale dev DB,
    network blip) degrade gracefully — we return an empty mapping and
    log a warning rather than raising. The caller treats "no channels"
    the same as "channels feature not in use" and falls back to the
    legacy origin-list pathway.
    """
    try:
        client = get_service_client()
        result = client.table("agent_channels").select("*").eq(
            "is_active", True
        ).execute()
        rows: Iterable[Dict[str, Any]] = result.data or []
    except Exception as exc:
        logger.warning("channels: load_from_db failed (%s); returning empty map", exc)
        return {}
    return {row["slug"]: _normalize(row) for row in rows if row.get("slug")}


def get_active_channels() -> Dict[str, Dict[str, Any]]:
    """Return the active-channels map keyed by slug.

    Cached at first call and re-read after ``CHANNELS_CACHE_TTL_SEC``; call
    ``invalidate()`` from the admin write paths to bust the cache so the next
    request sees the latest policy.
    Returns a fresh shallow copy on each call so callers can mutate the
    top-level dict without poisoning the cache.
    """
    global _cache, _loaded_at
    with _lock:
        ttl = _ttl_seconds()
        stale = ttl > 0 and (time.monotonic() - _loaded_at) > ttl
        if _cache is None or stale:
            _cache = _load_from_db()
            _loaded_at = time.monotonic()
        return dict(_cache)


def get_channel(slug: str) -> Optional[Dict[str, Any]]:
    """Convenience for the common ``channels[slug]`` lookup with a None
    fallback. Returns a shallow copy so the caller can't mutate the
    cache by reference."""
    snapshot = get_active_channels()
    row = snapshot.get(slug)
    return dict(row) if row is not None else None


def list_active() -> List[Dict[str, Any]]:
    """All active channels as a list, ordered by slug for stable output
    in logs and admin diagnostics."""
    return sorted(get_active_channels().values(), key=lambda c: c["slug"])


def invalidate() -> None:
    """Drop the cache so the next call re-reads from Postgres. Called
    by ``admin/channel_handlers.py`` after every create/update/delete."""
    global _cache
    with _lock:
        _cache = None
