"""AI chat-session read/manage endpoints.

The chat transcript tables (``ai_chat_sessions`` / ``ai_chat_messages``)
live in the standalone AI Supabase project, which the Flutter app cannot
query directly: it has no copy of ``auth.users`` and no per-user RLS, only
a service-role policy. So the app reads its chat history through these
endpoints. The engine authenticates the caller's JWT, derives the user id,
and queries the AI project via the service client with explicit
user_id / business_id scoping (see ``core.chat_persistence``).

These are read/manage only — sessions and messages are *written* by the
streaming chat path (``core.chat_persistence.ensure_session`` /
``save_turn``).
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from core import chat_persistence
from core.auth import authenticate_verified

router = APIRouter(prefix="/ai", tags=["ai-sessions"])


async def _caller_user_id(authorization: Optional[str]) -> str:
    """Whose sessions this request may see: a signed-in user's, or, for a
    portal-issued API key, the key's own (a key acts as itself, so the chats
    it started through /v1/agents are the ones it can list and read here)."""
    from core import api_keys

    is_bearer, token = api_keys.split_bearer(authorization)
    if is_bearer and api_keys.looks_like_api_key(token):
        caller = await api_keys.authenticate(token, api_keys.SCOPE_CHAT)
        return caller["user_id"]
    _, user_id = await asyncio.to_thread(authenticate_verified, authorization)
    return user_id


class RenameSessionRequest(BaseModel):
    title: str


@router.get("/sessions")
async def list_ai_sessions(
    business_id: Optional[str] = Query(
        None, description="Business UUID to scope sessions to. Omit it to list the caller's "
                          "sessions that belong to no business (generic agents)."),
    source: Optional[str] = Query(
        None, description="Optional metadata.source filter, e.g. 'pulse'"
    ),
    authorization: Optional[str] = Header(None),
):
    """List the caller's active chat sessions, newest first: a business's when
    `business_id` is given, else the ones with no business. Always only the
    caller's own (the verified token's user id)."""
    user_id = await _caller_user_id(authorization)
    sessions = await chat_persistence.list_sessions(user_id, business_id or None, source=source)
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_ai_session(
    session_id: str,
    authorization: Optional[str] = Header(None),
):
    """Return a single session owned by the caller."""
    user_id = await _caller_user_id(authorization)
    session = await chat_persistence.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("/sessions/{session_id}/messages")
async def get_ai_session_messages(
    session_id: str,
    authorization: Optional[str] = Header(None),
):
    """Return a session's messages (oldest first) if owned by the caller."""
    user_id = await _caller_user_id(authorization)
    messages = await chat_persistence.list_session_messages(session_id, user_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"messages": messages}


@router.patch("/sessions/{session_id}")
async def rename_ai_session(
    session_id: str,
    body: RenameSessionRequest,
    authorization: Optional[str] = Header(None),
):
    """Rename a session owned by the caller."""
    user_id = await _caller_user_id(authorization)
    session = await chat_persistence.rename_session(session_id, user_id, body.title)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/sessions/{session_id}")
async def delete_ai_session(
    session_id: str,
    authorization: Optional[str] = Header(None),
):
    """Soft-delete a session owned by the caller."""
    user_id = await _caller_user_id(authorization)
    ok = await chat_persistence.soft_delete_session(session_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True}
