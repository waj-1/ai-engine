"""Per-tool detail screen for the admin portal.

Tools themselves are Python code synced into ``agent_tools`` at boot — the
Python source is the source-of-truth and is never editable from the admin
portal (security boundary). What admins *can* edit is the row-level
metadata that the rest of the platform reads:

* ``display_name``, ``description``, ``category``
* ``scope``  (``read`` / ``write``)
* ``is_deprecated``
* ``additional_agent_kinds``  (extra agent_kinds beyond the primary
  ``agent_kind`` that may use this tool)

Read-only on this screen:
* ``python_name``, ``source_file``, ``agent_kind``, ``argument_schema``
* The Python source itself, loaded from disk and shown in a ``<pre>``
  block. The loader is path-traversal-safe — only files inside
  ``ai_agent/`` resolve.

Routes:
    GET  /admin/tools/{tool_id}   → tool_detail.html
    POST /admin/tools/{tool_id}   → updates metadata, redirects back

We import the existing router from ``admin.router`` and attach our
endpoints to it; ``router.py`` does ``from admin import tool_handlers``
at module bottom so the registrations land before FastAPI scans routes.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import router, templates, _safe_list, current_env
from core.agent_profile import AgentProfileLoader
from core.auth import get_service_client, admin_service_client
from core.deployment import active_scope

logger = logging.getLogger(__name__)

# Resolve to the absolute ai_agent/ directory once at import time. All
# source-file lookups are constrained to descendants of this directory —
# any path that resolves outside is rejected before we touch disk.
_AI_AGENT_ROOT = Path(__file__).resolve().parent.parent

# Editable scope values surfaced in the form. Keep in sync with the DB
# enum / check constraint in ``agent_tools``.
_SCOPE_OPTIONS = ("read", "write")

# Agent kinds an HTTP tool can be primary-attached to. Mirrors the
# `agent_kind` enum + the values the seeders use. Kept here rather than
# queried from Postgres because enum introspection over PostgREST is
# clunky and the list rarely changes.
# The curated base now lives in admin/agent_kinds.py (one list for every
# Build screen); the WAJ branch below appends every live agent kind to it.
from admin.agent_kinds import BASE_AGENT_KINDS as _AGENT_KINDS_FOR_TOOLS  # noqa: E402


def _tool_agent_kinds() -> tuple:
    """Agent kinds an HTTP tool can be attached to. Client-only (e.g.
    ``("tenant",)``) on any scoped build; otherwise the local WAJ list
    (unchanged behavior)."""
    if (client := active_scope()):
        return (client.agent_kind, *client.extra_kinds)
    from admin.agent_kinds import live_agent_kinds
    return live_agent_kinds()

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

_DEFAULT_HTTP_TOOL_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User's search query"},
        },
        "required": ["query"],
    },
    indent=2,
)


def _read_source_file(source_file: Optional[str]) -> Dict[str, Any]:
    """Return ``{path, content, error}`` for the tool's source file.

    Guards against path traversal: the column stores a relative path like
    ``runtime/tools/business.py``, but a malicious upsert (or a bad sync)
    could land an absolute path or one with ``..`` segments. We resolve
    against ``ai_agent/`` and reject anything that escapes.
    """
    if not source_file:
        return {"path": None, "content": None, "error": "No source_file recorded."}
    rel = str(source_file).strip().lstrip("/")
    try:
        candidate = (_AI_AGENT_ROOT / rel).resolve()
    except (OSError, RuntimeError) as exc:
        return {"path": source_file, "content": None,
                "error": f"Could not resolve path: {exc}"}
    try:
        candidate.relative_to(_AI_AGENT_ROOT)
    except ValueError:
        return {"path": source_file, "content": None,
                "error": "Refusing to read file outside the repo root."}
    if not candidate.exists():
        return {"path": str(candidate.relative_to(_AI_AGENT_ROOT)),
                "content": None,
                "error": f"File not found on disk: {source_file}"}
    if not candidate.is_file():
        return {"path": str(candidate.relative_to(_AI_AGENT_ROOT)),
                "content": None,
                "error": "Path is not a regular file."}
    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"path": str(candidate.relative_to(_AI_AGENT_ROOT)),
                "content": None,
                "error": f"Could not read file: {exc}"}
    return {
        "path": str(candidate.relative_to(_AI_AGENT_ROOT)),
        "content": content,
        "error": None,
    }


def _parse_additional_kinds(raw: Optional[str]) -> List[str]:
    """Split the textarea body on newlines/commas, lower-snake, dedupe."""
    if not raw:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for piece in raw.replace(",", "\n").splitlines():
        kind = piece.strip().lower()
        if not kind or kind in seen:
            continue
        seen.add(kind)
        out.append(kind)
    return out


# ---------------------------------------------------------------------------
# HTTP tool builder — operator can create new agent tools from the portal
# by configuring an HTTP endpoint + JSON schema, no Python required.
# These rows are kind='http' and source_file='__portal__'; sync_tools.py
# leaves them alone (it only walks Python factories).
#
# IMPORTANT: these routes are declared BEFORE the catch-all
# `/tools/{tool_id}` so FastAPI's first-match routing doesn't send
# `/tools/new` into the detail handler (which would 404 looking for a
# tool with id="new").
# ---------------------------------------------------------------------------


def _validate_argument_schema(raw: str) -> Dict[str, Any]:
    """Parse + sanity-check the JSON-schema textarea. Returns the dict on
    success; raises HTTPException(400) with a clear message otherwise."""
    if not raw or not raw.strip():
        raise HTTPException(400, "argument_schema is required")
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"argument_schema is not valid JSON: {exc}")
    if not isinstance(schema, dict):
        raise HTTPException(400, "argument_schema must be a JSON object")
    if schema.get("type") and schema["type"] != "object":
        raise HTTPException(400, "argument_schema root type must be 'object'")
    return schema


_PYTHON_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


def parse_http_tool_form(form, *, agent_kinds) -> Dict[str, Any]:
    """Validate an HTTP-tool creation form and return the ``agent_tools`` insert
    payload (``kind='http'``, ``source_file='__portal__'``). Raises
    ``ValueError(message)`` on the first validation failure so callers can
    re-render their form with the message. Used by the in-MCP add-tool flow
    (``POST /admin/agent-mcp-servers/{id}/add-tool``).
    """
    python_name = (form.get("python_name") or "").strip().lower()
    if not _PYTHON_NAME_RE.match(python_name):
        raise ValueError("python_name must be lowercase + underscores, 2–63 chars, starting with a letter")

    description = (form.get("description") or "").strip()
    if not description:
        raise ValueError("description is required — this is what the LLM sees")

    endpoint_url = (form.get("endpoint_url") or "").strip()
    if not endpoint_url or not endpoint_url.lower().startswith(("http://", "https://")):
        raise ValueError("endpoint_url must be an absolute http(s) URL")

    http_method = (form.get("http_method") or "POST").strip().upper()
    if http_method not in _HTTP_METHODS:
        raise ValueError(f"http_method must be one of {_HTTP_METHODS}")

    scope = (form.get("scope") or "read").strip().lower()
    if scope not in _SCOPE_OPTIONS:
        raise ValueError(f"scope must be one of {_SCOPE_OPTIONS}")

    agent_kind = (form.get("agent_kind") or "").strip()
    if agent_kind not in agent_kinds:
        raise ValueError(f"agent_kind must be one of {list(agent_kinds)}")

    try:
        schema = _validate_argument_schema(form.get("argument_schema") or "")
    except HTTPException as exc:
        raise ValueError(str(exc.detail))

    try:
        timeout_seconds = int(form.get("timeout_seconds") or 30)
        if not (1 <= timeout_seconds <= 300):
            raise ValueError("out of range")
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be an integer between 1 and 300")

    return {
        "python_name": python_name,
        "display_name": (form.get("display_name") or "").strip() or python_name,
        "description": description,
        "category": (form.get("category") or "http").strip(),
        "scope": scope,
        "agent_kind": agent_kind,
        "additional_agent_kinds": _parse_additional_kinds(form.get("additional_agent_kinds")),
        "kind": "http",
        "endpoint_url": endpoint_url,
        "http_method": http_method,
        "auth_env_var": (form.get("auth_env_var") or "").strip() or None,
        "timeout_seconds": timeout_seconds,
        "argument_schema": schema,
        "source_file": "__portal__",
        "is_deprecated": False,
        "usage_guidance": (form.get("usage_guidance") or "").strip() or None,
    }


@router.get("/tools/new")
async def tool_new_form(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Retired: standalone HTTP-tool creation is gone — author HTTP tools inside
    an internal MCP (open an internal MCP from /admin/tools and use
    '＋ Add HTTP tool'). Kept as a redirect so old bookmarks land on the screen."""
    return RedirectResponse(url="/admin/tools", status_code=307)


