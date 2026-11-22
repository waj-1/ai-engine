"""Settings > API keys: `/admin/api-keys`.

    GET  /admin/api-keys                  the list and the create form
    POST /admin/api-keys                  create a key; the response is the page
                                          with the new key on it, once
    POST /admin/api-keys/{key_id}/revoke  revoke a key

The logic lives in `admin/services/api_keys.py` (shared with the `api_keys`
MCP tool); these handlers only turn a form into a call and a result into a
page. Request authentication with a key is `core/api_keys.py`.

Why create answers with a page instead of the usual redirect: the key exists
only in the response to the request that created it. A redirect would need it
parked somewhere (a query string, a cookie, the session) to show it on the next
page, and each of those is a place a key could be read back from later. So the
POST renders the list itself, with the key in it, and nothing keeps a copy.
Every /admin response is already `Cache-Control: no-store`, so the browser does
not keep that page either; reloading it asks to submit the form again rather
than showing the key.

Keys are not per environment (see the service), so the screen hides the
environment switcher and ignores the selected environment.

Session and request forgery: these are plain form POSTs under the portal's
session cookie, like every other portal write. The cookie is `SameSite=Lax`,
so a form on another site cannot send it. A forged create could not read the
key back in any case, and a revoke needs a key id an outsider does not know.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from admin.auth import require_superuser
from admin.router import _redirect_with_error, router, templates
from admin.screens.govern import relative_time
from admin.services import api_keys as service
from core import api_keys as keys

logger = logging.getLogger(__name__)

PAGE = "/admin/api-keys"

#: The form's access choice -> the scopes stored. `admin` lists `chat` too so
#: the table says plainly what a full-access key can do.
ACCESS_CHOICES: Dict[str, Dict[str, Any]] = {
    "chat": {"label": "Chat only", "scopes": [keys.SCOPE_CHAT],
             "help": "Can talk to your agents through the chat API. Cannot change anything."},
    "admin": {"label": "Full access", "scopes": [keys.SCOPE_CHAT, keys.SCOPE_ADMIN],
              "help": "Chat, plus full control of the engine through the MCP server: "
                      "agents, prompts, tools, team and API keys. Treat it like an admin password."},
}
SCOPE_LABELS = {keys.SCOPE_CHAT: "Chat", keys.SCOPE_ADMIN: "Admin"}


def _present(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["scope_labels"] = [SCOPE_LABELS.get(s, s) for s in row.get("scopes") or []]
        item["created"] = relative_time(row.get("created_at")) if row.get("created_at") else ""
        item["last_used"] = relative_time(row["last_used_at"]) if row.get("last_used_at") else ""
        expires = keys.parse_timestamp(row.get("expires_at"))
        item["expires"] = expires.strftime("%d %b %Y") if expires else ""
        out.append(item)
    return out


def _page(request: Request, ctx: dict, *, new_key: Optional[str] = None,
          new_row: Optional[Dict[str, Any]] = None, error: Optional[str] = None,
          form: Optional[Dict[str, str]] = None, status_code: int = 200) -> HTMLResponse:
    context: Dict[str, Any] = {
        "ctx": ctx, "active": "settings", "hide_env_switcher": True,
        "error": error or request.query_params.get("error"),
        "revoked": request.query_params.get("revoked") == "1",
        "access_choices": ACCESS_CHOICES, "expiry_choices": service.EXPIRY_CHOICES,
        "new_key": new_key, "new_row": new_row, "form": form or {},
        "key_prefix": keys.KEY_PREFIX, "rows": [],
    }
    try:
        context["rows"] = _present(service.list_keys())
    except Exception as exc:  # noqa: BLE001 - show the error state, not a 500
        logger.warning("api keys: list failed (%s)", type(exc).__name__)
        context["load_error"] = True
    return templates.TemplateResponse(request, "api_keys.html", context, status_code=status_code)


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_screen(request: Request, ctx: dict = Depends(require_superuser)) -> HTMLResponse:
    """Settings > API keys: every key (never the key itself) and the create form."""
    return _page(request, ctx)


@router.post("/api-keys", response_class=HTMLResponse)
async def api_keys_create(
    request: Request,
    ctx: dict = Depends(require_superuser),
    name: str = Form(""),
    access: str = Form("chat"),
    expires: str = Form(""),
) -> HTMLResponse:
    form = {"name": name, "access": access, "expires": expires}
    choice = ACCESS_CHOICES.get(access)
    if choice is None or expires not in service.EXPIRY_CHOICES:
        return _page(request, ctx, error="Choose one of the listed options.", form=form, status_code=400)
    try:
        row, key = service.create_key(
            name, choice["scopes"], created_by=ctx["user_id"],
            expires_at=service.expiry_from_days(service.EXPIRY_CHOICES[expires][1]))
    except ValueError as exc:
        return _page(request, ctx, error=str(exc).capitalize() + ".", form=form, status_code=400)
    except Exception as exc:  # noqa: BLE001
        logger.error("api keys: create failed (%s)", type(exc).__name__)
        return _page(request, ctx, error="The key could not be created. Try again.", form=form,
                     status_code=500)
    return _page(request, ctx, new_key=key, new_row=_present([row])[0])


@router.post("/api-keys/{key_id}/revoke", response_class=Response)
async def api_keys_revoke(key_id: str, ctx: dict = Depends(require_superuser)) -> Response:
    try:
        row = service.revoke_key(key_id, revoked_by=ctx["user_id"])
    except Exception as exc:  # noqa: BLE001
        logger.error("api keys: revoke failed (%s)", type(exc).__name__)
        return _redirect_with_error(PAGE, "The key could not be revoked. Try again.")
    if row is None:
        return _redirect_with_error(PAGE, "That key no longer exists.")
    return RedirectResponse(url=f"{PAGE}?revoked=1", status_code=302)
