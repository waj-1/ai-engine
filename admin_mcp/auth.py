"""Bearer auth for the mounted MCP app.

Runs BEFORE any MCP protocol handling: extracts the Authorization header and
accepts one of two credentials, then stashes the actor identity in MCP_ACTOR
for audit attribution. 401/403 come back as plain JSON.

- A **portal-issued API key** (`wae_...`) with the `admin` scope
  (`core/api_keys.py`). A key with only the `chat` scope is refused with 403:
  it can talk to agents, it cannot administer the engine. The actor is the key
  itself (its row id), so the audit log shows which key made a change.
- A **superuser token**: the JWT signature + superuser flag are verified via
  admin.auth.resolve_superuser (the same trust boundary as the portal).
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import HTTPException

from admin.auth import resolve_superuser
from admin_mcp.context import MCP_ACTOR
from core import api_keys

logger = logging.getLogger(__name__)


class MCPAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return await self._reject(
                send, 401, "MCP requires Authorization: Bearer <API key or token>")
        token = authorization[len("Bearer "):].strip()
        try:
            if api_keys.looks_like_api_key(token):
                ctx = await api_keys.authenticate(token, api_keys.SCOPE_ADMIN)
            else:
                # resolve_superuser makes a synchronous Supabase network call;
                # run it off-thread so it doesn't stall the single-worker event
                # loop on every MCP message.
                ctx = await asyncio.to_thread(resolve_superuser, token)
        except HTTPException as exc:
            return await self._reject(send, exc.status_code, str(exc.detail))

        reset = MCP_ACTOR.set(
            {"user_id": ctx["user_id"], "email": ctx["email"],
             "user_token": ctx["user_token"],
             # Set only for a key: lets a tool say which key acted.
             **({"api_key_id": ctx["api_key_id"]} if ctx.get("api_key_id") else {})}
        )
        try:
            await self.app(scope, receive, send)
        finally:
            MCP_ACTOR.reset(reset)

    @staticmethod
    async def _reject(send, status: int, detail: str) -> None:
        body = json.dumps({"error": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
