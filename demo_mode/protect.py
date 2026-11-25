"""The demo's own agent, the Builder assistant, is read-only and out of sight.

Every visitor shares one demo, so a visitor who edited, archived, forked or
promoted the Builder assistant would break the "Build with AI" panel for
everyone until the nightly reset. In demo mode:

* `refusal_for_path` refuses any change through the portal to its profile or
  its instructions (applied by demo_mode/gate.py to every non-GET request,
  `/admin/agents/<id>...`, `/admin/profiles/<id>...`, `/admin/prompts/<id>...`,
  and to a promote or fork form that names it, read by the handlers through
  `is_protected`);
* `demo_mode.assistant.check_call` refuses the assistant's own write tools
  for it;
* `hidden_agent` keeps it out of the Agents list, the pickers, the
  Conversations filter and search (the panel's "About this demo" note says
  what it is).

The ids are looked up by slug, every environment, and cached for a minute.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Any, Dict, Iterable, Optional, Set

from demo_mode import settings

logger = logging.getLogger(__name__)

BUILDER_SLUG = "builder-assistant"
HIDDEN_SLUGS = frozenset({BUILDER_SLUG})
READ_ONLY_MESSAGE = ("The Builder assistant is read-only in the public demo, so every visitor "
                     "gets the same one. Make your own agent to change instead.")

_TTL = 60.0
_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "ids": frozenset(), "showcase": frozenset()}

_PATH = re.compile(r"^/admin/(?:agents|profiles|prompts|channels)/([0-9a-fA-F-]{32,36})(?:/|$)")

#: The product-page showcase's agents (demo_mode/showcase.py) and its channel:
#: read-only too, so a visitor cannot break the product page that runs them.
SHOWCASE_MESSAGE = ("The Falak Connect agents are read-only in the public demo: a product page "
                    "runs them live. Make your own agent to change instead; you can copy their "
                    "instructions from the Instructions tab.")


def reset_for_tests() -> None:
    with _lock:
        _cache.update(at=0.0, ids=frozenset(), showcase=frozenset())


def _showcase_lookup(client) -> Set[str]:
    """The showcase's profile, instructions and channel ids (empty when it is off)."""
    if not settings.showcase_enabled():
        return set()
    from admin.services.custom_prompts import own_prompt_slug
    from demo_mode import showcase

    slugs = sorted(showcase.SLUGS)
    ids: Set[str] = set()
    for row in (client.table("agent_profiles").select("profile_id")
                .eq("agent_kind", "custom").in_("slug", slugs).execute().data or []):
        ids.add(str(row["profile_id"]))
    for row in (client.table("agent_prompts").select("prompt_id")
                .eq("agent_kind", "custom").in_("slug", [own_prompt_slug(s) for s in slugs])
                .execute().data or []):
        ids.add(str(row["prompt_id"]))
    for row in (client.table("agent_channels").select("channel_id")
                .eq("slug", showcase.CHANNEL["slug"]).execute().data or []):
        ids.add(str(row["channel_id"]))
    return ids


def _lookup() -> Set[str]:
    from admin.services.custom_prompts import own_prompt_slug
    from core.auth import get_service_client

    client = get_service_client()
    ids: Set[str] = set()
    for row in (client.table("agent_profiles").select("profile_id")
                .eq("agent_kind", "custom").eq("slug", BUILDER_SLUG).execute().data or []):
        ids.add(str(row["profile_id"]))
    for row in (client.table("agent_prompts").select("prompt_id")
                .eq("agent_kind", "custom").eq("slug", own_prompt_slug(BUILDER_SLUG))
                .execute().data or []):
        ids.add(str(row["prompt_id"]))
    showcase_ids = _showcase_lookup(client)
    with _lock:
        _cache["showcase"] = frozenset(showcase_ids)
    return ids | showcase_ids


def showcase_ids() -> frozenset:
    """The protected ids that belong to the showcase (a subset of `protected_ids`)."""
    protected_ids()
    with _lock:
        return _cache.get("showcase") or frozenset()


