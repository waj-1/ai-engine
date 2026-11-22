"""Overview (/admin/live) — the partner-facing "how are my agents doing?" page.

Everything here answers in plain language, for one **period** (last 24 hours,
7 days or 30 days) compared with the period before it:

* **Status strip** — one component that says whether the numbers below are
  live, partial or unavailable (replaces four differently-worded alerts).
* **Outcome tiles** — only metrics the telemetry actually supports:
  conversations, conversations that need attention (a failed action or an
  error), conversations handed to a person (a hand-off/escalation action ran),
  failed actions, and average response time — straight from telemetry.
* **Conversation insights** — outcome mix (resolved / handed to a person /
  left before finishing / unclear), top topics and languages, from the
  ``ai_conversation_insights`` table the background sweep fills
  (``core/conversation_insights.py``; ``admin/screens/insights.py``). The
  outcome is labelled as an estimate — a hand-off action, or an AI reading of
  the transcript — never as a customer-confirmed result.
* **Agents table** — per agent, linking into Conversations pre-filtered.
* **Needs attention** — failures grouped by cause, in words.
* **Voice** — calls / minutes (vs the daily allowance) / time to first reply /
  interruptions, when any voice traffic exists.

Sources: ClickHouse (``core.lmnr_clickhouse``) for everything conversational;
the AI Supabase project for voice minutes (``ai_voice_usage_daily``) and for
the conversation-log fallback count when ClickHouse is down. Every source is
best-effort and reports whether it answered: a tile whose source failed shows
"—", never a confident zero.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from admin.screens import plain_language as pl

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------

PERIODS: Dict[str, Dict[str, Any]] = {
    "24h": {"hours": 24, "label": "Last 24 hours", "short": "24h",
            "previous": "previous 24 hours"},
    "7d": {"hours": 168, "label": "Last 7 days", "short": "7 days",
           "previous": "previous 7 days"},
    "30d": {"hours": 720, "label": "Last 30 days", "short": "30 days",
            "previous": "previous 30 days"},
}
DEFAULT_PERIOD = "24h"


def resolve_period(value: Optional[str]) -> Dict[str, Any]:
    key = value if value in PERIODS else DEFAULT_PERIOD
    return {"key": key, **PERIODS[key]}


def _bucket_labels(hours: int, bucket: int) -> List[str]:
    """Every bucket start in the window, formatted like ClickHouse's
    ``toString(toStartOfInterval(...))`` (UTC), so quiet buckets plot as 0."""
    now = int(datetime.now(timezone.utc).timestamp())
    start = ((now - hours * 3600) // bucket) * bucket
    out = []
    t = start
    while t <= now:
        out.append(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        t += bucket
    return out


def _bucket_seconds(hours: int) -> int:
    """Sparkline resolution: hourly for a day, daily beyond."""
    return 3600 if hours <= 24 else 86400


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------

def _ch(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Indirection so tests can stub ClickHouse."""
    from core.lmnr_clickhouse import query

    return query(sql, params)


def _client_kinds() -> Tuple[str, ...]:
    from core.deployment import active_scope

    client = active_scope()
    return (client.agent_kind, *client.extra_kinds) if client is not None else ()


def _per_trace_subquery(window_param: str) -> str:
    """Per-trace aggregate over the window's spans: the ``waj.*`` attributes
    from whichever span carries them (see live_analytics._TRACE_SQL for why not
    ``top_span_id``), the error flag from OTel events, and action counts."""
    err = "arrayExists(x -> x.2 IN ('exception', 'error'), events)"
    return (
        "SELECT trace_id,\n"
        "  maxIf(simpleJSONExtractString(attributes, 'waj.agent_kind'),\n"
        "        simpleJSONExtractString(attributes, 'waj.agent_kind') != '') AS agent_kind,\n"
        "  maxIf(simpleJSONExtractString(attributes, 'waj.env'),\n"
        "        simpleJSONExtractString(attributes, 'waj.env') != '') AS env,\n"
        "  maxIf(simpleJSONExtractString(attributes, 'waj.session_id'),\n"
        "        simpleJSONExtractString(attributes, 'waj.session_id') != '') AS session_id,\n"
        f"  max({err}) AS has_error,\n"
        "  countIf(name LIKE '%.tool') AS tool_calls,\n"
        f"  countIf(name LIKE '%.tool' AND {err}) AS tool_errors,\n"
        "  countIf(name LIKE '%.tool' AND match(lower(name), {handoff_re:String})\n"
        f"          AND NOT {err}) AS handoffs\n"
        "FROM spans\n"
        "WHERE start_time > now() - toIntervalHour({" + window_param + ":UInt32})\n"
        "GROUP BY trace_id"
    )


