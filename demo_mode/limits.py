"""The demo's cost and abuse caps.

Per visitor
    Sliding one-hour windows, per bucket (`chat`, `tests`, `uploads`,
    `entry`, `assistant`, `assistant_tools`, `voice`), kept in this process's memory. A visitor is their session (the
    `session_id` claim of the token "Try the demo" gave them) AND their
    network address: both are counted, the address with
    ``DEMO_IP_MULTIPLIER`` times the room, so opening a new session does not
    reset a visitor's allowance and an office behind one address is not
    locked out by one person. The engine's own in-process calls (the
    builder assistant's test chats, a test run's cases) come from a
    loopback address and are counted by session only. Memory, not the database: the limits are about
    bursts, and a restart forgetting them costs at most one more hour.

The whole demo
    ``DEMO_DAILY_TURNS`` model turns per UTC day (agent chat turns, builder
    assistant turns and test-judge calls), counted in the database
    (`demo_usage_take`, migration 20261123210000_demo_usage.sql) so every
    engine process shares one count and a restart does not refill it. When
    the database cannot be reached the count is kept in memory instead, so
    the cap still holds (per process) rather than switching off.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Tuple

from demo_mode import settings

logger = logging.getLogger(__name__)

WINDOW_SEC = 3600.0
TURNS_BUCKET = "chat_turns"
RESET_BUCKET = "reset"

CAP_MESSAGE = (
    "The public demo has reached today's limit, so it cannot call the model right now. "
    "It opens again at midnight UTC. To keep going, self-host the engine: it is free "
    "and open source, see the link at the top of the page."
)
RATE_MESSAGE = (
    "You are going a little fast for the public demo. Wait a few minutes and try again."
)

_lock = threading.Lock()
_hits: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_local_used: Dict[Tuple[str, str], int] = {}


def reset_for_tests() -> None:
    with _lock:
        _hits.clear()
        _local_used.clear()


def _limit_for(bucket: str) -> int:
    return {
        "chat": settings.chat_per_hour,
        "tests": settings.test_runs_per_hour,
        "uploads": settings.uploads_per_hour,
        "entry": settings.entries_per_hour,
        "assistant": settings.assistant_per_hour,
        "assistant_tools": settings.assistant_tools_per_hour,
        "voice": settings.voice_calls_per_hour,
    }.get(bucket, settings.chat_per_hour)()


def session_key(token: Optional[str]) -> Optional[str]:
    """The `session_id` (else `sub`) claim of a sign-in token, read WITHOUT
    checking its signature: it only names a counter. A forged token gains a
    fresh session counter, never a fresh address counter."""
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:  # noqa: BLE001 — not a JWT: no session counter
        return None
    value = claims.get("session_id") or claims.get("sub")
    return str(value)[:80] if value else None


def _loopback(ip: Optional[str]) -> bool:
    return (ip or "") in ("127.0.0.1", "::1")


def hit(bucket: str, *, ip: str, session: Optional[str], limit: Optional[int] = None) -> bool:
    """Count one request; False (and nothing counted) when it is over a limit.
    `limit` overrides the bucket's setting (the showcase's own caps)."""
    limit = _limit_for(bucket) if limit is None else max(1, int(limit))
    keys = []
    if not _loopback(ip) or not session:
        keys.append((bucket, "ip:" + (ip or "unknown"), limit * settings.ip_multiplier()))
    if session:
        keys.append((bucket, "s:" + session, limit))
    now = time.monotonic()
    with _lock:
        for b, who, cap in keys:
            q = _hits[(b, who)]
            while q and now - q[0] >= WINDOW_SEC:
                q.popleft()
            if len(q) >= cap:
                return False
        for b, who, _cap in keys:
            _hits[(b, who)].append(now)
    return True


# --------------------------------------------------------------------------
# The daily cap
# --------------------------------------------------------------------------


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _db():
    from core.auth import get_service_client

    return get_service_client()


def _db_take(bucket: str, limit: int, amount: int = 1) -> bool:
    data = _db().rpc("demo_usage_take", {"p_bucket": bucket, "p_limit": int(limit),
                                         "p_amount": int(amount)}).execute().data
    if isinstance(data, list):
        data = data[0] if data else False
    return bool(data)


def _db_used(bucket: str) -> int:
    data = _db().rpc("demo_usage_used", {"p_bucket": bucket}).execute().data
    if isinstance(data, list):
        data = data[0] if data else 0
    return int(data or 0)


def _local_take(bucket: str, limit: int, amount: int = 1) -> bool:
    key = (_today(), bucket)
    with _lock:
        used = _local_used.get(key, 0)
        if used + amount > limit:
            return False
        _local_used[key] = used + amount
        return True


def take(bucket: str, limit: int, amount: int = 1) -> bool:
    """Take `amount` units of today's `bucket` (all or none); False when that
    would go over `limit`."""
    amount = max(1, int(amount))
    if limit <= 0:
        return False
    try:
        return _db_take(bucket, limit, amount)
    except Exception as exc:  # noqa: BLE001 — the cap must hold without the database
        logger.warning("demo: daily counter unavailable (%s); counting in memory", type(exc).__name__)
        return _local_take(bucket, limit, amount)


def used(bucket: str) -> int:
    try:
        return _db_used(bucket)
    except Exception:  # noqa: BLE001
        with _lock:
            return _local_used.get((_today(), bucket), 0)


def take_turn(amount: int = 1) -> bool:
    """`amount` model turns from today's cap (``DEMO_DAILY_TURNS``)."""
    return take(TURNS_BUCKET, settings.daily_turns(), amount)


def turns_left() -> int:
    return max(0, settings.daily_turns() - used(TURNS_BUCKET))
