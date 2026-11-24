"""Portal screens for CREATING bundle MCPs, CONNECTING external MCP servers,
and DISCOVERING an external server's tools. Sidecar to admin/router.py
(attached at its bottom, like admin/mcp_handlers.py).

These screens are purely additive operator config: they write agent_mcp_servers
(source=bundle|external), agent_mcp_tools (bundle membership), and never change
agent runtime behaviour directly. Built-in bundles (is_builtin=True) are
seeder-owned and NOT editable/deletable here.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import _safe_list, current_env, router, templates
# Imported at module level so tests can monkeypatch them on this module.
from core.auth import admin_service_client
from runtime.tools._http_adapter import _classify_endpoint
from runtime.tools._mcp_adapter import list_tools_cached, MCPError

from admin.tool_handlers import (
    _DEFAULT_HTTP_TOOL_SCHEMA,
    _tool_agent_kinds,
    parse_http_tool_form,
)
from core.agent_profile import AgentProfileLoader

logger = logging.getLogger(__name__)

TRANSPORT_OPTIONS = ("stdio", "sse", "http")


# ---------------------------------------------------------------------------
# Discover endpoint — live `tools/list` against an external MCP server so an
# operator can see what a candidate endpoint exposes BEFORE wiring it into
# agent_mcp_servers / agent_mcp_tools. Mirrors admin/tool_handlers.py's
# `POST /tools/dry-run`: always HTTP 200, `ok: bool` distinguishes "we talked
# to the endpoint" from "we never got far enough to call it", loopback/
# link-local targets are blocked up front, and the resolved auth token is
# used to call the server but is NEVER echoed back in the response.
# ---------------------------------------------------------------------------


@router.post("/mcp/discover")
async def mcp_discover(request: Request, ctx: dict = Depends(require_superuser)) -> JSONResponse:
    """Live `tools/list` against an external MCP endpoint. Always HTTP 200 with
    an {ok, tools, latency_ms, error, error_class} envelope so the frontend can
    colour-code without parsing messages (mirrors POST /tools/dry-run)."""
    try:
        payload = await request.json()
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"body not valid JSON: {exc}",
             "error_class": "ValidationError", "tools": [], "latency_ms": 0},
            status_code=200,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "error": "request body must be a JSON object",
             "error_class": "ValidationError", "tools": [], "latency_ms": 0},
            status_code=200,
        )

    # Coerce to str BEFORE any string op: a truthy non-str (int/list) would
    # bypass the `or ""` fallback and blow up `.strip()` with an
    # AttributeError, breaking the never-500 contract. str() is total.
    endpoint = str(payload.get("endpoint") or "").strip()
    auth_env_var = str(payload.get("auth_env_var")).strip() if payload.get("auth_env_var") else None
    if not endpoint or not endpoint.lower().startswith(("http://", "https://")):
        return JSONResponse(
            {"ok": False, "error": "endpoint must be an absolute http(s) URL",
             "error_class": "ConfigError", "tools": [], "latency_ms": 0},
            status_code=200,
        )

    # Block loopback / link-local / cloud-metadata targets BEFORE calling out
    # — consistent with runtime.tools._http_adapter._call, which refuses these
    # hosts. Escapable only via MCP_ALLOW_LOCAL_ENDPOINTS, for local dev against
    # a docker-composed mock MCP server. A malformed URL (e.g. a bad IPv6
    # bracket) makes urlparse().hostname raise ValueError inside
    # _classify_endpoint — degrade to the envelope rather than 500.
    try:
        classification = _classify_endpoint(endpoint)
    except Exception as exc:  # noqa: BLE001 — malformed URL, never 500
        return JSONResponse(
            {"ok": False, "error": f"could not parse endpoint: {exc}",
             "error_class": "ConfigError", "tools": [], "latency_ms": 0},
            status_code=200,
        )
    allow_local = os.getenv("MCP_ALLOW_LOCAL_ENDPOINTS", "").strip().lower() in {"1", "true", "yes", "on"}
    if classification == "blocked" and not allow_local:
        logger.warning(
            "mcp discover blocked: %s resolves to a loopback/link-local/metadata host (actor=%s)",
            endpoint, ctx.get("user_id"),
        )
        return JSONResponse(
            {"ok": False,
             "error": "endpoint resolves to a loopback, link-local, or cloud-metadata "
                      "address and was blocked",
             "error_class": "ConfigError", "tools": [], "latency_ms": 0},
            status_code=200,
        )

    # Never forward the secret to a target we didn't classify as public — same
    # rule build_http_tool applies to operator-authored HTTP tools.
    auth_token = os.getenv(auth_env_var) if (auth_env_var and classification == "public") else None

    started = time.monotonic()
    try:
        tools = list_tools_cached(endpoint, auth_token)
    except MCPError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "error_class": "MCPError",
             "tools": [], "latency_ms": int((time.monotonic() - started) * 1000)},
            status_code=200,
        )
    except Exception as exc:  # noqa: BLE001 — never leak a stack to the operator
        return JSONResponse(
            {"ok": False, "error": f"discover failed: {exc}", "error_class": type(exc).__name__,
             "tools": [], "latency_ms": int((time.monotonic() - started) * 1000)},
            status_code=200,
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    slim = [
        {"name": t.get("name"), "description": t.get("description"), "inputSchema": t.get("inputSchema")}
        for t in (tools or [])
        if isinstance(t, dict) and t.get("name")
    ]
    return JSONResponse(
        {"ok": True, "tools": slim, "error": None, "error_class": None, "latency_ms": latency_ms},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Connect MCP — register an EXTERNAL agent_mcp_servers row. Operators use the
# Discover panel above to sanity-check the endpoint/auth before submitting;
# the create path itself doesn't call out to the network — it just validates
# and persists. Mirrors admin/tool_handlers.py's `_err` re-render pattern:
# validation failures re-render the same form (HTTP 200) with the operator's
# input preserved rather than losing it to a redirect or a raw 4xx.
# ---------------------------------------------------------------------------


def _parse_tool_subset(raw: str) -> Optional[List[str]]:
    """Comma-separated tool names → list (or None = all tools exposed).

    Same shape as admin/mcp_handlers.py::_parse_tool_subset — kept as its own
    copy here so this module doesn't reach into mcp_handlers' internals, but
    intentionally identical behaviour: empty/whitespace-only → None.
    """
    if not raw or not raw.strip():
        return None
    tools = [t.strip() for t in raw.split(",") if t.strip()]
    return tools if tools else None


def _parse_context_fields(raw: str) -> Dict[str, Any]:
    """Parse the context_fields textarea into a dict.

    Blank input defaults to `{}` (no extra context wired). Anything that
    fails to parse as JSON, or parses to a non-object (list/str/number),
    raises ValueError so the caller can re-render with an error instead of
    persisting a malformed value.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ValueError("must be a JSON object, e.g. {\"token\": \"$ctx.user_token\"}")
    return parsed


