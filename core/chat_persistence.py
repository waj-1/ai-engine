"""
Server-side persistence for AI chat sessions and messages.

Reads/writes ``ai_chat_sessions`` / ``ai_chat_messages`` in the standalone AI
Supabase project via the service client (``get_service_client``). Business/user
scoping is enforced by explicit ``business_id`` / ``user_id`` filters in this
module, not Postgres RLS (the AI project has only a service-role policy).
Errors are swallowed on the write path — persistence failures must never abort
an SSE stream.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from core.auth import get_service_client

logger = logging.getLogger(__name__)

# ``ai_chat_sessions.metadata.client`` value stamped on conversations started by
# a signed-in merchant in a partner (scoped-build) portal — set by both entry points that
# can create one (the client's chat stream route and the duplex voice edge in
# ``endpoints/voice_live.py``), and filtered on by that portal's own history
# drawer via ``list_client_sessions``.
#
# It exists because a demo tenant's merchant identity is SHARED with machine
# traffic: the eval runner and smoke checks authenticate as the same demo
# merchant (the plugin's demo-session fallback) and out-number real portal
# conversations by roughly 25:1 on the live box. user_id/business_id alone
# therefore can't tell a human's history from a scenario run — this tag can.
PORTAL_CLIENT_TAG = "portal"


async def _execute(query) -> Any:
    """Run a (blocking, sync-Supabase) query off the event loop.

    The supabase-py client is synchronous; calling `.execute()` directly
    inside an async handler blocks the single-vCPU event loop and stalls
    every co-located request. Offloading to a worker thread keeps the loop
    free — critical because these run on the chat hot path (and several are
    fired via `asyncio.create_task`, which would otherwise block the loop
    when they execute).
    """
    return await asyncio.to_thread(query.execute)

# Stable namespace for deriving a UUID from a non-UUID session id. Fixed so the
# same client-supplied id always maps to the same row across turns.
_SESSION_NS = uuid.UUID("a4c1f2e0-7b3d-4e6a-9c2f-3d8b1e5a6c7d")


def coerce_session_id(session_id: Optional[str]) -> Optional[str]:
    """Return a value valid for the ``uuid`` ``ai_chat_sessions.id`` column.

    Some legacy chat request schemas accept arbitrary string session
    ids (``^[a-zA-Z0-9_-]{1,100}$``), but the chat tables key on ``uuid``. A raw
    non-UUID id makes every insert fail (``invalid input syntax for type uuid``)
    and silently drops anonymous chat history. We pass valid UUIDs through
    unchanged and map anything else to a deterministic UUIDv5 so the same id
    keeps landing on the same row. Returns ``None`` unchanged.
    """
    if session_id is None:
        return None
    try:
        return str(uuid.UUID(str(session_id)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(_SESSION_NS, str(session_id)))


def _writer_environment() -> str:
    """``prod`` / ``staging`` — stamped on new sessions as
    ``metadata.environment`` so per-environment conversation insights
    (core/conversation_insights.py) don't depend on telemetry. The table has
    no environment column of its own."""
    from core.agent_profile import _default_environment

    return _default_environment()


def _get_client(user_token: Optional[str] = None):
    """Chat tables live in the AI project; always use the AI service client.

    The `user_token` parameter is retained for call-site compatibility but no
    longer selects the client — business scoping is enforced via the row data
    (business_id / user_id) written below.
    """
    return get_service_client()


class SessionOwnershipError(PermissionError):
    """A write targeted a chat session that belongs to another user."""


async def session_owner_conflict(session_id: Optional[str], user_id: Optional[str]) -> bool:
    """True when ``session_id`` already exists and is NOT ``user_id``'s.

    The AI project has no per-user RLS (see module docstring), so the
    service-role upserts below would otherwise let any caller who knows a
    session id rewrite its ``user_id`` and take the transcript over. A row
    with no stored owner also counts as a conflict for an authenticated
    writer: claiming it would rewrite ``user_id``. Lookup failures raise —
    callers on a request path must fail closed.
    """
    if not session_id:
        return False
    sid = coerce_session_id(session_id)
    result = await _execute(
        _get_client().schema("public").table("ai_chat_sessions")
        .select("id, user_id").eq("id", sid).limit(1)
    )
    rows = result.data or []
    if not rows:
        return False
    return rows[0].get("user_id") != user_id


async def ensure_session(
    session_id: str,
    user_token: Optional[str] = None,
    business_id: Optional[str] = None,
    title: str = "",
    user_id: Optional[str] = None,
    session_type: str = "chat",
    source: str = "waj",
    agent_kind: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Create the session row on first message. No-op if row already exists.

    Args:
        session_id: UUID for the session
        user_token: User's JWT token for RLS enforcement (optional, falls back to service role)
        business_id: Business UUID (optional for anonymous sessions)
        title: Session title
        user_id: User UUID (optional for anonymous sessions)
        session_type: the session's kind of conversation, stored in
            metadata (same meaning as ``TurnIdentity.session_type``)
        source: the client surface the session came from (metadata.source)
        agent_kind: which agent owns the session (`ai_agent_kind` enum value,
            e.g. "custom"). Populates the
            `agent_kind` discriminator column the live-chat eval sampler
            filters on — without it those rows are invisible to real-data evals.
        extra_metadata: extra keys for the row's metadata (e.g. the record a
            conversation is about, for audit linkage). They never override
            ``source`` / ``session_type`` / ``environment``; None values are
            skipped.
    """
    try:
        session_id = coerce_session_id(session_id)
        logger.debug(f"[chat_persistence] ensure_session called for {session_id}, user_token={'present' if user_token else 'None'}")
        client = _get_client(user_token)
        metadata: Dict[str, Any] = {k: v for k, v in (extra_metadata or {}).items() if v}
        metadata.update({"source": source, "session_type": session_type,
                         "environment": _writer_environment()})

        row = {
            "id": session_id,
            "business_id": business_id,
            "title": title,
            "metadata": metadata,
        }
        if user_id:
            row["user_id"] = user_id
        if agent_kind:
            row["agent_kind"] = agent_kind

        if user_id and await session_owner_conflict(session_id, user_id):
            raise SessionOwnershipError(f"session {session_id} belongs to another user")
        logger.debug(f"[chat_persistence] upserting session row: {row}")
        await _execute(
            client.schema("public").table("ai_chat_sessions").upsert(row, on_conflict="id")
        )
        logger.debug(f"[chat_persistence] session ensured: {session_id}")
    except Exception as exc:
        logger.error(f"[chat_persistence] ensure_session failed for {session_id}: {exc}", exc_info=True)


