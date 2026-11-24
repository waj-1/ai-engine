"""
MCP server (agent_mcp_servers) detail / edit / delete handlers.

Kept in a separate module so `admin/router.py` stays slim.  Imports the
shared `router`, `templates`, `_safe_list`, `current_env` helpers from the
router module.

Routes added:
  GET  /admin/agent-mcp-servers/{mcp_server_id}        — editable detail view
  POST /admin/agent-mcp-servers/{mcp_server_id}        — persist edits
  POST /admin/agent-mcp-servers/{mcp_server_id}/delete — detach from profiles + hard-delete
  POST /admin/agent-mcp-servers/{mcp_server_id}/members — reconcile bundle membership

There is no list view or create route at /admin/agent-mcp-servers any more:
the GET is a 307 redirect to the merged /admin/tools screen (router.py), and
creation goes through the validated /admin/mcp/create and /admin/mcp/connect
flows (mcp_manage_handlers.py). The old _make_simple_crud factory that owned
both was removed 2026-08-26 — its list view was unreachable behind the
redirect and its create had no caller.

Built-in bundles (``is_builtin=True``) are seeder-managed: update, delete, and
the members route all refuse to write when the loaded row has ``is_builtin``
set, redirecting back to the detail page with an ``?error=`` message instead.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import (
    _safe_list,
    current_env,
    router,
    templates,
)
from core.auth import admin_service_client, get_service_client  # noqa: F401 — get_service_client kept importable for tests that patch it

logger = logging.getLogger(__name__)

TRANSPORT_OPTIONS = ("stdio", "sse", "http")

_BUILTIN_ERROR = "built-in+MCPs+are+managed+by+the+seeder"


def _parse_tool_subset(raw: str) -> Optional[List[str]]:
    """Parse a comma-separated tool subset string into a list (or None = all tools).

    Strips whitespace and drops empty tokens.  An empty / whitespace-only
    string → None so the DB stores NULL (meaning "all tools exposed").
    """
    if not raw or not raw.strip():
        return None
    tools = [t.strip() for t in raw.split(",") if t.strip()]
    return tools if tools else None


def _parse_context_fields(raw: str) -> Dict[str, Any]:
    """Parse the context_fields textarea into a dict.

    Blank input defaults to ``{}`` (no extra context wired). Anything that
    fails to parse as JSON, or parses to a non-object (list/str/number),
    raises ValueError so the caller can redirect with an error instead of
    persisting a malformed value.

    Kept as its own local copy rather than importing from
    admin/mcp_manage_handlers.py — same shape, but this module intentionally
    doesn't reach into that module's internals (see the module docstring).
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


def _load_tools_for_picker(client) -> List[Dict[str, Any]]:
    """Non-deprecated agent_tools, for the bundle member picker.

    Same query shape as admin/mcp_manage_handlers.py::_load_tools_for_picker
    (kept as its own copy for the same reason as `_parse_context_fields`).
    """
    return _safe_list(
        client.table("agent_tools")
        .select("tool_id, python_name, kind, agent_kind, additional_agent_kinds, description")
        .eq("is_deprecated", False)
        .order("agent_kind")
        .order("python_name")
        .execute()
    )


def _picker_tools_for_build(all_tools: List[Dict[str, Any]],
                            member_ids: set) -> List[Dict[str, Any]]:
    """The member picker on this build: this build's own agents' tools, plus
    any tool already in the bundle (so saving never silently drops it)."""
    from admin.agent_kinds import row_in_build

    return [t for t in all_tools if row_in_build(t) or t["tool_id"] in member_ids]


def _load_members(client, mcp_server_id: str) -> List[Dict[str, Any]]:
    """Current agent_mcp_tools membership for a bundle server, joined to
    agent_tools display fields (python_name/kind/agent_kind/description),
    ordered by position."""
    member_rows = _safe_list(
        client.table("agent_mcp_tools")
        .select("tool_id, position, context_fields")
        .eq("mcp_server_id", mcp_server_id)
        .order("position")
        .execute()
    )
    if not member_rows:
        return []
    tools_by_id = {t["tool_id"]: t for t in _load_tools_for_picker(client)}
    members = []
    for m in sorted(member_rows, key=lambda r: r.get("position") or 0):
        merged = {**tools_by_id.get(m["tool_id"], {}), **m}
        members.append(merged)
    return members


# ---------------------------------------------------------------------------
# GET /admin/agent-mcp-servers/{mcp_server_id}
# ---------------------------------------------------------------------------


