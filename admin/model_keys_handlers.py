"""Settings › Model keys: provider keys stored encrypted (/admin/model-keys).

One row per provider with where its key comes from (stored here, the
environment variable, or nowhere), a form to set or replace the stored key, a
Remove button and a Test button. Named credentials (a second key for another
OpenAI-compatible endpoint) sit below.

A stored key is never rendered: the page, the JSON status and the test result
carry only its hint (the last four characters). The work is in
``admin/services/provider_keys.py``; it blocks (database, and the provider for
a test), so every call here goes through ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from urllib.parse import quote_plus

from admin.auth import require_superuser
from admin import router as _router
from admin.router import current_env, router, templates
from admin.services import provider_keys as service
from core import provider_keys as store

logger = logging.getLogger(__name__)

PAGE = "/admin/model-keys"


def _db(env: str) -> Any:
    """The selected environment's database, resolved at call time."""
    return _router.client_for(env)


def _back(**params: str) -> RedirectResponse:
    query = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items() if v)
    return RedirectResponse(url=PAGE + (f"?{query}" if query else ""), status_code=302)


@router.get("/model-keys", response_class=HTMLResponse)
async def model_keys_screen(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    context: Dict[str, Any] = {
        "ctx": ctx, "active": "settings", "current_env": env,
        "error": request.query_params.get("error"),
        "saved": request.query_params.get("saved"),
        "removed": request.query_params.get("removed"),
    }
    try:
        context["status"] = await asyncio.to_thread(service.key_status, env=env, client=_db(env))
    except Exception as exc:  # noqa: BLE001 - show the error state, not a 500
        logger.warning("model keys: load failed (%s)", type(exc).__name__)
        context["status"] = {"can_store": store.encryption_available(),
                             "problem": store.secret_key_problem(), "providers": [],
                             "load_error": "The stored keys could not be read."}
    return templates.TemplateResponse(request, "model_keys.html", context)


@router.get("/model-keys/status.json")
async def model_keys_status(
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Where each provider's key comes from, for the New model form."""
    sources = await asyncio.to_thread(service.provider_sources, env=env, client=_db(env))
    return JSONResponse({"sources": sources, "manage_url": PAGE})


@router.post("/model-keys", response_class=Response)
async def model_keys_set(
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    provider: str = Form(...),
    value: str = Form(""),
    name: str = Form(""),
) -> Response:
    try:
        row = await asyncio.to_thread(
            service.set_key, provider, value, ctx["user_id"], name or None, env=env, client=_db(env))
    except (store.SecretKeyMissing, service.ProviderKeyError) as exc:
        # These messages are written here and never contain the submitted key.
        return _back(error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("model keys: save failed (%s)", type(exc).__name__)
        return _back(error="The key could not be saved: the database did not accept it.")
    label = store.PROVIDERS[row["provider"]][0]
    return _back(saved=label + (f" ({row['name']})" if row.get("name") else ""))


@router.post("/model-keys/delete", response_class=Response)
async def model_keys_delete(
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    provider: str = Form(...),
    name: str = Form(""),
) -> Response:
    try:
        removed = await asyncio.to_thread(
            service.remove_key, provider, ctx["user_id"], name or None, env=env, client=_db(env))
    except service.ProviderKeyError as exc:
        return _back(error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("model keys: remove failed (%s)", type(exc).__name__)
        return _back(error="The key could not be removed.")
    if not removed:
        return _back(error="There was no stored key to remove.")
    label = store.PROVIDERS[store.canonical_provider(provider)][0]
    return _back(removed=label + (f" ({name})" if name else ""))


@router.post("/model-keys/test")
async def model_keys_test(
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    provider: str = Form(...),
    name: str = Form(""),
) -> JSONResponse:
    """One free call to the provider with the key in use. Returns
    ``{"ok", "source", "detail"}``; the detail never contains the key."""
    try:
        result = await asyncio.to_thread(service.check_key, provider, name or None, env=env, client=_db(env))
    except service.ProviderKeyError as exc:
        return JSONResponse({"ok": False, "source": "none", "detail": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model keys: test failed (%s)", type(exc).__name__)
        return JSONResponse({"ok": False, "source": "none",
                             "detail": "The test could not be run."}, status_code=500)
    return JSONResponse(result)
