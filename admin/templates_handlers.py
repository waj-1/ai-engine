"""New-from-template gallery — browse the protected mold catalog and fork
one into a named draft.

Sidecar to `admin.router.router` (same wiring as profile_fork_handlers /
channel_handlers): attaches `/admin/templates` + `/admin/agents/from-template`
to the shared router. Imported at the bottom of router.py for its
side-effects.

Routes:
    GET  /admin/templates              → templates_gallery.html (mold catalog)
    POST /admin/agents/from-template    → fork a mold into a new draft profile

Both routes are superuser-only, matching every other /admin/* route. The
catalog itself (`agent_profiles.status='template'`) is a protected,
read-only surface from the portal — there is no create/edit/delete here,
only "use" (fork).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from admin.auth import require_superuser
from admin.router import current_env, router, templates
from admin.services.profiles import ProfileNotFoundError
from admin.services.templates import TemplateNotFoundError, list_templates, use_template
from core.auth import admin_service_client

logger = logging.getLogger(__name__)


def _model_options() -> List[Dict[str, Any]]:
    """Chat models the wizard offers. Without this the new agent silently
    took the template's model, which on an install with no key for it meant
    an agent that could not answer until someone found Settings › Model."""
    from admin._model_picker import fetch_models_for_picker

    return fetch_models_for_picker(active_only=True)


def _parent_options(client) -> List[Dict[str, Any]]:
    """Agents a new one can be attached under ("Make it a sub-agent of…").

    `use_template` only accepts a STAGING parent, so the picker lists the
    staging agents (templates excluded) — replacing the old free-typed
    profile_id box. Best-effort: a failed lookup leaves the picker empty
    (the agent is then created standalone), never a 500.
    """
    try:
        rows = (client.table("agent_profiles")
                .select("profile_id, display_name, agent_kind, status")
                .eq("environment", "staging").execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("templates gallery: parent lookup failed (%s)", exc)
        return []
    rows = [r for r in rows if isinstance(r, dict) and r.get("status") != "template"]
    from core.deployment import scoped_build
    if scoped_build():
        # Same scoping as the agents list: only agents this build serves.
        from admin.seed_agent_profiles import visible_agent_kinds
        served = set(visible_agent_kinds())
        rows = [r for r in rows if r.get("agent_kind") in served]
    return sorted(
        ({"profile_id": r["profile_id"],
          "name": r.get("display_name") or r.get("agent_kind")} for r in rows),
        key=lambda r: (r["name"] or "").lower(),
    )


def _render_gallery(
    request: Request,
    env: str,
    ctx: dict,
    *,
    error: Optional[str] = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Fetch the mold catalog and render the gallery, grouped single vs
    composite. The only DB call on this path is `list_templates` — the
    optional `as_subagent_of` field on the "use" form is a free-typed
    profile id rather than a server-rendered picker, so this stays a
    single, cheap read."""
    client = admin_service_client()
    catalog = list_templates(client)
    singles = [t for t in catalog if not t.get("is_composite")]
    composites = [t for t in catalog if t.get("is_composite")]
    return templates.TemplateResponse(
        request,
        "templates_gallery.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": env,
            "singles": singles,
            "composites": composites,
            "parent_options": _parent_options(client),
            "model_options": _model_options(),
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/templates", response_class=HTMLResponse)
async def templates_gallery(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Browse the protected mold catalog
    (`agent_profiles.status='template'`), grouped single-agent vs
    composite (a root mold plus sub-agent molds)."""
    return _render_gallery(request, env, ctx)


@router.post("/agents/from-template")
async def agents_from_template(
    request: Request,
    env: str = Depends(current_env),
    template_id: str = Form(...),
    display_name: str = Form(...),
    slug: str = Form(""),
    as_subagent_of: str = Form(""),
    model_id: str = Form(""),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Fork a mold (and its sub-agent molds, if composite) into a new
    named draft. See `admin.services.templates.use_template` for the
    fork + rewire + audit logic — this route is a thin wrapper.

    An optional `slug` is used verbatim (no uniqueness suffix) so an
    operator can create a profile with a stable, predictable slug — needed
    for a delegation specialist, whose synthetic `ask_<slug>` tool name
    must match what a parent (and any eval) expects. Left blank, the fork
    keeps the usual uniqueness suffix.

    `actor_user_id` is taken from the authenticated superuser session
    (never the form body), matching every other admin write path.
    """
    try:
        result = use_template(
            template_id=template_id,
            display_name=display_name,
            slug=slug.strip() or None,
            as_subagent_of=as_subagent_of.strip() or None,
            model_id=model_id.strip() or None,
            actor_user_id=ctx["user_id"],
        )
    except (TemplateNotFoundError, ProfileNotFoundError) as exc:
        return _render_gallery(request, env, ctx, error=str(exc), status_code=400)
    return RedirectResponse(
        url=f"/admin/agents?profile_id={result['root']}", status_code=303,
    )
