"""
FastAPI server for WAJ AI Agent with Server-Sent Events (SSE) streaming.
"""

# MUST be the first non-stdlib import. `_laminar_bootstrap` calls
# `Laminar.initialize()` at module-load time so OpenLLMetry's auto-
# instrumentation patches LangChain/LangGraph BEFORE the agent imports
# below pull those classes into the import graph. Initializing later
# (e.g. inside lifespan) is silent no-op: the agent code is already
# holding references to unpatched class methods and no spans are emitted.
from core import _laminar_bootstrap  # noqa: F401

import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from core.config import AGENT_CONFIG
from core.cors import build_allowed_origins
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel
from admin.router import router as admin_router
# Sidecar handler modules attach to admin_router on import. router.py
# already wires the long-standing sidecars (tool_handlers, eval_handlers,
# profile_fork_handlers, …) at the bottom of its own file; the
# prompt-engineering sidecar is loaded here so router.py stays untouched
# while the diff / compare / optimize routes still attach at boot.
from admin import prompt_handlers as _prompt_handlers  # noqa: F401
from admin import channel_handlers as _channel_handlers  # noqa: F401  -- attaches /admin/channels routes
from admin import observability_explorer_handlers as _observability_explorer_handlers  # noqa: F401  -- attaches /admin/observability/explorer routes
from admin import jobs_handlers as _jobs_handlers  # noqa: F401  -- attaches /admin/jobs routes
from admin import eval_runs_handlers as _eval_runs_handlers  # noqa: F401  -- attaches /admin/eval-runs routes
from admin import memory_handlers as _memory_handlers  # noqa: F401  -- attaches /admin/memory routes
from endpoints.voice_live import router as voice_live_router
from endpoints.agents_portal import router as agents_portal_router
from endpoints.agents_v1 import router as agents_v1_router
from endpoints.openai_compat import router as openai_compat_router
from endpoints.ai_sessions import router as ai_sessions_router
from runtime.chat_images import InvalidImage
import voice.live.providers.gemini_live  # noqa: F401 — registers the google_genai realtime provider
import voice.live.providers.azure_realtime  # noqa: F401 — registers the azure_openai stub
import voice.live.providers.azure_voice_live  # noqa: F401 — registers the azure_voice_live provider
import voice.live.simulated  # noqa: F401  — registers the eval "simulated" provider

# Load environment variables
load_dotenv()

# Configure logging
log_level = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Product code attaches through plugin packages (core/plugins.py). Each
# plugin's `app` module may define any of these, all optional:
#   register(app)                    routes, routers, exception handlers, mounts
#   required_env_vars(client)        extra names _validate_env_or_die requires
#   validate_env(env, client)        extra boot probes (raise RuntimeError)
#   BOOT_SEEDERS_AFTER_MODELS        seeder module paths, run after seed_agent_models
#   skip_boot_seeders()              a reason string to skip seeding on this app
#   on_startup(app)                  work to start once the app is booting
#   lifespan_contexts()              async context managers held while serving
#   health_issues()                  problems /health should report
#   validation_error_response(request, exc)  a response for a 422 on its own routes, or None
# The engine names no product here; with no plugin it runs on its own.
from core.plugins import call_hook as _call_plugins, load_hook as _load_plugin_hook  # noqa: E402

_PLUGIN_APPS = _load_plugin_hook("app")

REQUIRED_ENV_VARS = (
    "ENVIRONMENT",  # production | staging — explicit, not inferred
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
)


def _validate_env_or_die(env: dict | None = None) -> None:
    """Raise RuntimeError listing any missing required env vars.

    Call from the FastAPI lifespan before serving traffic. Takes an optional
    `env` mapping for testability; defaults to os.environ.
    """
    if env is None:
        env = os.environ
    required = list(REQUIRED_ENV_VARS)
    # Resolve the deployment scope eagerly: a plugin that does not recognise
    # its own scope configuration must refuse to boot (raise-at-boot), never
    # silently serve the full engine from a mistyped single-tenant deploy.
    from core.deployment import active_scope

    try:
        scope = active_scope()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    # Plugins add the variables their own routes cannot run without.
    for extra in _call_plugins(_PLUGIN_APPS, "required_env_vars", scope):
        required.extend(name for name in extra if name not in required)
    missing = [name for name in required if not (env.get(name) or "").strip()]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}. "
            f"This process refuses to start with incomplete configuration."
        )
    # Plugin boot probes: a misconfiguration that would otherwise surface as a
    # per-request 500 fails loudly here, once, at startup.
    _call_plugins(_PLUGIN_APPS, "validate_env", env, scope)


_jwks_refresh_task: Optional[asyncio.Task] = None
_insights_task: Optional[asyncio.Task] = None
_demo_reset_task: Optional[asyncio.Task] = None


