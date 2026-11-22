# ai_agent/core/memory/store.py
"""DB I/O for the memory layer (ai_user_memory + session summary columns).

All calls are offloaded to a worker thread (supabase-py is sync) so they're
safe to await on the async chat hot path. Every function accepts an explicit
`client` so the admin portal can pass a per-env client and tests can pass a
fake; `default_client()` returns the AI service client otherwise.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from core.auth import get_service_client
from core.memory.embeddings import to_vector_literal

logger = logging.getLogger(__name__)


def default_client():
    return get_service_client()


async def _exec(query) -> Any:
    return await asyncio.to_thread(query.execute)


async def insert_memory(
    client,
    *,
    user_id: str,
    business_id: str,
    agent_kind: str,
    content: str,
    embedding: Optional[List[float]],
    importance: float = 0.5,
    source: str = "auto",
    pinned: bool = False,
    origin_session_id: Optional[str] = None,
    origin_message_id: Optional[str] = None,
) -> None:
    row: Dict[str, Any] = {
        "user_id": user_id,
        "business_id": business_id,
        "agent_kind": agent_kind,
        "content": content,
        "importance": importance,
        "source": source,
        "pinned": pinned,
        "origin_session_id": origin_session_id,
        "origin_message_id": origin_message_id,
    }
    if embedding is not None:
        row["embedding"] = to_vector_literal(embedding)
    await _exec(client.table("ai_user_memory").insert(row))


async def update_memory_content(client, *, memory_id: str, content: str, embedding: Optional[List[float]]) -> None:
    patch: Dict[str, Any] = {"content": content}
    if embedding is not None:
        patch["embedding"] = to_vector_literal(embedding)
    await _exec(client.table("ai_user_memory").update(patch).eq("id", memory_id))


async def archive_memory(client, *, memory_id: str) -> None:
    await _exec(client.table("ai_user_memory").update({"status": "archived"}).eq("id", memory_id))


async def set_pinned(client, *, memory_id: str, pinned: bool) -> None:
    await _exec(client.table("ai_user_memory").update({"pinned": pinned}).eq("id", memory_id))


async def match_memories(
    client,
    *,
    user_id: str,
    agent_kind: str,
    query_embedding: List[float],
    match_count: int,
    min_similarity: float,
) -> List[Dict[str, Any]]:
    """Top-K active memories by cosine similarity via the match_user_memory RPC."""
    literal = to_vector_literal(query_embedding)
    result = await _exec(
        client.rpc(
            "match_user_memory",
            {
                "p_user_id": user_id,
                "p_agent_kind": agent_kind,
                "p_query_embedding": literal,
                "p_match_count": match_count,
                "p_min_similarity": min_similarity,
            },
        )
    )
    return result.data or []


async def list_pinned(client, *, user_id: str, agent_kind: str) -> List[Dict[str, Any]]:
    result = await _exec(
        client.table("ai_user_memory")
        .select("id, content, importance, pinned, source")
        .eq("user_id", user_id)
        .eq("agent_kind", agent_kind)
        .eq("status", "active")
        .eq("pinned", True)
    )
    return result.data or []


async def nearest_one(
    client, *, user_id: str, agent_kind: str, query_embedding: List[float],
) -> Optional[Dict[str, Any]]:
    """Single nearest active memory (for the dedup decision). min_similarity=0."""
    rows = await match_memories(
        client, user_id=user_id, agent_kind=agent_kind,
        query_embedding=query_embedding, match_count=1, min_similarity=0.0,
    )
    return rows[0] if rows else None


async def bump_usage(client, *, memory_ids: List[str]) -> None:
    """Best-effort: increment use_count + refresh last_used_at for retrieved memories."""
    if not memory_ids:
        return
    try:
        await _exec(client.rpc("touch_user_memory", {"p_ids": memory_ids}))
    except Exception as exc:
        logger.debug("memory.store: bump_usage skipped (%s)", exc)


async def list_for_user(client, *, user_id: str, agent_kind: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Admin browse: all of a user's memories (active + archived), newest first."""
    result = await _exec(
        client.table("ai_user_memory")
        .select("*")
        .eq("user_id", user_id)
        .eq("agent_kind", agent_kind)
        .order("created_at", desc=True)
        .limit(limit)
    )
    return result.data or []


async def get_session_summary(client, *, session_id: str) -> Dict[str, Any]:
    result = await _exec(
        client.table("ai_chat_sessions")
        .select("running_summary, summary_message_count")
        .eq("id", session_id)
        .limit(1)
    )
    rows = result.data or []
    return rows[0] if rows else {}


async def set_session_summary(
    client, *, session_id: str, summary: str, message_count: int,
    expected_count: Optional[int] = None,
) -> bool:
    """Write the session's running summary; True when a row was updated.

    With `expected_count`, write only if `summary_message_count` still equals
    it, i.e. no other save refreshed the summary since this one read it. Two
    quick messages run two background saves at once; without the check the
    one that finishes last overwrites the other's summary, newer or not.
    """
    from datetime import datetime, timezone

    query = client.table("ai_chat_sessions").update(
        {
            "running_summary": summary,
            "summary_message_count": message_count,
            "summary_updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", session_id)
    if expected_count is not None:
        query = query.eq("summary_message_count", expected_count)
    result = await _exec(query)
    return bool(result.data)


async def count_messages(client, *, session_id: str) -> int:
    """Exact row count, computed by the database: counting fetched rows would
    stop at one PostgREST page (`_PAGE_ROWS`)."""
    result = await _exec(
        client.table("ai_chat_messages").select("id", count="exact", head=True)
        .eq("session_id", session_id)
    )
    return int(result.count or 0)


# PostgREST returns at most this many rows per request (supabase/config.toml
# `max_rows`; hosted projects default to the same), so longer reads are paged.
_PAGE_ROWS = 1000


async def list_session_history(
    client, *, session_id: str, start: int = 0, end: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Transcript rows [start, end), oldest first, as [{role, content}].

    Paged, so a thread longer than one PostgREST page is read in full."""
    rows: List[Dict[str, Any]] = []
    while end is None or start < end:
        last = start + _PAGE_ROWS - 1 if end is None else min(start + _PAGE_ROWS, end) - 1
        result = await _exec(
            client.table("ai_chat_messages").select("role, content")
            .eq("session_id", session_id).order("created_at", desc=False)
            .range(start, last)
        )
        page = result.data or []
        rows.extend({"role": r.get("role", "user"), "content": r.get("content") or ""}
                    for r in page)
        if len(page) < last - start + 1:
            break
        start = last + 1
    return rows