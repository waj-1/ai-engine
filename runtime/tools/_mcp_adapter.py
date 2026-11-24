"""Minimal MCP (Model Context Protocol) client over Streamable HTTP / JSON-RPC 2.0.

Used by the engine to call external MCP servers (a partner's real server, or
a local mock of it — swapped by URL). Sync (httpx.Client) — LangChain runs
sync StructuredTool funcs in a threadpool under ainvoke.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-03-26"


class MCPError(Exception):
    """An MCP transport or tool-level error."""


class MCPClient:
    def __init__(self, endpoint: str, *, auth_token: Optional[str] = None, timeout: float = 30.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.endpoint = endpoint
        self.auth_token = auth_token
        self.timeout = timeout
        self._transport = transport
        self._session_id: Optional[str] = None
        self._next_id = 0

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        if self.auth_token:
            h["Authorization"] = f"Bearer {self.auth_token}"
        return h

    async def _apost(self, body: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            return await client.post(self.endpoint, json=body, headers=self._headers())

    def _post(self, body: dict) -> httpx.Response:
        # Safe whether called from a plain sync context (LangChain runs sync
        # StructuredTool funcs in a worker thread) or from within a running
        # event loop (e.g. an async endpoint resolving merchant context):
        # in the latter case asyncio.run() would raise, so offload to a thread.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._apost(body))
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, self._apost(body)).result()

    def _rpc(self, method: str, params: Optional[dict] = None, *, capture_session: bool = False) -> dict:
        self._next_id += 1
        body: dict = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params
        try:
            resp = self._post(body)
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP transport error: {exc}") from exc
        if capture_session:
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
        try:
            data = resp.json()
        except Exception as exc:
            raise MCPError(f"MCP non-JSON response ({resp.status_code})") from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise MCPError(f"{err.get('code')}: {err.get('message')}")
        return (data or {}).get("result", {}) if isinstance(data, dict) else {}

    def _notify(self, method: str) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method})
        except Exception:  # notifications are best-effort
            pass

    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
             "clientInfo": {"name": "waj-ai-engine", "version": "1.0.0"}},
            capture_session=True,
        )
        self._notify("notifications/initialized")
        return result

    def call_tool(self, name: str, arguments: dict) -> Any:
        if self._session_id is None:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            content = result.get("content") or [{}]
            raise MCPError(content[0].get("text", "MCP tool error"))
        content = result.get("content") or []
        if content and content[0].get("type") == "text":
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except Exception:
                return text
        return result

    def list_tools(self) -> list:
        """MCP tools/list -> the raw tools array [{name, description, inputSchema}]."""
        if self._session_id is None:
            self.initialize()
        result = self._rpc("tools/list")
        return list(result.get("tools") or [])


# One client per (endpoint, auth_token): keeps the initialized MCP session so
# each tools/call is one round-trip, not an initialize handshake + call.
# Benign under threadpool concurrency: a double-initialize just re-captures
# the session id.
_POOL: dict = {}
_TOOLS_CACHE: dict = {}  # endpoint -> (monotonic_ts, tools list)


def get_pooled_client(endpoint: str, auth_token: Optional[str] = None, *,
                      transport: Optional[httpx.AsyncBaseTransport] = None) -> MCPClient:
    key = (endpoint, auth_token)
    client = _POOL.get(key)
    if client is None:
        client = MCPClient(endpoint, auth_token=auth_token, transport=transport)
        _POOL[key] = client
    return client


def list_tools_cached(endpoint: str, auth_token: Optional[str] = None, *,
                      ttl: float = 300.0,
                      transport: Optional[httpx.AsyncBaseTransport] = None) -> list:
    """TTL-cached tools/list, keyed by (endpoint, auth_token). Prod won't hit
    this on the request path once Phase 1a freezes tools/list into the profile
    snapshot; dev/staging use the cache so discovery isn't a per-request
    round-trip. Keying by auth_token (like the pool) keeps one tenant's cached
    tool list from being served to another tenant sharing the endpoint."""
    key = (endpoint, auth_token)
    cached = _TOOLS_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < ttl:
        return cached[1]
    tools = get_pooled_client(endpoint, auth_token, transport=transport).list_tools()
    _TOOLS_CACHE[key] = (now, tools)
    return tools


def last_listed_tools(endpoint: str, auth_token: Optional[str] = None) -> Optional[list]:
    """The tools this process last saw ``endpoint`` list (Test connection,
    an agent's turn, a portal read), however old; None if it never listed.
    Never calls out — for screens that count a server's tools."""
    cached = _TOOLS_CACHE.get((endpoint, auth_token))
    return cached[1] if cached is not None else None


def clear_mcp_pool() -> None:
    """Drop pooled sessions + cached tool lists (tests / admin reconnect)."""
    _POOL.clear()
    _TOOLS_CACHE.clear()
