"""Live Monitor analytics — aggregation over Laminar trace telemetry.

The data lives in Laminar's ClickHouse (the agent only writes a lean
``agent_traces`` bridge to Postgres now; rich telemetry goes to Laminar). This
module has two layers:

* **Pure builders** (`bucket_series`, `per_agent_rollup`, `cost_share`,
  `latency_histogram`, …) take a list of normalised *trace-row* dicts and shape
  the dashboard payload. No DB, no network — unit-testable in isolation.
* **`load_analytics`** does the I/O: it runs a small set of read-only SQL
  queries against ClickHouse (via `core.lmnr_clickhouse`), maps the results into
  trace-row dicts, and feeds them through the pure builders.

A *trace-row* dict has: ``created_at`` (str), ``agent_kind``, ``channel_slug``,
``input_tokens``, ``output_tokens``, ``cost_usd`` (precomputed by Laminar),
``latency_ms``, ``iteration_count``, ``error`` (bool).

ClickHouse data-model notes (verified against the live stack):
* trace table is ``traces_replacing`` (ReplacingMergeTree → ``FINAL``); cost is
  precomputed in ``total_cost``; ``duration`` is **seconds**.
* ``waj.agent_kind`` / ``waj.channel.slug`` live on the **top span's**
  attributes, reached by joining ``spans`` on ``top_span_id``.
* ``status`` is empty everywhere — error detection uses OTel ``exception``
  events on a trace's spans.
* tool calls are DEFAULT spans named ``<tool>.tool``; agentic iterations are
  LLM spans (``span_type = 1``).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.lmnr_clickhouse import ClickHouseUnavailable
from core.lmnr_clickhouse import query as ch_query

logger = logging.getLogger(__name__)

# model_id -> (cost_per_1k_in, cost_per_1k_out) — only used as a fallback when a
# row has no precomputed cost_usd (Laminar always provides one, so this is dead
# weight in the ClickHouse path but keeps the builders reusable/testable).
CostMap = Dict[str, Tuple[float, float]]

# model_id -> the agent_models row (for the pricing layer). Lazily loaded; the
# admin process is long-lived so we refresh on each analytics load (cheap table).
_model_pricing: Dict[str, Dict[str, Any]] = {}


def _load_model_pricing(client=None) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        if client is None:
            from core.auth import get_service_client as _gsc
            client = _gsc()
        rows = client.table("agent_models").select(
            "model_id, model_string, extra_config"
        ).execute().data or []
        for r in rows:
            out[r["model_id"]] = r
    except Exception:  # pragma: no cover - defensive
        pass
    return out


# Stable agent ordering for stacked/breakdown charts. Mirrors AGENT_PALETTE in
# admin.screens.observability and AGENT_COLORS in static/live-charts.js.
from admin.agent_kinds import catalog_tuple as _catalog_tuple  # noqa: E402

AGENT_KINDS = _catalog_tuple("chart_kinds")


def _chart_agent_kinds() -> tuple:
    """Agent kinds the stacked volume chart pre-seeds/orders. Narrowed to the
    active deployment scope's own agent_kind on any scoped
    build; otherwise the full WAJ list (unchanged behavior). Empty series are
    dropped downstream regardless, so this only affects ordering + which
    zero-rows the chart reserves.

    It once checked for one particular scope by name and fell through to the
    full list on every other scope, a list that does not contain that scope's
    kind: its traces were pre-seeded under kinds they never matched and the
    stacked chart rendered as if there were zero traffic.
    """
    from core.deployment import active_scope

    client = active_scope()
    return (client.agent_kind, *client.extra_kinds) if client is not None else AGENT_KINDS

# Cap rows pulled for one window so a runaway query can't blow up memory.
_ROW_LIMIT = 100_000


# --------------------------------------------------------------------------- #
# Pure builders
# --------------------------------------------------------------------------- #
def choose_bucket(hours: int) -> timedelta:
    """Pick a bucket width that yields a readable number of points."""
    if hours <= 6:
        return timedelta(minutes=5)
    if hours <= 168:  # <= 7 days
        return timedelta(hours=1)
    return timedelta(days=1)


def percentile(values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile. Sorts a copy; returns None when empty."""
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(pct * (len(s) - 1)))))
    return s[idx]


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-ish timestamp, tolerating ClickHouse DateTime64(9).

    ClickHouse renders ``2026-05-21 10:59:56.067467000`` (space separator, nine
    fractional digits) which `datetime.fromisoformat` rejects — truncate the
    fraction to microseconds first.
    """
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)
    if m:
        base, frac, tz = m.group(1), m.group(2), m.group(3)
        s = base + (("." + frac[:6]) if frac else "") + tz
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def cost_for_row(row: Dict[str, Any], cost_map: CostMap) -> float:
    """USD cost of one trace. Precedence: the pricing layer (tokencost auto, or
    manual rates) for a known model_id; then Laminar's precomputed ``cost_usd``;
    then the legacy cost_map rates."""
    from core import pricing

    model = _model_pricing.get(row.get("model_id"))
    if model is not None:
        priced = pricing.cost_usd(
            model, row.get("input_tokens") or 0, row.get("output_tokens") or 0
        )
        if priced is not None:
            return float(priced)
    precomputed = row.get("cost_usd")
    if precomputed is not None:
        return float(precomputed)
    in_rate, out_rate = cost_map.get(row.get("model_id"), (0.0, 0.0))
    in_tok = row.get("input_tokens") or 0
    out_tok = row.get("output_tokens") or 0
    return in_tok / 1000 * in_rate + out_tok / 1000 * out_rate


def estimate_cache_savings_usd(client, cache_read_tokens: int) -> float:
    """USD saved by cache-read tokens, priced with the primary active model's
    cache-read discount. The Live Monitor KPI and the caching_analytics MCP
    tool both call this so the savings figure is computed one way. Returns the
    raw (unrounded) estimate — callers round for display so a truthiness check
    (`saved if saved else "—"`) sees a sub-cent, still-nonzero value as present.
    Best-effort: returns 0.0 when no active model can be priced, never raises."""
    from core import pricing

    try:
        rows = (client.table("agent_models").select("model_string, extra_config")
                .eq("is_active", True).limit(1).execute().data or [])
        primary = rows[0] if rows else None
    except Exception:
        primary = None
    if not primary:
        return 0.0
    return pricing.cache_savings_usd(primary, int(cache_read_tokens or 0))


def _bucket_start(ts: datetime, bucket: timedelta, origin: datetime) -> datetime:
    """Floor `ts` to the bucket grid anchored at `origin`."""
    delta = (ts - origin).total_seconds()
    n = int(delta // bucket.total_seconds())
    return origin + n * bucket


def bucket_series(
    rows: List[Dict[str, Any]],
    bucket: timedelta,
    cost_map: CostMap,
) -> Dict[str, Any]:
    """Bucket rows into an ordered time series.

    Returns ``{"buckets": [iso, ...], "series": {metric: [...]},
    "volume_by_agent": {kind: [...]}}``.
    """
    parsed = [(_parse_ts(r.get("created_at")), r) for r in rows]
    parsed = [(t, r) for t, r in parsed if t is not None]
    if not parsed:
        return {"buckets": [], "series": _empty_series(), "volume_by_agent": {}}

    origin = min(t for t, _ in parsed)
    groups: Dict[datetime, List[Dict[str, Any]]] = {}
    for t, r in parsed:
        groups.setdefault(_bucket_start(t, bucket, origin), []).append(r)

    keys = sorted(groups)
    buckets = [k.isoformat() for k in keys]
    series: Dict[str, List[Any]] = _empty_series()
    _chart_kinds = _chart_agent_kinds()
    vol_by_agent: Dict[str, List[int]] = {k: [] for k in _chart_kinds}

    for k in keys:
        bucket_rows = groups[k]
        n = len(bucket_rows)
        errs = sum(1 for r in bucket_rows if r.get("error"))
        lats = [r["latency_ms"] for r in bucket_rows if r.get("latency_ms") is not None]
        iters = [r["iteration_count"] for r in bucket_rows if r.get("iteration_count") is not None]
        series["volume"].append(n)
        _tin = sum(r.get("input_tokens") or 0 for r in bucket_rows)
        _cr = sum(r.get("cache_read_tokens") or 0 for r in bucket_rows)
        series["tokens_in"].append(_tin)
        series["tokens_cached"].append(_cr)
        series["tokens_uncached"].append(max(0, _tin - _cr))
        series["tokens_out"].append(sum(r.get("output_tokens") or 0 for r in bucket_rows))
        series["cost_usd"].append(round(sum(cost_for_row(r, cost_map) for r in bucket_rows), 4))
        series["p50_ms"].append(percentile(lats, 0.50))
        series["p95_ms"].append(percentile(lats, 0.95))
        series["error_rate"].append(round(100 * errs / n, 1) if n else 0.0)
        series["avg_iters"].append(round(sum(iters) / len(iters), 2) if iters else 0.0)
        ttfts = [r["ttft_ms"] for r in bucket_rows if r.get("ttft_ms") is not None]
        ttlts = [r["ttlt_ms"] for r in bucket_rows if r.get("ttlt_ms") is not None]
        series["ttft_p50"].append(percentile(ttfts, 0.50))
        series["ttft_p95"].append(percentile(ttfts, 0.95))
        series["ttlt_p50"].append(percentile(ttlts, 0.50))
        series["ttlt_p95"].append(percentile(ttlts, 0.95))
        per_kind: Dict[Any, int] = {}
        for r in bucket_rows:
            kind = r.get("agent_kind")
            per_kind[kind] = per_kind.get(kind, 0) + 1
        for kind in _chart_kinds:
            vol_by_agent[kind].append(per_kind.get(kind, 0))

    # Drop agent rows that never appear so the stacked chart stays tidy.
    vol_by_agent = {k: v for k, v in vol_by_agent.items() if any(v)}
    return {"buckets": buckets, "series": series, "volume_by_agent": vol_by_agent}


def per_agent_rollup(rows: List[Dict[str, Any]], cost_map: CostMap) -> List[Dict[str, Any]]:
    """One rollup row per agent_kind present in `rows`, volume desc."""
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_kind.setdefault(r.get("agent_kind") or "unknown", []).append(r)

    out: List[Dict[str, Any]] = []
    for kind, krows in by_kind.items():
        n = len(krows)
        errs = sum(1 for r in krows if r.get("error"))
        lats = [r["latency_ms"] for r in krows if r.get("latency_ms") is not None]
        tin = sum(r.get("input_tokens") or 0 for r in krows)
        cr = sum(r.get("cache_read_tokens") or 0 for r in krows)
        out.append({
            "agent_kind": kind,
            "volume": n,
            "error_rate": round(100 * errs / n, 1) if n else 0.0,
            "tokens_in": sum(r.get("input_tokens") or 0 for r in krows),
            "tokens_out": sum(r.get("output_tokens") or 0 for r in krows),
            "cache_read_tokens": cr,
            "cache_hit_rate": round(100 * cr / tin, 1) if tin else 0.0,
            "cost_usd": round(sum(cost_for_row(r, cost_map) for r in krows), 4),
            "p50_ms": percentile(lats, 0.50),
            "p95_ms": percentile(lats, 0.95),
        })
    out.sort(key=lambda d: d["volume"], reverse=True)
    return out


def cost_share(rows: List[Dict[str, Any]], cost_map: CostMap) -> List[Dict[str, Any]]:
    """Total USD per agent_kind, desc."""
    totals: Dict[str, float] = {}
    for r in rows:
        kind = r.get("agent_kind") or "unknown"
        totals[kind] = totals.get(kind, 0.0) + cost_for_row(r, cost_map)
    share = [{"agent_kind": k, "cost_usd": round(v, 4)} for k, v in totals.items()]
    share.sort(key=lambda d: d["cost_usd"], reverse=True)
    return share


def _format_ms(ms: float) -> str:
    """Render a millisecond bound as a compact label ("500ms" / "5s")."""
    if ms >= 1000:
        secs = ms / 1000
        return (f"{secs:.0f}s" if secs == int(secs) else f"{secs:g}s")
    return f"{ms:g}ms"


def _nice_step(rough: float) -> float:
    """Round a rough bin width up to a "nice" round number (1/2/5 × 10ⁿ).

    Keeps bucket edges readable (100, 250, 500, 1000 …) instead of arbitrary
    fractions, regardless of the actual data range.
    """
    import math

    if rough <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(rough))
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * mag
        if step >= rough:
            return step
    return 10 * mag


# Number of finite (non-outlier) bins the histogram aims for. The data-driven
# range (min .. p95) is divided into this many nice, equal-width bins; anything
# above the cutoff lands in a single open-ended ">cutoff" outlier bucket.
_LATENCY_TARGET_BINS = 8


def latency_histogram(
    rows: List[Dict[str, Any]],
    *,
    cutoff_pct: float = 0.95,
    target_bins: int = _LATENCY_TARGET_BINS,
) -> Dict[str, Any]:
    """Data-driven vertical histogram of latency_ms.

    Bucket edges are derived from the data distribution rather than fixed: the
    main range runs from 0 up to a high percentile (``cutoff_pct``, p95 by
    default), divided into ``target_bins`` *nice* rounded bins. Everything above
    the cutoff is collapsed into a single open-ended outlier bucket ("≥ X"),
    so a few slow traces can't blow the range out and flatten every other bar.

    Returns ``{"labels": [...], "counts": [...], "edges": [...], "outlier": bool,
    "cutoff_ms": float|None}`` where ``labels`` read as round ms/s values plus a
    trailing outlier label, ``edges`` are the finite upper bounds of the main
    bins (the open-ended outlier bucket has no finite edge), and ``counts`` has
    one entry per label (``len(edges) + 1`` when an outlier bucket exists).
    Empty input returns empty lists cleanly.
    """
    vals = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    if not vals:
        return {"labels": [], "counts": [], "edges": [],
                "outlier": False, "cutoff_ms": None}

    hi = percentile(vals, cutoff_pct) or 0.0
    vmax = max(vals)
    # When the whole distribution sits below the cutoff (no real tail), bin the
    # full range and skip the outlier bucket. Otherwise reserve the tail.
    has_outlier = vmax > hi and hi > 0
    span = hi if has_outlier else vmax
    if span <= 0:  # all values identical / zero — single 0-bucket
        return {"labels": [f"{_format_ms(0)}–{_format_ms(0)}"],
                "counts": [len(vals)], "edges": [0.0],
                "outlier": False, "cutoff_ms": None}

    step = _nice_step(span / target_bins)
    edges: List[float] = []
    edge = step
    while edge < span - 1e-9:
        edges.append(edge)
        edge += step
    edges.append(edge)  # final finite edge: top of the main range (≥ span)
    cutoff = edges[-1]

    # Labels for the finite bins, then the open-ended outlier label if needed.
    labels: List[str] = []
    prev = 0.0
    for e in edges:
        labels.append(f"{_format_ms(prev)}–{_format_ms(e)}")
        prev = e
    if has_outlier:
        labels.append(f"≥{_format_ms(cutoff)}")

    counts = [0] * len(labels)
    tail_idx = len(labels) - 1
    for v in vals:
        idx = len(edges)  # would be the outlier bucket
        for i, e in enumerate(edges):
            if v < e:
                idx = i
                break
        if idx >= len(edges):
            idx = tail_idx if has_outlier else len(edges) - 1
        counts[idx] += 1

    return {
        "labels": labels,
        "counts": counts,
        "edges": edges,
        "outlier": has_outlier,
        "cutoff_ms": cutoff if has_outlier else None,
    }


# --------------------------------------------------------------------------- #
# ClickHouse data layer
# --------------------------------------------------------------------------- #
def _empty_series() -> Dict[str, List[Any]]:
    return {k: [] for k in (
        "volume", "tokens_in", "tokens_out", "cost_usd",
        "p50_ms", "p95_ms", "error_rate", "avg_iters",
        "ttft_p50", "ttft_p95", "ttlt_p50", "ttlt_p95",
        "tokens_cached", "tokens_uncached")}


# Per-trace rows. agent_kind/channel/env are read from whichever span in the
# trace actually carries the `waj.*` attributes (the explicit root span opened
# by `core.observability.root_span` / `sse_response`), NOT from `top_span_id`.
# Laminar's `top_span_id` heuristic can pick an auto-instrumented LLM span when
# the agent's LLM spans aren't nested under the request root span (orphaned
# spans → multiple roots), and that span has no `waj.*` attributes — which made
# every such trace show up as `agent_kind=unknown` / unattributable by env.
# Aggregating the attributes per-trace (maxIf over the non-empty value) is
# robust to that: it finds the tagged span wherever it sits in the tree.
# __FILTERS__ is replaced with parameterised AND-clauses (never raw values), and
# ClickHouse {name:Type} placeholders are filled via query params.
_TRACE_SQL = """
SELECT
  toString(t.start_time) AS created_at,
  sagg.agent_kind AS agent_kind,
  sagg.channel_slug AS channel_slug,
  toInt64(t.input_tokens) AS input_tokens,
  toInt64(t.output_tokens) AS output_tokens,
  toInt64(sagg.cache_read_tokens) AS cache_read_tokens,
  toFloat64(t.total_cost) AS cost_usd,
  toFloat64(t.duration) * 1000 AS latency_ms,
  toUInt32(sagg.llm_spans) AS iteration_count,
  toUInt8(sagg.has_error) AS error,
  nullIf(sagg.ttft_ms, 0) AS ttft_ms,
  nullIf(sagg.ttlt_ms, 0) AS ttlt_ms
