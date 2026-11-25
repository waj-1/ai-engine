"""Northwind Internet's tool server: a fictional MCP server inside the engine.

The demo's Support desk agent has tools (check an outage, look up an order,
open a ticket, hand over to a person, list the plans). They come from an ordinary MCP server row
(Connections), whose address is this engine's own ``/demo/northwind/mcp``.

Two ways in, one implementation (`handle`):

* Over HTTP at ``POST /demo/northwind/mcp`` (demo mode only), so a visitor can
  point any MCP client at it, and the portal's Connect server dialog can
  discover it.
* In process, for the engine's own calls: `runtime.tools._mcp_adapter` gives
  the endpoint `northwind_endpoint()` a client whose transport is this ASGI
  app (`httpx.ASGITransport`), so a tool call never leaves the process. That
  is why the SSRF guard did not have to be loosened: it treats exactly that
  one address as a private one (no secret is ever sent to it) and every other
  local or internal address stays blocked. A server row pointing anywhere
  else, even at another path on this host, goes through the normal guard.

The server is stateless and answers JSON (MCP streamable HTTP with JSON
responses): ``initialize``, ``tools/list``, ``tools/call``, ``ping`` and
notifications. Its data is fixed fiction.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

PATH = "/demo/northwind/mcp"
SERVER_NAME = "Northwind tools"
_PROTOCOL_VERSION = "2025-03-26"

_OUTAGE_AREAS = {"riverside", "old town"}
_ORDERS = {
    "NW-1042": "Router shipped yesterday, arrives tomorrow.",
    "NW-1077": "Plan upgrade to Fiber 500 is active.",
}


def check_outage(area: str) -> str:
    if (area or "").lower().strip() in _OUTAGE_AREAS:
        return f"Yes: planned maintenance in {area} until 14:00 today. Service returns automatically."
    return f"No known outage in {area}. Suggest restarting the router."


_ORDER_FORMAT = re.compile(r"^NW-\d{4}$")


class ToolFailure(Exception):
    """The tool could not do what it was asked: answered as an MCP error
    result (``isError``), which the engine records as a failed action."""


def get_order_status(order_id: str) -> str:
    key = (order_id or "").upper().strip()
    if not _ORDER_FORMAT.match(key):
        # A real failure of the call, not an empty answer: the order system
        # rejects anything that is not an order number.
        raise ToolFailure(f"Invalid order number '{order_id}': order numbers are NW- and four digits, "
                          "for example NW-1042.")
    return _ORDERS.get(key, f"No order {order_id} found.")


def create_ticket(summary: str, customer_email: str = "") -> str:
    return "Ticket T-2318 opened. A technician will reply within 4 working hours."


def escalate_to_human(reason: str) -> str:
    return ("Handed to the support team. A person will take over this chat within 10 minutes "
            "during support hours (8am to 8pm, every day).")


def list_plans() -> str:
    return "Fiber 100: $29/month. Fiber 500: $45/month. Fiber 1000: $65/month. All plans have no contract."


def _schema(props: Dict[str, str], required: List[str]) -> Dict[str, Any]:
    return {"type": "object",
            "properties": {k: {"type": "string", "description": d} for k, d in props.items()},
            "required": required}


TOOLS: List[Dict[str, Any]] = [
    {"name": "check_outage",
     "description": "Check whether there is a known internet outage in a customer's area (city or district name).",
     "inputSchema": _schema({"area": "The city or district."}, ["area"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "get_order_status",
     "description": "Look up a Northwind order (router, plan upgrade) by its order number, e.g. NW-1042.",
     "inputSchema": _schema({"order_id": "The order number, e.g. NW-1042."}, ["order_id"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "create_ticket",
     "description": "Open a support ticket for a technician to follow up.",
     "inputSchema": _schema({"summary": "What the problem is, in one or two sentences.",
                             "customer_email": "The customer's email, if they gave one."}, ["summary"])},
    {"name": "escalate_to_human",
     "description": "Hand the conversation to a person on Northwind's support team. Use it when the "
                    "customer asks for a person, or is still stuck or upset after you tried to help.",
     "inputSchema": _schema({"reason": "Why a person is needed, in one sentence."}, ["reason"])},
    {"name": "list_plans",
     "description": "List Northwind's internet plans and monthly prices.",
     "inputSchema": _schema({}, [])},
]

_IMPL: Dict[str, Callable[..., str]] = {
    "check_outage": check_outage,
    "get_order_status": get_order_status,
    "create_ticket": create_ticket,
    "escalate_to_human": escalate_to_human,
    "list_plans": list_plans,
}
TOOL_NAMES = tuple(_IMPL)


def northwind_endpoint() -> str:
    """The address the demo's server row stores (and visitors can use)."""
    from demo_mode import settings

    return settings.public_url() + PATH


