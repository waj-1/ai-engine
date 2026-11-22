"""Profile↔MCP attachment editor — `POST /admin/profiles/{id}/mcp-attachments`.

Reconciles the ``agent_profile_mcp_servers`` join (the table the MCP resolver
reads via ``AgentProfileLoader`` — i.e. THE runtime tool binding) from the
Agents-screen editor. Each attached MCP may pin a ``tool_subset`` (a subset of
the MCP's own tools; empty = all). Additive: this does NOT touch the legacy
``agent_profiles.mcp_server_ids`` array (managed by the mapping form in
``profile_update``), which still drives a client endpoint helper + the
compiler snapshot during dark-launch coexistence.

The subset arrives in one of two wire shapes, parsed by ``_parse_subset`` into
the identical ``desired[mid]`` value (same validation, same reconcile, same
join rows):

  * checkbox variant (the Agents screen's per-server member-tool picker for
    bundle MCPs): ``tool_subset_mode__{mid}=list`` marks the variant present;
    ``tool_subset_all__{mid}`` truthy → NULL (all tools, follow the bundle);
    otherwise the checked ``tool_subset_cb__{mid}`` values are the subset
    (none checked → NULL, matching the blank-text-field contract).
  * comma-separated text field ``tool_subset__{mid}`` — the original contract,
    kept as the fallback for external MCPs and every scripted POST.

Two guards, both applied BEFORE any destructive write:
  * scoped-build attach-ability — on a scoped build an operator may only
    attach MCPs visible to the active scope (the same set the picker shows),
    so a hand-crafted POST can't wire a client agent to a WAJ-only bundle
    (attachments are runtime-consumed → a real cross-tenant capability leak).
  * tool_subset validity — a pinned name must be one the MCP actually exposes.

Sidecar to admin/router.py (attached at its bottom, like mcp_handlers.py).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import router, _safe_list
from core.agent_profile import AgentProfileLoader
from core.auth import admin_service_client

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


def _parse_subset(form, mid: str) -> Optional[List[str]]:
    """One attachment's desired ``tool_subset`` from either wire variant.

    ``None`` always means "all tools" (stored as NULL) — in the checkbox
    variant via the "All tools" master OR an empty selection, in the text
    variant via a blank field. See the module docstring for the two shapes;
    keep them reconciling identically or a scripted POST and a portal save
    stop meaning the same thing.
    """
    if form.get(f"tool_subset_mode__{mid}") == "list":
        if form.get(f"tool_subset_all__{mid}"):
            return None
        seen: set = set()
        subset: List[str] = []
        for v in form.getlist(f"tool_subset_cb__{mid}"):
            v = (v or "").strip()
            if v and v not in seen:
                seen.add(v)
                subset.append(v)
        return subset or None
    raw = (form.get(f"tool_subset__{mid}") or "").strip()
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()] or None


@router.post("/profiles/{profile_id}/mcp-attachments", name="profile_mcp_attachments_update")
async def profile_mcp_attachments_update(
    profile_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()

    # Dedupe posted MCP ids, preserving attach order (drives `position`).
    attach_ids: List[str] = []
    seen: set = set()
    for mid in form.getlist("attach_mcp_ids"):
        if mid and mid not in seen:
            seen.add(mid)
            attach_ids.append(mid)

    client = admin_service_client()
    prof_rows = _safe_list(
        client.table("agent_profiles")
        .select("profile_id, agent_kind, environment")
        .eq("profile_id", profile_id).limit(1).execute()
    )
    if not prof_rows:
        raise HTTPException(404, "profile not found")
    profile = prof_rows[0]

    # Attachable set (+ each MCP's real tool names) — reused for BOTH the
    # scoped-build attach-ability guard and the tool_subset validation. On a
    # scoped build we pass the client's visible profiles so the set is
    # restricted exactly as the picker is; on a WAJ build it's unrestricted.
    from admin.screens.agents import _attachable_mcps
    from core.deployment import scoped_build

    visible_profiles: Optional[List[Dict[str, Any]]] = None
    if scoped_build():
        from admin.seed_agent_profiles import visible_agent_kinds

        _visible_kinds = set(visible_agent_kinds())
        # select("*") — _attachable_mcps unions each visible profile's legacy
        # `mcp_server_ids` array into the allow-set, so that column must be
        # present or the array half silently no-ops (would falsely refuse
        # attaching an MCP wired to a visible profile only via the legacy array
        # during dark-launch coexistence). Wrapped: a lookup failure fails
        # CLOSED (empty visible set → every attach refused), never a 500.
        try:
            all_profiles = _safe_list(
                client.table("agent_profiles").select("*").execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp-attachments: visible-profile lookup failed (%s)", exc)
            all_profiles = []
        visible_profiles = [p for p in all_profiles if p.get("agent_kind") in _visible_kinds]

    attachable = _attachable_mcps(client, visible_profiles=visible_profiles)
    attachable_by_id: Dict[str, Dict[str, Any]] = {a["mcp_server_id"]: a for a in attachable}

    # Guard 1 — attach-ability. Refuse a posted MCP that isn't attachable
    # (unknown, inactive, or hidden on this scoped build). Before any write.
    for mid in attach_ids:
        if mid not in attachable_by_id:
            return _err(profile_id, "One or more selected MCPs are not available to attach.")

    # Guard 2 — tool_subset validity. A pinned name must be one the MCP
    # exposes — enforced on the PARSED subset regardless of which wire variant
    # carried it, so a forged checkbox value is refused exactly like a forged
    # comma-string token.
    desired: Dict[str, Optional[List[str]]] = {}
    for mid in attach_ids:
        subset = _parse_subset(form, mid)
        if subset:
            from admin.mcp_membership import subset_names_for
            valid, why = subset_names_for(attachable_by_id[mid])
            if valid is None:
                return _err(profile_id, f"{why}.")
            bad = [s for s in subset if s not in valid]
            if bad:
                name = attachable_by_id[mid].get("name") or mid
                return _err(profile_id, f"{name}: unknown tool(s) {', '.join(bad)}.")
        desired[mid] = subset

    # Reconcile (validation passed → safe to mutate). Upsert-then-delete-stale
    # via the shared helper: the join is never emptied mid-write, so a crash
    # can't silently strip a live agent of its tools (see
    # admin/mcp_membership.py). Returns the prior ids for the audit before.
    from admin.mcp_membership import reconcile_profile_mcp_attachments

    before_ids = set(reconcile_profile_mcp_attachments(
        client, profile_id, list(desired.items())
    ))

    # Attachments feed the resolved profile — invalidate its cache so a live
    # (staging) profile reflects the change on the next request.
    AgentProfileLoader.invalidate(profile.get("agent_kind"), profile.get("environment"))
    record_change(
        entity_type="agent_profile_mcp_servers",
        entity_id=profile_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before={"attachments": sorted(before_ids)},
        after={"attachments": sorted(desired.keys())},
    )
    # Land back on the profile that was just edited, in that profile's own
    # environment (the row is already in hand) — a bare /admin/agents
    # re-picks the first active profile.
    return RedirectResponse(
        url=f"/admin/agents?profile_id={quote(profile_id, safe='')}"
            f"&env={profile['environment']}",
        status_code=302,
    )
