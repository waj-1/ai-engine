"""ClickHouse fakes for the observability tests.

`admin/screens/observability.py` reads ClickHouse and nothing else, so every
test that used to hand it a fake Laminar-frontend client now hands it a fake
`core.lmnr_clickhouse.query`. All of those queries funnel through the single
`observability._ch_trace_query` indirection, which is the seam these helpers
patch.

The engine issues several *different* queries per screen, so a fake has to
dispatch on the SQL rather than return one canned payload. `make_ch_query`
routes by distinctive fragments of each statement; anything unmatched returns
`[]` (the honest "query ran, matched nothing" answer) so a new query added to
production code shows up as an empty tile rather than a confusing crash.

Row shapes mirror what ClickHouse actually returns over the HTTP JSON
interface, including its habit of **quoting 64-bit integers** — token counts
arrive as strings. `ch_trace_row` keeps that quirk so the mappers' coercion
stays honest.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Row builders
# --------------------------------------------------------------------------- #
def ch_trace_row(
    trace_id: str,
    *,
    agent_kind: Optional[str] = None,
    channel_slug: Optional[str] = None,
    session_id: Optional[str] = None,
    env: Optional[str] = None,
    simulated: bool = False,
    has_error: int = 0,
    in_tokens: int = 10,
    out_tokens: int = 5,
    duration_sec: float = 1.0,
    top_span_name: str = "sse_response",
    start: str = "2026-05-20 10:00:00.000000000",
    end: str = "2026-05-20 10:00:01.000000000",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A `traces_replacing FINAL` row as `_trace_list_sql` selects it.

    agent_kind / channel / session / env ride on the trace **metadata** JSON —
    stamped there by `core/observability.py:root_span` via
    `Laminar.set_trace_metadata` — exactly as `_annotate_trace_channel_meta`
    reads them. A legacy (pre-tagging) trace passes none of them and gets `""`.

    `has_error` mirrors the events-aggregated column the trace queries join
    from `spans` (a trace is an error iff any span carries an
    exception/error event). There is deliberately NO `status` field: the real
    `traces_replacing.status` column is `''` on every row and production no
    longer selects it — a fake row carrying a usable `status` would let a test
    pass against a signal that does not exist on the live stack.
    """
    meta: Dict[str, Any] = {}
    if agent_kind:
        meta["waj.agent_kind"] = agent_kind
    if channel_slug:
        meta["waj.channel.slug"] = channel_slug
    if session_id:
        meta["waj.session_id"] = session_id
    if env:
        meta["waj.env"] = env
    if simulated:
        meta["waj.voice.simulated"] = "true"
    if extra_meta:
        meta.update(extra_meta)
    return {
        "id": trace_id,
        "startTime": start,
        "endTime": end,
        # ClickHouse's JSON format quotes Int64 — keep it a string on purpose.
        "inputTokens": str(in_tokens),
        "outputTokens": str(out_tokens),
        "durationSec": duration_sec,
        "has_error": has_error,
        "topSpanName": top_span_name,
        "metadata": json.dumps(meta) if meta else "",
        "sessionId": "",
    }


def ch_span_row(
    span_id: str,
    *,
    name: str,
    span_type: int = 0,
    parent_span_id: str = "00000000-0000-0000-0000-000000000000",
    trace_id: str = "11111111-1111-1111-1111-111111111111",
    start: str = "2026-05-20 10:00:00.000000000",
    end: str = "2026-05-20 10:00:01.000000000",
    duration: float = 1.0,
    in_tokens: int = 0,
    out_tokens: int = 0,
    total_cost: float = 0.0,
    attributes: Optional[Dict[str, Any]] = None,
    events: Optional[List[Any]] = None,
    has_error: int = 0,
) -> Dict[str, Any]:
    """A `spans` row as `_SPAN_LIST_SQL` selects it.

    `attributes` is a JSON **string** on the wire (the column's real type), and
    `events` a positional array per tuple — both as ClickHouse hands them over.
    """
    return {
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "traceId": trace_id,
        "name": name,
        "spanTypeCode": span_type,
        "startTime": start,
        "endTime": end,
        "duration": duration,
        "inputTokens": str(in_tokens),
        "outputTokens": str(out_tokens),
        "totalCost": total_cost,
        "attributes": json.dumps(attributes or {}),
        "events": events or [],
        "has_error": has_error,
    }


def ch_content_rows(span_id: str, *, inputs=(), outputs=()) -> List[Dict[str, Any]]:
    """`deduped_content`-join rows for one span, in message order.

    Each entry may be a dict (serialised as a `{role, content}` message, which
    is what the store actually holds) or a bare string.
    """
    rows: List[Dict[str, Any]] = []
    for side, items in (("input", inputs), ("output", outputs)):
        for pos, item in enumerate(items):
            rows.append({
                "span_id": span_id,
                "side": side,
                "pos": pos,
                "content": item if isinstance(item, str)
                else json.dumps(item, ensure_ascii=False),
            })
    return rows


# --------------------------------------------------------------------------- #
# Query dispatcher
# --------------------------------------------------------------------------- #
def make_ch_query(
    *,
    traces: Optional[List[Dict[str, Any]]] = None,
    spans: Optional[List[Dict[str, Any]]] = None,
    content: Optional[List[Dict[str, Any]]] = None,
    trace_row: Optional[Dict[str, Any]] = None,
    sessions: Optional[int] = None,
    ttft: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_matches: Optional[List[str]] = None,
    span_io: Optional[List[Dict[str, Any]]] = None,
    calls: Optional[List[Dict[str, Any]]] = None,
    session_tools: Optional[List[Dict[str, Any]]] = None,
    raises: bool = False,
):
    """Build a stand-in for `core.lmnr_clickhouse.query`.

    Pass only the payloads a test cares about; every other statement answers
    `[]`. When `calls` is given, each `{sql, params}` is appended to it so a test
    can assert what was pushed down. `raises=True` makes every query fail — the
    "ClickHouse is down" case.
    """
    def _query(sql: str, params: Optional[Dict[str, Any]] = None, **_kw):
        if calls is not None:
            calls.append({"sql": sql, "params": params or {}})
        if raises:
            raise RuntimeError("clickhouse down")
        # Order matters: the more specific fragments are tested first.
        if "deduped_content" in sql:
            return list(content or [])
        if "uniqExact" in sql:
            return [{"sessions": sessions}] if sessions is not None else []
        if "waj.ttft_ms" in sql:
            return [ttft] if ttft else []
        # Conversation-wide tool calls (transcript) — before the generic
        # tool-rollup match, which its `LIKE '%.tool'` would also hit.
        if "waj.session_id') = {sid:String}" in sql:
            return list(session_tools or [])
        # The trace list also counts tool spans (outcome column), so it must
        # be recognised before the `LIKE '%.tool'` rollup branch.
        if "AS topSpanName" in sql and "FROM (SELECT * FROM traces_replacing FINAL" in sql:
            return list(traces or [])
        if "SELECT DISTINCT toString(trace_id)" in sql:
            return [{"trace_id": t} for t in (tool_matches or [])]
        if "LIKE '%.tool'" in sql:
            return list(tools or [])
        if "WHERE id = {trace_id:String}" in sql:
            return [trace_row] if trace_row else []
        if "toInt16(span_type) AS spanTypeCode" in sql:
            return list(spans or [])
        if "toInt16(span_type) AS span_type" in sql:
            return list(span_io or [])
        if "FROM traces_replacing FINAL" in sql:
            return list(traces or [])
        return []

    return _query
