"""Skills screens — `/admin/skills*`.

Operator CRUD + versioning for agent skills (platform AND tenant rows;
tenant self-serve authoring is Phase 3 — until then operators manage
tenant skills here too). Sidecar to admin/router.py (imported at its
bottom, like mcp_handlers.py). Every write: validation → audit
(`record_change`) → cache invalidation (`invalidate_skills_cache` +
`AgentProfileLoader.invalidate`).
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import router, _safe_list, templates
from core.agent_profile import AgentProfileLoader
from core.auth import admin_service_client
from core.skills import (
    MAX_BODY_BYTES,
    MAX_RESOURCE_BYTES,
    invalidate_skills_cache,
    validate_skill_metadata,
)

logger = logging.getLogger(__name__)


def _err(msg: str, back: str = "/admin/skills") -> Response:
    return RedirectResponse(url=f"{back}?error={quote(msg)}", status_code=302)


def _validate_body(content: str) -> Optional[str]:
    if not (content or "").strip():
        return "content is required"
    if len(content.encode("utf-8")) > MAX_BODY_BYTES:
        return f"content exceeds {MAX_BODY_BYTES // 1024} KB"
    return None


def _skill_agents(client, skills) -> dict:
    """skill_id -> the agents it applies to, as picker values
    (admin.agent_kinds). A product agent's skill applies by its kind. Every
    generic agent shares the kind ``custom``, so a custom skill applies to
    the generic agents it is attached to (agent_profile_skills), by name."""
    from admin.agent_kinds import SLUG_ADDRESSED_KIND, agent_choice

    custom_ids = [sk["skill_id"] for sk in skills
                  if sk.get("agent_kind") == SLUG_ADDRESSED_KIND and sk.get("skill_id")]
    by_skill: dict = {}
    if custom_ids:
        try:
            atts = _safe_list(client.table("agent_profile_skills")
                              .select("profile_id, skill_id")
                              .in_("skill_id", custom_ids).execute())
            pids = sorted({a["profile_id"] for a in atts if a.get("profile_id")})
            profs = {p["profile_id"]: p for p in _safe_list(
                client.table("agent_profiles").select("profile_id, agent_kind, slug, status")
                .in_("profile_id", pids).execute())} if pids else {}
        except Exception as exc:  # noqa: BLE001 - names are decoration
            logger.info("skills: attachment names unavailable (%s)", exc)
            atts, profs = [], {}
        for a in atts:
            p = profs.get(a.get("profile_id")) or {}
            if not p.get("slug") or p.get("status") == "template":
                continue
            v = agent_choice(p.get("agent_kind") or "", p["slug"])
            lst = by_skill.setdefault(a["skill_id"], [])
            if v not in lst:
                lst.append(v)
    out = {}
    for sk in skills:
        if sk.get("agent_kind") == SLUG_ADDRESSED_KIND:
            out[sk["skill_id"]] = by_skill.get(sk["skill_id"], [])
        else:
            out[sk["skill_id"]] = [sk["agent_kind"]] if sk.get("agent_kind") else []
    return out


def _attach_skill_to_agent(client, skill_id: str, slug: str) -> int:
    """Attach a platform skill to the generic agent ``slug`` — every one of
    its non-template rows (staging and production), at the end of each
    profile's skill list. Returns how many profiles it was attached to."""
    from admin.agent_kinds import SLUG_ADDRESSED_KIND

    profiles = _safe_list(
        client.table("agent_profiles").select("profile_id, environment, status")
        .eq("agent_kind", SLUG_ADDRESSED_KIND).eq("slug", slug).execute())
    n = 0
    for p in profiles:
        if p.get("status") == "template":
            continue
        current = _safe_list(client.table("agent_profile_skills").select("skill_id, position")
                             .eq("profile_id", p["profile_id"]).execute())
        if any(r.get("skill_id") == skill_id for r in current):
            continue
        position = max([r.get("position") or 0 for r in current] or [-1]) + 1
        client.table("agent_profile_skills").insert({
            "profile_id": p["profile_id"], "skill_id": skill_id, "position": position,
        }).execute()
        AgentProfileLoader.invalidate(SLUG_ADDRESSED_KIND, p.get("environment"))
        n += 1
    return n


@router.get("/skills", name="skills_list")
async def skills_list(request: Request, ctx: dict = Depends(require_superuser)):
    client = admin_service_client()
    skills = _safe_list(
        client.table("agent_skills").select("*")
        .order("created_at", desc=True).execute()
    )
    from admin.agent_kinds import build_agent_choices, scope_to_build

    # Same shared agent list the other Build screens use, one entry per
    # agent (each generic agent by name); a partner build lists and offers
    # only its own agents' skills.
    kinds = list(build_agent_choices())
    skills = scope_to_build(skills, also=(), keep_unkinded=True)
    agents_by_skill = _skill_agents(client, skills)
    for sk in skills:
        sk["agents"] = agents_by_skill.get(sk.get("skill_id"), [])
    return templates.TemplateResponse(request, "skills.html", {
        "request": request, "active": "skills", "skills": skills,
        "ctx": ctx,
        "agent_kinds": kinds,
        "error": request.query_params.get("error"),
        "superuser": ctx,
    })