FROM traces_replacing AS t FINAL
INNER JOIN (
  SELECT trace_id,
         countIf(span_type = 1) AS llm_spans,
         sumIf(simpleJSONExtractUInt(attributes, 'gen_ai.usage.cache_read_input_tokens'),
               span_type = 1) AS cache_read_tokens,
         max(arrayExists(x -> x.2 IN ('exception', 'error'), events)) AS has_error,
         maxIf(simpleJSONExtractString(attributes, 'waj.agent_kind'),
               simpleJSONExtractString(attributes, 'waj.agent_kind') != '') AS agent_kind,
         maxIf(simpleJSONExtractString(attributes, 'waj.channel.slug'),
               simpleJSONExtractString(attributes, 'waj.channel.slug') != '') AS channel_slug,
         maxIf(simpleJSONExtractString(attributes, 'waj.env'),
               simpleJSONExtractString(attributes, 'waj.env') != '') AS env,
         -- ttft/ttlt are stamped as NUMBERS (floats), so simpleJSONExtractString
         -- returns '' for them — must use simpleJSONExtractFloat. maxIf returns
         -- 0 when a trace has no ttft span; nullIf(.,0) above turns that into NULL
         -- so the chart skips it instead of plotting a spurious 0.
         maxIf(simpleJSONExtractFloat(attributes, 'waj.ttft_ms'),
               simpleJSONExtractFloat(attributes, 'waj.ttft_ms') > 0) AS ttft_ms,
         maxIf(simpleJSONExtractFloat(attributes, 'waj.ttlt_ms'),
               simpleJSONExtractFloat(attributes, 'waj.ttlt_ms') > 0) AS ttlt_ms
  FROM spans
  WHERE start_time >= now() - toIntervalHour({hours:UInt32})
  GROUP BY trace_id
) AS sagg ON sagg.trace_id = t.id
WHERE t.start_time >= now() - toIntervalHour({hours:UInt32})
__FILTERS__
ORDER BY created_at DESC
LIMIT {limit:UInt32}
"""

# Tool-call volume: DEFAULT spans named "<tool>.tool", scoped to the same
# window/agent/channel via the same per-trace `waj.*` aggregate as _TRACE_SQL
# (see the note there on why we don't key off `top_span_id`).
_TOOLS_SQL = """
SELECT replaceRegexpOne(s.name, '\\.tool$', '') AS name, count() AS calls
FROM spans AS s
INNER JOIN traces_replacing AS t FINAL ON t.id = s.trace_id
INNER JOIN (
  SELECT trace_id,
         maxIf(simpleJSONExtractString(attributes, 'waj.agent_kind'),
               simpleJSONExtractString(attributes, 'waj.agent_kind') != '') AS agent_kind,
         maxIf(simpleJSONExtractString(attributes, 'waj.channel.slug'),
               simpleJSONExtractString(attributes, 'waj.channel.slug') != '') AS channel_slug,
         maxIf(simpleJSONExtractString(attributes, 'waj.env'),
               simpleJSONExtractString(attributes, 'waj.env') != '') AS env
  FROM spans
  WHERE start_time >= now() - toIntervalHour({hours:UInt32})
  GROUP BY trace_id
) AS sagg ON sagg.trace_id = t.id
WHERE s.name LIKE '%.tool'
  AND t.start_time >= now() - toIntervalHour({hours:UInt32})