def protected_ids() -> frozenset:
    """The Builder assistant's profile ids (every environment) and its
    instructions' prompt id. Empty outside demo mode. Blocking when stale."""
    if not settings.enabled():
        return frozenset()
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < _TTL:
            return _cache["ids"]
    try:
        ids = frozenset(_lookup())
    except Exception as exc:  # noqa: BLE001 — keep the last known ids
        logger.warning("demo: builder ids unavailable (%s)", type(exc).__name__)
        with _lock:
            return _cache["ids"]
    with _lock:
        _cache.update(at=now, ids=ids)
    return ids


_refreshing = threading.Event()


def _refresh_in_background() -> None:
    if _refreshing.is_set():
        return
    _refreshing.set()

    def run() -> None:
        try:
            ids = frozenset(_lookup())
            with _lock:
                _cache.update(at=time.monotonic(), ids=ids)
        except Exception as exc:  # noqa: BLE001 — keep the last known ids, look again later
            logger.warning("demo: builder ids unavailable (%s)", type(exc).__name__)
            with _lock:
                _cache["at"] = time.monotonic()
        finally:
            _refreshing.clear()

    threading.Thread(target=run, name="demo-protect-refresh", daemon=True).start()


def data_generation() -> Optional[str]:
    """A short id for the demo's current data: it changes when the demo is
    wiped and seeded again (the Builder assistant's rows get new ids then),
    whoever did it (the nightly loop, scripts.demo_reset, an outside
    scheduler), and stays put when a reset failed or a boot only added what
    was missing. The guided tour keys a visitor's progress to it.

    Reuses `protected_ids`' cached lookup: blocking only for the very first
    call of the process; afterwards a stale cache is refreshed in the
    background and the last known value is returned. None outside demo mode
    or when the ids are unknown (the caller falls back to the clock)."""
    if not settings.enabled():
        return None
    with _lock:
        filled, at, ids = _cache["at"] > 0, _cache["at"], _cache["ids"]
    if not filled:
        ids = protected_ids()
    elif time.monotonic() - at >= _TTL:
        _refresh_in_background()
    if not ids:
        return None
    return hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()[:10]


def forget() -> None:
    """Drop the cached ids (after a reset), so the next look reads them again."""
    with _lock:
        _cache.update(at=0.0, ids=frozenset())


def is_protected(value: Optional[str]) -> bool:
    return bool(value) and str(value) in protected_ids()


def mentions_protected(values: Iterable[Any]) -> bool:
    """True when any value (nested lists and dicts too) is a protected id."""
    ids = protected_ids()
    if not ids:
        return False

    def walk(v: Any) -> bool:
        if isinstance(v, str):
            return v in ids
        if isinstance(v, dict):
            return any(walk(x) for x in v.values())
        if isinstance(v, (list, tuple, set)):
            return any(walk(x) for x in v)
        return False

    return any(walk(v) for v in values)


def path_target(path: str) -> Optional[str]:
    match = _PATH.match(path or "")
    return match.group(1) if match else None


def refusal_for_path(method: str, path: str) -> Optional[str]:
    """Why this portal request may not change the Builder assistant, or None.
    Only a change is refused; reading it is fine."""
    if (method or "GET").upper() in ("GET", "HEAD") or not settings.enabled():
        return None
    target = path_target(path)
    if target and is_protected(target):
        return SHOWCASE_MESSAGE if target in showcase_ids() else READ_ONLY_MESSAGE
    return None


def hidden_agent(row: Optional[Dict[str, Any]]) -> bool:
    """True for an agent the demo keeps out of lists, pickers and search."""
    if not row or not settings.enabled():
        return False
    return (row.get("agent_kind") in (None, "custom")
            and str(row.get("slug") or "") in HIDDEN_SLUGS)


def visible(rows: Iterable[Dict[str, Any]]) -> list:
    """`rows` without the agents the demo hides (all of them outside demo mode)."""
    return [r for r in rows if not hidden_agent(r)]
