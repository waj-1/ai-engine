# ai_agent/admin/memory_handlers.py
"""Admin /memory UI — sidecar route module.

    GET  /admin/memory                     — tabs: memories | config | inspector
    POST /admin/memory/config              — save memory_config on the business profile
    POST /admin/memory/{memory_id}/delete  — soft-archive a memory
    POST /admin/memory/{memory_id}/pin     — pin / unpin
    POST /admin/memory/{memory_id}/edit    — edit content (re-embeds on save)
    POST /admin/memory/add                 — manual memory insert
    POST /admin/memory/{memory_id}/assign  — move an unassigned custom-kind
                                             memory to one generic agent

Pattern matches admin/jobs_handlers.py: import the shared `router` and decorate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import client_for, current_env, router, templates
from admin.screens import memory as memory_screen
from admin.seed_agent_profiles import visible_agent_kinds
from core.agent_profile import AgentProfileLoader
from core.deployment import scoped_build
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


def _picker_agent_kinds(current: str, env: Optional[str] = None) -> list:
    """Agent picker options: the shared live list without system agents
    (title / suggestion generators and the like), one entry per agent — each
    generic agent by name (``custom:<slug>``), since they all share one kind
    — plus the current value if a deep link selected one of those."""
    from admin.agent_kinds import build_agent_choices

    kinds = list(build_agent_choices(env=env))
    if current and current not in kinds:
        kinds.append(current)
    if any(k.startswith(_GENERIC_PREFIX) for k in kinds):
        # Generic agents are listed one by one: the bare kind names none.
        kinds = [k for k in kinds if k != _GENERIC_KIND]
    return kinds


_GENERIC_KIND = "custom"
_GENERIC_PREFIX = _GENERIC_KIND + ":"


def _open_on_agent(agent_kind: str, choices: list) -> str:
    """The agent a memory screen opens on. The bare generic kind names no
    agent (every generic agent shares it, and each keeps its own memories
    and settings), so it opens on the first generic agent of the picker
    instead (else its first agent): a build with no plugin opens on its first
    agent rather than on "Custom agent". A product kind is kept."""
    if agent_kind != _GENERIC_KIND:
        return agent_kind
    return (next((c for c in choices if c.startswith(_GENERIC_PREFIX)), None)
            or next((c for c in choices if c != _GENERIC_KIND), agent_kind))


def _resolve_agent_kind(agent_kind: str) -> str:
    """Validate agent_kind against the visible kinds; fall back to the
    default visible kind if invalid (e.g. a full-build kind on a scoped build).
    A generic agent's value (``custom:<slug>``) is kept when that agent exists."""
    from admin.agent_kinds import custom_agent_rows, split_agent_choice

    kind, slug = split_agent_choice(agent_kind)
    if slug and not scoped_build() and any(r.get("slug") == slug for r in custom_agent_rows()):
        return agent_kind
    if not slug and agent_kind in _valid_agent_kinds():
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
    choices = _picker_agent_kinds(agent_kind, env)
    agent_kind = _open_on_agent(agent_kind, choices)
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
            "agent_kinds": choices,
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
    choices = _picker_agent_kinds(agent_kind, env)
    agent_kind = _open_on_agent(agent_kind, choices)
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
            "agent_kinds": choices,
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
        client, agent_kind, env, select="profile_id, agent_kind, memory_config")
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
        AgentProfileLoader.invalidate(profile.get("agent_kind") or agent_kind, env)
    return RedirectResponse(
        url=f"/admin/agents/memory?agent_kind={quote(agent_kind)}&env={env}&saved=1", status_code=302
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


@router.post("/memory/{memory_id}/assign", name="memory_assign")
async def memory_assign(
    memory_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    user: Optional[str] = Form(None),
    agent_kind: str = Form(...),
) -> Response:
    """Give a custom-kind memory that belongs to no agent (saved before
    memories were kept per agent) to the generic agent being viewed. Only
    such a row moves: an agent's own memories are never re-assigned here."""
    from admin.agent_kinds import split_agent_choice

    agent_kind = _resolve_agent_kind(agent_kind)
    kind, slug = split_agent_choice(agent_kind)
    if slug:
        client = client_for(env)
        client.table("ai_user_memory").update({"agent_slug": slug}).eq("id", memory_id) \
            .eq("agent_kind", kind).is_("agent_slug", "null").execute()
        record_change(
            entity_type="ai_user_memory", entity_id=memory_id, action="assign",
            actor_user_id=ctx["user_id"], after={"agent_slug": slug},
        )
    url = (f"/admin/memory?tab=memories&agent_kind={quote(agent_kind)}&env={env}"
           + (f"&user={quote(user)}" if user else ""))
    return RedirectResponse(url=url, status_code=302)


def memory_row(*, user_id: str, agent_kind: str, content: str,
               business_id: Optional[str] = None, source: str = "manual") -> dict:
    """The ai_user_memory row for a hand-added memory. A generic agent's
    (``custom:<slug>``) is saved under its slug, so no other generic agent
    reads it; a product kind's is saved by kind, as before. ``business_id``
    is kept when given (a build whose plugin has businesses)."""
    from admin.agent_kinds import split_agent_choice

    kind, slug = split_agent_choice(agent_kind)
    row = {"user_id": user_id, "agent_kind": kind, "content": content, "source": source}
    if business_id:
        row["business_id"] = business_id
    if slug:
        row["agent_slug"] = slug
    return row


@router.post("/memory/add", name="memory_add")
async def memory_add(
    user_id: str = Form(...),
    content: str = Form(...),
    agent_kind: str = Form(_DEFAULT_AGENT_KIND),
    business_id: Optional[str] = Form(None),
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    from admin.agent_kinds import split_agent_choice

    agent_kind = _resolve_agent_kind(agent_kind)
    kind, slug = split_agent_choice(agent_kind)
    if kind == "custom" and not slug:
        # The bare kind names no agent: a row saved here would belong to none.
        return RedirectResponse(
            url=f"/admin/memory?tab=memories&agent_kind={quote(agent_kind)}&user={user_id}&env={env}",
            status_code=302,
        )
    resolved = memory_screen.resolve_user_id(user_id)
    if not resolved:
        # Not a uuid and no user with that email: nothing to save it to.
        return RedirectResponse(
            url=f"/admin/memory?tab=memories&agent_kind={quote(agent_kind)}&user={quote(user_id)}&env={env}",
            status_code=302,
        )
    user_id = resolved
    client = client_for(env)
    embedding = await asyncio.to_thread(embed_text, content)
    row = memory_row(user_id=user_id, agent_kind=agent_kind, content=content,
                     business_id=(business_id or "").strip() or None)
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
        url=f"/admin/memory?tab=memories&agent_kind={quote(agent_kind)}&user={user_id}&env={env}",
        status_code=302,
    )