@router.get("/mcp/connect", response_class=HTMLResponse)
async def mcp_connect_form(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Render the Connect MCP (external server) creation form."""
    return templates.TemplateResponse(
        request,
        "mcp_connect.html",
        {
            "ctx": ctx,
            "active": "tools",
            "current_env": env,
            "transport_options": TRANSPORT_OPTIONS,
            "error": None,
            "form": {},
        },
    )


@router.post("/mcp/connect")
async def mcp_connect_create(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    """Insert a new external agent_mcp_servers row from the Connect MCP form."""
    form = await request.form()
    name = (form.get("name") or "").strip()
    endpoint = (form.get("endpoint") or "").strip()
    transport = (form.get("transport") or "http").strip()

    def _err(msg: str) -> Response:
        return templates.TemplateResponse(
            request, "mcp_connect.html",
            {
                "ctx": ctx, "active": "tools", "error": msg,
                "transport_options": TRANSPORT_OPTIONS, "form": dict(form),
            },
            status_code=200,
        )

    if not name:
        return _err("Name is required.")
    # The __builtin__: prefix is reserved for seeder-managed built-in bundles
    # (matches Task 4's create-bundle guard); operators can't claim it here.
    if name.startswith("__builtin__:"):
        return _err("The __builtin__: prefix is reserved for seeder-managed bundles.")
    if not endpoint or not endpoint.lower().startswith(("http://", "https://")):
        return _err("Endpoint must be an absolute http(s) URL.")
    from demo_mode import policy as demo_policy

    problem = demo_policy.tool_endpoint_problem(endpoint, form.get("auth_env_var"))
    if problem:
        return _err(problem)
    try:
        context_fields = _parse_context_fields(form.get("context_fields") or "")
    except ValueError as exc:
        return _err(f"context_fields must be a JSON object: {exc}")

    row = {
        "name": name,
        "source": "external",
        "is_builtin": False,
        "transport": transport,
        "endpoint": endpoint,
        "auth_env_var": (form.get("auth_env_var") or "").strip() or None,
        "tool_subset": _parse_tool_subset(form.get("tool_subset") or ""),
        "context_fields": context_fields,
        "is_active": True,
    }
    client = admin_service_client()
    # agent_mcp_servers has UNIQUE(name): a duplicate (double-submit or a real
    # collision) surfaces from PostgREST as an exception — route it back to the
    # form rather than 500ing, same as admin/tool_handlers.py does on insert.
    try:
        inserted = client.table("agent_mcp_servers").insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — PostgREST surfaces unique-violations as exceptions
        msg = str(exc)
        if "duplicate" in msg.lower() or "unique" in msg.lower() or "already exists" in msg.lower():
            return _err(f"An MCP named {name!r} already exists — pick a different name.")
        return _err(f"Could not create the MCP: {exc}")
    new = inserted.data[0] if inserted.data else {}
    new_id = new.get("mcp_server_id")
    if not new_id:
        return _err("The MCP was not created (no id returned). Please retry.")
    record_change(
        entity_type="agent_mcp_servers",
        entity_id=new_id,
        action="create",
        actor_user_id=ctx["user_id"],
        after=new,
    )
    return RedirectResponse(url=f"/admin/agent-mcp-servers/{new_id}", status_code=303)


# ---------------------------------------------------------------------------
# Create MCP — author a bundle agent_mcp_servers row (source='bundle') plus
# its agent_mcp_tools membership from a checkbox picker over non-deprecated
# agent_tools. Mirrors the Connect screen's validation/_err shape above:
# same reserved-prefix and unique-name guards, same "re-render with HTTP 200
# and the operator's input preserved" pattern on every failure path.
# ---------------------------------------------------------------------------


def _load_tools_for_picker(client) -> List[Dict[str, Any]]:
    """Non-deprecated agent_tools (python + http), for the bundle member picker.

    Ordered by (agent_kind, python_name) so the list is deterministic across the
    GET render and any _err re-render — matches the ordering convention in
    admin/router.py (the tools list view).
    """
    from admin.agent_kinds import scope_to_build

    # A partner build offers only its own agents' tools (the agents build
    # listed all 147, other agents' included) — admin.agent_kinds is the one
    # place that decides what a build serves.
    return scope_to_build(_safe_list(
        client.table("agent_tools")
        .select("tool_id, python_name, display_name, kind, agent_kind, "
                "additional_agent_kinds, description")
        .eq("is_deprecated", False)
        .order("agent_kind")
        .order("python_name")
        .execute()
    ))


@router.get("/mcp/create", response_class=HTMLResponse)
async def mcp_create_form(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Render the Create MCP (bundle) form with the member picker."""
    client = admin_service_client()
    tools = _load_tools_for_picker(client)
    return templates.TemplateResponse(
        request,
        "mcp_create.html",
        {
            "ctx": ctx,
            "active": "tools",
            "current_env": env,
            "error": None,
            "tools": tools,
            "form": {},
            "checked": set(),
        },
    )


@router.post("/mcp/create")
async def mcp_create_bundle(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    """Insert a new bundle agent_mcp_servers row, then an agent_mcp_tools
    membership row (in form/checkbox order) for each checked member tool."""
    form = await request.form()
    name = (form.get("name") or "").strip()
    member_ids = [v for v in form.getlist("member_tool_ids") if v]

    def _err(msg: str) -> Response:
        client = admin_service_client()
        tools = _load_tools_for_picker(client)
        return templates.TemplateResponse(
            request, "mcp_create.html",
            {
                "ctx": ctx, "active": "tools", "error": msg, "tools": tools,
                "form": dict(form), "checked": set(member_ids),
            },
            status_code=200,
        )

    if not name:
        return _err("Name is required.")
    # The __builtin__: prefix is reserved for seeder-managed built-in bundles
    # (matches mcp_connect_create's guard) — operators can't claim it here.
    if name.startswith("__builtin__:"):
        return _err("The __builtin__: prefix is reserved for seeder-managed bundles.")
    try:
        context_fields = _parse_context_fields(form.get("context_fields") or "")
    except ValueError as exc:
        return _err(f"context_fields must be a JSON object: {exc}")

    # Server-side enforcement of the picker's "non-deprecated only" invariant:
    # validate every posted member id against the SAME query the picker renders
    # from, BEFORE any write. A forged/stale/deprecated id would otherwise be
    # inserted-then-silently-dropped by the FK + per-row try/except below, with
    # no operator feedback — instead re-render the picker with an explicit error.
    valid_ids = {t["tool_id"] for t in _load_tools_for_picker(admin_service_client())}
    if any(mid not in valid_ids for mid in member_ids):
        return _err("Some selected tools are unavailable or deprecated — reselect from the current list.")

    client = admin_service_client()
    row = {
        "name": name,
        "source": "bundle",
        "is_builtin": False,
        "usage_guidance": (form.get("usage_guidance") or "").strip() or None,
        "context_fields": context_fields,
        "is_active": True,
    }
    # agent_mcp_servers has UNIQUE(name): a duplicate (double-submit or a real
    # collision) surfaces from PostgREST as an exception — route it back to the
    # form rather than 500ing, same as mcp_connect_create above.
    try:
        inserted = client.table("agent_mcp_servers").insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — PostgREST surfaces unique-violations as exceptions
        msg = str(exc)
        if "duplicate" in msg.lower() or "unique" in msg.lower() or "already exists" in msg.lower():
            return _err(f"An MCP named {name!r} already exists — pick a different name.")
        return _err(f"Could not create the MCP: {exc}")
    new = inserted.data[0] if inserted.data else {}
    mcp_id = new.get("mcp_server_id")
    if not mcp_id:
        # Nothing to attach membership to and nothing to redirect to — re-render
        # rather than write orphan agent_mcp_tools rows or redirect to /None.
        return _err("The MCP was not created (no id returned). Please retry.")

    for pos, tool_id in enumerate(member_ids):
        try:
            client.table("agent_mcp_tools").insert(
                {"mcp_server_id": mcp_id, "tool_id": tool_id, "position": pos}
            ).execute()
        except Exception:  # noqa: BLE001 — one bad member shouldn't lose the whole bundle
            logger.exception(
                "mcp bundle %s: failed to attach member tool %s at position %s",
                mcp_id, tool_id, pos,
            )

    record_change(
        entity_type="agent_mcp_servers",
        entity_id=mcp_id,
        action="create",
        actor_user_id=ctx["user_id"],
        after=new,
    )
    return RedirectResponse(url=f"/admin/agent-mcp-servers/{mcp_id}", status_code=303)


# ---------------------------------------------------------------------------
# Add HTTP tool INSIDE an internal (bundle, non-builtin) MCP. Authoring a tool
# standalone is retired — a tool is born as a member of its MCP. Reuses the
# HTTP-tool form + validation from admin/tool_handlers, then inserts the
# agent_mcp_tools membership row so the new tool is immediately encapsulated.
# Built-in (seeder-managed) and external MCPs are refused server-side.
# ---------------------------------------------------------------------------


def _load_internal_bundle_or_none(client, mcp_server_id: str) -> Optional[Dict[str, Any]]:
    """The agent_mcp_servers row iff it is an editable internal bundle
    (source='bundle', not is_builtin); else None. Built-in and external MCPs
    can't author tools here."""
    rows = _safe_list(
        client.table("agent_mcp_servers").select("*")
        .eq("mcp_server_id", mcp_server_id).limit(1).execute()
    )
    if not rows:
        return None
    row = rows[0]
    if row.get("source") != "bundle" or row.get("is_builtin"):
        return None
    return row


@router.get("/agent-mcp-servers/{mcp_server_id}/add-tool", response_class=HTMLResponse)
async def mcp_add_tool_form(
    mcp_server_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Render the HTTP-tool creation form scoped to an internal MCP bundle."""
    client = admin_service_client()
    mcp = _load_internal_bundle_or_none(client, mcp_server_id)
    if mcp is None:
        # Built-in / external / missing — no in-MCP authoring; bounce to detail.
        return RedirectResponse(url=f"/admin/agent-mcp-servers/{mcp_server_id}", status_code=303)
    return templates.TemplateResponse(
        request, "tool_new.html",
        {
            "ctx": ctx, "active": "tools", "current_env": env,
            "form": {}, "default_schema": _DEFAULT_HTTP_TOOL_SCHEMA,
            "agent_kinds": list(_tool_agent_kinds()), "error": None,
            "form_action": f"/admin/agent-mcp-servers/{mcp_server_id}/add-tool",
            "cancel_url": f"/admin/agent-mcp-servers/{mcp_server_id}",
            "mcp_name": mcp.get("name"),
        },
    )


@router.post("/agent-mcp-servers/{mcp_server_id}/add-tool")
async def mcp_add_tool_submit(
    mcp_server_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Create an HTTP tool AND add it as a member of this internal MCP bundle."""
    form = await request.form()
    client = admin_service_client()
    mcp = _load_internal_bundle_or_none(client, mcp_server_id)
    if mcp is None:
        return RedirectResponse(url=f"/admin/agent-mcp-servers/{mcp_server_id}", status_code=303)

    def _err(message: str) -> Response:
        return templates.TemplateResponse(
            request, "tool_new.html",
            {
                "ctx": ctx, "active": "tools", "current_env": env,
                "form": {k: form.get(k) for k in form.keys()},
                "default_schema": form.get("argument_schema") or _DEFAULT_HTTP_TOOL_SCHEMA,
                "agent_kinds": list(_tool_agent_kinds()), "error": message,
                "form_action": f"/admin/agent-mcp-servers/{mcp_server_id}/add-tool",
                "cancel_url": f"/admin/agent-mcp-servers/{mcp_server_id}",
                "mcp_name": mcp.get("name"),
            },
            status_code=400,
        )

    try:
        payload = parse_http_tool_form(form, agent_kinds=_tool_agent_kinds())
    except ValueError as exc:
        return _err(str(exc))

    try:
        inserted = client.table("agent_tools").insert(payload).execute()
    except Exception as exc:  # noqa: BLE001 — PostgREST surfaces unique-violations as exceptions
        return _err(f"Could not create the tool: {exc}")
    new_tool = (inserted.data or [{}])[0]
    new_tool_id = new_tool.get("tool_id")
    if not new_tool_id:
        return _err("The tool was created but no id was returned. Please retry.")

    record_change(
        entity_type="agent_tools", entity_id=new_tool_id, action="create",
        actor_user_id=ctx["user_id"], after=new_tool,
    )

    # Append as the last member of the bundle.
    existing = _safe_list(
        client.table("agent_mcp_tools").select("tool_id")
        .eq("mcp_server_id", mcp_server_id).execute()
    )
    position = len(existing)
    try:
        client.table("agent_mcp_tools").insert(
            {"mcp_server_id": mcp_server_id, "tool_id": new_tool_id, "position": position}
        ).execute()
    except Exception:  # noqa: BLE001 — tool exists; membership recoverable via the picker
        logger.exception(
            "add-tool: created tool %s but failed to attach to MCP %s",
            new_tool_id, mcp_server_id,
        )
        return _err(
            "The HTTP tool was created but could not be attached to this MCP "
            "automatically. Add it from the Membership picker on the MCP page."
        )
    record_change(
        entity_type="agent_mcp_tools", entity_id=mcp_server_id, action="update",
        actor_user_id=ctx["user_id"],
        after={"mcp_server_id": mcp_server_id, "tool_id": new_tool_id, "position": position},
    )

    AgentProfileLoader.invalidate()
    return RedirectResponse(url=f"/admin/agent-mcp-servers/{mcp_server_id}", status_code=303)
