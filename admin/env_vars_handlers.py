"""
Env-vars CRUD — the list view lives on /admin/env?tab=variables.

Companion to /admin/deployments (Cloud Run URLs) and /admin/env (per-env
profile state). The old standalone /admin/env-vars list is a 307 redirect
to the Variables tab of /admin/env; the mutating POST routes and the
per-var detail page stay here. Today env vars live in two places:

  - Local dev:        ai_agent/.env (python-dotenv at startup).
  - Staging / prod:   GCP Secret Manager referenced from the Cloud Run
                      service yaml via `valueFrom: secretKeyRef`.

This screen writes a third lookup location: rows in agent_env_vars keyed
by (environment, name). The runtime read-path is core.env_resolver,
which prefers os.environ (so Cloud Run overrides still win) and falls
back to a row here.

Secret Manager integration follows in a separate PR — for now
`source='db'` stores the value inline. Rows can declare
`source='secret_manager:<project>/<secret>'` for documentation, but the
resolver returns None for those until the GCP wiring lands.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import client_for, current_env, router, templates
# Env symmetry: the Variables tab reads each env group through that env's
# branch client (`fetch_env_vars_by_env` → `client_for(env)`), so create
# writes to `client_for(<form environment>)` and update/delete go through the
# switcher-scoped `admin_service_client()` — matching the detail read. The
# old bare `get_service_client()` always wrote the LOCAL branch, invisible to
# a cross-env read (identical behavior while cross-env vars are unset).
from core.auth import admin_service_client

logger = logging.getLogger(__name__)

SECRET_PLACEHOLDER = "••••"


def _safe_list(execute_result: Any) -> List[Dict[str, Any]]:
    return execute_result.data or []


# PLAN §2.12 — a value is masked when the row is flagged secret OR its name
# looks like a credential OR the value carries URL credentials, regardless of
# the manual flag (AI_DATABASE_URL used to render its password in clear).
_SECRET_NAME_RE = re.compile(r"(?:^|_)(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|CREDENTIALS?)(?:_|$)", re.I)
_URL_CREDS_RE = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")


def is_sensitive(row: Dict[str, Any]) -> bool:
    """Whether ``row``'s value must be masked in list/detail/audit views."""
    if row.get("is_secret"):
        return True
    if _SECRET_NAME_RE.search(str(row.get("name") or "")):
        return True
    value = row.get("value")
    return bool(value) and bool(_URL_CREDS_RE.search(str(value)))


def masked_value(row: Dict[str, Any]) -> Optional[str]:
    """The display form of a sensitive value. A URL whose only secret is its
    embedded password keeps host/db visible (``postgres://u:••••@host/db``);
    everything else becomes the placeholder."""
    value = row.get("value")
    if not value:
        return None
    text = str(value)
    if not row.get("is_secret") and not _SECRET_NAME_RE.search(str(row.get("name") or "")):
        if _URL_CREDS_RE.search(text):
            return _URL_CREDS_RE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER + m.group(3), text)
    return SECRET_PLACEHOLDER