def _scope_filters(env: Optional[str], scope_kinds: Sequence[str],
                   agent_kind: Optional[str] = None) -> Tuple[List[str], Dict[str, Any]]:
    """AND-clauses on the ``sagg`` per-trace aggregate + their params.

    Env is STRICT for prod (only traces tagged prod) and admits untagged
    traces for staging — the same reading the analytics charts and the KPI
    tiles use. Background work (a background agent kind) is never a
    conversation."""
    from admin.screens.live_analytics import agent_kind_clause
    from admin.screens.observability import _BACKGROUND_AGENT_KINDS

    clauses: List[str] = []
    params: Dict[str, Any] = {}
    if env:
        params["env"] = env
        clauses.append("(sagg.env = {env:String} OR sagg.env = '')" if env == "staging"
                       else "sagg.env = {env:String}")
    kinds = (agent_kind,) if agent_kind else tuple(scope_kinds)
    if kinds:
        clause, kp = agent_kind_clause(kinds)
        clauses.append(clause)
        params.update(kp)
    if _BACKGROUND_AGENT_KINDS:   # `NOT IN ()` is not valid ClickHouse
        bg = ", ".join("'%s'" % k for k in _BACKGROUND_AGENT_KINDS)
        clauses.append("sagg.agent_kind NOT IN (%s)" % bg)
    return clauses, params


def _trace_subquery(window_param: str) -> str:
    """The trace rows of the window, minus diagnostic self-tests and every
    background class (observability.background_trace_clauses: voice-live
    metric markers, untagged bare-LLM / admin-MCP traces, eval test traffic) —
    the same exclusions as the Conversations list. The FINAL scan sits in its own
    subquery (ClickHouse 26.5 rejects a filtered FINAL scan joined directly —
    see observability._trace_list_sql)."""
    from admin.screens.observability import _DIAGNOSTIC_TOP_SPANS, background_trace_clauses

    diag = ", ".join("'%s'" % s for s in _DIAGNOSTIC_TOP_SPANS)
    return (
        "SELECT id, start_time, duration, top_span_name, metadata "
        "FROM traces_replacing FINAL "
        "WHERE start_time > now() - toIntervalHour({" + window_param + ":UInt32}) "
        "AND top_span_name NOT IN (%s) " % diag
        + "".join("AND " + c + " " for c in background_trace_clauses())
    )


_CONV_KEY = "if(sagg.session_id != '', sagg.session_id, toString(t.id))"


def outcome_sql(env: Optional[str], scope_kinds: Sequence[str] = ()) -> Tuple[str, Dict[str, Any]]:
    """Per (period, agent) outcome rollup over the current AND previous
    period in one scan. ``{hours}`` is the period length, ``{hours2}`` twice
    that. A conversation needs attention on an error, a failed action or a
    turn slower than ``SLOW_TURN_SEC`` (same rule as the Conversations list)."""
    from admin.screens.observability import SLOW_TURN_SEC

    clauses, params = _scope_filters(env, scope_kinds)
    sql = (
        "SELECT if(t.start_time > now() - toIntervalHour({hours:UInt32}), 'cur', 'prev') AS period,\n"
        "  sagg.agent_kind AS agent_kind,\n"
        "  count() AS turns,\n"
        f"  uniqExact({_CONV_KEY}) AS conversations,\n"
        f"  uniqExactIf({_CONV_KEY}, sagg.tool_errors > 0 OR sagg.has_error = 1 "
        f"OR t.duration > {SLOW_TURN_SEC}) AS conv_attention,\n"
        f"  uniqExactIf({_CONV_KEY}, sagg.handoffs > 0) AS conv_handoff,\n"
        "  sum(sagg.tool_calls) AS actions,\n"
        "  sum(sagg.tool_errors) AS failed_actions,\n"
        "  avg(t.duration) AS avg_response_sec\n"
        "FROM (" + _trace_subquery("hours2") + ") AS t\n"
        "INNER JOIN (" + _per_trace_subquery("hours2") + ") AS sagg ON sagg.trace_id = t.id\n"
        "WHERE " + " AND ".join(clauses) + "\n"
        "GROUP BY period, agent_kind\n"
    )
    params["handoff_re"] = pl.HANDOFF_TOOL_RE
    return sql, params


