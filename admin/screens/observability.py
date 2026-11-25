"""Helpers backing the /admin/observability screens.

**Every screen here reads ClickHouse and nothing else** (`core/lmnr_clickhouse.py`,
read-only SQL over `traces_replacing FINAL` + `spans`). There is no HTTP client
for a dashboard/web UI any more: the deployed self-hosted build serves no
NextAuth routes (`/api/auth/csrf` → 404), so the session-cookie client this
module used to depend on could not authenticate at all and every screen it
backed was dead in production.

Two ClickHouse facts drive the shape of the code below; both were measured on
the live stack and both are load-bearing:

* **LLM span I/O is not in `spans.input` / `spans.output`.** Those columns are
  empty for 100% of `span_type = 1` (LLM) spans; the content lives in a
  content-addressed store and is recovered by array-joining
  `input_message_hashes` / `output_message_hashes` against `deduped_content`
  (see ``_SPAN_CONTENT_SQL``). Tool / DEFAULT spans DO keep inline I/O.
  `deduped_content` is the live table — `llm_messages`, `deduped_content_dict`
  and `llm_messages_dict` are all empty, don't build against them.
* **`spans.status` is useless as an error signal** (`''` on ~27,687 of 27,694
  rows). Errors are derived from the OTel `events` array instead —
  ``arrayExists(x -> x.2 IN ('exception','error'), events)``.
  **`traces_replacing.status` is just as dead** (`''` on every row the live
  stack has ever written), so a trace's error state is the same predicate
  AGGREGATED over its spans: a trace is an error iff ANY of its spans carries
  an exception/error event. Both the trace list and the single-trace lookup
  join that aggregate in SQL (so the Error/Success list filter works
  server-side); nothing on the trace path reads the `status` column any more.

Span ids are OTel 8-byte values zero-left-padded into UUIDs
(`00000000-0000-0000-afc1-b3f70b5cfd5c`); only the last 16 hex chars carry
information, so never truncate one from the left.

Every loader is best-effort: a ClickHouse failure degrades to an
empty-but-shaped result plus a `notice`, never a 500.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Env-aware fallback client for the Supabase side-reads below (chat
# transcript / session / recordings / tool names): with no explicit ``env``
# these resolve the request-scoped switcher env instead of always the local
# branch. Module-level so the admin test conftest's hermetic shim can patch it.
from core.auth import admin_service_client


# The duplex-voice realtime providers a call recording names (voice/live/
# factory.py slugs), in the words the Call audio tab shows.
_VOICE_PROVIDER_LABELS = {
    "google_genai": "Gemini Live",
    "azure_voice_live": "Azure Voice Live",
    "azure_openai": "Azure OpenAI Realtime",
}
logger = logging.getLogger(__name__)

# Shown when ClickHouse failed and no classified failure was recorded (e.g. a
# stubbed query in tests). The observability screens have no second source to
# fall back to, so the notice is the whole failure story — `_ch_notice()` names
# the actual cause whenever the client recorded one.
_CH_NOTICE = (
    "Telemetry unavailable — the observability ClickHouse could not be reached. "
    "Check LMNR_CLICKHOUSE_URL / credentials (see Telemetry diagnose)."
)

# Cap on the ClickHouse error text echoed into the banner.
_CH_ERROR_MAX = 300


def _ch_cause() -> Optional[str]:
    """One sentence naming why ClickHouse failed, from the client's most recent
    recorded failure — or None when nothing was recorded.

    A ClickHouse that is up and rejecting a query must never read as "could
    not be reached": that banner sent the 2026-08-26 → 09-29 trace-list bug
    (Code: 10 on every load) to the network and credentials for a month."""
    from core.lmnr_clickhouse import last_failure

    failure = last_failure()
    if failure is None:
        return None
    detail = failure.message
    if len(detail) > _CH_ERROR_MAX:
        detail = detail[:_CH_ERROR_MAX] + "…"
    if failure.kind == "unreachable":
        return (
            "the observability ClickHouse could not be reached (%s). "
            "Check LMNR_CLICKHOUSE_URL and that the lmnr-server VM is up." % detail
        )
    if failure.kind == "auth":
        return (
            "the observability ClickHouse refused the login (%s). "
            "Check LMNR_CLICKHOUSE_USER / LMNR_CLICKHOUSE_PASSWORD." % detail
        )
    return (
        "the observability ClickHouse is reachable but rejected the query — "
        "this is a query bug, not connectivity: %s" % detail
    )


def _ch_notice() -> str:
    """Banner for a page whose ClickHouse reads all failed (see `_ch_cause`)."""
    cause = _ch_cause()
    if cause is None:
        return _CH_NOTICE
    return "Telemetry unavailable — %s (see Telemetry diagnose)." % cause

# --- Partial failure --------------------------------------------------------
#
# The Dashboard runs FIVE independent ClickHouse reads and `lmnr_ok` is an OR
# across them — deliberately, so one slow query can't blank five tiles. The
# cost of that OR is that a PARTIAL failure used to render as an affirmative
# all-clear: the heavy 1000-row trace list times out, the cheap TTFT aggregate
# succeeds, and the page shows a green "No error traces in this window." card
# and a $0 "Token spend 24h" tile beside live-looking numbers, with no notice.
# During exactly the incident the Dashboard exists for, the operator reads
# confident wrong numbers.
#
# So every loader reports WHICH reads answered (`insights["sources"]`), a tile
# whose source failed renders as a dash rather than 0/an all-clear, and the
# notice below names the stale parts.
_LIVE_SOURCE_LABELS: Dict[str, str] = {
    "traces_recent": "recent traces (error list, latency, active agents)",
    "traces_24h": "24h traces (token spend)",
    "sessions": "session count",
    "ttft": "TTFT / TTLT",
    "top_tools": "tool-call rollup",
}


def _partial_ch_notice(failed: List[str]) -> str:
    """Notice for a PARTIAL ClickHouse outage, naming the stale sources.

    Distinct from ``_CH_NOTICE`` ("nothing answered"): here some numbers on the
    page are live and some are missing, and the operator has to be told which
    is which — an unqualified "telemetry unavailable" beside populated tiles
    reads as noise and gets dismissed."""
    names = ", ".join(_LIVE_SOURCE_LABELS.get(k, k) for k in failed)
    cause = _ch_cause()
    return (
        "Telemetry PARTIALLY unavailable — these reads failed, so the tiles "
        "they feed show — rather than a value, and lists they feed are "
        "unavailable rather than empty: %s. Everything else on this page comes "
        "from the reads that did answer. %s"
        % (
            names,
            ("Most recent failure: %s." % cause) if cause
            else "Check LMNR_CLICKHOUSE_URL / credentials (see Telemetry diagnose).",
        )
    )

# `parent_span_id` is this sentinel on a root span, and ClickHouse renders an
# unset DateTime64 as the epoch — both mean "absent", not "zero".
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# Span attribute keys the templates look at to render LLM I/O. Laminar tags
# these on auto-instrumented spans (OpenLLMetry conventions). Listed in
# precedence order so the first hit wins.
_INPUT_KEYS = (
    "lmnr.span.input",
    "traceloop.entity.input",
    "gen_ai.prompt",
)
_OUTPUT_KEYS = (
    "lmnr.span.output",
    "traceloop.entity.output",
    "gen_ai.completion",
)

# Tool-span attribute keys. OpenLLMetry's LangChain instrumentor tags tool
# spans with `traceloop.span.kind == "tool"` plus a mix of these keys
# (varies by SDK version). Listed in precedence order — first hit wins.
_TOOL_NAME_KEYS = (
    "gen_ai.tool.name",
    "lmnr.span.tool_name",
    "traceloop.entity.name",
    "langchain.task.name",
    "tool.name",
)
_TOOL_INPUT_KEYS = (
    "gen_ai.tool.input",
    "gen_ai.tool.parameters",
    "lmnr.span.input",
    "traceloop.entity.input",
    "input.value",
    "tool.input",
)
_TOOL_OUTPUT_KEYS = (
    "gen_ai.tool.output",
    "lmnr.span.output",
    "traceloop.entity.output",
    "output.value",
    "tool.output",
)
_TOOL_ERROR_KEYS = (
    "gen_ai.tool.error",
    "exception.message",
    "error.message",
)
_TOOL_KIND_KEYS = (
    "traceloop.span.kind",
    "lmnr.span.type",
    "openinference.span.kind",
)


# ---------------------------------------------------------------------------
# Agent-kind palette — single source of truth for the colored trace cards in
# the explorer's left pane. The template defines the matching `.agent-<kind>`
# CSS classes; this dict drives the legend + lets the live monitor / tests
# resolve a color without re-parsing CSS.
# ---------------------------------------------------------------------------
from admin.agent_kinds import catalog_dict as _catalog_dict, catalog_tuple as _catalog_tuple  # noqa: E402

# A plugin colours its own agents (`agent_catalog.kind_colors()`); every other
# kind gets the neutral fallback.
AGENT_PALETTE: Dict[str, str] = _catalog_dict("kind_colors")
_AGENT_FALLBACK_COLOR = "#94a3b8"  # neutral slate for legacy / unknown kinds


def agent_color(agent_kind: Optional[str]) -> str:
    """Return the chip / left-border color for an agent_kind.

    Falls back to a neutral slate when the kind is unknown or absent
    (legacy traces from before root-span agent_kind tagging landed)."""
    if not agent_kind:
        return _AGENT_FALLBACK_COLOR
    return AGENT_PALETTE.get(agent_kind, _AGENT_FALLBACK_COLOR)


def _coerce_text(value: Any) -> Optional[str]:
    """Render an attribute value into something safe to show in the UI."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _extract_io(attrs: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Return {'input': ..., 'output': ...} pulled from span attributes.

    Handles the OpenLLMetry pattern where prompts are spread across
    `gen_ai.prompt.0.content`, `gen_ai.prompt.1.content`, etc. — collapses
    them into a single string with role prefixes.
    """
    if not isinstance(attrs, dict):
        return {"input": None, "output": None}

    def _direct(keys: tuple) -> Optional[str]:
        for k in keys:
            if k in attrs:
                return _coerce_text(attrs[k])
        return None

    def _prefixed(prefix: str) -> Optional[str]:
        parts: List[str] = []
        # gen_ai uses dotted index keys; sort by the integer index.
        items = [
            (k, v) for k, v in attrs.items()
            if k.startswith(f"{prefix}.") and k.endswith(".content")
        ]
        items.sort(key=lambda kv: kv[0])
        for k, v in items:
            role_key = k.replace(".content", ".role")
            role = attrs.get(role_key) or "?"
            parts.append(f"{role}: {_coerce_text(v) or ''}")
        return "\n\n".join(parts) if parts else None

    return {
        "input": _direct(_INPUT_KEYS) or _prefixed("gen_ai.prompt"),
        "output": _direct(_OUTPUT_KEYS) or _prefixed("gen_ai.completion"),
    }


def _is_tool_span(span: Dict[str, Any], attrs: Dict[str, Any]) -> bool:
    """Heuristic: is this span a tool invocation?

    Looks at the explicit Laminar span type first, then the OpenLLMetry
    `traceloop.span.kind` attribute (which the LangChain instrumentor
    emits as 'tool' for `@tool`-decorated callables). Falls back to
    LangChain's task-name attribute pattern.
    """
    if (span.get("spanType") or "").upper() == "TOOL":
        return True
    # The agent emits each tool invocation as a DEFAULT span named
    # "<tool>.tool" (this is what the ClickHouse tool-volume query keys off).
    # Such a span can arrive without any of the traceloop/langchain attributes
    # below, so match the name suffix directly — otherwise the Live Monitor
    # "Tool-call success %" KPI counts 0 calls and shows "—".
    if (span.get("name") or "").endswith(".tool"):
        return True
    if not isinstance(attrs, dict):
        return False
    for key in _TOOL_KIND_KEYS:
        val = attrs.get(key)
        if isinstance(val, str) and val.strip().lower() in ("tool", "tool_call"):
            return True
    # LangChain task names like 'tool.<name>' or 'ChatPromptTemplate'.
    task = attrs.get("langchain.task.name")
    if isinstance(task, str) and task.lower().startswith("tool"):
        return True
    return False


def _extract_tool_io(span: Dict[str, Any], attrs: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Return {'name': ..., 'input': ..., 'output': ..., 'error': ...}.

    Returns Nones-only dict when the span isn't a tool span or no
    tool I/O attributes were captured. Templates check for the presence
    of `name` to decide whether to render the tool-IO block.

    NOTE on attribute key drift: the OpenLLMetry instrumentor for
    LangChain has used different key conventions across versions —
    sometimes `gen_ai.tool.input`/`output`, sometimes `input.value`/
    `output.value`, sometimes only the generic `lmnr.span.input`/
    `output` Laminar-level wrappers. We probe every known key
    defensively. If none of these populate for tool calls in practice,
    the fix is upstream in `core/_laminar_bootstrap.py` — set
    `capture_content=True` on the LangChain instrumentor (some
    instrumentors default to omitting payloads to reduce span size).
    """
    if not isinstance(attrs, dict):
        return {"name": None, "input": None, "output": None, "error": None}

    def _direct(keys: tuple) -> Optional[str]:
        for k in keys:
            if k in attrs:
                return _coerce_text(attrs[k])
        return None

    name = _direct(_TOOL_NAME_KEYS) or (span.get("name") if _is_tool_span(span, attrs) else None)
    if not name and not _is_tool_span(span, attrs):
        return {"name": None, "input": None, "output": None, "error": None}

    return {
        "name": name,
        "input": _direct(_TOOL_INPUT_KEYS),
        "output": _direct(_TOOL_OUTPUT_KEYS),
        "error": _direct(_TOOL_ERROR_KEYS),
    }


# ---------------------------------------------------------------------------
# Span I/O content (prompts / completions / tool payloads).
#
# Tool / DEFAULT spans keep their payload inline in `spans.input` / `spans.output`.
# LLM spans (`span_type = 1`) DO NOT: those columns are empty for every one of
# them, and the messages live in the content-addressed `deduped_content` table
# keyed by the per-span `input_message_hashes` / `output_message_hashes` arrays.
# `_clickhouse_spans` below reads the inline columns and then BACKFILLS the LLM
# ones from that store, so every consumer (transcript, span detail, overview)
# sees populated I/O regardless of which kind of span it is looking at.
# ---------------------------------------------------------------------------

_SPAN_IO_SQL = """
SELECT toString(span_id) AS span_id,
       name,
       toInt16(span_type) AS span_type,
       toString(start_time) AS start_time,
       toString(end_time) AS end_time,
       toInt64(input_tokens) AS input_tokens,
       toInt64(output_tokens) AS output_tokens,
       toUInt8(position(attributes, 'traceloop.association.properties.ls_model_type') > 0) AS wrapper,
       input,
       output,
       simpleJSONExtractUInt(attributes, 'gen_ai.usage.cache_read_input_tokens') AS cache_read_tokens,
       simpleJSONExtractUInt(attributes, 'gen_ai.usage.cache_creation_input_tokens') AS cache_creation_tokens,
       toUInt8(arrayExists(x -> x.2 IN ('exception', 'error'), events)) AS has_error,
       simpleJSONExtractString(attributes, 'waj.session_id') AS waj_session_id,
       simpleJSONExtractString(attributes, 'waj.env') AS waj_env
FROM spans
WHERE trace_id = {trace_id:String}
ORDER BY start_time ASC
"""


# One row per (span, side, position) of message content, resolved through the
# content-addressed store. The ARRAY JOIN is paired with `arrayEnumerate` so the
# original message ORDER survives (system → user → assistant matters); the outer
# LEFT JOIN then resolves each hash to its text. Verified against the live stack:
# 12,825/12,825 hash refs resolve, no misses.
_SPAN_CONTENT_SQL = """
SELECT toString(x.span_id) AS span_id,
       x.side AS side,
       toUInt32(x.pos) AS pos,
       d.content AS content
FROM (
    SELECT span_id, project_id, 'input' AS side, idx AS pos, h AS content_hash
    FROM spans
    ARRAY JOIN input_message_hashes AS h, arrayEnumerate(input_message_hashes) AS idx
    WHERE trace_id = {trace_id:String}
    UNION ALL
    SELECT span_id, project_id, 'output' AS side, idx AS pos, h AS content_hash
    FROM spans
    ARRAY JOIN output_message_hashes AS h, arrayEnumerate(output_message_hashes) AS idx
    WHERE trace_id = {trace_id:String}
) AS x
LEFT JOIN deduped_content AS d
       ON d.content_hash = x.content_hash AND d.project_id = x.project_id
ORDER BY span_id ASC, side ASC, pos ASC
"""