async def update_session_title(
    session_id: str, title: str = "", user_token: Optional[str] = None,
) -> None:
    """Update the title on an existing session row."""
    try:
        session_id = coerce_session_id(session_id)
        client = _get_client(user_token)
        await _execute(
            client.schema("public").table("ai_chat_sessions").update({"title": title}).eq("id", session_id)
        )
        logger.debug(f"[chat_persistence] title updated for session {session_id}")
    except Exception as exc:
        logger.error(f"[chat_persistence] update_session_title failed for {session_id}: {exc}")


async def update_session_metadata(
    session_id: str,
    user_token: Optional[str],
    patch: Dict[str, Any],
) -> None:
    """Merge `patch` into the session's `metadata` jsonb column.

    Used by the chat agents to surface the latest suggested questions on
    the session row (read by the /admin/sessions/{id} detail view).
    Failures are swallowed — persistence shouldn't take down a chat turn.
    """
    try:
        session_id = coerce_session_id(session_id)
        client = _get_client(user_token)
        result = await _execute(
            client.schema("public").table("ai_chat_sessions").select("metadata")
            .eq("id", session_id).limit(1)
        )
        rows = result.data or []
        existing = (rows[0].get("metadata") if rows else {}) or {}
        merged = {**existing, **patch}
        await _execute(
            client.schema("public").table("ai_chat_sessions").update({"metadata": merged}).eq(
                "id", session_id
            )
        )
        logger.debug(f"[chat_persistence] metadata merged for session {session_id}")
    except Exception as exc:
        logger.error(f"[chat_persistence] update_session_metadata failed for {session_id}: {exc}")