__FILTERS__
GROUP BY name
ORDER BY calls DESC
LIMIT 10
"""


def agent_kind_clause(kinds: Sequence[str]) -> Tuple[str, Dict[str, Any]]:
    """``sagg.agent_kind`` matching any of ``kinds``, every value a param.

    The first kind travels as ``agent_kind`` (so a single-kind filter reads
    exactly as it always did), the rest as ``agent_kind_1``, ``agent_kind_2``…
    Shared with observability's tool rollup so both scope a deployment scope
    the same way."""
    params = {("agent_kind" if i == 0 else "agent_kind_%d" % i): k
              for i, k in enumerate(kinds)}
    if len(params) == 1:
        return "sagg.agent_kind = {agent_kind:String}", params
    return ("sagg.agent_kind IN (%s)" % ", ".join("{%s:String}" % n for n in params),
            params)


def _filter_clauses(
    agent_kind: Optional[str],
    channel: Optional[str],
    environment: Optional[str] = None,
    *,
    scope_kinds: Sequence[str] = (),
) -> Tuple[str, Dict[str, Any]]:
    """Build parameterised AND-clauses on the per-trace `waj.*` aggregate.

    References the `sagg` subquery columns (agent_kind / channel_slug / env)
    that both _TRACE_SQL and _TOOLS_SQL expose — these are the `waj.*`
    attributes pulled from the trace's tagged span, not from `top_span_id`.

    ``scope_kinds`` scopes a scoped build to all of its client's agents
    (main + extra kinds) when no single ``agent_kind`` was picked."""
    clauses: List[str] = []
    params: Dict[str, Any] = {}
    kinds = (agent_kind,) if agent_kind else tuple(scope_kinds)
    if kinds:
        clause, kind_params = agent_kind_clause(kinds)
        clauses.append(clause)
        params.update(kind_params)
    if channel:
        clauses.append("sagg.channel_slug = {channel:String}")
        params["channel"] = channel
    if environment:
        # Env lands on the tagged span's attributes via root_span_attrs
        # (waj.env). Traces written before env tagging shipped have NO waj.env
        # (the aggregate returns ''). Treat those untagged traces as the default
        # env ('dev') so the dev dashboard keeps showing historical data, while
        # staging/prod match only their explicitly-tagged traces.
        if environment == "staging":
            clauses.append("(sagg.env = {environment:String} OR sagg.env = '')")
        else:
            clauses.append("sagg.env = {environment:String}")
        params["environment"] = environment
    return ("".join("  AND " + c + "\n" for c in clauses), params)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _map_trace_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a raw ClickHouse row into a normalised trace-row dict.

    Coercion is defensive: ClickHouse may serialise 64-bit ints as JSON strings
    depending on settings, so we never assume the wire type.
    """
    return {
        "created_at": r.get("created_at"),
        "agent_kind": r.get("agent_kind") or None,
        "channel_slug": r.get("channel_slug") or None,
        "input_tokens": _as_int(r.get("input_tokens")),
        "output_tokens": _as_int(r.get("output_tokens")),
        "cache_read_tokens": _as_int(r.get("cache_read_tokens")),
        "cost_usd": _as_float(r.get("cost_usd")),
        "latency_ms": _as_float(r.get("latency_ms")),
        "iteration_count": _as_int(r.get("iteration_count")),
        "error": bool(_as_int(r.get("error"))),
        "ttft_ms": _as_int(r.get("ttft_ms")) or None,
        "ttlt_ms": _as_int(r.get("ttlt_ms")) or None,
    }


