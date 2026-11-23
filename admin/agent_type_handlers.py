"""Agents › Categories — `/admin/agent-types`.

User-defined groups for agents ("Support", "Sales", "Internal tools"):
create, rename, recolour, reorder, delete. A category is data, not an agent
kind: it changes nothing about how an agent runs. All rules live in
`admin/services/agent_types.py`, shared with the `agent_types` admin MCP
tool; these routes only parse the form and redirect.

Sidecar to `admin.router.router`, imported at the bottom of router.py for its
side effects (same wiring as skill_handlers / templates_handlers).

Routes:
    GET  /admin/agent-types                    the screen
    POST /admin/agent-types                    create
    POST /admin/agent-types/{id}/update        rename, re-describe, recolour
    POST /admin/agent-types/{id}/move          one place up or down
    POST /admin/agent-types/{id}/delete        delete; its agents stay, uncategorised

An agent is put in a category from its own Settings tab
(`POST /admin/profiles/{id}`, field `agent_type_id`) or in the New agent
wizard (`POST /admin/agents/from-template`, same field).
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from admin.auth import require_superuser
from admin.router import current_env, router, templates
from admin.services import agent_types as svc

logger = logging.getLogger(__name__)

PAGE = "/admin/agent-types"


def _back(env: str, *, error: Optional[str] = None, notice: Optional[str] = None) -> Response:
    url = f"{PAGE}?env={quote(env, safe='')}"
    if error:
        url += f"&error={quote(error)}"
    elif notice:
        url += f"&notice={quote(notice)}"
    return RedirectResponse(url=url, status_code=303)


def _visible_kinds() -> Optional[set]:
    """The agent kinds this build shows, or None for all (the same scoping as
    the Agents list, so the counts here match what that list shows)."""
    from core.deployment import scoped_build

    if not scoped_build():
        return None
    from admin.seed_agent_profiles import visible_agent_kinds

    return set(visible_agent_kinds())


@router.get("/agent-types", response_class=HTMLResponse, name="agent_types_page")
async def agent_types_page(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    load_error = None
    rows = []
    try:
        rows = svc.list_types(kinds=_visible_kinds())
    except Exception as exc:  # noqa: BLE001 — show the page with the reason
        logger.warning("agent types: could not load (%s)", exc)
        load_error = str(exc)
    return templates.TemplateResponse(
        request, "agent_types.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": env,
            "categories": rows,
            "palette": svc.PALETTE,
            "default_color": svc.DEFAULT_COLOR,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("notice"),
            "load_error": load_error,
        },
    )


@router.post("/agent-types", name="agent_types_create")
async def agent_types_create(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    try:
        row = svc.create_type(
            display_name=form.get("display_name") or "",
            slug=form.get("slug") or "",
            description=form.get("description") or "",
            color=form.get("color") or "",
            actor_user_id=ctx["user_id"],
        )
    except svc.AgentTypeError as exc:
        return _back(env, error=str(exc))
    return _back(env, notice=f"Added “{row['display_name']}”.")


@router.post("/agent-types/{agent_type_id}/update", name="agent_types_update")
async def agent_types_update(
    agent_type_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    changes = {k: form.get(k) for k in ("display_name", "description", "color") if k in form}
    try:
        row = svc.update_type(agent_type_id, changes, actor_user_id=ctx["user_id"])
    except svc.AgentTypeError as exc:
        return _back(env, error=str(exc))
    return _back(env, notice=f"Saved “{row['display_name']}”.")


@router.post("/agent-types/{agent_type_id}/move", name="agent_types_move")
async def agent_types_move(
    agent_type_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    try:
        svc.move_type(agent_type_id, form.get("direction") or "",
                      actor_user_id=ctx["user_id"])
    except svc.AgentTypeError as exc:
        return _back(env, error=str(exc))
    return _back(env)


@router.post("/agent-types/{agent_type_id}/delete", name="agent_types_delete")
async def agent_types_delete(
    agent_type_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    try:
        out = svc.delete_type(agent_type_id, actor_user_id=ctx["user_id"])
    except svc.AgentTypeError as exc:
        return _back(env, error=str(exc))
    n = out["agents_uncategorised"]
    name = out["deleted"].get("display_name")
    if n:
        tail = f" {n} agent{'' if n == 1 else 's'} {'is' if n == 1 else 'are'} now uncategorised."
    else:
        tail = " No agent was in it."
    return _back(env, notice=f"Deleted “{name}”.{tail}")