# Full span rows for one trace — the replacement for the retired dashboard
# `GET /traces/{id}/spans`. `status` is deliberately NOT selected (it is empty
# on ~all rows); the error signal is the OTel `events` array.
_SPAN_LIST_SQL = """
SELECT toString(span_id) AS spanId,
       toString(parent_span_id) AS parentSpanId,
       toString(trace_id) AS traceId,
       name AS name,
       toInt16(span_type) AS spanTypeCode,
       toString(start_time) AS startTime,
       toString(end_time) AS endTime,
       toFloat64(if(end_time > start_time,
                    dateDiff('microsecond', start_time, end_time), 0)) / 1000000 AS duration,
       toInt64(input_tokens) AS inputTokens,
       toInt64(output_tokens) AS outputTokens,
       toFloat64(total_cost) AS totalCost,
       attributes AS attributes,
       events AS events,
       toUInt8(arrayExists(x -> x.2 IN ('exception', 'error'), events)) AS has_error
FROM spans
WHERE trace_id = {trace_id:String}
ORDER BY start_time ASC
"""

# One trace's aggregate row — the replacement for the retired dashboard
# `GET /traces/{id}`. `status` is deliberately NOT selected (like
# `spans.status`, it is '' on every row); the trace-level error signal is the
# span predicate aggregated over the trace's spans, joined here so the mapped
# row carries `has_error` exactly like the list query's rows do.
_TRACE_ONE_SQL = """
SELECT toString(id) AS id,
       toString(start_time) AS startTime,
       toString(end_time) AS endTime,
       toInt64(err.input_tokens_once) AS inputTokens,
       toInt64(err.output_tokens_once) AS outputTokens,
       toFloat64(err.cost_once) AS totalCost,
       toUInt8(err.has_error) AS has_error,
       top_span_name AS topSpanName,
       metadata AS metadata,
       session_id AS sessionId
FROM traces_replacing FINAL
LEFT JOIN (
    SELECT trace_id,
           -- Each model call once (core/llm_calls.py), not Laminar's totals.
           __ONCE__,
           max(arrayExists(x -> x.2 IN ('exception', 'error'), events)) AS has_error
    FROM spans
    WHERE trace_id = {trace_id:String}
    GROUP BY trace_id
) AS err ON err.trace_id = traces_replacing.id
WHERE id = {trace_id:String}
LIMIT 1
"""
from core.llm_calls import once_columns as _once_columns  # noqa: E402

_TRACE_ONE_SQL = _TRACE_ONE_SQL.replace("__ONCE__", _once_columns().replace("\n", "\n           "))

# Laminar's SpanType enum → the uppercase labels `_is_tool_span` / `_is_llm_span`
# and the templates match on. Only 0 (DEFAULT) and 1 (LLM) are confirmed present
# on the live stack; the rest are kept so an unexpected code still reads sanely.
_SPAN_TYPE_LABELS = {
    0: "DEFAULT",
    1: "LLM",
    2: "PIPELINE",
    3: "EXECUTOR",
    4: "EVALUATOR",
    5: "EVALUATION",
    6: "TOOL",
    7: "HUMAN_EVALUATOR",
}


def _as_int(value: Any) -> int:
    """ClickHouse's JSON format quotes 64-bit ints, so a token count arrives as
    ``"1234"``. Coerce so arithmetic (and Jinja's ``+``) works on numbers."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso_ts(raw: Any) -> Optional[str]:
    """ClickHouse ``'YYYY-MM-DD hh:mm:ss.fffffffff'`` → ISO-8601 ``…Z``.

    The templates + `_enrich_span` parse timestamps with
    ``datetime.fromisoformat``, which rejects a 9-digit fraction, so the
    fraction is truncated to microseconds. An unset DateTime64 renders as the
    epoch — that means "absent" (e.g. a span with no end yet), so it returns
    ``None`` rather than a 1970 timestamp that would compute a negative duration.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s or s.startswith("1970-01-01"):
        return None
    if s.endswith("Z"):
        s = s[:-1]
    if "T" not in s:
        s = s.replace(" ", "T", 1)
    if "." in s:
        head, frac = s.split(".", 1)
        frac = "".join(ch for ch in frac if ch.isdigit())[:6]
        s = f"{head}.{frac}" if frac else head
    return s + "Z"


