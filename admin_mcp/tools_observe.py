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
    """Usage numbers read from the tracing store: volume, cost, latency
    percentiles, error rate, breakdowns per agent and per channel, and top
    tools. hours is clamped to 1-8760. status is success, error or '' (both).
    trace_env (dev, staging, prod, or '' for all) keeps only traces tagged
    with that environment; environment is its deprecated alias. The
    per-bucket time series (series, buckets) is the bulk of the payload and
    is left out unless include_series=true. When no tracing store is
    configured the numbers are empty and notice says so."""
    # Maintainer notes (kept out of the description clients read):
    # Same payload as the portal's /admin/live/analytics.json. trace_env is
    # wider than env elsewhere because traces can carry a dev tag.
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
    """Prompt-cache numbers read from the tracing store: overall cache hit
    rate, cache-read tokens, estimated USD saved, a cached against uncached
    token time series, and the cache hit rate per agent. Use it to see how
    much caching saves and which agents cache well. Hit rate = cache-read
    tokens / total input tokens. hours is clamped to 1-8760. trace_env (dev,
    staging, prod, or '' for all) keeps only traces tagged with that
    environment; environment is its deprecated alias. env (staging or prod)
    selects the config branch whose prices are used for the savings estimate."""
    # Maintainer notes (kept out of the description clients read):
    # A focused slice of /admin/live/analytics.json. Cache-read comes from the
    # auto-instrumented gen_ai.usage.cache_read_input_tokens span attribute
    # (deduped to the LLM span).
    validate_env(env)  # fail fast before the (expensive) tracing-store query
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
    """Search and read traces of agent conversations.

    Actions:
    - search (default): recent traces, newest first, paged with limit
      (1-100) and offset. Filters: hours (1-8760), status (success, error or
      ''), search_text, tool, channel_slug, agent_kind, include_background.
      Returns {traces, total, offset, limit, has_more, notice,
      tool_filter_applied, ...}.
    - get (trace_id): the span tree.
    - span (trace_id, span_id): the full detail of one span.
    - transcript (trace_id): the saved conversation turns and tool calls.
    - overview (trace_id): session, per-tool summary and errors.
    - session (session_id): that chat session's traces, newest first.
    - audio (trace_id or session_id): call-recording METADATA for the
      conversation: duration, status and the portal path of the playable
      WAV. Never the audio itself.

    If you pass tool and tool_filter_applied comes back false, the tool
    filter failed and traces is the UNFILTERED list for the window. Do not
    read it as "every trace called that tool"."""
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
    """Latency summary for voice calls over the last `hours` (clamped to
    1-8760): sessions, assistant turns, tool calls, barge-ins, and p50/p95 of
    end of turn to first audio, end of turn to spoken answer, delegate wall
    time and the answering model's LLM time. trace_env filters by environment
    tag. ch_ok=false with a notice means the tracing store could not be
    reached or is not set up; the numbers are then blank, not zero. For one
    call's audio use traces(action="audio")."""
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
    customers used. period is 24h, 7d or 30d. Returns outcomes (resolved,
    escalated, abandoned, unknown; each with count, pct and the change in
    points against the previous period), topics (top labels, merged across
    agents), languages, channels (chat or voice) and agents (per agent:
    outcome mix, resolved %, top topic).

    available=false means the data could not be read (unknown, not zero).
    has_data=false means nothing has been analysed in the period yet.
    Outcomes are an estimate: a recorded hand-off, or else an AI reading of
    the transcript. Conversations are analysed in the background, about 30
    minutes after they go quiet. A build limited to one partner only sees
    its own agents."""
    # Maintainer notes (kept out of the description clients read):
    # Same payload as the Overview's "What happened in conversations" section
    # (admin.screens.insights.build_insights). Rows come from the background
    # sweep in core/conversation_insights.py (the last 30 days are backfilled in
    # batches).
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
    """Search across the admin portal: screens and tabs, agents, models,
    tools, toolsets, knowledge bases, skills, channels, tests, team,
    conversations and the people in them (masked), and help sections. Prefix
    the query with > for actions, @ for people or ? for docs. Required: q.
    Returns ranked {results: [{group, type, label, context, sublabel, href,
    ...}]}, at most limit (up to 30)."""
    from admin.search import search as _search
    results = _search(q, validate_env(env))
    return {"results": results[:max(1, min(30, limit))]}
