"""The first-run page: `/admin/setup`.

    GET  /admin/setup             step 1 form, only while no admin exists
    POST /admin/setup             create the admin (email, password, setup code), sign in
    GET  /admin/setup/model-key   step 2: store a provider key and test it, or skip
    POST /admin/setup/model-key
    GET  /admin/setup/api-key     step 3: create the first API key
    POST /admin/setup/api-key     …and show it once, with examples

The rules (what first run is, the code, the lockout, the single admin) live in
`admin/services/first_run.py`. These handlers turn forms into calls.

Who may reach what
    Step 1 has no session to check: the setup code is the proof. It answers
    only while the engine is in first run; afterwards it redirects to the
    login page, for good. Steps 2 and 3 are ordinary signed-in portal pages
    (`require_superuser`) that only the admin who completed step 1 sees, and
    only until the walkthrough is finished; everyone else is sent to the
    portal. They do nothing Settings > Model keys and Settings > API keys do
    not do.

Request forgery
    The portal has no CSRF token. Step 1 is protected by the setup code, which
    a forged form cannot know; steps 2 and 3 by the `SameSite=Lax` session
    cookie, like every portal write. On top of that every POST here refuses a
    request a browser marks as coming from another site (`Sec-Fetch-Site`,
    or an `Origin` that is not this host), so a page on another site cannot
    even burn the code's five tries on a machine that only listens on
    localhost.

Nothing here puts the setup code, a password or a key in a URL, a log line, a
cookie or a redirect. The API key of step 3 exists only in the body of the
response that created it (see `admin/api_keys_handlers.py` for why that is a
page and not a redirect).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from admin import router as _router
from admin.auth import require_superuser
from admin.router import _set_admin_cookie, current_env, router, templates
from admin.services import api_keys as api_keys_service
from admin.services import first_run
from admin.services import provider_keys as provider_keys_service
from core import api_keys as keys
from core import provider_keys as store

logger = logging.getLogger(__name__)

PAGE = "/admin/setup"
MODEL_KEY_PAGE = "/admin/setup/model-key"
API_KEY_PAGE = "/admin/setup/api-key"
LOGIN = "/admin/login"
PORTAL = "/admin/"
FIRST_KEY_NAME = "First API key (created during setup)"


def _to(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def same_origin(request: Request) -> bool:
    """False when a browser says this request was started by another site.

    `Sec-Fetch-Site` is set by the browser and cannot be forged by a page; an
    older browser without it still sends `Origin` on a cross-site POST. A
    client that sends neither (curl, a script) is not a browser being tricked.
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site:
        return site in ("same-origin", "none")
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return True
    if origin == "null":
        return False
    hosts = {h.strip().lower() for h in (
        request.headers.get("host") or "",
        (request.headers.get("x-forwarded-host") or "").split(",")[0],
    ) if h.strip()}
    return urlsplit(origin).netloc.lower() in hosts


def _cross_site() -> HTMLResponse:
    return HTMLResponse("This form can only be submitted from the engine's own page.", status_code=403)


def _render(request: Request, step: int, context: Optional[Dict[str, Any]] = None,
            status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "setup.html", {"step": step, "error": None, **(context or {})},
        status_code=status_code)


# --------------------------------------------------------------------------
# Step 1: the admin account
# --------------------------------------------------------------------------


def _step1(request: Request, *, error: Optional[str] = None, email: str = "",
           locked: bool = False, status_code: int = 200) -> HTMLResponse:
    return _render(request, 1, {
        "error": error, "email": email, "locked": locked,
        "password_min": first_run.PASSWORD_MIN, "max_attempts": first_run.MAX_ATTEMPTS,
    }, status_code)


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request) -> Response:
    if not await first_run.in_first_run():
        return _to(LOGIN)
    return _step1(request)


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    code: str = Form(""),
) -> Response:
    if not same_origin(request):
        return _cross_site()
    if not await first_run.in_first_run():
        return _to(LOGIN)
    if password != password_confirm:
        return _step1(request, error="The two passwords are not the same.", email=email, status_code=400)
    try:
        account = await first_run.create_admin(email, password, code)
    except first_run.SetupClosed:
        return _to(LOGIN)
    except first_run.SetupError as exc:
        return _step1(request, error=exc.message, email=email,
                      locked=exc.kind in ("locked", "expired"), status_code=exc.status)
    except Exception as exc:  # noqa: BLE001 — never echo what was submitted
        logger.error("first run: creating the admin failed (%s)", type(exc).__name__)
        return _step1(request, error="The account could not be created. Check the engine's log "
                                     "and try again.", email=email, status_code=500)

    # Sign in exactly as the login form does: exchange the new credentials for
    # a session and store its access token in the portal cookie.
    from core.auth import SignInError, sign_in_with_password

    try:
        session = await asyncio.to_thread(sign_in_with_password, account["email"], password)
    except SignInError:
        return _to(LOGIN)
    redirect = _to(MODEL_KEY_PAGE)
    _set_admin_cookie(redirect, session["access_token"])
    return redirect