def _epoch_ns_iso(value: Any) -> Optional[str]:
    """Span-event timestamps are Int64 epoch NANOseconds; the detail pane shows
    them as an ISO string sliced to seconds."""
    try:
        ns = int(float(value))
    except (TypeError, ValueError):
        return None
    if ns <= 0:
        return None
    from datetime import datetime, timezone

    try:
        return (
            datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _parse_attributes(raw: Any) -> Dict[str, Any]:
    """`spans.attributes` is a JSON *string* (100% populated). Returns {} on
    anything unparseable so a malformed row can't break a whole trace."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_bulk_message_attr(key: str) -> bool:
    """Is this one of the per-message ``gen_ai.prompt.N.*`` /
    ``gen_ai.completion.N.*`` attributes? That family dominates an LLM span's
    attribute payload; `_extract_io` still reads it (it is the fallback when the
    content store has nothing), but the span-detail JSON drops it."""
    return key.startswith("gen_ai.prompt.") or key.startswith("gen_ai.completion.")


def _map_events(raw: Any) -> List[Dict[str, Any]]:
    """``Array(Tuple(timestamp Int64, name String, attributes String))`` →
    ``[{timestamp, name, attributes}]`` (what the detail pane's JS reads).
    ClickHouse's JSON format renders a tuple as a positional array."""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for ev in raw:
        if isinstance(ev, dict):
            ts, name = ev.get("timestamp"), ev.get("name")
            attrs = ev.get("attributes")
        elif isinstance(ev, (list, tuple)) and len(ev) >= 2:
            ts, name = ev[0], ev[1]
            attrs = ev[2] if len(ev) > 2 else None
        else:
            continue
        out.append({
            "timestamp": _epoch_ns_iso(ts) or "",
            "name": name or "",
            "attributes": attrs or "",
        })
    return out


def _messages_json(contents: List[Any]) -> str:
    """Reassemble ordered `deduped_content` rows into the JSON message array
    `_parse_messages` consumes. Each stored content is usually itself JSON (a
    ``{role, content}`` message); anything that isn't rides through as a plain
    string. Returns ``""`` when nothing resolved, so callers can treat it as
    "no content" exactly like an empty inline column."""
    parts: List[Any] = []
    for raw in contents:
        if raw is None:
            continue
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        if not text.strip():
            continue
        try:
            parts.append(json.loads(text))
        except Exception:  # noqa: BLE001 - not JSON: keep the raw text
            parts.append(text)
    if not parts:
        return ""
    return json.dumps(parts, ensure_ascii=False)


def _session_id_from_spans(spans: List[Dict[str, Any]]) -> Optional[str]:
    """The chat session id the agent tagged on its root span (``waj.session_id``).
    The trace aggregate row has no chat-session column of its own, so we carry it
    on the span and read it from ClickHouse."""
    for s in spans:
        sid = s.get("waj_session_id")
        if sid:
            return sid
    return None


def _env_from_spans(spans: List[Dict[str, Any]]) -> Optional[str]:
    """The runtime env the trace was recorded in (``waj.env`` root-span
    attribute). Needed because the explorer can browse traces from BOTH envs
    (one shared telemetry store) while the persisted conversation lives in one
    specific branch DB."""
    for s in spans:
        e = s.get("waj_env")
        if e:
            return e
    return None


def _span_content_map(trace_id: str) -> Dict[str, Dict[str, str]]:
    """``span_id -> {"input": <json array>, "output": <json array>}`` recovered
    from the content-addressed store. ``{}`` when ClickHouse is unavailable."""
    try:
        rows = _ch_trace_query(_SPAN_CONTENT_SQL, {"trace_id": trace_id}) or []
    except Exception as exc:  # noqa: BLE001 - degrade to "no content"
        logger.warning("observability: span content store unavailable (%s)", exc)
        return {}
    buckets: Dict[str, Dict[str, List[Any]]] = {}
    for row in rows:
        span_id = row.get("span_id")
        side = row.get("side")
        if not span_id or side not in ("input", "output"):
            continue
        buckets.setdefault(span_id, {"input": [], "output": []})[side].append(
            row.get("content")
        )
    return {
        span_id: {
            "input": _messages_json(sides["input"]),
            "output": _messages_json(sides["output"]),
        }
        for span_id, sides in buckets.items()
    }


def _clickhouse_spans(trace_id: str) -> List[Dict[str, Any]]:
    """Per-span input/output from ClickHouse, ordered by start time.

    LLM spans carry no inline I/O (see the module docstring), so anything left
    empty is backfilled from the content-addressed store. Returns [] when
    ClickHouse is unavailable.
    """
    try:
        from core.lmnr_clickhouse import ClickHouseUnavailable
        from core.lmnr_clickhouse import query as ch_query
    except Exception:
        return []
    try:
        rows = ch_query(_SPAN_IO_SQL, {"trace_id": trace_id}) or []
    except ClickHouseUnavailable as exc:
        logger.warning("observability: ClickHouse span I/O unavailable (%s)", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("observability: span I/O query failed (%s)", exc)
        return []
    if not rows:
        return []
    # Only pay for the content join when something actually lacks inline I/O —
    # which in practice is every LLM span in the trace.
    if any(not (r.get("input") or r.get("output")) for r in rows):
        content = _span_content_map(trace_id)
        if content:
            for row in rows:
                found = content.get(row.get("span_id") or "")
                if not found:
                    continue
                if not row.get("input"):
                    row["input"] = found.get("input") or ""
                if not row.get("output"):
                    row["output"] = found.get("output") or ""
    return rows


def _cache_tokens_once(spans: List[Dict[str, Any]]) -> Dict[str, int]:
    """Cache-read and cache-creation tokens of one trace's `_SPAN_IO_SQL`
    rows, each model call counted once (core/llm_calls.py: a call is exported
    as two LLM spans on some providers, and both carry the cache counts)."""
    from core.llm_calls import count_once

    once = count_once(s for s in spans if int(s.get("span_type") or 0) == 1)
    return {"read": once["cache_read_tokens"], "creation": once["cache_creation_tokens"]}


def _map_ch_span_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map a ``spans`` row onto the span shape every consumer here expects:
    camelCase keys, an uppercase ``spanType`` label, a parsed ``attributes``
    dict, ISO timestamps, ``duration`` in SECONDS, and an events-derived
    ``status`` (the column itself is empty on ~all rows)."""
    attrs = _parse_attributes(row.get("attributes"))
    parent = str(row.get("parentSpanId") or "")
    try:
        type_code = int(row.get("spanTypeCode") or 0)
    except (TypeError, ValueError):
        type_code = 0
    has_error = _as_int(row.get("has_error")) != 0
    duration = _as_float(row.get("duration"))
    return {
        "spanId": row.get("spanId"),
        # A root span's parent is the zero UUID, not a real span.
        "parentSpanId": None if parent in ("", _ZERO_UUID) else parent,
        "traceId": row.get("traceId"),
        "name": row.get("name"),
        "spanType": _SPAN_TYPE_LABELS.get(type_code, str(type_code)),
        "status": "error" if has_error else "success",
        "startTime": _iso_ts(row.get("startTime")),
        "endTime": _iso_ts(row.get("endTime")),
        "duration": duration or None,
        "model": (
            attrs.get("gen_ai.response.model")
            or attrs.get("gen_ai.request.model")
            or None
        ),
        "inputTokens": _as_int(row.get("inputTokens")),
        "outputTokens": _as_int(row.get("outputTokens")),
        "totalCost": _as_float(row.get("totalCost")),
        "events": _map_events(row.get("events")),
        "attributes": attrs,
        "path": attrs.get("lmnr.span.path"),
    }


def ch_list_spans_for_trace(trace_id: str) -> List[Dict[str, Any]]:
    """Flat span list for one trace, in start-time order. [] when ClickHouse is
    unavailable — callers surface their own notice.

    ``[]`` is AMBIGUOUS here: it means both "this trace has no spans" and "the
    store is unreachable". Any caller that turns an empty result into a
    user-facing claim ("Span not found") must use
    ``ch_list_spans_for_trace_result`` instead, or it reports a connectivity
    problem as data loss."""
    return ch_list_spans_for_trace_result(trace_id)[0]


def ch_list_spans_for_trace_result(
    trace_id: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """``(spans, ok)`` — the unambiguous variant of the above.

    ``ok`` is False only when the query could not run; ``([], True)`` is a real
    "the trace has no spans" answer."""
    try:
        rows = _ch_trace_query(_SPAN_LIST_SQL, {"trace_id": trace_id}) or []
    except Exception as exc:  # noqa: BLE001 - degrade, never 500 a pane
        logger.warning("observability: span list unavailable for %s (%s)", trace_id, exc)
        return [], False
    return [_map_ch_span_row(r) for r in rows], True


def ch_get_trace(trace_id: str) -> Optional[Dict[str, Any]]:
    """One trace's aggregate row, or ``None`` when it's missing / ClickHouse is
    down. Keys mirror what the templates and JSON endpoints already read.

    ``None`` is ambiguous (missing vs unreachable); anything that turns it
    into a user-facing claim uses ``ch_get_trace_result``."""
    return ch_get_trace_result(trace_id)[0]


def ch_get_trace_result(trace_id: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """``(trace, ok)`` — ``ok`` is False only when the query could not run, so
    ``(None, True)`` is a real "no such conversation" answer."""
    try:
        rows = _ch_trace_query(_TRACE_ONE_SQL, {"trace_id": trace_id}) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("observability: trace lookup failed for %s (%s)", trace_id, exc)
        return None, False
    if not rows:
        return None, True
    return _map_trace_one_row(rows[0], trace_id), True


def looks_like_trace_id(trace_id: Any) -> bool:
    """Trace ids are UUIDs. A malformed one cannot match anything — and
    ClickHouse REJECTS the comparison, which must not read as an outage, so
    a lookup that found nothing for a malformed id is "not found"."""
    try:
        uuid.UUID(str(trace_id))
        return True
    except (TypeError, ValueError):
        return False


def _map_trace_one_row(row: Dict[str, Any], trace_id: str) -> Dict[str, Any]:
    # Events-derived, never the dead `status` column (see the module
    # docstring) — reading that column mapped every trace to "success".
    has_error = _as_int(row.get("has_error")) != 0
    return {
        "id": row.get("id") or trace_id,
        "status": "error" if has_error else "success",
        "startTime": _iso_ts(row.get("startTime")),
        "endTime": _iso_ts(row.get("endTime")),
        "inputTokens": _as_int(row.get("inputTokens")),
        "outputTokens": _as_int(row.get("outputTokens")),
        "totalCost": _as_float(row.get("totalCost")),
        "sessionId": row.get("sessionId") or "",
        "topSpanName": row.get("topSpanName"),
        # No `top_span_type` column exists; `load_span_tree_json` fills this in
        # from the root span it already has.
        "topSpanType": None,
        "metadata": row.get("metadata") or "",
    }


def build_span_tree(spans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert a flat span list into a tree rooted at the top span.

    Pure function over ``spanId`` / ``parentSpanId`` / ``startTime``: each output
    span gains a ``children`` list and a ``depth`` int. Orphans (spans whose
    parent isn't in the list — possible with cross-process traces) render at the
    root. The live stack resolves 9,595/9,595 non-root parents within their own
    trace, so orphan handling is a safety net, not the common path.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for span in spans:
        span = dict(span)
        span["children"] = []
        span["depth"] = 0
        by_id[span["spanId"]] = span

    roots: List[Dict[str, Any]] = []
    for span in by_id.values():
        parent_id = span.get("parentSpanId") or _ZERO_UUID
        parent = by_id.get(parent_id)
        if parent and parent is not span:
            parent["children"].append(span)
        else:
            roots.append(span)

    # Stamp depth + sort siblings by start_time.
    def _walk(node: Dict[str, Any], depth: int) -> None:
        node["depth"] = depth
        node["children"].sort(key=lambda c: c.get("startTime") or "")
        for child in node["children"]:
            _walk(child, depth + 1)

    for root in roots:
        _walk(root, 0)
    roots.sort(key=lambda c: c.get("startTime") or "")
    return roots


def _pretty_json(raw: str) -> str:
    """Pretty-print a JSON string; return it unchanged when not JSON.

    Handles doubly-serialized JSON (a JSON string wrapping JSON) by
    repeatedly parsing until the value is stable (capped at 3 iterations).
    If the final decoded value is a plain string, return it as-is rather
    than JSON-quoted.
    """
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        for _ in range(2):  # up to 2 extra parse attempts (3 total)
            if not isinstance(obj, str):
                break
            stripped = obj.strip()
            if not stripped or stripped[0] not in ("{", "[", '"'):
                break
            try:
                obj = json.loads(obj)
            except Exception:
                break
        if isinstance(obj, str):
            return obj
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return raw


def _content_to_text(content: Any) -> str:
    """Flatten an LLM message ``content`` into display text.

    Handles the shapes we see in practice:
      * plain ``str``
      * LangChain blocks: ``[{"type":"text","text":...}, ...]``
      * Gemini native: ``{"parts":[{"text":...,"thought_signature":...}], "role":...}``
        (and lists thereof) — without this, the whole dict was ``json.dumps``'d,
        which is what surfaced raw JSON blobs in the Transcript tab.
    Falls back to pretty JSON only for genuinely unstructured payloads."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # Gemini: {"parts": [{"text": ...}], "role": ...}
        if isinstance(content.get("parts"), list):
            text = "\n".join(_content_to_text(p) for p in content["parts"]).strip()
            if text:
                return text
        # LangChain/Anthropic block: {"type":"text","text":...} or {"text":...}
        if content.get("text"):
            return str(content["text"])
        if "content" in content:
            return _content_to_text(content["content"])
        return json.dumps(content, ensure_ascii=False, indent=2)
    if isinstance(content, list):
        parts: List[str] = [_content_to_text(block) for block in content]
        return "\n".join(p for p in parts if p)
    try:
        return json.dumps(content, ensure_ascii=False, indent=2)
    except Exception:
        return str(content)


def _parse_messages(raw: str) -> List[Dict[str, str]]:
    """Parse a ClickHouse span input/output (a JSON message array) into a list
    of ``{role, text}``. Tolerates non-message JSON."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return [{"role": "", "text": raw}]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return [{"role": "", "text": _content_to_text(data)}]
    msgs: List[Dict[str, str]] = []
    for m in data:
        if isinstance(m, dict) and ("role" in m or "content" in m or "parts" in m):
            # Gemini puts the text under "parts"; LangChain under "content".
            text = _content_to_text(m.get("content") if "content" in m else m)
            msgs.append({"role": _normalize_role(m.get("role")), "text": text})
        else:
            msgs.append({"role": "", "text": _content_to_text(m)})
    return msgs


def _normalize_role(role: Any) -> str:
    """Map provider-specific roles to the canonical user/assistant/system/tool.

    Gemini uses ``model`` for the assistant and LangChain serialises
    ``human``/``ai``; the transcript classifies on ``user``/``assistant``."""
    r = str(role or "").strip().lower()
    return {"human": "user", "model": "assistant", "ai": "assistant"}.get(r, r)


def _ch_io_map(trace_id: str) -> Dict[str, Dict[str, Any]]:
    """``span_id -> {input, output}`` for backfilling I/O onto span-detail views.
    Empty when ClickHouse is down."""
    return {
        s["span_id"]: {"input": s.get("input") or "", "output": s.get("output") or "",
                       "cache_read_tokens": int(s.get("cache_read_tokens") or 0)}
        for s in _clickhouse_spans(trace_id)
        if s.get("span_id")
    }


def _enrich_span(span: Dict[str, Any], ch_io: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Add UI-friendly fields to a span: duration_ms, llm_io, tool_io,
    short_path. ``ch_io`` is the ``{input, output}`` payload for this span; when
    present it backfills the LLM / tool I/O that isn't carried on attributes."""
    out = dict(span)
    duration = span.get("duration")
    duration_ms = round(duration * 1000) if duration else None
    if duration_ms is None:
        # A span with no end time yet has no computable duration, which rendered
        # the row as "—". Fall back to (endTime - startTime) so every span shows
        # a wall-clock time. start/end are ISO-8601 strings.
        st, et = span.get("startTime"), span.get("endTime")
        if st and et:
            try:
                from datetime import datetime
                s_dt = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
                e_dt = datetime.fromisoformat(str(et).replace("Z", "+00:00"))
                delta_ms = int((e_dt - s_dt).total_seconds() * 1000)
                if delta_ms >= 0:
                    duration_ms = delta_ms
            except Exception:
                duration_ms = None
    out["duration_ms"] = duration_ms
    attrs = span.get("attributes") or {}
    out["llm_io"] = _extract_io(attrs)
    out["tool_io"] = _extract_tool_io(span, attrs)
    if ch_io:
        name = span.get("name") or ""
        ch_in = ch_io.get("input") or ""
        ch_out = ch_io.get("output") or ""
        if name.endswith(".tool") or (out["tool_io"] or {}).get("name"):
            tool_name = (out["tool_io"] or {}).get("name") or name[:-5]
            out["tool_io"] = {
                "name": tool_name,
                "input": _pretty_json(ch_in) or (out["tool_io"] or {}).get("input"),
                "output": _pretty_json(ch_out) or (out["tool_io"] or {}).get("output"),
                "error": (out["tool_io"] or {}).get("error"),
            }
        else:
            if ch_in and not out["llm_io"].get("input"):
                out["llm_io"]["input"] = "\n\n".join(
                    f"{m['role']}: {m['text']}".lstrip(": ")
                    for m in _parse_messages(ch_in) if m["text"]
                )
            if ch_out and not out["llm_io"].get("output"):
                out["llm_io"]["output"] = "\n".join(
                    m["text"] for m in _parse_messages(ch_out) if m["text"]
                )
    # `path` is a list/string from the lmnr.span.path attribute; the last
    # segment is the most specific.
    path = span.get("path")
    if isinstance(path, list):
        out["short_path"] = path[-1] if path else span.get("name")
    else:
        out["short_path"] = path or span.get("name")
    out["cache_read_tokens"] = int((ch_io or {}).get("cache_read_tokens") or 0)
    return out


def _item_env_matches(item: Dict[str, Any], env: Optional[str]) -> bool:
    """Whether a trace item belongs to the selected env.

    Read from the item's ``waj.env`` trace metadata. Mirrors the SQL in
    ``_filter_clauses`` so the Live Monitor's tiles scope the same way its
    charts do: prod is strict, staging ALSO admits untagged (legacy) traces,
    and ``None`` (no selection) admits everything. Without this the KPI tiles
    aggregated all envs at once and never changed when the operator switched
    STAGING/PRODUCTION.
    """
    if not env:
        return True
    item_env = _trace_meta_attrs(item).get("waj.env") or ""
    if env == "staging":
        return item_env in ("staging", "")
    return item_env == env


def _trace_meta_attrs(trace: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a trace's ``metadata`` into a flat attribute dict.

    ``traces_replacing.metadata`` is a JSON string (or ``""`` for traces that set
    none). Returns ``{}`` on anything unparseable.
    """
    raw = trace.get("metadata")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _annotate_trace_channel_meta(traces: List[Dict[str, Any]]) -> None:
    """Mutate traces in-place to add ``channel_slug`` + ``agent_kind`` keys.

    Read from each trace's **metadata** — set on the request root span in
    ``core/observability.py:root_span`` via ``Laminar.set_trace_metadata``, and
    carried on the ``traces_replacing`` row itself. Reading it from the trace
    aggregate keeps this a pure in-memory pass over the list we already have:
    no per-trace span query (the earlier approach was ~100 sequential calls for
    a 24h window). Traces from before root-span metadata tagging landed carry no
    metadata and resolve to ``None`` for both keys (rendered as the neutral
    "legacy" chip).
    """
    for t in traces:
        meta = _trace_meta_attrs(t)
        t["channel_slug"] = meta.get("waj.channel.slug") or None
        t["agent_kind"] = meta.get("waj.agent_kind") or None
        # Which agent of a shared kind (every generic agent is `custom`).
        t["agent_slug"] = meta.get("waj.agent_slug") or None
        # Chat session id for the "Group by session" list view. Same
        # metadata ride-along as the keys above (waj.session_id is set by
        # root_span_attrs); the trace row's own session_id is the fallback.
        t["session_id"] = meta.get("waj.session_id") or t.get("sessionId") or None
        # Bot-driven voice calls (eval/scenario runs) stamp this on their
        # spans; the list renders it as a "sim" chip so simulated sessions
        # never blend in with real callers. Absent on real sessions.
        t["simulated"] = str(meta.get("waj.voice.simulated") or "").lower() == "true"
        # A request made by the eval runner (X-WAJ-Eval-Run) or the runner's
        # own suite trace is test traffic too — same "Test" chip.
        t["eval_run_id"] = meta.get("waj.eval.run_id") or None
        if t["eval_run_id"] or str(t.get("topSpanName") or "").startswith(_EVAL_TOP_SPAN_PREFIX):
            t["simulated"] = True


def group_traces_by_session(traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bucket a recency-ordered trace list into conversation groups.

    Each group is one chat session (= one conversation; each trace is one
    turn). Groups keep the position of their NEWEST trace so the list stays
    recency-ordered. Traces without a session id (legacy, background work)
    become single-trace groups with ``session_id: None`` so nothing is
    hidden by the toggle.

    Aggregates per group: turn count, newest/oldest start time, summed
    duration + tokens, error flag, and the first agent_kind/channel seen —
    enough for the session header row without extra queries.
    """
    groups: List[Dict[str, Any]] = []
    by_sid: Dict[str, Dict[str, Any]] = {}
    for t in traces:
        sid = t.get("session_id")
        if not sid:
            groups.append({"session_id": None, "traces": [t]})
            continue
        g = by_sid.get(sid)
        if g is None:
            g = {"session_id": sid, "traces": []}
            by_sid[sid] = g
            groups.append(g)
        g["traces"].append(t)

    for g in groups:
        ts = g["traces"]
        g["turns"] = len(ts)
        g["agent_kind"] = next((t.get("agent_kind") for t in ts if t.get("agent_kind")), None)
        g["agent_slug"] = next((t.get("agent_slug") for t in ts if t.get("agent_slug")), None)
        g["channel_slug"] = next((t.get("channel_slug") for t in ts if t.get("channel_slug")), None)
        g["has_error"] = any(t.get("status") == "error" for t in ts)
        # Action counts (tool spans) for the partner-facing outcome column.
        g["actions"] = sum(int(t.get("tool_calls") or 0) for t in ts)
        g["failed_actions"] = sum(int(t.get("tool_errors") or 0) for t in ts)
        g["handoffs"] = sum(int(t.get("handoffs") or 0) for t in ts)
        g["simulated"] = any(t.get("simulated") for t in ts)
        g["slow_turns"] = sum(1 for t in ts if (t.get("duration_ms") or 0) > SLOW_TURN_SEC * 1000)
        # The list is newest-first, so the first trace is the latest turn.
        g["latest_start"] = ts[0].get("startTime")
        g["earliest_start"] = ts[-1].get("startTime")
        g["duration_ms_total"] = sum(t.get("duration_ms") or 0 for t in ts)
        g["tokens_total"] = sum(
            (t.get("inputTokens") or 0) + (t.get("outputTokens") or 0) for t in ts
        )
    return groups


# ---------------------------------------------------------------------------
# Source-side trace filtering (ClickHouse)
# ---------------------------------------------------------------------------
#
# A background agent can emit one trace per transaction, far outnumbering
# conversations. Fetching the newest N traces and filtering CLIENT-side fails:
# the newest page is all background work, so genuine chat sessions never
# appear on it. We read the per-trace aggregate table (`traces_replacing`) and
# exclude non-conversational kinds + scope by `waj.env` IN THE QUERY, so chat
# sessions surface regardless of the background flood.

# agent_kinds that are background / non-conversational (a plugin names its
# own, `agent_catalog.background_kinds()`): shown only when the operator opts
# in (?background=1), never in the default conversational view.
_BACKGROUND_AGENT_KINDS = _catalog_tuple("background_kinds")

# Top-span names of UNTAGGED background traces (no `waj.agent_kind`): work no
# endpoint opened a root span for, so it can never be a labelled conversation.
#   - bare LLM calls: background agents before root-span tagging, eval judges,
#     the conversation-insights job (gemini.generate_content[_stream],
#     ChatGoogleGenerativeAI.chat) and stray embeddings (openai.embeddings);
#   - the admin MCP server's own tool calls (ResponseStreamWriter), which on
#     2026-10-14 were 62 of the newest 100 rows of the prod list.
# Excluded by structural signature, only when untagged — a tagged trace with
# one of these top spans is still a conversation.
_BACKGROUND_TOP_SPANS = (
    "ChatGoogleGenerativeAI.chat",
    "gemini.generate_content",
    "gemini.generate_content_stream",
    "openai.embeddings",
    "ResponseStreamWriter",
    # The same bare calls on an OpenAI, Azure OpenAI or Anthropic model (the
    # LangChain wrapper's span and the provider SDK's own), which is what a
    # build with no Gemini key runs its judges and helpers on...
    "ChatOpenAI.chat",
    "AzureChatOpenAI.chat",
    "openai.chat",
    "ChatAnthropic.chat",
    "anthropic.chat",
    # ...and the public demo's Builder assistant (admin/demo_handlers.py): a
    # helper in the portal, not a conversation with one of the agents. One
    # trace a turn, its model calls inside (they used to run outside the
    # root span, as traces of their own: the bare-call names above).
    "demo.builder_assistant",
    # The conversation-insights sweep's classifier call (core/conversation_insights.py).
    "conversation.insights",
)

# Test traffic: the eval runner's own suite trace (`eval_suite::<suite>`, which
# holds the per-case `eval_scenario::*` spans) and every agent request it makes
# (stamped `waj.eval.run_id` from the X-WAJ-Eval-Run header). Background class:
# shown with ?background=1, and each case links straight to its trace from the
# test run page.
_EVAL_TOP_SPAN_PREFIX = "eval_suite::"

# What a trace that is not a customer conversation IS, named from what the
# engine itself stamped on it: the eval runner's headers (`waj.eval.run_id`)
# and the names the engine gives its own root spans. Used by the technical
# rollups (Overview > Technical details, the cost share), which count every
# trace: without it these all fell into one "Unlabelled agent / unknown" row.
# Order matters: the first match wins. Each entry is (key, label, exact top-span
# names, top-span prefixes). Keys carry the `activity:` prefix so they can never
# be mistaken for an agent kind or a `custom:<slug>` agent.
ACTIVITY_PREFIX = "activity:"
ACTIVITY_TEST_RUNS = ACTIVITY_PREFIX + "test_runs"
ACTIVITY_CLASSES: Tuple[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    # The eval runner's own trace: the suite span, its per-case spans and the
    # judge's model calls inside them (admin/evals.py).
    (ACTIVITY_PREFIX + "test_judge", "Test judge", (), (_EVAL_TOP_SPAN_PREFIX,)),
    (ACTIVITY_PREFIX + "builder_assistant", "Builder assistant", ("demo.builder_assistant",), ()),
    (ACTIVITY_PREFIX + "conversation_insights", "Conversation insights", ("conversation.insights",), ()),
    (ACTIVITY_PREFIX + "voice_measurements", "Voice call measurements", (), ("voice.live.",)),
    (ACTIVITY_PREFIX + "admin_tools", "Admin tools (MCP)", ("ResponseStreamWriter",), ()),
    (ACTIVITY_PREFIX + "embeddings", "Embeddings", ("openai.embeddings",), ()),
    # A bare model call no root span was opened for: a helper, or a judge
    # called outside a test run's trace. Nothing on it says which.
    (ACTIVITY_PREFIX + "model_calls", "Other model calls",
     ("ChatGoogleGenerativeAI.chat", "gemini.generate_content", "gemini.generate_content_stream",
      "ChatOpenAI.chat", "AzureChatOpenAI.chat", "openai.chat", "ChatAnthropic.chat",
      "anthropic.chat"), ()),
)
ACTIVITY_LABELS: Dict[str, str] = {ACTIVITY_TEST_RUNS: "Test runs",
                                   **{key: label for key, label, _n, _p in ACTIVITY_CLASSES}}


def activity_class(top_span_name: Optional[str], *, agent_kind: Optional[str] = None,
                   eval_run_id: Optional[str] = None) -> Optional[str]:
    """The ``activity:*`` key of a trace that is test traffic or the engine's
    own background work, or None for an agent's ordinary turn (and for an
    untagged trace nothing identifies).

    An agent's turn made by the eval runner (it carries ``waj.eval.run_id``)
    is a test run whatever agent answered. Everything else is classified only
    when it has NO agent kind, by its top span's name: a tagged trace is that
    agent's, whatever its top span is called."""
    if eval_run_id:
        return ACTIVITY_TEST_RUNS
    if agent_kind:
        return None
    name = top_span_name or ""
    if not name:
        return None
    for key, _label, names, prefixes in ACTIVITY_CLASSES:
        if name in names or any(name.startswith(p) for p in prefixes):
            return key
    return None


# A turn that ran this long is a problem in any channel (the eval runner and
# most callers give up at 120s), so it marks its conversation "Needs attention"
# even without an error event. Whole voice CALLS are metric spans (excluded
# above); a voice.agent_turn is one answer and is held to the same bar.
SLOW_TURN_SEC = 60


def _trace_duration_sec(trace: Optional[Dict[str, Any]]) -> Optional[float]:
    """Seconds between a trace row's start and end, or None when unknown."""
    if not trace:
        return None
    ms = trace.get("duration_ms")
    if isinstance(ms, (int, float)) and ms > 0:
        return ms / 1000.0
    from admin.screens.conversations import _parse_ts

    start, end = _parse_ts(trace.get("startTime")), _parse_ts(trace.get("endTime"))
    if start and end and end > start:
        return (end - start).total_seconds()
    return None


def background_trace_clauses() -> List[str]:
    """ClickHouse AND-clauses on a `traces_replacing` row that drop every
    non-conversational trace: background kinds, untagged bare-LLM/MCP traces,
    voice metric markers and eval traffic. Shared by the Conversations list
    and the Overview rollups so both count the same thing."""
    kinds = ", ".join("'%s'" % k for k in _BACKGROUND_AGENT_KINDS)
    spans = ", ".join("'%s'" % s for s in _BACKGROUND_TOP_SPANS)
    # A build may name no background kind at all: `NOT IN ()` is not valid
    # ClickHouse, so the clause is left out then.
    by_kind = (["simpleJSONExtractString(metadata, 'waj.agent_kind') NOT IN (%s)" % kinds]
               if kinds else [])
    return [
        *by_kind,
        "NOT (simpleJSONExtractString(metadata, 'waj.agent_kind') = '' "
        "AND top_span_name IN (%s))" % spans,
        "NOT startsWith(top_span_name, '%s')" % _METRIC_TOP_SPAN_PREFIX,
        "NOT startsWith(top_span_name, '%s')" % _EVAL_TOP_SPAN_PREFIX,
        "simpleJSONExtractString(metadata, 'waj.eval.run_id') = ''",
    ]

# Diagnostic self-test spans — the /admin/lmnr/diagnose page emits one every
# time an operator runs the telemetry self-test. They are never real traffic, so
# they're excluded from the trace list UNCONDITIONALLY (even with background on)
# to stop them showing as empty traces. Still reachable via the diagnose page's
# own deep-link.
_DIAGNOSTIC_TOP_SPANS = ("lmnr_diagnose_test_span",)

# Voice-live metric point spans (voice/live/metrics.py) — instantaneous
# markers whose measurements live in `waj.voice.live.*` ATTRIBUTES, never in
# span length. As trace rows they read as ~0ms "conversations" and, after a
# voice-heavy session, bury the real chat traces under a wall of sub-ms
# entries. Background class: hidden from the default conversational list,
# shown with ?background=1 (same opt-in as background agents). Their real
# rollup is load_voice_live_insights / the Dashboard's Duplex-voice card, and
# load_live_insights inherits this exclusion by reading the same query.
_METRIC_TOP_SPAN_PREFIX = "voice.live."


def _env_sql_clause(env: Optional[str], source: str) -> tuple[str, Dict[str, Any]]:
    """A ``waj.env`` scoping fragment for a ClickHouse query.

    ``source`` is the JSON column the env is read from: ``'attributes'`` on the
    ``spans`` table, ``'metadata'`` on ``traces_replacing``. Mirrors
    ``_filter_clauses``: prod is strict, staging also admits untagged (legacy)
    traces, and ``None`` yields no clause. Returns ``(sql_fragment, params)``;
    the env value travels as a ``{env:String}`` param, never inlined."""
    if not env:
        return "", {}
    expr = "simpleJSONExtractString(%s, 'waj.env')" % source
    if env == "staging":
        return ("  AND (%s = {env:String} OR %s = '')\n" % (expr, expr), {"env": env})
    return ("  AND %s = {env:String}\n" % expr, {"env": env})


def _ch_trace_query(
    sql: str, params: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Indirection over ``core.lmnr_clickhouse.query`` so tests can stub it."""
    from core.lmnr_clickhouse import query as ch_query

    return ch_query(sql, params)


def _trace_list_sql(
    *,
    env: Optional[str],
    include_background: bool,
    status: Optional[str],
    search: Optional[str] = None,
) -> tuple[str, Dict[str, Any]]:
    """Build the parameterised ClickHouse query for the trace list.

    Reads ``traces_replacing FINAL`` (ReplacingMergeTree → FINAL dedups). All
    filters — time window, env scope, background exclusion, status — are applied
    server-side so a background-dominated firehose still yields the recent
    conversational traces. Only the fixed enum tokens (kind/span names) are
    inlined; every caller-supplied value travels as a ``{name:Type}`` param.
    """
    from admin.screens.plain_language import HANDOFF_TOOL_RE

    params: Dict[str, Any] = {"handoff_re": HANDOFF_TOOL_RE}
    where = ["start_time > now() - toIntervalHour({hours:UInt32})"]

    if env:
        params["env"] = env
        # Scope to the selected env, but ALSO admit env-less traces: a
        # conversational trace that misses env tagging (the bug class this PR
        # fixes) must never be silently hidden — only the known background flood
        # is excluded, and that happens by top-span signature below regardless
        # of env.
        where.append(
            "(simpleJSONExtractString(metadata, 'waj.env') = {env:String} "
            "OR simpleJSONExtractString(metadata, 'waj.env') = '')"
        )

    if not include_background:
        # Background kinds, untagged bare-LLM / admin-MCP traces, voice-live
        # metric markers and eval test traffic.
        where.extend(background_trace_clauses())

    # The Error/Success filter runs on the events-derived aggregate joined
    # below, NEVER on `traces_replacing.status` — that column is '' on every
    # row, so filtering it made Error return nothing and Success everything.
    # ClickHouse LEFT JOIN default-fills a spanless trace with 0 → success.
    # Applied OUTSIDE the FINAL subquery (see the SQL below) — it is the only
    # predicate on the join.
    outer_where: List[str] = []
    if status == "error":
        outer_where.append("err.has_error = 1")
    elif status == "success":
        outer_where.append("err.has_error != 1")

    if search:
        # Free-text narrow, pushed down so a search still works on a
        # background-dominated firehose. The needle travels as a param.
        params["search"] = search
        where.append(
            "(positionCaseInsensitive(top_span_name, {search:String}) > 0"
            " OR positionCaseInsensitive(metadata, {search:String}) > 0"
            " OR positionCaseInsensitive(toString(session_id), {search:String}) > 0"
            " OR positionCaseInsensitive(toString(id), {search:String}) > 0)"
        )

    # Diagnostic self-test spans are never real traffic — drop them always.
    diag = ", ".join("'%s'" % s for s in _DIAGNOSTIC_TOP_SPANS)
    where.append("top_span_name NOT IN (%s)" % diag)

    # Trace-level error state, aggregated from span events in ONE join over the
    # window's spans (a span starts no earlier than its trace, so the same time
    # bound covers every span of every listed trace) — never an N+1 per trace,
    # and never the dead `status` column. Same per-span predicate as
    # `_SPAN_LIST_SQL`'s `has_error`.
    #
    # The FINAL scan and every trace-level filter live in their OWN subquery,
    # and only the `err.*` status predicate sits on the join. With
    # `traces_replacing FINAL` joined directly and the filters in the outer
    # WHERE, ClickHouse 26.5 (the deployed lmnr-server) pushes the WHERE into
    # the FINAL scan and fails with `Code: 10 Not found column
    # and(greater(__table1.start_time …` — every unfiltered list load 500'd and
    # the explorer showed "Telemetry unavailable" (2026-08-26 → 2026-09-29).
    sql = (
        "SELECT toString(t.id) AS id, "
        "toString(t.start_time) AS startTime, "
        "toString(t.end_time) AS endTime, "
        # Each model call counted once (core/llm_calls.py): Laminar's own
        # per-trace totals add both spans of a call.
        "toInt64(err.input_tokens_once) AS inputTokens, "
        "toInt64(err.output_tokens_once) AS outputTokens, "
        "toFloat64(t.duration) AS durationSec, "
        "toUInt8(err.has_error) AS has_error, "
        "toUInt32(err.tool_calls) AS tool_calls, "
        "toUInt32(err.tool_errors) AS tool_errors, "
        "toUInt32(err.handoffs) AS handoffs, "
        "t.top_span_name AS topSpanName, "
        "t.metadata AS metadata, "
        "t.session_id AS sessionId "
        "FROM (SELECT * FROM traces_replacing FINAL "
        "WHERE " + " AND ".join(where) + ") AS t "
        "LEFT JOIN ("
        "SELECT trace_id, "
        + _once_columns().replace("\n", " ") + ", "
        "max(arrayExists(x -> x.2 IN ('exception', 'error'), events)) AS has_error, "
        # Action (tool) counts for the Conversations outcome column: failed
        # actions and hand-off/escalation actions (plain_language.HANDOFF_TOOL_RE).
        "countIf(name LIKE '%.tool') AS tool_calls, "
        "countIf(name LIKE '%.tool' AND arrayExists(x -> x.2 IN ('exception', 'error'), events)) AS tool_errors, "
        "countIf(name LIKE '%.tool' AND match(lower(name), {handoff_re:String}) "
        "AND NOT arrayExists(x -> x.2 IN ('exception', 'error'), events)) AS handoffs "
        "FROM spans "
        "WHERE start_time > now() - toIntervalHour({hours:UInt32}) "
        "GROUP BY trace_id"
        ") AS err ON err.trace_id = t.id "
        + ("WHERE " + " AND ".join(outer_where) + " " if outer_where else "")
        + "ORDER BY t.start_time DESC "
        "LIMIT {limit:UInt32}"
    )
    return sql, params


def _map_ch_trace_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map a ``traces_replacing`` row onto the trace shape the templates +
    ``_annotate_trace_channel_meta`` / ``group_traces_by_session`` expect
    (camelCase keys, ``metadata`` preserved, ``duration_ms`` precomputed).

    Token counts are coerced to ``int``: ClickHouse's JSON format quotes 64-bit
    integers, and the template ADDS ``inputTokens + outputTokens`` — left as
    strings that renders "105" for 10 + 5.

    ``status`` is derived from the row's events-aggregated ``has_error`` (the
    field name is kept so templates/JS don't change); the ``status`` COLUMN is
    '' on every row and is not selected any more."""
    has_error = _as_int(row.get("has_error")) != 0
    try:
        duration_ms = int(float(row.get("durationSec") or 0) * 1000)
    except (TypeError, ValueError):
        duration_ms = None
    return {
        "id": row.get("id"),
        "startTime": _iso_ts(row.get("startTime")),
        "endTime": _iso_ts(row.get("endTime")),
        "inputTokens": _as_int(row.get("inputTokens")),
        "outputTokens": _as_int(row.get("outputTokens")),
        "duration_ms": duration_ms,
        "status": "error" if has_error else "success",
        "tool_calls": _as_int(row.get("tool_calls")),
        "tool_errors": _as_int(row.get("tool_errors")),
        "handoffs": _as_int(row.get("handoffs")),
        "topSpanName": row.get("topSpanName"),
        "name": row.get("topSpanName"),
        "metadata": row.get("metadata") or "",
        "sessionId": row.get("sessionId") or "",
    }


def load_clickhouse_traces(
    *,
    past_hours: int,
    page_size: int,
    env: Optional[str],
    include_background: bool,
    status: Optional[str],
    search: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Return source-filtered, mapped trace rows from ClickHouse, or ``None``
    when ClickHouse is unavailable. ``None`` is the "no data source" signal —
    distinct from ``[]``, which means "the query ran and matched nothing"."""
    sql, params = _trace_list_sql(
        env=env,
        include_background=include_background,
        status=status,
        search=search,
    )
    params = {**params, "hours": int(past_hours), "limit": int(page_size)}
    try:
        rows = _ch_trace_query(sql, params)
    except Exception as exc:  # noqa: BLE001 - any failure → no data source
        logger.warning("observability: ClickHouse trace list unavailable (%s)", exc)
        return None
    return [_map_ch_trace_row(r) for r in (rows or [])]


def load_traces_list(
    *,
    past_hours: int = 24,
    page_size: int = 50,
    search: Optional[str] = None,
    status: Optional[str] = None,
    channel_slug: Optional[str] = None,
    agent_kind: Optional[str] = None,
    env: Optional[str] = None,
    include_background: bool = False,
) -> Dict[str, Any]:
    """Return template context for the trace-list view.

    Everything the query can express is applied server-side in ClickHouse: the
    time window, ``waj.env`` scope, background/diagnostic exclusion, status, and
    free-text ``search``. Only ``channel_slug`` / ``agent_kind`` narrow in memory
    — they read the ``waj.channel.slug`` / ``waj.agent_kind`` keys that
    ``_annotate_trace_channel_meta`` already lifted off each row's metadata, so
    the narrow costs no extra queries. Legacy traces from before root-span
    tagging carry neither key and are excluded when either filter is set.
    """
    ch_rows = load_clickhouse_traces(
        past_hours=past_hours,
        page_size=page_size,
        env=env,
        include_background=include_background,
        status=status,
        search=search,
    )
    if ch_rows is None:
        return {
            "traces": [],
            "total": 0,
            "notice": _ch_notice(),
            "past_hours": past_hours,
            "search": search or "",
            "status": status or "",
            "channel_slug": channel_slug or "",
            "agent_kind": agent_kind or "",
        }

    _annotate_trace_channel_meta(ch_rows)
    # Scoped build: this engine is a
    # standalone single-client service, so the explorer must list ONLY that
    # client's traces. ClickHouse is ONE shared store across every env + client,
    # so without this narrow the default (unfiltered) list surfaces WAJ's traces
    # on a client's portal. No-op on the full WAJ engine.
    from admin.seed_agent_profiles import filter_trace_rows_to_client
    ch_rows = filter_trace_rows_to_client(ch_rows)
    if channel_slug:
        ch_rows = [t for t in ch_rows if t.get("channel_slug") == channel_slug]
    if agent_kind:
        from admin.agent_kinds import split_agent_choice

        # `custom:<slug>` picks ONE generic agent: they all share the kind.
        kind, slug = split_agent_choice(agent_kind)
        ch_rows = [t for t in ch_rows if t.get("agent_kind") == kind]
        if slug:
            _fill_agent_slugs_from_sessions(ch_rows, env)
            ch_rows = [t for t in ch_rows if t.get("agent_slug") == slug]
    return {
        "traces": ch_rows,
        "total": len(ch_rows),
        "notice": None,
        "past_hours": past_hours,
        "search": search or "",
        "status": status or "",
        "channel_slug": channel_slug or "",
        "agent_kind": agent_kind or "",
    }


def _fill_agent_slugs_from_sessions(traces: List[Dict[str, Any]], env: Optional[str]) -> None:
    """Traces recorded before ``waj.agent_slug`` was tagged name no agent;
    their chat session does (``ai_chat_sessions.metadata.agent_slug``).
    Fills ``agent_slug`` in place for those that have a session. Best-effort:
    a failed read leaves them unnamed (so a one-agent filter skips them)."""
    missing: Dict[str, List[Dict[str, Any]]] = {}
    for t in traces:
        sid = t.get("session_id")
        if t.get("agent_slug") or not sid:
            continue
        coerced = str(_coerce_session_id_for_lookup(sid) or sid)
        missing.setdefault(coerced, []).append(t)
    if not missing:
        return
    try:
        from admin.router import client_for

        rows = (
            client_for(env or "staging").schema("public").table("ai_chat_sessions")
            .select("id, metadata").in_("id", sorted(missing)[:500]).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001 - names are a narrowing aid only
        logger.debug("traces list: session agent slugs unavailable (%s)", exc)
        return
    for r in rows:
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        slug = meta.get("agent_slug")
        if slug:
            for t in missing.get(str(r.get("id")), []):
                t["agent_slug"] = slug


_EVAL_TRACES_SQL = (
    "SELECT toString(id) AS id, "
    "simpleJSONExtractString(metadata, 'waj.eval.ref') AS ref "
    "FROM traces_replacing FINAL "
    "WHERE start_time > now() - toIntervalHour({hours:UInt32}) "
    "AND simpleJSONExtractString(metadata, 'waj.eval.run_id') = {run_id:String} "
    "ORDER BY start_time ASC "
    "LIMIT 5000"
)


def eval_trace_ids(run_id: str, started_at: Any) -> Optional[Dict[str, List[str]]]:
    """The agent traces one eval run produced, keyed by the case attempt's
    ``eval_ref`` (oldest first — a multi-turn case has one trace per turn).

    The engine stamps ``waj.eval.run_id`` / ``waj.eval.ref`` on its request
    root span from the runner's headers. ``None`` when ClickHouse could not be
    queried (the page then says so instead of "no trace recorded")."""
    from admin.screens.conversations import _parse_ts
    from datetime import datetime, timezone

    if not looks_like_trace_id(run_id):
        return {}
    start = _parse_ts(started_at)
    age_h = 24
    if start is not None:
        age_h = int((datetime.now(timezone.utc) - start).total_seconds() // 3600) + 2
    try:
        rows = _ch_trace_query(_EVAL_TRACES_SQL, {"run_id": str(run_id).lower(),
                                                  "hours": max(1, min(age_h, 24 * 365))}) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("observability: eval trace lookup failed for run %s (%s)", run_id, exc)
        return None
    out: Dict[str, List[str]] = {}
    for r in rows:
        ref = r.get("ref")
        if ref and r.get("id"):
            out.setdefault(ref, []).append(r["id"])
    return out


def load_spans_for_trace(trace_id: str) -> List[Dict[str, Any]]:
    """Tiny helper for screens that just want the flat span list (e.g. the
    eval-run detail page rendering scenarios inline)."""
    spans = ch_list_spans_for_trace(trace_id)
    if not spans:
        return []
    io_map = _ch_io_map(trace_id)
    return [_enrich_span(s, io_map.get(s.get("spanId"))) for s in spans]


# ---------------------------------------------------------------------------
# Explorer helpers — three-pane UI at /admin/observability/explorer
# ---------------------------------------------------------------------------


def load_span_tree_json(trace_id: str) -> Dict[str, Any]:
    """Return {trace, spans, tree} as plain JSON-safe dicts for the middle
    pane of the explorer.

    `tree` is a list of nested {span_id, parent_span_id, name, type,
    status, depth, start_offset_ms, duration_ms, model, in_tokens,
    out_tokens, tool_name, children: []} nodes. The middle pane uses
    `start_offset_ms` + `duration_ms` for the inline Gantt bar.
    """
    trace = ch_get_trace(trace_id)
    raw_spans, spans_ok = ch_list_spans_for_trace_result(trace_id)
    if trace is None and not raw_spans:
        found, unavailable = _trace_found(trace_id, None, [])
        # A malformed id makes ClickHouse REJECT the span query; that is "not
        # found", not an outage (_trace_found already answered for it).
        unavailable = unavailable or (not spans_ok and looks_like_trace_id(trace_id))
        return {"trace": None, "tree": [], "found": found, "unavailable": unavailable,
                "notice": _ch_notice() if unavailable else "No steps recorded for this conversation."}

    io_map = _ch_io_map(trace_id)
    enriched = [_enrich_span(s, io_map.get(s.get("spanId"))) for s in raw_spans]
    nested = build_span_tree(enriched)

    trace_start = (trace or {}).get("startTime")
    trace_end = (trace or {}).get("endTime")
    # A trace row can lag its spans (ReplacingMergeTree merge) — fall back to the
    # span window so the Gantt bar still has a baseline.
    if not trace_start and raw_spans:
        trace_start = raw_spans[0].get("startTime")
    if not trace_end and raw_spans:
        trace_end = max(
            (s.get("endTime") or "" for s in raw_spans), default=""
        ) or None
    try:
        from datetime import datetime
        start_dt = datetime.fromisoformat((trace_start or "").replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat((trace_end or "").replace("Z", "+00:00"))
        trace_duration_ms = max(1, int((end_dt - start_dt).total_seconds() * 1000))
    except Exception:
        start_dt = None
        trace_duration_ms = 1

    def _node(span: Dict[str, Any], depth: int) -> Dict[str, Any]:
        start_offset_ms: Optional[int] = None
        if start_dt is not None and span.get("startTime"):
            try:
                from datetime import datetime
                s_dt = datetime.fromisoformat(span["startTime"].replace("Z", "+00:00"))
                start_offset_ms = int((s_dt - start_dt).total_seconds() * 1000)
            except Exception:
                start_offset_ms = None
        return {
            "span_id": span.get("spanId"),
            "parent_span_id": span.get("parentSpanId"),
            "name": span.get("name"),
            "type": span.get("spanType"),
            "status": span.get("status"),
            "depth": depth,
            "duration_ms": span.get("duration_ms"),
            "start_offset_ms": start_offset_ms,
            "model": span.get("model"),
            "in_tokens": span.get("inputTokens"),
            "out_tokens": span.get("outputTokens"),
            "cache_read": span.get("cache_read_tokens"),
            "tool_name": (span.get("tool_io") or {}).get("name"),
            "children": [_node(c, depth + 1) for c in span.get("children", [])],
        }

    # `traces_replacing` has no top_span_type column — the root span the tree
    # already resolved is the authority, and it also lets the header render when
    # only spans came back.
    root = nested[0] if nested else {}
    hdr = trace or {}
    return {
        "trace": {
            "id": hdr.get("id") or trace_id,
            "status": hdr.get("status") or root.get("status"),
            "start_time": trace_start,
            "end_time": trace_end,
            "duration_ms": trace_duration_ms,
            "input_tokens": hdr.get("inputTokens"),
            "output_tokens": hdr.get("outputTokens"),
            "total_cost": hdr.get("totalCost"),
            "session_id": hdr.get("sessionId"),
            "top_span_name": hdr.get("topSpanName") or root.get("name"),
            "top_span_type": hdr.get("topSpanType") or root.get("spanType"),
        },
        "tree": [_node(r, 0) for r in nested],
        "found": True,
        "unavailable": False,
        "notice": None,
    }


def load_span_detail_json(trace_id: str, span_id: str) -> Dict[str, Any]:
    """Return one span's full detail for the right pane.

    "Span not found" is only ever said about a store that ANSWERED. When
    ClickHouse is unreachable the span list comes back empty, and reporting
    that as not-found sends the operator hunting for data loss during a
    connectivity problem — while the tree pane beside it still shows the span
    they just clicked. Sibling loaders surface ``_CH_NOTICE`` for this
    condition; so does this one."""
    raw_spans, ch_ok = ch_list_spans_for_trace_result(trace_id)
    target = next((s for s in raw_spans if s.get("spanId") == span_id), None)
    if target is None:
        return {"span": None, "notice": "Span not found" if ch_ok else _ch_notice()}

    enriched = _enrich_span(target, _ch_io_map(trace_id).get(span_id))
    # Stringify attribute values so the JSON payload is always serialisable
    # (nested structures, datetimes, …). The per-message
    # `gen_ai.prompt.N.*` / `gen_ai.completion.N.*` family is dropped: it is by
    # far the largest thing on an LLM span and `llm_io` above already renders the
    # same content in readable form, so shipping it too would only bloat the
    # response and the attributes table.
    from demo_mode.policy import span_attribute_hidden

    safe_attrs: Dict[str, Any] = {}
    for k, v in (enriched.get("attributes") or {}).items():
        # ...and, in the public demo, where the model is served (demo_mode/policy.py).
        if _is_bulk_message_attr(k) or span_attribute_hidden(k):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe_attrs[k] = v
        else:
            try:
                safe_attrs[k] = json.dumps(v, default=str, ensure_ascii=False)
            except Exception:
                safe_attrs[k] = str(v)

    return {
        "span": {
            "span_id": enriched.get("spanId"),
            "name": enriched.get("name"),
            "type": enriched.get("spanType"),
            "status": enriched.get("status"),
            "start_time": enriched.get("startTime"),
            "end_time": enriched.get("endTime"),
            "duration_ms": enriched.get("duration_ms"),
            "model": enriched.get("model"),
            "in_tokens": enriched.get("inputTokens"),
            "out_tokens": enriched.get("outputTokens"),
            "cache_read": enriched.get("cache_read_tokens"),
            "llm_io": enriched.get("llm_io"),
            "tool_io": enriched.get("tool_io"),
            "events": enriched.get("events") or [],
            "attributes": safe_attrs,
        },
        "notice": None,
    }


def _is_llm_span(span: Dict[str, Any], attrs: Dict[str, Any]) -> bool:
    """Heuristic: is this span an LLM call (i.e. an assistant turn)?

    Mirrors `_is_tool_span` but for the LLM side — the explicit Laminar
    span type wins, then OpenLLMetry's `traceloop.span.kind == "llm"`,
    then the presence of `gen_ai.*` content / model attributes."""
    if (span.get("spanType") or "").upper() == "LLM":
        return True
    if not isinstance(attrs, dict):
        return False
    for key in _TOOL_KIND_KEYS:
        val = attrs.get(key)
        if isinstance(val, str) and val.strip().lower() == "llm":
            return True
    if attrs.get("gen_ai.system") or attrs.get("gen_ai.request.model"):
        return True
    return False


def _persisted_tokens(meta: Dict[str, Any]) -> Optional[int]:
    """Total tokens for a turn from the assistant message metadata. Agents
    persist ``usage={input_tokens,output_tokens}``; older rows used ``tokens``."""
    usage = meta.get("usage")
    if isinstance(usage, dict):
        total = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        if total:
            return total
    tok = meta.get("tokens")
    return tok if isinstance(tok, int) else None


def _persisted_reasoning(meta: Dict[str, Any]) -> Optional[str]:
    """The agent's reasoning trail persisted on the assistant message
    (``thoughts`` — a list of step strings, or a single string)."""
    th = meta.get("thoughts")
    if isinstance(th, list):
        parts = [str(t) for t in th if t]
        return "\n".join(parts) if parts else None
    if isinstance(th, str) and th.strip():
        return th
    return None


def _persisted_tool_calls(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalise persisted ``tool_calls`` into the transcript tool shape.
    Tolerates the small key variations agents use (name/input/output/error)."""
    raw = meta.get("tool_calls")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or t.get("tool") or t.get("tool_name")
        if not name:
            continue
        inp = t.get("input", t.get("args"))
        outp = t.get("output", t.get("result"))
        out.append({
            "name": str(name),
            "input": inp if isinstance(inp, str) else _pretty_json(json.dumps(inp, ensure_ascii=False, default=str) if inp is not None else ""),
            "output": outp if isinstance(outp, str) else _pretty_json(json.dumps(outp, ensure_ascii=False, default=str) if outp is not None else ""),
            "is_error": bool(t.get("is_error") or t.get("error")),
        })
    return out


def _coerce_session_id_for_lookup(session_id: Optional[str]) -> Optional[str]:
    """Map a trace's raw ``waj.session_id`` to the id the chat tables key on.

    ``core.chat_persistence`` coerces every session id to a valid ``uuid`` before
    insert (non-UUID client ids → a deterministic UUIDv5). The trace, however,
    carries the RAW id. Reading with the raw id therefore misses the persisted
    (coerced) row, which is why conversational agents with non-UUID session ids
    (and synthetic sessions) fell back to
    telemetry reconstruction. Applying the SAME coercion on read fixes the
    linkage; it is idempotent for ids that are already UUIDs. Best-effort — if
    the persistence module can't be imported we return the id unchanged."""
    if not session_id:
        return session_id
    try:
        from core.chat_persistence import coerce_session_id
        return coerce_session_id(session_id)
    except Exception:  # noqa: BLE001 - never block the transcript on this
        return session_id


def _structured_message_md(meta: Dict[str, Any]) -> Optional[str]:
    """Markdown for an assistant message whose real output is structured data
    in ``metadata`` rather than its text (a one-shot report, say): the first
    answer from a plugin's ``portal_display`` hook, ``message_markdown(meta)``.
    Without it the transcript would show a bare sentence and drop the actual
    output. ``None`` for an ordinary message."""
    if not isinstance(meta, dict):
        return None
    from core.plugins import call_hook, load_hook

    for md in call_hook(load_hook("portal_display"), "message_markdown", meta):
        if md:
            return md
    return None


# agent_kinds that never persist a user/assistant chat transcript — a trace for
# one of these is EXPECTED to have no linked chat session (one-shot generation
# or background work), so its transcript is honestly telemetry-only rather than
# a symptom of a broken session link.
_NON_CHAT_AGENT_KINDS = ("voice",)


def _non_chat_agent_kinds() -> tuple:
    """_NON_CHAT_AGENT_KINDS plus a plugin's own (its ``portal_display`` hook,
    ``non_chat_agent_kinds()``)."""
    from core.plugins import call_hook, load_hook

    kinds = list(_NON_CHAT_AGENT_KINDS)
    for extra in call_hook(load_hook("portal_display"), "non_chat_agent_kinds"):
        kinds.extend(k for k in (extra or ()) if k not in kinds)
    return tuple(kinds)


def _reconstruction_notice(agent_kind: Optional[str]) -> str:
    """Banner shown when a transcript is rebuilt from spans (no persisted chat
    messages). Informational for one-shot / background kinds; diagnostic for
    conversational agents, where a missing session usually signals a real gap
    (and, since the coercion fix, should be rare)."""
    if agent_kind in _non_chat_agent_kinds():
        return (
            f"This is a {agent_kind} run — it has no chat transcript by design. "
            "Showing the activity reconstructed from telemetry."
        )
    return (
        "Reconstructed from telemetry — no persisted chat session is linked to "
        "this trace."
    )


def _persisted_conversation(session_id: Optional[str], env: Optional[str]) -> List[Dict[str, Any]]:
    """The persisted ai_chat_messages for a session, as ordered user/assistant
    turns. This is the reliable source of truth for the conversation (the
    Transcript's spine); span-derived turns are only a fallback because span
    *input* capture is often disabled, which is why user messages went missing.

    Returns [] when there's no session, no env, or the table is unreachable."""
    if not session_id:
        return []
    # The trace carries the RAW session id; the row is stored under the coerced
    # id. Query the coerced id or non-UUID sessions never match (see helper).
    lookup_id = _coerce_session_id_for_lookup(session_id)
    try:
        if env:
            from admin.router import client_for
            client = client_for(env)
        else:
            client = admin_service_client()
        rows = (
            client.schema("public").table("ai_chat_messages")
            .select("role, content, metadata, created_at")
            .eq("session_id", lookup_id)
            .order("created_at")
            .limit(200)
            .execute()
            .data or []
        )
    except Exception as exc:  # noqa: BLE001 - never block the transcript on this
        logger.debug("persisted conversation fetch failed for %s: %s", lookup_id, exc)
        return []
    turns: List[Dict[str, Any]] = []
    for r in rows:
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        role = _normalize_role(r.get("role")) or "assistant"
        turn: Dict[str, Any] = {
            "role": role,
            "content": r.get("content") or "",
            "ts": r.get("created_at"),
            "source": "persisted",
            "tokens": _persisted_tokens(meta),
        }
        if role == "assistant":
            # DB-backed tool calls + reasoning so the transcript renders fully
            # from Postgres, independent of the ClickHouse retention window.
            tools = _persisted_tool_calls(meta)
            if tools:
                turn["tools"] = tools
            reasoning = _persisted_reasoning(meta)
            if reasoning:
                turn["reasoning"] = reasoning
            # A structured result stashed in metadata — fold it into the
            # visible bubble so the transcript shows the actual output.
            structured_md = _structured_message_md(meta)
            if structured_md:
                base = (turn["content"] or "").strip()
                turn["content"] = (
                    structured_md if (not base or base in structured_md)
                    else base + "\n\n" + structured_md
                )
        turns.append(turn)
    return turns


def load_transcript_json(trace_id: str, env: Optional[str] = None) -> Dict[str, Any]:
    """Return the conversation transcript for a trace.

    The conversation spine (user + assistant turns) comes from the persisted
    ``ai_chat_messages`` for the trace's session when available — that is the
    reliable record and the reason this view fully replaces the old Sessions
    page. This trace's **tool calls** and the raw **model output** (reasoning)
    are pulled from the spans (ClickHouse) and attached to the matching
    assistant turn, so one view shows both the clean conversation *and* what the
    agent did under the hood.

    When no chat session is linked (or its messages are unavailable), the turns
    are reconstructed from spans alone (telemetry-only fallback).

    Turn shape::

        {"role": "user",      "content": "...", "ts": "..."}
        {"role": "assistant", "content": "...", "ts": "...", "tokens": 123,
         "tools": [{"name", "input", "output", "is_error"}], "reasoning": "..."}
    """
    trace = ch_get_trace(trace_id)

    spans = _clickhouse_spans(trace_id)
    # Prefer the trace row's own session_id, but fall back to the
    # waj.session_id we tag on the root span (our agents don't set the former).
    session_id = (trace or {}).get("sessionId") or _session_id_from_spans(spans)
    trace_hdr = {
        "id": (trace or {}).get("id") or trace_id,
        "status": (trace or {}).get("status"),
        "top_span_name": (trace or {}).get("topSpanName"),
        "session_id": session_id,
    }

    # --- span-derived: this trace's tool calls + a fallback user/assistant ----
    tools: List[Dict[str, Any]] = []
    span_user: Optional[str] = None
    span_assistant: Optional[str] = None
    turn_cache_read = _cache_tokens_once(spans)["read"]
    for s in spans:
        name = s.get("name") or ""
        try:
            span_type = int(s.get("span_type") or 0)
        except (TypeError, ValueError):
            span_type = 0
        ch_in = s.get("input") or ""
        ch_out = s.get("output") or ""
        if name.endswith(".tool"):
            tools.append({
                "name": name[: -len(".tool")],
                "input": _pretty_json(ch_in),
                "output": _pretty_json(ch_out),
                "is_error": bool(int(s.get("has_error") or 0)),
                "ts": s.get("start_time"),
            })
        elif span_type == 1:
            if span_user is None:
                u = [m for m in _parse_messages(ch_in) if m["role"] == "user" and m["text"]]
                if u:
                    span_user = u[-1]["text"]
            out_text = "\n".join(m["text"] for m in _parse_messages(ch_out) if m["text"])
            if out_text:
                span_assistant = out_text  # last LLM completion wins

    # The conversation lives in the env the TRACE was recorded in (waj.env on
    # the root span / trace metadata), which is not necessarily the portal's
    # selected env — the control-plane shows one shared telemetry store for
    # both envs. Try the trace's own env first, then the selected one, then
    # the remaining env, so a prod conversation resolves even while the
    # operator is browsing with the staging cookie.
    trace_env = _trace_meta_attrs(trace or {}).get("waj.env") or _env_from_spans(spans)
    candidate_envs: List[str] = []
    for e in (trace_env, env, "prod", "staging"):
        if e and e not in candidate_envs:
            candidate_envs.append(e)
    persisted: List[Dict[str, Any]] = []
    for e in candidate_envs:
        persisted = _persisted_conversation(session_id, e)
        if persisted:
            break
    notice: Optional[str] = None

    if persisted:
        turns = persisted
        # Persisted turns are DB-first: if the assistant message already carries
        # tool calls / reasoning (metadata), use those. Only fall back to this
        # trace's spans when the DB lacks them (older messages, or
        # agents that don't persist tool calls yet). A trace == one agent turn,
        # so the span activity belongs to the most recent answer.
        target = next((t for t in reversed(turns) if t["role"] == "assistant"), None)
        if target is None:
            target = {"role": "assistant", "content": "", "ts": None, "source": "span"}
            turns.append(target)
        # Every action of the CONVERSATION, not only this trace's: each turn
        # is its own trace, so the selected trace alone showed the newest
        # turn's actions and none of the earlier ones. DB-first still wins —
        # if any persisted turn carries tool calls, spans are not merged in.
        if not any(t.get("tools") for t in turns):
            session_tools = _session_tool_spans(session_id, (trace or {}).get("startTime")
                                                or (spans[0].get("start_time") if spans else None))
            if session_tools:
                _attach_tools_by_time(turns, session_tools)
            elif tools and not target.get("tools"):
                target["tools"] = tools
        if (
            not target.get("reasoning")
            and span_assistant
            and span_assistant.strip() != (target.get("content") or "").strip()
        ):
            # The model's raw output differs from the saved answer (e.g. it
            # carried structured/JSON reasoning) — surface it collapsibly
            # rather than letting it overwrite the clean answer.
            target["reasoning"] = span_assistant
        if turn_cache_read and target is not None:
            target["cache_read"] = turn_cache_read
    elif span_user or span_assistant or tools:
        turns = []
        if span_user:
            turns.append({"role": "user", "content": span_user, "source": "span"})
        asst = {"role": "assistant", "content": span_assistant or "", "source": "span"}
        if tools:
            asst["tools"] = tools
        if turn_cache_read:
            asst["cache_read"] = turn_cache_read
        turns.append(asst)
        # Tailor the banner to the agent kind: one-shot / background kinds have
        # no chat transcript by design, so the message should read as expected,
        # not as a broken session link.
        agent_kind = _trace_meta_attrs(trace or {}).get("waj.agent_kind")
        notice = _reconstruction_notice(agent_kind)
    else:
        turns = []
        notice = (
            "No conversation captured — this conversation's messages are not "
            "available."
            if not spans else
            "No conversation turns could be derived for this trace."
        )

    found, unavailable = _trace_found(trace_id, trace, spans)
    if not found:
        notice = ("Conversation details are temporarily unavailable — please try "
                  "again in a few minutes." if unavailable
                  else "We couldn't find this conversation. The link may be "
                       "wrong, or the conversation is older than the retention window.")

    summary = _conversation_summary(trace, spans, session_id, turns, env)
    return {"trace": trace_hdr, "session_id": session_id, "turns": turns, "notice": notice,
            "found": found, "unavailable": unavailable, "summary": summary}


def _trace_found(trace_id: str, trace: Optional[Dict[str, Any]],
                 spans: List[Dict[str, Any]]) -> Tuple[bool, bool]:
    """``(found, unavailable)`` for a trace lookup that already ran.

    Found = the trace row OR any span came back. When neither did, ask the
    trace query once more for the unambiguous answer: ClickHouse answered
    (→ genuinely not found) or it failed (→ temporarily unavailable). The two
    must never collapse — a zero-filled record for a link that does not exist
    reads as a real, empty conversation (portal UX audit P0 #4)."""
    if trace is not None or spans:
        return True, False
    if not looks_like_trace_id(trace_id):
        return False, False
    _row, ok = ch_get_trace_result(trace_id)
    if _row is not None:
        return True, False
    return False, not ok


_SESSION_TOOLS_SQL = """
SELECT s.name AS name,
       s.input AS input,
       s.output AS output,
       toUInt8(arrayExists(x -> x.2 IN ('exception', 'error'), s.events)) AS has_error,
       arrayFirst(x -> x.2 IN ('exception', 'error'), s.events).3 AS event_attrs,
       toString(s.start_time) AS ts,
       toString(s.trace_id) AS trace_id
FROM spans AS s
WHERE s.start_time > parseDateTime64BestEffort({from:String}, 9)
  AND s.start_time < parseDateTime64BestEffort({to:String}, 9)
  AND s.name LIKE '%.tool'
  AND s.trace_id IN (
      SELECT DISTINCT trace_id FROM spans
      WHERE start_time > parseDateTime64BestEffort({from:String}, 9)
        AND start_time < parseDateTime64BestEffort({to:String}, 9)
        AND simpleJSONExtractString(attributes, 'waj.session_id') = {sid:String})
ORDER BY s.start_time ASC
LIMIT 500
"""


def _session_tool_spans(session_id: Optional[str], around: Optional[str]) -> List[Dict[str, Any]]:
    """Every tool call of one conversation (all its traces), oldest first.

    Bounded to ±2 days around the selected trace so the session-id scan stays
    cheap on a large store. ``[]`` on any failure — callers then fall back to
    the selected trace's own tool calls."""
    if not session_id:
        return []
    try:
        from datetime import datetime, timedelta, timezone
        center = (datetime.fromisoformat(str(around).replace("Z", "+00:00").replace(" ", "T"))
                  if around else datetime.now(timezone.utc))
        if center.tzinfo is None:
            center = center.replace(tzinfo=timezone.utc)
        params = {
            "sid": session_id,
            "from": (center - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "to": (center + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        rows = _ch_trace_query(_SESSION_TOOLS_SQL, params) or []
    except Exception as exc:  # noqa: BLE001 - fall back to this trace's tools
        logger.debug("session tool spans unavailable for %s (%s)", session_id, exc)
        return []
    out = []
    for r in rows:
        exc_info = None
        try:
            from admin.screens.plain_language import exception_from_event_attrs
            exc_info = exception_from_event_attrs(r.get("event_attrs"))
        except Exception:  # noqa: BLE001
            exc_info = None
        out.append({
            "name": _strip_tool_suffix(r.get("name")),
            "input": _pretty_json(r.get("input") or ""),
            "output": _pretty_json(r.get("output") or ""),
            "is_error": bool(_as_int(r.get("has_error"))),
            "error": ((exc_info or {}).get("type", "") + ": " + (exc_info or {}).get("message", "")).strip(": ")
                     if exc_info and (exc_info.get("type") or exc_info.get("message")) else None,
            "ts": _iso_ts(r.get("ts")),
            "trace_id": r.get("trace_id"),
        })
    return out


def _strip_tool_suffix(name: Any) -> str:
    n = str(name or "")
    return n[: -len(".tool")] if n.endswith(".tool") else n


def _ts_key(value: Any) -> Optional[str]:
    """Comparable UTC timestamp string from a ClickHouse or Postgres value."""
    if not value:
        return None
    from datetime import datetime, timezone
    raw = str(value).strip().replace(" ", "T", 1)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if "." in raw:
        head, rest = raw.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits):]
        raw = head + "." + digits[:6].ljust(6, "0") + tz
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _attach_tools_by_time(turns: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> None:
    """Attach each tool call to the assistant reply it fed: the first
    assistant turn persisted at/after the call; calls after the last reply
    (still running when the reply was saved) go on the last assistant turn."""
    assistants = [t for t in turns if t.get("role") == "assistant"]
    if not assistants:
        target = {"role": "assistant", "content": "", "ts": None, "source": "span"}
        turns.append(target)
        assistants = [target]
    for tool in tools:
        tk = _ts_key(tool.get("ts"))
        dest = None
        if tk:
            for a in assistants:
                ak = _ts_key(a.get("ts"))
                if ak and ak >= tk:
                    dest = a
                    break
        if dest is None:
            dest = assistants[-1]
        dest.setdefault("tools", []).append(tool)


def _count_messages(client, row: Dict[str, Any]) -> Optional[int]:
    from admin.screens.saved_chats import count_session_messages

    return count_session_messages(client, row.get("id"), row.get("message_count"))


def _session_row(session_id: Optional[str], env: Optional[str]) -> Optional[Dict[str, Any]]:
    """The linked ``ai_chat_sessions`` row (coerced id), or ``None``."""
    if not session_id:
        return None
    try:
        if env:
            from admin.router import client_for
            client = client_for(env)
        else:
            client = admin_service_client()
        rows = (
            client.schema("public").table("ai_chat_sessions")
            .select("id, agent_kind, business_id, branch_id, title, message_count, metadata, created_at")
            .eq("id", _coerce_session_id_for_lookup(session_id)).limit(1).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("session row fetch failed for %s: %s", session_id, exc)
        return None
    return rows[0] if rows else None


def _summary_agent_name(agent_kind: Optional[str], trace: Optional[Dict[str, Any]],
                        row: Optional[Dict[str, Any]], agent_names: Dict[str, str]) -> Optional[str]:
    """The agent's name for one conversation. Generic agents share a kind, so
    theirs comes from the slug on the trace (``waj.agent_slug``) or on the
    saved session, the same way the Conversations list names the row."""
    if not agent_kind:
        return None
    meta = (row or {}).get("metadata")
    slug = (_trace_meta_attrs(trace).get("waj.agent_slug") if trace else None) or (
        meta.get("agent_slug") if isinstance(meta, dict) else None)
    if slug:
        from admin.screens.conversations import _agent_slug_name

        return _agent_slug_name(agent_kind, str(slug))
    from admin.screens import plain_language as pl

    return pl.agent_label(agent_kind, agent_names)


def _conversation_summary(trace: Optional[Dict[str, Any]], spans: List[Dict[str, Any]],
                          session_id: Optional[str], turns: List[Dict[str, Any]],
                          env: Optional[str]) -> Dict[str, Any]:
    """Plain-language header + failure summary for the conversation viewer.

    Also labels every tool call in ``turns`` in place (``label`` = the action's
    display name, ``reason`` = a plain failure reason, ``is_handoff``).
    Best-effort: every lookup degrades to a humanised slug."""
    from admin.screens import plain_language as pl

    meta = _trace_meta_attrs(trace or {})
    agent_kind = meta.get("waj.agent_kind")
    channel = meta.get("waj.channel.slug")
    names: Dict[str, str] = {}
    try:
        names = pl.load_tool_display_names(env)
    except Exception:  # noqa: BLE001
        names = {}

    failures: List[Dict[str, Any]] = []
    handoffs = 0
    actions = 0
    for i, turn in enumerate(turns):
        for tool in turn.get("tools") or []:
            actions += 1
            tool["label"] = pl.action_label(tool.get("name"), names)
            tool["is_handoff"] = pl.is_handoff_tool(tool.get("name"))
            if tool.get("is_error"):
                reason = pl.plain_failure_reason(tool.get("error"), (tool.get("output") or "")[:600])
                tool["reason"] = reason
                reply = next((t.get("content") for t in turns[i:]
                              if t.get("role") == "assistant" and (t.get("content") or "").strip()), None)
                failures.append({
                    "sentence": pl.failure_sentence(tool["label"], reason),
                    "turn_index": i,
                    "agent_reply": (reply or "")[:220] or None,
                })
            elif tool["is_handoff"]:
                handoffs += 1

    row = _session_row(session_id, env)
    business_name = None
    if row and row.get("business_id"):
        business_name = pl.fetch_business_names([row["business_id"]]).get(str(row["business_id"]))
    agent_kind = agent_kind or (row or {}).get("agent_kind")
    agent_names = pl.load_agent_display_names(env) if agent_kind else {}
    has_error = (trace or {}).get("status") == "error" or any(
        int(s.get("has_error") or 0) for s in spans)
    slow_sec = _trace_duration_sec(trace)
    slow = slow_sec is not None and slow_sec > SLOW_TURN_SEC
    if slow:
        failures.append({
            "sentence": "This reply took %d seconds, so the customer was most likely "
                        "no longer waiting" % round(slow_sec),
            "turn_index": None,
            "agent_reply": None,
        })
    outcome = pl.outcome_for(failed_actions=len(failures), has_error=has_error,
                             handoffs=handoffs, slow=slow)
    started = next((t.get("ts") for t in turns if t.get("ts")), None) or (trace or {}).get("startTime")
    ended = next((t.get("ts") for t in reversed(turns) if t.get("ts")), None) or (trace or {}).get("endTime")
    return {
        "agent_kind": agent_kind,
        "agent_name": _summary_agent_name(agent_kind, trace, row, agent_names),
        "channel": channel,
        "business_id": (row or {}).get("business_id"),
        "business_name": business_name,
        "title": (row or {}).get("title") if (row or {}).get("title") not in (None, "", "New Chat") else None,
        "started": started,
        "ended": ended,
        "messages": sum(1 for t in turns if t.get("role") in ("user", "assistant")),
        "actions": actions,
        "failures": failures,
        "error_without_action": bool(has_error and not any(
            f.get("turn_index") is not None for f in failures)),
        "handoffs": handoffs,
        "outcome": outcome,
        "outcome_label": pl.OUTCOMES[outcome][0],
        "outcome_variant": pl.OUTCOMES[outcome][1],
        "simulated": str(meta.get("waj.voice.simulated") or "").lower() == "true",
    }


def load_trace_overview_json(trace_id: str, env: Optional[str] = None) -> Dict[str, Any]:
    """Trace + session metrics for the explorer's "Details" tab.

    Consolidates the numbers that used to live on the standalone Sessions page:
    trace-level cost/latency/tokens (``traces_replacing``), the linked
    chat-session metadata (ai_chat_sessions), and a per-tool call/error rollup
    (ClickHouse spans). Every section is best-effort — a missing source degrades
    to nulls, not an error, so the tab always renders."""
    trace = ch_get_trace(trace_id)

    duration_ms: Optional[int] = None
    if trace and trace.get("startTime") and trace.get("endTime"):
        try:
            from datetime import datetime
            s = datetime.fromisoformat(trace["startTime"].replace("Z", "+00:00"))
            e = datetime.fromisoformat(trace["endTime"].replace("Z", "+00:00"))
            duration_ms = max(0, int((e - s).total_seconds() * 1000))
        except Exception:
            duration_ms = None

    # --- per-tool rollup from spans -------------------------------------------
    spans = _clickhouse_spans(trace_id)
    cache = _cache_tokens_once(spans)
    cache_read, cache_creation = cache["read"], cache["creation"]

    trace_block = {
        "id": (trace or {}).get("id") or trace_id,
        "status": (trace or {}).get("status"),
        "top_span_name": (trace or {}).get("topSpanName"),
        "duration_ms": duration_ms,
        "input_tokens": (trace or {}).get("inputTokens"),
        "output_tokens": (trace or {}).get("outputTokens"),
        "total_cost": (trace or {}).get("totalCost"),
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
    }

    session_id = (trace or {}).get("sessionId") or _session_id_from_spans(spans)
    tool_stats: Dict[str, Dict[str, int]] = {}
    error_spans: List[str] = []
    for s in spans:
        name = s.get("name") or ""
        is_err = bool(int(s.get("has_error") or 0))
        if name.endswith(".tool"):
            st = tool_stats.setdefault(name[: -len(".tool")], {"calls": 0, "errors": 0})
            st["calls"] += 1
            if is_err:
                st["errors"] += 1
        if is_err:
            error_spans.append(name)
    tools = [
        {"name": n, "calls": v["calls"], "errors": v["errors"]}
        for n, v in sorted(tool_stats.items(), key=lambda kv: kv[1]["calls"], reverse=True)
    ]

    # --- linked chat session --------------------------------------------------
    session_block: Optional[Dict[str, Any]] = None
    if session_id:
        try:
            if env:
                from admin.router import client_for
                sess_client = client_for(env)
            else:
                sess_client = admin_service_client()
            rows = (
                sess_client.schema("public").table("ai_chat_sessions")
                # Same raw→coerced mapping as the transcript reader: the trace
                # carries the raw session id, the row is keyed on the coerced one.
                .select("id, agent_kind, business_id, branch_id, title, message_count, metadata, created_at")
                .eq("id", _coerce_session_id_for_lookup(session_id)).limit(1).execute().data or []
            )
            if rows:
                r = rows[0]
                meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
                session_block = {
                    "id": r.get("id"),
                    "agent_kind": r.get("agent_kind"),
                    "business_id": r.get("business_id"),
                    "branch_id": r.get("branch_id"),
                    "title": r.get("title"),
                    # The stored column is never maintained: count the messages.
                    "message_count": _count_messages(sess_client, r),
                    "locale": meta.get("locale"),
                    "source": meta.get("source"),
                    "created_at": r.get("created_at"),
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("trace overview session fetch failed for %s: %s", session_id, exc)

    found, unavailable = _trace_found(trace_id, trace, spans)
    notice = None
    if not found:
        # Never a zero-filled record for a conversation that isn't there.
        trace_block = None
        notice = ("Conversation details are temporarily unavailable." if unavailable
                  else "We couldn't find this conversation.")
    return {
        "trace": trace_block,
        "session": session_block,
        "tools": tools,
        "tool_calls": sum(t["calls"] for t in tools),
        "errors": error_spans,
        "found": found,
        "unavailable": unavailable,
        "notice": notice,
    }


def _recordings_client(env: Optional[str]) -> Any:
    if env:
        from admin.router import client_for
        return client_for(env)
    return admin_service_client()


def _session_recordings(lookup_id: str, candidate_envs: List[Optional[str]],
                        trace_call_id: Optional[str]) -> List[Dict[str, Any]]:
    """Every call recording of one chat session, newest first, from the
    first of `candidate_envs` that has any. `trace_call_id` flags the
    recording of the call being viewed. Best-effort: [] on failure."""
    rows: List[Dict[str, Any]] = []
    found_env: Optional[str] = None
    for e in candidate_envs:
        try:
            rows = (
                _recordings_client(e)
                .table("ai_voice_recordings")
                .select(
                    "recording_id, call_id, provider, simulated, "
                    "sample_rate, duration_ms, size_bytes, status, note, started_at"
                )
                .eq("session_id", lookup_id)
                .order("started_at", desc=True)
                .limit(50)
                .execute()
                .data or []
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("voice audio fetch failed for %s in env=%s: %s",
                         lookup_id, e, exc)
            rows = []
        if rows:
            found_env = e
            break

    recordings: List[Dict[str, Any]] = []
    for r in rows:
        rid = r.get("recording_id")
        url = f"/admin/observability/explorer/recording/{rid}.wav"
        if found_env:
            url += f"?env={found_env}"
        recordings.append({
            "id": rid,
            "call_id": r.get("call_id"),
            # Plain name for the realtime provider slug ("google_genai" →
            # "Gemini Live"); an unknown slug shows nothing rather than raw.
            "provider": _VOICE_PROVIDER_LABELS.get(str(r.get("provider") or "")),
            "simulated": bool(r.get("simulated")),
            "duration_ms": r.get("duration_ms"),
            "size_bytes": r.get("size_bytes"),
            "status": r.get("status"),
            "note": r.get("note"),
            "started_at": r.get("started_at"),
            "is_current_trace": bool(trace_call_id) and r.get("call_id") == trace_call_id,
            "url": url if r.get("status") == "complete" else None,
        })

    return recordings


def load_voice_audio_json(trace_id: str, env: Optional[str] = None) -> Dict[str, Any]:
    """Call recordings for the explorer's "Audio" tab.

    A voice call appears in the trace list as N ``voice.agent_turn`` traces
    grouped under one session, while its audio is recorded per CALL
    (``ai_voice_recordings``, one row per websocket connection — see
    ``voice/live/recorder.py``). So this resolves the trace's session id the
    same way the Transcript tab does and returns every recording of that
    conversation, flagging the one whose ``call_id`` matches this trace's
    ``waj.voice.call_id`` metadata. Best-effort throughout — a missing source
    degrades to an empty list + notice, never an error."""
    trace = ch_get_trace(trace_id)

    spans = _clickhouse_spans(trace_id)
    session_id = (trace or {}).get("sessionId") or _session_id_from_spans(spans)
    meta = _trace_meta_attrs(trace or {})
    trace_call_id = meta.get("waj.voice.call_id")
    lookup_id = _coerce_session_id_for_lookup(session_id)
    if not lookup_id:
        return {
            "trace": {"id": trace_id}, "session_id": None, "recordings": [],
            "notice": "No chat session is linked to this trace, so there is no "
                      "call audio to look up.",
        }

    # Same cross-env resolution as the Transcript tab: recordings live in the
    # env the call ran in, not necessarily the one the operator is browsing.
    trace_env = meta.get("waj.env") or _env_from_spans(spans)
    candidate_envs: List[Optional[str]] = []
    for e in (trace_env, env, "prod", "staging", None):
        if e not in candidate_envs:
            candidate_envs.append(e)

    recordings = _session_recordings(lookup_id, candidate_envs, trace_call_id)

    notice = None
    if not recordings:
        notice = (
            "No call recordings for this conversation. Recordings exist only "
            "for /voice/live calls made while VOICE_RECORDING_ENABLED was on, "
            "and are pruned after the retention window."
        )
    return {
        "trace": {"id": (trace or {}).get("id") or trace_id},
        "session_id": session_id,
        "recordings": recordings,
        "notice": notice,
    }


def load_recording_wav(
    recording_id: str, env: Optional[str] = None
) -> Tuple[Optional[bytes], Optional[Dict[str, Any]]]:
    """Reassemble one recording's WAV from its base64 chunks.

    Returns ``(wav_bytes, row)`` or ``(None, None)`` when the recording is
    missing, has no audio, or the DB is unreachable. Chunks are concatenated
    in ``seq`` order — chunk 0 starts with the RIFF header, so the join IS the
    file (see the chunks table comment in the migration)."""
    try:
        client = _recordings_client(env)
        rows = (
            client.table("ai_voice_recordings")
            .select("recording_id, status, chunk_count, duration_ms, sample_rate")
            .eq("recording_id", recording_id)
            .limit(1)
            .execute()
            .data or []
        )
        if not rows or rows[0].get("status") != "complete":
            return None, None
        chunks = (
            client.table("ai_voice_recording_chunks")
            .select("seq, audio_b64")
            .eq("recording_id", recording_id)
            .order("seq")
            .execute()
            .data or []
        )
        # Belt-and-braces against a torn read: `complete` is only set after
        # the last chunk lands (voice/live/recorder.py::_persist), but serving
        # fewer chunks than the row declares would cache a truncated file in
        # the operator's browser for a day.
        declared = int(rows[0].get("chunk_count") or 0)
        if declared and len(chunks) != declared:
            logger.warning(
                "recording %s: %d/%d chunks present; refusing to serve a torn file",
                recording_id, len(chunks), declared,
            )
            return None, None
        wav = b"".join(
            base64.b64decode(c.get("audio_b64") or "")
            for c in sorted(chunks, key=lambda c: int(c.get("seq") or 0))
        )
        return (wav or None), rows[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("recording wav fetch failed for %s: %s", recording_id, exc)
        return None, None


def list_tool_names_from_db(env: Optional[str] = None) -> List[str]:
    """Pull distinct tool python_names from the agent_tools table.

    Used to populate the explorer's tool-filter dropdown. Returns [] if
    the DB isn't reachable — the dropdown then just renders empty rather
    than blocking the page. When ``env`` is given, reads from that
    environment's branch via ``client_for`` so the dropdown matches the
    selected env; otherwise falls back to the default service client.
    """
    try:
        if env is not None:
            from admin.router import client_for
            client = client_for(env)
        else:
            client = admin_service_client()
        resp = (
            client.table("agent_tools")
            .select("python_name, agent_kind, additional_agent_kinds")
            .order("python_name")
            .execute()
        )
        from admin.agent_kinds import scope_to_build

        seen: List[str] = []
        # A partner build filters by its own agents' tools only.
        for row in scope_to_build(resp.data or []):
            name = (row.get("python_name") or "").strip()
            if name and name not in seen:
                seen.append(name)
        return seen
    except Exception as exc:
        logger.warning("list_tool_names_from_db failed: %s", exc)
        return []


def _tool_match_sql(trace_ids: List[str]) -> str:
    """SQL finding which of ``trace_ids`` contain a span for ``{tool:String}``.

    The ids are inlined rather than parameterised, which is safe **by
    construction**: the caller passes them through ``uuid.UUID()`` first, so a
    value that isn't a canonical UUID never reaches here. (Array params would
    have to be stringified by ``core.lmnr_clickhouse.query``, which only handles
    scalars.) The caller-supplied tool name — the one genuinely untrusted value —
    travels as a param."""
    in_list = ", ".join("toUUID('%s')" % tid for tid in trace_ids)
    return (
        "SELECT DISTINCT toString(trace_id) AS trace_id\n"
        "FROM spans\n"
        "WHERE trace_id IN (%s)\n" % in_list
        + "  AND (name = {tool:String}\n"
          "       OR name = concat({tool:String}, '.tool')\n"
          "       OR simpleJSONExtractString(attributes, 'gen_ai.tool.name') = {tool:String}\n"
          "       OR simpleJSONExtractString(attributes, 'lmnr.span.tool_name') = {tool:String}\n"
          "       OR simpleJSONExtractString(attributes, 'traceloop.entity.name') = {tool:String})\n"
    )


def tool_filter_notice(tool_name: str) -> str:
    """Banner text for a tool filter that could not be applied.

    Without this the failure is invisible: the list comes back unnarrowed while
    the picker still shows the tool as selected, so the page reads as "every
    trace in this window called `tool_name`". During forensics that is a wrong
    answer, not a degraded one."""
    return (
        "Tool filter '%s' could NOT be applied — the ClickHouse match query "
        "failed, so this list is EVERY trace in the window, not only those that "
        "called it. Do not read it as a filtered result. "
        "Check LMNR_CLICKHOUSE_URL / credentials (see Telemetry diagnose)."
        % tool_name
    )


def filter_traces_by_tool(
    traces: List[Dict[str, Any]],
    tool_name: str,
) -> List[Dict[str, Any]]:
    """Rows only — see ``filter_traces_by_tool_result`` for the applied flag.

    Callers that RENDER the result must use the ``_result`` variant: this one
    cannot tell "narrowed" from "the query died and you're looking at
    everything"."""
    return filter_traces_by_tool_result(traces, tool_name)[0]


def filter_traces_by_tool_result(
    traces: List[Dict[str, Any]],
    tool_name: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """``(traces, applied)`` — narrow a trace list to those that called
    `tool_name`.

    One ClickHouse query for the whole page (the previous implementation fanned
    out one HTTP call per trace — ~100 sequential round-trips for a 24h window).

    On failure this returns the list **unnarrowed** with ``applied=False``, not
    empty: an empty result set reads as "no trace used this tool", which is a
    wrong answer rather than a degraded one. Unnarrowed is only *visibly* wrong
    if the caller renders the flag — hence ``tool_filter_notice``.
    """
    if not tool_name:
        return traces, True
    trace_ids: List[str] = []
    for t in traces:
        raw = t.get("id")
        if not raw:
            continue
        try:
            trace_ids.append(str(uuid.UUID(str(raw))))
        except (ValueError, AttributeError, TypeError):
            continue
    if not trace_ids:
        return [], True

    try:
        rows = _ch_trace_query(_tool_match_sql(trace_ids), {"tool": tool_name}) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "observability: tool filter %r unavailable, showing the list "
            "unnarrowed (%s)", tool_name, exc,
        )
        return traces, False
    matched = {r.get("trace_id") for r in rows if r.get("trace_id")}
    return [t for t in traces if str(t.get("id")) in matched], True


# ---------------------------------------------------------------------------
# Live monitor — ClickHouse-derived insights
# ---------------------------------------------------------------------------


def _percentile(sorted_values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile over a pre-sorted list. None when empty."""
    if not sorted_values:
        return None
    idx = max(0, min(len(sorted_values) - 1, int(round(pct * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def _ttft_ttlt_insight(
    *, past_hours: int, env: Optional[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """(ttft, ttlt, ok) p50/p95 over the window, aggregated in ClickHouse.

    The agent stamps ``waj.ttft_ms`` / ``waj.ttlt_ms`` on the root span via
    ``common/sse.py`` as NUMBERS, so they must be read with
    ``simpleJSONExtractFloat`` — ``simpleJSONExtractString`` returns ``''`` for a
    numeric JSON value, which silently zeroed these tiles. ``quantileIf`` is
    gated on ``> 0`` because a missing key extracts as 0 and would drag the
    percentile down. ``ok`` reports whether the query itself ran.
    """
    env_clause, params = _env_sql_clause(env, "attributes")
    params["hours"] = int(past_hours)
    sql = """
SELECT
  quantileIf(0.5)(simpleJSONExtractFloat(attributes, 'waj.ttft_ms'),
                   simpleJSONExtractFloat(attributes, 'waj.ttft_ms') > 0) AS ttft_p50,
  quantileIf(0.95)(simpleJSONExtractFloat(attributes, 'waj.ttft_ms'),
                    simpleJSONExtractFloat(attributes, 'waj.ttft_ms') > 0) AS ttft_p95,
  quantileIf(0.5)(simpleJSONExtractFloat(attributes, 'waj.ttlt_ms'),
                   simpleJSONExtractFloat(attributes, 'waj.ttlt_ms') > 0) AS ttlt_p50,
  quantileIf(0.95)(simpleJSONExtractFloat(attributes, 'waj.ttlt_ms'),
                    simpleJSONExtractFloat(attributes, 'waj.ttlt_ms') > 0) AS ttlt_p95
FROM spans
WHERE start_time >= now() - toIntervalHour({hours:UInt32})
""" + env_clause + """  AND (simpleJSONExtractFloat(attributes, 'waj.ttft_ms') > 0
       OR simpleJSONExtractFloat(attributes, 'waj.ttlt_ms') > 0)
"""
    try:
        rows = _ch_trace_query(sql, params) or []
    except Exception as exc:  # noqa: BLE001 - best-effort, never block the monitor
        logger.debug("load_live_insights: TTFT/TTLT query failed (%s)", exc)
        return None, None, False
    if not rows:
        return None, None, True

    row = rows[0]

    def _positive_int(value: Any) -> Optional[int]:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return int(f) if f > 0 else None

    t50, t95 = _positive_int(row.get("ttft_p50")), _positive_int(row.get("ttft_p95"))
    l50, l95 = _positive_int(row.get("ttlt_p50")), _positive_int(row.get("ttlt_p95"))
    ttft = {"p50_ms": t50, "p95_ms": t95} if (t50 or t95) else None
    ttlt = {"p50_ms": l50, "p95_ms": l95} if (l50 or l95) else None
    return ttft, ttlt, True


def _sessions_count(*, past_hours: int, env: Optional[str]) -> Optional[int]:
    """Distinct ``waj.session_id`` in the window, env-scoped. ``None`` when the
    query couldn't run (``_live_kpis`` then falls back to ``agent_traces``).

    The tile used to count distinct session_id in ``agent_traces``, which has NO
    env column — so switching STAGING/PRODUCTION never changed it. ALL agent
    kinds are counted, background ones included (each of their calls is a distinct
    session): this is total session activity, not just chat. Only the diagnose
    self-test span (never real traffic) is dropped.
    """
    env_clause, params = _env_sql_clause(env, "metadata")
    params["hours"] = int(past_hours)
    diag = ", ".join("'%s'" % s for s in _DIAGNOSTIC_TOP_SPANS)
    sql = (
        "SELECT uniqExact(simpleJSONExtractString(metadata, 'waj.session_id')) AS sessions\n"
        "FROM traces_replacing FINAL\n"
        "WHERE start_time > now() - toIntervalHour({hours:UInt32})\n"
        + env_clause
        + "  AND simpleJSONExtractString(metadata, 'waj.session_id') != ''\n"
        + "  AND top_span_name NOT IN (%s)\n" % diag
    )
    try:
        rows = _ch_trace_query(sql, params) or []
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.debug("load_live_insights: sessions query failed (%s)", exc)
        return None
    if not rows:
        return None
    try:
        return int(rows[0].get("sessions") or 0)
    except (TypeError, ValueError):
        return None


def _top_tools(*, past_hours: int, env: Optional[str]) -> List[Dict[str, Any]]:
    """Tool-call volume + error rate — rows only. ``[]`` on failure.

    Prefer ``_top_tools_result`` inside the Dashboard: an empty list here means
    BOTH "no tool calls in the window" and "the query never ran", and the
    Tool-call success % tile must not certify 100% off the second one."""
    return _top_tools_result(past_hours=past_hours, env=env)[0]


def _top_tools_result(
    *, past_hours: int, env: Optional[str]
) -> Tuple[List[Dict[str, Any]], bool]:
    """``(rows, ok)`` — tool-call volume + error rate over the window.

    ``ok`` is False when the ClickHouse query itself could not run, which is
    the case the rows alone cannot express (see ``_top_tools``).

    Tool invocations are DEFAULT spans named ``<tool>.tool``. Errors come from
    the OTel ``events`` array, never ``spans.status`` (empty on ~all rows).

    ``waj.agent_kind`` / ``waj.env`` live on the trace's tagged ROOT span, not on
    the tool span, so both the env scope and the scoped-build narrow join a
    per-trace aggregate of those attributes — the same ``sagg`` idiom
    ``live_analytics._TOOLS_SQL`` uses, and for the same reason: keying off
    ``top_span_id`` picks an untagged auto-instrumented span whenever the LLM
    spans aren't nested under the request root.
    """
    params: Dict[str, Any] = {"hours": int(past_hours)}
    filters: List[str] = []
    if env:
        # Mirrors _env_sql_clause: prod is strict, staging also admits untagged.
        params["env"] = env
        filters.append(
            "(sagg.env = {env:String} OR sagg.env = '')" if env == "staging"
            else "sagg.env = {env:String}"
        )
    # A scoped build is a single-client service: its tool rollup must not
    # include the other agents' calls. No-op on the full WAJ engine.
    from core.deployment import active_scope

    from admin.screens.live_analytics import agent_kind_clause

    client = active_scope()
    if client is not None:
        clause, kind_params = agent_kind_clause((client.agent_kind, *client.extra_kinds))
        params.update(kind_params)
        filters.append(clause)

    sql = (
        # NB the escaping: this must reach ClickHouse as `'\\.tool$'`, byte for byte
        # the same literal `live_analytics._TOOLS_SQL` sends (a proven-in-prod
        # query). One backslash too many and the two tool rollups are running
        # different regexes while looking identical in source —
        # `test_tool_name_regex_matches_live_analytics` pins them together.
        "SELECT replaceRegexpOne(s.name, '\\.tool$', '') AS name,\n"
        "       count() AS calls,\n"
        "       countIf(arrayExists(x -> x.2 IN ('exception', 'error'), s.events)) AS errors\n"
        "FROM spans AS s\n"
        "INNER JOIN (\n"
        "  SELECT trace_id,\n"
        "         maxIf(simpleJSONExtractString(attributes, 'waj.agent_kind'),\n"
        "               simpleJSONExtractString(attributes, 'waj.agent_kind') != '') AS agent_kind,\n"
        "         maxIf(simpleJSONExtractString(attributes, 'waj.env'),\n"
        "               simpleJSONExtractString(attributes, 'waj.env') != '') AS env\n"
        "  FROM spans\n"
        "  WHERE start_time >= now() - toIntervalHour({hours:UInt32})\n"
        "  GROUP BY trace_id\n"
        ") AS sagg ON sagg.trace_id = s.trace_id\n"
        "WHERE s.name LIKE '%.tool'\n"
        "  AND s.start_time >= now() - toIntervalHour({hours:UInt32})\n"
        + "".join("  AND " + f + "\n" for f in filters)
        + "GROUP BY name\n"
        "ORDER BY calls DESC\n"
        "LIMIT 10\n"
    )
    try:
        rows = _ch_trace_query(sql, params) or []
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.debug("load_live_insights: top-tools query failed (%s)", exc)
        return [], False
    out: List[Dict[str, Any]] = []
    for row in rows:
        name = row.get("name")
        calls = _as_int(row.get("calls"))
        errors = _as_int(row.get("errors"))
        if not name or not calls:
            continue
        out.append({
            "name": name,
            "calls": calls,
            "errors": errors,
            "error_rate_pct": round(100 * errors / calls, 1),
        })
    return out, True


def load_live_insights(*, past_hours: int = 1, env: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate trace telemetry for the Live Monitor. ClickHouse only.

    ``env`` scopes every rollup to traces tagged with that ``waj.env``
    (staging / prod), so the KPI tiles change when the operator switches
    environment — matching the charts, which already filter by env. ``None``
    aggregates all envs.

    Returns a dict the live.html template renders directly::

        {
          "lmnr_ok": bool,            # False when ClickHouse isn't reachable
          "notice": str | None,
          "window_hours": int,
          "error_traces": [           # recent error traces, newest first
            {"id", "top_span_name", "agent_kind", "started", "duration_ms"}, ...
          ],
          "tokens": {"in_1h", "out_1h", "in_24h", "out_24h"},
          "top_tools": [              # by call volume, desc
            {"name", "calls", "errors", "error_rate_pct"}, ...
          ],
          "latency": {"p50_ms", "p95_ms"},
          "active_agents": [          # agent_kinds with traffic in the window
            {"kind", "color", "traces"}, ...
          ],
          "sessions_24h": int,        # present only when its query succeeded
          "ttft"/"ttlt": {"p50_ms", "p95_ms"},   # same
          "sources": {              # which of the five reads actually answered
            "traces_recent", "traces_24h", "sessions", "ttft", "top_tools"
          },
          "degraded": bool,         # at least one source failed
        }

    **Independent queries run independently.** Each block below is its own
    ClickHouse read, and one failing must not strand the others: an early return
    on the trace-list read used to skip the TTFT and session-count queries
    entirely, which is why five Dashboard KPI tiles rendered "—" while the data
    they needed was sitting in ClickHouse.

    **…and a partial failure says so.** ``lmnr_ok`` is an OR across the reads,
    so it stays True while a source is dead — which is why ``sources`` exists.
    Anything rendering off this dict must consult it: a tile whose source failed
    shows a dash, never ``0``, and the error-traces card must not claim "no error
    traces" when the query that would have found them never ran.
    """
    result: Dict[str, Any] = {
        "lmnr_ok": False,
        "notice": None,
        "window_hours": past_hours,
        "error_traces": [],
        "tokens": {"in_1h": 0, "out_1h": 0, "in_24h": 0, "out_24h": 0},
        "top_tools": [],
        "latency": {"p50_ms": None, "p95_ms": None},
        "active_agents": [],
        "sources": {k: False for k in _LIVE_SOURCE_LABELS},
        "degraded": True,
    }

    # --- TTFT / TTLT + distinct sessions (own queries; run FIRST) ------------
    ttft, ttlt, ttft_ok = _ttft_ttlt_insight(past_hours=past_hours, env=env)
    sessions_24h = _sessions_count(past_hours=past_hours, env=env)

    # --- trace rollups -------------------------------------------------------
    # `load_clickhouse_traces` already excludes the background/diagnostic/
    # voice-metric classes and env-scopes server-side; None means the query
    # itself couldn't run.
    recent_items = load_clickhouse_traces(
        past_hours=past_hours, page_size=200, env=env,
        include_background=False, status=None,
    )
    day_items = load_clickhouse_traces(
        past_hours=24, page_size=1000, env=env,
        include_background=False, status=None,
    )
    # Tracked SEPARATELY, not collapsed into one `traces_ok`: the two windows
    # feed different tiles (recent → error list / latency / active agents; 24h →
    # token spend), so a 1000-row 24h timeout with a healthy 1h read must blank
    # the spend tile alone, not everything and not nothing.
    recent_ok = recent_items is not None
    day_ok = day_items is not None
    traces_ok = recent_ok or day_ok
    recent_items = recent_items or []
    day_items = day_items or []

    # A scoped build  is a standalone
    # single-client service — every rollup here must reflect ONLY that client's
    # traces. Annotate both windows with agent_kind (a cheap in-memory metadata
    # read, see _annotate_trace_channel_meta) then narrow. Run unconditionally:
    # filter_trace_rows_to_client is itself a no-op when no deployment scope is
    # active, so full-WAJ behaviour is unchanged. Gating this on a check for
    # one scope by name used to silently skip scoping for every OTHER
    # deployment scope, leaking the full multi-agent trace set into
    # what should be a single-tenant rollup.
    from admin.seed_agent_profiles import filter_trace_rows_to_client
    _annotate_trace_channel_meta(recent_items)
    _annotate_trace_channel_meta(day_items)
    recent_items = filter_trace_rows_to_client(recent_items)
    day_items = filter_trace_rows_to_client(day_items)

    # The SQL admits env-less traces for either env so a mis-tagged
    # conversational trace is never hidden from the LIST; the tiles want the
    # stricter reading (prod counts only prod), so narrow again in memory.
    if env:
        recent_items = [t for t in recent_items if _item_env_matches(t, env)]
        day_items = [t for t in day_items if _item_env_matches(t, env)]

    def _sum_tokens(items: List[Dict[str, Any]]) -> Dict[str, int]:
        return {
            "in": sum(_as_int(t.get("inputTokens")) for t in items),
            "out": sum(_as_int(t.get("outputTokens")) for t in items),
        }

    tok_1h = _sum_tokens(recent_items)
    tok_24h = _sum_tokens(day_items)

    def _dur_ms(t: Dict[str, Any]) -> Optional[int]:
        ms = t.get("duration_ms")
        if isinstance(ms, int):
            return ms
        start, end = t.get("startTime") or "", t.get("endTime") or ""
        try:
            from datetime import datetime
            return int(
                (datetime.fromisoformat(end.replace("Z", "+00:00"))
                 - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() * 1000
            )
        except Exception:  # noqa: BLE001
            return None

    durations = sorted(d for d in (_dur_ms(t) for t in recent_items) if d is not None)
    p50 = _percentile(durations, 0.50)
    p95 = _percentile(durations, 0.95)

    error_traces: List[Dict[str, Any]] = []
    agent_counts: Dict[str, int] = {}
    for t in recent_items:
        kind = t.get("agent_kind")
        if kind:
            agent_counts[kind] = agent_counts.get(kind, 0) + 1
        if t.get("status") == "error":
            error_traces.append({
                "id": t.get("id"),
                "top_span_name": t.get("topSpanName") or "—",
                "agent_kind": kind,
                "started": (t.get("startTime") or "")[:19],
                "duration_ms": _dur_ms(t),
            })

    result["error_traces"] = error_traces
    result["active_agents"] = [
        {"kind": k, "color": agent_color(k), "traces": n}
        for k, n in sorted(agent_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    result["tokens"] = {
        "in_1h": tok_1h["in"], "out_1h": tok_1h["out"],
        "in_24h": tok_24h["in"], "out_24h": tok_24h["out"],
    }
    result["latency"] = {
        "p50_ms": int(p50) if p50 is not None else None,
        "p95_ms": int(p95) if p95 is not None else None,
    }

    # --- top tools (own query) ----------------------------------------------
    result["top_tools"], tools_ok = _top_tools_result(past_hours=past_hours, env=env)

    if sessions_24h is not None:
        result["sessions_24h"] = sessions_24h
    if ttft is not None:
        result["ttft"] = ttft
    if ttlt is not None:
        result["ttlt"] = ttlt

    # `lmnr_ok` gates the error-traces card + the KPI tiles in `_live_kpis`. It
    # means "ClickHouse answered at least one of these reads", so a single slow
    # query can't blank the whole dashboard.
    result["lmnr_ok"] = bool(traces_ok or ttft_ok or sessions_24h is not None)
    # …which is exactly why the per-source map has to travel with it: `lmnr_ok`
    # True does NOT mean every number on the page is real.
    result["sources"] = {
        "traces_recent": recent_ok,
        "traces_24h": day_ok,
        "sessions": sessions_24h is not None,
        "ttft": ttft_ok,
        "top_tools": tools_ok,
    }
    failed = [k for k, ok in result["sources"].items() if not ok]
    result["degraded"] = bool(failed)
    if not result["lmnr_ok"]:
        result["notice"] = _ch_notice()
    elif failed:
        result["notice"] = _partial_ch_notice(failed)
    return result


def load_voice_live_insights(
    *, past_hours: int = 24, env: Optional[str] = None
) -> Dict[str, Any]:
    """Duplex-voice latency rollup for the Dashboard's voice card.

    Aggregates the `waj.voice.live.*` span attributes emitted by
    `voice/live/metrics.py` (same numbers-as-span-attributes pattern as
    `waj.ttft_ms` above; see the design spec
    `docs/superpowers/specs/2026-07-30-voice-live-latency-metrics-design.md`):

    - ``eot_first_audio`` — caller end-of-turn → first audio delta, the
      headline responsiveness number (turn spans).
    - ``answer`` — the user EOT that asked a data question → first spoken
      ask_agent result (tool spans, ``eot_to_result_ms``).
    - ``delegate`` — `run_delegated_turn` wall time (tool spans).
    - ``brain_llm`` — total brain-LLM wall time inside the delegated turn
      (``waj.voice.live.stage.llm_ms``, stamped on the ``voice.agent_turn``
      root span by voice/live/delegate.py — those spans carry no
      ``waj.voice.live.op``, hence the widened name filter below). Read
      against ``delegate``: the gap between the two is
      history/memory/prefetch/tool time (the other ``stage.*`` attributes,
      per-trace in the span detail pane).
    - counts: sessions / assistant turns / tool calls / barge-ins.

    All aggregation is server-side in ClickHouse (`quantileIf` over
    `simpleJSONExtractFloat`), env-scoped via ``waj.env`` on span attributes.
    Best-effort like every other Live Monitor loader: any ClickHouse failure
    returns an empty-but-shaped dict with ``ch_ok: False`` and a notice, and
    the card renders dashes instead of the page 500-ing.
    """
    empty: Dict[str, Any] = {
        "ch_ok": False,
        "notice": None,
        "window_hours": past_hours,
        "sessions": 0,
        "turns": 0,
        "tool_calls": 0,
        "barge_ins": 0,
        "eot_first_audio": {"p50_ms": None, "p95_ms": None},
        "answer": {"p50_ms": None, "p95_ms": None},
        "delegate": {"p50_ms": None, "p95_ms": None},
        "brain_llm": {"p50_ms": None},
    }

    env_clause, params = _env_sql_clause(env, "attributes")
    params["hours"] = int(past_hours)
    # One pass over the window's voice.live.* spans. Each metric is read with
    # simpleJSONExtractFloat and quantileIf-gated on > 0 — the ttft tile's
    # lesson: simpleJSONExtractString returns '' for numeric JSON values, and
    # an ungated quantile would average in the rows that don't carry the
    # attribute at all (extract returns 0 for a missing key).
    op = "simpleJSONExtractString(attributes, 'waj.voice.live.op')"
    sql = (
        "SELECT\n"
        f"  countIf({op} = 'session') AS sessions,\n"
        f"  countIf({op} = 'turn') AS turns,\n"
        f"  countIf({op} = 'tool') AS tool_calls,\n"
        "  sumIf(simpleJSONExtractFloat(attributes, 'waj.voice.live.interrupted'),\n"
        f"        {op} = 'turn') AS barge_ins,\n"
        "  quantileIf(0.5)(simpleJSONExtractFloat(attributes, 'waj.voice.live.eot_to_first_audio_ms'),\n"
        "                  simpleJSONExtractFloat(attributes, 'waj.voice.live.eot_to_first_audio_ms') > 0) AS eot_p50,\n"
        "  quantileIf(0.95)(simpleJSONExtractFloat(attributes, 'waj.voice.live.eot_to_first_audio_ms'),\n"
        "                   simpleJSONExtractFloat(attributes, 'waj.voice.live.eot_to_first_audio_ms') > 0) AS eot_p95,\n"
        "  quantileIf(0.5)(simpleJSONExtractFloat(attributes, 'waj.voice.live.eot_to_result_ms'),\n"
        "                  simpleJSONExtractFloat(attributes, 'waj.voice.live.eot_to_result_ms') > 0) AS answer_p50,\n"
        "  quantileIf(0.95)(simpleJSONExtractFloat(attributes, 'waj.voice.live.eot_to_result_ms'),\n"
        "                   simpleJSONExtractFloat(attributes, 'waj.voice.live.eot_to_result_ms') > 0) AS answer_p95,\n"
        "  quantileIf(0.5)(simpleJSONExtractFloat(attributes, 'waj.voice.live.delegate_ms'),\n"
        "                  simpleJSONExtractFloat(attributes, 'waj.voice.live.delegate_ms') > 0) AS delegate_p50,\n"
        "  quantileIf(0.95)(simpleJSONExtractFloat(attributes, 'waj.voice.live.delegate_ms'),\n"
        "                   simpleJSONExtractFloat(attributes, 'waj.voice.live.delegate_ms') > 0) AS delegate_p95,\n"
        "  quantileIf(0.5)(simpleJSONExtractFloat(attributes, 'waj.voice.live.stage.llm_ms'),\n"
        "                  simpleJSONExtractFloat(attributes, 'waj.voice.live.stage.llm_ms') > 0) AS brain_llm_p50\n"
        "FROM spans\n"
        "WHERE start_time >= now() - toIntervalHour({hours:UInt32})\n"
        + env_clause
        # `voice.agent_turn` root spans carry the stage.* attributes but no
        # `waj.voice.live.op` — widened by NAME rather than dropping the op
        # gate so the scan stays bounded to voice rows. The op-keyed countIf/
        # quantileIf aggregates above are unaffected (op is '' on these rows).
        + f"  AND ({op} != '' OR name = 'voice.agent_turn')\n"
    )

    try:
        rows = _ch_trace_query(sql, params)
    except Exception as exc:  # noqa: BLE001 - CH down/misconfigured is expected
        empty["notice"] = f"ClickHouse unavailable: {exc}"
        return empty
    if not rows:
        empty["ch_ok"] = True
        return empty

    row = rows[0]

    def _int_or_none(value: Any) -> Optional[int]:
        try:
            f = float(value)
            return int(f) if f > 0 else None
        except (TypeError, ValueError):
            return None

    def _count(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    return {
        "ch_ok": True,
        "notice": None,
        "window_hours": past_hours,
        "sessions": _count(row.get("sessions")),
        "turns": _count(row.get("turns")),
        "tool_calls": _count(row.get("tool_calls")),
        "barge_ins": _count(row.get("barge_ins")),
        "eot_first_audio": {
            "p50_ms": _int_or_none(row.get("eot_p50")),
            "p95_ms": _int_or_none(row.get("eot_p95")),
        },
        "answer": {
            "p50_ms": _int_or_none(row.get("answer_p50")),
            "p95_ms": _int_or_none(row.get("answer_p95")),
        },
        "delegate": {
            "p50_ms": _int_or_none(row.get("delegate_p50")),
            "p95_ms": _int_or_none(row.get("delegate_p95")),
        },
        "brain_llm": {
            "p50_ms": _int_or_none(row.get("brain_llm_p50")),
        },
    }
