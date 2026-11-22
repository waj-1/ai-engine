"""Conversations from the saved chats, for an install with no tracing store.

The Conversations screen (/admin/observability) was built on the tracing store
(ClickHouse). A stack that runs with tracing off (`LMNR_DISABLED=true`, the
local compose default) has no such store, yet every conversation is saved in
Postgres: `ai_chat_sessions`, `ai_chat_messages`, and one `agent_traces` row
per turn (tokens, latency, model, cost, ok/error). This module builds the same
screen from those rows.

It is used ONLY when `core.lmnr_clickhouse.tracing_configured()` is False.
With tracing configured the screen reads the tracing store exactly as before
and nothing here runs.

How it plugs in
---------------
The screen identifies a conversation by a trace id (the URL hash, the pane
endpoints). Here the id is the chat SESSION id, so the list rows, deep links
and pane endpoints work unchanged; `admin/observability_explorer_handlers.py`
routes each pane to the loader below when tracing is off.

What it shows, and what it cannot
---------------------------------
* The transcript is the persisted messages, read by the same function the
  tracing-backed transcript uses (`observability._persisted_conversation`):
  the same rows, the same fields, for every agent kind. Nothing is added from
  another source, so a kind whose telemetry is exported redacted
  (core/sensitive_content.py) shows here exactly what it shows there.
* Tool calls appear where the assistant message's metadata carries them.
* Tokens, response time, model and cost come from `agent_traces`, one row per
  turn, matched to the replies in order.
* Step-level detail (each model call, tool inputs and outputs, timings inside
  a turn) exists only in the tracing store. The screen says so in a plain
  note; it is not an error.

Everything is synchronous (supabase-py): callers run it off the event loop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from admin.screens import plain_language as pl

logger = logging.getLogger(__name__)

LIST_LIMIT = 100
_MESSAGE_ROWS_LIMIT = 5000
_SEARCH_HITS_LIMIT = 500

LIST_NOTE = (
    "Tracing is off on this install, so this list comes from the saved conversations. "
    "Step-by-step detail (timings, each model call) needs the tracing stack."
)
STEPS_NOTE = (
    "Step-by-step detail (each model call, tool inputs and outputs, timings inside a reply) "
    "is recorded by the tracing stack, which is off on this install. The conversation above "
    "and the totals here come from the saved chat."
)
_SOURCE_LABELS = {"api": "API", "portal": "Portal", "voice": "Voice call", "mcp": "MCP"}


def _client(env: Optional[str]):
    if env:
        from admin.router import client_for

        return client_for(env)
    from core.auth import admin_service_client

    return admin_service_client()


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _like_pattern(search: str) -> str:
    """`search` as an ILIKE pattern matching it literally anywhere."""
    escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def _search_session_ids(client, search: str, cutoff: str) -> List[str]:
    """Sessions whose messages or title contain `search` (case-insensitive)."""
    pattern = _like_pattern(search)
    ids: List[str] = []
    try:
        hits = (client.schema("public").table("ai_chat_messages").select("session_id")
                .ilike("content", pattern).gte("created_at", cutoff)
                .order("created_at", desc=True).limit(_SEARCH_HITS_LIMIT).execute().data or [])
        ids.extend(str(r["session_id"]) for r in hits if r.get("session_id"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved chats: message search unavailable (%s)", exc)
    try:
        titled = (client.schema("public").table("ai_chat_sessions").select("id")
                  .ilike("title", pattern).is_("deleted_at", "null")
                  .limit(_SEARCH_HITS_LIMIT).execute().data or [])
        ids.extend(str(r["id"]) for r in titled if r.get("id"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved chats: title search unavailable (%s)", exc)
    return list(dict.fromkeys(ids))


_SESSION_COLUMNS = "id, business_id, title, message_count, metadata, agent_kind, created_at, updated_at"


def _recently_active_ids(client, cutoff: str) -> List[str]:
    """Sessions with a message in the window. A session started earlier and
    continued inside the window belongs in the list even where its
    `updated_at` was not moved by the turn."""
    try:
        rows = (client.schema("public").table("ai_chat_messages").select("session_id")
                .gte("created_at", cutoff).order("created_at", desc=True)
                .limit(_MESSAGE_ROWS_LIMIT).execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved chats: recent messages unavailable (%s)", exc)
        return []
    return list(dict.fromkeys(str(r["session_id"]) for r in rows if r.get("session_id")))


def _session_rows(client, *, cutoff: str, agent_kind: Optional[str],
                  ids: Optional[List[str]]) -> List[Dict[str, Any]]:
    """Sessions created or active since `cutoff` (not deleted), optionally
    narrowed to `ids` (a search's hits)."""
    def query():
        q = (client.schema("public").table("ai_chat_sessions").select(_SESSION_COLUMNS)
             .is_("deleted_at", "null"))
        return q.eq("agent_kind", agent_kind) if agent_kind else q

    q = query().gte("updated_at", cutoff)
    if ids is not None:
        q = q.in_("id", ids)
    rows = q.order("updated_at", desc=True).limit(LIST_LIMIT).execute().data or []
    have = {str(r["id"]) for r in rows}
    extra = [i for i in _recently_active_ids(client, cutoff)
             if i not in have and (ids is None or i in ids)][:LIST_LIMIT]
    if extra:
        rows += query().in_("id", extra).limit(LIST_LIMIT).execute().data or []
    return rows


def _message_facts(client, sids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per session: first user message, message count, first and last time."""
    if not sids:
        return {}
    try:
        rows = (client.schema("public").table("ai_chat_messages")
                .select("session_id, role, content, created_at")
                .in_("session_id", sids).order("created_at")
                .limit(_MESSAGE_ROWS_LIMIT).execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved chats: messages unavailable (%s)", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sid = str(r.get("session_id") or "")
        if not sid:
            continue
        f = out.setdefault(sid, {"count": 0, "first": None, "last": None, "first_user": None})
        if r.get("role") in ("user", "assistant"):
            f["count"] += 1
        ts = r.get("created_at")
        f["first"] = f["first"] or ts
        f["last"] = ts or f["last"]
        if f["first_user"] is None and r.get("role") == "user":
            text = " ".join(str(r.get("content") or "").split())
            if text:
                f["first_user"] = text[:200]
    return out


def _turn_rows(client, sids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """`agent_traces` rows per session, oldest first (one per turn)."""
    if not sids:
        return {}
    try:
        rows = (client.schema("public").table("agent_traces")
                .select("session_id, created_at, status, input_tokens, output_tokens, "
                        "cache_read_tokens, cache_creation_tokens, latency_ms, model_string, cost_usd")
                .in_("session_id", sids).order("created_at")
                .limit(_MESSAGE_ROWS_LIMIT).execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved chats: turn rows unavailable (%s)", exc)
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("session_id"):
            out.setdefault(str(r["session_id"]), []).append(r)
    return out


def _agent_names(client) -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    """((kind, slug) -> name, kind -> name). Many agents can share a kind
    (every generic agent is `custom`), so the slug saved on the session picks
    the right one."""
    by_slug: Dict[Tuple[str, str], str] = {}
    by_kind: Dict[str, str] = {}
    try:
        rows = (client.table("agent_profiles")
                .select("agent_kind, slug, display_name, parent_profile_id, status")
                .execute().data or [])
    except Exception as exc:  # noqa: BLE001 - names are cosmetic
        logger.debug("saved chats: agent names unavailable (%s)", exc)
        return by_slug, by_kind
    rows.sort(key=lambda r: 1 if r.get("parent_profile_id") else 0)
    for r in rows:
        kind, name = r.get("agent_kind"), (r.get("display_name") or "").strip()
        if not kind or not name or r.get("status") == "template":
            continue
        by_slug.setdefault((kind, r.get("slug") or ""), name)
        by_kind.setdefault(kind, name)
    return by_slug, by_kind


def _agent_name(kind: Optional[str], meta: Dict[str, Any],
                names: Tuple[Dict[Tuple[str, str], str], Dict[str, str]]) -> str:
    by_slug, by_kind = names
    slug = meta.get("agent_slug")
    if kind and slug:
        return by_slug.get((kind, slug)) or pl.humanize_slug(str(slug))
    return pl.agent_label(kind, by_kind)


def _channel(meta: Dict[str, Any], channel_names: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """(slug, label) for where the conversation came from, when it was saved."""
    slug = meta.get("channel_slug") or meta.get("channel")
    if isinstance(slug, str) and slug:
        return slug, channel_names.get(slug) or pl.humanize_slug(slug)
    source = meta.get("source")
    if isinstance(source, str) and source:
        return None, _SOURCE_LABELS.get(source.lower()) or pl.humanize_slug(source)
    return None, None


def list_conversations(*, env: Optional[str], hours: int, search: Optional[str] = None,
                       agent_kind: Optional[str] = None, channel_slug: Optional[str] = None,
                       outcome: Optional[str] = None,
                       channels: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Conversation rows for the list, newest activity first, shaped like the
    groups `conversations.enrich_groups` produces (the template is shared)."""
    from admin.screens.conversations import friendly_duration, friendly_time

    client = _client(env)
    now = datetime.now(timezone.utc)
    cutoff = _iso_z(now - timedelta(hours=hours))
    ids: Optional[List[str]] = None
    if (search or "").strip():
        ids = _search_session_ids(client, search.strip(), cutoff)
        if not ids:
            return []
    sessions = _session_rows(client, cutoff=cutoff, agent_kind=agent_kind or None, ids=ids)
    sids = [str(s["id"]) for s in sessions if s.get("id")]
    facts = _message_facts(client, sids)
    turns = _turn_rows(client, sids)
    names = _agent_names(client)
    channel_names = {c.get("slug"): c.get("name") or c.get("slug") for c in channels or []}
    businesses = pl.fetch_business_names(s.get("business_id") for s in sessions)

    groups: List[Dict[str, Any]] = []
    for s in sessions:
        sid = str(s["id"])
        meta = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
        ch_slug, ch_label = _channel(meta, channel_names)
        if channel_slug and ch_slug != channel_slug:
            continue
        f = facts.get(sid) or {}
        rows = turns.get(sid) or []
        has_error = any((r.get("status") or "ok") == "error" for r in rows)
        result = pl.outcome_for(failed_actions=0, has_error=has_error, handoffs=0)
        if outcome in pl.OUTCOMES and result != outcome:
            continue
        count = f.get("count") or (s.get("message_count") if isinstance(s.get("message_count"), int) else 0)
        last = f.get("last") or s.get("updated_at") or s.get("created_at")
        title = s.get("title")
        groups.append({
            "session_id": sid,
            "newest_trace_id": sid,
            "traces": [{"id": sid, "topSpanName": ""}],
            "agent_kind": s.get("agent_kind"),
            "agent_name": _agent_name(s.get("agent_kind"), meta, names),
            "channel_slug": ch_slug,
            "channel_name": ch_label,
            "is_voice": (meta.get("source") or "") == "voice",
            "business_name": businesses.get(str(s.get("business_id") or "")),
            "title": title if title and title != "New Chat" else None,
            "first_message": f.get("first_user"),
            "message_count": count or None,
            "turns": len(rows),
            "has_error": has_error,
            "actions": 0,
            "failed_actions": 0,
            "handoffs": 0,
            "simulated": False,
            "outcome": result,
            "outcome_label": pl.OUTCOMES[result][0],
            "outcome_variant": pl.OUTCOMES[result][1],
            "when": friendly_time(last, now),
            "started": friendly_time(f.get("first") or s.get("created_at"), now),
            "duration": friendly_duration(sum(int(r.get("latency_ms") or 0) for r in rows)),
            "_last": str(last or ""),
        })
    groups.sort(key=lambda g: g["_last"], reverse=True)
    return groups[:LIST_LIMIT]


def build_list_context(*, env: str, hours: int, search: Optional[str], outcome: Optional[str],
                       channel_slug: Optional[str], agent_kind: Optional[str]) -> Dict[str, Any]:
    """The Conversations template context, from the saved chats.

    Same keys as `conversations.build_conversations_context`. The filters the
    saved data cannot answer (action used, one row per reply, background
    activity, the Success/Error technical filter) are left off rather than
    silently ignored: `saved_chats` tells the template to hide them.
    """
    from admin._channel_picker import fetch_channels_for_picker, visible_agent_kinds_for_picker
    from admin.agent_kinds import in_build
    from admin.screens.conversations import HOURS_OPTIONS, OUTCOME_OPTIONS

    try:
        channels = fetch_channels_for_picker()
    except Exception:  # noqa: BLE001
        channels = []
    failed = False
    try:
        groups = list_conversations(env=env, hours=hours, search=search, agent_kind=agent_kind,
                                    channel_slug=channel_slug, outcome=outcome, channels=channels)
    except Exception as exc:  # noqa: BLE001 - the shell must render
        logger.warning("saved chats: list failed (%s)", exc)
        groups, failed = [], True
    agent_names = pl.load_agent_display_names(env)
    kinds = list(visible_agent_kinds_for_picker())
    for k in list(agent_names) + [g["agent_kind"] for g in groups if g.get("agent_kind")]:
        if k not in kinds and in_build(k):
            kinds.append(k)
    agent_options = sorted(
        ({"kind": k, "name": pl.agent_label(k, agent_names)} for k in kinds),
        key=lambda o: o["name"].lower())
    return {
        "groups": groups,
        "total": len(groups),
        "traces": [],
        "tool_names": [],
        "channels": channels,
        "agent_options": agent_options,
        "agent_kinds": kinds,
        "hours_options": HOURS_OPTIONS,
        "outcome_options": OUTCOME_OPTIONS,
        "past_hours": hours,
        "search": search or "",
        "status": "",
        "outcome": outcome or "",
        "tool": "",
        "channel_slug": channel_slug or "",
        "agent_kind": agent_kind or "",
        "group_by_session": True,
        "show_background": False,
        "more_filters_active": False,
        "list_unavailable": failed,
        "list_detail": "The saved conversations could not be read from the database." if failed else None,
        "tool_filter_failed": False,
        "tool_filter_notice": None,
        "saved_chats": True,
        "saved_chats_note": LIST_NOTE,
    }


# ---------------------------------------------------------------------------
# One conversation
# ---------------------------------------------------------------------------

def _session(session_id: str, env: Optional[str]) -> Optional[Dict[str, Any]]:
    """The saved session, or None when it does not exist or was deleted."""
    from admin.screens.observability import _coerce_session_id_for_lookup

    try:
        rows = (
            _client(env).schema("public").table("ai_chat_sessions")
            .select("id, agent_kind, business_id, branch_id, title, message_count, metadata, "
                    "created_at, deleted_at")
            .eq("id", _coerce_session_id_for_lookup(session_id)).limit(1).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved chats: session %s unavailable (%s)", session_id, exc)
        return None
    return rows[0] if rows and not rows[0].get("deleted_at") else None


def _session_turn_rows(session_id: str, env: Optional[str]) -> List[Dict[str, Any]]:
    from admin.screens.observability import _coerce_session_id_for_lookup

    sid = str(_coerce_session_id_for_lookup(session_id) or session_id)
    try:
        return _turn_rows(_client(env), [sid]).get(sid) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved chats: turn rows unavailable for %s (%s)", sid, exc)
        return []


def _attach_turn_usage(turns: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> None:
    """Put each turn row's numbers on its reply.

    A completed turn wrote one assistant message and one `agent_traces` row,
    in the same order, so they pair up one to one. A FAILED turn wrote a row
    and no assistant message; pairing skips those rows so one failure does not
    shift every later reply onto the wrong numbers.
    """
    ok_rows = [r for r in rows if (r.get("status") or "ok") != "error"]
    replies = [t for t in turns if t.get("role") == "assistant"]
    for turn, row in zip(replies, ok_rows):
        total = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
        if total and turn.get("tokens") is None:
            turn["tokens"] = total
        if row.get("cache_read_tokens"):
            turn["cache_read"] = int(row["cache_read_tokens"])
        if row.get("latency_ms") is not None:
            turn["latency_ms"] = int(row["latency_ms"])
        if row.get("model_string"):
            turn["model"] = row["model_string"]


def _attach_tools_used(turns: List[Dict[str, Any]], session_id: str, env: Optional[str]) -> None:
    """Name the actions a reply ran, from `metadata.tools_used` (the names
    `common.chat_session.run_turn` saves). Names only: what went in and came
    out of a tool is recorded by the tracing stack. A reply whose metadata
    already carries full tool calls keeps those."""
    from admin.screens.observability import _coerce_session_id_for_lookup

    if not any(t.get("role") == "assistant" and not t.get("tools") for t in turns):
        return
    try:
        rows = (_client(env).schema("public").table("ai_chat_messages")
                .select("role, metadata, created_at")
                .eq("session_id", _coerce_session_id_for_lookup(session_id))
                .order("created_at").limit(200).execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved chats: tools_used unavailable for %s (%s)", session_id, exc)
        return
    if len(rows) != len(turns):
        return  # not the same rows the transcript read: do not guess
    for turn, r in zip(turns, rows):
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        names = meta.get("tools_used")
        if turn.get("role") == "assistant" and not turn.get("tools") and isinstance(names, list):
            turn["tools"] = [{"name": str(n), "input": "", "output": "", "is_error": False,
                              "name_only": True} for n in names if n]


def _summary(row: Dict[str, Any], turns: List[Dict[str, Any]], rows: List[Dict[str, Any]],
             env: Optional[str]) -> Dict[str, Any]:
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    try:
        tool_names = pl.load_tool_display_names(env)
    except Exception:  # noqa: BLE001
        tool_names = {}
    failures: List[Dict[str, Any]] = []
    actions = handoffs = 0
    for i, turn in enumerate(turns):
        for tool in turn.get("tools") or []:
            actions += 1
            tool["label"] = pl.action_label(tool.get("name"), tool_names)
            tool["is_handoff"] = pl.is_handoff_tool(tool.get("name"))
            if tool.get("is_error"):
                reason = pl.plain_failure_reason(tool.get("error"), (tool.get("output") or "")[:600])
                tool["reason"] = reason
                failures.append({"sentence": pl.failure_sentence(tool["label"], reason),
                                 "turn_index": i, "agent_reply": None})
            elif tool["is_handoff"]:
                handoffs += 1
    has_error = any((r.get("status") or "ok") == "error" for r in rows)
    outcome = pl.outcome_for(failed_actions=len(failures), has_error=has_error, handoffs=handoffs)
    business_name = None
    if row.get("business_id"):
        business_name = pl.fetch_business_names([row["business_id"]]).get(str(row["business_id"]))
    try:
        names = _agent_names(_client(env))
    except Exception:  # noqa: BLE001
        names = ({}, {})
    _slug, channel = _channel(meta, {})
    title = row.get("title")
    return {
        "agent_kind": row.get("agent_kind"),
        "agent_name": _agent_name(row.get("agent_kind"), meta, names),
        "channel": channel,
        "business_id": row.get("business_id"),
        "business_name": business_name,
        "title": title if title not in (None, "", "New Chat") else None,
        "started": next((t.get("ts") for t in turns if t.get("ts")), None) or row.get("created_at"),
        "ended": next((t.get("ts") for t in reversed(turns) if t.get("ts")), None),
        "messages": sum(1 for t in turns if t.get("role") in ("user", "assistant")),
        "actions": actions,
        "failures": failures,
        "error_without_action": bool(has_error and not failures),
        "handoffs": handoffs,
        "outcome": outcome,
        "outcome_label": pl.OUTCOMES[outcome][0],
        "outcome_variant": pl.OUTCOMES[outcome][1],
        "simulated": False,
    }


_NOT_FOUND = "We couldn't find this conversation. The link may be wrong, or it was deleted."


def load_transcript_json(session_id: str, env: Optional[str] = None) -> Dict[str, Any]:
    """The transcript pane for a saved conversation (same shape as
    `observability.load_transcript_json`)."""
    from admin.screens.observability import _persisted_conversation

    row = _session(session_id, env)
    hdr = {"id": session_id, "status": None, "top_span_name": None, "session_id": session_id}
    if row is None:
        return {"trace": hdr, "session_id": session_id, "turns": [], "notice": _NOT_FOUND,
                "found": False, "unavailable": False, "summary": {}, "saved_chat": True}
    turns = _persisted_conversation(session_id, env)
    _attach_tools_used(turns, session_id, env)
    rows = _session_turn_rows(session_id, env)
    _attach_turn_usage(turns, rows)
    summary = _summary(row, turns, rows, env)
    hdr["status"] = "error" if summary["error_without_action"] or summary["failures"] else "ok"
    return {"trace": hdr, "session_id": session_id, "turns": turns,
            "notice": None if turns else "No messages were saved for this conversation.",
            "found": True, "unavailable": False, "summary": summary, "saved_chat": True}


def load_overview_json(session_id: str, env: Optional[str] = None) -> Dict[str, Any]:
    """The Technical pane's totals for a saved conversation: every turn's
    `agent_traces` row summed (same shape as `load_trace_overview_json`)."""
    row = _session(session_id, env)
    if row is None:
        return {"trace": None, "session": None, "tools": [], "tool_calls": 0, "errors": [],
                "found": False, "unavailable": False, "notice": _NOT_FOUND, "saved_chat": True}
    rows = _session_turn_rows(session_id, env)

    def total(key: str) -> int:
        return sum(int(r.get(key) or 0) for r in rows)

    costs = [float(r["cost_usd"]) for r in rows if r.get("cost_usd") is not None]
    failed = sum(1 for r in rows if (r.get("status") or "ok") == "error")
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    models = list(dict.fromkeys(r["model_string"] for r in rows if r.get("model_string")))
    return {
        "trace": {
            "id": session_id,
            "status": "error" if failed else "ok",
            "duration_ms": total("latency_ms") if rows else None,
            "input_tokens": total("input_tokens") if rows else None,
            "output_tokens": total("output_tokens") if rows else None,
            "total_cost": sum(costs) if costs else None,
            "cache_read_tokens": total("cache_read_tokens") if rows else None,
            "cache_creation_tokens": total("cache_creation_tokens") if rows else None,
            "replies": len(rows),
            "failed_replies": failed,
            "models": models,
        },
        "session": {
            "id": row.get("id"), "agent_kind": row.get("agent_kind"),
            "business_id": row.get("business_id"), "branch_id": row.get("branch_id"),
            "title": row.get("title"), "message_count": row.get("message_count"),
            "locale": meta.get("locale"), "source": meta.get("source"),
            "created_at": row.get("created_at"),
        },
        "tools": [], "tool_calls": None, "errors": [],
        "found": True, "unavailable": False, "notice": None, "saved_chat": True,
    }


def load_steps_json(session_id: str) -> Dict[str, Any]:
    """The steps pane: there are none without the tracing store. A plain
    note, never an error."""
    return {"trace": None, "tree": [], "found": True, "unavailable": False,
            "notice": STEPS_NOTE, "saved_chat": True}


def load_audio_json(session_id: str, env: Optional[str] = None) -> Dict[str, Any]:
    """Call recordings of a saved conversation (they are stored with the
    session, not in the tracing store)."""
    from admin.screens.observability import (
        _coerce_session_id_for_lookup, _session_recordings,
    )

    lookup_id = _coerce_session_id_for_lookup(session_id)
    envs: List[Optional[str]] = []
    for e in (env, None):
        if e not in envs:
            envs.append(e)
    recordings = _session_recordings(lookup_id, envs, None) if lookup_id else []
    return {"trace": {"id": session_id}, "session_id": session_id, "recordings": recordings,
            "notice": None if recordings else "No call recordings for this conversation.",
            "saved_chat": True}
