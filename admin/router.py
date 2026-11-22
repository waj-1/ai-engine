"""
Admin portal — FastAPI router mounted at /admin/*.

Navigation is defined in admin/nav.py. Operate screens:
  - /admin/live          Overview (outcomes per period, needs attention,
                         technical details; ?tab=jobs → /admin/jobs)
  - /admin/observability Conversations (list + conversation viewer;
                         /admin/sessions/* redirect into it)
  - /admin/audit         Activity (paginated audit log)
  Configure:
  - /admin/agents        agent profiles (prompts, tools, data sources)
  - /admin/kb            knowledge base / RAG sources
  - /admin/tools     external integrations (MCP servers etc.)
  - /admin/evals         list + trigger eval runs
  Govern:
  - /admin/users         users & roles
  - /admin/env           environments

Back-compat redirects (302):
  /admin/           → /admin/live
  /admin/profiles   → /admin/agents
  /admin/prompts    → /admin/agents
  /admin/data-sources → /admin/kb
  /admin/mcp-servers  → /admin/tools
  /admin/observability/{trace_id} → /admin/observability/explorer#trace=…

All routes require `Depends(require_superuser)`. UI is Jinja-rendered with
HTMX for interactivity (no Node build). CSS served from /admin/static/portal.css.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from admin.auth import ADMIN_TOKEN_COOKIE, require_superuser
from admin.audit import record_change
from admin.integrations_registry import INTEGRATIONS, visible_integrations  # noqa: F401
from admin.evals import run_eval_suite_sync
from admin.screens import agents as agents_screen
from admin.screens import agents_views
from core.agent_profile import AgentProfileLoader
from core.auth import get_service_client, get_main_client, admin_service_client
from core.prompt_resolver import PromptResolver

logger = logging.getLogger(__name__)


def _run_in_background(fn, *, _label: str = "task", **kwargs) -> None:
    """Spawn a daemon thread to run a sync function fire-and-forget.

    Used by the eval + E2E trigger endpoints so the redirect returns
    immediately and the admin UI shows the row's status transition
    (queued → running → passed/failed) instead of waiting on the LLM
    call for 30+ seconds per scenario. Exceptions inside `fn` are
    logged here and otherwise swallowed — the workers persist their
    own status row, so a crash before they can write is the only case
    we'd lose telemetry on, and that's captured in this log line.
    """
    import threading

    def _wrap():
        try:
            fn(**kwargs)
        except Exception as exc:
            logger.exception("background %s crashed: %s", _label, exc)

    t = threading.Thread(target=_wrap, name=_label, daemon=True)
    t.start()

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

AUDIT_ACTION_VARIANT = {
    "TOOL_CALL": "mauve",
    "OTP_SKIP": "brand",
    "SQL_EXEC": "brand",
    "CALLEE_VERIFY": "brand",
    "CONFIG_PROMOTE": "mauve",
    "HUMAN_HANDOFF": "warn",
    "NUMERIC_GUARD": "err",
    "DIAL": "ghost",
    "JOB_TRIGGER": "ghost",
}


def _pretty_json(value: Any) -> str:
    """Pretty JSON for a template — and **empty string for an absent value**.

    This used to be `json.dumps(v or {}, …)`, which rendered `{}` for a value
    that was never set. In a `<pre>` that is only noise; in a TEXTAREA it is a
    write. `agents.html`'s `voice_by_language` box is the case that bit: an
    unset key rendered as `{}`, the save path read that back as valid JSON, and
    the first save of ANY profile persisted `voice_config.voice_by_language =
    {}` — a key the operator never touched, appearing in every diff and every
    promote snapshot from then on. Recorded as D11 in the voice-config
    unification spec.

    Only `None` becomes "": a stored `{}` still renders as `{}`, because that
    one IS set and round-tripping it is correct.
    """
    if value is None:
        return ""
    return json.dumps(value, indent=2, default=str)


def _add_filters(env: Jinja2Templates) -> None:
    """Tiny Jinja filter for pretty JSON in templates."""
    env.env.filters["pretty_json"] = _pretty_json


_add_filters(templates)

# Asset version — recomputed on each Python process start (matches a
# typical `make admin-portal` restart). Used by _base.html to bust the
# browser cache for portal.css + portal-table.js so reloads pick up the
# latest JS without the user having to hard-refresh.
try:
    _STATIC_DIR = Path(__file__).parent / "static"
    _STATIC_VERSION = str(int(max(
        (_STATIC_DIR / name).stat().st_mtime
        for name in ("portal.css", "portal-table.js")
        if (_STATIC_DIR / name).exists()
    )))
except Exception:
    _STATIC_VERSION = "dev"

templates.env.globals["asset_version"] = _STATIC_VERSION

# Plain agent names for Build-area pickers/chips (admin/agent_kinds.py) and
# plain source-type names for knowledge bases.
from admin.agent_kinds import agent_label as _agent_label  # noqa: E402

templates.env.globals["agent_label"] = _agent_label

# Extra run-context id fields a plugin's agents read (admin/eval_context.py).
from admin.eval_context import eval_context_fields as _eval_context_fields  # noqa: E402

templates.env.globals["eval_context_fields"] = _eval_context_fields

KB_KIND_LABELS = {
    "pgvector": "Uploaded files",
    "gitbook": "GitBook space",
    "ms_drive": "Microsoft Drive folder",
    "http": "Website / URL",
    "supabase_table": "Database table (advanced)",
}
# Source types that sync from somewhere else — only these have a meaningful
# "last refreshed"; uploaded files and live tables do not.
KB_SYNCED_KINDS = frozenset({"gitbook", "ms_drive", "http"})
templates.env.globals["kb_kind_label"] = lambda k: KB_KIND_LABELS.get(k or "", k or "—")

# A scoped build whose agent picks its own demo tenant: the Test panel shows a
# note instead of the business picker (core.deployment.demo_tenant_build).
# Read once at import — stable per process.
try:
    from core.deployment import demo_tenant_build as _demo_tenant_build
    templates.env.globals["demo_tenant_build"] = _demo_tenant_build()
except Exception:
    templates.env.globals["demo_tenant_build"] = False

# NOTE: there are deliberately no `lmnr_*_url` Jinja globals any more. The
# `lmnr_trace_url()` / `lmnr_dataset_url()` deep-link helpers needed a project id
# resolvable only through the Laminar web UI's session-cookie API — routes the
# deployed build doesn't serve — and the plain `lmnr_dashboard_url` global was
# rendered by zero templates. The portal draws traces itself under
# /admin/observability, straight from ClickHouse. `LMNR_DASHBOARD_URL` is still
# read where it's genuinely useful: the diagnose page's config readout.


router = APIRouter(prefix="/admin", tags=["admin"])

from fastapi import Query as _Q, Cookie as _Cookie

# The AI engine is one control-plane managing TWO environments.
ENV_COOKIE = "waj_admin_env"
ALLOWED_ENVS = ("staging", "prod")
DEFAULT_ENV = "staging"


# Which environment(s) this deployment actually has. Defined in core.deployment
# so every admin Jinja env can register it without importing this module (they
# each build their own `Jinja2Templates`, and _base.html calls it on every
# render — a missing global is an UndefinedError, not a silent falsy).
from core.deployment import single_env_deployment  # noqa: E402

# Registered as a CALLABLE (not a value) so it re-reads env per render: the
# module is imported once at boot, long before any test sets a deployment scope.
templates.env.globals["single_env_deployment"] = single_env_deployment

# How the Test drawer talks to a profile (endpoint, the kind's own body fields,
# whether it can be tested at all) — shared with the admin MCP `chat` tool.
from admin.services.test_chat import request_shape as _test_chat_shape  # noqa: E402
templates.env.globals["test_chat_shape"] = _test_chat_shape

# Sidebar / section tabs / Settings hub / registry-driven branding (admin/nav.py).
from admin import nav as portal_nav  # noqa: E402

portal_nav.install_template_globals(templates)

# Testing-area display filters (percent / points / status vocabulary, admin/testing_view.py).
from admin import testing_view as _testing_view  # noqa: E402

_testing_view.install_filters(templates)


def current_env(
    env: Optional[str] = _Q(None, pattern=r"^(staging|prod)$"),
    waj_admin_env: Optional[str] = _Cookie(None),
) -> str:
    """Resolve the active environment for the request.

    Precedence: this deployment's own env when it serves only one (see
    ``single_env_deployment``) → explicit ``?env=`` (the switcher) → the
    persisted cookie → staging. The persist-env middleware (main.py) sets the
    cookie whenever a request carries ``?env=``, so the selection survives
    navigation between screens without every link threading the param.

    A single-env deployment wins over BOTH, and that ordering is load-bearing.
    The other env has no rows there by construction, the switcher that would
    let an operator correct a bad value is hidden on those boxes
    (``_base.html``), and the cookie lives for 30 days — so one stray
    ``?env=prod`` link left a client demo portal rendering "No profiles
    seeded for the prod environment yet" on every config screen with no way
    back. Honouring a selection you cannot then un-select is not respecting
    the operator, it is trapping them.
    """
    only = single_env_deployment()
    if only:
        return only
    if env in ALLOWED_ENVS:
        return env
    if waj_admin_env in ALLOWED_ENVS:
        return waj_admin_env
    return DEFAULT_ENV


def current_env_live(
    env: Optional[str] = _Q(None, pattern=r"^(staging|prod)$"),
    waj_admin_env: Optional[str] = _Cookie(None),
) -> str:
    """``current_env`` for the **Operate** screens — Overview (/admin/live),
    Conversations (/admin/observability + its JSON panes) and Telemetry
    diagnose — defaulting to **prod** instead of staging when nothing is
    selected.

    Why the two defaults differ, and why all Operate screens share this one:

    * Operate screens answer "what are my agents doing for customers?". Real
      customer traffic is prod, so a fresh session opens on prod. (The old
      justification — "the staging AI service was retired 2026-07-08, so staging
      telemetry is empty" — is stale: ``ai-engine-staging`` was recreated on
      2026-08-09 and CI rolls it. Staging has telemetry again; it is just not
      where customers are.)
    * Config screens keep the staging default via ``current_env`` — edits are
      meant to land on staging and be promoted, so opening a config screen on
      prod by default would invite editing live agents directly.
    * The three Operate screens MUST agree. Overview used this dependency while
      Conversations and Diagnose used ``current_env``, so in one session with
      no cookie the Overview showed PRODUCTION and Conversations silently
      switched to STAGING (portal UX audit 2026-10-11, P0 #3).

    An explicit ``?env=`` or the persisted cookie still wins, so an operator
    who deliberately picks staging stays there across every screen.

    On a scoped build that prod default is actively wrong — it names the one
    environment the box does *not* have — so its own env wins there, ahead of
    the query param and the cookie both (see ``current_env`` for why an
    un-selectable selection is a trap rather than a preference)."""
    only = single_env_deployment()
    if only:
        return only
    if env in ALLOWED_ENVS:
        return env
    if waj_admin_env in ALLOWED_ENVS:
        return waj_admin_env
    return "prod"


def client_for(env: str):
    """Service-role Supabase client for the *selected* environment's branch.

    On the control-plane, ``STAGING_AI_SUPABASE_*`` / ``PROD_AI_SUPABASE_*``
    select the staging / production branch DBs, so switching env reads real
    per-env data. Locally (cross-env vars unset) it falls back to the single
    local AI DB so dev + tests keep working.
    """
    from core.auth import CrossEnvNotConfiguredError, get_service_client_for_env
    try:
        return get_service_client_for_env(env)
    except CrossEnvNotConfiguredError:
        return get_service_client()


# ---------------------------------------------------------------------------
# Portal error page (404 / 422 / … for browser GETs). main.py's exception
# handlers call this for /admin/* requests that accept HTML, so a bad id or a
# bad ?env= shows a page with a way back instead of a raw JSON body. API-ish
# routes (*.json, /admin/api/*, non-GET) keep their JSON envelope.
# ---------------------------------------------------------------------------

_ERROR_HEADINGS = {
    400: "Something is missing",
    401: "Please sign in",
    403: "You don't have access",
    404: "Page not found",
    422: "This link isn't valid",
}


def _is_uuid(value: Any) -> bool:
    import uuid as _uuid
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def wants_portal_error_page(request: Request) -> bool:
    path = request.url.path
    if not (path == "/admin" or path.startswith("/admin/")):
        return False
    if path.startswith(("/admin/static", "/admin/help/media", "/admin/api/", "/admin/docs/api")):
        return False
    if path.endswith((".json", ".wav", ".csv")):
        return False
    if request.method not in ("GET", "HEAD"):
        return False
    return "text/html" in (request.headers.get("accept") or "")


def _plain_detail(detail: Any) -> str:
    if isinstance(detail, str) and detail.strip():
        text = detail.strip()
        return text[0].upper() + text[1:] + ("" if text.endswith((".", "!", "?")) else ".")
    return ""


def render_error_page(
    request: Request,
    status_code: int,
    *,
    detail: Any = None,
    bad_params: Optional[List[str]] = None,
) -> HTMLResponse:
    """Render admin/templates/error.html with a link back to the page's area."""
    section = portal_nav.nav_item(portal_nav.section_for(request.url.path)) or portal_nav.PRIMARY[0]
    back_href, back_label = section.href, f"Back to {section.label}"
    retry_href = retry_label = None
    if status_code == 401:
        back_href, back_label = "/admin/login", "Sign in"
        message = "Your session has ended or you are not signed in."
    elif bad_params:
        names = ", ".join(sorted(set(bad_params)))
        if "env" in bad_params:
            message = ("The environment in this link isn't one this portal knows. "
                       "Choose Staging or Production instead.")
        else:
            message = f"This link has a value the page can't use ({names})."
        # Same page without the query string — the usual fix for a bad param.
        retry_href, retry_label = request.url.path, "Open this page without it"
    elif status_code == 404:
        message = (_plain_detail(detail) or "We couldn't find that page.") + \
            " It may have been deleted, or the link is out of date."
    else:
        message = _plain_detail(detail) or "The request couldn't be completed."
    heading = _ERROR_HEADINGS.get(status_code, "Something went wrong")
    resp = templates.TemplateResponse(
        request, "error.html",
        {"ctx": None, "status": status_code, "heading": heading, "message": message,
         "back_href": back_href, "back_label": back_label,
         "retry_href": retry_href, "retry_label": retry_label,
         "hide_env_switcher": True},
        status_code=status_code,
    )
    return resp


# ---------------------------------------------------------------------------
# Auth / login
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


def _set_admin_cookie(redirect: Response, token: str) -> None:
    secure_cookie = os.getenv("ENVIRONMENT", "development").lower() in ("production", "prod")
    redirect.set_cookie(
        key=ADMIN_TOKEN_COOKIE,
        value=token.strip(),
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        max_age=60 * 60 * 8,
    )


@router.post("/login")
async def login_submit(
    request: Request,
    email: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    token: Optional[str] = Form(None),
) -> Response:
    """Sign in to the admin portal.

    Primary path: email + password → exchanged for a Supabase session via
    GoTrue; the resulting access_token is stored in the admin cookie.
    Fallback path: paste a raw JWT (kept for service tokens / debugging).

    Either way the stored token is validated on every protected endpoint
    by `require_superuser`, which also enforces the is_superuser flag — so
    a non-superuser who signs in with valid creds still can't get past the
    gate; they'll see the 403 on the first protected page.
    """
    from core.auth import SignInError, sign_in_with_password

    # Email + password is the main flow.
    if email and password:
        try:
            session = sign_in_with_password(email.strip(), password)
        except SignInError as exc:
            return templates.TemplateResponse(
                request, "login.html",
                {"error": str(exc), "email": email},
                status_code=401,
            )
        redirect = RedirectResponse(url="/admin/", status_code=302)
        _set_admin_cookie(redirect, session["access_token"])
        return redirect

    # Fallback: a raw token was pasted.
    if token and token.strip():
        redirect = RedirectResponse(url="/admin/", status_code=302)
        _set_admin_cookie(redirect, token)
        return redirect

    return templates.TemplateResponse(
        request, "login.html",
        {"error": "Enter your email and password.", "email": email or ""},
        status_code=400,
    )


@router.post("/logout")
async def logout() -> Response:
    redirect = RedirectResponse(url="/admin/login", status_code=302)
    redirect.delete_cookie(ADMIN_TOKEN_COOKIE)
    return redirect


# ---------------------------------------------------------------------------
# Root redirect + Live Monitor
# ---------------------------------------------------------------------------


# Both "" (/admin) and "/" (/admin/) are handled explicitly. Without the
# no-slash route, Starlette's automatic slash-redirect builds an ABSOLUTE URL
# from the request Host — which behind a reverse proxy (e.g. Vercel, which also
# strips the trailing slash) leaks the internal Cloud Run host and bounces the
# user off the public domain. A relative redirect here keeps everything on the
# public origin.
@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def root_redirect() -> Response:
    return RedirectResponse(url="/admin/live", status_code=302)


@router.get("/live", response_class=HTMLResponse)
async def live_monitor(
    request: Request,
    env: str = Depends(current_env_live),
    tab: str = Query("overview"),
    period: Optional[str] = Query(None),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Overview — "how are my agents doing?" for one period vs the previous one.

    The partner-facing part (status strip, outcome tiles, agents table, needs
    attention, voice) comes from ``admin.screens.overview.build_overview``; the
    engineering tiles, stage latencies and the 14 charts live under the
    collapsed "Technical details" section, unchanged in substance.
    """
    if tab == "jobs":
        # The Jobs tab moved to Settings › Diagnostics (/admin/jobs). Old
        # links keep working; filters carry over.
        qs = "&".join(f"{k}={v}" for k, v in request.query_params.multi_items() if k != "tab")
        return RedirectResponse(url="/admin/jobs" + (f"?{qs}" if qs else ""), status_code=307)

    import asyncio

    from fastapi.concurrency import run_in_threadpool
    from admin._channel_picker import fetch_channels_for_picker, visible_agent_kinds_for_picker
    from admin.screens.live_analytics import estimate_cache_savings_usd, load_analytics
    from admin.screens.observability import load_live_insights, load_voice_live_insights
    from admin.screens.overview import build_overview, resolve_period
    from admin.screens import plain_language as pl

    client = client_for(env)
    hours = int(resolve_period(period)["hours"])
    # Independent sources, fetched concurrently and off the event loop (the
    # ClickHouse + supabase clients are synchronous).
    overview, insights, voice_live, analytics = await asyncio.gather(
        run_in_threadpool(build_overview, env=env, period_key=period, client=client),
        run_in_threadpool(load_live_insights, past_hours=hours, env=env),
        run_in_threadpool(load_voice_live_insights, past_hours=hours, env=env),
        run_in_threadpool(load_analytics, hours=hours, environment=env),
    )
    kpis = _live_kpis(client, env, insights)
    # Engineering tiles for the period come from the analytics rollup (full
    # row cap) rather than the 200-row recent list; a failed rollup is "—",
    # never 0 — the old Cache hit tile rendered a confident 0.0% during an
    # outage while every other tile said "—".
    totals = analytics.get("totals") or {}
    if analytics.get("ok"):
        kpis["latency_p95_ms"] = (
            int(totals["p95_ms"]) if totals.get("p95_ms") is not None else "—")
        kpis["token_spend_usd"] = round(float(totals.get("cost_usd") or 0.0), 2)
        kpis["cache_hit_rate_pct"] = totals.get("cache_hit_rate", 0.0)
        saved = estimate_cache_savings_usd(client, totals.get("cache_read_tokens", 0))
        kpis["cache_saved_usd"] = round(saved, 2) if saved else 0.0
    else:
        kpis["latency_p95_ms"] = "—"
        kpis["token_spend_usd"] = "—"
        kpis["cache_hit_rate_pct"] = "—"
        kpis["cache_saved_usd"] = "—"
    agent_names = pl.load_agent_display_names(env)
    per_agent_tech = [
        {**r, "name": pl.agent_label(r.get("agent_kind") if r.get("agent_kind") != "unknown" else None,
                                     agent_names)}
        for r in (analytics.get("per_agent") or [])
    ]
    show_voice = bool(
        (voice_live.get("ch_ok") and (voice_live.get("sessions") or voice_live.get("turns")))
        or ((overview.get("voice_usage") or {}).get("minutes"))
        or ((overview.get("voice_usage") or {}).get("prev_minutes"))
    )
    return templates.TemplateResponse(
        request, "live.html",
        {"ctx": ctx, "active": "live", "current_env": env,
         "tab": "overview",
         "ov": overview,
         "kpis": kpis,
         "lmnr_insights": insights,
         "voice_live": voice_live,
         "show_voice": show_voice,
         "analytics_ok": bool(analytics.get("ok")),
         "per_agent_tech": per_agent_tech,
         "agent_kinds": list(visible_agent_kinds_for_picker()),
         "agent_names": agent_names,
         "channels": fetch_channels_for_picker(),
         "hours": hours},
    )


@router.get("/live/analytics.json", response_class=JSONResponse)
async def live_analytics_json(
    hours: int = Query(24, ge=1, le=8760),
    agent_kind: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern=r"^(success|error)?$"),
    environment: Optional[str] = Query(None),
    _ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Aggregated ClickHouse analytics for the Live Monitor charts.

    Filters: time window (hours), agent_kind, channel slug, status, environment.
    ``environment`` scopes the analytics to traces tagged with that ``waj.env``
    (dev/staging/prod) so each portal env shows its own numbers. Returns the
    payload documented in admin.screens.live_analytics.load_analytics."""
    from fastapi.concurrency import run_in_threadpool
    from admin.screens.live_analytics import load_analytics

    payload = await run_in_threadpool(
        load_analytics,
        hours=hours,
        agent_kind=agent_kind or None,
        channel=channel or None,
        status=status or None,
        environment=environment or None,
    )
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Configure — retired landing screen. Its card grid duplicated the Build group
# in the sidebar (Agents / Tools / Channels / Knowledge), so the screen was
# removed; the route survives as a redirect for old bookmarks/links.
# ---------------------------------------------------------------------------


@router.get("/configure")
async def configure_landing(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    """Retired — the sidebar's Build group replaced the Configure landing.
    Redirects to the first Build screen; query params (env) are carried over."""
    qs = str(request.query_params)
    url = "/admin/agents" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=url, status_code=307)


# ---------------------------------------------------------------------------
# Docs — in-portal rendering of docs/admin-portal/*.md
#
# The same markdown that lives in the repo (and is grep-friendly for an AI) is
# rendered here with marked.js + DOMPurify (both already loaded by _base.html).
# `slug` is only ever used as a dict key, never as a filesystem path, so there
# is no path-traversal surface.
# ---------------------------------------------------------------------------

_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs" / "admin-portal"

# Order here defines the sidebar order. slug → file/title/group.
DOC_PAGES: List[Dict[str, str]] = [
    {"slug": "index", "file": "README.md", "title": "Overview", "group": "Guide"},
    {"slug": "operate", "file": "01-operate.md", "title": "Operate", "group": "Sections"},
    {"slug": "configure", "file": "02-configure.md", "title": "Configure", "group": "Sections"},
    {"slug": "test", "file": "03-test.md", "title": "Test", "group": "Sections"},
    {"slug": "govern", "file": "04-govern.md", "title": "Govern", "group": "Sections"},
    {"slug": "deploy", "file": "05-deploy.md", "title": "Deploy", "group": "Sections"},
    {"slug": "mcp", "file": "06-mcp.md", "title": "MCP Server", "group": "Sections"},
    {"slug": "architecture", "file": "00-architecture.md", "title": "Architecture", "group": "Reference"},
    {"slug": "data-model", "file": "data-model.md", "title": "Data Model", "group": "Reference"},
]
_DOC_BY_SLUG = {p["slug"]: p for p in DOC_PAGES}
_DOC_LINK_MAP = {p["file"]: p["slug"] for p in DOC_PAGES}


def _docs_gated() -> bool:
    """Developer docs are staff-only: a partner (scoped) build sends
    /admin/docs* to the partner Help instead. The nav, the Settings hub and
    global search hide them on the same predicate (admin/nav.py)."""
    return portal_nav.partner_build()


def _render_doc(request: Request, slug: str, ctx: dict) -> HTMLResponse:
    page = _DOC_BY_SLUG.get(slug)
    if page is None:
        raise HTTPException(status_code=404, detail="Unknown docs page")
    try:
        markdown = (_DOCS_DIR / page["file"]).read_text(encoding="utf-8")
    except FileNotFoundError:
        markdown = f"# {page['title']}\n\nDocumentation file `{page['file']}` is missing."
    groups: Dict[str, List[Dict[str, str]]] = {}
    for p in DOC_PAGES:
        groups.setdefault(p["group"], []).append(p)
    return templates.TemplateResponse(
        request, "docs.html",
        {"ctx": ctx, "active": "docs", "hide_env_switcher": True, "page": page, "groups": groups,
         "markdown": markdown, "link_map": _DOC_LINK_MAP,
         "docs_base": "/admin/docs", "docs_label": "Developer docs",
         "docs_subtitle": "Technical reference for engineers: architecture, data model, MCP server and API.",
         "show_api": True, "media_base": None},
    )


@router.get("/docs", response_class=HTMLResponse)
async def docs_index(
    request: Request, ctx: dict = Depends(require_superuser),
) -> Response:
    if _docs_gated():
        return RedirectResponse(url="/admin/help", status_code=307)
    return _render_doc(request, "index", ctx)


@router.get("/docs/api", response_class=HTMLResponse, include_in_schema=False)
async def api_swagger(ctx: dict = Depends(require_superuser)) -> Response:
    if _docs_gated():
        return RedirectResponse(url="/admin/help", status_code=307)
    return get_swagger_ui_html(
        openapi_url="/admin/docs/api/openapi.json",
        title="WAJ AI Engine API — Swagger",
    )


@router.get("/docs/api/redoc", response_class=HTMLResponse, include_in_schema=False)
async def api_redoc(ctx: dict = Depends(require_superuser)) -> Response:
    if _docs_gated():
        return RedirectResponse(url="/admin/help", status_code=307)
    return get_redoc_html(
        openapi_url="/admin/docs/api/openapi.json",
        title="WAJ AI Engine API — ReDoc",
    )


@router.get("/docs/api/openapi.json", include_in_schema=False)
async def api_openapi(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    if _docs_gated():
        raise HTTPException(status_code=404, detail="Not available on this deployment")
    app = request.app
    return JSONResponse(get_openapi(
        title=app.title, version=app.version,
        description=app.description, routes=app.routes,
    ))


@router.get("/docs/{slug}", response_class=HTMLResponse)
async def docs_page(
    request: Request, slug: str, ctx: dict = Depends(require_superuser),
) -> Response:
    if _docs_gated():
        return RedirectResponse(url="/admin/help", status_code=307)
    return _render_doc(request, slug, ctx)


# ---------------------------------------------------------------------------
# Help — partner guides in docs/partner/, rendered with the same viewer as the
# developer docs. Layout:
#
#   docs/partner/README.md                  → /admin/help            (Help home)
#   docs/partner/NN-section/NN-page.md      → /admin/help/section/page
#   docs/partner/NN-page.md (legacy, flat)  → /admin/help/page
#   docs/partner/media/<section>/<file>     → /admin/help/media/<section>/<file>
#
# Numeric prefixes set the order and are stripped from slugs and section
# names. A page's title is its first "# " heading. Relative links between
# pages (../02-agents/08-test-your-agent.md) and media references (media/…,
# ../media/…) are rewritten server-side into portal URLs, so the same markdown
# reads correctly on GitHub and in the portal. Adding a guide is adding a file.
# `slug` is only ever a dict key, never a filesystem path.
# ---------------------------------------------------------------------------

HELP_DIR = Path(__file__).resolve().parent.parent / "docs" / "partner"
HELP_MEDIA_URL = "/admin/help/media"
HELP_BASE_URL = "/admin/help"
_HELP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HELP_PREFIX_RE = re.compile(r"^\d+[-_]")


def _help_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _help_slug_part(name: str) -> str:
    return _HELP_PREFIX_RE.sub("", name.lower()).replace("_", "-")


def _help_section_title(folder: str) -> str:
    """'08-team-and-settings' → 'Team & settings'."""
    words = _help_slug_part(folder).replace("-", " ").strip()
    words = re.sub(r"\band\b", "&", words)
    return words[:1].upper() + words[1:]


def _help_page(path: Path, rel: str, slug: str, group: str, section: str) -> Optional[Dict[str, str]]:
    try:
        title = _help_title(path.read_text(encoding="utf-8"), path.stem)
    except OSError:
        return None
    return {"slug": slug, "file": rel, "title": title, "group": group, "section": section}


def help_pages() -> List[Dict[str, str]]:
    """Partner help pages in reading order: the Help home, loose top-level
    pages ("Guides"), then each section folder in numeric order."""
    pages: List[Dict[str, str]] = []
    if not HELP_DIR.is_dir():
        return pages
    loose: List[Dict[str, str]] = []
    for path in sorted(HELP_DIR.glob("*.md")):
        if path.name.lower() == "readme.md":
            page = _help_page(path, path.name, "index", "Start here", "")
            if page:
                page["title"] = "Help home"  # the sidebar entry; the page keeps its own heading
                pages.insert(0, page)
            continue
        slug = _help_slug_part(path.stem)
        if _HELP_SLUG_RE.match(slug):
            page = _help_page(path, path.name, slug, "Guides", "")
            if page:
                loose.append(page)
    pages.extend(loose)
    for folder in sorted(d for d in HELP_DIR.iterdir() if d.is_dir() and d.name != "media"):
        section = _help_slug_part(folder.name)
        if not _HELP_SLUG_RE.match(section):
            continue
        group = _help_section_title(folder.name)
        for path in sorted(folder.glob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            leaf = _help_slug_part(path.stem)
            if not _HELP_SLUG_RE.match(leaf):
                continue
            page = _help_page(path, f"{folder.name}/{path.name}", f"{section}/{leaf}", group, section)
            if page:
                pages.append(page)
    return pages


def help_url(slug: str) -> str:
    return HELP_BASE_URL if slug == "index" else f"{HELP_BASE_URL}/{slug}"


def _help_resolve(target: str, page_file: str, file_to_slug: Dict[str, str]) -> Optional[str]:
    """Portal URL for a link/media target written relative to `page_file`
    (a path relative to docs/partner), or None to leave it untouched."""
    if not target or re.match(r"^(?:[a-z][a-z0-9+.-]*:|/|#)", target, re.I):
        return None  # absolute URL, site path, mailto:, in-page anchor
    path, _, frag = target.partition("#")
    frag = f"#{frag}" if frag else ""
    # Media is always under docs/partner/media; accept media/…, ./media/…
    # and ../media/… (what a subfolder page needs to render on GitHub).
    m = re.match(r"^(?:\./)?(?:\.\./)*media/(.+)$", path)
    if m:
        return f"{HELP_MEDIA_URL}/{m.group(1)}"
    if not path.lower().endswith(".md"):
        return None
    parts = page_file.split("/")[:-1]
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    resolved = "/".join(parts)
    slug = file_to_slug.get(resolved)
    if slug is None and "/" not in path:
        # Bare file name from anywhere (e.g. "08-test-your-agent.md"): match
        # it when it is unique across the Help.
        hits = [s for f, s in file_to_slug.items() if f.rsplit("/", 1)[-1] == path]
        slug = hits[0] if len(hits) == 1 else None
    return help_url(slug) + frag if slug is not None else None


_HELP_MD_LINK_RE = re.compile(r"(!?\[[^\]]*\]\()(<[^>]*>|[^)\s]+)((?:\s+\"[^\"]*\")?\))")
_HELP_ATTR_RE = re.compile(r"""(\b(?:src|poster|href)=)(["'])([^"']*)\2""")


def help_rewrite_links(markdown: str, page_file: str, file_to_slug: Dict[str, str]) -> str:
    """Rewrite relative page links and media paths in a Help page's markdown
    (both markdown syntax and raw HTML attributes) into portal URLs."""
    def md(m: "re.Match[str]") -> str:
        raw = m.group(2)
        target = raw[1:-1] if raw.startswith("<") else raw
        url = _help_resolve(target, page_file, file_to_slug)
        return f"{m.group(1)}{url}{m.group(3)}" if url else m.group(0)

    def attr(m: "re.Match[str]") -> str:
        url = _help_resolve(m.group(3), page_file, file_to_slug)
        return f"{m.group(1)}{m.group(2)}{url}{m.group(2)}" if url else m.group(0)

    out: List[str] = []
    fenced = False
    for line in markdown.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        out.append(line if fenced or line.lstrip().startswith("```")
                   else _HELP_ATTR_RE.sub(attr, _HELP_MD_LINK_RE.sub(md, line)))
    return "".join(out)


def _render_help(request: Request, slug: str, ctx: dict) -> HTMLResponse:
    pages = help_pages()
    idx = next((i for i, p in enumerate(pages) if p["slug"] == slug), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Help page not found")
    page = pages[idx]
    try:
        markdown = (HELP_DIR / page["file"]).read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(status_code=404, detail="Help page not found")
    file_to_slug = {p["file"]: p["slug"] for p in pages}
    markdown = help_rewrite_links(markdown, page["file"], file_to_slug)
    groups: Dict[str, List[Dict[str, str]]] = {}
    for p in pages:
        groups.setdefault(p["group"], []).append(p)
    prev_page = pages[idx - 1] if idx > 0 else None
    next_page = pages[idx + 1] if idx + 1 < len(pages) else None
    return templates.TemplateResponse(
        request, "docs.html",
        {"ctx": ctx, "active": "help", "hide_env_switcher": True, "page": page,
         "groups": groups, "markdown": markdown,
         "link_map": {p["file"].rsplit("/", 1)[-1]: p["slug"] for p in pages},
         "docs_base": HELP_BASE_URL, "docs_label": "Help",
         "docs_subtitle": "Step-by-step guides for using the portal.",
         "show_api": False, "media_base": HELP_MEDIA_URL,
         "prev_page": prev_page, "next_page": next_page},
    )


@router.get("/help", response_class=HTMLResponse)
async def help_index(
    request: Request, ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    return _render_help(request, "index", ctx)


@router.get("/help/{slug}", response_class=HTMLResponse)
async def help_page(
    request: Request, slug: str, ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    return _render_help(request, slug, ctx)


@router.get("/help/{section}/{leaf}", response_class=HTMLResponse)
async def help_section_page(
    request: Request, section: str, leaf: str, ctx: dict = Depends(require_superuser),
) -> Response:
    if section == "media":
        # This route is registered before the /admin/help/media static mount
        # (main.py), so a two-segment media path lands here: serve it from
        # the media folder, confined to it.
        from fastapi.responses import FileResponse
        media_dir = (HELP_DIR / "media").resolve()
        target = (media_dir / leaf).resolve()
        if target.parent != media_dir or not target.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(target)
    return _render_help(request, f"{section}/{leaf}", ctx)


# ---------------------------------------------------------------------------
# Settings hub — one plain-language page that links to the screens that moved
# under Settings (Team & access, Releases, Environment variables, Voice
# budgets, Diagnostics, Developer docs). The screens keep their own URLs; the
# entries come from admin/nav.py, which drops the staff-only ones on a
# partner build.
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def settings_hub(
    request: Request, ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "settings.html",
        {"ctx": ctx, "active": "settings", "hide_env_switcher": True,
         "entries": portal_nav.settings_entries()},
    )


# ---------------------------------------------------------------------------
# Agents (was: Profiles)
# ---------------------------------------------------------------------------


@router.get("/agents", response_class=HTMLResponse)
async def agents_view(
    request: Request,
    env: str = Depends(current_env),
    tab: str = Query("agents"),
    kind: Optional[str] = Query(None, pattern=r"^[a-z_]+$"),
    profile_id: Optional[str] = Query(None),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    if tab == "models":
        # Models tab — the old /admin/agent-models screen, merged in.
        # ``build_models_context`` lives next to the models POST handlers
        # further down this module.
        return templates.TemplateResponse(
            request, "agents.html",
            {
                "ctx": ctx,
                "active": "agents",
                "current_env": env,
                "tab": "models",
                **build_models_context(env),
            },
        )

    # No agent picked → the Agents TABLE (it used to auto-open the first
    # agent, which hid the list and made the rail the only way to navigate).
    if not profile_id and not kind:
        return _render_agents_list(request, env, ctx)

    # `profile_id` wins over `kind` because the fork redirect lands the
    # operator on the new draft row by id (multiple rows may share a
    # kind once forks exist, so the kind selector alone can't pick the
    # right row).
    ctx_data = agents_screen.load_agents_context(env, kind, profile_id=profile_id)
    if not ctx_data.get("active_profile") or ctx_data.get("profile_missing"):
        # E.g. the env switcher flipped under an open agent: that agent is not
        # in this environment, so show the list with a notice rather than
        # silently opening some other agent.
        return _render_agents_list(request, env, ctx, missing=True)

    detail_tab = agents_views.normalize_tab(tab)
    active_id = ctx_data["active_profile"]["profile_id"]
    if detail_tab == "voice":
        # The Voice tab IS the voice settings screen (same header and tabs).
        return RedirectResponse(
            url=f"/admin/agents/{quote(active_id, safe='')}/voice?env={env}",
            status_code=302,
        )
    from admin._business_picker import fetch_businesses_for_picker

    view = agents_views.build_detail_view(ctx_data, env, ctx.get("user_id"))
    _ap = ctx_data["active_profile"]
    view["promote_counterpart"] = agents_views.promote_counterpart(
        (view.get("promote_target") or {}).get("env"), _ap.get("agent_kind"), _ap.get("slug"))
    return templates.TemplateResponse(
        request, "agent_detail.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": env,
            "tab": "agents",
            "detail_tab": detail_tab,
            "businesses": fetch_businesses_for_picker(),
            **ctx_data,
            **view,
        },
    )


def _render_agents_list(
    request: Request, env: str, ctx: dict, *, missing: bool = False,
) -> HTMLResponse:
    """The Agents table (/admin/agents with no agent picked)."""
    data = agents_views.load_agents_list(env, ctx.get("user_id"))
    return templates.TemplateResponse(
        request, "agents_list.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": env,
            "tab": "agents",
            "missing_profile": missing,
            **data,
        },
    )


# ---------------------------------------------------------------------------
# Back-compat redirects from the pre-reskin route table.
#
# Mappings:
#   /                       -> /admin/live
#   /profiles, /profiles/{} -> /admin/agents
#   /prompts,  /prompts/{}  -> /admin/agents
#   /data-sources           -> /admin/kb
#   /agent-data-sources     -> /admin/kb
#   /mcp-servers            -> /admin/tools
#   /agent-mcp-servers      -> /admin/tools
#   /observability(*)       -> /admin/live
# ---------------------------------------------------------------------------


@router.get("/profiles", include_in_schema=False)
async def _redirect_profiles() -> Response:
    return RedirectResponse(url="/admin/agents", status_code=302)


@router.get("/profiles/{profile_id}", include_in_schema=False)
async def _redirect_profile_detail(profile_id: str) -> Response:
    return RedirectResponse(url="/admin/agents", status_code=302)


@router.post("/profiles/{profile_id}")
async def profile_update(
    profile_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    payload: Dict[str, Any] = {}
    for field_name in (
        "display_name",
        "description",
        "environment",
        "status",
        "model_main_id",
        "model_light_id",
        "endpoint_path",
    ):
        value = form.get(field_name)
        if value is not None and value != "":
            payload[field_name] = value
    if "is_active" in form:
        payload["is_active"] = form.get("is_active") == "on"
    # Checkbox is always rendered by the mapping form (agents.html #status
    # card), so absent means the operator unchecked it — unconditional
    # assignment is correct (same semantics as models_update's is_active).
    payload["delegatable"] = form.get("delegatable") == "on"
    if "prompt_version_ids" in form:
        try:
            payload["prompt_version_ids"] = json.loads(form.get("prompt_version_ids") or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="prompt_version_ids must be valid JSON")
    # `voice_config` is deliberately NOT assembled here any more. The mapping
    # form's 26-key duplex fieldset was deleted 2026-08-26: it wrote the block
    # to whichever row the operator was VIEWING — on a voice specialist, a row
    # the runtime never reads — and it could only express a subset of the keys
    # the runtime honours. The single portal writer of `voice_config` is the
    # voice cockpit's POST (`/admin/agents/{id}/voice` below), which resolves
    # the OWNER row and owns every `_VOICE_FIELDS` key. A stray `live_*` field
    # in a scripted POST to this route is now ignored, never written.
    # Mapping form (agents.html) sends repeated checkbox inputs; absent
    # means "user unchecked all" and we must persist the empty list.
    # Legacy callers using a single comma-joined hidden field still work.
    if form.get("_mapping_form") == "1":
        for arr_field in ("tool_ids", "data_source_ids", "mcp_server_ids",
                          "downstream_profile_ids"):
            payload[arr_field] = [v for v in form.getlist(arr_field) if v]
    else:
        for arr_field in ("tool_ids", "data_source_ids", "mcp_server_ids",
                          "downstream_profile_ids"):
            values = [v for v in form.getlist(arr_field) if v]
            if len(values) > 1 or (values and "," not in values[0]):
                payload[arr_field] = values
            elif arr_field in form:
                raw = form.get(arr_field) or ""
                payload[arr_field] = [v for v in raw.split(",") if v]

    from admin.services.profiles import (DualActiveProfileError,
                                         ProfileNotFoundError,
                                         TemplateProtectedError,
                                         update_profile_row)
    try:
        update_profile_row(profile_id=profile_id, payload=payload,
                           actor_user_id=ctx["user_id"])
    except ProfileNotFoundError:
        raise HTTPException(status_code=404, detail="profile not found")
    except (ValueError, TemplateProtectedError, DualActiveProfileError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Land back on the profile that was just edited — a bare /admin/agents
    # re-picks the first active profile, bouncing the operator off their work.
    # profile_id is caller input: quote it so it stays ONE query value (a raw
    # `&` would smuggle extra query params through the Location header).
    return RedirectResponse(
        url=f"/admin/agents?profile_id={quote(profile_id, safe='')}&env={env}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Prompts — redirects only (GET handlers removed; POST kept below)
# ---------------------------------------------------------------------------


@router.get("/prompts", include_in_schema=False)
async def _redirect_prompts() -> Response:
    return RedirectResponse(url="/admin/agents", status_code=302)


@router.get("/prompts/{prompt_id}", include_in_schema=False)
async def _redirect_prompt_detail(prompt_id: str) -> Response:
    return RedirectResponse(url="/admin/agents", status_code=302)


@router.post("/agents/{profile_id}/instructions")
async def custom_agent_instructions_save(
    profile_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    content: str = Form(...),
    notes: str = Form(""),
    override_validation: bool = Form(False),
) -> Response:
    """Save a generic (`custom`) agent's instructions.

    A custom agent owns its prompt (admin/services/custom_prompts.py) rather
    than sharing its kind's slots, and one made from a template starts pinned
    to the template's text. This route makes sure the agent has its own prompt
    row — creating it, and moving the pin off the template, on the first save
    — then hands over to `prompt_version_create`, so validation, the
    "Save anyway" override, the audit trail and the pin repoint are the ONE
    implementation every other prompt save uses.
    """
    from admin.services import custom_prompts

    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_profiles").select("*").eq("profile_id", profile_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="profile not found")
    profile = rows[0]
    if profile.get("agent_kind") != custom_prompts.CUSTOM_AGENT_KIND:
        raise HTTPException(status_code=400, detail="only a custom agent has its own instructions")
    if profile.get("status") == "template":
        raise HTTPException(status_code=400, detail="templates are read-only")
    # Validate BEFORE creating anything, so a refused save leaves no empty
    # prompt row behind.
    from core.prompt_validator import has_errors, validate_prompt
    if has_errors(validate_prompt(content, name=None)) and not override_validation:
        return await prompt_version_create(
            prompt_id="", request=request, env=env, ctx=ctx, content=content, notes=notes,
            profile_id=profile_id, prompt_key=custom_prompts.SYSTEM_KEY,
            override_validation=False, voice_return_profile_id="",
        )
    prompt = custom_prompts.ensure_own_prompt(client, profile, actor_user_id=ctx["user_id"])
    return await prompt_version_create(
        prompt_id=prompt["prompt_id"], request=request, env=env, ctx=ctx, content=content,
        notes=notes, profile_id=profile_id, prompt_key=custom_prompts.SYSTEM_KEY,
        override_validation=override_validation, voice_return_profile_id="",
    )


@router.post("/prompts/{prompt_id}/versions")
async def prompt_version_create(
    prompt_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    content: str = Form(...),
    notes: str = Form(""),
    profile_id: str = Form(""),
    prompt_key: str = Form("system"),
    override_validation: bool = Form(False),
    voice_return_profile_id: str = Form(""),
) -> Response:
    """Create a new `agent_prompt_versions` row for `prompt_id`.

    When `profile_id` is supplied (the agents.html "Save prompt" form always
    sends the profile being edited — see the hidden inputs there), this ALSO
    repoints that profile's `prompt_version_ids[prompt_key]` pin at the
    freshly-inserted version, reusing the same repoint logic as the
    standalone `/profiles/{id}/serve-prompt-version` route below. Without
    this, saving a new prompt version was a silent no-op: the runtime serves
    whatever version is PINNED in `prompt_version_ids`, never "latest", so
    the edit would never actually be served.

    Prompts are shared across forks by `agent_kind` — repointing only the
    ONE profile whose editor this POST came from leaves every other fork's
    own pin untouched.

    `profile_id` omitted (back-compat for any other caller) → behaves
    exactly as before: create the version only, no repoint.

    **Gated by `core.prompt_validator.validate_prompt`** — the identical
    check `admin_mcp/tools_configure.py`'s `prompts` MCP tool runs before
    `create_version`/`serve`/`publish_from_repo` writes, so the portal and
    MCP paths can't drift into different safety bars. An error-severity
    problem (a size-budget breach, a missing/reverted bilingual half, or an
    `ask_agent` instruction worded descriptively instead of imperatively)
    refuses the save with 400 and nothing is written, unless
    `override_validation` (the "Save anyway" checkbox — see agents.html) is
    checked, in which case the write proceeds and the override is recorded
    via `record_change` (`action="validation_override"`) so it stays visible
    in the audit log.
    """
    from core.prompt_validator import has_errors, to_dicts, validate_prompt
    problems = validate_prompt(content, name=None)
    if has_errors(problems) and not override_validation:
        raise HTTPException(
            status_code=400,
            detail="Prompt failed validation and was NOT saved: "
                   + "; ".join(p.message for p in problems if p.severity == "error")
                   + " — check \"Save anyway\" to force it through "
                     "(recorded in the audit log).",
        )
    client = admin_service_client()
    if has_errors(problems) and override_validation:
        record_change(
            entity_type="agent_prompt_versions",
            entity_id=prompt_id,
            action="validation_override",
            actor_user_id=ctx["user_id"],
            after={"problems": to_dicts(problems), "prompt_id": prompt_id,
                  "profile_id": profile_id, "prompt_key": prompt_key},
        )
    last = _safe_list(
        client.table("agent_prompt_versions")
        .select("version_number")
        .eq("prompt_id", prompt_id)
        .order("version_number", desc=True)
        .limit(1)
        .execute()
    )
    next_number = (last[0]["version_number"] + 1) if last else 1
    inserted = client.table("agent_prompt_versions").insert(
        {
            "prompt_id": prompt_id,
            "version_number": next_number,
            "content": content,
            "notes": notes or None,
            "created_by": ctx["user_id"],
        }
    ).execute()
    new_row = inserted.data[0] if inserted.data else {}
    new_version_id = new_row.get("prompt_version_id")
    PromptResolver.invalidate()
    record_change(
        entity_type="agent_prompt_versions",
        entity_id=new_version_id or prompt_id,
        action="create",
        actor_user_id=ctx["user_id"],
        after=new_row,
    )

    # env for the return redirect: the profile row's own environment once it's
    # in hand (loaded below for the repoint), else the operator's current env.
    redirect_env = env
    if profile_id and new_version_id:
        before = _safe_list(
            client.table("agent_profiles")
            .select("agent_kind, environment, prompt_version_ids")
            .eq("profile_id", profile_id)
            .limit(1)
            .execute()
        )
        if before:
            row = before[0]
            redirect_env = row["environment"]
            pins = dict(row.get("prompt_version_ids") or {})
            pins[prompt_key] = new_version_id
            client.table("agent_profiles").update(
                {"prompt_version_ids": pins}
            ).eq("profile_id", profile_id).execute()
            AgentProfileLoader.invalidate(row["agent_kind"], row["environment"])
            PromptResolver.invalidate()
            record_change(
                entity_type="agent_profiles",
                entity_id=profile_id,
                action="serve_prompt_version",
                actor_user_id=ctx["user_id"],
                before={"prompt_version_ids": row.get("prompt_version_ids")},
                after={"prompt_version_ids": pins},
            )

    # The voice cockpit's persona editor posts through this same route (its
    # hidden profile_id is always the OWNER row — the only pin the runtime
    # reads). `voice_return_profile_id` sends the operator back to the cockpit
    # they came from instead of the Agents screen. Character-checked before it
    # lands in a Location header: it is form input, and superuser gating is
    # not a reason to interpolate untrusted text into a redirect. The
    # isinstance guard matters for direct (non-HTTP) callers, where the
    # unfilled default is FastAPI's Form sentinel, not a string.
    if isinstance(voice_return_profile_id, str) \
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", voice_return_profile_id):
        return RedirectResponse(
            url=f"/admin/agents/{voice_return_profile_id}/voice"
                f"?env={redirect_env}",
            status_code=302,
        )
    # Return to the profile whose editor this save came from (when known) so
    # the operator isn't bounced onto the first active profile. profile_id is
    # a raw form value here — quote it so a `&` inside it can't smuggle extra
    # query params through the Location header.
    if profile_id:
        return RedirectResponse(
            url=f"/admin/agents?profile_id={quote(profile_id, safe='')}"
                f"&env={redirect_env}",
            status_code=302,
        )
    return RedirectResponse(url=f"/admin/agents?env={redirect_env}", status_code=302)


@router.post("/profiles/{profile_id}/serve-prompt-version")
async def profile_serve_prompt_version(
    profile_id: str,
    ctx: dict = Depends(require_superuser),
    prompt_key: str = Form("system"),
    version_id: str = Form(...),
) -> Response:
    """Pin WHICH prompt version the profile's endpoint serves.

    Sets `agent_profiles.prompt_version_ids[prompt_key] = version_id`. The
    agent's `prepare()` reads this pin in dev/staging (prod serves the
    promoted snapshot). Invalidates the profile + prompt caches so the next
    request serves the chosen version immediately.

    Kept intentionally FUNCTIONAL (not gutted to a no-op) after Task 2.3
    (profile-version-lifecycle, phase P2) deleted the "Serve version"
    chip/dropdown that used to POST here from agents.html — grepping the
    templates confirms nothing links this route anymore, so it's back-compat
    only now: `tests/admin/test_serve_prompt_version.py` still exercises this
    exact pinning behaviour directly, and the admin MCP's `prompts` tool
    (`action="serve"`, `admin_mcp/tools_configure.py`) writes the same
    `prompt_version_ids` field via its own independent code path. Turning
    this into a redirect would silently break both without buying anything.
    """
    client = admin_service_client()
    before = _safe_list(
        client.table("agent_profiles")
        .select("agent_kind, environment, prompt_version_ids")
        .eq("profile_id", profile_id)
        .limit(1)
        .execute()
    )
    if not before:
        raise HTTPException(status_code=404, detail="profile not found")
    row = before[0]
    pins = dict(row.get("prompt_version_ids") or {})
    pins[prompt_key] = version_id
    client.table("agent_profiles").update(
        {"prompt_version_ids": pins}
    ).eq("profile_id", profile_id).execute()
    AgentProfileLoader.invalidate(row["agent_kind"], row["environment"])
    PromptResolver.invalidate()
    record_change(
        entity_type="agent_profiles",
        entity_id=profile_id,
        action="serve_prompt_version",
        actor_user_id=ctx["user_id"],
        before={"prompt_version_ids": row.get("prompt_version_ids")},
        after={"prompt_version_ids": pins},
    )
    return RedirectResponse(
        url=f"/admin/agents?profile_id={quote(profile_id, safe='')}"
            f"&env={row['environment']}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# KB (was: Data sources)
# ---------------------------------------------------------------------------


@router.get("/kb", response_class=HTMLResponse)
async def kb_screen(
    request: Request,
    env: str = Depends(current_env),
    kind: Optional[str] = Query(None, pattern=r"^[a-z_]*$"),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    kind = kind or None  # the filter <select> submits "" for "All agents"
    client = client_for(env)
    q = client.table("agent_data_sources").select("*").order("agent_kind").order("name")
    if kind:
        q = q.eq("agent_kind", kind)
    rows = _safe_list(q.execute())
    # Scoped build: hide other agents' data-source rows (their agent_kind chips
    # would expose the full catalog). The scope's own sources are tagged with
    # its kind, so they stay. No-op on a full build.
    from admin.agent_kinds import build_agent_kinds, build_kinds, scope_to_build

    # Generic (un-kinded) sources stay; another agent's do not.
    rows = scope_to_build(rows, keep_unkinded=True)
    # Filter chip bar + the create-form "Primary agent kind" dropdown: this
    # build's agents (admin/agent_kinds.py — never a partner's reserved
    # title/suggestions kinds; the full live list on the WAJ build).
    filter_kinds = list(build_agent_kinds())
    if build_kinds() is None:
        # A filter link for a kind outside the list (legacy rows) still works.
        for r in rows:
            k = r.get("agent_kind")
            if k and k not in filter_kinds:
                filter_kinds.append(k)
    _attach_kb_document_stats(client, rows)
    return templates.TemplateResponse(
        request, "kb.html",
        {"ctx": ctx, "active": "kb", "current_env": env,
         "rows": rows, "kind_filter": kind, "filter_kinds": filter_kinds,
         "kb_kinds": list(KB_KIND_LABELS)},
    )


def _attach_kb_document_stats(client, rows: List[Dict[str, Any]]) -> None:
    """Stamp each KB row with ``doc_count`` / ``last_upload_at`` / ``status``.

    "Never refreshed" used to show (as a warning) on every source, including
    upload-based ones where refreshing means nothing. Status is now per type:
    uploaded files → document count; synced sources (GitBook / Drive / URL)
    → last sync; a live database table → nothing to refresh.
    """
    counts: Dict[str, int] = {}
    last: Dict[str, str] = {}
    try:
        pages = _safe_list(
            client.table("ai_kb_pages").select("source_id, created_at").execute()
        )
    except Exception:  # noqa: BLE001 - stats are decoration, never block the list
        pages = None
    for p in pages or []:
        sid = p.get("source_id")
        if not sid:
            continue
        counts[sid] = counts.get(sid, 0) + 1
        ts = p.get("created_at") or ""
        if ts > last.get(sid, ""):
            last[sid] = ts
    for r in rows:
        sid = r.get("source_id")
        kind = r.get("kind")
        r["doc_count"] = counts.get(sid, 0) if pages is not None else None
        r["last_upload_at"] = last.get(sid)
        if kind in KB_SYNCED_KINDS:
            r["status"] = ("ok", "Synced") if r.get("last_refreshed_at") else ("warn", "Never synced")
        elif kind == "pgvector":
            if pages is None:
                r["status"] = ("neutral", "Uploaded files")
            elif r["doc_count"]:
                r["status"] = ("ok", "Ready")
            else:
                r["status"] = ("warn", "No documents yet")
        else:
            r["status"] = ("neutral", "Live table")


@router.get("/data-sources", include_in_schema=False)
async def _redirect_data_sources() -> Response:
    return RedirectResponse(url="/admin/kb", status_code=302)


@router.get("/agent-data-sources", include_in_schema=False)
async def _redirect_agent_data_sources() -> Response:
    return RedirectResponse(url="/admin/kb", status_code=302)


# ---------------------------------------------------------------------------
# Voice budgets — per-tenant daily duplex-voice audio-second allowances.
# The engine-wide default is VOICE_LIVE_DAILY_SECONDS; a row here overrides it
# for one business/merchant id (voice/live/budget.py::get_daily_limit).
#
# The standalone SCREEN was retired 2026-08-18 (spec D4): the allowance table,
# the set form and the "burning the default today" list now render as a card on
# every realtime `agent_models` row (`/admin/agent-models/{id}`), where an
# operator already is when they think about duplex-voice spend. The write
# routes below did NOT move — the card posts to them with a `next=` hop so the
# operator lands back on the model page — and the GET is now a redirect, so old
# bookmarks and the `docs/admin-portal/` links keep resolving.
# ---------------------------------------------------------------------------

#: Where a voice-budget write returns when the form named no `next=` (the
#: pre-D4 screen is gone, so the Models tab is the nearest live surface).
VOICE_BUDGETS_FALLBACK = "/admin/voice-budgets"


def safe_admin_next(candidate: Optional[str], fallback: str) -> str:
    """Return ``candidate`` only when it points back into this portal.

    A `next=` on an authenticated admin form is an open-redirect vector: the
    operator holds a superuser session cookie, and "the admin portal sent me
    here" is exactly the trust a phishing link wants to borrow. Every
    destination a portal form legitimately returns to is a path under
    `/admin/`, so that prefix IS the allow-list — no host parsing to get
    subtly wrong, and nothing off-site can satisfy it.

    Refused, each of which is a real bypass of a naive "starts with /" check:
    absolute URLs (``https://evil.example``); protocol-relative ``//evil.example``
    (a valid Location that leaves the site); backslash variants
    (``/\\evil.example`` — some browsers normalise the backslash to a slash);
    ``..`` segments, so the destination is still inside `/admin/` after the
    browser resolves it; and CR/LF/NUL, which is header splitting.
    """
    if not candidate:
        return fallback
    value = candidate.strip()
    if not value.startswith("/admin/"):
        return fallback
    if "\\" in value or ".." in value:
        return fallback
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        return fallback
    return value


def _redirect_with_error(url: str, message: str) -> RedirectResponse:
    """302 to ``url`` with ``?error=`` appended, preserving any query it has."""
    parts = urlsplit(url)
    query = f"error={quote_plus(message)}"
    if parts.query:
        query = f"{parts.query}&{query}"
    return RedirectResponse(
        url=urlunsplit(("", "", parts.path, query, parts.fragment)),
        status_code=302,
    )


def collect_voice_budgets(client) -> Dict[str, Any]:
    """Allowance rows + today's burn + the tenants running on the default.

    Shared by every screen that renders the budgets card — it is per *tenant*
    data, so the same payload is correct on any host screen.
    """
    from datetime import datetime, timezone

    from voice.live.budget import DEFAULT_DAILY_SECONDS

    rows = _safe_list(
        client.table("ai_voice_budgets").select("*").order("business_id").execute()
    )
    # Today's spend alongside each allowance, so the operator sees limit AND
    # burn in one place instead of cross-referencing two tables.
    today = datetime.now(timezone.utc).date().isoformat()
    usage_rows = _safe_list(
        client.table("ai_voice_usage_daily").select("business_id, seconds_used")
        .eq("usage_date", today).execute()
    )
    usage = {r["business_id"]: r.get("seconds_used") or 0 for r in usage_rows}
    for row in rows:
        row["used_today"] = usage.get(row["business_id"], 0)
    # Usage rows with NO budget row still matter — they show which tenants are
    # burning the shared default and probably deserve their own allowance.
    unbudgeted = [
        {"business_id": bid, "used_today": used}
        for bid, used in sorted(usage.items(), key=lambda kv: -kv[1])
        if bid not in {r["business_id"] for r in rows}
    ]
    return {
        "voice_budget_rows": rows,
        "voice_budget_unbudgeted": unbudgeted,
        "voice_budget_default_seconds": DEFAULT_DAILY_SECONDS,
    }


@router.get("/voice-budgets", response_class=HTMLResponse)
async def voice_budgets_screen(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Settings › Voice budgets — per-business daily voice allowances.

    The allowances are per tenant, so they get one page of their own instead
    of a copy of the same card on every realtime model page (2026-10-11; the
    model pages now link here). The write routes below are unchanged.
    """
    context: Dict[str, Any] = {
        "ctx": ctx, "active": "settings", "current_env": env,
        "error": request.query_params.get("error"),
    }
    try:
        context.update(collect_voice_budgets(client_for(env)))
    except Exception as exc:  # noqa: BLE001 - show the error state, not a 500
        logger.warning("voice budgets: load failed (%s)", exc)
        context["load_error"] = str(exc)
    return templates.TemplateResponse(request, "voice_budgets.html", context)


@router.post("/voice-budgets", response_class=Response)
async def voice_budgets_upsert(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    business_id: str = Form(...),
    daily_seconds: int = Form(...),
    note: str = Form(""),
    next_url: str = Form("", alias="next"),
) -> Response:
    import uuid as _uuid

    # `next` is operator-supplied, so it is validated (not trusted) before it
    # ever reaches a Location header — see safe_admin_next.
    destination = safe_admin_next(next_url, VOICE_BUDGETS_FALLBACK)
    try:
        bid = str(_uuid.UUID(business_id.strip()))
    except (ValueError, AttributeError):
        # Same derivation the voice edge applies to non-UUID merchant ids
        # (endpoints/voice_live.py::_budget_key), so an operator can paste the
        # merchant id they know and land on the same row the edge charges.
        from endpoints.voice_live import _ANON_BUDGET_NAMESPACE

        bid = str(_uuid.uuid5(
            _ANON_BUDGET_NAMESPACE, f"voice-live-business:{business_id.strip()}"
        ))
    if daily_seconds < 0:
        # The error goes back to wherever the form was, or the operator loses
        # the message to a screen they never opened.
        return _redirect_with_error(destination, "daily_seconds must be >= 0")
    from datetime import datetime, timezone

    client_for(env).table("ai_voice_budgets").upsert(
        {
            "business_id": bid,
            "daily_seconds": daily_seconds,
            "note": note.strip() or None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="business_id",
    ).execute()
    record_change(
        entity_type="ai_voice_budgets",
        entity_id=bid,
        action="upsert",
        actor_user_id=ctx["user_id"],
        after={"daily_seconds": daily_seconds, "note": note.strip() or None,
               "raw_business_id": business_id},
    )
    return RedirectResponse(url=destination, status_code=302)


@router.post("/voice-budgets/{business_id}/delete", response_class=Response)
async def voice_budgets_delete(
    business_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    next_url: str = Form("", alias="next"),
) -> Response:
    client_for(env).table("ai_voice_budgets").delete().eq(
        "business_id", business_id
    ).execute()
    record_change(
        entity_type="ai_voice_budgets",
        entity_id=business_id,
        action="delete",
        actor_user_id=ctx["user_id"],
    )
    return RedirectResponse(
        url=safe_admin_next(next_url, VOICE_BUDGETS_FALLBACK), status_code=302
    )


# ---------------------------------------------------------------------------
# Voice scenarios — DB-defined live-call acceptance runs (WS /voice/live).
# Definitions in ai_voice_scenarios, executions in ai_voice_scenario_runs;
# admin/voice_scenario_runner.py does the actual call.
# ---------------------------------------------------------------------------


@router.get("/voice-scenarios", response_class=HTMLResponse)
async def voice_scenarios_screen(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    client = client_for(env)
    scenarios = _safe_list(
        client.table("ai_voice_scenarios").select("*").order("slug").execute()
    )
    runs = _safe_list(
        client.table("ai_voice_scenario_runs").select("*")
        .order("started_at", desc=True).limit(25).execute()
    )
    # A partner build lists only its own agents' call scripts (and their runs).
    from admin.agent_kinds import build_kinds, scope_to_build

    if build_kinds() is not None:
        scenarios = scope_to_build(scenarios, also=())
        mine = {sc.get("slug") for sc in scenarios}
        runs = [r for r in runs if r.get("slug") in mine]
    return templates.TemplateResponse(
        request, "voice_scenarios.html",
        {"ctx": ctx, "active": "voice_scenarios", "current_env": env,
         "scenarios": scenarios, "runs": runs,
         "error": request.query_params.get("error")},
    )


@router.post("/voice-scenarios", response_class=Response)
async def voice_scenarios_upsert(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    slug: str = Form(...),
    agent_kind: str = Form(...),
    title: str = Form(""),
    ws_path: str = Form("/voice/live"),
    locale: str = Form(""),
    steps: str = Form("[]"),
    notes: str = Form(""),
    enabled: str = Form(""),
) -> Response:
    from admin.voice_scenario_runner import STEP_TYPES

    try:
        parsed_steps = json.loads(steps or "[]")
        assert isinstance(parsed_steps, list)
        for s in parsed_steps:
            assert isinstance(s, dict) and s.get("type") in STEP_TYPES, (
                f"each step needs a type in {STEP_TYPES}"
            )
    except Exception as exc:
        return RedirectResponse(
            url=f"/admin/voice-scenarios?error=steps+JSON+invalid%3A+{str(exc)[:80]}",
            status_code=302,
        )
    from datetime import datetime, timezone

    # PARTIAL upsert, matching the admin MCP's `voice_scenarios(action="set")`
    # (admin_mcp/tools_test.py), which drops `None`s from its payload.
    #
    # This used to be a FULL overwrite: every column the form did not carry a
    # value for was written as NULL. The screen has one shared "add scenario"
    # form and no per-row editor, so re-submitting an existing slug to change
    # its steps also blanked its title, locale and notes — a full overwrite
    # driven by a form that cannot express every column is how fields get
    # silently cleared (spec D11). Two writers disagreeing about it is worse
    # still: the same edit had different consequences depending on whether it
    # arrived over MCP or the portal.
    #
    # `enabled` is the one deliberate difference from MCP, and for the reason
    # `_LIVE_CHECKBOX_KEYS` exists: MCP's `enabled` is a tri-state
    # `Optional[bool]` because a partial call may not mention it, while this
    # form ALWAYS renders the checkbox — so absent means the operator unchecked
    # it, not "not offered". Dropping it here would make disabling a scenario
    # from the portal impossible.
    payload = {
        k: v for k, v in {
            "slug": slug.strip(),
            "agent_kind": agent_kind.strip() or None,
            "title": title.strip() or None,
            "ws_path": ws_path.strip() or None,
            "locale": locale.strip() or None,
            "steps": parsed_steps if (steps or "").strip() else None,
            "notes": notes.strip() or None,
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }.items() if v is not None
    }
    client_for(env).table("ai_voice_scenarios").upsert(
        payload, on_conflict="slug"
    ).execute()
    record_change(
        entity_type="ai_voice_scenarios", entity_id=payload["slug"],
        action="upsert", actor_user_id=ctx["user_id"], after=payload,
    )
    return RedirectResponse(url="/admin/voice-scenarios", status_code=302)


@router.post("/voice-scenarios/{slug}/run", response_class=Response)
async def voice_scenarios_run(
    slug: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    target: str = Form(""),
    headers_json: str = Form(""),
) -> Response:
    from admin._jobs import start_tracked_job
    from admin.voice_scenario_runner import run_scenario_job

    # Optional per-invocation HTTP/WS headers (e.g. a portal login cookie for
    # a deep protected-flow scenario) — never stored on the scenario row, the
    # same headers passthrough the admin MCP voice_scenarios `run` action
    # exposes, so the portal route isn't behind it. The UI form doesn't post
    # this field yet; it's here so a direct POST (or a future form field) can
    # supply it without another route change.
    headers: Optional[Dict[str, str]] = None
    if headers_json.strip():
        try:
            parsed = json.loads(headers_json)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            headers = {str(k): str(v) for k, v in parsed.items()}

    start_tracked_job(
        run_scenario_job,
        kind="other",
        label=f"voice-scenario:{slug}",
        actor_user_id=ctx["user_id"],
        related_entity_type="ai_voice_scenarios",
        related_entity_id=slug,
        metadata={"environment": env},
        slug=slug,
        environment=env,
        triggered_by=ctx["user_id"],
        target=target.strip() or None,
        headers=headers,
    )
    return RedirectResponse(url="/admin/voice-scenarios", status_code=302)


@router.post("/voice-scenarios/{slug}/delete", response_class=Response)
async def voice_scenarios_delete(
    slug: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    client_for(env).table("ai_voice_scenarios").delete().eq("slug", slug).execute()
    record_change(
        entity_type="ai_voice_scenarios", entity_id=slug,
        action="delete", actor_user_id=ctx["user_id"],
    )
    return RedirectResponse(url="/admin/voice-scenarios", status_code=302)


# ---------------------------------------------------------------------------
# Integrations (was: MCP servers) — merged into the /admin/tools MCP & Tools screen
# ---------------------------------------------------------------------------


@router.get("/integrations")
async def integrations_screen(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    """Merged into /admin/tools (MCP & Tools screen) — kept as a redirect so
    old bookmarks/links keep working. Query params (env) are carried over."""
    qs = str(request.query_params)
    url = "/admin/tools" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=url, status_code=307)


@router.get("/mcp-servers", include_in_schema=False)
async def _redirect_mcp_servers() -> Response:
    # Straight to the merged screen — no double hop through /admin/integrations.
    return RedirectResponse(url="/admin/tools", status_code=307)


@router.get("/agent-mcp-servers", include_in_schema=False)
async def _redirect_agent_mcp_servers() -> Response:
    # Straight to the merged screen — no double hop through /admin/integrations.
    return RedirectResponse(url="/admin/tools", status_code=307)


# The `_make_simple_crud` factory that used to live here (list + create for
# agent_data_sources / agent_mcp_servers) was removed 2026-08-26: its GET list
# views were unreachable — the redirects above (`/agent-data-sources` → /admin/kb,
# `/agent-mcp-servers` → /admin/tools) registered FIRST on the same paths — and
# its unvalidated POST creates had no remaining caller (only the two dead list
# templates posted there). The live, validated equivalents are
# `POST /admin/kb` (kb_handlers.kb_create) and `POST /admin/mcp/create` /
# `POST /admin/mcp/connect` (mcp_manage_handlers — reserved-name, endpoint and
# context_fields guards). tests/admin/test_route_registration.py now pins
# "no duplicate (method, path) registrations" so this class of dead route
# can't come back silently.

# ---------------------------------------------------------------------------
# Models — bespoke CRUD because we surface tuning knobs (top_p/cost/etc.)
# packed into `extra_config` JSON so we don't have to ALTER TABLE for every
# new provider parameter.
# ---------------------------------------------------------------------------

_MODEL_EXTRA_CONFIG_KEYS = (
    "top_p", "top_k",
    "frequency_penalty", "presence_penalty",
    "cost_per_1k_in", "cost_per_1k_out",
)

# Offered by the Models form. `vertex_ai`, `anthropic_vertex` and `vertex_maas`
# were removed 2026-08-16 along with their builders — they needed GCP Application
# Default Credentials, absent on Azure compute, so the form was offering three
# picks that could never build. Claude now goes through `anthropic` (direct API).
# `_read_model_form` separately validates against `SUPPORTED_PROVIDERS`
# (runtime/llm_factory), which is the real gate; this tuple only drives the UI.
_MODEL_PROVIDERS = (
    "google_genai",
    "openai", "anthropic", "azure_openai",
    "azure_voice_live",  # duplex realtime rows (voice/live/providers/azure_voice_live.py)
    "ollama",  # a local model server, over its OpenAI-compatible API
    "google",  # legacy alias for google_genai rows.
)


# Plain names for the provider slugs (the slug stays visible as a technical
# name; the select still posts the slug).
_MODEL_PROVIDER_LABELS = {
    "google_genai": "Google Gemini", "google": "Google (legacy)",
    "openai": "OpenAI", "anthropic": "Anthropic", "azure_openai": "Azure OpenAI",
    "azure_voice_live": "Azure Voice Live", "bedrock": "AWS Bedrock",
    "ollama": "Ollama",
}


def build_models_context(env: str) -> Dict[str, Any]:
    """Build the template context for the Models tab of /admin/agents.

    All envs visible by default — the table in the tab scopes the view
    client-side via Tabulator. Rows order primary by name then environment
    so siblings sit next to each other. Returns the data keys only — the
    caller adds request/ctx/active/current_env boilerplate.
    """
    client = client_for(env)
    rows = _safe_list(
        client.table("agent_models").select("*")
        .order("name").order("environment").execute()
    )
    # "Used by" column: which agents in this environment point at each model
    # (main, light, or the voice block's realtime model). Best-effort — a
    # failed lookup just leaves the column empty.
    usage: Dict[str, List[Dict[str, str]]] = {}
    try:
        prof_rows = _safe_list(
            client.table("agent_profiles")
            .select("profile_id, display_name, agent_kind, status, model_main_id, model_light_id, voice_config")
            .eq("environment", env).execute()
        )
        from admin.agent_kinds import in_build

        for pr in prof_rows:
            # Templates aren't agents; another build's agents aren't ours.
            if pr.get("status") == "template" or not in_build(pr.get("agent_kind")):
                continue
            vc = pr.get("voice_config") if isinstance(pr.get("voice_config"), dict) else {}
            ids = {pr.get("model_main_id"), pr.get("model_light_id"), (vc or {}).get("model_id")}
            for mid in ids - {None, ""}:
                usage.setdefault(mid, []).append({
                    "profile_id": pr["profile_id"],
                    "name": pr.get("display_name") or pr.get("agent_kind"),
                })
    except Exception as exc:  # noqa: BLE001
        logger.warning("models tab: usage lookup failed (%s)", exc)
    return {
        "rows": rows,
        "providers": _MODEL_PROVIDERS,
        "provider_labels": _MODEL_PROVIDER_LABELS,
        "extra_config_keys": _MODEL_EXTRA_CONFIG_KEYS,
        "model_usage": usage,
    }


@router.get("/agent-models", name="agent_models_list")
async def models_list(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    """Merged into /admin/agents (Models tab) — kept as a redirect so old
    bookmarks/links keep working. Query params (env) are carried over; the
    POST + detail sub-routes are unchanged."""
    qs = str(request.query_params)
    url = "/admin/agents?tab=models" + (f"&{qs}" if qs else "")
    return RedirectResponse(url=url, status_code=307)


@router.post("/agent-models")
async def models_create(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    payload = _read_model_form(form)
    client = admin_service_client()
    inserted = client.table("agent_models").insert(payload).execute()
    new_row = inserted.data[0] if inserted.data else {}
    record_change(
        entity_type="agent_models",
        entity_id=new_row.get("model_id", ""),
        action="create",
        actor_user_id=ctx["user_id"],
        after=new_row,
    )
    return RedirectResponse(url="/admin/agents?tab=models", status_code=302)


@router.post("/agent-models/{model_id}")
async def models_update(
    model_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    payload = _read_model_form(form)
    # is_active toggles arrive only when the checkbox is checked, so the
    # template includes a hidden marker so we can persist "unchecked".
    if form.get("_active_form") == "1":
        payload["is_active"] = form.get("is_active") == "on"
    client = admin_service_client()
    before = _safe_list(client.table("agent_models").select("*").eq("model_id", model_id).execute())
    if not before:
        raise HTTPException(404, "model not found")
    client.table("agent_models").update(payload).eq("model_id", model_id).execute()
    record_change(
        entity_type="agent_models",
        entity_id=model_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before=before[0],
        after={**before[0], **payload},
    )
    return RedirectResponse(url="/admin/agents?tab=models", status_code=302)


def _read_model_form(form) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for f in ("name", "provider", "model_string", "environment"):
        v = form.get(f)
        if v:
            payload[f] = v
    # Reject providers we can't actually build at runtime. The list in
    # `_MODEL_PROVIDERS` is the *advertised* set; the *buildable* set is
    # the union of llm_factory.SUPPORTED_PROVIDERS. Stopping unsupported
    # rows at save time keeps the runtime from silently routing to a
    # google fallback or raising at boot.
    provider = payload.get("provider")
    if provider:
        from runtime.llm_factory import SUPPORTED_PROVIDERS, UNSUPPORTED_PROVIDERS
        if provider in UNSUPPORTED_PROVIDERS:
            raise HTTPException(
                400,
                f"provider '{provider}' is not yet wired into the runtime. "
                f"Pick one of: {sorted(SUPPORTED_PROVIDERS)}.",
            )
        if provider not in SUPPORTED_PROVIDERS:
            raise HTTPException(
                400,
                f"unknown provider '{provider}'. Supported: {sorted(SUPPORTED_PROVIDERS)}.",
            )
    for f in ("temperature",):
        v = form.get(f)
        if v not in (None, ""):
            try:
                payload[f] = float(v)
            except ValueError:
                pass
    for f in ("max_output_tokens",):
        v = form.get(f)
        if v not in (None, ""):
            try:
                payload[f] = int(v)
            except ValueError:
                pass
    extra: Dict[str, Any] = {}
    for k in _MODEL_EXTRA_CONFIG_KEYS:
        v = form.get(k)
        if v not in (None, ""):
            try:
                extra[k] = float(v)
            except ValueError:
                extra[k] = v
    # Thinking controls — Gemini 3+ `thinking_level` (minimal/low/medium/high),
    # Gemini 2.5 integer `thinking_budget` (0 disables). Validated here so a typo
    # can't reach the SDK and fail every request; see
    # runtime.llm_factory._google_thinking_kwargs.
    level = (form.get("thinking_level") or "").strip().lower()
    if level in ("minimal", "low", "medium", "high"):
        extra["thinking_level"] = level
    tb = form.get("thinking_budget")
    if tb not in (None, ""):
        try:
            extra["thinking_budget"] = int(tb)
        except (TypeError, ValueError):
            pass
    # Pricing control: mode (auto|manual) + optional tokencost key override.
    mode = (form.get("pricing_mode") or "").strip().lower()
    if mode in ("auto", "manual"):
        extra["pricing_mode"] = mode
    tcm = (form.get("tokencost_model") or "").strip()
    if tcm:
        extra["tokencost_model"] = tcm
    # Where an OpenAI-compatible server lives (Ollama, vLLM, LM Studio, a
    # hosted catalog). It used to be reachable only by knowing to type
    # {"base_url": ...} into the raw JSON box below.
    base_url = (form.get("base_url") or "").strip()
    if base_url:
        if not base_url.lower().startswith(("http://", "https://")):
            raise HTTPException(400, "Base URL must start with http:// or https://")
        extra["base_url"] = base_url
    # Pass-through raw JSON pasted into the legacy textarea.
    if form.get("extra_config_raw"):
        try:
            extra.update(json.loads(form.get("extra_config_raw") or "{}"))
        except json.JSONDecodeError:
            raise HTTPException(400, "extra_config_raw must be valid JSON")
    if extra:
        payload["extra_config"] = extra
    return payload


_BUILTIN_MCP_PREFIX = "__builtin__:"


def mcp_display_name(name: Optional[str]) -> str:
    """Plain name for an MCP server row.

    Built-in bundles are stored as ``__builtin__:<agent_kind>`` — a code name
    a partner should never read as a title. They become "Tools for <agent>"
    (the agent's own display name); other names are humanised. The stored
    name stays visible as muted technical text where it matters.
    """
    raw = (name or "").strip()
    if raw.startswith(_BUILTIN_MCP_PREFIX):
        from admin.agent_kinds import agent_label

        return f"Tools for {agent_label(raw[len(_BUILTIN_MCP_PREFIX):])}"
    pretty = re.sub(r"[_\-]+", " ", raw).strip()
    return (pretty[:1].upper() + pretty[1:]) if pretty else raw


templates.env.globals["mcp_display_name"] = mcp_display_name


def _mcp_server_cards(env: Optional[str] = None) -> List[Dict[str, Any]]:
    """The configurable MCP servers (agent_mcp_servers) for the MCP tab. Each
    links to its editor so an operator can repoint the endpoint (e.g. mock →
    the client's real MCP). On any scoped build  keep only
    servers a visible profile references, mirroring the agents mapping editor."""
    try:
        # No explicit env → the request-scoped switcher env (falls back to the
        # local branch outside an admin request), keeping this read on the
        # same branch as the caller's other env-scoped queries.
        client = client_for(env) if env else admin_service_client()
        from admin._query_batch import run_parallel
        from core.deployment import scoped_build

        # servers + tool-membership + attachment counts are independent — fan
        # them out (plus the scoped-build profile probe, only when needed).
        tasks = {
            "servers": lambda: client.table("agent_mcp_servers").select("*").order("name").execute().data or [],
            "members": lambda: client.table("agent_mcp_tools").select("mcp_server_id").execute().data or [],
            "atts": lambda: client.table("agent_profile_mcp_servers").select("mcp_server_id, profile_id").execute().data or [],
        }
        if scoped_build():
            tasks["profs"] = lambda: client.table("agent_profiles").select(
                "profile_id, agent_kind, mcp_server_ids").execute().data or []
        batch = run_parallel(tasks)
        rows = batch["servers"]
        member_rows = batch["members"]
        att_rows = batch["atts"]
        if scoped_build():
            # Which agents use each server — via the junction table (how every
            # current profile attaches) AND the legacy mcp_server_ids column.
            # Reading only the legacy column hid every server on the agents
            # build (its profiles attach through the junction), which also
            # emptied the Tool catalog: its groups are per server.
            from admin.agent_kinds import mcp_server_in_build

            kind_by_profile = {p.get("profile_id"): p.get("agent_kind") for p in batch["profs"]}
            attached_kinds: Dict[str, set] = {}
            for p in batch["profs"]:
                for sid in p.get("mcp_server_ids") or []:
                    attached_kinds.setdefault(sid, set()).add(p.get("agent_kind"))
            for a in att_rows:
                kind = kind_by_profile.get(a.get("profile_id"))
                if kind:
                    attached_kinds.setdefault(a.get("mcp_server_id"), set()).add(kind)
            rows = [r for r in rows if mcp_server_in_build(r, attached_kinds=attached_kinds)]
        from collections import Counter
        member_counts = Counter(m.get("mcp_server_id") for m in member_rows)
        att_counts = Counter(a.get("mcp_server_id") for a in att_rows)
        # Template ("mold") profiles are blueprints, not agents anyone talks to.
        try:
            profiles = client.table("agent_profiles").select(
                "profile_id, display_name, agent_kind, environment, status"
            ).execute().data or []
        except Exception:  # noqa: BLE001 - "Used by" is decoration; never sink the list
            profiles = []
        from admin.agent_kinds import in_build

        prof_by_id = {
            p.get("profile_id"): p for p in profiles
            if p.get("status") != "template" and in_build(p.get("agent_kind"))
        }
        used_by: Dict[str, List[str]] = {}
        for a in att_rows:
            p = prof_by_id.get(a.get("profile_id"))
            if p:
                from admin.agent_kinds import _clean_label
                name = _clean_label(p.get("display_name") or "") or p.get("agent_kind") or ""
                lst = used_by.setdefault(a.get("mcp_server_id"), [])
                if name and name not in lst:
                    lst.append(name)
        for row in rows:
            sid = row.get("mcp_server_id")
            if row.get("source") == "bundle":
                row["tool_count"] = member_counts.get(sid, 0)
            else:
                row["tool_count"] = len(row.get("tool_subset") or []) or None
            row["attached_count"] = att_counts.get(sid, 0)
            row["used_by"] = used_by.get(sid, [])
            row["display_name"] = mcp_display_name(row.get("name"))
            row["type_label"] = (
                "Built-in" if row.get("is_builtin")
                else ("Your bundle" if row.get("source") == "bundle" else "External")
            )
        return rows
    except Exception:  # noqa: BLE001 - never block the tab on a DB hiccup
        return []


def _all_tool_rows(env: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every agent_tools row for the "All tools" table on /admin/tools.

    Includes deprecated rows (flagged in the table) — this is the catalog, and
    hiding a deprecated tool is how a stale prompt mention goes undiagnosed.
    On a scoped build, rows are narrowed to the visible agent kinds
    (primary or additional — the same `_kind_visible` rule the Agents mapping
    editor applies) so a tenant operator never sees WAJ's full catalog.
    Any failure degrades to [] rather than blocking the screen.
    """
    try:
        # Same env-resolution as _mcp_server_cards post-#153: an explicit env
        # routes through client_for, otherwise the request's admin env
        # (ContextVar) decides — never a bare get_service_client that would
        # read a different env than the screen displays.
        client = client_for(env) if env else admin_service_client()
        rows = (
            client.table("agent_tools").select(
                "tool_id, python_name, display_name, description, agent_kind, "
                "additional_agent_kinds, scope, kind, usage_guidance, is_deprecated"
            ).order("python_name").execute().data or []
        )
        from admin.agent_kinds import scope_to_build
        rows = scope_to_build(rows)
        # Label honestly even before the next boot sync rewrites the row: a
        # tool known to change data never reads as READ in the catalog.
        from admin.sync_tools import WRITE_TOOL_NAMES
        for r in rows:
            r["scope"] = "write" if (
                r.get("scope") == "write" or r.get("python_name") in WRITE_TOOL_NAMES
            ) else (r.get("scope") or "read")
        return rows
    except Exception:  # noqa: BLE001 - never block the screen on a DB hiccup
        return []


def build_integrations_context(env: Optional[str] = None) -> Dict[str, Any]:
    """Build the template context for the /admin/tools MCP & Tools screen.

    Three sections: (1) the configurable MCP servers (agent_mcp_servers),
    partitioned by source/is_builtin into mcp_builtin / mcp_bundles /
    mcp_external, each row enriched with tool_count + attached_count; (2) the
    flat `all_tools` catalog (every agent_tools row — the "All tools" table,
    which links each row to its /admin/tools/{tool_id} editor); (3) one
    card per visible integration registry entry, with an env-var presence
    probe. Scoped builds show only the platform deps; a full build the
    whole catalog. Returns the data keys only — the caller adds
    request/ctx/active/current_env boilerplate.
    """
    cards = []
    for it in visible_integrations():
        present = all(bool(os.getenv(v, "").strip()) for v in it.env_vars)
        cards.append({
            "name": it.name,
            "scope": it.scope,
            "location": it.location,
            "description": it.description,
            "env_vars": it.env_vars,
            "status": "ok" if present else "warn",
            "status_label": "Configured" if present else "Missing env",
        })
    servers = _mcp_server_cards(env)
    all_tools = _all_tool_rows(env)
    builtin = [s for s in servers if s.get("is_builtin")]
    bundles = [s for s in servers if not s.get("is_builtin") and s.get("source") == "bundle"]
    external = [s for s in servers if not s.get("is_builtin") and s.get("source") == "external"]
    return {
        "cards": cards,
        "mcp_builtin": builtin,
        "mcp_bundles": bundles,
        "mcp_external": external,
        # Internal group = operator bundles first, then seeder-managed built-ins.
        "mcp_internal": bundles + builtin,
        "all_tools": all_tools,
        # Tool catalog collapsed per server (a tool can sit in several).
        "tool_groups": _tool_groups(env, bundles + builtin, all_tools),
        "tool_agent_kinds": sorted({t.get("agent_kind") for t in all_tools if t.get("agent_kind")}),
    }


def _tool_groups(env: Optional[str], servers: List[Dict[str, Any]],
                 tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group the catalog by the bundle MCP that exposes each tool.

    Returns ``[{server, tools}]`` in server order, plus a trailing
    "Not in any server" group for tools no bundle exposes. External servers
    expose remote tools that are not catalog rows, so they have no group.
    """
    try:
        client = client_for(env) if env else admin_service_client()
        members = _safe_list(
            client.table("agent_mcp_tools").select("mcp_server_id, tool_id, position").execute()
        )
    except Exception:  # noqa: BLE001 - degrade to one flat group
        members = []
    by_id = {t.get("tool_id"): t for t in tools}
    shown_servers = {s.get("mcp_server_id") for s in servers}
    per_server: Dict[str, List[tuple]] = {}
    grouped: set = set()
    for m in members:
        t = by_id.get(m.get("tool_id"))
        # Membership in a server this screen doesn't show (another build's,
        # an external one) must not swallow the tool — it falls through to
        # "Not in any server" instead of disappearing from the catalog.
        if t is None or m.get("mcp_server_id") not in shown_servers:
            continue
        per_server.setdefault(m.get("mcp_server_id"), []).append((m.get("position") or 0, t))
        grouped.add(t.get("tool_id"))
    groups: List[Dict[str, Any]] = []
    for srv in servers:
        items = [t for _, t in sorted(per_server.get(srv.get("mcp_server_id"), []), key=lambda x: x[0])]
        if items:
            groups.append({"server": srv, "tools": items})
    rest = [t for t in tools if t.get("tool_id") not in grouped]
    if rest:
        groups.append({"server": None, "tools": rest})
    return groups


@router.get("/tools", response_class=HTMLResponse)
async def tools_list(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """The 'MCP & Tools' screen — one card per MCP (Internal = bundles + built-ins,
    External = connections), the flat "All tools" catalog table (every agent_tools
    row linking to its /admin/tools/{tool_id} editor — this is a catalog, NOT a
    binding: a tool only reaches an LLM via a profile's MCP-attachment subset),
    plus the platform-integrations credential grid. There is still no direct
    HTTP-tool creation entry point here (author those inside an Internal MCP)."""
    return templates.TemplateResponse(
        request, "tools_list.html",
        {
            "ctx": ctx,
            "active": "tools",
            "current_env": env,
            **build_integrations_context(env),
        },
    )


# ---------------------------------------------------------------------------
# Sessions (browse + replay)
# ---------------------------------------------------------------------------


# The standalone Sessions browser was retired: it duplicated the conversation
# the Observability explorer's Transcript tab now shows (backed by the same
# persisted ai_chat_messages + the trace's tool calls). All session paths
# redirect into the explorer — a session deep-links to its trace's Transcript.
def _redirect_session_to_trace(session_id: str, env: str) -> Response:
    """Resolve a chat session to its trace and deep-link the explorer
    Transcript; fall back to the explorer list when no trace is linked."""
    try:
        rows = _safe_list(
            client_for(env).table("agent_traces")
            .select("lmnr_trace_id")
            .eq("session_id", session_id)
            .not_.is_("lmnr_trace_id", "null")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        tid = rows[0].get("lmnr_trace_id") if rows else None
    except Exception:
        tid = None
    if tid:
        return RedirectResponse(
            f"/admin/observability/explorer#trace={tid}", status_code=302
        )
    return RedirectResponse("/admin/observability/explorer", status_code=302)


@router.get("/observability/sessions", include_in_schema=False)
async def sessions_list_redirect_obs() -> Response:
    return RedirectResponse("/admin/observability/explorer", status_code=302)


@router.get("/observability/sessions/{session_id}", include_in_schema=False)
async def session_detail_redirect_obs(
    session_id: str,
    env: str = Depends(current_env),
    _ctx: dict = Depends(require_superuser),
) -> Response:
    return _redirect_session_to_trace(session_id, env)


@router.get("/sessions", include_in_schema=False)
async def sessions_list_redirect() -> Response:
    return RedirectResponse("/admin/observability/explorer", status_code=302)


@router.get("/sessions/{session_id}", include_in_schema=False)
async def session_detail_redirect(
    session_id: str,
    env: str = Depends(current_env),
    _ctx: dict = Depends(require_superuser),
) -> Response:
    return _redirect_session_to_trace(session_id, env)


# ---------------------------------------------------------------------------
# Observability — native render of ClickHouse trace data inside admin
# ---------------------------------------------------------------------------


@router.get("/observability", response_class=HTMLResponse)
async def observability_list(
    request: Request,
    env: str = Depends(current_env_live),
    hours: int = Query(24, ge=1, le=8760),
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern=r"^(success|error)?$"),
    outcome: Optional[str] = Query(None, pattern=r"^(attention|handoff|ok)?$"),
    tool: Optional[str] = Query(None),
    channel_slug: Optional[str] = Query(None),
    agent_kind: Optional[str] = Query(None),
    group: str = Query("session", pattern=r"^(session|none)?$"),
    background: int = Query(0, ge=0, le=1),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Conversations — one row per conversation + the conversation viewer.
    Same page as /admin/observability/explorer (see
    observability_explorer_handlers.render_conversations)."""
    from fastapi.concurrency import run_in_threadpool
    from admin.observability_explorer_handlers import render_conversations

    return await run_in_threadpool(
        render_conversations, request, env=env, ctx=ctx, hours=hours,
        status=status, outcome=outcome, search=search, tool=tool,
        channel_slug=channel_slug, agent_kind=agent_kind, group=group,
        background=background,
    )


@router.get("/lmnr/diagnose", response_class=HTMLResponse)
async def lmnr_diagnose(
    request: Request,
    env: str = Depends(current_env_live),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """One-shot telemetry self-test — both ends of the pipeline.

    **Write side:** bootstrap status + a manual root span, so the operator can
    confirm spans reach the collector.

    **Read side:** a live ClickHouse probe. Every observability screen reads
    ClickHouse and nothing else, so its reachability IS the "can the portal show
    me traces?" answer. (This page used to report a Laminar *project id* instead;
    a project is a web-UI concept with no ClickHouse analogue, and resolving one
    required the session-cookie API the deployed build no longer serves.)
    """
    from core import _laminar_bootstrap

    bootstrap_ok = _laminar_bootstrap.is_initialized()
    bootstrap_error = _laminar_bootstrap.last_error()
    content_capture = _laminar_bootstrap.content_capture_enabled()
    dashboard_url = os.environ.get("LMNR_DASHBOARD_URL", "").rstrip("/")
    sdk_base_url = os.environ.get("LMNR_BASE_URL", "https://api.lmnr.ai")
    api_key_set = bool(os.environ.get("LMNR_PROJECT_API_KEY"))

    clickhouse_url = os.environ.get("LMNR_CLICKHOUSE_URL", "http://lmnr-clickhouse:8123")
    clickhouse_ok = False
    clickhouse_error: Optional[str] = None
    clickhouse_spans_24h: Optional[int] = None
    try:
        from core.lmnr_clickhouse import query as _ch_query

        rows = _ch_query(
            "SELECT count() AS spans FROM spans "
            "WHERE start_time >= now() - toIntervalHour(24)"
        ) or []
        clickhouse_ok = True
        if rows:
            try:
                clickhouse_spans_24h = int(rows[0].get("spans") or 0)
            except (TypeError, ValueError):
                clickhouse_spans_24h = None
    except Exception as exc:  # noqa: BLE001 - the probe's failure IS the result
        clickhouse_error = f"{type(exc).__name__}: {exc}"

    # Emit a manual test span. If bootstrap is healthy this should land in
    # ClickHouse within a couple seconds; if it doesn't, the failure is between
    # Laminar.initialize() and the OTLP exporter — either bad base_url / port,
    # or the app-server isn't reachable.
    test_trace_id: Optional[str] = None
    test_span_error: Optional[str] = None
    if bootstrap_ok:
        try:
            from lmnr import Laminar

            with Laminar.start_as_current_span(
                name="lmnr_diagnose_test_span",
                span_type="DEFAULT",
            ):
                Laminar.set_span_attributes({
                    "waj.diagnose.triggered_by": ctx.get("email", ""),
                    "waj.diagnose.env": env,
                })
                test_trace_id = str(Laminar.get_trace_id() or "") or None
        except Exception as exc:
            test_span_error = f"{type(exc).__name__}: {exc}"

    dashboard_signin = (
        f"{dashboard_url}/sign-in"
        if dashboard_url else None
    )

    return templates.TemplateResponse(
        request, "lmnr_diagnose.html",
        {
            "ctx": ctx,
            "active": "observability",
            "current_env": env,
            "bootstrap_ok": bootstrap_ok,
            "bootstrap_error": bootstrap_error,
            "content_capture": content_capture,
            "dashboard_url": dashboard_url,
            "dashboard_signin": dashboard_signin,
            "sdk_base_url": sdk_base_url,
            "api_key_set": api_key_set,
            "clickhouse_url": clickhouse_url,
            "clickhouse_ok": clickhouse_ok,
            "clickhouse_error": clickhouse_error,
            "clickhouse_spans_24h": clickhouse_spans_24h,
            "test_trace_id": test_trace_id,
            "test_span_error": test_span_error,
        },
    )


@router.get("/observability/{trace_id}")
async def observability_trace_detail(trace_id: str) -> Response:
    # The classic waterfall page was retired — the explorer renders trace
    # detail in its right pane via a hash deep-link. Redirect any old
    # bookmark / link straight into the explorer at the right trace.
    return RedirectResponse(
        url=f"/admin/observability/explorer#trace={trace_id}", status_code=302
    )


# ---------------------------------------------------------------------------
# Datasets — the dataset browse + import screens were retired (low value:
# datasets were rarely populated, and CSV import + suite-create + chat-based
# eval runs cover the need). Old paths redirect to the evals home.
# ---------------------------------------------------------------------------


@router.get("/datasets")
async def datasets_list_redirect() -> Response:
    return RedirectResponse(url="/admin/evals/suites", status_code=302)


@router.get("/datasets/{dataset_id}")
async def datasets_detail_redirect(dataset_id: str) -> Response:
    return RedirectResponse(url="/admin/evals/suites", status_code=302)


# (A `/observability/data` → /admin/live back-compat redirect used to sit here.
# It was registered AFTER the `/observability/{trace_id}` catch-all above, so it
# never actually matched — the catch-all sent it to the explorer with
# trace="data" — and nothing links or documents the path. Deleted 2026-08-26.)


# ---------------------------------------------------------------------------
# Eval runner
# ---------------------------------------------------------------------------


# Filter params of the old combined Suites & Runs page. A bookmarked
# /admin/evals?suite_id=… keeps working: it lands on the Runs tab with the
# same filters.
_RUN_FILTER_PARAMS = ("suite_id", "source", "agent_kind", "status", "days", "profile_id", "kind")


@router.get("/evals", response_class=HTMLResponse)
async def evals_list(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Testing › Overview — one health card per agent (verdict first)."""
    qp = request.query_params
    if any(qp.get(k) for k in _RUN_FILTER_PARAMS) or qp.get("tab") == "runs":
        return RedirectResponse(url=f"/admin/evals/runs?{qp}", status_code=307)
    if qp.get("tab") == "suites":
        return RedirectResponse(url="/admin/evals/suites", status_code=307)
    from admin.eval_runs_handlers import build_testing_overview_context

    return templates.TemplateResponse(
        request,
        "evals_overview.html",
        {
            "ctx": ctx,
            "active": "evals",
            "tab": "overview",
            "current_env": env,
            **build_testing_overview_context(),
        },
    )


def _evals_page(request: Request) -> Dict[str, Any]:
    from admin.eval_runs_handlers import build_evals_page_context

    qp = request.query_params
    return build_evals_page_context(**{k: qp.get(k, "") for k in _RUN_FILTER_PARAMS})


@router.get("/evals/suites", response_class=HTMLResponse)
async def evals_suites(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Testing › Test suites — every suite with its last result and a Run button."""
    return templates.TemplateResponse(
        request,
        "evals_list.html",
        {"ctx": ctx, "active": "evals", "tab": "suites", "current_env": env, **_evals_page(request)},
    )


@router.get("/evals/runs", response_class=HTMLResponse)
async def evals_runs(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Testing › Runs — every run (filters, compare, score trend, Run tests drawer)."""
    return templates.TemplateResponse(
        request,
        "evals_runs.html",
        {"ctx": ctx, "active": "evals", "tab": "runs", "current_env": env, **_evals_page(request)},
    )


@router.get("/evals/scheduling", response_class=HTMLResponse)
async def evals_scheduling(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Tab 2 of the /admin/evals surface: production monitoring — prod-chat
    schedules bound to a profile (Task 2.4). Indicators/trend land in 2.5."""
    from admin.eval_runs_handlers import build_scheduling_context

    page_ctx = build_scheduling_context()
    return templates.TemplateResponse(
        request,
        "evals_scheduling.html",
        {
            "ctx": ctx,
            "active": "evals",
            "tab": "scheduling",
            "current_env": env,
            **page_ctx,
        },
    )


@router.get("/evals/bakeoffs", response_class=HTMLResponse)
async def evals_bakeoffs(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Tab 3 of the /admin/evals surface: generic model/prompt/config
    bake-offs (agent_bakeoffs) — recent list + the create wizard
    (profile -> variants -> suites -> repeats/name). Detail view is
    GET /evals/bakeoffs/{bakeoff_id} below (registered after this static
    route and the POST create route, per the routing convention here)."""
    from admin.eval_runs_handlers import build_bakeoffs_context

    page_ctx = build_bakeoffs_context()
    return templates.TemplateResponse(
        request,
        "evals_bakeoffs.html",
        {
            "ctx": ctx,
            "active": "evals",
            "tab": "bakeoffs",
            "current_env": env,
            "error": None,
            "posted": {},
            **page_ctx,
        },
    )


def _parse_bakeoff_variants(form: Any) -> List[Dict[str, Any]]:
    """Parse the create-wizard's parallel per-variant form arrays into
    `variants=[{label, profile_version_id, model_id?, gen_config?}]`
    (the shape `admin.services.bakeoffs.create_bakeoff` expects).

    Field names are repeated (unbracketed) form keys, one value per variant
    row, kept in lockstep across every array by the wizard's client-side
    "+ Add variant" clone: `variant_label`, `variant_profile_version_id`,
    `variant_model_id`, `variant_temperature`, `variant_thinking_level`,
    `variant_max_output_tokens`, `variant_top_p`. Empty
    profile_version_id/model_id values are dropped rather than persisted as
    `""` — `create_bakeoff` (and the runner underneath it) treats an absent
    key as "no pin" / "no override", not an empty-string one. `gen_config`
    is built only from the keys actually provided; an all-empty gen-config
    row omits the key entirely (Task 4.2's MCP `bakeoffs` tool mirrors this
    exact parsing contract for its own `variants` input).
    """
    labels = form.getlist("variant_label")
    version_ids = form.getlist("variant_profile_version_id")
    model_ids = form.getlist("variant_model_id")
    temps = form.getlist("variant_temperature")
    thinking_levels = form.getlist("variant_thinking_level")
    max_tokens = form.getlist("variant_max_output_tokens")
    top_ps = form.getlist("variant_top_p")

    n = max(len(labels), len(version_ids), len(model_ids), 0)

    def _at(values: List[str], i: int) -> str:
        return (values[i] if i < len(values) else "") or ""

    variants: List[Dict[str, Any]] = []
    for i in range(n):
        label = _at(labels, i).strip() or f"variant {i + 1}"
        variant: Dict[str, Any] = {"label": label}

        version_id = _at(version_ids, i).strip()
        if version_id:
            variant["profile_version_id"] = version_id
        model_id = _at(model_ids, i).strip()
        if model_id:
            variant["model_id"] = model_id

        gen_config: Dict[str, Any] = {}
        temp = _at(temps, i).strip()
        if temp:
            try:
                gen_config["temperature"] = float(temp)
            except ValueError:
                pass
        level = _at(thinking_levels, i).strip().lower()
        if level in ("minimal", "low", "medium", "high"):
            gen_config["thinking_level"] = level
        mot = _at(max_tokens, i).strip()
        if mot:
            try:
                gen_config["max_output_tokens"] = int(mot)
            except ValueError:
                pass
        top_p = _at(top_ps, i).strip()
        if top_p:
            try:
                gen_config["top_p"] = float(top_p)
            except ValueError:
                pass
        if gen_config:
            variant["gen_config"] = gen_config

        variants.append(variant)
    return variants


@router.post("/evals/bakeoffs")
async def evals_bakeoffs_create(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Create a bake-off from the wizard: spawns
    `len(variants) x len(suite_ids) x repeats` linked eval runs and redirects
    to the detail page. A `ValueError` from `create_bakeoff` (< 2 variants,
    no suites, missing judge model on a suite, a variant's pinned version not
    belonging to the profile) re-renders the wizard with the error instead of
    a 500 — mirrors the `_err` re-render pattern used by the Connect-MCP /
    bundle-create forms (`admin/mcp_manage_handlers.py`)."""
    from admin.eval_runs_handlers import build_bakeoffs_context
    from admin.services.bakeoffs import create_bakeoff

    form = await request.form()
    name = (form.get("name") or "").strip()
    profile_id = (form.get("profile_id") or "").strip()
    suite_ids = [s for s in form.getlist("suite_ids") if s]
    try:
        repeats = int(form.get("repeats") or 1)
    except (TypeError, ValueError):
        repeats = 1
    variants = _parse_bakeoff_variants(form)

    def _err(msg: str) -> Response:
        page_ctx = build_bakeoffs_context()
        return templates.TemplateResponse(
            request,
            "evals_bakeoffs.html",
            {
                "ctx": ctx,
                "active": "evals",
                "tab": "bakeoffs",
                "current_env": env,
                "error": msg,
                "posted": {"name": name, "profile_id": profile_id, "repeats": repeats},
                **page_ctx,
            },
            status_code=200,
        )

    if not profile_id:
        return _err("Choose an agent.")

    try:
        result = create_bakeoff(
            name=name,
            profile_id=profile_id,
            variants=variants,
            suite_ids=suite_ids,
            repeats=repeats,
            actor_user_id=ctx.get("user_id"),
            user_token=ctx.get("user_token"),
        )
    except ValueError as exc:
        return _err(str(exc))

    return RedirectResponse(
        url=f"/admin/evals/bakeoffs/{result['bakeoff_id']}", status_code=303
    )


@router.get("/evals/bakeoffs/{bakeoff_id}", response_class=HTMLResponse)
async def evals_bakeoff_detail(
    bakeoff_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    suite_id: str = Query(""),
) -> HTMLResponse:
    """Detail view of one bake-off: the metric matrix (aggregated across
    every suite, or focused on one via `?suite_id=`), winner highlighting
    per metric, and the chart mount point Task 3.4 wires up. Registered
    AFTER the static /evals/bakeoffs routes above."""
    from admin.eval_runs_handlers import build_bakeoff_detail_context

    if not _is_uuid(bakeoff_id):  # a malformed id used to 500 inside postgrest
        raise HTTPException(status_code=404, detail="model comparison not found")
    try:
        page_ctx = build_bakeoff_detail_context(bakeoff_id, suite_focus=suite_id or None)
    except ValueError:
        raise HTTPException(status_code=404, detail="model comparison not found")

    return templates.TemplateResponse(
        request,
        "evals_bakeoff_detail.html",
        {
            "ctx": ctx,
            "active": "evals",
            "tab": "bakeoffs",
            "current_env": env,
            **page_ctx,
        },
    )


@router.get("/evals/bakeoffs/{bakeoff_id}/data.json")
async def evals_bakeoff_data_json(
    bakeoff_id: str,
    suite_id: str = Query(""),
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Bake-off matrix as `{status, matrix}` JSON — fed to
    `evals-bakeoff-chart.js` for the grouped-bar + cost-vs-quality charts on
    the detail page, and polled every ~4s until `status` settles
    (complete/errored). `?suite_id=` narrows the matrix to one suite exactly
    like the HTML detail route above (forwarded as `suite_focus=`).

    Distinct path suffix (`/data.json`) from `/evals/bakeoffs/{bakeoff_id}`
    above, so route order between the two doesn't matter — a 3-segment
    request can't match the 2-segment HTML route's pattern. Registered right
    after that HTML route, still after the static /evals/bakeoffs routes.
    """
    from admin.services.bakeoffs import get_bakeoff

    if not _is_uuid(bakeoff_id):
        raise HTTPException(status_code=404, detail=f"bake-off {bakeoff_id} not found")
    try:
        result = get_bakeoff(bakeoff_id, suite_focus=suite_id or None)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"bake-off {bakeoff_id} not found")

    return JSONResponse(
        {"status": result.get("status"), "matrix": result.get("matrix")}
    )


@router.post("/evals/run")
async def evals_run(
    request: Request,
    ctx: dict = Depends(require_superuser),
    suite_id: str = Form(...),
    profile_id: str = Form(...),
    ctx_business_id: str = Form(""),
    ctx_branch_id: str = Form(""),
    ctx_customer_id: str = Form(""),
    ctx_json: str = Form(""),
) -> Response:
    client = admin_service_client()
    # Build the per-run context dict from form inputs. Empty strings are
    # dropped so the eval runner only sees keys the operator actually
    # filled in. `ctx_json` is a textarea escape hatch for fields we
    # haven't enumerated (screen_context, previous_messages, etc.).
    run_context: Dict[str, Any] = {}
    if ctx_business_id.strip():
        run_context["business_id"] = ctx_business_id.strip()
    if ctx_branch_id.strip():
        run_context["branch_id"] = ctx_branch_id.strip()
    if ctx_customer_id.strip():
        run_context["customer_id"] = ctx_customer_id.strip()
    from admin.eval_context import run_context_from_form

    run_context.update(run_context_from_form(await request.form()))
    if ctx_json.strip():
        try:
            extra = json.loads(ctx_json)
            if not isinstance(extra, dict):
                raise ValueError("ctx_json must be a JSON object")
            run_context.update(extra)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                400,
                f"ctx_json could not be parsed as a JSON object: {exc}",
            )

    run = client.table("agent_eval_runs").insert(
        {
            "suite_id": suite_id,
            "profile_id": profile_id,
            "status": "queued",
            "triggered_by": ctx["user_id"],
            "context": run_context,
        }
    ).execute()
    run_id = run.data[0]["run_id"]
    # Fire the run on a daemon thread so the HTTP handler can redirect
    # immediately. Real-LLM scenarios take 10-60s each — blocking the
    # request would freeze the admin tab until the whole suite finished.
    # The run row's `status` transitions queued → running → passed/failed
    # so the /admin/evals list reflects progress without the user
    # holding a stale tab open.
    record_change(
        entity_type="agent_eval_runs",
        entity_id=run_id,
        action="run",
        actor_user_id=ctx["user_id"],
    )
    # Forward the operator's Supabase JWT to the background runner so
    # the agent endpoints validate against a real session (otherwise
    # auth_user returns 401 "Invalid token: Not enough segments").
    # Wrapped in start_tracked_job so the run shows up at /admin/jobs
    # with status transitions, error capture, and operator cancel.
    from admin._jobs import start_tracked_job

    start_tracked_job(
        run_eval_suite_sync,
        kind="eval_run",
        label=f"eval:{run_id[:8]}",
        actor_user_id=ctx["user_id"],
        related_entity_type="agent_eval_runs",
        related_entity_id=run_id,
        metadata={"suite_id": suite_id, "profile_id": profile_id},
        run_id=run_id,
        user_token=ctx["user_token"],
    )
    return RedirectResponse(url=f"/admin/evals/runs/{run_id}", status_code=302)


def _voice_run_detail(request: Request, ctx: dict, env: str,
                      voice: Dict[str, Any]) -> HTMLResponse:
    """Run detail for a voice check — same verdict-first layout as a text run,
    with the call's steps as the cases."""
    from admin.eval_handlers import build_case_rows
    from admin.eval_runs_handlers import profile_label
    from admin.testing_view import duration as _tv_duration

    run, prev = voice["run"], voice["previous"]
    profile_index: Dict[str, Dict[str, Any]] = {}
    if run.get("profile_id"):
        prow = _safe_list(
            client_for(env).table("agent_profiles").select("profile_id, display_name, slug")
            .eq("profile_id", run["profile_id"]).limit(1).execute()
        )
        profile_index = {p["profile_id"]: p for p in prow if p.get("profile_id")}
    return templates.TemplateResponse(
        request, "voice_run_detail.html",
        {"ctx": ctx, "active": "evals", "current_env": env,
         "run": run, "prev_run": prev, "scenario": voice["scenario"],
         "steps": voice["steps"], "transcript": voice["transcript"],
         "cases": build_case_rows(run.get("results") or [], (prev or {}).get("results")),
         "agent_name": profile_label(run.get("profile_id"), profile_index)
         if run.get("profile_id") else "the agent",
         "run_duration": _tv_duration(run.get("started_at"), run.get("finished_at"))},
    )


@router.get("/evals/runs/{run_id}", response_class=HTMLResponse)
async def evals_run_detail(
    run_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    if not _is_uuid(run_id):  # a malformed id used to 500 inside postgrest
        raise HTTPException(status_code=404, detail="test run not found")
    client = client_for(env)
    rows = _safe_list(
        client.table("agent_eval_runs").select("*").eq("run_id", run_id).execute()
    )
    if not rows:
        # A voice check's run shares the Runs list (admin/voice_checks.py),
        # so its link lands here too.
        from admin.voice_checks import load_voice_run

        voice = load_voice_run(client, run_id)
        if voice is None:
            raise HTTPException(status_code=404, detail="test run not found")
        return _voice_run_detail(request, ctx, env, voice)
    run = rows[0]

    # Pull the suite-level trace + scenario child spans from ClickHouse so the
    # detail view can render per-scenario timing inline (vs. the link-out
    # we used before). Best-effort: when ClickHouse is down, lmnr_spans stays
    # empty and the template falls through to the existing pass/fail list.
    lmnr_spans: List[Dict[str, Any]] = []
    if run.get("lmnr_trace_id"):
        from admin.screens.observability import load_spans_for_trace

        lmnr_spans = load_spans_for_trace(run["lmnr_trace_id"])
    # Sibling runs of the same suite — used by the "Compare to…" picker
    # at the top of the detail page so the operator can diff two runs
    # without typing run_ids by hand.
    # Live-chat runs have no suite_id (NULL), so they have no suite-siblings.
    # Querying .eq("suite_id", None) serializes to the string "None" and 500s
    # on the uuid cast — skip the lookup entirely for suite-less runs.
    sibling_runs: List[Dict[str, Any]] = []
    if run.get("suite_id"):
        sibling_runs = _safe_list(
            client.table("agent_eval_runs")
            .select("run_id, status, score, finished_at, created_at, profile_id, suite_id, "
                    "bakeoff_id, context")
            .eq("suite_id", run["suite_id"])
            .neq("run_id", run_id)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
    # Resolve the in-flight tracker row (if any) so the template can render
    # the operator's Cancel button. Only relevant while running — cancel is
    # a no-op against terminal states and the template guards on it too.
    cancel_job_id: Optional[str] = None
    if run.get("status") == "running":
        job_rows = _safe_list(
            client.table("admin_jobs")
            .select("job_id")
            .eq("related_entity_type", "agent_eval_runs")
            .eq("related_entity_id", run_id)
            .eq("status", "running")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if job_rows:
            cancel_job_id = str(job_rows[0]["job_id"])
    from admin.eval_handlers import (
        build_case_rows,
        group_results_by_scenario,
        run_chart_data,
        run_detail_kpis,
    )
    from admin.eval_runs_handlers import _comparable, profile_label
    from admin.testing_view import duration as _tv_duration

    # Verdict context: the suite (name + target), the agent's name, and the
    # previous comparable run of the same suite + agent (for "−3 pts since
    # last run", "Compare with previous" and which cases broke / got fixed).
    suite: Dict[str, Any] = {}
    if run.get("suite_id"):
        srows = _safe_list(
            client.table("agent_eval_suites")
            .select("suite_id, name, pass_threshold, runner_kind, profile_id")
            .eq("suite_id", run["suite_id"]).limit(1).execute()
        )
        suite = srows[0] if srows else {}
    profile_index: Dict[str, Dict[str, Any]] = {}
    if run.get("profile_id"):
        prow = _safe_list(
            client.table("agent_profiles").select("profile_id, display_name, slug")
            .eq("profile_id", run["profile_id"]).limit(1).execute()
        )
        profile_index = {p["profile_id"]: p for p in prow if p.get("profile_id")}
    prev_run: Optional[Dict[str, Any]] = None
    if _comparable(run):
        for sib in sibling_runs:
            if str(sib.get("created_at") or "") >= str(run.get("created_at") or ""):
                continue
            if sib.get("profile_id") != run.get("profile_id"):
                continue
            if _comparable(sib):
                prev_run = sib
                break
    prev_results = None
    if prev_run:
        prow = _safe_list(
            client.table("agent_eval_runs").select("run_id, results")
            .eq("run_id", prev_run["run_id"]).limit(1).execute()
        )
        prev_results = (prow[0].get("results") if prow else None) or []

    results = run.get("results")
    # Link each case attempt to the agent's own trace (the model's messages,
    # tool calls and output), via the `eval_ref` the runner sent as a header.
    # Runs from before the correlation existed carry no refs.
    eval_traces_unavailable = False
    if isinstance(results, list) and any(isinstance(r, dict) and r.get("eval_ref") for r in results):
        from admin.screens.observability import eval_trace_ids

        by_ref = eval_trace_ids(run_id, run.get("started_at") or run.get("created_at"))
        if by_ref is None:
            eval_traces_unavailable = True
        else:
            for r in results:
                if isinstance(r, dict) and r.get("eval_ref"):
                    r["_trace_ids"] = by_ref.get(str(r["eval_ref"]).lower(), [])
    target = None
    if suite:
        from admin.testing_view import threshold_of
        target = threshold_of(suite)
    return templates.TemplateResponse(
        request, "eval_run_detail.html",
        {
            "ctx": ctx,
            "active": "evals",
            "current_env": env,
            "suite": suite,
            "target": target,
            "agent_name": profile_label(run.get("profile_id"), profile_index),
            "agent_missing": bool(run.get("profile_id")) and not profile_index,
            "prev_run": prev_run,
            "cases": build_case_rows(results, prev_results),
            "run_duration": _tv_duration(run.get("started_at"), run.get("finished_at")),
            "show": request.query_params.get("show", ""),
            "run": run,
            "run_kpis": run_detail_kpis(results),
            # Minimal, JSON-safe projection for the charts (the full results dump
            # embedded raw newlines that broke JSON.parse and blanked canvases).
            "chart_data": run_chart_data(results),
            # Per-scenario grouping so repeated attempts (pass^k) render together.
            "scenario_groups": group_results_by_scenario(results),
            "lmnr_spans": lmnr_spans,
            "eval_traces_unavailable": eval_traces_unavailable,
            "sibling_runs": sibling_runs,
            "cancel_job_id": cancel_job_id,
        },
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@router.get("/audit")
async def audit_list(
    request: Request,
    page: int = Query(1, ge=1),
    entity_type: Optional[str] = Query(None),
    who: str = Query("", max_length=64),
    what: str = Query("", max_length=32),
    when: str = Query("", max_length=8),
    format: Optional[str] = Query(None, pattern=r"^(html|json)$"),
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Activity — the audit log as sentences, with Who / What / When chips.

    `?entity_type=` (the old free-text filter) still works; `?format=json`
    exports the filtered rows (up to 1,000) with the sentence attached.
    """
    from admin.screens import govern

    client = client_for(env)
    me = ctx.get("user_id")
    page_size = 50

    def _filtered(select: str = "*", count: Optional[str] = None):
        q = client.table("agent_audit_log").select(select, count=count) if count else \
            client.table("agent_audit_log").select(select)
        return govern.apply_activity_filters(q, who=who, what=what, when=when,
                                             entity_type=entity_type, me=me)

    if format == "json":
        rows = _safe_list(_filtered().order("created_at", desc=True).limit(1000).execute())
    else:
        try:
            res = (_filtered(count="exact").order("created_at", desc=True)
                   .range((page - 1) * page_size, page * page_size - 1).execute())
            rows, total = res.data or [], getattr(res, "count", None)
        except Exception as exc:  # noqa: BLE001 — render an error state, never 500
            logger.warning("audit: list failed (%s)", exc)
            rows, total = [], None
    main = None
    try:
        main = get_main_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit: main client unavailable (%s)", exc)

    # Recent distinct actors → the "Who" chips (named people, newest first).
    recent_actor_ids: List[str] = []
    for r in _safe_list(client.table("agent_audit_log").select("actor_user_id")
                        .order("created_at", desc=True).limit(500).execute()):
        a = r.get("actor_user_id") if isinstance(r, dict) else None
        if a and a not in recent_actor_ids:
            recent_actor_ids.append(a)
    people = govern.resolve_people(
        main, [r.get("actor_user_id") for r in rows] + recent_actor_ids
        + [r.get("entity_id") for r in rows if r.get("entity_type") == "user"])
    entities = govern.resolve_entities(client, rows)
    versions = govern.promote_versions(client, rows)
    items = [govern.describe_row(r, people=people, entities=entities, versions=versions, me=me)
             for r in rows]

    if format == "json":
        return JSONResponse([
            {**r, "summary": govern.sentence_text(i), "actor_name": i["actor"]["label"],
             "entity_name": i["name"]}
            for r, i in zip(rows, items)
        ])

    who_chips = [("", "Anyone"), ("me", "You"), ("system", "Automated")]
    for a in recent_actor_ids:
        p = people.get(str(a)) or {}
        if p.get("is_system") or str(a) == str(me) or len(who_chips) >= 9:
            continue
        who_chips.append((str(a), p.get("label") or str(a)[:8]))
    if who and govern._is_uuid(who) and who not in [c[0] for c in who_chips]:
        who_chips.append((who, (people.get(who) or {}).get("label") or govern.resolve_people(main, [who])[who]["label"]))

    return templates.TemplateResponse(
        request, "audit_list.html",
        {"ctx": ctx, "active": "audit", "current_env": env, "hide_env_switcher": True,
         "rows": rows, "items": items, "page": page, "page_size": page_size, "total": total,
         "entity_type": entity_type, "who": who, "what": what, "when": when,
         "who_chips": who_chips,
         "what_chips": [("", "Everything")] + [(k, v[0]) for k, v in govern.WHAT_FILTERS.items()],
         "when_chips": [(k, v[0]) for k, v in govern.WHEN_FILTERS.items()],
         "action_variant": AUDIT_ACTION_VARIANT},
    )


@router.get("/audit/{audit_id}", response_class=HTMLResponse)
async def audit_detail(
    audit_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """One activity entry: the sentence, who/what/when, a readable
    before/after diff, and the raw JSON behind a disclosure."""
    from admin.screens import govern

    if not _is_uuid(audit_id):
        raise HTTPException(404, "audit entry not found")
    client = client_for(env)
    rows = _safe_list(
        client.table("agent_audit_log").select("*").eq("audit_id", audit_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "audit entry not found")
    row = rows[0]
    main = None
    try:
        main = get_main_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit: main client unavailable (%s)", exc)
    ids = [row.get("actor_user_id")] + ([row.get("entity_id")] if row.get("entity_type") == "user" else [])
    people = govern.resolve_people(main, ids)
    item = govern.describe_row(row, people=people, entities=govern.resolve_entities(client, [row]),
                               versions=govern.promote_versions(client, [row]), me=ctx.get("user_id"))
    return templates.TemplateResponse(
        request, "audit_detail.html",
        {"ctx": ctx, "active": "audit", "current_env": env, "hide_env_switcher": True,
         "row": row, "entry": item,
         "changes": govern.field_changes(
             row.get("before"), row.get("after"),
             version_labels=govern.prompt_version_labels(client, row.get("before"), row.get("after"))),
         "action_variant": AUDIT_ACTION_VARIANT},
    )


@router.post("/audit/verify")
async def audit_verify(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    record_change(
        entity_type="agent_audit_log",
        entity_id="00000000-0000-0000-0000-000000000000",
        action="CALLEE_VERIFY",
        actor_user_id=ctx["user_id"],
    )
    return RedirectResponse(url="/admin/audit", status_code=302)


# ---------------------------------------------------------------------------
# Users & Roles
# ---------------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
async def users_screen(
    request: Request,
    env: str = Depends(current_env),
    service: bool = Query(False),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Team & access — one table of people with the Admin flag.

    "Admin" is the portal's superuser bit (`user.permission.is_superuser`
    in the main DB) — the only access level the portal has. Service / e2e
    accounts are hidden unless `?service=1`. The `user` table has no name
    columns, so people are identified by email (phone as the fallback).
    """
    from admin.screens import govern

    client = get_main_client()
    rows = _safe_list(
        client.table("user").select(
            "id, auth_id, email, phone_number, country_code, role, permission_level, permission, "
            "business_id, created_at"
        )
        .eq("is_deleted", False)
        .order("created_at", desc=True)
        .limit(500)
        .execute()
    )
    for r in rows:
        # Kept for older callers/tests of this context.
        r["display_label"] = govern.person_label(r)
        r["is_superuser"] = bool((r.get("permission") or {}).get("is_superuser"))
    # Last active = the later of the last recorded change (Activity) and, for
    # admins, the last portal sign-in (main project's auth admin API).
    last_seen = govern.merge_latest(
        govern.last_activity_by_actor(client_for(env), rows),
        govern.last_sign_in(client, [r for r in rows if r["is_superuser"]]),
    )
    people = govern.build_team(rows, last_seen, ctx.get("user_id"))
    service_count = sum(1 for p in people if p["is_service"])
    shown = people if service else [p for p in people if not p["is_service"]]
    return templates.TemplateResponse(
        request, "users.html",
        {"ctx": ctx, "active": "users", "current_env": env, "hide_env_switcher": True,
         "rows": rows, "superusers": [r for r in rows if r["is_superuser"]],
         "people": shown, "admin_count": sum(1 for p in shown if p["is_admin"]),
         "service_count": service_count, "show_service": service},
    )


@router.post("/users/{user_id}/superuser")
async def user_toggle_superuser(
    user_id: str,
    grant: str = Form(...),
    return_service: str = Form(""),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Flip the is_superuser flag in the user.permission jsonb column."""
    client = get_main_client()
    rows = _safe_list(client.table("user").select("permission").eq("id", user_id).limit(1).execute())
    if not rows:
        raise HTTPException(404, "user not found")
    perm = (rows[0].get("permission") or {})
    perm["is_superuser"] = grant == "true"
    client.table("user").update({"permission": perm}).eq("id", user_id).execute()
    record_change(
        entity_type="user", entity_id=user_id,
        action="GRANT_SUPERUSER" if perm["is_superuser"] else "REVOKE_SUPERUSER",
        actor_user_id=ctx["user_id"],
        after={"is_superuser": perm["is_superuser"]},
    )
    # Keep the "Show service accounts" view when the change was made there.
    back = "/admin/users?service=1" if return_service == "1" else "/admin/users"
    return RedirectResponse(url=back, status_code=302)


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


@router.get("/env", response_class=HTMLResponse)
async def env_screen(
    request: Request,
    env: str = Depends(current_env),
    tab: str = Query("promote"),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    client = client_for(env)
    if tab == "variables":
        # Variables tab — the env-vars registry (the old standalone
        # /admin/env-vars screen, merged in). Lazy import: env_vars_handlers
        # imports from admin.router at module load. Returns
        # {"staging": [...], "prod": [...]} with secrets masked.
        from admin.env_vars_handlers import fetch_env_vars_by_env

        return templates.TemplateResponse(
            request, "env.html",
            {"ctx": ctx, "active": "env", "current_env": env,
             "tab": "variables", "hide_env_switcher": True,
             "env_vars": fetch_env_vars_by_env()},
        )

    # Promote tab (default): one overview grid — per agent: live version,
    # unpublished changes, last quality score, Promote. The source column is
    # staging and the target prod; each side reads through its own env client
    # (on the control-plane they can be different branch DBs).
    data = _release_overview_data()
    return templates.TemplateResponse(
        request, "env.html",
        {"ctx": ctx, "active": "env", "current_env": env,
         "tab": "promote", "hide_env_switcher": True,
         "releases": data["rows"], "labels": data["labels"],
         "changed_count": sum(1 for r in data["rows"] if r["has_changes"] and r["can_promote"]),
         "only_changed": request.query_params.get("changed") == "1",
         "q": request.query_params.get("q", ""),
         "promoted": request.query_params.get("promoted"),
         "error": request.query_params.get("error")},
    )


def _release_overview_data(profile_id: Optional[str] = None) -> Dict[str, Any]:
    """Promote overview rows (all agents, or just `profile_id`'s)."""
    from admin.screens import govern

    src_env, tgt_env = "staging", "prod"
    src_client, tgt_client = client_for(src_env), client_for(tgt_env)
    q = src_client.table("agent_profiles").select("*").eq("environment", src_env)
    if profile_id:
        q = q.eq("profile_id", profile_id)
    src_rows = _safe_list(q.order("agent_kind").execute())
    tgt_q = tgt_client.table("agent_profiles").select("*").eq("environment", tgt_env)
    if profile_id and src_rows:
        tgt_q = tgt_q.eq("agent_kind", src_rows[0].get("agent_kind"))
    tgt_rows = _safe_list(tgt_q.order("agent_kind").execute())
    if profile_id:
        key = (src_rows[0].get("agent_kind"), src_rows[0].get("slug")) if src_rows else None
        tgt_rows = [t for t in tgt_rows if (t.get("agent_kind"), t.get("slug")) == key]
    eval_status = {p["profile_id"]: _latest_eval_for_profile(src_client, p) for p in src_rows
                   if p.get("status") != "template"}
    main = None
    try:
        main = get_main_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("releases: main client unavailable (%s)", exc)
    rows = govern.release_rows(src_client=src_client, tgt_client=tgt_client, src_rows=src_rows,
                               tgt_rows=tgt_rows, eval_status=eval_status, main_client=main)
    return {"rows": rows, "labels": govern.env_labels(single_env_deployment()),
            "src_rows": src_rows, "src_client": src_client}


@router.get("/env/promote/preview")
async def env_promote_preview(
    profile_id: str = Query(...),
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """What promoting `profile_id` would change — read by the promote dialog
    before it asks for confirmation. Also runs the two gates the promote
    enforces (quality score, prompt-vs-tools) so the dialog can ask for a
    reason up front instead of failing after the click."""
    from admin.services.promotion import _prompt_mandated_unbound_tools

    if not _is_uuid(profile_id):
        raise HTTPException(404, "agent not found")
    data = _release_overview_data(profile_id)
    rows = [r for r in data["rows"] if r.get("profile_id") == profile_id]
    if not rows:
        raise HTTPException(404, "agent not found")
    row = rows[0]
    missing_tools: List[str] = []
    try:
        missing_tools = _prompt_mandated_unbound_tools(data["src_client"], data["src_rows"][0])
    except Exception as exc:  # noqa: BLE001 — a check hiccup must not block the preview
        logger.warning("promote preview: tool check failed (%s)", exc)
    needs_reason = bool(row["quality"].get("blocks") or missing_tools)
    return JSONResponse({
        "profile_id": profile_id, "name": row["name"], "labels": data["labels"],
        "live": row["live"], "is_new": row["is_new"], "changes": row["changes"],
        "quality": row["quality"], "missing_tools": missing_tools, "needs_reason": needs_reason,
    }, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Deployment targets (/admin/deployments)
# ---------------------------------------------------------------------------


@router.get("/deployments", response_class=HTMLResponse)
async def deployments_screen(
    request: Request,
    env: str = Depends(current_env),
    tab: str = Query("dispatch"),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    # Deploy-from-portal UI needs the GITHUB_DEPLOY_TOKEN status to know
    # whether the deploy buttons will work.
    from admin._runtime_env import collect_runtime_env

    runtime_env = collect_runtime_env()

    # Targets data is only needed on the Targets tab; skip the DB round-trip
    # on Dispatch and History tabs.
    rows: list = []
    if tab == "targets":
        client = client_for(env)
        rows = _safe_list(
            client.table("agent_deployment_targets").select("*")
            .order("environment").order("is_default", desc=True).order("label")
            .execute()
        )

    return templates.TemplateResponse(
        request, "deployments.html",
        {"ctx": ctx, "active": "deployments", "current_env": env, "hide_env_switcher": True, "rows": rows,
         "runtime_env": runtime_env, "tab": tab},
    )


@router.post("/deployments")
async def deployment_create(
    request: Request,
    environment: str = Form(..., pattern=r"^(staging|prod)$"),
    label: str = Form(...),
    base_url: str = Form(...),
    auth_env_var: str = Form(""),
    description: str = Form(""),
    is_default: str = Form(""),
    ctx: dict = Depends(require_superuser),
) -> Response:
    client = admin_service_client()
    payload = {
        "environment": environment, "label": label, "base_url": base_url,
        "auth_env_var": auth_env_var or None, "description": description or None,
        "is_default": is_default == "on",
    }
    if payload["is_default"]:
        # Only one default per env — clear the flag on siblings first.
        client.table("agent_deployment_targets").update({"is_default": False}).eq(
            "environment", environment
        ).execute()
    inserted = client.table("agent_deployment_targets").insert(payload).execute()
    new_id = inserted.data[0]["target_id"] if inserted.data else None
    record_change(
        entity_type="agent_deployment_targets", entity_id=new_id or "",
        action="create", actor_user_id=ctx["user_id"], after=payload,
    )
    return RedirectResponse(url="/admin/deployments?tab=targets", status_code=302)


@router.post("/deployments/{target_id}/delete")
async def deployment_delete(
    target_id: str,
    ctx: dict = Depends(require_superuser),
) -> Response:
    client = admin_service_client()
    client.table("agent_deployment_targets").delete().eq("target_id", target_id).execute()
    record_change(
        entity_type="agent_deployment_targets", entity_id=target_id,
        action="delete", actor_user_id=ctx["user_id"],
    )
    return RedirectResponse(url="/admin/deployments?tab=targets", status_code=302)


@router.post("/deployments/{target_id}")
async def deployment_update(
    target_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    payload: Dict[str, Any] = {}
    for field_name in ("environment", "label", "base_url", "auth_env_var", "description"):
        v = form.get(field_name)
        if v is not None:
            payload[field_name] = v or None
    payload["is_default"] = form.get("is_default") == "on"
    payload["is_active"] = form.get("is_active") == "on"
    client = admin_service_client()
    before = _safe_list(client.table("agent_deployment_targets").select("*").eq("target_id", target_id).execute())
    if not before:
        raise HTTPException(404, "deployment target not found")
    if payload.get("is_default"):
        client.table("agent_deployment_targets").update({"is_default": False}).eq(
            "environment", payload.get("environment") or before[0]["environment"]
        ).execute()
    client.table("agent_deployment_targets").update(payload).eq("target_id", target_id).execute()
    record_change(
        entity_type="agent_deployment_targets", entity_id=target_id,
        action="update", actor_user_id=ctx["user_id"],
        before=before[0], after={**before[0], **payload},
    )
    return RedirectResponse(url="/admin/deployments?tab=targets", status_code=302)


# ---------------------------------------------------------------------------
# Test viewer (/admin/tests) — runs pytest from the portal
# ---------------------------------------------------------------------------


@router.get("/tests", response_class=HTMLResponse)
async def tests_screen(
    request: Request,
    env: str = Depends(current_env),
    reveal: Optional[str] = Query(None),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    from admin.test_runner import list_suites
    from admin.test_fixtures_handlers import fetch_test_fixtures

    client = client_for(env)
    runs = _safe_list(
        client.table("agent_test_runs").select("*")
        .order("created_at", desc=True).limit(20).execute()
    )
    # Per-agent E2E runs are started from an agent's page; list them here too
    # so every engineering test result is reachable from one place.
    try:
        e2e_runs = _safe_list(
            client.table("agent_e2e_runs")
            .select("run_id, profile_id, environment, status, score, pass_count, fail_count, created_at, finished_at")
            .order("created_at", desc=True).limit(20).execute()
        )
        pids = sorted({r.get("profile_id") for r in e2e_runs if r.get("profile_id")})
        names = {
            p["profile_id"]: p.get("display_name")
            for p in (_safe_list(client.table("agent_profiles").select("profile_id, display_name")
                                 .in_("profile_id", pids).execute()) if pids else [])
            if p.get("profile_id")
        }
        for r in e2e_runs:
            r["_agent"] = names.get(r.get("profile_id")) or f"Unknown agent ({str(r.get('profile_id'))[:8]})"
    except Exception:  # pragma: no cover — never fail the page over the E2E list
        logger.exception("tests: e2e run list failed")
        e2e_runs = []
    return templates.TemplateResponse(
        request, "tests.html",
        {"ctx": ctx, "active": "tests", "current_env": env,
         "suites": list_suites(), "runs": runs, "e2e_runs": e2e_runs,
         # Test Fixtures vault folded into this screen (the standalone
         # /admin/test-fixtures list now redirects here).
         "test_fixtures": fetch_test_fixtures(client, env, reveal),
         "fixtures_export_url": f"/admin/test-fixtures/export?env={env}"},
    )


@router.get("/tests/{run_id}", response_class=HTMLResponse)
async def test_run_detail(
    run_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    if not _is_uuid(run_id):  # a malformed id used to 500 inside postgrest
        raise HTTPException(status_code=404, detail="test run not found")
    client = client_for(env)
    rows = _safe_list(
        client.table("agent_test_runs").select("*").eq("run_id", run_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="run not found")
    return templates.TemplateResponse(
        request, "test_run_detail.html",
        {"ctx": ctx, "active": "tests", "current_env": env, "run": rows[0]},
    )


@router.post("/tests/run")
async def tests_run(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Run one or more test suites sequentially. Accepts repeated
    `suites` form fields (multi-select checkbox UI on /admin/tests)."""
    from admin.test_runner import run_suite
    form = await request.form()
    suites = [s for s in form.getlist("suites") if s]
    if not suites and form.get("suite"):  # back-compat with single-suite POSTs
        suites = [form.get("suite")]
    for suite in suites:
        try:
            result = await asyncio.to_thread(run_suite, suite=suite, triggered_by=ctx["user_id"])
            logger.info(
                "tests: suite=%s result=%s", suite,
                {k: v for k, v in result.items() if k != "output"},
            )
        except Exception as exc:
            logger.exception("tests_run failed for %s: %s", suite, exc)
    return RedirectResponse(url="/admin/tests", status_code=302)


# ---------------------------------------------------------------------------
# E2E test runner
# ---------------------------------------------------------------------------


@router.get("/e2e/runs/{run_id}", response_class=HTMLResponse)
async def e2e_run_detail(
    run_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    if not _is_uuid(run_id):  # a malformed id used to 500 inside postgrest
        raise HTTPException(404, "E2E run not found")
    client = client_for(env)
    rows = _safe_list(
        client.table("agent_e2e_runs").select("*").eq("run_id", run_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "E2E run not found")
    run = rows[0]
    # E2E jobs track by profile_id (run_e2e mints its own run_id inside the
    # worker, so the tracker row can't reference it). One run is in flight
    # per profile at a time — the trigger button is disabled while running —
    # so the newest running job for this profile is the right one to cancel.
    cancel_job_id: Optional[str] = None
    if run.get("status") == "running" and run.get("profile_id"):
        job_rows = _safe_list(
            client.table("admin_jobs")
            .select("job_id")
            .eq("related_entity_type", "agent_profiles")
            .eq("related_entity_id", run["profile_id"])
            .eq("status", "running")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if job_rows:
            cancel_job_id = str(job_rows[0]["job_id"])
    return templates.TemplateResponse(
        request, "e2e_run_detail.html",
        {"ctx": ctx, "active": "agents", "current_env": env, "run": run,
         "cancel_job_id": cancel_job_id},
    )


# ---------------------------------------------------------------------------
# Duplex voice — the ONE cockpit (spec D2, extended 2026-08-26).
#
# The call's settings live on TWO rows: the PARENT (active chat profile) holds
# every `_VOICE_FIELDS` key (see `voice/live/config.py` — the count is
# `len(_VOICE_FIELDS)`, don't hand-write it here; a hardcoded "38" sat in this
# comment while the registry held 40), the persona pin and the frozen realtime
# model; the SPECIALIST named by `voice_config.agent_profile_slug` is the
# brain — its `model_main_id`, its system-prompt pin, its bound tools, its
# `memory_config`. The decision: keep two rows, present ONE screen. The
# Agents mapping form's 26-key duplex fieldset is deleted — it wrote to
# whichever row was being VIEWED, and on a specialist that row is read by
# nothing.
#
# Two writers, each pinned to its row:
#   * POST /agents/{id}/voice        → voice_config on the OWNER (resolved from
#     the DB by slug, not from the Agents rail's filtered list — the single
#     portal writer of that column).
#   * POST /agents/{id}/voice/brain  → model_main_id / memory_config on the
#     BRAIN row named in the path (the cockpit's brain panel embeds the
#     resolved specialist's id).
# ---------------------------------------------------------------------------


@router.get("/agents/{profile_id}/voice", response_class=HTMLResponse)
async def agent_voice_view(
    request: Request,
    profile_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    from admin.screens import agent_voice

    ctx_data = agent_voice.load_voice_context(env, profile_id)
    if ctx_data is None:
        raise HTTPException(status_code=404, detail="profile not found")
    # The voice settings ARE the agent's Voice tab: same header + tab strip.
    # Degrades to {} (the page then shows a plain back link) on any failure.
    header = agents_views.load_header_view(env, profile_id, ctx.get("user_id"))
    return templates.TemplateResponse(
        request, "agent_voice.html",
        {"ctx": ctx, "active": "agents", "current_env": env,
         "detail_tab": "voice", **header, **ctx_data},
    )


@router.post("/agents/{profile_id}/voice", response_class=Response)
async def agent_voice_update(
    profile_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    from admin.screens import agent_voice
    from admin.services.profiles import (DualActiveProfileError,
                                         ProfileNotFoundError,
                                         TemplateProtectedError,
                                         update_profile_row)

    form = await request.form()
    existing = (
        admin_service_client().table("agent_profiles").select("voice_config")
        .eq("profile_id", profile_id).limit(1).execute().data or []
    )
    voice_config = agent_voice.voice_config_from_form(
        form, existing=(existing[0].get("voice_config") if existing else None),
    )
    if voice_config is None:
        # No `_voice_form=1` marker: this submission did not come from the voice
        # screen, so it owns nothing here. Silently writing the form's absent
        # checkboxes would turn a stray POST into a config change — the exact
        # failure Part B input #6 records against the mapping form's gate.
        raise HTTPException(status_code=400, detail="not a voice-screen submission")
    try:
        update_profile_row(
            profile_id=profile_id, payload={"voice_config": voice_config},
            actor_user_id=ctx["user_id"],
        )
    except ProfileNotFoundError:
        raise HTTPException(status_code=404, detail="profile not found")
    except (ValueError, TemplateProtectedError, DualActiveProfileError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Return to the profile the operator was LOOKING at, which on a specialist
    # is not the one just saved. Character-checked before it goes into a
    # Location header: it is form input, and `../` or `//evil.example` in a
    # redirect target is an open redirect however superuser-gated the route is.
    landing = str(form.get("return_profile_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", landing):
        landing = profile_id
    return RedirectResponse(
        url=f"/admin/agents/{landing}/voice?env={env}", status_code=303,
    )


@router.post("/agents/{profile_id}/voice/brain", response_class=Response)
async def agent_voice_brain_update(
    profile_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """The cockpit's brain panel — writes to the SPECIALIST row.

    `profile_id` is the BRAIN's id (the cockpit embeds the resolved
    specialist's id in the form action — or the owner's own id when
    `agent_profile_slug` names no separate row, matching the runtime's
    fallback to the kind's active profile). This is the whole point of the
    panel: `model_main_id` and `memory_config` are read off the brain at
    request time, so a write landing on the owner instead would look applied
    and change nothing. `voice_config` is never touched here — the voice
    form above stays that column's single writer.
    """
    from admin.screens import agent_voice
    from admin.services.profiles import (DualActiveProfileError,
                                         ProfileNotFoundError,
                                         TemplateProtectedError,
                                         update_profile_row)

    form = await request.form()
    existing = (
        admin_service_client().table("agent_profiles").select("memory_config")
        .eq("profile_id", profile_id).limit(1).execute().data or []
    )
    payload = agent_voice.brain_update_from_form(
        form,
        existing_memory_config=(existing[0].get("memory_config") if existing else None),
    )
    if payload is None:
        # No `_brain_form=1` marker: this POST cannot prove the brain panel
        # was rendered, so it owns nothing here — writing its absent memory
        # checkbox would turn a stray request into a config change. Same
        # refuse-don't-guess posture as the voice form's gate above.
        raise HTTPException(status_code=400, detail="not a brain-panel submission")
    try:
        update_profile_row(
            profile_id=profile_id, payload=payload, actor_user_id=ctx["user_id"],
        )
    except ProfileNotFoundError:
        raise HTTPException(status_code=404, detail="profile not found")
    except (ValueError, TemplateProtectedError, DualActiveProfileError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Back to the cockpit the operator was looking at (the OWNER's screen when
    # they opened the parent, the specialist's when they opened that) — same
    # character check as the voice form's redirect, same open-redirect reason.
    landing = str(form.get("return_profile_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", landing):
        landing = profile_id
    return RedirectResponse(
        url=f"/admin/agents/{landing}/voice?env={env}", status_code=303,
    )


@router.post("/agents/{profile_id}/e2e/run")
async def agents_e2e_run(
    profile_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    from admin.e2e_runner import run_e2e

    # Background thread for the same reason as /evals/run: a real E2E
    # against an LLM-backed endpoint can take minutes. The row in
    # `agent_e2e_runs` transitions running → passed/failed so the
    # admin/agents view reflects status without blocking the redirect.
    from admin._jobs import start_tracked_job

    start_tracked_job(
        run_e2e,
        kind="eval_run",
        label=f"e2e:{profile_id[:8]}/{env}",
        actor_user_id=ctx["user_id"],
        related_entity_type="agent_profiles",
        related_entity_id=profile_id,
        metadata={"environment": env},
        profile_id=profile_id,
        environment=env,
        triggered_by=ctx["user_id"],
    )
    # Keep the operator on the profile whose E2E they just triggered.
    return RedirectResponse(
        url=f"/admin/agents?profile_id={quote(profile_id, safe='')}&env={env}",
        status_code=302,
    )


@router.post("/agents/{profile_id}/versions")
async def agents_save_version(
    profile_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    notes: str = Form(""),
) -> Response:
    """Snapshot the profile's current (live/draft) config into a brand new
    `agent_profile_versions` row for the active environment — a checkpoint
    the operator can later inspect, diff, or pin. Does not touch
    `served_version_id` (see `admin.services.profile_versions.save_version`).
    """
    from admin.services.profile_versions import save_version

    save_version(profile_id, env, actor_user_id=ctx["user_id"], notes=notes or None)
    return RedirectResponse(
        url=f"/admin/agents?profile_id={profile_id}", status_code=303,
    )


@router.post("/agents/{profile_id}/pin-version")
async def agents_pin_version(
    profile_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
    version_id: str = Form(""),
) -> Response:
    """Pin the `agent_profile_versions` snapshot `AgentProfileLoader` serves
    for this profile in the active environment — or, with an empty
    `version_id`, clear the pin back to default resolution. See
    `admin.services.profile_versions.pin_served_version` for the
    version-belongs-to-profile guard this wraps.
    """
    from admin.services.profile_versions import pin_served_version

    try:
        pin_served_version(
            profile_id, env, version_id or None, actor_user_id=ctx["user_id"],
        )
    except ValueError as exc:
        # Same convention as profile_mcp_attach_handlers._err /
        # profile_skill_attach_handlers._err: redirect back to the Agents
        # screen with `?error=` for the template to surface, no write.
        return RedirectResponse(
            url=f"/admin/agents?profile_id={profile_id}&error={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/admin/agents?profile_id={profile_id}", status_code=303,
    )


@router.get("/agents/{profile_id}/versions/{version_id}/diff", response_class=HTMLResponse)
async def agents_version_diff(
    profile_id: str,
    version_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Diff a saved `agent_profile_versions` snapshot against the profile's
    live/draft row.

    Reuses `compute_profile_diff` — the same builder
    `GET /agents/diff` (fork-vs-fork) uses — by shaping the version's
    denormalized snapshot into an `agent_profiles`-row-like dict for the
    left-hand side (`profile_a`); the live row is `profile_b`.
    """
    from runtime.compiler import load_snapshot_by_version
    from admin.profile_fork_handlers import compute_profile_diff

    entry = load_snapshot_by_version(version_id)
    if not entry or entry.get("profile_id") != profile_id:
        raise HTTPException(
            status_code=404,
            detail=f"version {version_id} not found for profile {profile_id}",
        )

    client = admin_service_client()
    live_rows = _safe_list(
        client.table("agent_profiles").select("*")
        .eq("profile_id", profile_id).limit(1).execute()
    )
    if not live_rows:
        raise HTTPException(status_code=404, detail="profile not found")
    profile_b = live_rows[0]

    # The snapshot (runtime.compiler._SHALLOW_FIELDS + resolved fields) has
    # no `profile_id`/`environment` of its own — the version row carries
    # those — so splice them in to get an agent_profiles-row-shaped dict
    # compute_profile_diff (and agents_diff.html) can treat identically to
    # profile_b.
    profile_a = {
        **(entry.get("snapshot") or {}),
        "profile_id": entry["profile_id"],
        "environment": entry.get("environment"),
        # Toolsets/skills come from the snapshot's frozen copies, never a
        # live re-read of this same profile (compute_profile_diff).
        "_snapshot": True,
    }

    # compute_profile_diff.SCALAR_FIELDS also includes `status`,
    # `parent_profile_id`, and `model_light_id` — none of which a version
    # snapshot freezes (they're profile-row bookkeeping, not config a
    # version captures; see runtime.compiler._SHALLOW_FIELDS). Left as-is,
    # profile_a would be missing them (-> None) while the live profile_b
    # row always has them, so every version diff would render a phantom,
    # permanently-red "changed" row for these fields. Splice profile_b's
    # live values into profile_a so they compare equal, while every real
    # config field still diffs correctly.
    _NON_SNAPSHOT_SCALAR_FIELDS = ("status", "parent_profile_id", "model_light_id")
    for _field in _NON_SNAPSHOT_SCALAR_FIELDS:
        profile_a[_field] = profile_b.get(_field)

    diff = compute_profile_diff(client, profile_a, profile_b)
    from admin.profile_fork_handlers import diff_display

    try:
        version_rows = _safe_list(
            client.table("agent_profile_versions")
            .select("version_number, promoted_at, notes")
            .eq("version_id", version_id).limit(1).execute()
        )
    except Exception:  # noqa: BLE001 — the title falls back to "saved version"
        version_rows = []
    return templates.TemplateResponse(
        request, "agents_diff.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": profile_b.get("environment") or "staging",
            "profile_a": profile_a,
            "profile_b": profile_b,
            "diff": diff,
            # Version-vs-live mode: the title names the version, not two ids.
            "version_info": (version_rows[0] if version_rows else {"version_number": None}),
            **diff_display(client, profile_a, profile_b),
        },
    )


@router.post("/agents/{profile_id}/versions/{version_id}/restore")
async def agents_restore_version(
    profile_id: str,
    version_id: str,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Copy `version_id`'s snapshot config back onto the live draft row.

    See `admin.services.profile_versions.restore_version` for exactly what
    gets copied (config fields only, never profile identity) and what gets
    filtered out (id-references — tools/KB sources/MCP servers/model/
    prompt versions — that no longer exist in this env are dropped and
    reported back, never silently written as a dangling id).

    This is copy-on-write, not a checkout: the live draft now differs from
    any saved version, so the redirect carries `edited_from={version_id}`
    for the template to resolve against the already-loaded
    `profile_versions` list and show the "you're editing a copy of vN —
    Save as new version to keep it" banner — plus a per-category
    dropped-refs count (`dropped_tools` / `dropped_kb` / `dropped_mcp` /
    `dropped_model` / `dropped_prompts`) so the template can surface
    "restored — dropped N tools, M KB sources, ..." without a second round
    trip to the DB.

    ValueError (version not found, or belongs to a different profile) ->
    redirect with `?error=`, same convention as pin-version above.
    """
    from admin.services.profile_versions import restore_version

    try:
        result = restore_version(
            profile_id, env, version_id, actor_user_id=ctx["user_id"],
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin/agents?profile_id={profile_id}&error={quote(str(exc))}",
            status_code=302,
        )

    dropped = result.get("dropped") or {}
    return RedirectResponse(
        url=(
            f"/admin/agents?profile_id={profile_id}"
            f"&edited_from={quote(version_id)}"
            f"&dropped_tools={len(dropped.get('tools') or [])}"
            f"&dropped_kb={len(dropped.get('data_sources') or [])}"
            f"&dropped_mcp={len(dropped.get('mcp_servers') or [])}"
            f"&dropped_model={1 if dropped.get('model') else 0}"
            f"&dropped_prompts={len(dropped.get('prompts') or [])}"
        ),
        status_code=303,
    )


@router.post("/env/promote")
async def env_promote(
    request: Request,
    profile_id: str = Form(...),
    target_env: str = Form(..., pattern=r"^(staging|prod)$"),
    force: bool = Form(False),
    reason: str = Form("", max_length=500),
    ui: str = Form(""),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Promote a profile from the current environment to staging or prod.

    Thin wrapper — see `admin.services.promotion.promote` for the eval
    gate, cross-env sibling remap, upsert, and compile-on-promote logic
    shared with the MCP promote_profile tool.

    `reason` (optional) is recorded on the override audit rows. The portal's
    confirm dialog posts `ui=1`: it requires a reason whenever it forces past
    a gate, and failures come back to the Releases page as a readable message
    instead of a JSON error. Without `ui` the contract is unchanged.
    """
    from admin.services.profiles import TemplateProtectedError
    from admin.services.promotion import PromotionError, promote

    from_ui = ui == "1"

    def _back(**params: str) -> RedirectResponse:
        from urllib.parse import urlencode
        return RedirectResponse(url="/admin/env?" + urlencode(params), status_code=303)

    if from_ui and force and not reason.strip():
        return _back(error="Give a reason to publish past a failed check.", agent=profile_id)

    try:
        result = promote(
            profile_id=profile_id, target_env=target_env, force=force,
            actor_user_id=ctx["user_id"], actor_email=ctx.get("email", "admin"),
            reason=reason or None,
        )
    except TemplateProtectedError as exc:
        if from_ui:
            return _back(error="Templates can't be published.", agent=profile_id)
        raise HTTPException(status_code=400, detail=str(exc))
    except PromotionError as exc:
        if from_ui:
            return _back(error=_promotion_error_text(exc), agent=profile_id)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    # Precompiled prod config bundle (GCS "middle path"): after a successful
    # PROD promote (the DB snapshot has already been written by promote()),
    # regenerate the immutable bundle from the prod branch's active profiles +
    # snapshots and push it to GCS so the prod service can load it at boot.
    #
    # Best-effort: the DB snapshot is the runtime fallback, so a bundle-push
    # failure must NEVER fail the promote. We do NOT bounce Cloud Run from here
    # (the operator/deploy handles reload). No-op for staging promotes.
    if target_env == "prod":
        try:
            from core.auth import (
                CrossEnvNotConfiguredError,
                admin_service_client,
                get_service_client_for_env,
            )
            from runtime.prod_bundle import compile_prod_bundle, push_bundle

            # The prod-targeting client — the SAME resolution promote() uses to
            # write the snapshot into the prod branch (get_service_client_for_env
            # returns the PROD_AI_SUPABASE_* client, or falls back to the local
            # branch when this service IS prod / single-DB).
            try:
                prod_client = get_service_client_for_env("prod")
            except CrossEnvNotConfiguredError:
                prod_client = admin_service_client()

            bundle = compile_prod_bundle(prod_client)
            gs_path = push_bundle(bundle)
            profile_count = len(bundle.get("profiles") or {})
            logger.info(
                "env_promote: pushed prod config bundle to %s (profiles=%d)",
                gs_path, profile_count,
            )
            try:
                from admin.audit import record_change
                record_change(
                    entity_type="agent_profiles", entity_id=profile_id,
                    action="PROD_CONFIG_BUNDLE_PUSH", actor_user_id=ctx["user_id"],
                    after={"gs_path": gs_path, "profile_count": profile_count},
                )
            except Exception as audit_exc:
                logger.warning(
                    "env_promote: prod config bundle audit write failed (%s)",
                    audit_exc,
                )
        except Exception as bundle_exc:
            logger.warning(
                "env_promote: prod config bundle regenerate/push failed (%s); "
                "prod keeps serving the previous bundle / DB snapshot",
                bundle_exc,
            )

    if from_ui:
        return RedirectResponse(url=f"/admin/env?promoted={quote(result.get('profile_id') or profile_id)}",
                                status_code=303)
    return RedirectResponse(url="/admin/env", status_code=302)


def _promotion_error_text(exc: Any) -> str:
    """A PromotionError as one plain sentence for the Releases page."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        if detail.get("error") == "eval_gate_failed":
            return ("The last test run scored below the bar. Publish again and give a reason "
                    "to go ahead anyway.")
        if detail.get("error") == "prompt_tool_mismatch":
            tools = ", ".join(detail.get("missing_tools") or [])
            return (f"The instructions ask the agent to use tools it can't reach ({tools}). "
                    "Fix the agent's tools, or publish again with a reason.")
    if getattr(exc, "status_code", None) == 404:
        return "That agent no longer exists."
    return f"Publishing failed: {detail}"


# `_upsert_target_profile` / `_latest_eval_for_profile` moved to
# admin.services.promotion — re-exported here for existing importers.
from admin.services.promotion import (  # noqa: E402,F401 — moved to services
    _latest_eval_for_profile, _upsert_target_profile,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_list(execute_result: Any) -> List[Dict[str, Any]]:
    return execute_result.data or []


def _days_ago_iso(days: int) -> str:
    """ISO timestamp `days` days before now, UTC."""
    import datetime as _dt
    return (_dt.datetime.utcnow() - _dt.timedelta(days=days)).isoformat() + "Z"


def _live_kpis(client, env: str, insights: dict) -> dict:
    """Engineering KPI tiles (Overview › Technical details). sessions_24h is the env-scoped distinct-session count from
    ClickHouse (`insights["sessions_24h"]`); tokens / latency /
    tool-success come from the same `insights` (load_live_insights). Shows "—"
    when a source is empty/unavailable.

    `insights["sources"]` says WHICH of the independent ClickHouse reads
    answered. It has to be consulted per tile, because `lmnr_ok` is an OR: with
    the 24h trace read dead and any other read alive, the token sums are 0 and
    this used to price them into a confident `$0` spend tile next to live
    numbers. A tile whose source failed renders "—", never a zero."""
    def _src_ok(key: str) -> bool:
        """Did the read behind this tile answer? Unknown (a caller that predates
        `sources`, or a stub) counts as OK so a healthy render is unchanged —
        only an explicit False blanks a tile."""
        return (insights.get("sources") or {}).get(key, True) is not False

    sessions = insights.get("sessions_24h")
    # "env" = the env-scoped ClickHouse count; "all" = the agent_traces
    # fallback below, which is NOT env-scoped and must be labelled as such
    # wherever it is shown (it used to sit under "PROD" unmarked).
    sessions_scope = "env"
    if sessions is None:
        sessions_scope = "all"
        # Fallback when the env-scoped ClickHouse count is unavailable: distinct
        # sessions from the lean agent_traces index. NOT env-scoped (agent_traces
        # has no env column), so this is a global number — a degraded fallback
        # only, which is why the primary count now comes from ClickHouse.
        cutoff = _days_ago_iso(1)
        try:
            rows = (client.table("agent_traces")
                    .select("session_id")
                    .gte("created_at", cutoff)
                    .limit(50000).execute().data or [])
            sessions = len({r.get("session_id") for r in rows if r.get("session_id")})
        except Exception:
            sessions = "—"
            sessions_scope = None

    if not insights.get("lmnr_ok"):
        return {"sessions_24h": sessions, "sessions_scope": sessions_scope,
                "tool_success_pct": "—",
                "latency_p95_ms": "—", "token_spend_usd": "—",
                "ttft_p50_ms": "—", "ttlt_p50_ms": "—"}

    tools = insights.get("top_tools") or []
    calls = sum(int(t.get("calls", 0)) for t in tools)
    errs = sum(int(t.get("errors", 0)) for t in tools)
    success = round(100 * (1 - errs / calls), 1) if calls else "—"
    if not _src_ok("top_tools"):
        success = "—"
    p95 = (insights.get("latency") or {}).get("p95_ms") or "—"
    if not _src_ok("traces_recent"):
        p95 = "—"
    tok = insights.get("tokens") or {}
    # Per-1k spend estimate from the agent's primary model (auto-priced via
    # tokencost). Falls back to a flat rate when the model can't be priced.
    from core import pricing
    in_24h = int(tok.get("in_24h") or 0)
    out_24h = int(tok.get("out_24h") or 0)
    primary = None
    try:
        rows = client.table("agent_models").select(
            "model_string, extra_config").eq("is_active", True).limit(1).execute().data or []
        primary = rows[0] if rows else None
    except Exception:
        primary = None
    priced = pricing.cost_usd(primary, in_24h, out_24h) if primary else None
    spend = round(priced if priced is not None else (in_24h + out_24h) / 1000 * 0.0005, 2)
    # The headline false-affirmative: a dead 24h trace read sums to 0 tokens and
    # prices to $0.00, which is indistinguishable from a genuinely idle day.
    if not _src_ok("traces_24h"):
        spend = "—"
    ttft_p50 = (insights.get("ttft") or {}).get("p50_ms") or "—"
    ttlt_p50 = (insights.get("ttlt") or {}).get("p50_ms") or "—"
    if not _src_ok("ttft"):
        ttft_p50 = ttlt_p50 = "—"
    return {"sessions_24h": sessions, "sessions_scope": sessions_scope,
            "tool_success_pct": success,
            "latency_p95_ms": p95, "token_spend_usd": spend,
            "ttft_p50_ms": ttft_p50, "ttlt_p50_ms": ttlt_p50}


from admin import tool_handlers  # noqa: F401, E402  -- attaches /admin/tools/{tool_id} routes
from admin import env_vars_handlers  # noqa: F401, E402  -- attaches /admin/env-vars routes
from admin import eval_handlers  # noqa: F401, E402  -- attaches /admin/evals/suites/{suite_id} routes
from admin import model_handlers  # noqa: F401, E402  -- attaches /admin/agent-models/{model_id} routes
from admin import deploy_handlers  # noqa: F401, E402  -- attaches /admin/env/{env}/deploy routes
from admin import kb_handlers  # noqa: F401, E402  -- attaches /admin/kb/{source_id} + create routes
from admin import mcp_handlers  # noqa: F401, E402  -- attaches /admin/agent-mcp-servers/{id} edit/delete routes
from admin import mcp_manage_handlers  # noqa: F401, E402  -- attaches /admin/mcp/discover + /admin/mcp/connect + /admin/mcp/create routes
from admin import profile_mcp_attach_handlers  # noqa: F401, E402  -- attaches /admin/profiles/{id}/mcp-attachments
from admin import test_fixtures_handlers  # noqa: F401, E402  -- attaches /admin/test-fixtures routes
from admin import profile_fork_handlers  # noqa: F401, E402  -- attaches /admin/agents/{id}/fork + /admin/agents/diff
from admin import templates_handlers  # noqa: F401, E402  -- attaches /admin/templates + /admin/agents/from-template
from admin import mentionables_handlers  # noqa: F401, E402  -- attaches /admin/api/profiles/{id}/mentionables
from admin import mcp_parity_handlers  # noqa: F401, E402  -- attaches /admin/api/mcp-flag-parity
from admin import drift_handlers  # noqa: F401, E402  -- attaches /admin/drift
from admin import search as _search_handlers  # noqa: F401, E402  -- attaches /admin/search.json
from admin import skill_handlers  # noqa: F401, E402  -- attaches /admin/skills* CRUD + versioning routes
from admin import profile_skill_attach_handlers  # noqa: F401, E402  -- attaches /admin/profiles/{id}/skill-attachments