def handles(endpoint: str) -> bool:
    """True for exactly the demo's own Northwind address, in demo mode."""
    from demo_mode import settings

    if not settings.enabled() or not endpoint:
        return False
    try:
        got, want = urlsplit(endpoint.strip()), urlsplit(northwind_endpoint())
    except ValueError:
        return False
    return (got.scheme, got.netloc.lower(), got.path.rstrip("/")) == \
        (want.scheme, want.netloc.lower(), want.path.rstrip("/"))


def _call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    fn = _IMPL.get(name)
    if fn is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    spec = next(t for t in TOOLS if t["name"] == name)
    allowed = set(spec["inputSchema"]["properties"])
    args = {k: str(v) for k, v in (arguments or {}).items() if k in allowed and v is not None}
    missing = [k for k in spec["inputSchema"]["required"] if not args.get(k)]
    if missing:
        return {"content": [{"type": "text", "text": f"Missing: {', '.join(missing)}"}], "isError": True}
    try:
        text = fn(**args)
    except ToolFailure as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One JSON-RPC message in, its response out (None for a notification)."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notifications/initialized and friends
    params = message.get("params") or {}
    if method == "initialize":
        result: Dict[str, Any] = {
            "protocolVersion": params.get("protocolVersion") or _PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "northwind", "version": "1.0.0"},
            "instructions": "Fictional tools of Northwind Internet, for the engine's public demo.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = _call(str(params.get("name") or ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


async def _serve(scope, receive, send):  # noqa: ANN001 — ASGI
    """The HTTP face of `handle`. 404 outside demo mode."""
    from demo_mode import settings

    async def reply(status: int, body: Any, headers: Optional[List] = None) -> None:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        hdrs = [(b"content-type", b"application/json")] + (headers or [])
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": raw})

    if scope.get("type") != "http":
        return
    if not settings.enabled():
        return await reply(404, {"detail": "Not Found"})
    if scope.get("method") != "POST":
        return await reply(405, {"detail": "POST JSON-RPC messages to this address."},
                           [(b"allow", b"POST")])
    body = b""
    while True:
        event = await receive()
        body += event.get("body", b"")
        if len(body) > 64 * 1024:
            return await reply(413, {"detail": "Message too large."})
        if not event.get("more_body"):
            break
    try:
        message = json.loads(body or b"null")
    except ValueError:
        return await reply(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "Parse error"}})
    if isinstance(message, list):
        out = [r for r in (handle(m) for m in message) if r is not None]
        return await reply(200 if out else 202, out or None)
    response = handle(message)
    if response is None:
        return await reply(202, None)
    extra = []
    if message.get("method") == "initialize":
        extra.append((b"mcp-session-id", uuid.uuid4().hex.encode()))
    return await reply(200, response, extra)


class _App:
    """`_serve` as an ASGI app object (Starlette mounts a plain function as a
    request handler, not as an ASGI app)."""

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        await _serve(scope, receive, send)


app = _App()


def transport():
    """An httpx transport that delivers requests to `app` in this process."""
    import httpx

    return httpx.ASGITransport(app=app)