@router.post("/skills/create", name="skills_create")
async def skills_create(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    form = await request.form()
    slug = (form.get("slug") or "").strip()
    display_name = (form.get("display_name") or "").strip()
    description = (form.get("description") or "").strip()
    agent_kind = (form.get("agent_kind") or "").strip()
    business_id = (form.get("business_id") or "").strip() or None
    content = form.get("content") or ""

    msg = validate_skill_metadata(slug, display_name, description)
    if msg is None:
        msg = _validate_body(content)
    if msg is None and not agent_kind:
        msg = "agent_kind is required"
    # A generic agent is picked by name (`custom:<slug>`): the skill is of
    # the kind they share, and attached to that agent so it applies to it.
    from admin.agent_kinds import split_agent_choice

    agent_kind, agent_slug = split_agent_choice(agent_kind)
    if msg is None and agent_slug and business_id:
        msg = ("a skill for one business applies to every agent of its kind; "
               "pick the agent without a business, or leave the business empty")
    if msg:
        return _err(msg)

    client = admin_service_client()
    inserted = client.table("agent_skills").insert({
        "slug": slug, "display_name": display_name, "description": description,
        "agent_kind": agent_kind, "business_id": business_id,
        "always_on": form.get("always_on") == "on",
        "created_by": ctx.get("user_id"), "updated_by": ctx.get("user_id"),
    }).execute()
    if not inserted.data:
        return _err("insert failed")
    skill = inserted.data[0]
    client.table("agent_skill_versions").insert({
        "skill_id": skill["skill_id"], "version_number": 1, "content": content,
        "notes": "initial version", "created_by": ctx.get("user_id"),
        "updated_by": ctx.get("user_id"),
    }).execute()
    attached = _attach_skill_to_agent(client, skill["skill_id"], agent_slug) if agent_slug else 0
    record_change(entity_type="agent_skills", entity_id=skill["skill_id"],
                  action="create", actor_user_id=ctx.get("user_id"),
                  before=None, after={"slug": slug, "agent_kind": agent_kind,
                                      **({"attached_to": agent_slug,
                                          "profiles": attached} if agent_slug else {})})
    invalidate_skills_cache()
    AgentProfileLoader.invalidate()
    return RedirectResponse(url=f"/admin/skills/{skill['skill_id']}", status_code=302)


@router.get("/skills/{skill_id}", name="skill_detail")
async def skill_detail(skill_id: str, request: Request,
                       ctx: dict = Depends(require_superuser)):
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_skills").select("*").eq("skill_id", skill_id)
        .limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "skill not found")
    versions = _safe_list(
        client.table("agent_skill_versions").select("*").eq("skill_id", skill_id)
        .order("version_number", desc=True).execute()
    )
    latest = versions[0] if versions else None
    resources = _safe_list(
        client.table("agent_skill_resources").select("*")
        .eq("skill_version_id", latest["skill_version_id"]).order("position").execute()
    ) if latest else []
    data_sources = _safe_list(
        client.table("agent_data_sources").select("source_id, name")
        .order("name", desc=False).execute()
    )
    skill_agents = _skill_agents(client, rows).get(rows[0].get("skill_id"), [])
    return templates.TemplateResponse(request, "skill_detail.html", {
        "request": request, "active": "skills", "skill": rows[0],
        "skill_agents": skill_agents,
        "versions": versions, "latest": latest, "resources": resources,
        "data_sources": data_sources,
        "error": request.query_params.get("error"), "superuser": ctx, "ctx": ctx,
    })


@router.post("/skills/{skill_id}/update", name="skill_update")
async def skill_update(skill_id: str, request: Request,
                       ctx: dict = Depends(require_superuser)) -> Response:
    form = await request.form()
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_skills").select("*").eq("skill_id", skill_id)
        .limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "skill not found")
    before = rows[0]
    display_name = (form.get("display_name") or "").strip()
    description = (form.get("description") or "").strip()
    msg = validate_skill_metadata(before["slug"], display_name, description)
    if msg:
        return _err(msg, back=f"/admin/skills/{skill_id}")
    linked = [s for s in form.getlist("linked_data_source_ids") if s]
    client.table("agent_skills").update({
        "display_name": display_name, "description": description,
        "always_on": form.get("always_on") == "on",
        "linked_data_source_ids": linked,
        "updated_by": ctx.get("user_id"),
    }).eq("skill_id", skill_id).execute()
    record_change(entity_type="agent_skills", entity_id=skill_id, action="update",
                  actor_user_id=ctx.get("user_id"),
                  before={"display_name": before.get("display_name"),
                          "always_on": before.get("always_on")},
                  after={"display_name": display_name,
                         "always_on": form.get("always_on") == "on"})
    invalidate_skills_cache()
    AgentProfileLoader.invalidate()
    return RedirectResponse(url=f"/admin/skills/{skill_id}", status_code=302)