def _totals(rows: List[Dict[str, Any]], cost_map: CostMap) -> Dict[str, Any]:
    n = len(rows)
    errs = sum(1 for r in rows if r.get("error"))
    lats = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    tin = sum(r.get("input_tokens") or 0 for r in rows)
    cr = sum(r.get("cache_read_tokens") or 0 for r in rows)
    return {
        "volume": n,
        "tokens_in": sum(r.get("input_tokens") or 0 for r in rows),
        "tokens_out": sum(r.get("output_tokens") or 0 for r in rows),
        "cache_read_tokens": cr,
        "cache_hit_rate": round(100 * cr / tin, 1) if tin else 0.0,
        "cost_usd": round(sum(cost_for_row(r, cost_map) for r in rows), 4),
        "error_rate": round(100 * errs / n, 1) if n else 0.0,
        "p50_ms": percentile(lats, 0.50),
        "p95_ms": percentile(lats, 0.95),
        "p99_ms": percentile(lats, 0.99),
    }


def _empty_payload(hours, agent_kind, channel, status, environment=None, *, ok: bool, notice=None) -> Dict[str, Any]:
    return {
        "ok": ok,
        "notice": notice,
        "window_hours": hours,
        "bucket_seconds": int(choose_bucket(hours).total_seconds()),
        "filters": {"agent_kind": agent_kind, "channel": channel,
                    "status": status, "environment": environment},
        "buckets": [],
        "series": _empty_series(),
        "volume_by_agent": {},
        "cost_share": [],
        "latency_hist": {"labels": [], "counts": [], "edges": [],
                         "outlier": False, "cutoff_ms": None},
        "top_tools": [],
        "per_agent": [],
        "totals": {"volume": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                   "cache_read_tokens": 0, "cache_hit_rate": 0.0,
                   "error_rate": 0.0, "p50_ms": None, "p95_ms": None,
                   "p99_ms": None},
    }


