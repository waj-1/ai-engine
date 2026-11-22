"""Editable model detail + env-scoped promote handler.

Mounted onto `admin.router.router` by importing this module from
`admin/router.py` (see the import block at the bottom of that file).

`agent_models.environment` (added in 20260519200000) makes each row
env-scoped. Promote copies the row into a new (name, target_env) tuple
so the same logical model can carry per-env tuning knobs and the
runtime resolver can pick the right one for the active environment.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin._model_picker import is_realtime_model
from admin.router import (
    _MODEL_EXTRA_CONFIG_KEYS,
    _MODEL_PROVIDERS,
    _safe_list,
    client_for,
    current_env,
    router,
    templates,
)
from core.auth import get_service_client, admin_service_client

logger = logging.getLogger(__name__)


@router.get("/agent-models/{model_id}", response_class=HTMLResponse)
async def model_detail(
    request: Request,
    model_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    client = client_for(env)
    rows = _safe_list(
        client.table("agent_models").select("*").eq("model_id", model_id).execute()
    )
    if not rows:
        raise HTTPException(404, "model not found")
    row = rows[0]
    # Siblings = every row sharing the same name across envs.
    siblings = _safe_list(
        client.table("agent_models")
        .select("model_id, name, environment, is_active")
        .eq("name", row.get("name"))
        .order("environment")
        .execute()
    )
    related = [s for s in siblings if s.get("model_id") != model_id]
    context = {
        "ctx": ctx,
        "active": "agents",
        "current_env": env,
        "row": row,
        "extra_config": row.get("extra_config") or {},
        # The Automatic row (core.model_resolver) keeps its own provider in the
        # select; without it, saving the form re-typed it as the first option.
        "providers": _MODEL_PROVIDERS + (("auto",) if row.get("provider") == "auto" else ()),
        "extra_config_keys": _MODEL_EXTRA_CONFIG_KEYS,
        "related": related,
        "target_envs": ("staging", "prod"),
        "is_realtime": is_realtime_model(row),
        "error": request.query_params.get("error"),
    }
    # Voice budgets are per tenant: they moved to Settings › Voice budgets
    # (/admin/voice-budgets). A realtime model page only links there.
    return templates.TemplateResponse(request, "model_detail.html", context)


@router.post("/agent-models/{model_id}/promote")
async def model_promote(
    request: Request,
    model_id: str,
    target_env: str = Form(..., pattern=r"^(staging|prod)$"),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Insert a `(name, target_env)` sibling so the model can be tuned
    independently per environment. The runtime resolver picks the row
    matching the current environment; falls back to dev otherwise.

    Idempotent: re-clicking promote when the sibling already exists
    redirects to it rather than failing the unique constraint loudly.
    """
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_models").select("*").eq("model_id", model_id).execute()
    )
    if not rows:
        raise HTTPException(404, "model not found")
    src = rows[0]

    # Same logical name in the target env already? bounce to it.
    existing = _safe_list(
        client.table("agent_models").select("model_id")
        .eq("name", src.get("name"))
        .eq("environment", target_env)
        .execute()
    )
    if existing:
        return RedirectResponse(
            url=f"/admin/agent-models/{existing[0]['model_id']}", status_code=302
        )

    new_row = {
        "name": src.get("name"),
        "environment": target_env,
        "provider": src.get("provider"),
        "model_string": src.get("model_string"),
        "temperature": src.get("temperature"),
        "max_output_tokens": src.get("max_output_tokens"),
        "extra_config": src.get("extra_config") or {},
        "is_active": src.get("is_active", True),
    }
    inserted = client.table("agent_models").insert(new_row).execute()
    new_id = inserted.data[0]["model_id"] if inserted.data else None
    record_change(
        entity_type="agent_models",
        entity_id=new_id or model_id,
        action="CONFIG_PROMOTE",
        actor_user_id=ctx["user_id"],
        before=src,
        after=new_row,
    )
    redirect_to = f"/admin/agent-models/{new_id}" if new_id else "/admin/agents?tab=models"
    return RedirectResponse(url=redirect_to, status_code=302)
