"""Observe-domain MCP tools: live KPIs, cache insights, trace explorer,
conversation insights (outcome / topic / language), global search.

Read-only — no audit trail, no mutations. All four tools call the same
portal screen functions the HTTP admin routes use (see
admin/router.py:live_analytics_json, admin/observability_explorer_handlers.py,
admin/router.py:search_json) so the payload shapes match what an operator
sees in the browser.
"""
from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal

from pydantic import Field

from admin_mcp._common import (READ_ONLY, Env, TraceEnv, client_for_env, err,
                               page, resolve_alias, rows, tool_errors,
                               validate_env)
from admin_mcp.server import mcp

_STATUS_VALUES = ("", "success", "error")
_Status = Literal["", "success", "error"]
# Deprecated alias for trace_env — kept so old callers still work.
_TraceEnv = Literal["", "dev", "staging", "prod"]


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def live_analytics(hours: int = 24, agent_kind: str = "", channel: str = "",
                   status: _Status = "", trace_env: TraceEnv = "",
                   environment: _TraceEnv = "",
                   include_series: bool = False) -> Dict[str, Any]:
    """Live KPI dashboard data (Laminar/ClickHouse-backed): volume, cost,
    latency percentiles, error rate, per-agent/channel breakdowns, top tools.
    Same payload as /admin/live/analytics.json. hours is clamped to
    [1, 8760]; status must be success | error | '' (both = no filter).
    trace_env (dev|staging|prod|'') scopes to traces tagged with that
    waj.env — unlike env elsewhere, this isn't restricted to staging|prod
    since Laminar traces can carry a dev tag too (`environment` is the
    deprecated alias). The per-bucket time series (`series`, `buckets`) is
    dropped unless include_series=true — the totals and breakdowns answer
    most questions and the series is the bulk of the payload."""
    from admin.screens.live_analytics import load_analytics
    if status not in _STATUS_VALUES:
        return err("status must be success | error | ''")
    trace_env = resolve_alias(trace_env, environment,
                              new_name="trace_env", old_name="environment")
    data = load_analytics(hours=max(1, min(8760, hours)),
                          agent_kind=agent_kind or None,
                          channel=channel or None, status=status or None,
                          environment=trace_env or None)
    if not include_series and isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in ("series", "buckets")}
        data["series_omitted"] = True
    return data


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def caching_analytics(hours: int = 24, agent_kind: str = "",
                      trace_env: TraceEnv = "",
                      environment: _TraceEnv = "",
                      env: Env = "staging") -> Dict[str, Any]:
    """Prompt-cache insights (Laminar/ClickHouse-backed): overall cache hit
    rate, cache-read tokens, estimated USD saved, a cached-vs-uncached token
    time series, and a per-agent cache-hit-rate breakdown. A focused slice of
    the Live Monitor / /admin/live/analytics.json for "how much are we saving
    from caching / which agents cache well". Cache-read is read from the
    auto-instrumented gen_ai.usage.cache_read_input_tokens span attribute
    (deduped to the LLM span); hit rate = cache_read / total input tokens.
    hours is clamped to [1, 8760]; trace_env (dev|staging|prod|'') scopes to
    traces tagged with that waj.env (`environment` is the deprecated alias);
    env (staging|prod) selects the config branch used to price the savings
    estimate."""
    validate_env(env)  # fail fast before the (expensive) ClickHouse query
    environment = resolve_alias(trace_env, environment,
                                new_name="trace_env", old_name="environment")
    from admin.screens.live_analytics import (estimate_cache_savings_usd,
                                              load_analytics)
    a = load_analytics(hours=max(1, min(8760, hours)),
                       agent_kind=agent_kind or None,
                       environment=environment or None)
    totals = a.get("totals") or {}
    series = a.get("series") or {}
    saved = round(estimate_cache_savings_usd(
        client_for_env(env), int(totals.get("cache_read_tokens") or 0)), 2)
    return {
        "ok": a.get("ok"),
        "notice": a.get("notice"),
        "window_hours": a.get("window_hours"),
        "totals": {
            "cache_read_tokens": totals.get("cache_read_tokens", 0),
            "input_tokens": totals.get("tokens_in", 0),
            "cache_hit_rate": totals.get("cache_hit_rate", 0.0),
            "cache_saved_usd": saved,
        },
        "buckets": a.get("buckets", []),
        "series": {
            "tokens_cached": series.get("tokens_cached", []),
            "tokens_uncached": series.get("tokens_uncached", []),
        },
        "per_agent": [
            {"agent_kind": r.get("agent_kind"),
             "cache_read_tokens": r.get("cache_read_tokens", 0),
             "input_tokens": r.get("tokens_in", 0),
             "cache_hit_rate": r.get("cache_hit_rate", 0.0)}
            for r in (a.get("per_agent") or [])
        ],
        "filters": {"agent_kind": agent_kind or None, "environment": environment or None},
    }


