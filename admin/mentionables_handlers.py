"""GET /admin/api/profiles/{id}/mentionables — feeds the prompt editor's @-dropdown.

Returns the tools/MCPs/KBs available to a profile (from its MCP attachments +
data sources) as {tools, mcps, kbs} lists of {name, label}. Read-only, superuser-
gated. Scoped to the profile's own attachments — no org-wide catalog leak.

The tools list is filtered through ``bound_tool_names_from_attachments`` — the
same subset resolution the runtime MCP resolver and the promote gate's
prompt-tool guard apply — NOT every member tool of every attached MCP. Listing
members that the attachment's ``tool_subset`` excludes offered the operator
tools that would never bind, and the promote gate then blocked the profile
with ``prompt_tool_mismatch`` for mentioning exactly what this dropdown
suggested.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from admin.auth import require_superuser
from admin.router import router, _safe_list
from core.agent_profile import _fetch_mcp_attachments
from core.auth import admin_service_client

logger = logging.getLogger(__name__)


@router.get("/api/profiles/{profile_id}/mentionables")
async def profile_mentionables(
    profile_id: str, request: Request, ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    client = admin_service_client()
    prof = _safe_list(
        client.table("agent_profiles").select("profile_id, data_source_ids")
        .eq("profile_id", profile_id).limit(1).execute()
    )
    if not prof:
        raise HTTPException(404, "profile not found")

    tools: Dict[str, None] = {}
    mcps: List[Dict[str, str]] = []
    try:
        # Function-level import (same as admin/services/promotion.py) so the
        # admin package doesn't pull the runtime tool stack at module load.
        from runtime.tools._mcp_resolver import bound_tool_names_from_attachments

        attachments = _fetch_mcp_attachments(client, profile_id)
        bound_names = bound_tool_names_from_attachments(attachments)
        for att in attachments:
            server = att.get("mcp_server") or {}
            if server.get("name"):
                mcps.append({"name": server["name"], "label": server["name"]})
            for m in att.get("member_tools") or []:
                name = (m.get("tool") or {}).get("python_name")
                # Only tools that will actually BIND (attachment tool_subset
                # applied) — a member outside the subset never reaches the LLM
                # and mentioning it trips the promote gate.
                if name and name in bound_names:
                    tools.setdefault(name, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mentionables: attachment lookup failed for %s (%s)", profile_id, exc)

    kbs: List[Dict[str, str]] = []
    ds_ids = prof[0].get("data_source_ids") or []
    if ds_ids:
        try:
            ds_rows = _safe_list(
                client.table("agent_data_sources").select("source_id, name")
                .in_("source_id", ds_ids).execute()
            )
            for r in ds_rows:
                if r.get("name"):
                    kbs.append({"name": r["name"], "label": r["name"]})
        except Exception as exc:  # noqa: BLE001
            logger.warning("mentionables: data-source lookup failed for %s (%s)", profile_id, exc)

    return JSONResponse({
        "tools": [{"name": n, "label": n} for n in tools],
        "mcps": mcps,
        "kbs": kbs,
    })