def series_sql(env: Optional[str], scope_kinds: Sequence[str] = ()) -> Tuple[str, Dict[str, Any]]:
    """Conversations per agent per bucket over the current period (sparklines)."""
    clauses, params = _scope_filters(env, scope_kinds)
    sql = (
        "SELECT toString(toStartOfInterval(t.start_time, toIntervalSecond({bucket:UInt32}))) AS bucket,\n"
        "  sagg.agent_kind AS agent_kind,\n"
        f"  uniqExact({_CONV_KEY}) AS conversations\n"
        "FROM (" + _trace_subquery("hours") + ") AS t\n"
        "INNER JOIN (" + _per_trace_subquery("hours") + ") AS sagg ON sagg.trace_id = t.id\n"
        "WHERE " + " AND ".join(clauses) + "\n"
        "GROUP BY bucket, agent_kind\n"
        "ORDER BY bucket\n"
    )
    params["handoff_re"] = pl.HANDOFF_TOOL_RE
    return sql, params


def failures_sql(env: Optional[str], scope_kinds: Sequence[str] = ()) -> Tuple[str, Dict[str, Any]]:
    """Every failing span in the current period with its exception, newest
    first — grouped by cause in Python (``group_failures``)."""
    clauses, params = _scope_filters(env, scope_kinds)
    err = "arrayExists(x -> x.2 IN ('exception', 'error'), s.events)"
    sql = (
        "SELECT s.name AS name,\n"
        "  toUInt8(s.name LIKE '%.tool') AS is_tool,\n"
        "  arrayFirst(x -> x.2 IN ('exception', 'error'), s.events).3 AS event_attrs,\n"
        "  s.output AS output,\n"
        "  toString(s.trace_id) AS trace_id,\n"
        "  toString(s.start_time) AS ts,\n"
        "  sagg.session_id AS session_id,\n"
        "  sagg.agent_kind AS agent_kind\n"
        "FROM spans AS s\n"
        "INNER JOIN (" + _per_trace_subquery("hours") + ") AS sagg ON sagg.trace_id = s.trace_id\n"
        "WHERE s.start_time > now() - toIntervalHour({hours:UInt32})\n"
        f"  AND {err}\n"
        "  AND s.trace_id IN (SELECT id FROM (" + _trace_subquery("hours") + "))\n"
        "  AND " + " AND ".join(clauses) + "\n"
        "ORDER BY s.start_time DESC\n"
        "LIMIT 2000\n"
    )
    params["handoff_re"] = pl.HANDOFF_TOOL_RE
    return sql, params


