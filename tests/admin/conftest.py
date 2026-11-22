"""Pytest fixtures for admin portal smoke tests.

Boots the FastAPI app with stubbed credentials and a permissive bypass
for the require_superuser dependency so smoke tests don't need a real
Supabase JWT.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.deployment import scope_env_vars

# Make sure ai_agent/ is on sys.path so `import main` works.
AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def _make_null_supabase_client():
    """Return a MagicMock that quacks like a postgrest client and returns empty data."""
    mock_result = MagicMock()
    mock_result.data = []
    mock_result.count = 0

    # Build a fluent-chain mock: every chained method returns an object whose
    # .execute() returns mock_result.
    builder = MagicMock()
    builder.execute.return_value = mock_result
    # All chained calls (select, eq, order, limit, range, gte, head, in_) return builder.
    for method in ("select", "eq", "order", "limit", "range", "gte", "in_", "or_",
                   "insert", "update", "delete"):
        getattr(builder, method).return_value = builder

    client = MagicMock()
    client.table.return_value = builder
    return client


@pytest.fixture(scope="session", autouse=True)
def _stub_env():
    """Force-override the env vars main.py demands at startup.

    Uses explicit set + restore (not os.environ.setdefault) so a developer
    or CI environment with real SUPABASE_URL/etc. cannot leak into tests.
    """
    stubs = {
        "SUPABASE_URL": "http://127.0.0.1:54321",
        "SUPABASE_ANON_KEY": "test-anon",
        "SUPABASE_SERVICE_KEY": "test-service",
        "ENVIRONMENT": "development",
        "GOOGLE_API_KEY": "test-google",
        "ALLOWED_ORIGINS": "*",
        # Run as a full build: clear every env var a plugin reads to pick a
        # deployment scope (core/deployment.py). main.py's load_dotenv() would
        # otherwise leak a scoped demo instance's `.env` into the process env,
        # narrowing the whole admin portal and breaking every full-catalog
        # assertion. Tests that exercise a scope monkeypatch it themselves.
        **{name: "" for name in scope_env_vars()},
    }
    # The product plugin's own boot env, from tests/product_fixtures.py
    # (shipped only with the product; loaded by name in tests/conftest.py).
    from core.plugins import optional_import

    product = optional_import("tests.product_fixtures")
    stubs.update(getattr(product, "ADMIN_APP_ENV_STUBS", {}) if product else {})
    previous = {k: os.environ.get(k) for k in stubs}
    os.environ.update(stubs)
    try:
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fake_smoke_superuser():
    """Stable identity used by both the `client` fixture below and the
    `_ensure_smoke_auth` autouse fixture. We keep it a module-level
    function so identity comparisons via `is` are deterministic — the
    autouse hook needs to know whether the override is "ours" vs a
    per-test override another file set up (and was supposed to restore)."""
    return {
        "user_token": "test-token",
        "user_id": "00000000-0000-0000-0000-000000000001",
        "email": "admin@waj.test",
    }


@pytest.fixture(autouse=True)
def _route_admin_service_client_to_get_service_client():
    """Make the env-switcher `admin_service_client()` follow module-local
    `get_service_client` patches.

    The env-scoped admin handlers were refactored to call
    `admin_service_client()` (core.auth) instead of `get_service_client()`, but
    most admin tests still patch `<module>.get_service_client`. With `_ADMIN_ENV`
    unset (the test default) `admin_service_client()` is *defined* to return
    `get_service_client()` — but each handler imports it as its own module-local
    name, so a `<module>.get_service_client` patch never reaches it (the patch
    became a no-op and the handler hit the real Supabase). Re-point every handler
    module's `admin_service_client` at a shim that, with no admin env selected,
    calls THAT module's current `get_service_client` — so the existing patches
    (and the session `client` fixture's) work again and the suite needs no live
    DB. Cross-env (`_ADMIN_ENV` set) still routes through the real helper.
    """
    import importlib
    from unittest.mock import patch as _patch

    import core.auth as _auth

    # A shared empty client for handlers whose get_service_client a test didn't
    # patch — so an unmocked DB read returns empty data instead of connecting to
    # a real Supabase (the suite must be hermetic for CI).
    _null_db = _make_null_supabase_client()
    # The pristine (unpatched) get_service_client, captured before any test-level
    # patch runs. Used to tell "patched" from "original" at either the module or
    # the core.auth level (some tests patch <module>.get_service_client, others
    # patch core.auth.get_service_client — e.g. test_profile_fork).
    _orig_get_service_client = _auth.get_service_client

    _HANDLER_MODULES = (
        "admin.router", "admin.mcp_handlers", "admin.kb_handlers",
        "admin.eval_runs_handlers", "admin.eval_handlers", "admin.evals",
        "admin.prompt_handlers", "admin.tool_handlers", "admin.model_handlers",
        "admin.profile_fork_handlers", "admin._model_picker", "admin.e2e_runner",
        "admin.services.promotion", "admin.mcp_manage_handlers",
        "admin.mcp_parity_handlers", "admin.mentionables_handlers",
        "admin.profile_mcp_attach_handlers", "admin.templates_handlers",
        "admin.services.evals", "admin.services.templates", "admin.services.profiles",
        "admin.services.bakeoffs", "admin.services.profile_versions",
        "admin.skill_handlers", "admin.profile_skill_attach_handlers",
        # Converted to admin_service_client by the env-client-symmetry sweep
        # (reads and writes must resolve the same branch): keep them hermetic
        # under the same shim.
        "admin.channel_handlers", "admin.env_vars_handlers",
        "admin.test_fixtures_handlers", "admin.audit", "admin._jobs",
        "admin.test_runner", "admin.voice_scenario_runner",
        "admin.screens.observability",
    )

    def _make_shim(mod):
        def _shim():
            # Mirror core.auth.admin_service_client, but route the local-branch
            # call through the module's (patchable) get_service_client. In tests
            # the env middleware often defaults `_ADMIN_ENV` to "staging" while
            # cross-env vars are unset, so the real code falls back to the local
            # client — replicate that try/except fallback.
            env = _auth._ADMIN_ENV.get()
            if env:
                try:
                    return _auth.get_service_client_for_env(env)
                except _auth.CrossEnvNotConfiguredError:
                    pass  # fall through to the local client
            # Honor a patch at EITHER location: the module-local
            # get_service_client (most tests) or core.auth.get_service_client
            # (test_profile_fork-style). Anything still equal to the pristine
            # original counts as "not patched".
            gsc_mod = getattr(mod, "get_service_client", None)
            if gsc_mod is not None and gsc_mod is not _orig_get_service_client:
                return gsc_mod()
            if _auth.get_service_client is not _orig_get_service_client:
                return _auth.get_service_client()
            # Nothing patched → return the empty client rather than connect to a
            # real Supabase.
            return _null_db
        return _shim

    patchers = []
    for name in _HANDLER_MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if hasattr(mod, "admin_service_client"):
            p = _patch.object(mod, "admin_service_client", _make_shim(mod))
            p.start()
            patchers.append(p)
    try:
        yield
    finally:
        for p in reversed(patchers):
            p.stop()


@pytest.fixture(autouse=True)
def _ensure_smoke_auth():
    """Per-test guard that re-installs the smoke auth override after any
    sibling test file popped it during its teardown.

    Several admin test modules (test_tool_routes, test_kb_upload,
    test_eval_csv_import, test_profile_fork, test_tool_dry_run,
    test_channels, …) set + unconditionally pop the auth override
    around their own tests. That's fine in isolation but it leaves the
    session-scoped smoke `client` 401-ing because the override is gone
    after the first sibling test runs. Re-asserting the override at the
    START of every test makes the smoke suite resilient to order. The
    sibling teardowns still run; this fixture wins because it executes
    LATER (function-scoped, runs after module-scoped setups)."""
    from admin import auth
    try:
        from main import app
    except Exception:
        # Some unit tests don't need the app at all — skip the guard.
        yield
        return
    app.dependency_overrides[auth.require_superuser] = _fake_smoke_superuser
    yield


@pytest.fixture(scope="session")
def client():
    """One TestClient shared by every smoke test.

    Bypasses require_superuser via FastAPI's dependency_overrides and
    patches get_service_client (AI project) + get_main_client (MAIN
    project) so handlers that still hit the DB don't need a real Supabase
    instance.
    """
    from fastapi.testclient import TestClient
    from admin import auth

    from main import app

    app.dependency_overrides[auth.require_superuser] = _fake_smoke_superuser
    null_db = _make_null_supabase_client()
    # NB: `fetch_businesses_for_picker` is patched in the function-scoped
    # `_stub_business_picker` autouse fixture below — NOT here. A session-scoped
    # patch of it leaked for the whole session and poisoned a later top-level
    # test (tests/test_main_client_routing.py) that asserts the picker calls
    # get_main_client (order-dependent; passed only in isolation).
    with patch("admin.router.get_service_client", return_value=null_db), \
            patch("admin.router.client_for", return_value=null_db), \
            patch("admin.router.get_main_client", return_value=null_db), \
            patch("admin._business_picker.get_main_client", return_value=null_db), \
            patch("admin._model_picker.get_service_client", return_value=null_db), \
            patch("admin.eval_runs_handlers.get_service_client", return_value=null_db):
        with TestClient(app) as c:
            yield c
    app.dependency_overrides.pop(auth.require_superuser, None)


@pytest.fixture(autouse=True)
def _stub_business_picker():
    """Function-scoped: keep the admin business picker from hitting the DB during
    smoke renders, without leaking a session-wide patch into non-admin tests."""
    try:
        with patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]):
            yield
    except (ImportError, AttributeError):
        yield


@pytest.fixture(autouse=True)
def _deploy_repo():
    """A deploy repository, so the deploy and rollback tests exercise the
    token and dispatch paths whether or not a plugin supplies a default
    (admin/deploy_handlers.github_repo). Tests of the repo itself unset it.

    Plain set/restore, NOT `monkeypatch`: an autouse fixture that requests
    `monkeypatch` sets it up before `_stub_business_picker`, so its undo ran
    last and re-installed that fixture's mock for every later test."""
    previous = os.environ.get("GITHUB_DEPLOY_REPO")
    os.environ["GITHUB_DEPLOY_REPO"] = "example-org/ai-engine"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GITHUB_DEPLOY_REPO", None)
        else:
            os.environ["GITHUB_DEPLOY_REPO"] = previous
