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


class RenameSessionRequest(BaseModel):
    title: str


@router.get("/sessions")
async def list_ai_sessions(
    business_id: str = Query(..., description="Business UUID to scope sessions to"),
    source: Optional[str] = Query(
        None, description="Optional metadata.source filter, e.g. 'pulse'"
    ),
    authorization: Optional[str] = Header(None),
):
    """List the caller's active chat sessions for a business, newest first."""
    _, user_id = await asyncio.to_thread(authenticate_verified, authorization)
    sessions = await chat_persistence.list_sessions(user_id, business_id, source=source)
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_ai_session(
    session_id: str,
    authorization: Optional[str] = Header(None),
):
    """Return a single session owned by the caller."""
    _, user_id = await asyncio.to_thread(authenticate_verified, authorization)
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
    _, user_id = await asyncio.to_thread(authenticate_verified, authorization)
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
    _, user_id = await asyncio.to_thread(authenticate_verified, authorization)
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
    _, user_id = await asyncio.to_thread(authenticate_verified, authorization)
    ok = await chat_persistence.soft_delete_session(session_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True}
