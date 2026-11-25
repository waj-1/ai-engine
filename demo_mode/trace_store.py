"""The demo's trace store (ClickHouse), emptied by the nightly reset.

With tracing on, every chat a visitor has is also kept as a trace: the model's
input and output, each tool call's arguments and result. The reset deletes the
conversations from the database (demo_mode/reset.py); this deletes the same
conversations from the trace store, so nothing a visitor typed outlives the
night there either.

Only where the demo has a store of its own: ``LMNR_CLICKHOUSE_URL`` set
explicitly. Every table that holds rows is emptied (``TRUNCATE``), except the
store's own bookkeeping: ``_migrations`` (truncating it would make Laminar's
migration runner apply the whole schema again) and the schema marker.

The demo's own sample conversations can be kept (demo_mode/retention.py
decides which traces; `wipe(keep=...)` keeps exactly those). Then, instead of
a TRUNCATE, the three tables that hold them are filtered:

* ``spans`` and ``traces_replacing``: every row whose trace is not kept is
  deleted.
* ``deduped_content`` (message text, keyed by hash and shared between spans):
  after the spans are gone, every row no remaining span refers to is deleted.
  What stays is exactly the text of the kept sample conversations.
* every other table is emptied as before.

ClickHouse deletes are mutations that normally finish in the background. Each
one here is run with ``mutations_sync = 2`` (the statement returns when the
mutation is done), and afterwards the store is asked whether any row outside
the kept traces is left and whether any mutation is still running. If either
is so, or anything fails on the way, every table is emptied after all: the
reset never leaves a visitor's trace behind in order to keep a sample one.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Collection, Dict, List, Optional

import httpx

from demo_mode import settings

logger = logging.getLogger(__name__)

#: The list of kept trace ids, written before the deletes read it.
KEEP_TABLE = "engine_keep_traces"
KEPT_TABLES = frozenset({"_migrations", "engine_schema_ready", KEEP_TABLE})
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_KEPT = f"(SELECT trace_id FROM `{KEEP_TABLE}`)"
#: Tables filtered by trace instead of emptied: table -> its trace-id column.
_BY_TRACE = {"spans": "trace_id", "traces_replacing": "id", "llm_messages": "trace_id"}
_CONTENT_TABLE = "deduped_content"
#: Every content hash a span refers to (its input and output messages and its tool definitions).
_REFERENCED = ("(SELECT arrayJoin(arrayConcat(input_message_hashes, output_message_hashes, "
               "[tool_definitions_hash])) FROM spans)")


def has_own_store() -> bool:
    return settings.enabled() and bool((os.getenv("LMNR_CLICKHOUSE_URL") or "").strip())


def _post(cfg: dict, sql: str, *, timeout: float = 30.0, body: Optional[bytes] = None,
          **params: str) -> httpx.Response:
    """Run one statement. With `body`, the statement travels in the URL and
    `body` is its data (an INSERT)."""
    query = {"database": cfg["database"], **params}
    if body is not None:
        query["query"] = sql
    resp = httpx.post(cfg["url"], params=query, content=sql.encode("utf-8") if body is None else body,
                      auth=(cfg["user"], cfg["password"]), timeout=timeout)
    resp.raise_for_status()
    return resp


def _tables(cfg: dict) -> List[str]:
    listed = _post(cfg, "SELECT name FROM system.tables WHERE database = {db:String} "
                        "AND engine LIKE '%MergeTree' ORDER BY name FORMAT TSV",
                   param_db=cfg["database"]).text
    return [name for name in (line.strip() for line in listed.splitlines())
            if name and name not in KEPT_TABLES and _NAME.match(name)]


def _count(cfg: dict, sql: str) -> int:
    return int((_post(cfg, sql + " FORMAT TSV").text or "0").strip() or 0)


def counts() -> Optional[Dict[str, int]]:
    """Rows in the three tables the portal reads (None when there is no store
    or it does not answer). For the reset's log line and for a rehearsal."""
    if not has_own_store():
        return None
    from core.lmnr_clickhouse import _config

    cfg = _config()
    try:
        return {"spans": _count(cfg, "SELECT count() FROM spans"),
                "traces": _count(cfg, "SELECT count() FROM traces_replacing FINAL"),
                "content": _count(cfg, f"SELECT count() FROM `{_CONTENT_TABLE}` FINAL")}
    except Exception:  # noqa: BLE001
        return None


def trace_index() -> Optional[List[Dict[str, Any]]]:
    """One row for every trace that names a conversation: ``trace_id``,
    ``session_id``, ``marked`` (1 when a span of it carries the sample
    generator's mark) and ``started`` (epoch seconds). None when there is no
    store. Raises when the store does not answer (the caller keeps nothing)."""
    if not has_own_store():
        return None
    from core.lmnr_clickhouse import _config
    from demo_mode.retention import MARK_ATTR

    cfg = _config()
    # JSONExtractString reads the attribute as a top-level key of the span's
    # own attributes (a real JSON parse): text inside a value cannot pass for it.
    sql = (
        "SELECT toString(trace_id) AS trace_id, "
        "maxIf(JSONExtractString(attributes, 'waj.session_id'), "
        "JSONExtractString(attributes, 'waj.session_id') != '') AS session_id, "
        "toUInt8(max(JSONExtractString(attributes, {mark:String}) != '')) AS marked, "
        "toUnixTimestamp(min(start_time)) AS started "
        "FROM spans WHERE position(attributes, 'waj.session_id') > 0 "
        "GROUP BY trace_id FORMAT JSON")
    return list(_post(cfg, sql, timeout=120.0, param_mark=MARK_ATTR).json().get("data") or [])


