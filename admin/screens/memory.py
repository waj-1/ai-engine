# ai_agent/admin/screens/memory.py
"""Context builders for the /admin/memory screen.

Three tabs: memories (per-user browse/curate), config (memory_config editor +
kill-switch), inspector (per-session summary + retrieved memories). DB access
goes through the per-env client passed in by the handler (client_for(env)).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from core.memory.config import resolve_memory_config

logger = logging.getLogger(__name__)

# The kind the memory screens open on: the plugin's (`agent_catalog.
# default_kind()`), else the generic one.
from admin.agent_kinds import catalog_tuple as _catalog_tuple  # noqa: E402

_DEFAULT_AGENT_KIND = (_catalog_tuple("default_kind") or ("custom",))[0]


def _exec(query):
    return query.execute()


# --- Config tab -------------------------------------------------------------

def shape_config_for_form(raw_memory_config: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the (defaults-merged) memory_config into flat form fields."""
    cfg = resolve_memory_config(raw_memory_config)
    return {
        "enabled": bool(cfg["enabled"]),
        "window_max_messages": cfg["window"]["max_messages"],
        "window_max_message_chars": cfg["window"]["max_message_chars"],
        "window_suggestion_context_messages": cfg["window"]["suggestion_context_messages"],
        "extraction_model_slot": cfg["extraction"]["model_slot"],
        "extraction_importance_threshold": cfg["extraction"]["importance_threshold"],
        "extraction_max_per_turn": cfg["extraction"]["max_per_turn"],
        "dedup_similarity_threshold": cfg["dedup"]["similarity_threshold"],
        "retrieval_top_k": cfg["retrieval"]["top_k"],
        "retrieval_min_similarity": cfg["retrieval"]["min_similarity"],
        "retrieval_always_include_pinned": bool(cfg["retrieval"]["always_include_pinned"]),
        "summary_trigger_every_n_turns": cfg["summary"]["trigger_every_n_turns"],
        "summary_keep_recent_verbatim": cfg["summary"]["keep_recent_verbatim"],
        "summary_model_slot": cfg["summary"]["model_slot"],
        "retention_days": cfg["retention_days"],
    }


def _to_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_config_form(form: Dict[str, Any]) -> Dict[str, Any]:
    """Build a nested memory_config dict from posted flat form fields.

    Checkbox fields arrive as 'on' when checked, absent when unchecked.
    """
    return {
        "enabled": form.get("enabled") == "on",
        "window": {
            "max_messages": _to_int(form.get("window_max_messages"), 10),
            "max_message_chars": _to_int(form.get("window_max_message_chars"), 4000),
            "suggestion_context_messages": _to_int(
                form.get("window_suggestion_context_messages"), 6),
        },
        "extraction": {
            "model_slot": form.get("extraction_model_slot", "light"),
            "importance_threshold": _to_float(form.get("extraction_importance_threshold"), 0.5),
            "max_per_turn": _to_int(form.get("extraction_max_per_turn"), 5),
        },
        "dedup": {
            "similarity_threshold": _to_float(form.get("dedup_similarity_threshold"), 0.9),
        },
        "retrieval": {
            "top_k": _to_int(form.get("retrieval_top_k"), 6),
            "min_similarity": _to_float(form.get("retrieval_min_similarity"), 0.7),
            "always_include_pinned": form.get("retrieval_always_include_pinned") == "on",
        },
        "summary": {
            "trigger_every_n_turns": _to_int(form.get("summary_trigger_every_n_turns"), 6),
            "keep_recent_verbatim": _to_int(form.get("summary_keep_recent_verbatim"), 4),
            "model_slot": form.get("summary_model_slot", "light"),
        },
        "retention_days": _to_int(form.get("retention_days"), None) if form.get("retention_days") else None,
    }