@router.post("/tools/new")
async def tool_new_submit(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Retired: see tool_new_form. A stray POST (old bookmark) redirects to the
    MCP & Tools screen instead of creating a standalone, un-encapsulated tool."""
    return RedirectResponse(url="/admin/tools", status_code=303)


# ---------------------------------------------------------------------------
# Dry-run endpoint — exercise an HTTP-tool config against its endpoint with
# sample kwargs WITHOUT persisting anything. Lets operators iterate on URL +
# schema + auth in the same form they're filling out, instead of having to
# save, attach to a profile, and run an eval just to see the response.
#
# Always returns HTTP 200; the body carries `ok: bool` to distinguish "the
# dry-run plumbing worked, here's what the endpoint said" from "we never
# got far enough to call it". This matches the way the agent itself treats
# tool failures — the LLM gets a string it can read, not a stack trace.
# ---------------------------------------------------------------------------


_PRIVATE_CIDRS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + AWS/GCP metadata
)


def _is_private_endpoint(url: str) -> bool:
    """Best-effort: True if the URL's host resolves to a private/link-local
    IP literal or is one of the well-known internal hostnames. We don't
    DNS-resolve here — operator-supplied input is already gated on
    superuser, this is advisory logging only."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_CIDRS)


def _sample_args_request_echo(
    *, url: str, method: str, auth_env_var: Optional[str], headers_sent: Dict[str, str],
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the redacted request echo we return to the page. We never echo
    the raw Authorization value — substitute a marker that names the env var
    so the operator can confirm WHICH secret was used without leaking it."""
    redacted = dict(headers_sent)
    if "Authorization" in redacted:
        # Surface the env-var name (operator already knows it from the form),
        # never the token itself.
        env_label = f", env_var={auth_env_var}" if auth_env_var else ""
        redacted["Authorization"] = f"Bearer <redacted{env_label}>"
    return {
        "url": url,
        "method": method,
        "headers": redacted,
        "body": args,
    }


def _truncate_body(text: str, *, limit: int = 4096) -> Dict[str, Any]:
    """Cap response body at `limit` bytes so a huge upstream payload doesn't
    blow up the page. Returns the body + a `truncated` flag + original size."""
    raw = text or ""
    size = len(raw.encode("utf-8"))
    if size <= limit:
        return {"body": raw, "size_bytes": size, "truncated": False}
    # Slice on chars not bytes — close enough for human reading, and avoids
    # leaving a half-encoded UTF-8 sequence at the end.
    return {"body": raw[:limit] + "\n…(truncated)", "size_bytes": size, "truncated": True}


def run_tool_dry_run(payload: Dict[str, Any], *,
                      actor_user_id: Optional[str] = None) -> Dict[str, Any]:
    """Body of the stateless HTTP-tool dry-run — see the route docstring
    below. Extracted to a plain sync function so both the portal route
    (``await request.json()`` → this) and the admin-MCP ``tools`` action
    (``dry_run``) share one implementation. Always returns a result dict
    shaped ``{"ok": bool, ...}``; never raises for input-shaped problems —
    those come back as ``ok: False`` with an ``error_class``.

    ``actor_user_id`` is only used for the blocked-endpoint warning log;
    callers without an authenticated actor can omit it.
    """
    # Local import: keeps the admin module importable in environments
    # (smoke tests, type-checks) that don't have the runtime tools wired.
    from runtime.tools._http_adapter import build_http_tool, _classify_endpoint
    from pydantic import ValidationError

    if not isinstance(payload, dict):
        return {"ok": False, "error": "request body must be a JSON object",
                "error_class": "ValidationError", "request": None, "latency_ms": 0}

    endpoint_url = (payload.get("endpoint_url") or "").strip()
    http_method = (payload.get("http_method") or "POST").strip().upper()
    auth_env_var = (payload.get("auth_env_var") or "").strip() or None
    try:
        timeout_seconds = int(payload.get("timeout_seconds") or 30)
    except (TypeError, ValueError):
        timeout_seconds = 30
    schema = payload.get("argument_schema") or {}
    args = payload.get("args") or {}

    if not endpoint_url or not endpoint_url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "endpoint_url must be an absolute http(s) URL",
                "error_class": "ConfigError", "request": None, "latency_ms": 0}
    if http_method not in _HTTP_METHODS:
        return {"ok": False, "error": f"http_method must be one of {_HTTP_METHODS}",
                "error_class": "ConfigError", "request": None, "latency_ms": 0}
    if not isinstance(args, dict):
        return {"ok": False, "error": "args must be a JSON object",
                "error_class": "ValidationError", "request": None, "latency_ms": 0}

    # Block loopback / link-local / cloud-metadata targets BEFORE building or
    # calling the tool — consistent with the runtime adapter
    # (runtime.tools._http_adapter._call), which refuses these hosts. RFC1918
    # ("private") is intentionally allowed (the adapter just withholds the
    # auth token), matching runtime behavior.
    if _classify_endpoint(endpoint_url) == "blocked":
        logger.warning(
            "tool dry-run blocked: %s resolves to a loopback/link-local/metadata host (actor=%s)",
            endpoint_url, actor_user_id,
        )
        return {"ok": False,
                "error": "endpoint resolves to a loopback, link-local, or cloud-metadata "
                         "address and was blocked",
                "error_class": "ConfigError", "request": None, "latency_ms": 0}

    # Build the tool the same way the agent will — catches bad JSON schemas
    # the operator may not have noticed.
    pseudo_row = {
        "python_name": "dry_run_tool",
        "description": "dry-run",
        "endpoint_url": endpoint_url,
        "http_method": http_method,
        "auth_env_var": auth_env_var,
        "timeout_seconds": timeout_seconds,
        "argument_schema": schema,
    }
    try:
        tool = build_http_tool(pseudo_row)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_class": "ValidationError",
                "request": None, "latency_ms": 0}

    # Validate kwargs against the Pydantic model BEFORE we call the URL.
    # `StructuredTool.args_schema` is the pydantic model the LLM is bound to.
    args_model = tool.args_schema
    try:
        validated = args_model(**args)  # type: ignore[misc]
        call_kwargs = validated.dict() if hasattr(validated, "dict") else dict(args)
    except ValidationError as exc:
        # Build a compact field/error list for the page rather than dumping
        # the whole pydantic error blob.
        problems = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []))
            problems.append(f"{loc}: {err.get('msg')}")
        return {"ok": False,
                "error": "argument validation failed: " + "; ".join(problems),
                "error_class": "ValidationError",
                "request": None,
                "latency_ms": 0}
    except Exception as exc:
        return {"ok": False, "error": f"argument validation failed: {exc}",
                "error_class": "ValidationError", "request": None, "latency_ms": 0}

    # Construct the same headers `_http_adapter._call` would, so the echoed
    # request matches reality. We can't easily intercept the adapter's own
    # httpx call to capture headers, so we reproduce the auth-attachment
    # logic here — keep this in sync with `_http_adapter._call`.
    import os as _os
    headers_sent: Dict[str, str] = {}
    if auth_env_var and _os.getenv(auth_env_var):
        headers_sent["Authorization"] = f"Bearer {_os.getenv(auth_env_var)}"

    request_echo = _sample_args_request_echo(
        url=endpoint_url, method=http_method, auth_env_var=auth_env_var,
        headers_sent=headers_sent, args=call_kwargs,
    )

    started = time.monotonic()
    try:
        result_str = tool.func(**call_kwargs)
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "error_class": "TransportError",
                "request": request_echo, "latency_ms": latency_ms}
    latency_ms = int((time.monotonic() - started) * 1000)

    # `_http_adapter._call` returns a string. It already encoded transport
    # errors as `"HTTP request failed: ..."` — surface those as
    # TransportError so the frontend can colour them red rather than green.
    result_text = result_str if isinstance(result_str, str) else str(result_str)
    if result_text.startswith("HTTP request failed:"):
        return {"ok": False, "error": result_text,
                "error_class": "TransportError",
                "request": request_echo, "latency_ms": latency_ms}

    # Try to recover the underlying status code from the adapter's wording
    # ("HTTP 4xx error: ..."). Falls back to 200 if the call succeeded.
    http_status: Optional[int] = 200
    m = re.match(r"^HTTP (\d{3}) error:\s*(.*)$", result_text, flags=re.DOTALL)
    if m:
        http_status = int(m.group(1))
        result_text = m.group(2)

    # Heuristic content-type detection on the echoed body. The adapter has
    # already json-encoded objects via `_stringify_response`, so anything
    # that round-trips through json.loads is JSON.
    content_type = "text/plain"
    try:
        json.loads(result_text)
        content_type = "application/json"
    except Exception:
        pass

    body_info = _truncate_body(result_text)
    response_block: Dict[str, Any] = {
        "body": body_info["body"],
        "content_type": content_type,
        "size_bytes": body_info["size_bytes"],
    }
    if body_info["truncated"]:
        response_block["truncated"] = True

    return {
        "ok": True,
        "http_status": http_status,
        "latency_ms": max(latency_ms, 1),  # always > 0 so the UI shows a number
        "request": request_echo,
        "response": response_block,
    }


@router.post("/tools/dry-run")
async def tool_dry_run(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Stateless dry-run for an HTTP tool — see the file docstring section
    above. Body shape and response shape are documented in the spec; this
    handler is intentionally chatty in its error categorisation so the
    frontend can colour-code the result without having to parse messages.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"request body is not valid JSON: {exc}",
             "error_class": "ValidationError", "request": None, "latency_ms": 0},
            status_code=200,
        )
    result = run_tool_dry_run(payload, actor_user_id=ctx.get("user_id"))
    return JSONResponse(result, status_code=200)


@router.post("/tools/{tool_id}/edit")
async def tool_http_edit(
    tool_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Update HTTP-specific config (URL/method/schema/auth/timeout).
    Refuses Python tools — their behavior lives in source and the row is
    metadata-only."""
    form = await request.form()
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_tools").select("*").eq("tool_id", tool_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "tool not found")
    before = rows[0]
    if before.get("kind") != "http":
        raise HTTPException(400, "only HTTP tools are editable here")

    schema = _validate_argument_schema(form.get("argument_schema") or "")
    endpoint_url = (form.get("endpoint_url") or "").strip()
    if not endpoint_url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "endpoint_url must be an absolute http(s) URL")
    http_method = (form.get("http_method") or "POST").strip().upper()
    if http_method not in _HTTP_METHODS:
        raise HTTPException(400, f"http_method must be one of {_HTTP_METHODS}")
    try:
        timeout_seconds = int(form.get("timeout_seconds") or 30)
        if not (1 <= timeout_seconds <= 300):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(400, "timeout_seconds must be an integer between 1 and 300")

    payload = {
        "endpoint_url": endpoint_url,
        "http_method": http_method,
        "auth_env_var": (form.get("auth_env_var") or "").strip() or None,
        "timeout_seconds": timeout_seconds,
        "argument_schema": schema,
    }
    client.table("agent_tools").update(payload).eq("tool_id", tool_id).execute()
    record_change(
        entity_type="agent_tools",
        entity_id=tool_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before=before,
        after={**before, **payload},
    )
    AgentProfileLoader.invalidate()
    return RedirectResponse(url=f"/admin/tools/{tool_id}", status_code=303)


@router.post("/tools/{tool_id}/delete")
async def tool_http_delete(
    tool_id: str,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Soft-delete an HTTP tool (flips is_deprecated). Python tools are
    managed by sync_tools.py — remove the @tool decorator in source and
    redeploy to drop one of those."""
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_tools").select("*").eq("tool_id", tool_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "tool not found")
    before = rows[0]
    if before.get("kind") != "http":
        raise HTTPException(400, "only HTTP tools can be deleted from the portal")
    client.table("agent_tools").update({"is_deprecated": True}).eq("tool_id", tool_id).execute()
    record_change(
        entity_type="agent_tools",
        entity_id=tool_id,
        action="deprecate",
        actor_user_id=ctx["user_id"],
        before=before,
        after={**before, "is_deprecated": True},
    )
    AgentProfileLoader.invalidate()
    return RedirectResponse(url="/admin/tools", status_code=303)


# ---------------------------------------------------------------------------
# Detail screen + metadata save. Declared LAST so the catch-all
# `/tools/{tool_id}` doesn't shadow `/tools/new` and friends.
# ---------------------------------------------------------------------------


@router.get("/tools/{tool_id}", response_class=HTMLResponse)
async def tool_detail(
    tool_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_tools").select("*").eq("tool_id", tool_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "tool not found")
    tool = rows[0]
    source = _read_source_file(tool.get("source_file"))
    # The server(s) that expose this tool — for the back link and Details.
    servers: List[Dict[str, Any]] = []
    try:
        member_rows = _safe_list(
            client.table("agent_mcp_tools").select("mcp_server_id").eq("tool_id", tool_id).execute()
        )
        ids = [m.get("mcp_server_id") for m in member_rows if m.get("mcp_server_id")]
        if ids:
            servers = _safe_list(
                client.table("agent_mcp_servers").select("mcp_server_id, name")
                .in_("mcp_server_id", ids).order("name").execute()
            )
    except Exception:  # noqa: BLE001 - decoration only
        servers = []
    return templates.TemplateResponse(
        request,
        "tool_detail.html",
        {
            "ctx": ctx,
            "active": "tools",
            "current_env": env,
            "tool": tool,
            "source": source,
            "scope_options": _SCOPE_OPTIONS,
            "servers": servers,
            "agent_kinds": list(_tool_agent_kinds()),
        },
    )


@router.post("/tools/{tool_id}")
async def tool_update(
    tool_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    payload: Dict[str, Any] = {}

    for field_name in ("display_name", "description", "category", "usage_guidance"):
        if field_name in form:
            value = form.get(field_name)
            payload[field_name] = (value or "").strip() or None

    scope_val = form.get("scope")
    if scope_val:
        scope_val = scope_val.strip().lower()
        if scope_val not in _SCOPE_OPTIONS:
            raise HTTPException(400, f"scope must be one of {_SCOPE_OPTIONS}")
        payload["scope"] = scope_val

    # The form always submits a hidden marker so we can persist
    # "unchecked" — without it an absent checkbox would look the same as
    # "leave field alone".
    if form.get("_meta_form") == "1":
        payload["is_deprecated"] = form.get("is_deprecated") == "on"
        # One checkbox per agent now (repeated field); the old one-per-line
        # textarea body is still accepted — both join into the same parser.
        getlist = getattr(form, "getlist", None)
        raw_kinds = getlist("additional_agent_kinds") if getlist else [form.get("additional_agent_kinds")]
        payload["additional_agent_kinds"] = _parse_additional_kinds(
            "\n".join(v for v in raw_kinds if v)
        )

    client = admin_service_client()
    before = _safe_list(
        client.table("agent_tools").select("*").eq("tool_id", tool_id).limit(1).execute()
    )
    if not before:
        raise HTTPException(404, "tool not found")
    client.table("agent_tools").update(payload).eq("tool_id", tool_id).execute()
    record_change(
        entity_type="agent_tools",
        entity_id=tool_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before=before[0],
        after={**before[0], **payload},
    )
    return RedirectResponse(url=f"/admin/tools/{tool_id}", status_code=302)
