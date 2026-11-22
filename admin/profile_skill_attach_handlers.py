"""Profile↔skill attachment editor — `POST /admin/profiles/{id}/skill-attachments`.

Reconciles the ``agent_profile_skills`` join from the Agents-screen editor.
Guards, applied BEFORE any destructive write (mirrors
profile_mcp_attach_handlers.py):
  * only PLATFORM skills (business_id IS NULL) are attachable — tenant skills
    resolve dynamically and must never be pinned to a profile;
  * agent_kind must match the profile's kind (one agent's skill can't attach
    to another kind of agent);
  * scoped-build attach-ability — on a scoped build only skills whose
    agent_kind is visible to the active scope may be attached.

Sidecar to admin/router.py (attached at its bottom).
"""
from __future__ import annotations

import logging
from typing import List
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import router, _safe_list
from core.agent_profile import AgentProfileLoader
from core.auth import admin_service_client
from core.skills import invalidate_skills_cache

logger = logging.getLogger(__name__)


def _err(profile_id: str, msg: str) -> Response:
    """Redirect back to the edited profile with a surfaced error, no write.

    ``profile_id`` is caller input (a path segment can legally contain `&`),
    so it is quoted like ``msg`` — one query value, no smuggled params.
    """
    return RedirectResponse(
        url=f"/admin/agents?profile_id={quote(profile_id, safe='')}"
            f"&error={quote(msg)}",
        status_code=302,
    )


@router.post("/profiles/{profile_id}/skill-attachments",
             name="profile_skill_attachments_update")
async def profile_skill_attachments_update(
    profile_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    attach_ids: List[str] = []
    seen: set = set()
    for sid in form.getlist("attach_skill_ids"):
        if sid and sid not in seen:
            seen.add(sid)
            attach_ids.append(sid)

    client = admin_service_client()
    prof_rows = _safe_list(
        client.table("agent_profiles")
        .select("profile_id, agent_kind, environment")
        .eq("profile_id", profile_id).limit(1).execute()
    )
    if not prof_rows:
        raise HTTPException(404, "profile not found")
    profile = prof_rows[0]

    # Attachable set: active platform skills of this profile's kind, filtered
    # by scoped-build visibility. Fail CLOSED on lookup failure.
    from core.deployment import scoped_build

    kind_ok = {profile["agent_kind"]}
    if scoped_build():
        from admin.seed_agent_profiles import visible_agent_kinds
        kind_ok &= set(visible_agent_kinds())

    try:
        skill_rows = _safe_list(
            client.table("agent_skills").select("*")
            .eq("is_active", True).eq("status", "active").execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill-attachments: skill lookup failed (%s)", exc)
        skill_rows = []
    attachable = {
        r["skill_id"]: r for r in skill_rows
        if r.get("business_id") is None and r.get("agent_kind") in kind_ok
    }

    for sid in attach_ids:
        if sid not in attachable:
            return _err(profile_id,
                        "One or more selected skills are not available to attach.")

    before_rows = _safe_list(
        client.table("agent_profile_skills").select("*")
        .eq("profile_id", profile_id).execute()
    )
    before_ids = {r["skill_id"] for r in before_rows}

    # Detach skills no longer desired.
    for r in before_rows:
        if r["skill_id"] not in attach_ids:
            client.table("agent_profile_skills").delete().eq(
                "profile_id", profile_id).eq("skill_id", r["skill_id"]).execute()

    # (Re-)attach in submitted order — position is the catalog order.
    for pos, sid in enumerate(attach_ids):
        client.table("agent_profile_skills").delete().eq(
            "profile_id", profile_id).eq("skill_id", sid).execute()
        client.table("agent_profile_skills").insert({
            "profile_id": profile_id, "skill_id": sid, "position": pos,
        }).execute()

    AgentProfileLoader.invalidate(profile.get("agent_kind"), profile.get("environment"))
    invalidate_skills_cache()
    record_change(
        entity_type="agent_profile_skills", entity_id=profile_id, action="update",
        actor_user_id=ctx["user_id"],
        before={"attachments": sorted(before_ids)},
        after={"attachments": attach_ids},
    )
    # Land back on the profile that was just edited, in that profile's own
    # environment (the row is already in hand) — a bare /admin/agents
    # re-picks the first active profile.
    return RedirectResponse(
        url=f"/admin/agents?profile_id={quote(profile_id, safe='')}"
            f"&env={profile['environment']}",
        status_code=302,
    )