# --------------------------------------------------------------------------
# Step 2: a model key (optional)
# --------------------------------------------------------------------------


def _step2(request: Request, ctx: dict, **extra: Any) -> HTMLResponse:
    status_code = extra.pop("status_code", 200)
    providers = [{"slug": slug, "label": label} for slug, (label, _env) in store.PROVIDERS.items()]
    return _render(request, 2, {
        "ctx": ctx, "providers": providers,
        "can_store": store.encryption_available(), "problem": store.secret_key_problem(),
        "provider": "", "result": None, **extra,
    }, status_code)


@router.get("/setup/model-key", response_class=HTMLResponse)
async def setup_model_key(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    if not await first_run.walkthrough_open(ctx["user_id"]):
        return _to(PORTAL)
    return _step2(request, ctx)


@router.post("/setup/model-key", response_class=HTMLResponse)
async def setup_model_key_submit(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    provider: str = Form(""),
    value: str = Form(""),
) -> Response:
    if not same_origin(request):
        return _cross_site()
    if not await first_run.walkthrough_open(ctx["user_id"]):
        return _to(PORTAL)
    db = _router.client_for(env)
    try:
        await asyncio.to_thread(
            provider_keys_service.set_key, provider, value, ctx["user_id"], None, env=env, client=db)
    except (store.SecretKeyMissing, provider_keys_service.ProviderKeyError) as exc:
        # Written by the service; they never contain the submitted key.
        text = str(exc)
        return _step2(request, ctx, provider=provider, error=text[:1].upper() + text[1:],
                      status_code=400)
    except Exception as exc:  # noqa: BLE001
        logger.warning("first run: storing the model key failed (%s)", type(exc).__name__)
        return _step2(request, ctx, provider=provider, status_code=500,
                      error="The key could not be saved: the database did not accept it.")
    try:
        result = await asyncio.to_thread(
            provider_keys_service.check_key, provider, None, env=env, client=db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("first run: testing the model key failed (%s)", type(exc).__name__)
        result = {"ok": False, "detail": "The test could not be run."}
    label = store.PROVIDERS[store.canonical_provider(provider)][0]
    return _step2(request, ctx, provider=provider,
                  result={"ok": bool(result.get("ok")), "detail": result.get("detail") or "",
                          "label": label})


# --------------------------------------------------------------------------
# Step 3: the first API key
# --------------------------------------------------------------------------


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes")


def _first_agent_slug() -> Optional[str]:
    """The address of a published generic agent, for the example. Blocking."""
    from core.auth import get_service_client

    rows = (get_service_client().table("agent_profiles").select("slug")
            .eq("agent_kind", "custom").eq("is_active", True).limit(1).execute().data) or []
    slug = rows[0].get("slug") if rows and isinstance(rows[0], dict) else None
    return slug if isinstance(slug, str) and slug else None


def _step3(request: Request, ctx: dict, **extra: Any) -> HTMLResponse:
    status_code = extra.pop("status_code", 200)
    return _render(request, 3, {
        "ctx": ctx, "new_key": None, "agent_slug": None,
        "base_url": str(request.base_url).rstrip("/"),
        "chat_enabled": _flag("ENABLE_GENERIC_AGENTS"), "mcp_enabled": _flag("ENABLE_ADMIN_MCP"),
        **extra,
    }, status_code)


@router.get("/setup/api-key", response_class=HTMLResponse)
async def setup_api_key(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    if not await first_run.walkthrough_open(ctx["user_id"]):
        return _to(PORTAL)
    return _step3(request, ctx)


@router.post("/setup/api-key", response_class=HTMLResponse)
async def setup_api_key_create(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    """Create the Full access key and show it once. This closes the
    walkthrough, so a reload cannot mint a second key."""
    if not same_origin(request):
        return _cross_site()
    if not await first_run.walkthrough_open(ctx["user_id"]):
        return _to(PORTAL)
    try:
        _row, key = await asyncio.to_thread(
            api_keys_service.create_key, FIRST_KEY_NAME,
            [keys.SCOPE_CHAT, keys.SCOPE_ADMIN], ctx["user_id"])
    except Exception as exc:  # noqa: BLE001
        logger.error("first run: creating the API key failed (%s)", type(exc).__name__)
        return _step3(request, ctx, status_code=500,
                      error="The key could not be created. Try again, or create one later "
                            "under Settings > API keys.")
    await first_run.finish_walkthrough()
    try:
        slug = await asyncio.to_thread(_first_agent_slug)
    except Exception:  # noqa: BLE001 — the example falls back to a placeholder
        slug = None
    return _step3(request, ctx, new_key=key, agent_slug=slug)
