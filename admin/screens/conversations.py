"""Conversations (/admin/observability) — list context.

One row per conversation (chat session), described the way a partner would:
which agent, which customer/business, what the customer first said, how it
went and when — instead of span names and session UUIDs. The engineering view
(one row per turn/trace, the tool filter, background traffic) stays available
under "More filters".

The trace rows come from ClickHouse (``load_traces_list``); the names, first
messages and business come from the AI project's chat tables and the main
project's ``business`` table in a handful of batched reads. Every enrichment
is best-effort: a failed lookup leaves the row with its plainer fallback, it
never hides the conversation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from admin.screens import plain_language as pl

logger = logging.getLogger(__name__)

HOURS_OPTIONS = ((1, "Last hour"), (24, "Last 24 hours"), (168, "Last 7 days"), (720, "Last 30 days"))
OUTCOME_OPTIONS = (("", "Any outcome"), ("attention", "Needs attention"),
                   ("handoff", "Handed to a person"), ("ok", "No problems"))
VOICE_TOP_SPANS = ("voice.agent_turn",)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip().replace(" ", "T", 1)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if "." in raw:
        head, rest = raw.split(".", 1)
        digits = "".join(c for c in rest if c.isdigit())
        raw = head + "." + digits[:6].ljust(6, "0") + rest[len(digits):]
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def friendly_time(value: Any, now: Optional[datetime] = None) -> Dict[str, str]:
    """``{"short": "Today 09:14", "ago": "2h ago", "iso": ...}`` (UTC)."""
    dt = _parse_ts(value)
    if dt is None:
        return {"short": "—", "ago": "", "iso": ""}
    now = now or datetime.now(timezone.utc)
    dt = dt.astimezone(timezone.utc)
    days = (now.date() - dt.date()).days
    hm = dt.strftime("%H:%M")
    if days == 0:
        short = "Today " + hm
    elif days == 1:
        short = "Yesterday " + hm
    else:
        short = "%d %s %s" % (dt.day, dt.strftime("%b"), hm)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:
        ago = "just now"
    elif secs < 3600:
        ago = "%dm ago" % (secs // 60)
    elif secs < 86400:
        ago = "%dh ago" % (secs // 3600)
    else:
        ago = "%dd ago" % (secs // 86400)
    return {"short": short, "ago": ago, "iso": dt.strftime("%Y-%m-%d %H:%M:%S UTC")}


def friendly_duration(ms: Any) -> str:
    try:
        sec = float(ms) / 1000.0
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    if sec < 60:
        return "%.1f s" % sec if sec < 10 else "%d s" % round(sec)
    return "%d:%02d min" % (int(sec // 60), int(sec % 60))


def _group_outcome(g: Dict[str, Any]) -> str:
    return pl.outcome_for(failed_actions=g.get("failed_actions", 0),
                          has_error=bool(g.get("has_error")), handoffs=g.get("handoffs", 0),
                          slow=bool(g.get("slow_turns")))


def _session_rows(client, sids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not sids:
        return {}
    try:
        rows = (
            client.schema("public").table("ai_chat_sessions")
            .select("id, business_id, title, message_count, agent_kind")
            .in_("id", sids).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("conversations: session rows unavailable (%s)", exc)
        return {}
    return {str(r["id"]): r for r in rows if r.get("id")}


def _first_user_messages(client, sids: List[str]) -> Dict[str, str]:
    if not sids:
        return {}
    try:
        rows = (
            client.schema("public").table("ai_chat_messages")
            .select("session_id, content, created_at")
            .in_("session_id", sids).eq("role", "user")
            .order("created_at").limit(3000).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("conversations: first messages unavailable (%s)", exc)
        return {}
    out: Dict[str, str] = {}
    for r in rows:
        sid = str(r.get("session_id") or "")
        text = " ".join(str(r.get("content") or "").split())
        if sid and text and sid not in out:
            out[sid] = text[:200]
    return out


def enrich_groups(groups: List[Dict[str, Any]], *, env: str,
                  channels: List[Dict[str, Any]]) -> None:
    """Add partner-facing fields to each conversation group, in place."""
    from admin.router import client_for
    from admin.screens.observability import _coerce_session_id_for_lookup

    agent_names = pl.load_agent_display_names(env)
    channel_names = {c.get("slug"): c.get("name") or c.get("slug") for c in channels or []}

    coerced: Dict[str, str] = {}
    for g in groups:
        sid = g.get("session_id")
        if sid:
            coerced[sid] = str(_coerce_session_id_for_lookup(sid) or sid)
    client = client_for(env)
    sids = sorted(set(coerced.values()))
    sessions = _session_rows(client, sids)
    firsts = _first_user_messages(client, sids)
    businesses = pl.fetch_business_names(r.get("business_id") for r in sessions.values())

    now = datetime.now(timezone.utc)
    for g in groups:
        ts = g.get("traces") or []
        cid = coerced.get(g.get("session_id") or "")
        srow = sessions.get(cid or "") or {}
        kind = g.get("agent_kind") or srow.get("agent_kind")
        is_voice = any((t.get("topSpanName") or "") in VOICE_TOP_SPANS for t in ts)
        g["agent_name"] = pl.agent_label(kind, agent_names)
        g["channel_name"] = (
            channel_names.get(g.get("channel_slug")) or
            (pl.humanize_slug(g.get("channel_slug")) if g.get("channel_slug") else None)
        )
        g["is_voice"] = is_voice
        g["business_name"] = businesses.get(str(srow.get("business_id") or ""))
        title = srow.get("title")
        g["title"] = title if title and title != "New Chat" else None
        g["first_message"] = firsts.get(cid or "")
        mc = srow.get("message_count")
        g["message_count"] = mc if isinstance(mc, int) and mc > 0 else None
        g["outcome"] = _group_outcome(g)
        g["outcome_label"], g["outcome_variant"] = pl.OUTCOMES[g["outcome"]]
        g["when"] = friendly_time(g.get("latest_start"), now)
        g["started"] = friendly_time(g.get("earliest_start"), now)
        g["duration"] = friendly_duration(g.get("duration_ms_total"))
        g["newest_trace_id"] = ts[0].get("id") if ts else None


def _single_groups(traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One group per trace — the "one row per turn" engineering view."""
    from admin.screens.observability import group_traces_by_session

    out = []
    for t in traces:
        out.extend(group_traces_by_session([{**t, "session_id": None}]))
        out[-1]["session_id"] = t.get("session_id")
    return out


