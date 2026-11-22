"""Read-only client for Laminar's self-hosted ClickHouse.

**This is the ONLY read path for observability data.** Every `/admin` trace,
span, transcript and Live-Monitor number comes through here: it POSTs SQL to
ClickHouse's HTTP interface (``:8123``) and parses ``FORMAT JSON`` results.
Server-side aggregation (``toStartOfInterval``, ``quantile``, per-agent rollups)
is the reason — but it is also the only interface the deployed self-hosted
Laminar exposes that we can authenticate against.

Design rules:

* **Read-only by intent.** We only ever send ``SELECT``s, authenticate with
  ClickHouse's RO user, and pass ``readonly=1`` so a malformed query can't
  mutate anything even by accident.
* **Parameterised, never string-formatted.** Callers use ``{name:Type}``
  placeholders and pass values via ``params``; we forward them as ClickHouse
  ``param_<name>`` HTTP params. User-supplied filters never get concatenated
  into SQL.
* **Graceful degradation.** Every failure (stack down, DNS, timeout, bad SQL,
  non-JSON) raises :class:`ClickHouseUnavailable`. Callers catch it and render
  an empty-but-shaped dashboard rather than 500-ing.
* **Say which failure.** Each :class:`ClickHouseUnavailable` carries a
  ``kind`` — ``unreachable`` (transport), ``auth`` (credentials refused) or
  ``rejected`` (ClickHouse answered and refused the query / sent garbage) — and
  the most recent one is kept for :func:`last_failure`, so the portal banner
  can name the real cause. It used to read "could not be reached" for all
  three, and a month of rejected trace-list SQL (Code: 10, 2026-08-26 →
  2026-09-29) was chased as a network problem.

Configuration (env, with sensible defaults for the dev compose network):

* ``LMNR_CLICKHOUSE_URL``      → ``http://lmnr-clickhouse:8123``
* ``LMNR_CLICKHOUSE_USER``     → ``clickhouse``
* ``LMNR_CLICKHOUSE_PASSWORD`` → ``clickhouse``
* ``LMNR_CLICKHOUSE_DB``       → ``default``
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, NamedTuple, Optional

import httpx

logger = logging.getLogger(__name__)


class ClickHouseUnavailable(RuntimeError):
    """ClickHouse could not be reached, or the query failed.

    ``kind`` is ``"unreachable"``, ``"auth"`` or ``"rejected"`` (see the module
    docstring)."""

    def __init__(self, message: str, *, kind: str = "rejected") -> None:
        super().__init__(message)
        self.kind = kind


class Failure(NamedTuple):
    kind: str
    message: str
    at: float  # time.monotonic()


# HTTP statuses ClickHouse uses for a refused login / missing grant (516 is
# AUTHENTICATION_FAILED; 401/403 come from proxies and the HTTP handler).
_AUTH_STATUSES = {401, 403, 516}

# Process-wide, not per-request: the observability loaders swallow the
# exception and return None, and several of them run per page, so the screen
# that renders the banner reads the most recent failure back from here. A
# failure is system state (the one ClickHouse), not per-user data.
_last_failure: Optional[Failure] = None
_lock = threading.Lock()


def _record_failure(exc: ClickHouseUnavailable) -> ClickHouseUnavailable:
    global _last_failure
    with _lock:
        _last_failure = Failure(exc.kind, str(exc), time.monotonic())
    return exc


def last_failure(max_age_sec: float = 120.0) -> Optional[Failure]:
    """The most recent query failure, if it happened within ``max_age_sec``."""
    with _lock:
        f = _last_failure
    if f is None or time.monotonic() - f.at >= max_age_sec:
        return None
    return f


def clear_last_failure() -> None:
    global _last_failure
    with _lock:
        _last_failure = None


def tracing_configured() -> bool:
    """False when this install runs with no tracing store at all.

    That is the case when tracing is switched off (``LMNR_DISABLED``) and no
    ClickHouse address was given (``LMNR_CLICKHOUSE_URL`` unset): nothing
    writes spans and there is nothing to read. The portal then lists
    conversations from the saved chats instead (admin/screens/saved_chats.py)
    and says plainly what tracing would add, rather than reporting an outage.

    An explicit ``LMNR_CLICKHOUSE_URL`` always counts as configured, so a
    reader that exports no spans of its own (a control plane) still reads the
    shared store exactly as before.
    """
    if (os.getenv("LMNR_CLICKHOUSE_URL") or "").strip():
        return True
    return (os.getenv("LMNR_DISABLED") or "").strip().lower() not in ("1", "true", "yes", "on")


TRACING_OFF_NOTE = (
    "Tracing is off on this install. Conversations are saved and shown from the database; "
    "step-by-step detail (timings, each model call, tool inputs and outputs) needs the tracing stack."
)


def _config() -> Dict[str, str]:
    return {
        "url": os.getenv("LMNR_CLICKHOUSE_URL", "http://lmnr-clickhouse:8123").rstrip("/"),
        "user": os.getenv("LMNR_CLICKHOUSE_USER", "clickhouse"),
        "password": os.getenv("LMNR_CLICKHOUSE_PASSWORD", "clickhouse"),
        "database": os.getenv("LMNR_CLICKHOUSE_DB", "default"),
    }


def query(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Run a read-only ``SELECT`` and return its rows as a list of dicts.

    ``params`` are sent as ClickHouse query parameters: use ``{name:Type}`` in
    ``sql`` and pass ``{"name": value}`` here. Raises
    :class:`ClickHouseUnavailable` on any failure.
    """
    if not tracing_configured():
        # Not an outage: no network call, and not recorded as a failure, so no
        # screen reports "could not be reached" for a store that is not there.
        raise ClickHouseUnavailable("tracing is off on this install", kind="off")
    cfg = _config()
    http_params: Dict[str, str] = {
        "database": cfg["database"],
        "default_format": "JSON",
        "readonly": "1",
    }
    for key, value in (params or {}).items():
        http_params[f"param_{key}"] = str(value)

    try:
        resp = httpx.post(
            cfg["url"],
            params=http_params,
            content=sql.encode("utf-8"),
            auth=(cfg["user"], cfg["password"]),
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise _record_failure(ClickHouseUnavailable(
            f"ClickHouse request failed: {exc}", kind="unreachable"
        )) from exc

    if resp.status_code != 200:
        # ClickHouse puts the error code + message in the body; keep it short.
        raise _record_failure(ClickHouseUnavailable(
            f"ClickHouse returned {resp.status_code}: {resp.text[:200]}",
            kind=(
                "auth"
                if resp.status_code in _AUTH_STATUSES or "Code: 516." in resp.text
                else "rejected"
            ),
        ))

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - any parse failure is "unavailable"
        raise _record_failure(ClickHouseUnavailable(
            f"ClickHouse returned non-JSON: {exc}", kind="rejected"
        )) from exc

    return payload.get("data", [])
