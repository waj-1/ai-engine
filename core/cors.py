"""CORS origin resolution for the FastAPI app.

The allow-list is the union of:

* an explicit ``ALLOWED_ORIGINS`` env var (comma-separated),
* every active row's ``allowed_origins`` in the admin-portal-managed
  ``agent_channels`` table, the source of truth for who may call the agent
  runtime, and
* any origins a plugin allows at request time (its ``cors_origins()`` hook,
  passed to ``FirstPartyCORSMiddleware`` as ``dynamic_origins``).

Channel origins are read at request time
========================================
``FirstPartyCORSMiddleware`` asks ``core.channels`` (an in-process cache the
admin portal busts on every channel write, re-read after
``CHANNELS_CACHE_TTL_SEC`` in every other process) whenever an origin is not
already on the boot-time list. So a channel created or edited in the portal
allows its origins without a restart: at once in the process that served the
edit, within the TTL everywhere else. The boot-time list stays as the fast
path, and only an origin that is not on it costs a cache lookup.

With ``ALLOWED_ORIGINS=*`` every origin is allowed anyway (without
credentials), so channels do not change CORS at all.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable, Iterable, List, Optional, Set

from starlette.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


# First-party origins that are ALWAYS allowed, independent of
# the ALLOWED_ORIGINS env list and the channels table. Using ``*`` (zero-or-
# more subdomain labels) rather than ``+`` so the bare apex is covered too —
# e.g. ``https://tenants.example`` as well as ``https://tenant.tenants.example``. The apex
# marketing origins 301-redirect to company.example, but a direct or stale hit from an
# apex must not 400 the CORS preflight and silently break the landing page's
# public chat (the "something went wrong" failure mode). ``company.example`` is the
# public marketing site that hosts that chat, so it gets a pattern of its own
# rather than depending on the mutable ALLOWED_ORIGINS env list.
_STATIC_ALLOWED_ORIGIN_PATTERNS = (
    re.compile(r"^https?://([a-z0-9-]+\.)*(localhost|127\.0\.0\.1)(:\d+)?$"),
    re.compile(r"^https://([a-z0-9-]+\.)*company\.example$"),
    re.compile(r"^https://([a-z0-9-]+\.)*tenants\.example$"),
    re.compile(r"^https://([a-z0-9-]+\.)*staging\.example$"),
)


def is_static_allowed_origin(origin: str) -> bool:
    """Return True for first-party origins that are always
    allowed regardless of the env/channels allow-list.

    Covers localhost (any port, for dev) and the apex + any subdomain of
    ``company.example``, ``tenants.example`` and ``staging.example``. Kept pure (no I/O) so the
    CORS middleware can call it per-request and tests can assert it directly.
    """
    if not isinstance(origin, str) or not origin:
        return False
    return any(pattern.match(origin) for pattern in _STATIC_ALLOWED_ORIGIN_PATTERNS)


def _split_env_origins(raw: Optional[str]) -> List[str]:
    """Parse a comma-separated ALLOWED_ORIGINS string into a clean list.

    Wildcard ("*") short-circuits — callers handle wildcard mode upstream
    so they shouldn't be calling us with it, but we defensively return
    ["*"] so a misconfigured caller still gets the right behaviour.
    """
    if not raw:
        return []
    if raw.strip() == "*":
        return ["*"]
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


def _channel_origins(channels_provider) -> Set[str]:
    """Aggregate ``allowed_origins`` from every active channel.

    ``channels_provider`` is a zero-arg callable returning the channels
    map. Injected for testability so tests don't have to monkeypatch
    a module-level import.
    """
    out: Set[str] = set()
    try:
        channels = channels_provider() or {}
    except Exception as exc:
        logger.warning("cors: channels_provider raised (%s); skipping channels", exc)
        return out
    for row in channels.values():
        if not isinstance(row, dict):
            continue
        origins = row.get("allowed_origins") or []
        for origin in origins:
            if isinstance(origin, str) and origin.strip():
                out.add(origin.strip())
    return out


def build_allowed_origins(
    *,
    env_value: Optional[str] = None,
    extra_origins: Optional[Iterable[str]] = None,
    channels_provider=None,
) -> List[str]:
    """Union the env, extra and channels-derived origin lists.

    Inputs (all keyword-only for clarity):

    * ``env_value`` — raw ``ALLOWED_ORIGINS`` env var content. Defaults
      to ``os.environ['ALLOWED_ORIGINS']``. If it is exactly ``"*"`` we
      short-circuit to ``["*"]`` — the caller switches into permissive
      mode (no credentials) without consulting channels.
    * ``extra_origins`` — further origins to allow, fixed at boot.
      Optional; defaults to empty.
    * ``channels_provider`` — zero-arg callable returning
      ``Dict[slug, channel_dict]``. Defaults to
      ``core.channels.get_active_channels`` (lazy-imported so this
      module stays importable in environments where the Supabase
      client can't bootstrap).

    Returns a deduplicated, sorted list. Sorting is purely for stable
    logs and snapshot-friendly tests — Starlette's CORSMiddleware does
    an ``in`` check so order doesn't matter at runtime.
    """
    if env_value is None:
        env_value = os.getenv("ALLOWED_ORIGINS", "")

    env_origins = _split_env_origins(env_value)
    if env_origins == ["*"]:
        # Wildcard mode: skip channels entirely. The caller wires the
        # middleware with allow_origins=["*"] and disables credentials.
        return ["*"]

    merged: Set[str] = set(env_origins)
    if extra_origins:
        for origin in extra_origins:
            if isinstance(origin, str) and origin.strip():
                merged.add(origin.strip())

    if channels_provider is None:
        try:
            from core.channels import get_active_channels as channels_provider  # type: ignore
        except Exception as exc:  # pragma: no cover - defensive: import-time
            logger.warning("cors: core.channels unavailable (%s)", exc)
            channels_provider = None  # type: ignore

    if channels_provider is not None:
        merged |= _channel_origins(channels_provider)

    return sorted(merged)


def live_channel_origins() -> Set[str]:
    """Every active channel's origins as ``core.channels`` sees them now
    (its cache: busted by portal writes, re-read after a TTL). Empty when
    the channels table cannot be read."""
    try:
        from core.channels import get_active_channels
    except Exception as exc:  # pragma: no cover - defensive: import-time
        logger.warning("cors: core.channels unavailable (%s)", exc)
        return set()
    return _channel_origins(get_active_channels)


class FirstPartyCORSMiddleware(CORSMiddleware):
    """The allow-list middleware ``main.py`` installs when ``ALLOWED_ORIGINS``
    is an explicit list: the boot-time list (env + channels, see
    ``build_allowed_origins``), PLUS every first-party origin
    (``is_static_allowed_origin`` — company.example, tenants.example, staging.example, localhost),
    PLUS every active channel's origins as they are NOW
    (``channel_origins()``, default ``live_channel_origins``: a channel added
    in the portal after boot works without a restart), PLUS whatever
    ``dynamic_origins()`` returns at request time (the plugins'
    ``cors_origins()`` hook; a callable so a plugin may refresh its set).

    Lives here rather than inline in main.py so the exact class prod runs can
    be tested (e.g. the landing page calling the public /agents API
    cross-origin, preflight included).
    """

    def __init__(self, app, *, dynamic_origins: Optional[Callable[[], Iterable[str]]] = None,
                 channel_origins: Optional[Callable[[], Iterable[str]]] = live_channel_origins,
                 **kwargs):
        super().__init__(app, **kwargs)
        self._dynamic_origins = dynamic_origins
        self._channel_origins = channel_origins

    def is_allowed_origin(self, origin: str) -> bool:
        if origin in self.allow_origins:
            return True
        if is_static_allowed_origin(origin):
            return True
        if self._channel_origins is not None:
            try:
                if origin in set(self._channel_origins() or ()):
                    return True
            except Exception:  # pragma: no cover - defensive
                pass
        if self._dynamic_origins is not None:
            try:
                return origin in set(self._dynamic_origins() or ())
            except Exception:  # pragma: no cover - defensive
                return False
        return False