def build_conversations_context(
    *,
    env: str,
    hours: int,
    search: Optional[str],
    status: Optional[str],
    outcome: Optional[str],
    tool: Optional[str],
    channel_slug: Optional[str],
    agent_kind: Optional[str],
    group: str,
    background: int,
) -> Dict[str, Any]:
    """Template context for observability_explorer.html (both routes)."""
    from admin._channel_picker import fetch_channels_for_picker, visible_agent_kinds_for_picker
    from admin.screens.observability import (
        _BACKGROUND_AGENT_KINDS, filter_traces_by_tool_result, group_traces_by_session,
        list_tool_names_from_db, load_traces_list, tool_filter_notice,
    )

    include_background = bool(background) or (agent_kind in _BACKGROUND_AGENT_KINDS)
    unavailable = False
    try:
        data = load_traces_list(
            past_hours=hours, page_size=100, search=search, status=status,
            channel_slug=channel_slug, agent_kind=agent_kind, env=env,
            include_background=include_background,
        )
    except Exception as exc:  # noqa: BLE001 - the shell must render
        logger.warning("conversations: load_traces_list failed (%s)", exc)
        data = {"traces": [], "notice": "Observability backend unavailable."}
    traces = data.get("traces", [])
    if data.get("notice"):
        unavailable = True
    tool_filter_applied = True
    if tool:
        traces, tool_filter_applied = filter_traces_by_tool_result(traces, tool)

    group_by_session = group != "none"
    groups = group_traces_by_session(traces) if group_by_session else _single_groups(traces)
    try:
        channels = fetch_channels_for_picker()
    except Exception:  # noqa: BLE001
        channels = []
    try:
        enrich_groups(groups, env=env, channels=channels)
    except Exception as exc:  # noqa: BLE001 - names are cosmetic
        logger.warning("conversations: enrichment failed (%s)", exc)
        for g in groups:
            g.setdefault("agent_name", pl.agent_label(g.get("agent_kind")))
            g.setdefault("outcome", _group_outcome(g))
            g["outcome_label"], g["outcome_variant"] = pl.OUTCOMES[g["outcome"]]
            g.setdefault("when", friendly_time(g.get("latest_start")))
            g.setdefault("newest_trace_id", (g.get("traces") or [{}])[0].get("id"))
    if outcome in pl.OUTCOMES:
        groups = [g for g in groups if g.get("outcome") == outcome]

    try:
        tool_names = list_tool_names_from_db(env)
    except Exception:  # noqa: BLE001
        tool_names = []
    agent_names = pl.load_agent_display_names(env)
    from admin.agent_kinds import in_build

    kinds = list(visible_agent_kinds_for_picker())
    for k in agent_names:
        # Every agent with a name — on a partner build only its own.
        if k not in kinds and k not in _BACKGROUND_AGENT_KINDS and in_build(k):
            kinds.append(k)
    agent_options = sorted(
        ({"kind": k, "name": pl.agent_label(k, agent_names)} for k in kinds),
        key=lambda o: o["name"].lower(),
    )
    more_filters_active = bool(tool) or include_background or not group_by_session or bool(status)
    return {
        "groups": groups,
        "total": len(groups),
        "traces": traces,
        "tool_names": tool_names,
        "channels": channels,
        "agent_options": agent_options,
        "agent_kinds": kinds,
        "hours_options": HOURS_OPTIONS,
        "outcome_options": OUTCOME_OPTIONS,
        "past_hours": hours,
        "search": search or "",
        "status": status or "",
        "outcome": outcome or "",
        "tool": tool or "",
        "channel_slug": channel_slug or "",
        "agent_kind": agent_kind or "",
        "group_by_session": group_by_session,
        "show_background": include_background,
        "more_filters_active": more_filters_active,
        "list_unavailable": unavailable,
        "list_detail": data.get("notice"),
        "tool_filter_failed": not tool_filter_applied,
        "tool_filter_notice": None if tool_filter_applied else tool_filter_notice(tool),
    }