async def save_turn(
    session_id: str,
    user_message: str = "",
    assistant_content: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    user_message_metadata: Optional[Dict[str, Any]] = None,
    user_token: Optional[str] = None,
) -> None:
    """Insert user + assistant messages for a completed turn.

    `metadata` is attached to the assistant row (existing behavior — token
    counts, proposals, etc.). `user_message_metadata` is attached to the
    user row when provided; used today to mark voice-transcribed inputs
    so they're identifiable in the chat history.
    """
    try:
        session_id = coerce_session_id(session_id)
        logger.debug(f"[chat_persistence] save_turn called for {session_id}, user_token={'present' if user_token else 'None'}, content_len={len(assistant_content)}")
        client = _get_client(user_token)
        # Stamp explicit, ordered created_at values. Both rows go in one insert,
        # so relying on the `DEFAULT now()` gives them an identical transaction
        # timestamp — and every reader orders by created_at, so the user/assistant
        # pair came back in a non-deterministic (often reversed) order, producing
        # an incoherent transcript. A 1ms gap makes the user turn sort strictly
        # before its assistant reply. (No audit trigger overrides created_at on
        # this AI-project table, so an explicit value sticks.)
        now = datetime.now(timezone.utc)
        user_ts = now.isoformat()
        assistant_ts = (now + timedelta(milliseconds=1)).isoformat()
        await _execute(
            client.schema("public").table("ai_chat_messages").insert(
                [
                    {
                        "session_id": session_id,
                        "role": "user",
                        "content": user_message,
                        "metadata": user_message_metadata or {},
                        "created_at": user_ts,
                    },
                    {
                        "session_id": session_id,
                        "role": "assistant",
                        "content": assistant_content,
                        "metadata": metadata,
                        "created_at": assistant_ts,
                    },
                ]
            )
        )
        logger.debug(f"[chat_persistence] turn saved for session {session_id}")
    except Exception as exc:
        logger.error(f"[chat_persistence] save_turn failed for {session_id}: {exc}")
        return

    # Best-effort: bump the session's updated_at so History sorts by last
    # activity instead of creation time (`ai_chat_sessions.updated_at` is
    # otherwise never touched after ensure_session's initial upsert). Kept in
    # its own try/except so a failure here — which would leave the row's
    # updated_at stale, not lose the turn just saved above — is logged
    # distinctly and never surfaces as a save_turn failure.
    try:
        await _execute(
            client.schema("public").table("ai_chat_sessions")
            .update({"updated_at": assistant_ts})
            .eq("id", session_id)
        )
    except Exception as exc:
        logger.error(f"[chat_persistence] updated_at bump failed for {session_id}: {exc}")


# ---------------------------------------------------------------------------
# Read / manage queries (back the /ai/sessions endpoints the app reads).
#
# The chat tables live in the standalone AI project, which has no per-user
# RLS and no copy of auth.users — so the app cannot query them directly. The
# engine reads on the app's behalf via the service client and scopes every
# query explicitly by user_id (from the caller's JWT) and business_id.
# ---------------------------------------------------------------------------