@router.get(
    "/agent-mcp-servers/{mcp_server_id}",
    response_class=HTMLResponse,
    name="mcp_server_detail",
)
async def mcp_server_detail(
    mcp_server_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_mcp_servers")
        .select("*")
        .eq("mcp_server_id", mcp_server_id)
        .limit(1)
        .execute()
    )
    if not rows:
        raise HTTPException(404, "MCP server not found")
    row = rows[0]

    # Render tool_subset as a comma-separated string for the textarea.
    tool_subset_str = ""
    subset = row.get("tool_subset")
    if subset:
        tool_subset_str = ", ".join(subset)

    context_fields_str = json.dumps(row.get("context_fields") or {}, indent=2)

    # Membership: current agent_mcp_tools rows (joined to agent_tools display
    # fields) + the full non-deprecated tools list for the member picker.
    # Only meaningful for source=bundle, but cheap enough to always load so
    # the template doesn't need a second round-trip.
    members = _load_members(client, mcp_server_id)
    checked_tool_ids = {m["tool_id"] for m in members}
    picker_tools = _picker_tools_for_build(_load_tools_for_picker(client), checked_tool_ids)

    att_rows = _safe_list(
        client.table("agent_profile_mcp_servers")
        .select("profile_id")
        .eq("mcp_server_id", mcp_server_id)
        .execute()
    )
    attached_count = len(att_rows)
    # Name the agents that use this server (the old screen only said
    # "N profiles"), so "which agents use this?" is answered in words.
    used_by: List[Dict[str, Any]] = []
    prof_ids = [a.get("profile_id") for a in att_rows if a.get("profile_id")]
    if prof_ids:
        try:
            profs = _safe_list(
                client.table("agent_profiles")
                .select("profile_id, display_name, agent_kind, environment, status")
                .in_("profile_id", prof_ids)
                .execute()
            )
        except Exception:  # noqa: BLE001 - decoration only
            profs = []
        used_by = sorted(
            (p for p in profs if p.get("status") != "template"),
            key=lambda p: ((p.get("display_name") or ""), p.get("environment") or ""),
        )
    if row.get("source") == "bundle":
        tool_count: Optional[int] = len(members)
    else:
        tool_count = len(row.get("tool_subset") or []) or None

    return templates.TemplateResponse(
        request,
        "mcp_server_detail.html",
        {
            "ctx": ctx,
            "active": "tools",
            "current_env": env,
            "row": row,
            "tool_subset_str": tool_subset_str,
            "context_fields_str": context_fields_str,
            "transport_options": TRANSPORT_OPTIONS,
            "members": members,
            "picker_tools": picker_tools,
            "checked_tool_ids": checked_tool_ids,
            "attached_count": attached_count,
            "used_by": used_by,
            "tool_count": tool_count,
            "error": request.query_params.get("error"),
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/agent-mcp-servers/{mcp_server_id}  — update
# ---------------------------------------------------------------------------


@router.post("/agent-mcp-servers/{mcp_server_id}", name="mcp_server_update")
async def mcp_server_update(
    mcp_server_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()

    # Env-aware client (main #1207): writes land in the switcher-selected
    # branch. My complete payload — incl. usage_guidance + context_fields —
    # is built below (after the built-in / reserved-prefix guards).
    client = admin_service_client()
    before_rows = _safe_list(
        client.table("agent_mcp_servers")
        .select("*")
        .eq("mcp_server_id", mcp_server_id)
        .limit(1)
        .execute()
    )
    if not before_rows:
        raise HTTPException(404, "MCP server not found")
    before = before_rows[0]

    # Built-in bundles are seeder-managed — refuse the write outright.
    if before.get("is_builtin"):
        return RedirectResponse(
            url=f"/admin/agent-mcp-servers/{mcp_server_id}?error={_BUILTIN_ERROR}",
            status_code=302,
        )

    # A rename to the reserved built-in prefix would produce a confusing display
    # name (and could collide with a seeder row) — reject it, matching create/connect.
    new_name = form.get("name")
    if new_name is not None and new_name.startswith("__builtin__:"):
        return RedirectResponse(
            url=f"/admin/agent-mcp-servers/{mcp_server_id}"
            f"?error=The+__builtin__:+prefix+is+reserved+for+seeder-managed+bundles.",
            status_code=302,
        )

    payload: dict[str, Any] = {}
    for field in ("name", "transport", "endpoint", "auth_env_var", "usage_guidance"):
        v = form.get(field)
        if v is not None:
            payload[field] = v if v != "" else None

    if payload.get("endpoint") or payload.get("auth_env_var"):
        from urllib.parse import quote_plus

        from demo_mode import policy as demo_policy

        problem = demo_policy.tool_endpoint_problem(
            payload.get("endpoint") or before.get("endpoint") or "", payload.get("auth_env_var"))
        if problem:
            return RedirectResponse(
                url=f"/admin/agent-mcp-servers/{mcp_server_id}?error={quote_plus(problem)}",
                status_code=302)

    # tool_subset: comma-separated → list (or None)
    if "tool_subset" in form:
        payload["tool_subset"] = _parse_tool_subset(form.get("tool_subset") or "")

    # context_fields: JSON object textarea → dict
    if "context_fields" in form:
        try:
            payload["context_fields"] = _parse_context_fields(form.get("context_fields") or "{}")
        except ValueError as exc:
            return RedirectResponse(
                url=f"/admin/agent-mcp-servers/{mcp_server_id}"
                f"?error=context_fields+must+be+a+JSON+object:+{exc}",
                status_code=302,
            )

    # is_active checkbox — only if the form opts in via marker
    if form.get("_active_form") == "1":
        payload["is_active"] = form.get("is_active") == "on"

    if not payload:
        return RedirectResponse(
            url=f"/admin/agent-mcp-servers/{mcp_server_id}", status_code=302
        )

    # agent_mcp_servers has UNIQUE(name): a rename that collides must re-render
    # with an error rather than 500 — matching the connect/create create-paths.
    try:
        client.table("agent_mcp_servers").update(payload).eq(
            "mcp_server_id", mcp_server_id
        ).execute()
    except Exception as exc:  # noqa: BLE001 — PostgREST surfaces unique-violations as exceptions
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg or "already exists" in msg:
            return RedirectResponse(
                url=f"/admin/agent-mcp-servers/{mcp_server_id}"
                f"?error=An+MCP+with+that+name+already+exists+%E2%80%94+pick+another.",
                status_code=302,
            )
        raise
    record_change(
        entity_type="agent_mcp_servers",
        entity_id=mcp_server_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before=before,
        after={**before, **payload},
    )
    return RedirectResponse(
        url=f"/admin/agent-mcp-servers/{mcp_server_id}", status_code=302
    )


# ---------------------------------------------------------------------------
# POST /admin/agent-mcp-servers/{mcp_server_id}/delete
# ---------------------------------------------------------------------------


@router.post(
    "/agent-mcp-servers/{mcp_server_id}/delete", name="mcp_server_delete"
)
async def mcp_server_delete(
    mcp_server_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Delete an MCP server and cascade-remove it from agent profiles.

    Steps:
      1. Load the server row for the audit "before" snapshot; refuse if
         it's a built-in bundle (seeder-managed).
      2. Detach (legacy): remove ``mcp_server_id`` from every
         ``agent_profiles.mcp_server_ids`` array that contains it.
      3. Cascade the join tables: ``agent_mcp_tools`` (bundle membership)
         and ``agent_profile_mcp_servers`` (profile attachments) both carry
         an ON DELETE CASCADE FK, but we delete them explicitly here too so
         the audit trail and this handler's behaviour don't depend on the
         DB's cascade actually firing.
      4. Delete the ``agent_mcp_servers`` row.
      5. Write an audit ``record_change``.
      6. Redirect to ``/admin/agent-mcp-servers``.

    If the row is already missing, redirect cleanly so idempotent re-POSTs
    from the browser don't 404 the operator.
    """
    client = admin_service_client()

    # Step 1: load the before snapshot.
    before_rows = _safe_list(
        client.table("agent_mcp_servers")
        .select("*")
        .eq("mcp_server_id", mcp_server_id)
        .limit(1)
        .execute()
    )
    if not before_rows:
        return RedirectResponse(url="/admin/agent-mcp-servers", status_code=302)
    before = before_rows[0]

    # Built-in bundles are seeder-managed — refuse the delete outright.
    if before.get("is_builtin"):
        return RedirectResponse(
            url=f"/admin/agent-mcp-servers/{mcp_server_id}?error={_BUILTIN_ERROR}",
            status_code=302,
        )

    # Step 2: detach mcp_server_id from all referencing agent_profiles
    # (legacy array column; still stripped alongside the new join table).
    all_profiles = _safe_list(
        client.table("agent_profiles").select("*").execute()
    )
    for profile in all_profiles:
        ids: list = list(profile.get("mcp_server_ids") or [])
        if mcp_server_id in ids:
            updated_ids = [i for i in ids if i != mcp_server_id]
            client.table("agent_profiles").update(
                {"mcp_server_ids": updated_ids}
            ).eq("profile_id", profile["profile_id"]).execute()

    # Step 3: cascade the join tables.
    client.table("agent_mcp_tools").delete().eq(
        "mcp_server_id", mcp_server_id
    ).execute()
    client.table("agent_profile_mcp_servers").delete().eq(
        "mcp_server_id", mcp_server_id
    ).execute()

    # Step 4: delete the server row.
    client.table("agent_mcp_servers").delete().eq(
        "mcp_server_id", mcp_server_id
    ).execute()

    # Step 5: audit.
    record_change(
        entity_type="agent_mcp_servers",
        entity_id=mcp_server_id,
        action="delete",
        actor_user_id=ctx["user_id"],
        before=before,
        after=None,
    )

    return RedirectResponse(url="/admin/agent-mcp-servers", status_code=302)


# ---------------------------------------------------------------------------
# POST /admin/agent-mcp-servers/{mcp_server_id}/members — membership editor
# ---------------------------------------------------------------------------


@router.post(
    "/agent-mcp-servers/{mcp_server_id}/members", name="mcp_server_members_update"
)
async def mcp_server_members_update(
    mcp_server_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Reconcile ``agent_mcp_tools`` membership for a ``source=bundle``
    server from the posted checkbox picker.

    Reconciliation is delete-all + re-insert in posted order (position
    0..N-1) rather than a diff — the picker always posts the full desired
    membership set, so this keeps the logic simple and idempotent. Refused
    for built-in bundles (seeder-managed) and for non-bundle (external)
    servers, which don't use this table at all.

    Every posted id is validated against the current non-deprecated tool
    set BEFORE the destructive delete (mirroring mcp_create_bundle) — a
    forged/deprecated/duplicate id must never wipe the existing membership
    and leave the bundle empty behind a false "success" redirect.
    """
    form = await request.form()
    # Dedupe while preserving first-occurrence order so a duplicated id can't
    # collide on the (mcp_server_id, tool_id) PK during re-insert.
    seen: set = set()
    member_ids: List[str] = []
    for v in form.getlist("member_tool_ids"):
        if v and v not in seen:
            seen.add(v)
            member_ids.append(v)

    client = admin_service_client()
    before_rows = _safe_list(
        client.table("agent_mcp_servers")
        .select("*")
        .eq("mcp_server_id", mcp_server_id)
        .limit(1)
        .execute()
    )
    if not before_rows:
        raise HTTPException(404, "MCP server not found")
    before = before_rows[0]

    if before.get("is_builtin"):
        return RedirectResponse(
            url=f"/admin/agent-mcp-servers/{mcp_server_id}?error={_BUILTIN_ERROR}",
            status_code=302,
        )
    if before.get("source") != "bundle":
        return RedirectResponse(
            url=f"/admin/agent-mcp-servers/{mcp_server_id}"
            f"?error=membership+editing+is+only+available+for+bundle+MCPs",
            status_code=302,
        )

    # Validate BEFORE any write: reject the whole POST (no delete, no insert)
    # if any posted id isn't a current non-deprecated tool. Otherwise a
    # forged/stale id would be delete-then-silently-dropped, wiping the
    # bundle's membership with no operator feedback.
    current_ids = {m["tool_id"] for m in _load_members(client, mcp_server_id)}
    valid_ids = {t["tool_id"] for t in _picker_tools_for_build(
        _load_tools_for_picker(client), current_ids)}
    if any(mid not in valid_ids for mid in member_ids):
        return RedirectResponse(
            url=f"/admin/agent-mcp-servers/{mcp_server_id}"
            f"?error=some+selected+tools+are+unavailable+or+deprecated"
            f"+%E2%80%94+reselect+from+the+current+list",
            status_code=302,
        )

    # Upsert-then-delete-stale via the shared helper: the bundle's tool set
    # is never emptied mid-write (see admin/mcp_membership.py). Returns the
    # prior members (ordered) for an honest audit before. Every posted id is
    # validated above, so member_ids == the persisted set.
    from admin.mcp_membership import reconcile_bundle_members

    before_members = reconcile_bundle_members(client, mcp_server_id, member_ids)

    record_change(
        entity_type="agent_mcp_servers",
        entity_id=mcp_server_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before={"member_tool_ids": before_members},
        after={"member_tool_ids": member_ids},
    )
    return RedirectResponse(
        url=f"/admin/agent-mcp-servers/{mcp_server_id}", status_code=302
    )
