"""Per-tenant daily audio-second budget for duplex voice.

Why this exists: `voice/rate_limit.py` bounds REQUESTS. A duplex session is
one request that can run for minutes and bill per audio token in both
directions, so it needs a different control entirely.

Persisted rather than in-process because the engine runs multiple replicas —
an in-memory counter would let each replica spend the whole budget. The
per-replica concurrency caps in the endpoint are deliberately NOT persisted
(see the spec): over-admitting a couple of sessions is cheap, over-spending a
day's budget is not.

Fails OPEN. If the AI project is unreachable we let the call proceed rather
than take voice down for everyone — the same posture
`core/chat_persistence.py` takes.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_DAILY_SECONDS = int(os.getenv("VOICE_LIVE_DAILY_SECONDS", "3600"))

_TABLE = "ai_voice_usage_daily"
_BUDGETS_TABLE = "ai_voice_budgets"
_RESERVE_RPC = "ai_reserve_voice_seconds"


class BudgetExhausted(RuntimeError):
    """The tenant has spent its daily duplex-voice budget."""


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _client_or_default(client: Optional[Any]) -> Any:
    if client is not None:
        return client
    from core.auth import get_service_client

    return get_service_client()


def _seconds_used(client: Any, business_id: str, today: str) -> int:
    rows = (
        client.table(_TABLE)
        .select("seconds_used")
        .eq("business_id", business_id)
        .eq("usage_date", today)
        .limit(1)
        .execute()
        .data
    )
    return int((rows[0] if rows else {}).get("seconds_used") or 0)


def get_daily_limit(business_id: str, *, client: Optional[Any] = None) -> int:
    """The tenant's daily audio-second allowance.

    Per-tenant row in `ai_voice_budgets` (admin-portal-managed) when one
    exists; the engine-wide `VOICE_LIVE_DAILY_SECONDS` default otherwise.
    Fails open to the default on any DB error — same posture as the
    accounting itself.
    """
    if not business_id:
        return DEFAULT_DAILY_SECONDS
    try:
        rows = (
            _client_or_default(client)
            .table(_BUDGETS_TABLE)
            .select("daily_seconds")
            .eq("business_id", business_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        logger.warning("voice budget: limit lookup unavailable; using default", exc_info=True)
        return DEFAULT_DAILY_SECONDS
    if rows and rows[0].get("daily_seconds") is not None:
        return max(0, int(rows[0]["daily_seconds"]))
    return DEFAULT_DAILY_SECONDS


def check_audio_budget(
    business_id: str,
    *,
    seconds: int = 0,
    client: Optional[Any] = None,
    daily_limit: Optional[int] = None,
) -> None:
    """Raise BudgetExhausted if the tenant is already at its daily cap.

    Read-only on purpose. The handshake used to CHARGE its 30s before the
    provider session was built, and nothing refunded it when setup then failed
    — so a client in a reconnect loop against a broken provider drained the
    whole day's budget without ever speaking. The endpoint now checks here
    first and only charges once `session.start()` has actually succeeded.

    Fails open (returns) on any DB error, matching reserve_audio_seconds.
    """
    if not business_id:
        return
    limit = get_daily_limit(business_id, client=client) if daily_limit is None else daily_limit
    try:
        current = _seconds_used(_client_or_default(client), business_id, _today())
    except Exception:
        logger.warning("voice budget: check unavailable; failing open", exc_info=True)
        return
    # `current >= limit` covers "nothing left at all" (a 0-second probe must
    # still refuse); the second clause mirrors reserve_audio_seconds exactly,
    # which is why `seconds` exists. With only the first, the two disagreed
    # over the window
    # `limit - seconds < current < limit`: the check passed, the provider
    # session was opened, and only THEN did the reservation raise -- so the
    # caller paid for a live upstream session that was immediately abandoned.
    if current >= limit or current + max(0, int(seconds)) > limit:
        raise BudgetExhausted(
            f"tenant {business_id} has used {current}s of its {limit}s daily voice budget"
        )


def reserve_audio_seconds(
    business_id: str,
    seconds: int,
    *,
    client: Optional[Any] = None,
    daily_limit: Optional[int] = None,
) -> int:
    """Add `seconds` to today's tally and return the new total.

    Raises BudgetExhausted when this reservation takes the tenant past its
    daily limit — but the usage is STILL RECORDED, clamped to the limit.
    Returns 0 (and records nothing) when there is no business id or the DB is
    unreachable.

    The clamp is the whole point, and it closes a cap bypass. This function
    used to raise BEFORE the upsert whenever `total > limit`, which meant the
    charge was silently thrown away — and the largest charge in a session was
    (before periodic charging) the teardown one (`elapsed - handshake`),
    precisely the one most likely to
    cross the line. A 15-minute call therefore recorded only its 30s handshake,
    leaving the tenant's tally far below the cap, so the next connect was
    admitted, and the next: roughly 30 hours of billable audio against a
    1-hour/day limit. Recording the clamped total instead means the tally
    reaches the cap and STAYS there, so the following handshake is refused.

    Reservation is ATOMIC where the DB carries `ai_reserve_voice_seconds`
    (migration 20260802140000): a row-locked stored function adds and clamps
    in one statement, so two sessions charging concurrently can no longer
    lose an increment. On a DB that predates the migration the legacy
    read-modify-write below still runs — its race costs at most one
    session's seconds and always errs toward under-counting, never toward
    refusing a legitimate caller.
    """
    if not business_id:
        return 0

    limit = get_daily_limit(business_id, client=client) if daily_limit is None else daily_limit
    today = _today()
    client = _client_or_default(client)

    # Atomic path first. Any failure here (function missing on an older DB,
    # transport error, unexpected payload shape) falls through to the legacy
    # path rather than failing the call — reservation must never be the
    # thing that takes voice down.
    try:
        payload = client.rpc(
            _RESERVE_RPC,
            {
                "p_business_id": business_id,
                "p_seconds": max(0, int(seconds)),
                "p_limit": max(0, int(limit)),
            },
        ).execute()
        data = getattr(payload, "data", payload)
        if isinstance(data, list):
            data = data[0] if data else None
        total = int(data)
        if total > limit:
            raise BudgetExhausted(
                f"tenant {business_id} used {total}s of its {limit}s daily voice budget"
            )
        return total
    except BudgetExhausted:
        raise
    except Exception:
        logger.debug(
            "voice budget: %s RPC unavailable; using legacy read-modify-write",
            _RESERVE_RPC,
            exc_info=True,
        )

    try:
        current = _seconds_used(client, business_id, today)
        total = current + max(0, int(seconds))
        exhausted = total > limit
        recorded = min(total, limit)
        client.table(_TABLE).upsert(
            {
                "business_id": business_id,
                "usage_date": today,
                "seconds_used": recorded,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="business_id,usage_date",
        ).execute()
        if exhausted:
            raise BudgetExhausted(
                f"tenant {business_id} used {total}s of its {limit}s daily voice budget"
            )
        return recorded
    except BudgetExhausted:
        raise
    except Exception:
        logger.warning("voice budget: accounting unavailable; failing open", exc_info=True)
        return 0
