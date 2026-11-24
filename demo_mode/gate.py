"""The demo-mode middleware: refuse locked routes, apply the visitor caps.

A plain ASGI middleware rather than ``@app.middleware("http")``, because the
latter never sees a WebSocket handshake, and voice calls are refused too.
With ``DEMO_MODE`` off it passes every request through untouched (read per
request, so a test can switch it).
"""
from __future__ import annotations

import asyncio
import html
import json
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import parse_qsl

from demo_mode import limits, policy, protect, settings

ADMIN_COOKIE = "waj_admin_token"


def _headers(scope: Dict[str, Any]) -> Dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}


def _cookie(headers: Dict[str, str], name: str) -> Optional[str]:
    for part in (headers.get("cookie") or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def visitor(scope: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """(network address, session key) of the caller."""
    headers = _headers(scope)
    client = scope.get("client") or ("unknown", 0)
    ip = str(client[0] if client else "unknown")
    header = settings.client_ip_header()
    if header and (headers.get(header) or "").strip():
        # Behind a CDN that names the visitor in its own header.
        ip = headers[header].split(",")[0].strip()[:64]
    token = None
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    token = token or _cookie(headers, ADMIN_COOKIE)
    return ip, limits.session_key(token)


async def _send(send: Callable, status: int, body: bytes, content_type: str,
                extra: Optional[Dict[str, str]] = None) -> None:
    headers = [(b"content-type", content_type.encode()), (b"cache-control", b"no-store")]
    for k, v in (extra or {}).items():
        headers.append((k.lower().encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _page(message: str) -> bytes:
    text = html.escape(message)
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Public demo</title>"
        "<body style='font-family:system-ui,sans-serif;max-width:36rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.3rem'>The public demo</h1><p>{text}</p>"
        f"<p>To use every screen, run the engine yourself: "
        f"<a href='{html.escape(settings.repo_url())}'>get it on GitHub</a>.</p>"
        "<p><a href='/admin/live'>Back to the portal</a></p></body></html>"
    ).encode("utf-8")


def _sse_answer(message: str) -> bytes:
    events = [{"type": "answer", "content": message}, {"type": "done"}]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


def _wants_html(scope: Dict[str, Any], headers: Dict[str, str]) -> bool:
    return (str(scope.get("path", "")).startswith("/admin")
            and "text/html" in (headers.get("accept") or ""))


async def _refuse(scope: Dict[str, Any], headers: Dict[str, str], send: Callable, status: int,
                  message: str, extra: Optional[Dict[str, str]] = None) -> None:
    if _wants_html(scope, headers):
        return await _send(send, status, _page(message), "text/html; charset=utf-8", extra)
    return await _send(send, status, json.dumps({"detail": message}).encode(), "application/json", extra)


class DemoGate:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001 — ASGI
        kind = scope.get("type")
        if kind not in ("http", "websocket") or not settings.enabled():
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        query = dict(parse_qsl((scope.get("query_string") or b"").decode("latin-1")))
        method = scope.get("method", "GET") if kind == "http" else "GET"
        reason = policy.refusal(method, path, query)

        if kind == "websocket":
            if reason:
                await send({"type": "websocket.close", "code": 1008})
                return
            return await self.app(scope, receive, send)

        headers = _headers(scope)
        if reason is None and method not in ("GET", "HEAD") and protect.path_target(path):
            # The Builder assistant is read-only (demo_mode/protect.py). The
            # id lookup is cached, but a stale cache reads the database.
            reason = await asyncio.to_thread(protect.refusal_for_path, method, path)
        if reason:
            return await _refuse(scope, headers, send, 403, reason)

        bucket = policy.limited_bucket(method, path)
        if bucket:
            if bucket == "uploads":
                try:
                    size = int(headers.get("content-length") or 0)
                except ValueError:
                    size = 0
                # The whole form, a little over the file itself for its fields.
                if size > settings.max_upload_bytes() + 64 * 1024:
                    mb = settings.max_upload_bytes() / (1024 * 1024)
                    return await _refuse(scope, headers, send, 413,
                                         f"Files in the public demo can be at most {mb:g} MB.")
            ip, session = visitor(scope)
            if not limits.hit(bucket, ip=ip, session=session):
                if bucket in policy.STREAM_BUCKETS:
                    return await _send(send, 200, _sse_answer(limits.RATE_MESSAGE), "text/event-stream")
                return await _refuse(scope, headers, send, 429, limits.RATE_MESSAGE, {"Retry-After": "300"})
            # Streamed buckets take their turn from the daily cap in the route
            # itself (and answer the cap message there).
            if (bucket in policy.MODEL_BUCKETS and bucket not in policy.STREAM_BUCKETS
                    and limits.turns_left() <= 0):
                return await _refuse(scope, headers, send, 429, limits.CAP_MESSAGE)
        return await self.app(scope, receive, send)