def load_analytics(
    *,
    hours: int = 24,
    agent_kind: Optional[str] = None,
    channel: Optional[str] = None,
    status: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    """Query ClickHouse for the window + filters and assemble the dashboard
    payload. Best-effort: if ClickHouse is unavailable, returns an
    empty-but-shaped payload with ``ok=False`` + a notice, so the client renders
    an empty dashboard and never errors.

    ``status``: ``"success"`` keeps non-errored traces, ``"error"`` keeps errored
    ones (derived from OTel exception events, not the empty ``status`` column).

    ``environment``: when set (``dev``/``staging``/``prod``), keeps only traces
    whose top span carries that ``waj.env`` tag — this is what makes the Live
    Monitor show distinct per-env numbers instead of one global aggregate.
    Traces written before env tagging shipped have no ``waj.env`` and are
    excluded by any env filter.
    """
    # On a scoped build this engine is
    # a standalone single-client service — the Live Monitor must show ONLY
    # that client's traces. Scope the SQL to the client's agents (main kind
    # AND its extra_kinds, e.g. a scope's outbound agents) so the
    # tool-volume query (which aggregates by trace, with no per-row agent_kind
    # to filter post-fetch) is narrowed at the source; the trace rows are also
    # belt-and-suspenders filtered after mapping below. No-op when there's no
    # active client (full multi-agent view), and never overrides an explicit
    # ``agent_kind`` the caller already passed in. Pinning the main kind alone
    # hid every extra kind's call from the dashboard.
    #
    # It once checked for one particular scope by name: under any other scope
    # this query's WHERE clause stayed unscoped, so the tool-volume rollup
    # (which has no post-fetch narrow) silently aggregated every agent's tool
    # calls instead of just the scope's.
    from admin.seed_agent_profiles import filter_trace_rows_to_client
    from core.deployment import active_scope

    client = active_scope()
    scope_kinds = (client.agent_kind, *client.extra_kinds) if client is not None else ()
    filters_sql, filter_params = _filter_clauses(
        agent_kind, channel, environment, scope_kinds=scope_kinds)
    try:
        raw = ch_query(
            _TRACE_SQL.replace("__FILTERS__", filters_sql),
            {"hours": hours, "limit": _ROW_LIMIT, **filter_params},
        )
        tool_rows = ch_query(
            _TOOLS_SQL.replace("__FILTERS__", filters_sql),
            {"hours": hours, **filter_params},
        )
    except ClickHouseUnavailable as exc:
        if getattr(exc, "kind", "") == "off":
            # No tracing store on this install: an honest empty state, not an outage.
            return _empty_payload(hours, agent_kind, channel, status, environment, ok=False,
                                  notice="Tracing is off on this install. These charts are "
                                         "drawn from traces, so they need the tracing stack.")
        logger.warning("load_analytics: ClickHouse unavailable: %s", exc)
        # Name the cause: "unavailable" alone read as a network outage even
        # when ClickHouse was up and rejecting the query.
        cause = {
            "unreachable": "could not be reached",
            "auth": "refused the login",
        }.get(getattr(exc, "kind", ""), "rejected the query")
        return _empty_payload(hours, agent_kind, channel, status, environment, ok=False,
                              notice="Analytics source (ClickHouse) %s: %s" % (cause, str(exc)[:300]))

    # The query orders newest-first, so on truncation we keep the most recent
    # traces (and warn) rather than silently aggregating a stale slice.
    truncated = len(raw) >= _ROW_LIMIT

    rows = [_map_trace_row(r) for r in raw]
    # Robust post-fetch narrow to the scope (no-op on a full build): the SQL
    # WHERE above already scopes by waj.agent_kind, but filtering the mapped
    # rows here is independent of how the attribute is stored on the span.
    rows = filter_trace_rows_to_client(rows)
    if status == "success":
        rows = [r for r in rows if not r.get("error")]
    elif status == "error":
        rows = [r for r in rows if r.get("error")]

    cost_map: CostMap = {}
    global _model_pricing
    _model_pricing = _load_model_pricing()
    ts = bucket_series(rows, choose_bucket(hours), cost_map)
    top_tools = [{"name": r.get("name"), "calls": _as_int(r.get("calls"))} for r in tool_rows]

    payload = _empty_payload(hours, agent_kind, channel, status, environment, ok=True)
    if truncated:
        payload["notice"] = (
            f"Showing the most recent {_ROW_LIMIT:,} traces; older traces in "
            "this window are excluded."
        )
    payload.update({
        "buckets": ts["buckets"],
        "series": ts["series"],
        "volume_by_agent": ts["volume_by_agent"],
        "cost_share": cost_share(rows, cost_map),
        "latency_hist": latency_histogram(rows),
        "top_tools": top_tools,
        "per_agent": per_agent_rollup(rows, cost_map),
        "totals": _totals(rows, cost_map),
    })
    return payload