async def _jwks_refresh_loop() -> None:
    """Refresh the main project's JWKS now, then every JWKS_REFRESH_INTERVAL_SEC,
    off the event loop (the fetch is blocking urllib)."""
    from core.auth import JWKS_REFRESH_INTERVAL_SEC, refresh_jwks_cache

    while True:
        try:
            await asyncio.to_thread(refresh_jwks_cache)
            await asyncio.sleep(JWKS_REFRESH_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Unexpected error in JWKS refresh loop: {e}")
            await asyncio.sleep(JWKS_REFRESH_INTERVAL_SEC)


# Boot-time registry seeders, in dependency order. Each module exposes a
# sync `run()`; `_run_boot_seeders()` below imports and runs them one by one,
# each isolated in its OWN try/except. Ordering constraints that matter:
#   - seed_agent_profiles AFTER prompts/tools/sources — it reads them to fill
#     the empty arrays on the migration-seeded profile rows.
#   - seed_builtin_mcps AFTER sync_tools + seed_agent_profiles: needs
#     agent_tools + profiles with resolved tool_ids already in place. Seeds
#     the built-in MCP bundle rows the runtime resolves tools from.
#   - seed_agent_models: shared model catalog — Gemini + Azure OpenAI + Azure
#     Voice Live (global; idempotent on name+env).
#   - A plugin's own seeders (`boot_seeders_after_models`) run next: after
#     the prompts, tools and models their profiles bind to. Each is
#     insert-only and never gates live traffic.
#   - seed_agents_demo AFTER seed_agent_models: the four AI-agents demo
#     portal kinds (/agents). Runs on a full build and on a deployment scope
#     that owns those kinds (no-op on other scoped builds); self-contained —
#     its own tools, prompts, KB source, profiles and bundles, insert-only.
#   - seed_demo_agent AFTER seed_agent_models: the one generic `demo` agent a
#     new install starts with. Seeded once (a marker row remembers it, so a
#     deleted or renamed demo agent never comes back); off on a build with a
#     product plugin unless SEED_DEMO_AGENT says otherwise.
#   - seed_templates: protected "architecture mold" template catalog
#     (agent_profiles rows with status='template', environment='staging') —
#     forkable starting points, inert at runtime. Idempotent on
#     (agent_kind, slug, env). MUST run AFTER seed_agent_models so
#     model_main_id is resolvable.
#   - seed_eval_suites AFTER the registry seeders so it can bind suites to
#     real profile/model rows.
#   - seed_voice_scenarios: starter live-voice scenarios (ai_voice_scenarios;
#     insert-only on slug). Tolerates the table not existing yet.
#   - seed_env_vars: mirrors ai_agent/.env into agent_env_vars for the dev
#     row so /admin/env-vars shows the current local config out of the box.
#   - Deployment targets are NOT seeded by the engine: with none registered
#     a test run is sent to this engine itself (admin.evals._base_url_for).
#     A deployment that tests another address registers it from its plugin
#     or under /admin/deployment-targets.
#   - seed_channels: default channel registry (insert-only,
#     allowed_agent_kinds=[] so seeding can never gate live traffic).
_BOOT_SEEDERS: tuple = (
    "admin.seed_prompts",
    "admin.sync_tools",
    "admin.seed_data_sources",
    "admin.seed_agent_profiles",
    "admin.seed_builtin_mcps",
    "admin.seed_agent_models",
    # Product seeders (a plugin's BOOT_SEEDERS_AFTER_MODELS) run here: after
    # the prompts, tools and models they bind to, before the templates and
    # eval suites that may reference their profiles.
    *(path for group in _call_plugins(_PLUGIN_APPS, "boot_seeders_after_models") for path in group),
    "admin.seed_agents_demo",
    "admin.seed_demo_agent",
    "admin.seed_templates",
    "admin.seed_eval_suites",
    "admin.seed_voice_scenarios",
    "admin.seed_env_vars",
    "admin.seed_channels",
)


# In demo mode the install shows the Northwind demo only: the sample-agents
# portal's four agents and the generic starter agent are not seeded, and the
# deployment's own variables are not mirrored into the portal.
_DEMO_SKIPPED_SEEDERS = ("admin.seed_agents_demo", "admin.seed_demo_agent", "admin.seed_env_vars")


async def _run_boot_seeders() -> None:
    """Run every boot seeder in `_BOOT_SEEDERS` order, isolating failures.

    Each seeder gets its OWN try/except: a failure (import or run) is logged
    with the seeder's name and the loop CONTINUES to the next one. Until
    2026-08-26 all 15 shared a single try/except, so any exception in an
    early seeder silently skipped every later one with one generic warning —
    which is how seed_voice_scenarios (11th) never ran and the portal showed
    "no voice scenarios" with nothing pointing at the seeder that actually
    threw.

    Skipped on the merchant MCP app (ENABLE_PUBLIC_MCP, waj-mcp): it reads
    none of these rows, every engine app seeds the same shared AI project on
    its own boot, and uvicorn binds the port only after the lifespan returns.
    Their sequential round trips (~0.3s each) ran until one second before the
    port opened in waj-mcp's ~2m45s cold start, while the ingress held every
    request with no first byte (2026-10-27). They also re-wrote the prod
    agent_env_vars mirror with waj-mcp's own env on every start.
    """
    for reason in _call_plugins(_PLUGIN_APPS, "skip_boot_seeders"):
        if reason:
            logger.info("boot seeders skipped: %s", reason)
            return
    from demo_mode import settings as _demo_settings

    skip = _DEMO_SKIPPED_SEEDERS if _demo_settings.enabled() else ()
    for module_path in _BOOT_SEEDERS:
        if module_path in skip:
            continue
        try:
            module = importlib.import_module(module_path)
            await asyncio.to_thread(module.run)
        except Exception as _exc:
            logger.warning(
                "boot seeder %s failed (%s: %s) — continuing with the remaining seeders",
                module_path, type(_exc).__name__, _exc,
            )


# App lifecycle management
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app lifecycle (startup/shutdown)"""
    _validate_env_or_die()
    # Laminar is already up: `core._laminar_bootstrap` ran at import time
    # so OpenLLMetry could patch LangChain BEFORE the agent classes were
    # bound. Nothing to do here. `shutdown_observability` still flushes
    # pending spans on app exit (see `yield` block below).
    # Model keys stored in the portal: load and decrypt them once, off the
    # loop, so the seeders below and the first request see them
    # (core/provider_keys.py). A no-op when SECRET_KEY is unset.
    from core import provider_keys as _provider_keys
    await _provider_keys.arefresh()
    await _run_boot_seeders()
    # A fresh install with no admin account: issue the one-time setup code and
    # print it (admin/services/first_run.py). Inert in a product build.
    from admin.services import first_run as _first_run
    await _first_run.on_boot()
    # Demo mode: the demo admin, the Northwind demo, and the nightly reset.
    from demo_mode import reset as _demo_reset
    global _demo_reset_task
    _demo_reset_task = await _demo_reset.on_boot()
    _call_plugins(_PLUGIN_APPS, "on_startup", app)
    # Keep the main project's JWKS warm in the background (now, then every
    # JWKS_REFRESH_INTERVAL_SEC) so verified requests read a cached key instead
    # of fetching on the request path. Never delays boot; a failed refresh keeps
    # the last good keys.
    global _jwks_refresh_task
    _jwks_refresh_task = asyncio.create_task(_jwks_refresh_loop())
    # Conversation insights (outcome / topic / language for the Overview): a
    # bounded background sweep, control-plane only by default — see
    # core/conversation_insights.py.
    global _insights_task
    from core.conversation_insights import run_loop as _insights_loop, sweep_enabled
    if sweep_enabled():
        _insights_task = asyncio.create_task(_insights_loop())
    logger.info(f"🚀 Starting WAJ AI Engine API (env={os.environ['ENVIRONMENT']})")
    async with contextlib.AsyncExitStack() as _mcp_stack:
        if os.getenv("ENABLE_ADMIN_MCP", "").lower() in ("1", "true", "yes"):
            from admin_mcp.server import mcp as _admin_mcp
            await _mcp_stack.enter_async_context(_admin_mcp.session_manager.run())
        for contexts in _call_plugins(_PLUGIN_APPS, "lifespan_contexts"):
            for context in contexts:
                await _mcp_stack.enter_async_context(context)
        yield
    # Shutdown tasks
    if _jwks_refresh_task is not None:
        _jwks_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _jwks_refresh_task
    if _demo_reset_task is not None:
        _demo_reset_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _demo_reset_task
    if _insights_task is not None:
        _insights_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _insights_task
    from core.observability import shutdown_observability
    shutdown_observability()
    logger.info("👋 Shutting down WAJ AI Engine API")


# Create FastAPI app
app = FastAPI(
    title="WAJ AI Engine API",
    description="AI Assistant for WAJ business management platform",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)



def _mount_prefix() -> str:
    """The prefix the duplex voice router mounts under: the active deployment
    scope's portal prefix, or "" (core.deployment.mount_prefix).

    A function of its own so a test can exercise it directly. The mount
    itself does NOT resolve at request time: `include_router`'s prefix is
    baked into the route table once, at import, like every other router here,
    so changing the scope after boot needs a restart.
    """
    from core.deployment import mount_prefix

    return mount_prefix()


# A WebSocket handshake never reaches `deployment_gate` below:
# `@app.middleware("http")` is a BaseHTTPMiddleware, which skips every
# non-"http" ASGI scope before the gate's body runs. What keeps a scoped build
# from serving another scope's socket is that only the ACTIVE scope's prefix
# is mounted in this process, so another scope's `<prefix>/voice/live` is a
# plain routing 404.
app.include_router(voice_live_router, prefix=_mount_prefix())

app.include_router(agents_portal_router)
app.include_router(agents_v1_router)
app.include_router(openai_compat_router)
app.include_router(ai_sessions_router)
app.include_router(admin_router)

# Admin MCP server — control-plane only (ENABLE_ADMIN_MCP=1). Exposes the
# management surface as MCP tools at /mcp; auth = signature-verified
# superuser JWT (see admin_mcp/auth.py). Chat services never set the flag.
#
# Registered as an exact Route, not a Mount: Mount only matches "/mcp" (no
# trailing slash) by 307-redirecting to an absolute URL built from
# scope["scheme"] — which is plaintext "http" here since uvicorn runs
# without --proxy-headers behind Cloud Run's TLS-terminating LB, so the
# redirect would tell MCP clients to replay their Authorization: Bearer
# token over http. add_route matches the literal path with no redirect,
# and — because it doesn't rewrite root_path/path the way Mount does — the
# inner FastMCP app still sees path "/mcp", which is also its own default
# streamable_http_path (see admin_mcp/server.py).
# Demo mode (demo_mode/): switch off what a public demo must never run, before
# anything below reads it. A no-op unless DEMO_MODE is set.
from demo_mode.settings import apply_process_lockdown as _demo_lockdown  # noqa: E402

_demo_lockdown()

if os.getenv("ENABLE_ADMIN_MCP", "").lower() in ("1", "true", "yes"):
    # A plugin that serves its own app at /mcp must not be enabled alongside
    # this one; its register() refuses that combination.
    from admin_mcp.server import build_mcp_asgi_app

    app.add_route("/mcp", build_mcp_asgi_app(), methods=["GET", "POST", "DELETE"])

# Rate limiting configuration (values resolved via AGENT_CONFIG → env)
RATE_LIMIT_REQUESTS = AGENT_CONFIG.rate_limit_requests  # requests per minute
RATE_LIMIT_WINDOW = AGENT_CONFIG.rate_limit_window_sec
rate_limit_store = defaultdict(list)


@app.middleware("http")
async def deployment_gate(request: Request, call_next):
    """On a scoped build, 404 every path the scope does not serve.

    With no active scope (a full build) this is a complete no-op. With one
    active, a request whose path starts with one of
    `core.deployment.blocked_path_prefixes(scope)` gets a plain 404,
    indistinguishable from "route doesn't exist" so the gate leaks nothing.
    Infra routes (/health, /static, /admin, /) and everything under the
    scope's own prefix are never blocked.

    Read per request, so a test can monkeypatch the scope. An unknown scope
    configuration never reaches this point in a real deployment:
    `_validate_env_or_die` refuses to boot first.
    """
    from core.deployment import active_scope, blocked_path_prefixes

    scope = active_scope()
    if scope is not None:
        path = request.url.path
        if any(path.startswith(prefix) for prefix in blocked_path_prefixes(scope)):
            return JSONResponse({"detail": "Not found"}, status_code=404)
    return await call_next(request)


@app.middleware("http")
async def persist_admin_env(request: Request, call_next):
    """Resolve + persist the admin environment selection.

    For every /admin request, resolve the selected env (this deployment's own
    env when it serves only one → ``?env=`` → the ``waj_admin_env`` cookie →
    ``staging``) and park it in the request-scoped `_ADMIN_ENV` ContextVar so
    env-scoped admin handlers (via `admin_service_client()`) read/write the
    SELECTED branch — this is what makes the control-plane a single portal over
    both staging and prod. Also (re)writes the cookie when the request carries
    a valid ``?env=`` so the selection survives navigation.
    `admin.router.current_env` mirrors the same precedence for the template's
    display value — keep the two in step.

    On a **single-env deployment** (a scoped build, core/deployment.py) its own
    env outranks query and cookie alike, and no cookie is written. This half
    matters more than the display value: `_ADMIN_ENV` is what
    `admin_service_client()` reads, so a stale ``waj_admin_env=prod`` on a
    staging-only box pointed the real queries at an environment with zero rows
    — the portal went blank with the env switcher hidden and no way to correct
    it. Refusing to persist the cookie there stops the trap re-arming itself.
    """
    from core.auth import set_admin_env, reset_admin_env
    from core.deployment import single_env_deployment

    is_admin = request.url.path.startswith("/admin")
    qp_env = request.query_params.get("env")
    only_env = single_env_deployment()
    resolved = None
    if is_admin:
        cookie_env = request.cookies.get("waj_admin_env")
        resolved = only_env or (
            qp_env if qp_env in ("staging", "prod") else (
                cookie_env if cookie_env in ("staging", "prod") else "staging"
            )
        )
    token = set_admin_env(resolved)
    try:
        response = await call_next(request)
    finally:
        reset_admin_env(token)
    try:
        if qp_env in ("staging", "prod") and is_admin and not only_env:
            # Production is remembered only until the browser closes (a
            # session cookie), so a later visit does not silently reopen the
            # portal on production; staging is kept for 30 days.
            response.set_cookie(
                "waj_admin_env", qp_env,
                max_age=None if qp_env == "prod" else 60 * 60 * 24 * 30,
                samesite="lax", path="/",
            )
    except Exception:
        pass
    return response


def _request_is_operator(request: Request) -> bool:
    """True only when the request carries a signature-verified SUPERUSER JWT,
    or a portal-issued API key with the `admin` scope.

    The `X-WAJ-*-Override` headers let a caller swap the LLM model or pin a
    prompt version for a request. That power must be limited to operators (the
    admin eval runner sends a verified superuser JWT with the override). This
    check fails safe: any missing/invalid token, missing SUPABASE_JWT_SECRET,
    non-superuser, or lookup error returns False (→ the override is ignored and
    the request proceeds normally), so it can never 500 the middleware chain or
    block ordinary unauthenticated agent traffic.
    """
    from admin.auth import resolve_superuser
    from core.auth import require_bearer

    try:
        token = request.cookies.get("waj_admin_token")
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer "):
            token = require_bearer(auth)
        if not token:
            return False
        from core import api_keys

        if api_keys.looks_like_api_key(token):
            # A portal-issued key is an operator only with the `admin` scope:
            # a `chat` key may talk to an agent, never choose its model or prompt.
            row = api_keys.verify_key(token)  # raises on a bad, revoked or expired key
            return api_keys.scope_allows(row.get("scopes") or [], api_keys.SCOPE_ADMIN)
        resolve_superuser(token)  # verifies signature + superuser row; raises otherwise
        return True
    except Exception:
        return False


@app.middleware("http")
async def prompt_override_middleware(request: Request, call_next):
    """Parse the optional `X-WAJ-Prompt-Override` header into the
    request-scoped ContextVar that `PromptResolver` consults.

    The header is JSON: `{"<agent_kind>/<prompt_key>": <version_number>}`.
    Used by the admin portal's prompt A/B feature — the eval runner sends
    one of these per parallel run so the suite executes against a pinned
    prompt version instead of the active one.

    Security: the override is honored ONLY for signature-verified superuser
    callers (`_request_is_operator`). On a public agent endpoint an anonymous
    caller could otherwise pin arbitrary/draft prompt versions. A missing /
    malformed / non-object / non-operator header is ignored and the request
    proceeds untouched. The ContextVar is per-task so concurrent requests can't
    see each other's overrides; we still `clear_prompt_override()` in a
    `finally` block as a belt-and-braces guard.
    """
    from core.prompt_resolver import clear_prompt_override, set_prompt_override

    header = request.headers.get("X-WAJ-Prompt-Override")
    applied = False
    if header:
        if await asyncio.to_thread(_request_is_operator, request):
            try:
                parsed = json.loads(header)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and parsed:
                # Coerce values to ints up front so the resolver's hot path
                # doesn't have to retry on non-numeric junk. Drop any entry
                # that can't be coerced — partial overrides are still useful.
                cleaned: dict = {}
                for key, value in parsed.items():
                    if not isinstance(key, str):
                        continue
                    try:
                        cleaned[key] = int(value)
                    except (TypeError, ValueError):
                        continue
                if cleaned:
                    set_prompt_override(cleaned)
                    applied = True
        else:
            # I1: the header was dropped by the operator gate — log loudly
            # rather than silently ignoring it, so a rejected pin is visible
            # in logs instead of just... not happening.
            logger.warning(
                "X-WAJ-Prompt-Override present but caller failed the operator "
                "gate (not a verified superuser) — override ignored: %s",
                request.url.path,
            )

    try:
        response = await call_next(request)
    finally:
        if applied:
            clear_prompt_override()
    return response


def _parse_model_override_header(header: Optional[str]) -> Optional[dict]:
    """Parse X-WAJ-Model-Override JSON into a partial agent_models dict.
    Returns None for missing/malformed/incomplete (must carry model_string)."""
    if not header:
        return None
    try:
        parsed = json.loads(header)
    except Exception:
        return None
    if not isinstance(parsed, dict) or not parsed.get("model_string"):
        return None
    return parsed


@app.middleware("http")
async def model_override_middleware(request: Request, call_next):
    """Park an `X-WAJ-Model-Override` partial model dict in a request-scoped
    ContextVar that runtime.agent._build_llm consults.

    Security: honored ONLY for signature-verified superuser callers
    (`_request_is_operator`). Only the admin eval runner sends this (with a
    verified operator JWT). Without the gate, an anonymous caller could force
    an arbitrary/expensive model or region on the public chat endpoints
    (cost/model abuse). Non-operator or malformed headers are ignored."""
    from core.model_resolver import clear_model_override, set_model_override

    parsed = _parse_model_override_header(request.headers.get("X-WAJ-Model-Override"))
    applied = False
    if parsed is not None:
        if await asyncio.to_thread(_request_is_operator, request):
            set_model_override(parsed)
            applied = True
        else:
            # I1: loud log so a dropped pin is visible rather than invisible.
            logger.warning(
                "X-WAJ-Model-Override present but caller failed the operator "
                "gate (not a verified superuser) — override ignored: %s",
                request.url.path,
            )
    try:
        response = await call_next(request)
    finally:
        if applied:
            clear_model_override()
    return response


@app.middleware("http")
async def profile_version_override_middleware(request: Request, call_next):
    """Park an `X-WAJ-Profile-Version` (agent_profile_versions.version_id) in a
    request-scoped ContextVar the profile loader consults. Honored ONLY for
    signature-verified operators (same gate as X-WAJ-Model-Override) — it swaps
    which config snapshot serves, never tenancy. Only the admin eval runner
    sends it (with a verified operator JWT). Malformed/non-operator → ignored."""
    from core.profile_version_resolver import (clear_profile_pin,
                                               clear_profile_version,
                                               set_profile_pin,
                                               set_profile_version)

    version = (request.headers.get("X-WAJ-Profile-Version") or "").strip()
    # X-WAJ-Profile-Id: the admin Test drawer / MCP chat pin the profile being
    # viewed (a draft copy, a specialist) — see core.profile_version_resolver.
    profile_pin = (request.headers.get("X-WAJ-Profile-Id") or "").strip()
    if profile_pin and not _PROFILE_ID_RE.fullmatch(profile_pin):
        profile_pin = ""
    applied = pinned = False
    if version or profile_pin:
        if await asyncio.to_thread(_request_is_operator, request):
            if version:
                set_profile_version(version)
                applied = True
            if profile_pin:
                set_profile_pin(profile_pin)
                pinned = True
        else:
            # I1: loud log so a dropped pin is visible rather than invisible.
            logger.warning(
                "X-WAJ-Profile-Version/-Id present but caller failed the "
                "operator gate (not a verified superuser) — override ignored: %s",
                request.url.path,
            )
    try:
        response = await call_next(request)
    finally:
        if applied:
            clear_profile_version()
        if pinned:
            clear_profile_pin()
    return response


_PROFILE_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


_RATE_LIMIT_EXEMPT_PATHS = frozenset({
    "/health", "/",
    *(path for group in _call_plugins(_PLUGIN_APPS, "rate_limit_exempt_paths") for path in group),
})


@app.middleware("http")
async def security_and_rate_limit_middleware(request: Request, call_next):
    """Combined security headers and rate limiting middleware"""
    # Skip rate limiting for health check, static files and the admin portal.
    # /admin/* is auth-gated by require_superuser; each page load also fans
    # out 5-10 sub-requests (CSS, JS, favicon, HTMX) which easily exhaust
    # 100/min on a single developer browsing the portal. A plugin names any
    # server-to-server route of its own that carries a stronger gate than a
    # per-IP bucket (`rate_limit_exempt_paths`).
    skip_rate_limit = (
        request.url.path in _RATE_LIMIT_EXEMPT_PATHS
        or request.url.path.startswith("/static")
        or (
            request.url.path.startswith("/admin")
            and request.url.path not in ("/admin/login", "/admin/logout", "/admin/setup")
        )
    )

    if not skip_rate_limit:
        # Get client IP (consider X-Forwarded-For in production)
        client_ip = request.client.host if request.client else "unknown"

        # Clean old entries
        current_time = time.time()
        rate_limit_store[client_ip] = [
            req_time
            for req_time in rate_limit_store[client_ip]
            if current_time - req_time < RATE_LIMIT_WINDOW
        ]
        # Delete key if list is now empty (prevent memory leak)
        if not rate_limit_store[client_ip]:
            del rate_limit_store[client_ip]

        # Check rate limit
        if len(rate_limit_store[client_ip]) >= RATE_LIMIT_REQUESTS:
            return Response(
                content=json.dumps(
                    {"error": "Rate limit exceeded. Please try again later."}
                ),
                status_code=429,
                media_type="application/json",
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(RATE_LIMIT_REQUESTS),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(current_time + RATE_LIMIT_WINDOW)),
                },
            )

        # Add current request
        rate_limit_store[client_ip].append(current_time)

    # Process request
    response = await call_next(request)

    # Add security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains"
    )

    # The admin portal is server-rendered operator state — the profile rail,
    # the env-scoped config cards, live traces — served with no validator and
    # no cache directive at all, which leaves it to the browser's heuristic
    # freshness. That is how an operator ends up staring at a profile list
    # captured before a row existed and reasonably concluding the row is
    # missing. Static assets are excluded: they carry their own `?v=`
    # cache-busting (a stale chart stylesheet once outlived a fix) and SHOULD be cached.
    _path = request.url.path
    if _path.startswith("/admin") and not _path.startswith("/admin/static"):
        response.headers["Cache-Control"] = "no-store"

    # Add rate limit info headers for successful requests
    if not skip_rate_limit:
        remaining = max(
            0, RATE_LIMIT_REQUESTS - len(rate_limit_store.get(client_ip, []))
        )
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_REQUESTS)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

    return response


def _plugin_cors_origins() -> set:
    """Origins the plugins allow at request time (their `cors_origins()`)."""
    origins: set = set()
    for extra in _call_plugins(_PLUGIN_APPS, "cors_origins"):
        origins.update(extra or ())
    return origins


# CORS configuration
allowed_origins_str = os.getenv("ALLOWED_ORIGINS", "*")
if allowed_origins_str == "*":
    # When using wildcard, disable credentials (CORS spec requirement)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # When using specific origins, credentials can be enabled. The
    # allow-list is the UNION of:
    #   1) ALLOWED_ORIGINS env var (legacy explicit list)
    #   2) every active channel's allowed_origins (agent_channels table),
    #      read again at request time, so a channel added in the portal
    #      after boot works without a restart (see ``core/cors.py``).
    allowed_origins = build_allowed_origins(env_value=allowed_origins_str)

    # localhost (dev) + first-party origins (the apex and any subdomain of
    # company.example / tenants.example / staging.example, centralized and unit-tested in core.cors
    # so an apex origin can't 400 the preflight and silently break a public
    # chat) + whatever origins the plugins allow at request time (their
    # `cors_origins()` hook, read per request so a plugin may refresh it).
    from core.cors import FirstPartyCORSMiddleware

    app.add_middleware(
        FirstPartyCORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        dynamic_origins=_plugin_cors_origins,
    )

# Demo mode's fictional tool server (demo_mode/northwind.py): answers only
# while DEMO_MODE is on (404 otherwise), so it is registered unconditionally.
from demo_mode import northwind as _northwind  # noqa: E402

app.add_route(_northwind.PATH, _northwind.app, methods=["GET", "POST", "DELETE"])

# Demo mode's gate (demo_mode/gate.py): refuses the locked screens and routes
# and applies the visitor caps, for HTTP and WebSocket alike. Added last, so it
# is the outermost middleware; it passes everything through when DEMO_MODE is off.
from demo_mode.gate import DemoGate  # noqa: E402

app.add_middleware(DemoGate)

# Admin portal assets (portal.css, etc.) live under ai_agent/admin/static/
admin_static_dir = os.path.join(os.path.dirname(__file__), "admin", "static")
if os.path.exists(admin_static_dir):
    app.mount("/admin/static", StaticFiles(directory=admin_static_dir), name="admin_static")

# Screenshots / short videos embedded in the partner Help pages
# (docs/partner/*.md → /admin/help/{slug}). Public like /admin/static: they are
# product illustrations, not data.
help_media_dir = os.path.join(os.path.dirname(__file__), "docs", "partner", "media")
if os.path.isdir(help_media_dir):
    app.mount("/admin/help/media", StaticFiles(directory=help_media_dir), name="admin_help_media")

# Mount static files for debug UI
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Response model of /health (the product routes and their request models live
# in plugin packages; see core/plugins.py).
class HealthResponse(BaseModel):
    """Health check response"""

    status: str
    version: str
    environment: str
    # Why `status` is "degraded" (a plugin's `health_issues`); omitted when
    # healthy.
    issues: Optional[List[str]] = None


# Product routes: every plugin's `app` hook attaches its own (core/plugins.py).
# Here, where they were defined inline until they moved out, so nothing about
# the order middleware and routers are installed in changes.
_call_plugins(_PLUGIN_APPS, "register", app)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint to verify service is running.

    Returns:
        Service health status and metadata
    """
    # Degraded is reported in the body only: the HTTP status stays 200 so a
    # container probe never restarts a process that is still serving.
    issues = [issue for found in _call_plugins(_PLUGIN_APPS, "health_issues") for issue in (found or [])]
    return HealthResponse(
        status="degraded" if issues else "healthy",
        version="1.0.0",
        environment=os.getenv("ENVIRONMENT", "development"),
        issues=issues or None,
    )


@app.get("/")
async def root():
    """Root endpoint with API information"""
    from fastapi.responses import RedirectResponse

    from admin.services import first_run as _first_run

    from demo_mode import settings as _demo

    if _demo.enabled():
        # The public demo: the front door is the portal's "Try the demo" page.
        return RedirectResponse(url="/admin/", status_code=302)
    if await _first_run.in_first_run():
        return RedirectResponse(url="/admin/setup", status_code=303)
    return {
        "name": "WAJ AI Engine API",
        "version": "1.0.0",
        "docs": "/admin/docs/api",
        "health": "/health",
    }


# Error handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler"""
    from fastapi.responses import JSONResponse

    # Browser GETs under /admin get the portal's error page (link back
    # included) instead of a bare JSON body; API callers are unchanged.
    from admin.router import render_error_page, wants_portal_error_page

    if wants_portal_error_page(request):
        return render_error_page(request, exc.status_code, detail=exc.detail)

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}},
    )


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Router-level errors (unknown /admin path → 404, 405). Same default
    JSON body FastAPI would send, except for a browser GET under /admin."""
    from fastapi.exception_handlers import http_exception_handler as _default

    from admin.router import render_error_page, wants_portal_error_page

    if wants_portal_error_page(request):
        return render_error_page(request, exc.status_code, detail=exc.detail)
    return await _default(request, exc)


@app.exception_handler(InvalidImage)
async def invalid_image_handler(request: Request, exc: InvalidImage):
    """A bad chat photo: plain 422 JSON (contract §3), raised before any SSE."""
    return JSONResponse(
        {"error": "invalid_image", "index": exc.index, "reason": exc.reason},
        status_code=422,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """A bad query/path value (e.g. ``/admin/agents?env=dev``): portal error
    page for a browser GET under /admin, FastAPI's default 422 otherwise."""
    from fastapi.exception_handlers import request_validation_exception_handler as _default

    from admin.router import render_error_page, wants_portal_error_page

    # A plugin may answer a validation error on its own routes (a route that
    # must not reveal itself through a different error body than its gate's).
    for response in _call_plugins(_PLUGIN_APPS, "validation_error_response", request, exc):
        if response is not None:
            return response

    if wants_portal_error_page(request):
        bad = [str(e.get("loc", ())[-1]) for e in exc.errors() if e.get("loc")]
        return render_error_page(request, 422, bad_params=bad or ["value"])
    return await _default(request, exc)