def active_profile_row(client, agent_kind: str, env: Optional[str],
                       select: str = "profile_id, slug, memory_config") -> Optional[Dict[str, Any]]:
    """The single active profile for an agent_kind in one environment.

    The AI config DB holds both the staging and prod rows of every agent_kind
    (staging and prod share one Supabase project, distinguished by the
    ``environment`` column). Without the ``environment`` predicate, a
    ``.limit(1)`` over agent_kind+is_active+status matches BOTH rows and returns
    whichever the DB happens to sort first — so a prod read/write could silently
    land on the staging profile (and vice-versa). ``env`` is required for a
    correct single-profile lookup; it's optional only so a per-env-DB caller
    that already scoped its client can pass ``None`` deliberately.
    """
    q = (client.table("agent_profiles").select(select)
         .eq("agent_kind", agent_kind).eq("is_active", True).eq("status", "active"))
    if env:
        q = q.eq("environment", env)
    rows = _exec(q.limit(1)).data or []
    return rows[0] if rows else None


def load_config_context(client, agent_kind: str = _DEFAULT_AGENT_KIND,
                        env: Optional[str] = None) -> Dict[str, Any]:
    """Fetch the agent profile row + its memory_config for ``env``."""
    profile = active_profile_row(client, agent_kind, env)
    form = shape_config_for_form((profile or {}).get("memory_config") or {})
    return {"profile": profile, "config_form": form}


# --- Memories tab -----------------------------------------------------------

def _resolve_user_id(query: str) -> Optional[str]:
    """Accept a uuid directly, or resolve an email via the MAIN project."""
    query = (query or "").strip()
    if not query:
        return None
    try:
        return str(uuid.UUID(query))
    except (ValueError, TypeError):
        pass
    # Treat as email — look up in the main project's user table.
    try:
        from core.auth import get_main_client

        rows = (
            get_main_client().table("user").select("id").eq("email", query).limit(1)
            .execute().data or []
        )
        return rows[0]["id"] if rows else None
    except Exception as exc:
        logger.warning("memory admin: email resolve failed (%s)", exc)
        return None


def load_memories_context(
    client, user_query: Optional[str], agent_kind: str = _DEFAULT_AGENT_KIND
) -> Dict[str, Any]:
    """List a user's memories. `user_query` is a uuid or email."""
    user_id = _resolve_user_id(user_query) if user_query else None
    memories: List[Dict[str, Any]] = []
    if user_id:
        memories = (
            client.table("ai_user_memory").select("*")
            .eq("user_id", user_id).eq("agent_kind", agent_kind)
            .order("created_at", desc=True).limit(200).execute().data or []
        )
    return {
        "user_query": user_query or "",
        "resolved_user_id": user_id,
        "memories": memories,
    }


# --- Inspector tab ----------------------------------------------------------

def load_inspector_context(client, session_id: Optional[str]) -> Dict[str, Any]:
    """For a session: its rolling summary + per assistant-turn retrieved memories."""
    if not session_id:
        return {"session_id": "", "session": None, "turns": []}
    session_rows = (
        client.table("ai_chat_sessions")
        .select("id, title, running_summary, summary_message_count, summary_updated_at, user_id")
        .eq("id", session_id).limit(1).execute().data or []
    )
    session = session_rows[0] if session_rows else None
    msgs = (
        client.table("ai_chat_messages").select("id, role, content, metadata, created_at")
        .eq("session_id", session_id).eq("role", "assistant").order("created_at", desc=False)
        .limit(500).execute().data or []
    )
    # Resolve retrieved memory ids per assistant turn to their content.
    all_ids: List[str] = []
    for m in msgs:
        mem = ((m.get("metadata") or {}).get("memory") or {})
        all_ids.extend(mem.get("retrieved_memory_ids") or [])
    id_to_content: Dict[str, str] = {}
    if all_ids:
        mem_rows = (
            client.table("ai_user_memory").select("id, content")
            .in_("id", list(set(all_ids))).execute().data or []
        )
        id_to_content = {r["id"]: r["content"] for r in mem_rows}
    turns = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        mem = ((m.get("metadata") or {}).get("memory") or {})
        ids = mem.get("retrieved_memory_ids") or []
        turns.append({
            "message_id": m["id"],
            "created_at": m.get("created_at"),
            "content_preview": (m.get("content") or "")[:200],
            "retrieved": [{"id": i, "content": id_to_content.get(i, "(deleted)")} for i in ids],
            "summary_used": mem.get("summary_used", False),
        })
    return {"session_id": session_id, "session": session, "turns": turns}