def _session_traces(session_id: str, env: str, limit: int = 20) -> List[Dict[str, Any]]:
    """agent_traces rows for a chat session (newest first) — the same lookup
    the portal's /admin/sessions/{id} redirect does, keyed by the coerced
    session id the chat tables use."""
    from admin.screens.observability import _coerce_session_id_for_lookup
    lookup = _coerce_session_id_for_lookup(session_id) or session_id
    client = client_for_env(env)
    return rows(client.table("agent_traces")
                .select("trace_id, session_id, agent_kind, lmnr_trace_id, created_at")
                .eq("session_id", lookup)
                .order("created_at", desc=True).limit(limit).execute())


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def traces(action: Literal["search", "get", "span", "transcript",
                           "overview", "session", "audio"] = "search",
           trace_id: str = "", span_id: str = "", session_id: str = "",
           hours: int = 24, status: _Status = "", search_text: str = "",
           tool: str = "", channel_slug: str = "", agent_kind: str = "",
           include_background: bool = False,
           limit: Annotated[int, Field(
               description="search: max traces to return (1-100, default 25).")] = 25,
           offset: Annotated[int, Field(
               description="search: skip this many traces (paging).")] = 0,
           env: Env = "staging") -> Dict[str, Any]:
    """Trace explorer (mirrors /admin/observability/explorer). action:
    search (filters -> recent traces, newest first, paged by limit/offset:
    {traces, total, offset, limit, has_more, notice, ...}) |
    get (trace_id -> span tree) | span (trace_id + span_id -> full span
    detail) | transcript (trace_id -> persisted conversation turns +
    tool calls) | overview (trace_id -> session, per-tool rollup, errors) |
    session (session_id -> that chat session's traces, newest first — the
    portal's /admin/sessions/{id} lookup) | audio (trace_id, or session_id
    -> call-recording METADATA for that conversation: duration, status and
    the portal path of the playable WAV; never the audio bytes).
    hours (search only) is clamped to [1, 8760]; status must be
    success | error | ''; tool (search only) narrows to traces that called
    that tool by name. search always returns tool_filter_applied: if a
    `tool` filter was requested and this is false, the ClickHouse match
    query failed and `traces` is the UNNARROWED list for the window — do
    not treat it as "every trace called `tool`"."""
    from admin.screens.observability import (load_span_detail_json,
                                             load_span_tree_json,
                                             load_trace_overview_json,
                                             load_traces_list,
                                             load_transcript_json)
    validate_env(env)
    if status not in _STATUS_VALUES:
        return err("status must be success | error | ''")
    if action == "search":
        limit = max(1, min(100, limit))
        offset = max(0, offset)
        data = load_traces_list(
            past_hours=max(1, min(8760, hours)), page_size=100,
            search=search_text or None, status=status or None,
            channel_slug=channel_slug or None, agent_kind=agent_kind or None,
            env=env, include_background=include_background)
        tool_filter_applied = True
        if tool:
            from admin.screens.observability import (filter_traces_by_tool_result,
                                                      tool_filter_notice)
            narrowed, tool_filter_applied = filter_traces_by_tool_result(
                data.get("traces", []), tool)
            notices = [n for n in (data.get("notice"),
                                   None if tool_filter_applied else tool_filter_notice(tool))
                       if n]
            data = {**data, "traces": narrowed, "total": len(narrowed),
                    "notice": " ".join(notices) or None}
        paged = page(data.get("traces", []), limit, offset, max_limit=100)
        # An LLM operator will otherwise treat an unnarrowed list as if every
        # trace called `tool` — the same false-affirmative the HTML explorer
        # fixed by rendering `tool_filter_failed`. Surface it in the payload
        # so the model can tell narrowed from unnarrowed.
        return {**data, "traces": paged["items"], "total": paged["total"],
                "offset": paged["offset"], "limit": paged["limit"],
                "has_more": paged["has_more"],
                "tool_filter_applied": tool_filter_applied}
    if action == "session":
        if not session_id:
            return err("session needs session_id")
        found = _session_traces(session_id, env)
        return {"session_id": session_id, "traces": found, "count": len(found),
                "latest_trace_id": next((r.get("lmnr_trace_id") for r in found
                                         if r.get("lmnr_trace_id")), None)}
    if action == "audio":
        if not trace_id and session_id:
            found = _session_traces(session_id, env)
            trace_id = next((r.get("lmnr_trace_id") for r in found
                             if r.get("lmnr_trace_id")), "")
            if not trace_id:
                return {"session_id": session_id, "recordings": [],
                        "notice": "no trace is linked to this session yet"}
        if not trace_id:
            return err("audio needs trace_id or session_id")
        from admin.screens.observability import load_voice_audio_json
        return load_voice_audio_json(trace_id, env=env)
    if not trace_id:
        return err(f"{action} needs trace_id")
    if action == "get":
        return load_span_tree_json(trace_id)
    if action == "span":
        if not span_id:
            return err("span needs span_id")
        return load_span_detail_json(trace_id, span_id)
    if action == "transcript":
        return load_transcript_json(trace_id, env=env)
    if action == "overview":
        return load_trace_overview_json(trace_id, env=env)
    return err(f"unknown action {action!r}",
               hint="search | get | span | transcript | overview | session | audio")


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def voice_insights(hours: int = 24, trace_env: TraceEnv = "") -> Dict[str, Any]:
    """Duplex-voice latency rollup — the Live Monitor's voice card
    (admin.screens.observability.load_voice_live_insights): sessions,
    assistant turns, tool calls, barge-ins, and p50/p95 of end-of-turn ->
    first audio, end-of-turn -> spoken answer, delegate wall time and brain
    LLM time, over the last `hours` (clamped to [1, 8760]). `ch_ok: false` +
    `notice` means ClickHouse was unreachable (numbers are blanks, not
    zeros). For one call's audio use traces(action="audio")."""
    from admin.screens.observability import load_voice_live_insights
    return load_voice_live_insights(past_hours=max(1, min(8760, hours)),
                                    env=trace_env or None)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def conversation_insights(
        period: Annotated[Literal["24h", "7d", "30d"], Field(
            description="Overview period: last 24h / 7 days / 30 days, each "
                        "compared with the period before it.")] = "7d",
        trace_env: TraceEnv = "prod",
        agent_kind: Annotated[str, Field(
            description="Only this agent kind ('' = every agent this build "
                        "serves).")] = "") -> Dict[str, Any]:
    """How conversations ended, what they were about and which language
    customers used — the Overview's "What happened in conversations" section
    (admin.screens.insights.build_insights), same payload: `outcomes`
    (resolved / escalated / abandoned / unknown with count, pct and the
    change in points vs the previous period), `topics` (top labels, merged
    across agents), `languages`, `channels` (chat/voice), `agents` (per-agent
    outcome mix, resolved %, top topic). `available: false` = the insights
    table could not be read (unknown, not zero); `has_data: false` = nothing
    analysed in the period yet (collecting). Outcomes are an estimate: a
    hand-off action in telemetry, else an AI reading of the transcript.
    Partner builds only ever see their own agents. Rows come from the
    background sweep in core/conversation_insights.py (~30 min after a
    conversation goes quiet; the last 30 days are backfilled in batches)."""
    from admin.screens import plain_language as pl
    from admin.screens.insights import build_insights
    from admin.screens.overview import resolve_period

    env = {"prod": "prod", "staging": "staging", "dev": "staging"}.get(trace_env or "")
    hours = int(resolve_period(period)["hours"])
    data = build_insights(env=env, hours=hours, client=client_for_env(env or "prod"),
                          agent_kind=agent_kind or None,
                          agent_names=pl.load_agent_display_names(env or "prod"))
    data.pop("by_kind", None)
    data["period"] = period
    return data


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def search(q: str, env: Env = "staging",
           limit: Annotated[int, Field(
               description="Max results (1-30, default 30).")] = 30) -> Dict[str, Any]:
    """Global search: screens and tabs, agents, models, tools, toolsets,
    knowledge bases, skills, channels, tests, team, conversations and the
    people in them (masked), Help/docs sections. Prefix `>` actions, `@`
    people, `?` docs. Returns {results: [{group, type, label, context,
    sublabel, href, ...}]} ranked, max `limit` (<= 30). Same rows as
    /admin/search.json."""
    from admin.search import search as _search
    results = _search(q, validate_env(env))
    return {"results": results[:max(1, min(30, limit))]}
