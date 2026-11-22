"""Bearer-JWT auth for the mounted MCP app.

Runs BEFORE any MCP protocol handling: extracts the Authorization header,
verifies the JWT signature + superuser flag via admin.auth.resolve_superuser
(the same trust boundary as the portal), and stashes the actor identity in
MCP_ACTOR for audit attribution. 401/403 come back as plain JSON.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import HTTPException

from admin.auth import resolve_superuser
from admin_mcp.context import MCP_ACTOR

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
            return await self._reject(send, 401, "MCP requires Authorization: Bearer <jwt>")
        token = authorization[len("Bearer "):].strip()
        try:
            # resolve_superuser makes a synchronous Supabase network call;
            # run it off-thread so it doesn't stall the single-worker event
            # loop on every MCP message.
            ctx = await asyncio.to_thread(resolve_superuser, token)
        except HTTPException as exc:
            return await self._reject(send, exc.status_code, str(exc.detail))

        reset = MCP_ACTOR.set(
            {"user_id": ctx["user_id"], "email": ctx["email"],
             "user_token": ctx["user_token"]}
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
