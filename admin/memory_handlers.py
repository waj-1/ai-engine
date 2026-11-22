# ai_agent/admin/memory_handlers.py
"""Admin /memory UI — sidecar route module.

    GET  /admin/memory                     — tabs: memories | config | inspector
    POST /admin/memory/config              — save memory_config on the business profile
    POST /admin/memory/{memory_id}/delete  — soft-archive a memory
    POST /admin/memory/{memory_id}/pin     — pin / unpin
    POST /admin/memory/{memory_id}/edit    — edit content (re-embeds on save)
    POST /admin/memory/add                 — manual memory insert

Pattern matches admin/jobs_handlers.py: import the shared `router` and decorate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import client_for, current_env, router, templates
from admin.screens import memory as memory_screen
from admin.seed_agent_profiles import visible_agent_kinds
from core.agent_profile import AgentProfileLoader
from core.memory.embeddings import embed_text

logger = logging.getLogger(__name__)

# The kind the memory screens open on: the plugin's (`agent_catalog.
# default_kind()`), else the generic one.
from admin.agent_kinds import catalog_tuple as _catalog_tuple  # noqa: E402

_DEFAULT_AGENT_KIND = (_catalog_tuple("default_kind") or ("custom",))[0]


def _default_agent_kind() -> str:
    """Fallback kind for the picker — the default kind when this build shows
    it, else the first visible kind (a partner build's own agent)."""
    visible = visible_agent_kinds()
    return _DEFAULT_AGENT_KIND if _DEFAULT_AGENT_KIND in visible or not visible else visible[0]


def _valid_agent_kinds() -> tuple:
    """Kinds memory can be inspected/configured for. Scoped builds keep
    their client-scoped list; a WAJ build also accepts every live agent kind
    (admin/agent_kinds.py), so every agent with a profile is configurable."""
    from core.deployment import scoped_build

    if scoped_build():
        return tuple(visible_agent_kinds())
    from admin.agent_kinds import live_agent_kinds

    seen = list(visible_agent_kinds())
    for k in live_agent_kinds(include_system=True):
        if k not in seen:
            seen.append(k)
    return tuple(seen)


def _picker_agent_kinds(current: str) -> list:
    """Agent picker options: the shared live list without system agents
    (title / suggestion generators and the like) — plus the current kind if
    a deep link selected one of those."""
    from admin.agent_kinds import build_agent_kinds

    kinds = list(build_agent_kinds())
    if current and current not in kinds:
        kinds.append(current)
    return kinds


def _resolve_agent_kind(agent_kind: str) -> str:
    """Validate agent_kind against the visible kinds; fall back to the
    default visible kind if invalid (e.g. a full-build kind on a scoped build)."""
    if agent_kind in _valid_agent_kinds():
        return agent_kind
    fallback = _default_agent_kind()
    logger.warning("memory admin: unknown agent_kind %r — falling back to %s", agent_kind, fallback)
    return fallback


@router.get("/memory", response_class=HTMLResponse, name="memory_screen")
async def memory_home(
    request: Request,
    env: str = Depends(current_env),
    tab: str = Query("memories"),
    agent_kind: str = Query(_DEFAULT_AGENT_KIND),
    user: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    ctx: dict = Depends(require_superuser),
) -> Response:
    agent_kind = _resolve_agent_kind(agent_kind)
    client = client_for(env)
    data = {}
    if tab == "config":
        # Per-agent memory settings moved to the agent's own memory page
        # (/admin/agents/memory); the old tab URL keeps working.
        return RedirectResponse(
            url=f"/admin/agents/memory?agent_kind={agent_kind}&env={env}", status_code=307
        )
    if tab == "inspector":
        data = memory_screen.load_inspector_context(client, session_id)
    else:
        tab = "memories"
        data = memory_screen.load_memories_context(client, user, agent_kind)
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "ctx": ctx,
            "active": "memory",
            "current_env": env,
            "tab": tab,
            "agent_kind": agent_kind,
            "agent_kinds": _picker_agent_kinds(agent_kind),
            **data,
        },
    )


