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


# The kind every generic agent shares. Its memories belong to ONE agent,
# named by `agent_slug`; every other kind is scoped by kind alone, as before.
SLUG_SCOPED_KIND = "custom"


class MissingAgentScope(ValueError):
    """A custom-kind memory read or write named no agent."""


def scope_query(query, *, agent_kind: str, agent_slug: Optional[str]):
    """Narrow an ai_user_memory query to one agent's memories.

    A product kind is one agent, so its rows are read by kind (unchanged).
    Many generic agents share the `custom` kind, so theirs are read by kind
    AND slug: without the slug, a support agent would recall what a user told
    a sales agent. A custom-kind read with no slug fails closed."""
    query = query.eq("agent_kind", agent_kind)
    if agent_kind == SLUG_SCOPED_KIND:
        if not agent_slug:
            raise MissingAgentScope("custom-kind memory needs the agent's slug")
        query = query.eq("agent_slug", agent_slug)
    return query


async def _exec(query) -> Any:
    return await asyncio.to_thread(query.execute)


async def insert_memory(
    client,
    *,
    user_id: str,
    business_id: Optional[str],
    agent_kind: str,
    content: str,
    embedding: Optional[List[float]],
    importance: float = 0.5,
    source: str = "auto",
    pinned: bool = False,
    origin_session_id: Optional[str] = None,
    origin_message_id: Optional[str] = None,
    agent_slug: Optional[str] = None,
) -> None:
    if agent_kind == SLUG_SCOPED_KIND and not agent_slug:
        raise MissingAgentScope("custom-kind memory needs the agent's slug")
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
    if agent_slug and agent_kind == SLUG_SCOPED_KIND:
        row["agent_slug"] = agent_slug
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
    agent_slug: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Top-K active memories by cosine similarity via the match_user_memory RPC.

    A custom agent's call carries its slug (the RPC then reads only that
    agent's rows); a product kind's call is the same as before."""
    literal = to_vector_literal(query_embedding)
    params: Dict[str, Any] = {
        "p_user_id": user_id,
        "p_agent_kind": agent_kind,
        "p_query_embedding": literal,
        "p_match_count": match_count,
        "p_min_similarity": min_similarity,
    }
    if agent_kind == SLUG_SCOPED_KIND:
        if not agent_slug:
            raise MissingAgentScope("custom-kind memory needs the agent's slug")
        params["p_agent_slug"] = agent_slug
    result = await _exec(client.rpc("match_user_memory", params))
    return result.data or []


async def list_pinned(
    client, *, user_id: str, agent_kind: str, agent_slug: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query = client.table("ai_user_memory").select("id, content, importance, pinned, source") \
        .eq("user_id", user_id)
    result = await _exec(
        scope_query(query, agent_kind=agent_kind, agent_slug=agent_slug)
        .eq("status", "active")
        .eq("pinned", True)
    )
    return result.data or []


async def nearest_one(
    client, *, user_id: str, agent_kind: str, query_embedding: List[float],
    agent_slug: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Single nearest active memory (for the dedup decision). min_similarity=0."""
    rows = await match_memories(
        client, user_id=user_id, agent_kind=agent_kind, agent_slug=agent_slug,
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


async def list_for_user(
    client, *, user_id: str, agent_kind: str, limit: int = 200,
    agent_slug: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Admin browse: all of a user's memories (active + archived), newest first."""
    query = client.table("ai_user_memory").select("*").eq("user_id", user_id)
    result = await _exec(
        scope_query(query, agent_kind=agent_kind, agent_slug=agent_slug)
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