def _aggregate_last_message_metadata(
    messages: List[Dict[str, Any]], latest: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the `last_message_metadata` the app expects for a session.

    Mirrors the aggregation the Flutter client used to do over the joined
    messages: start from the latest message's metadata, then roll up every
    ``download_links.files`` across all the session's messages so a session
    row surfaces all downloadable reports (CSV/Excel), not just the last one.
    """
    meta = dict(latest.get("metadata") or {})
    files: List[Any] = []
    links_message: Optional[str] = None
    for m in messages:
        download_links = (m.get("metadata") or {}).get("download_links")
        if isinstance(download_links, dict):
            dl_files = download_links.get("files")
            if isinstance(dl_files, list) and dl_files:
                files.extend(dl_files)
                if links_message is None:
                    links_message = download_links.get("message")
    if files:
        meta["download_links"] = {"message": links_message, "files": files}
        meta["download_links_count"] = len(files)
    return meta


async def list_sessions(
    user_id: str, business_id: str, source: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return a user's active chat sessions for a business, newest first.

    When ``source`` is provided, restricts to rows whose ``metadata->>source``
    equals it (one client surface's sessions only). Each row carries
    the base ``ai_chat_sessions`` columns plus aggregated ``last_message_at`` /
    ``last_message_metadata``. Soft-deleted sessions are excluded.

    ``message_count`` is COMPUTED from the messages this function already
    fetches, never returned as the stored ``ai_chat_sessions.message_count``
    column — that column has had no writer since waj#1424 (2026-06-17) moved
    message writes to the engine, so every row reads 0 regardless of how many
    messages it actually has. "Newest first" is by last message activity
    (falling back to ``updated_at``/``created_at`` for a session with no
    messages), not by the DB-side ``updated_at`` order alone: ``save_turn``
    now bumps ``updated_at`` on every turn, but older rows saved before that
    fix won't have a trustworthy ``updated_at``, so the final sort re-derives
    the order from the fetched messages to stay correct for old and new rows
    alike.
    """
    client = get_service_client()
    query = (
        client.schema("public").table("ai_chat_sessions").select("*")
        .eq("user_id", user_id)
        .eq("business_id", business_id)
        .is_("deleted_at", "null")
    )
    if source is not None:
        query = query.eq("metadata->>source", source)
    query = query.order("updated_at", desc=True)
    result = await _execute(query)
    sessions = result.data or []
    if not sessions:
        return []

    session_ids = [s["id"] for s in sessions]
    msg_result = await _execute(
        client.schema("public").table("ai_chat_messages")
        .select("session_id, created_at, metadata")
        .in_("session_id", session_ids)
        .order("created_at", desc=False)
    )
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for m in (msg_result.data or []):
        by_session.setdefault(m.get("session_id"), []).append(m)

    for s in sessions:
        msgs = by_session.get(s["id"]) or []
        s["message_count"] = len(msgs)  # stored column is never maintained (see docstring)
        if msgs:
            latest = msgs[-1]  # ordered ascending → last is the most recent turn
            s["last_message_at"] = latest.get("created_at")
            s["last_message_metadata"] = _aggregate_last_message_metadata(msgs, latest)

    # Re-sort by actual last activity: last_message_at when known, else
    # updated_at/created_at. ISO 8601 timestamps with a consistent offset sort
    # correctly as strings, which is all every field here ever is.
    sessions.sort(
        key=lambda s: s.get("last_message_at") or s.get("updated_at") or s.get("created_at") or "",
        reverse=True,
    )
    return sessions


async def get_session(session_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Return a single session owned by ``user_id``, or None if not found.

    Mirrors ``list_sessions``: ``message_count`` is computed from the
    session's actual message rows rather than the unmaintained stored
    column (see that function's docstring), and ``last_message_at`` /
    ``last_message_metadata`` are aggregated the same way.
    """
    sid = coerce_session_id(session_id)
    client = get_service_client()
    result = await _execute(
        client.schema("public").table("ai_chat_sessions").select("*")
        .eq("id", sid).eq("user_id", user_id).limit(1)
    )
    rows = result.data or []
    if not rows:
        return None
    session = rows[0]

    msg_result = await _execute(
        client.schema("public").table("ai_chat_messages")
        .select("created_at, metadata")
        .eq("session_id", sid)
        .order("created_at", desc=False)
    )
    msgs = msg_result.data or []
    session["message_count"] = len(msgs)
    if msgs:
        latest = msgs[-1]
        session["last_message_at"] = latest.get("created_at")
        session["last_message_metadata"] = _aggregate_last_message_metadata(msgs, latest)
    return session


async def list_session_messages(
    session_id: str, user_id: str, *, exclude_agent_kinds: Sequence[str] = (),
) -> Optional[List[Dict[str, Any]]]:
    """Return a session's messages (oldest first) if owned by ``user_id``.

    Returns None when the session doesn't exist or isn't the caller's — the
    ownership check stands in for the per-user RLS the AI project lacks, so
    one user can never read another's transcript.

    ``exclude_agent_kinds``: sessions owned by one of these agent kinds are
    treated as not found (a portal chat must never serve another agent's
    transcript).
    """
    sid = coerce_session_id(session_id)
    client = get_service_client()
    owner = await _execute(
        client.schema("public").table("ai_chat_sessions").select("id, agent_kind")
        .eq("id", sid).eq("user_id", user_id).limit(1)
    )
    rows = owner.data or []
    if not rows or rows[0].get("agent_kind") in exclude_agent_kinds:
        return None
    result = await _execute(
        client.schema("public").table("ai_chat_messages").select("*")
        .eq("session_id", sid).order("created_at", desc=False)
    )
    return result.data or []


async def list_client_sessions(
    user_id: str,
    business_id: str,
    *,
    client_tag: str,
    limit: int = 50,
    exclude_agent_kinds: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """Sessions for ``user_id``/``business_id`` tagged ``metadata.client``.

    Backs a partner portal's own history drawer, where several very
    different producers share ONE identity: on a client demo the merchant's
    user_id is also what the eval runner and smoke checks write under. Filtering
    on the ``client`` tag (set from the request that had a real signed-in
    session) is what keeps machine traffic out of a human's history.

    Returns compact rows — ``{id, title, updated_at, message_count}`` — shaped
    for a list view, not the full session record:

    * ``title`` falls back to the first user message when the stored title is
      still the placeholder. Titles are written asynchronously by the title
      agent, so a just-finished conversation is very often still "New Chat".
    * ``message_count`` is COUNTED, never read from ``ai_chat_sessions``'s
      column of that name — that column is not maintained (it reads 0 for every
      row on the live demo, including sessions with 200+ messages).

    Sessions with no messages are omitted: they're the empty rows a page-load
    creates before anyone types.
    """
    # Local import: common.chat_session imports THIS module, so taking the
    # constant at module scope would close an import cycle.
    from common.chat_session import DEFAULT_TITLE

    supabase = get_service_client()
    query = (
        supabase.schema("public").table("ai_chat_sessions")
        .select("id, title, updated_at")
        .eq("user_id", user_id)
        .eq("business_id", business_id)
        .eq("metadata->>client", client_tag)
        .is_("deleted_at", "null")
    )
    for kind in exclude_agent_kinds:
        # NULL-safe: a bare `neq` would also drop legacy rows with no agent_kind.
        query = query.or_(f"agent_kind.is.null,agent_kind.neq.{kind}")
    result = await _execute(query.order("updated_at", desc=True).limit(limit))
    sessions = result.data or []
    if not sessions:
        return []

    msg_result = await _execute(
        supabase.schema("public").table("ai_chat_messages")
        .select("session_id, role, content")
        .in_("session_id", [s["id"] for s in sessions])
        .order("created_at", desc=False)
    )
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for m in (msg_result.data or []):
        by_session.setdefault(m.get("session_id"), []).append(m)

    out: List[Dict[str, Any]] = []
    for s in sessions:
        msgs = by_session.get(s["id"]) or []
        if not msgs:
            continue
        title = (s.get("title") or "").strip()
        if not title or title == DEFAULT_TITLE:
            first_user = next(
                (m.get("content") for m in msgs
                 if m.get("role") == "user" and m.get("content")),
                None,
            )
            if first_user:
                flat = " ".join(str(first_user).split())
                title = flat[:60] + "…" if len(flat) > 60 else flat
        out.append({
            "id": s["id"],
            "title": title or DEFAULT_TITLE,
            "updated_at": s.get("updated_at"),
            "message_count": len(msgs),
        })
    return out


async def load_history_for_agent(
    session_id: str, limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return the last ``limit`` messages of a session as agent-shaped history.

    Session-scoped (keyed only on ``session_id``), for anonymous flows like the
    public chat where there is no ``user_id`` to own the row — so the
    per-user ownership check in ``list_session_messages`` can't apply. This is
    safe there precisely because the endpoint is anonymous: the session_id is
    the only handle to the conversation, the same secret the browser already
    holds, and no authenticated user's transcript is reachable through it. Do
    NOT use this for authenticated agents (e.g. business) — they must keep
    going through ``list_session_messages`` so one user can't read another's
    session by guessing its id.

    Returns ``[{role, content}, ...]`` oldest-first, matching the shape
    ``BaseStreamingAgent.build_messages`` consumes. Best-effort: any DB error
    degrades to an empty list so a history hiccup never breaks the reply.
    """
    try:
        sid = coerce_session_id(session_id)
        client = get_service_client()
        # Order newest-first + limit so a long session only pulls the tail,
        # then reverse to oldest-first for the agent.
        result = await _execute(
            client.schema("public").table("ai_chat_messages")
            .select("role, content")
            .eq("session_id", sid)
            .order("created_at", desc=True)
            .limit(limit)
        )
        rows = list(reversed(result.data or []))
        return [{"role": r.get("role", "user"), "content": r.get("content") or ""}
                for r in rows]
    except Exception as exc:
        logger.error(f"[chat_persistence] load_history_for_agent failed for {session_id}: {exc}")
        return []


async def rename_session(
    session_id: str, user_id: str, title: str
) -> Optional[Dict[str, Any]]:
    """Rename a session owned by ``user_id``; return the updated row or None."""
    sid = coerce_session_id(session_id)
    client = get_service_client()
    result = await _execute(
        client.schema("public").table("ai_chat_sessions")
        .update({"title": title})
        .eq("id", sid).eq("user_id", user_id)
    )
    rows = result.data or []
    return rows[0] if rows else None


async def soft_delete_session(session_id: str, user_id: str) -> bool:
    """Soft-delete a session owned by ``user_id`` (sets ``deleted_at``).

    Returns True if a row was affected, False if the session doesn't exist
    or isn't the caller's. Done inline rather than via the main project's
    ``soft_delete_session`` RPC, which the AI project doesn't have.
    """
    sid = coerce_session_id(session_id)
    client = get_service_client()
    now = datetime.now(timezone.utc).isoformat()
    result = await _execute(
        client.schema("public").table("ai_chat_sessions")
        .update({"deleted_at": now})
        .eq("id", sid).eq("user_id", user_id)
    )
    return bool(result.data or [])