def _mask(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``row`` with the value masked when it is sensitive
    (see ``is_sensitive``: the secret flag, a credential-looking name, or
    credentials inside a URL)."""
    if not is_sensitive(row):
        return row
    masked = dict(row)
    masked["value"] = masked_value(row)
    masked["_masked"] = True
    return masked


def _mask_for_audit(row: Dict[str, Any], secret: bool) -> Dict[str, Any]:
    """Mask ``row``'s value for an audit payload when ``secret`` is True,
    independent of ``row``'s own is_secret flag.

    ``env_var_update`` passes secret = before.is_secret OR merged.is_secret
    instead of calling ``_mask`` directly on the merged/after row: merging
    ``{**before, **payload}`` and masking on the merged flag alone means an
    update that unmarks a row as secret (before.is_secret=True,
    payload.is_secret=False, no new value submitted) carries the
    still-plaintext ``value`` straight through ``_mask``'s own is_secret
    check and into ``agent_audit_log``. ``_mask`` itself stays untouched
    since the list/detail views rely on its is_secret-only behavior.
    """
    if not secret:
        return row
    masked = _mask({**row, "is_secret": True})
    masked["is_secret"] = row.get("is_secret")
    return masked


def fetch_env_vars_by_env(client: Any = None) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch agent_env_vars rows, masked, grouped by environment.

    Data-only context builder for the Variables tab of the consolidated
    Environments screen (/admin/env?tab=variables). ONE dashboard manages
    BOTH environments, and each env's rows live in its own branch DB — so
    by default each group is read through that env's client. Passing an
    explicit ``client`` forces single-DB mode (local dev / tests). Secret
    values are masked via ``_mask`` before they leave this module.
    """
    from admin.router import client_for

    grouped: Dict[str, List[Dict[str, Any]]] = {"staging": [], "prod": []}
    for env in ("staging", "prod"):
        env_client = client if client is not None else client_for(env)
        rows = _safe_list(
            env_client.table("agent_env_vars").select("*")
            .eq("environment", env).order("name").execute()
        )
        grouped[env] = [_mask(r) for r in rows]
    return grouped


@router.get("/env-vars")
async def env_vars_list(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Merged into /admin/env (Variables tab) — kept as a redirect so old
    bookmarks/links keep working. Query params (env) are carried over."""
    qs = str(request.query_params)
    url = "/admin/env?tab=variables" + (f"&{qs}" if qs else "")
    return RedirectResponse(url=url, status_code=307)


@router.post("/env-vars")
async def env_vars_create(
    request: Request,
    environment: str = Form(..., pattern=r"^(staging|prod)$"),
    name: str = Form(...),
    value: str = Form(""),
    source: str = Form("db"),
    description: str = Form(""),
    is_secret: str = Form(""),
    ctx: dict = Depends(require_superuser),
) -> Response:
    name = name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    payload: Dict[str, Any] = {
        "environment": environment,
        "name": name,
        "value": value if value != "" else None,
        "source": (source or "db").strip() or "db",
        "description": description or None,
        "is_secret": is_secret == "on",
        "created_by": ctx["user_id"],
        "updated_by": ctx["user_id"],
    }
    # The row names its own environment — write it into THAT env's branch,
    # where the Variables tab's per-group read will look for it.
    client = client_for(environment)
    try:
        inserted = client.table("agent_env_vars").insert(payload).execute()
    except Exception as exc:
        logger.warning("env-vars create failed: %s", exc)
        raise HTTPException(400, f"insert failed: {exc}")
    new_row = inserted.data[0] if inserted.data else {}
    record_change(
        entity_type="agent_env_vars",
        entity_id=new_row.get("var_id", ""),
        action="create",
        actor_user_id=ctx["user_id"],
        # Never echo secret values into the audit log.
        after=_mask(new_row),
    )
    return RedirectResponse(url="/admin/env?tab=variables", status_code=302)


@router.get("/env-vars/{var_id}", response_class=HTMLResponse)
async def env_var_detail(
    var_id: str,
    request: Request,
    reveal: Optional[str] = Query(None),
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    client = client_for(env)
    rows = _safe_list(
        client.table("agent_env_vars").select("*").eq("var_id", var_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "env var not found")
    row = rows[0]
    sensitive = is_sensitive(row)
    # Reveal is an explicit, per-request choice; the revealed value is only
    # ever rendered into this response — never logged or audited.
    show_value = (reveal == "1") or not sensitive
    display_row = dict(row)
    if not show_value:
        display_row["value"] = masked_value(row)
    return templates.TemplateResponse(
        request,
        "env_var_detail.html",
        {
            "ctx": ctx,
            "active": "env_vars",
            "current_env": row.get("environment", env),
            "row": display_row,
            "raw_value": (row.get("value") or "") if show_value else "",
            "reveal": show_value,
            "sensitive": sensitive,
        },
    )


@router.post("/env-vars/{var_id}")
async def env_var_update(
    var_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    payload: Dict[str, Any] = {}
    for field_name in ("environment", "name", "source", "description"):
        v = form.get(field_name)
        if v is not None:
            payload[field_name] = v.strip() if isinstance(v, str) else v
    if "name" in payload and not payload["name"]:
        raise HTTPException(400, "name is required")
    value_submitted = "value" in form
    payload["is_secret"] = form.get("is_secret") == "on"
    payload["updated_by"] = ctx["user_id"]

    client = admin_service_client()
    before = _safe_list(
        client.table("agent_env_vars").select("*").eq("var_id", var_id).execute()
    )
    if not before:
        raise HTTPException(404, "env var not found")
    # Snapshot + decide the audit masking BEFORE the mutating update call —
    # some clients mutate the row dict returned by the earlier read in
    # place, so reading is_secret off `before` after `.update().execute()`
    # could silently observe the POST-update flag instead of the original.
    before_snapshot = dict(before[0])
    if value_submitted:
        v = form.get("value")
        # The detail form shows a masked value unless revealed; posting that
        # mask back unchanged means "keep the stored value", not "store ••••".
        if is_sensitive(before_snapshot) and v and v == masked_value(before_snapshot):
            pass
        else:
            payload["value"] = v if v != "" else None
    merged = {**before_snapshot, **payload}
    # Mask on EITHER side's is_secret — see _mask_for_audit's docstring for
    # why masking on the merged/after flag alone lets a secret's plaintext
    # value slip into the audit log when the same update unmarks it.
    audit_secret = bool(before_snapshot.get("is_secret")) or bool(merged.get("is_secret"))
    client.table("agent_env_vars").update(payload).eq("var_id", var_id).execute()
    record_change(
        entity_type="agent_env_vars",
        entity_id=var_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before=_mask_for_audit(before_snapshot, audit_secret),
        after=_mask_for_audit(merged, audit_secret),
    )
    return RedirectResponse(url="/admin/env?tab=variables", status_code=302)


@router.post("/env-vars/{var_id}/delete")
async def env_var_delete(
    var_id: str,
    ctx: dict = Depends(require_superuser),
) -> Response:
    client = admin_service_client()
    before = _safe_list(
        client.table("agent_env_vars").select("*").eq("var_id", var_id).execute()
    )
    if not before:
        raise HTTPException(404, "env var not found")
    client.table("agent_env_vars").delete().eq("var_id", var_id).execute()
    record_change(
        entity_type="agent_env_vars",
        entity_id=var_id,
        action="delete",
        actor_user_id=ctx["user_id"],
        before=_mask(before[0]),
    )
    return RedirectResponse(url="/admin/env?tab=variables", status_code=302)