def _as_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _as_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _sum_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Totals across agents. ``conversations`` sums per-agent distinct counts —
    a conversation belongs to one agent, so this is exact in practice."""
    turns = sum(_as_int(r.get("turns")) for r in rows)
    weighted = sum((_as_float(r.get("avg_response_sec")) or 0.0) * _as_int(r.get("turns"))
                   for r in rows)
    return {
        "conversations": sum(_as_int(r.get("conversations")) for r in rows),
        "attention": sum(_as_int(r.get("conv_attention")) for r in rows),
        "handoffs": sum(_as_int(r.get("conv_handoff")) for r in rows),
        "actions": sum(_as_int(r.get("actions")) for r in rows),
        "failed_actions": sum(_as_int(r.get("failed_actions")) for r in rows),
        "turns": turns,
        "avg_response_sec": (weighted / turns) if turns else None,
    }


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------

def _delta(cur: Optional[float], prev: Optional[float], *, good_when: str = "up",
           unit: str = "pct") -> Optional[Dict[str, Any]]:
    """Change vs the previous period, phrased for a tile. ``None`` when there
    is nothing to compare (both zero / unknown)."""
    if cur is None or prev is None:
        return None
    if unit == "points":
        diff = round(cur - prev, 1)
        if diff == 0:
            return {"text": "No change", "tone": "neutral"}
        better = (diff > 0) == (good_when == "up")
        return {"text": "%+.1f pts" % diff, "tone": "ok" if better else "bad"}
    if prev == 0:
        if cur == 0:
            return None
        return {"text": "New this period", "tone": "neutral"}
    pct = round(100.0 * (cur - prev) / prev)
    if pct == 0:
        return {"text": "No change", "tone": "neutral"}
    better = (pct > 0) == (good_when == "up")
    return {"text": "%+d%%" % pct, "tone": "ok" if better else "bad"}


def _pct(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def _fmt_seconds(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    if sec < 10:
        return "%.1f s" % sec
    if sec < 90:
        return "%d s" % round(sec)
    return "%.1f min" % (sec / 60.0)


def build_tiles(cur: Optional[Dict[str, Any]], prev: Optional[Dict[str, Any]],
                *, previous_label: str, fallback_conversations: Optional[int] = None,
                env: Optional[str] = None) -> List[Dict[str, Any]]:
    """The five headline tiles. ``cur is None`` = ClickHouse did not answer:
    every tile shows "—" with "Unavailable", except Conversations, which may
    show the conversation-log count — explicitly labelled as covering all
    environments, because that table has no environment column."""
    if cur is None:
        conv_tile: Dict[str, Any] = {
            "key": "conversations", "label": "Conversations", "value": "—",
            "caption": "Unavailable right now", "unavailable": True,
        }
        if fallback_conversations is not None:
            conv_tile.update({
                "value": "{:,}".format(fallback_conversations),
                "caption": "All environments — from the conversation log"
                           + (", not limited to %s" % _env_name(env) if env else ""),
                "unavailable": False, "approximate": True,
            })
        rest = [
            ("attention", "Needs attention"), ("handoffs", "Handed to a person"),
            ("failed_actions", "Failed actions"), ("response", "Avg response time"),
        ]
        return [conv_tile] + [
            {"key": k, "label": label, "value": "—", "caption": "Unavailable right now",
             "unavailable": True}
            for k, label in rest
        ]

    p = prev or {}
    vs = "vs %s" % previous_label
    att_pct = _pct(cur["attention"], cur["conversations"])
    prev_att_pct = _pct(p.get("attention", 0), p.get("conversations", 0))
    return [
        {"key": "conversations", "label": "Conversations",
         "value": "{:,}".format(cur["conversations"]),
         "delta": _delta(cur["conversations"], p.get("conversations")),
         "caption": vs},
        {"key": "attention", "label": "Needs attention",
         "value": "{:,}".format(cur["attention"]),
         "sub": ("%s%% of conversations" % _trim(att_pct)) if att_pct is not None else None,
         "delta": _delta(att_pct, prev_att_pct, good_when="down", unit="points")
         if att_pct is not None and prev_att_pct is not None else None,
         "caption": "A failed action or an error",
         "href": "/admin/observability?outcome=attention"},
        {"key": "handoffs", "label": "Handed to a person",
         "value": "{:,}".format(cur["handoffs"]),
         "delta": _delta(cur["handoffs"], p.get("handoffs"), good_when="down"),
         "caption": "The agent used a hand-off action",
         "href": "/admin/observability?outcome=handoff"},
        {"key": "failed_actions", "label": "Failed actions",
         "value": "{:,}".format(cur["failed_actions"]),
         "sub": ("of {:,} actions".format(cur["actions"])) if cur["actions"] else "No actions run",
         "delta": _delta(cur["failed_actions"], p.get("failed_actions"), good_when="down"),
         "caption": vs},
        {"key": "response", "label": "Avg response time",
         "value": _fmt_seconds(cur["avg_response_sec"]),
         "delta": _delta(cur["avg_response_sec"], p.get("avg_response_sec"), good_when="down"),
         "caption": "Per agent reply"},
    ]


def _trim(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return ("%d" % v) if float(v).is_integer() else ("%.1f" % v)


def _env_name(env: Optional[str]) -> str:
    return {"prod": "production", "staging": "staging"}.get(env or "", env or "")


# ---------------------------------------------------------------------------
# Needs attention
# ---------------------------------------------------------------------------

def group_failures(rows: List[Dict[str, Any]], *, tool_names: Dict[str, str],
                   agent_names: Dict[str, str], hours: int) -> List[Dict[str, Any]]:
    """Group failing spans into causes a partner can act on.

    One trace usually records the same failure at several levels (the tool
    span and the agent span around it). Per trace, tool failures win; a
    trace with no failing tool contributes ONE "the agent ran into a problem"
    entry from its first failing span — so each failure is counted once."""
    by_trace: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for r in rows:
        tid = r.get("trace_id") or ""
        if tid not in by_trace:
            by_trace[tid] = []
            order.append(tid)
        by_trace[tid].append(r)

    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for tid in order:
        spans = by_trace[tid]
        tools = [s for s in spans if _as_int(s.get("is_tool"))]
        picked = tools if tools else spans[:1]
        for s in picked:
            exc = pl.exception_from_event_attrs(s.get("event_attrs"))
            is_action = bool(_as_int(s.get("is_tool")))
            reason = pl.plain_failure_reason(exc["type"], exc["message"],
                                             (s.get("output") or "")[:500])
            label = pl.action_label(s.get("name"), tool_names) if is_action else ""
            key = (label if is_action else "__agent__", reason)
            g = groups.get(key)
            conv = s.get("session_id") or tid
            if g is None:
                g = groups[key] = {
                    "sentence": pl.failure_sentence(label, reason, is_action=is_action),
                    "action": label or None,
                    "reason": reason,
                    "technical": " · ".join(x for x in (
                        pl.tool_base_name(s.get("name")), exc["type"],
                        (exc["message"] or "")[:160]) if x),
                    "count": 0,
                    "_convs": set(),
                    "agents": [],
                    "first_seen": s.get("ts"),
                    "last_seen": s.get("ts"),
                    "trace_id": tid,  # newest first → the latest example
                }
            g["count"] += 1
            g["_convs"].add(conv)
            agent = pl.agent_label(s.get("agent_kind"), agent_names)
            if agent not in g["agents"]:
                g["agents"].append(agent)
            ts = s.get("ts")
            if ts and (not g["first_seen"] or ts < g["first_seen"]):
                g["first_seen"] = ts
            if ts and (not g["last_seen"] or ts > g["last_seen"]):
                g["last_seen"] = ts
    out = []
    for g in groups.values():
        g["conversations"] = len(g.pop("_convs"))
        g["href"] = "/admin/observability?hours=%d#trace=%s" % (hours, g["trace_id"])
        out.append(g)
    out.sort(key=lambda g: (g["count"], g["last_seen"] or ""), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

def voice_usage(client, hours: int) -> Optional[Dict[str, Any]]:
    """Voice minutes from ``ai_voice_usage_daily`` for the current and
    previous period, plus today's use against the daily allowance. ``None``
    when the table cannot be read. Not environment-scoped (the table has no
    environment column) — the template says so."""
    from voice.live.budget import DEFAULT_DAILY_SECONDS

    days = max(1, hours // 24)
    today = datetime.now(timezone.utc).date()
    start_prev = today - timedelta(days=2 * days - 1)
    start_cur = today - timedelta(days=days - 1)
    try:
        rows = (
            client.table("ai_voice_usage_daily")
            .select("business_id, usage_date, seconds_used")
            .gte("usage_date", start_prev.isoformat())
            .limit(20000).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("voice usage unavailable (%s)", exc)
        return None
    cur = prev = 0
    today_by_biz: Dict[str, int] = {}
    for r in rows:
        try:
            d = datetime.fromisoformat(str(r.get("usage_date"))[:10]).date()
        except ValueError:
            continue
        secs = _as_int(r.get("seconds_used"))
        if d >= start_cur:
            cur += secs
        else:
            prev += secs
        if d == today and r.get("business_id"):
            today_by_biz[str(r["business_id"])] = today_by_biz.get(str(r["business_id"]), 0) + secs
    allowance = None
    if today_by_biz:
        limits: Dict[str, int] = {}
        try:
            brows = (
                client.table("ai_voice_budgets").select("business_id, daily_seconds")
                .in_("business_id", list(today_by_biz)).execute().data or []
            )
            limits = {str(b["business_id"]): _as_int(b.get("daily_seconds")) for b in brows}
        except Exception:  # noqa: BLE001
            limits = {}
        allowance = sum(limits.get(b, DEFAULT_DAILY_SECONDS) for b in today_by_biz)
    used_today = sum(today_by_biz.values())
    return {
        "minutes": round(cur / 60.0, 1),
        "prev_minutes": round(prev / 60.0, 1),
        "delta": _delta(cur, prev, good_when="up"),
        "today_minutes": round(used_today / 60.0, 1),
        "today_allowance_minutes": round(allowance / 60.0) if allowance else None,
        "today_pct": round(100.0 * used_today / allowance) if allowance else None,
        "businesses_today": len(today_by_biz),
    }


# ---------------------------------------------------------------------------
# Fallback count
# ---------------------------------------------------------------------------

def conversation_log_count(client, hours: int) -> Optional[int]:
    """Conversations started in the window, from ``ai_chat_sessions`` — used
    only when ClickHouse is down. NOT environment-scoped."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        res = (
            client.table("ai_chat_sessions").select("id", count="exact")
            .gte("created_at", since).is_("deleted_at", "null").limit(1).execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("conversation log count unavailable (%s)", exc)
        return None
    count = getattr(res, "count", None)
    return int(count) if isinstance(count, int) else None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_overview(*, env: str, period_key: Optional[str], client) -> Dict[str, Any]:
    """Everything the Overview template renders above the Advanced section."""
    from admin.screens.observability import _ch_cause

    period = resolve_period(period_key)
    hours = int(period["hours"])
    kinds = _client_kinds()
    agent_names = pl.load_agent_display_names(env)

    sources = {"outcomes": False, "attention": False, "voice_usage": False}

    # --- outcomes (current + previous) --------------------------------------
    outcome_rows: Optional[List[Dict[str, Any]]] = None
    try:
        sql, params = outcome_sql(env, kinds)
        outcome_rows = _ch(sql, {**params, "hours": hours, "hours2": 2 * hours})
        sources["outcomes"] = True
    except Exception as exc:  # noqa: BLE001 - degrade to "—"
        logger.warning("overview: outcome rollup unavailable (%s)", exc)

    cur_rows = [r for r in (outcome_rows or []) if r.get("period") == "cur"]
    prev_rows = [r for r in (outcome_rows or []) if r.get("period") == "prev"]
    cur = _sum_rows(cur_rows) if outcome_rows is not None else None
    prev = _sum_rows(prev_rows) if outcome_rows is not None else None

    fallback = None
    if cur is None:
        fallback = conversation_log_count(client, hours)
    tiles = build_tiles(cur, prev, previous_label=period["previous"],
                        fallback_conversations=fallback, env=env)

    # --- per agent ------------------------------------------------------------
    series: Dict[str, List[int]] = {}
    if outcome_rows:
        try:
            sql, params = series_sql(env, kinds)
            bucket = _bucket_seconds(hours)
            srows = _ch(sql, {**params, "hours": hours, "bucket": bucket})
            buckets = _bucket_labels(hours, bucket)
            observed = {r.get("bucket") for r in srows if r.get("bucket")}
            if not observed.issubset(set(buckets)):
                # ClickHouse server not on UTC — fall back to the observed buckets.
                buckets = sorted(observed)
            idx = {b: i for i, b in enumerate(buckets)}
            for r in srows:
                k = r.get("agent_kind") or ""
                series.setdefault(k, [0] * len(buckets))[idx[r["bucket"]]] = _as_int(r.get("conversations"))
        except Exception as exc:  # noqa: BLE001 - sparklines are decoration
            logger.debug("overview: sparkline series unavailable (%s)", exc)

    prev_by_kind = {r.get("agent_kind") or "": _sum_rows([r]) for r in prev_rows}
    agents = []
    for r in sorted(cur_rows, key=lambda r: _as_int(r.get("conversations")), reverse=True):
        kind = r.get("agent_kind") or ""
        s = _sum_rows([r])
        p = prev_by_kind.get(kind) or {}
        agents.append({
            "kind": kind,
            "name": pl.agent_label(kind or None, agent_names),
            "conversations": s["conversations"],
            "delta": _delta(s["conversations"], p.get("conversations")),
            "attention": s["attention"],
            "attention_pct": _pct(s["attention"], s["conversations"]),
            "handoffs": s["handoffs"],
            "actions": s["actions"],
            "failed_actions": s["failed_actions"],
            "avg_response": _fmt_seconds(s["avg_response_sec"]),
            "series": series.get(kind) or [],
            "href": "/admin/observability?hours=%d" % hours
                    + ("&agent_kind=%s" % kind if kind else ""),
        })

    # --- conversation insights (outcome / topic / language) --------------------
    # Its own source (the AI DB, not ClickHouse), so it renders even when the
    # telemetry is down. See admin/screens/insights.py.
    from admin.screens.insights import build_insights

    insights = build_insights(env=env, hours=hours, client=client, agent_names=agent_names)
    by_kind = insights.get("by_kind") or {}
    for a in agents:
        ins = by_kind.get(a["kind"])
        a["resolved_text"] = (ins["resolved_text"] + "%") if ins and ins.get("total") else None
        a["insight_total"] = ins.get("total") if ins else 0

    # --- needs attention ------------------------------------------------------
    attention: List[Dict[str, Any]] = []
    if outcome_rows is not None:
        try:
            sql, params = failures_sql(env, kinds)
            frows = _ch(sql, {**params, "hours": hours})
            attention = group_failures(
                frows, tool_names=pl.load_tool_display_names(env),
                agent_names=agent_names, hours=hours)
            sources["attention"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("overview: failure list unavailable (%s)", exc)

    # --- voice ----------------------------------------------------------------
    usage = voice_usage(client, hours)
    sources["voice_usage"] = usage is not None

    # --- status strip ---------------------------------------------------------
    cause = _ch_cause()
    if not sources["outcomes"]:
        status = {
            "state": "down",
            "headline": "Live statistics are temporarily unavailable — conversations "
                        "are still being recorded.",
            "detail": cause,
        }
    elif not sources["attention"]:
        status = {
            "state": "partial",
            "headline": "Some live statistics are delayed — the list of problems "
                        "below could not be loaded.",
            "detail": cause,
        }
    else:
        n = len(attention)
        status = {
            "state": "attention" if n else "ok",
            "headline": (
                "%d problem%s found in the %s — see Needs attention below."
                % (n, "" if n == 1 else "s", period["label"].lower())
            ) if n else "All agents running normally in the %s." % period["label"].lower(),
            "detail": None,
        }

    return {
        "period": period,
        "periods": [{"key": k, "label": v["label"]} for k, v in PERIODS.items()],
        "status": status,
        "sources": sources,
        "tiles": tiles,
        "agents": agents,
        "attention": attention,
        "voice_usage": usage,
        "insights": insights,
        "has_traffic": bool(cur and cur["conversations"]),
        "fallback_conversations": fallback,
    }
