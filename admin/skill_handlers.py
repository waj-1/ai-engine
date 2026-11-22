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


@router.get("/skills", name="skills_list")
async def skills_list(request: Request, ctx: dict = Depends(require_superuser)):
    client = admin_service_client()
    skills = _safe_list(
        client.table("agent_skills").select("*")
        .order("created_at", desc=True).execute()
    )
    from admin.agent_kinds import build_agent_kinds, scope_to_build

    # Same shared agent list the other Build screens use; a partner build
    # lists and offers only its own agents' skills.
    kinds = list(build_agent_kinds())
    skills = scope_to_build(skills, also=(), keep_unkinded=True)
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
    record_change(entity_type="agent_skills", entity_id=skill["skill_id"],
                  action="create", actor_user_id=ctx.get("user_id"),
                  before=None, after={"slug": slug, "agent_kind": agent_kind})
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
    return templates.TemplateResponse(request, "skill_detail.html", {
        "request": request, "active": "skills", "skill": rows[0],
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