def _keep_only(cfg: dict, keep: Collection[str]) -> Dict[str, Any]:
    """Delete everything but the traces in `keep`. Raises unless the store
    confirms afterwards that nothing else is left."""
    ids = sorted({i.lower() for i in keep})
    if not ids or not all(_UUID.match(i) for i in ids):
        raise ValueError("no usable trace ids")
    _post(cfg, f"CREATE TABLE IF NOT EXISTS `{KEEP_TABLE}` (trace_id UUID) "
               "ENGINE = MergeTree ORDER BY trace_id")
    _post(cfg, f"TRUNCATE TABLE `{KEEP_TABLE}`")
    _post(cfg, f"INSERT INTO `{KEEP_TABLE}` FORMAT TSV", body=("\n".join(ids) + "\n").encode("ascii"))
    if _count(cfg, f"SELECT count() FROM `{KEEP_TABLE}`") != len(ids):
        raise RuntimeError("the list of kept traces was not stored whole")
    tables = _tables(cfg)
    if not set(_BY_TRACE) - {"llm_messages"} <= set(tables) or _CONTENT_TABLE not in tables:
        raise RuntimeError("not the schema this was written for")
    emptied: List[str] = []
    for name in tables:
        if name in _BY_TRACE:
            _post(cfg, f"ALTER TABLE `{name}` DELETE WHERE `{_BY_TRACE[name]}` NOT IN {_KEPT}",
                  timeout=600.0, mutations_sync="2")
        elif name != _CONTENT_TABLE:
            _post(cfg, f"TRUNCATE TABLE IF EXISTS `{name}`")
            emptied.append(name)
    # Message text last: what no remaining span refers to.
    _post(cfg, f"ALTER TABLE `{_CONTENT_TABLE}` DELETE WHERE content_hash NOT IN {_REFERENCED}",
          timeout=600.0, mutations_sync="2")
    left = {
        "spans": _count(cfg, f"SELECT count() FROM spans WHERE trace_id NOT IN {_KEPT}"),
        "traces": _count(cfg, f"SELECT count() FROM traces_replacing FINAL WHERE id NOT IN {_KEPT}"),
        "content": _count(cfg, f"SELECT count() FROM `{_CONTENT_TABLE}` FINAL "
                               f"WHERE content_hash NOT IN {_REFERENCED}"),
        "mutations": _count(cfg, "SELECT count() FROM system.mutations WHERE database = currentDatabase() "
                                 "AND NOT is_done"),
    }
    if any(left.values()):
        raise RuntimeError(f"rows outside the kept traces are left: {left}")
    return {"emptied": emptied, "filtered": sorted([*_BY_TRACE, _CONTENT_TABLE]), "kept_traces": len(ids)}


def wipe(keep: Collection[str] = ()) -> Optional[Dict[str, Any]]:
    """Empty the demo's trace store, except the traces in `keep` (the sample
    conversations the reset keeps: demo_mode/retention.py).

    Returns None when this install has no store of its own, else
    ``{"emptied": [tables], "kept_traces": n, "before": counts, "after": counts}``.
    ``kept_traces`` is 0 whenever everything was emptied: nothing was asked to
    be kept, or keeping failed and the store was emptied whole instead. Never
    raises: a store that is down is logged and the reset goes on."""
    if not has_own_store():
        return None
    from core.lmnr_clickhouse import _config

    cfg = _config()
    out: Dict[str, Any] = {"emptied": [], "kept_traces": 0, "before": counts()}
    if keep:
        try:
            out.update(_keep_only(cfg, keep))
        except Exception as exc:  # noqa: BLE001 — then nothing is kept
            logger.warning("demo reset: sample traces not kept (%s: %s); emptying the trace store",
                           type(exc).__name__, str(exc)[:200])
            out["kept_traces"] = 0
    if not out["kept_traces"]:
        emptied: List[str] = []
        try:
            for name in _tables(cfg):
                _post(cfg, f"TRUNCATE TABLE IF EXISTS `{name}`")
                emptied.append(name)
        except Exception as exc:  # noqa: BLE001 — the reset never fails on the trace store
            logger.warning("demo reset: trace store not emptied (%s); emptied so far: %d tables",
                           type(exc).__name__, len(emptied))
            out["emptied"] = emptied
            return out
        out["emptied"] = emptied
        keep = ()
    _wipe_trace_index(keep if out["kept_traces"] else ())
    out["after"] = counts()
    logger.info("demo reset: trace store: kept %d sample trace(s), emptied %d table(s); rows before %s, after %s",
                out["kept_traces"], len(out["emptied"]), out["before"], out.get("after"))
    return out


def _wipe_trace_index(keep: Collection[str] = ()) -> None:
    """Laminar also keeps one row per trace in its own database (ids, token
    counts, the trace's tags, the first and last text of its top span).
    Emptied too, except the kept traces' rows, where this install knows that
    database (``TRACING_DATABASE_URL``, written by the compose setup step).
    Never raises."""
    dsn = (os.getenv("TRACING_DATABASE_URL") or "").strip()
    if not dsn:
        return
    try:
        import psycopg2

        conn = psycopg2.connect(dsn, connect_timeout=5)
        try:
            with conn, conn.cursor() as cur:
                if keep:
                    cur.execute("DELETE FROM traces WHERE NOT (id = ANY(%s::uuid[]))", (sorted(keep),))
                else:
                    cur.execute("TRUNCATE TABLE traces")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("demo reset: trace index not emptied (%s)", type(exc).__name__)