@router.get("/agents/memory", response_class=HTMLResponse, name="agent_memory_settings")
async def agent_memory_settings(
    request: Request,
    env: str = Depends(current_env),
    agent_kind: str = Query(_DEFAULT_AGENT_KIND),
    saved: Optional[str] = Query(None),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Per-agent memory settings — reached from the agent (Agents page) and
    from Knowledge › Memory. Same form + POST (/admin/memory/config) as the
    retired Memory › Config tab."""
    agent_kind = _resolve_agent_kind(agent_kind)
    client = client_for(env)
    data = memory_screen.load_config_context(client, agent_kind, env)
    return templates.TemplateResponse(
        request,
        "agent_memory.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": env,
            "agent_kind": agent_kind,
            "agent_kinds": _picker_agent_kinds(agent_kind),
            "saved": bool(saved),
            **data,
        },
    )


@router.post("/memory/config", name="memory_config_save")
async def memory_config_save(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = dict(await request.form())
    # agent_kind is submitted as a hidden field in the config form.
    agent_kind = _resolve_agent_kind(form.pop("agent_kind", _DEFAULT_AGENT_KIND) or _DEFAULT_AGENT_KIND)
    new_cfg = memory_screen.parse_config_form(form)
    client = client_for(env)
    profile = memory_screen.active_profile_row(
        client, agent_kind, env, select="profile_id, memory_config")
    if profile:
        before = profile.get("memory_config")
        client.table("agent_profiles").update({"memory_config": new_cfg}).eq(
            "profile_id", profile["profile_id"]
        ).execute()
        record_change(
            entity_type="agent_profiles", entity_id=profile["profile_id"],
            action="memory_config_update", actor_user_id=ctx["user_id"],
            before={"memory_config": before}, after={"memory_config": new_cfg},
        )
        AgentProfileLoader.invalidate(agent_kind, env)
    return RedirectResponse(
        url=f"/admin/agents/memory?agent_kind={agent_kind}&env={env}&saved=1", status_code=302
    )


@router.post("/memory/{memory_id}/delete", name="memory_delete")
async def memory_delete(
    memory_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    user: Optional[str] = Form(None),
    agent_kind: str = Form(_DEFAULT_AGENT_KIND),
) -> Response:
    agent_kind = _resolve_agent_kind(agent_kind)
    client = client_for(env)
    client.table("ai_user_memory").update({"status": "archived"}).eq("id", memory_id).execute()
    record_change(
        entity_type="ai_user_memory", entity_id=memory_id, action="archive",
        actor_user_id=ctx["user_id"],
    )
    url = f"/admin/memory?tab=memories&agent_kind={agent_kind}&env={env}" + (f"&user={user}" if user else "")
    return RedirectResponse(url=url, status_code=302)


@router.post("/memory/{memory_id}/pin", name="memory_pin")
async def memory_pin(
    memory_id: str,
    pinned: str = Form(...),
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    user: Optional[str] = Form(None),
    agent_kind: str = Form(_DEFAULT_AGENT_KIND),
) -> Response:
    agent_kind = _resolve_agent_kind(agent_kind)
    client = client_for(env)
    new_val = pinned == "true"
    client.table("ai_user_memory").update({"pinned": new_val}).eq("id", memory_id).execute()
    record_change(
        entity_type="ai_user_memory", entity_id=memory_id,
        action="pin" if new_val else "unpin", actor_user_id=ctx["user_id"],
    )
    url = f"/admin/memory?tab=memories&agent_kind={agent_kind}&env={env}" + (f"&user={user}" if user else "")
    return RedirectResponse(url=url, status_code=302)


@router.post("/memory/{memory_id}/edit", name="memory_edit")
async def memory_edit(
    memory_id: str,
    content: str = Form(...),
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    user: Optional[str] = Form(None),
    agent_kind: str = Form(_DEFAULT_AGENT_KIND),
) -> Response:
    agent_kind = _resolve_agent_kind(agent_kind)
    client = client_for(env)
    existing = (
        client.table("ai_user_memory").select("content").eq("id", memory_id).limit(1)
        .execute().data or []
    )
    before = {"content": existing[0]["content"]} if existing else None
    embedding = await asyncio.to_thread(embed_text, content)
    patch = {"content": content}
    if embedding is not None:
        from core.memory.embeddings import to_vector_literal

        patch["embedding"] = to_vector_literal(embedding)
    client.table("ai_user_memory").update(patch).eq("id", memory_id).execute()
    record_change(
        entity_type="ai_user_memory", entity_id=memory_id, action="edit",
        actor_user_id=ctx["user_id"], before=before, after={"content": content},
    )
    url = f"/admin/memory?tab=memories&agent_kind={agent_kind}&env={env}" + (f"&user={user}" if user else "")
    return RedirectResponse(url=url, status_code=302)


@router.post("/memory/add", name="memory_add")
async def memory_add(
    user_id: str = Form(...),
    business_id: str = Form(...),
    content: str = Form(...),
    agent_kind: str = Form(_DEFAULT_AGENT_KIND),
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    agent_kind = _resolve_agent_kind(agent_kind)
    client = client_for(env)
    embedding = await asyncio.to_thread(embed_text, content)
    row = {
        "user_id": user_id, "business_id": business_id, "agent_kind": agent_kind,
        "content": content, "source": "manual",
    }
    if embedding is not None:
        from core.memory.embeddings import to_vector_literal

        row["embedding"] = to_vector_literal(embedding)
    result = client.table("ai_user_memory").insert(row).execute()
    new_id = result.data[0]["id"] if result.data else user_id
    record_change(
        entity_type="ai_user_memory", entity_id=new_id, action="manual_add",
        actor_user_id=ctx["user_id"], after={"content": content},
    )
    return RedirectResponse(
        url=f"/admin/memory?tab=memories&agent_kind={agent_kind}&user={user_id}&env={env}",
        status_code=302,
    )
