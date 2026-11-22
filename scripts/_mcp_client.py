"""Shared JSON-RPC client helpers for the admin MCP server (`/mcp`).

Extracted from `scripts/mcp_smoke.py` (`_rpc` / `_call_tool`) so other
one-off MCP-driven scripts (e.g. `create_business_optimization_suite.py`)
don't copy-paste the same streamable-HTTP JSON-RPC envelope. `mcp_smoke.py`
itself is left as-is (it's a standalone smoke test guarded by `__main__`);
this module is for NEW scripts to import.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx

ACCEPT = {"Accept": "application/json, text/event-stream",
          "Content-Type": "application/json"}


def _rpc(endpoint: str, method: str, params: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None, rid: int = 1) -> httpx.Response:
    """POST one JSON-RPC 2.0 request at the MCP endpoint."""
    h = dict(ACCEPT)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return httpx.post(endpoint, headers=h, timeout=30,
                      json={"jsonrpc": "2.0", "id": rid, "method": method,
                            "params": params or {}})


def _call_tool(endpoint: str, name: str, args: Dict[str, Any],
               token: Optional[str]) -> Any:
    """Call an MCP tool by name and unwrap its result.

    FastMCP serializes the tool's return value into the `content` text
    block (JSON-encoded) for normal returns; a bare dict result sometimes
    only appears nested under `structuredContent: {"result": ...}` — unwrap
    that shape if we fall back to it. Transport/protocol errors are
    returned as plain dicts rather than raised so callers can print/inspect
    them uniformly.
    """
    r = _rpc(endpoint, "tools/call", {"name": name, "arguments": args}, token=token)
    if r.status_code != 200:
        return {"_http": r.status_code, "_body": r.text[:200]}
    body = r.json()
    if "error" in body:
        return {"_rpc_error": body["error"]}
    res = body.get("result", {})
    for c in res.get("content", []):
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:
                return c["text"]
    sc = res.get("structuredContent")
    if isinstance(sc, dict):
        return sc["result"] if list(sc.keys()) == ["result"] else sc
    return res