@router.post("/skills/{skill_id}/versions/create", name="skill_version_create")
async def skill_version_create(skill_id: str, request: Request,
                               ctx: dict = Depends(require_superuser)) -> Response:
    form = await request.form()
    content = form.get("content") or ""
    msg = _validate_body(content)
    if msg:
        return _err(msg, back=f"/admin/skills/{skill_id}")
    client = admin_service_client()
    existing = _safe_list(
        client.table("agent_skill_versions").select("version_number")
        .eq("skill_id", skill_id).order("version_number", desc=True)
        .limit(1).execute()
    )
    next_num = (existing[0]["version_number"] + 1) if existing else 1
    client.table("agent_skill_versions").insert({
        "skill_id": skill_id, "version_number": next_num, "content": content,
        "notes": (form.get("notes") or "").strip() or None,
        "created_by": ctx.get("user_id"), "updated_by": ctx.get("user_id"),
    }).execute()
    record_change(entity_type="agent_skill_versions", entity_id=skill_id,
                  action="create", actor_user_id=ctx.get("user_id"),
                  before=None, after={"version_number": next_num})
    invalidate_skills_cache()
    AgentProfileLoader.invalidate()
    return RedirectResponse(url=f"/admin/skills/{skill_id}", status_code=302)


@router.post("/skills/{skill_id}/versions/{version_id}/resources/create",
             name="skill_resource_create")
async def skill_resource_create(skill_id: str, version_id: str, request: Request,
                                ctx: dict = Depends(require_superuser)) -> Response:
    form = await request.form()
    filename = (form.get("filename") or "").strip()
    content = form.get("content") or ""
    if not filename:
        return _err("filename is required", back=f"/admin/skills/{skill_id}")
    if len(content.encode("utf-8")) > MAX_RESOURCE_BYTES:
        return _err(f"resource exceeds {MAX_RESOURCE_BYTES // 1024} KB",
                    back=f"/admin/skills/{skill_id}")
    client = admin_service_client()
    # Idempotent per (version, filename): replace an existing file of the
    # same name rather than erroring on the unique constraint.
    client.table("agent_skill_resources").delete().eq(
        "skill_version_id", version_id).eq("filename", filename).execute()
    client.table("agent_skill_resources").insert({
        "skill_version_id": version_id, "filename": filename,
        "content_type": (form.get("content_type") or "text/markdown").strip(),
        "content": content, "position": int(form.get("position") or 0),
        "created_by": ctx.get("user_id"), "updated_by": ctx.get("user_id"),
    }).execute()
    record_change(entity_type="agent_skill_resources", entity_id=skill_id,
                  action="create", actor_user_id=ctx.get("user_id"),
                  before=None, after={"filename": filename})
    invalidate_skills_cache()
    AgentProfileLoader.invalidate()
    return RedirectResponse(url=f"/admin/skills/{skill_id}", status_code=302)


@router.post("/skills/{skill_id}/resources/{resource_id}/delete",
             name="skill_resource_delete")
async def skill_resource_delete(skill_id: str, resource_id: str,
                                ctx: dict = Depends(require_superuser)) -> Response:
    client = admin_service_client()
    client.table("agent_skill_resources").delete().eq(
        "resource_id", resource_id).execute()
    record_change(entity_type="agent_skill_resources", entity_id=skill_id,
                  action="delete", actor_user_id=ctx.get("user_id"),
                  before={"resource_id": resource_id}, after=None)
    invalidate_skills_cache()
    AgentProfileLoader.invalidate()
    return RedirectResponse(url=f"/admin/skills/{skill_id}", status_code=302)


@router.post("/skills/{skill_id}/archive", name="skill_archive")
async def skill_archive(skill_id: str,
                        ctx: dict = Depends(require_superuser)) -> Response:
    client = admin_service_client()
    client.table("agent_skills").update({
        "status": "archived", "is_active": False, "updated_by": ctx.get("user_id"),
    }).eq("skill_id", skill_id).execute()
    record_change(entity_type="agent_skills", entity_id=skill_id, action="archive",
                  actor_user_id=ctx.get("user_id"), before=None,
                  after={"status": "archived"})
    invalidate_skills_cache()
    AgentProfileLoader.invalidate()
    return RedirectResponse(url="/admin/skills", status_code=302)