def _expose_error_details() -> bool:
    """Whether a 500 response body may carry ``str(exc)``. Default: NO.

    This deliberately does NOT read ``ENVIRONMENT``. The previous gate here
    was ``ENVIRONMENT == "production"``, and the live values (read off the
    running services, not off a config comment) are:

      * ``ai-engine``          -> ``production``  — prod chat, matched
      * ``ai-engine-control``  -> ``dev``         — did NOT match
      * the client demo VMs       -> ``staging``     — did NOT match

    So it protected the prod chat service and nothing else. The two that
    slipped through are both internet-facing, and the control-plane
    (aiengine.staging.example) is the single service holding the staging *and*
    prod Supabase service keys — the worst possible place to serve
    exception text from.

    Widening the match to ``prod``/``staging`` would not fix it either: the
    control-plane's env is legitimately ``dev`` (it is the cross-env admin
    portal), so no value of ENVIRONMENT separates "developer's laptop" from
    "deployed service" here.

    So we fail closed and require an explicit opt-in instead. Nothing is
    lost by defaulting off: the full traceback is written to the server log
    on the line above, which is where it should be read from anyway.
    """
    return (os.getenv("EXPOSE_ERROR_DETAILS") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """General exception handler — sanitized unless explicitly opted out of."""
    from fastapi.responses import JSONResponse

    # Log full error details server-side. This is the ONLY place the
    # exception text is guaranteed to be available; the response body below
    # is intentionally not.
    logger.error(f"Unhandled exception: {exc}", exc_info=True)

    error: Dict[str, Any] = {
        "code": 500,
        "message": "An internal error occurred. Please try again later.",
    }
    if _expose_error_details():
        error["details"] = str(exc)
    return JSONResponse(status_code=500, content={"error": error})


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=True if os.getenv("ENVIRONMENT") == "development" else False,
        log_level=log_level.lower(),
    )
