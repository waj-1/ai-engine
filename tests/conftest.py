"""
Pytest configuration and fixtures for WAJ AI Agent tests.
"""

import os

# Run the suite as a FULL build by default. main.py calls load_dotenv() at
# import; a scoped deployment's `.env` (core/deployment.py) carries the env var
# that selects its scope, which would otherwise leak into the process env and
# narrow the admin portal and seeders to that scope, breaking full-catalog
# tests. load_dotenv() does not override already-set vars, and this runs
# before any `import main`, so clearing them here wins. Tests that exercise a
# scoped build set it explicitly (monkeypatch). The plugins name the vars.
from core.deployment import scope_env_vars as _scope_env_vars  # noqa: E402

for _name in _scope_env_vars():
    os.environ[_name] = ""

# The authenticated chat endpoints (the product agents' routes, and a
# tenant-site chat when a token is present) now use `authenticate_verified`,
# which verifies HS256 JWTs against SUPABASE_JWT_SECRET (ES256 against the JWKS
# — see tests/core/test_verified_jwt.py) and fails closed when
# unset. The `valid_jwt_token` fixture signs with 'test-secret', so pin the same
# value here (setdefault so a test can still monkeypatch its own). Tests that
# assert fail-closed behavior monkeypatch.delenv this explicitly.
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret")

import pytest
import sys
import jwt
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def test_context():
    """Provide test context for agent/tools"""
    return {
        'user_token': 'mock_test_token',
        'business_id': 'test_business_id_123',
        'user_id': 'test_user_id',
        'locale': 'en',
        'session_id': 'test_session_123'
    }


@pytest.fixture
def test_user_id():
    """Provide test user ID"""
    return "test-user-123"


def _jwt_secret() -> str:
    """The secret the chat endpoints' `authenticate_verified` checks against.

    Read at call time so the fixtures sign with whatever SUPABASE_JWT_SECRET is
    in the environment — 'test-secret' locally (set via setdefault above) or the
    CI-provided value (e.g. 'test-jwt-secret'). Signing with a hardcoded secret
    would 401 wherever the env value differs.
    """
    return os.environ.get("SUPABASE_JWT_SECRET", "test-secret")


@pytest.fixture
def valid_jwt_token(test_user_id):
    """Provide a valid JWT token for testing.

    Includes `aud="authenticated"` and is signed with the runtime
    SUPABASE_JWT_SECRET so it satisfies `authenticate_verified` on the chat
    endpoints, not just the unverified decode.
    """
    payload = {
        'sub': test_user_id,
        'email': 'test@example.com',
        'aud': 'authenticated',
        'role': 'business_owner',
        'user_metadata': {'name': 'Test User'},
        'exp': datetime.utcnow() + timedelta(hours=1)
    }
    return jwt.encode(payload, _jwt_secret(), algorithm='HS256')


@pytest.fixture
def expired_jwt_token(test_user_id):
    """Provide an expired JWT token for testing.

    Signed with the runtime secret + aud so it fails verification specifically
    on expiry (ExpiredSignatureError → 401), not on a signature/claim mismatch.
    """
    payload = {
        'sub': test_user_id,
        'email': 'test@example.com',
        'aud': 'authenticated',
        'exp': datetime.utcnow() - timedelta(hours=1)
    }
    return jwt.encode(payload, _jwt_secret(), algorithm='HS256')


@pytest.fixture
def mock_langchain_llm():
    """Mock LangChain LLM for testing"""
    mock_llm = Mock()
    mock_llm.ainvoke = Mock(return_value=Mock(content="Test response"))
    return mock_llm


@pytest.fixture
def mock_tool_context():
    """Provide mock context for tool creation"""
    return {
        'user_token': 'test_token_abc123',
        'business_id': 'business-uuid-123',
        'locale': 'en',
        'branch_id': None
    }


@pytest.fixture
def skip_if_no_api_key():
    """Skip test if GOOGLE_API_KEY is not set"""
    if not os.getenv('GOOGLE_API_KEY'):
        pytest.skip("GOOGLE_API_KEY not set")


@pytest.fixture
def mock_supabase_client():
    """Mock Supabase client for testing"""
    mock_client = Mock()
    mock_client.rpc = Mock(return_value=Mock(execute=Mock(return_value=Mock(data=[]))))
    mock_client.from_ = Mock(return_value=Mock(
        select=Mock(return_value=Mock(
            eq=Mock(return_value=Mock(
                order=Mock(return_value=Mock(execute=Mock(return_value=Mock(data=[]))))
            ))
        ))
    ))
    return mock_client


@pytest.fixture
def sample_business_data():
    """Sample business performance data for testing"""
    return {
        "total_revenue": 50000,
        "total_bookings": 150,
        "total_profit": 35000,
        "daily_breakdown": [
            {"date": "2024-01-01", "revenue": 5000, "bookings": 15},
            {"date": "2024-01-02", "revenue": 5500, "bookings": 18}
        ]
    }


@pytest.fixture
def sample_staff_data():
    """Sample staff performance data for testing"""
    return {
        "staff": [
            {"id": "staff-1", "name": "John Doe", "revenue": 15000, "bookings": 50},
            {"id": "staff-2", "name": "Jane Smith", "revenue": 18000, "bookings": 60}
        ]
    }


@pytest.fixture
def ai_access_full():
    """Full AI access permissions"""
    return {'data': True, 'docs': True, 'actions': True, 'all': True}


@pytest.fixture
def ai_access_docs_only():
    """Docs-only AI access permissions"""
    return {'docs': True, 'data': False, 'actions': False}


@pytest.fixture
def ai_access_none():
    """No AI access permissions"""
    return {'docs': False, 'data': False, 'actions': False}


@pytest.fixture
def fake_profile_loader(monkeypatch):
    """Stub ``AgentProfileLoader.load_active`` so tests that construct an
    agent don't need a live Supabase connection.

    Background: ``RuntimeAgent.__init__`` always calls
    ``AgentProfileLoader.load_active(agent_kind, environment)``, which hits
    Supabase. In the test environment there is no real Supabase, so the
    loader raises ``ProfileLoadError`` and every test that instantiates an
    agent crashes during construction. By design (see the docstring on
    ``core.agent_profile``) the loader no longer falls back to a
    file-system default, so tests have to mock around it.

    This fixture returns a builder callable. Tests that need to override
    fields can call it with kwargs::

        def test_x(fake_profile_loader):
            fake_profile_loader(agent_kind="support_bot",
                                tool_python_names=["search_orders"])

    If no override is requested, the patched ``load_active`` synthesises a
    Profile matching whatever ``agent_kind`` the agent's ``__init__`` asks
    for, with empty/default fields. ``model_main`` and ``model_light`` are
    intentionally ``None`` so ``RuntimeAgent._build_llm`` short-circuits to
    ``None`` — tests that need a working LLM patch
    ``runtime.llm_factory.build_chat_model`` themselves.

    Recommended usage at file scope so individual tests don't need to
    know about the fixture::

        pytestmark = pytest.mark.usefixtures("fake_profile_loader")
    """
    from core.agent_profile import Profile, AgentProfileLoader

    custom_profile: dict = {}

    def _synth_builtin_bundle(kind, tool_python_names):
        """Mirror the production seeder: a configured profile carries a
        built-in bundle MCP attachment whose members are its tools, so
        ``resolve_mcp_tools_for_profile`` binds exactly those from the pool
        (see admin/seed_builtin_mcps.py). Members are python-kind and are
        looked up in the request's tool pool by ``python_name``.
        """
        return {
            "mcp_server": {"mcp_server_id": f"m-{kind}", "name": f"__builtin__:{kind}",
                           "source": "bundle", "context_fields": {}},
            "tool_subset": None,  # expose all listed members
            "position": 0,
            "member_tools": [
                {"tool": {"tool_id": f"id-{n}", "python_name": n, "kind": "python"},
                 "context_fields": None, "position": i}
                for i, n in enumerate(tool_python_names)
            ],
        }

    def _build(**overrides):
        kind = overrides.pop("agent_kind", "business")
        env = overrides.pop("environment", None) or "dev"
        tool_python_names = overrides.pop("tool_python_names", [])
        is_fallback = overrides.pop("is_fallback", False)
        # MCP resolution is the only tool-resolution path: a non-fallback
        # profile binds what its MCP attachments name. Synthesize a built-in
        # bundle from tool_python_names (unless the test supplies attachments
        # explicitly) so "profile whitelists tools X" keeps meaning "binds X".
        mcp_attachments = overrides.pop("mcp_attachments", None)
        if mcp_attachments is None:
            mcp_attachments = (
                [_synth_builtin_bundle(kind, tool_python_names)]
                if (tool_python_names and not is_fallback) else []
            )
        defaults = dict(
            profile_id=overrides.pop("profile_id", f"test-profile-{kind}"),
            agent_kind=kind,
            slug=overrides.pop("slug", f"{kind}_default"),
            environment=env,
            model_main=overrides.pop("model_main", None),
            model_light=overrides.pop("model_light", None),
            prompt_version_ids=overrides.pop("prompt_version_ids", {}),
            resolved_prompts=overrides.pop("resolved_prompts", {}),
            tool_python_names=tool_python_names,
            data_source_ids=overrides.pop("data_source_ids", []),
            mcp_server_ids=overrides.pop("mcp_server_ids", []),
            mcp_attachments=mcp_attachments,
            escalation_policy=overrides.pop("escalation_policy", {}),
            is_fallback=is_fallback,
        )
        # Anything still in overrides is unexpected — raise loudly rather
        # than swallow a typo.
        if overrides:
            raise TypeError(f"unknown Profile fields: {sorted(overrides)}")
        profile = Profile(**defaults)
        custom_profile[kind] = profile
        return profile

    def _fake_load_active(agent_kind, environment=None):
        if agent_kind in custom_profile:
            return custom_profile[agent_kind]
        return _build(agent_kind=agent_kind, environment=environment)

    # Clear the loader's own cache so a previous (real) load attempt
    # doesn't leak into this test.
    AgentProfileLoader._cache.clear()

    monkeypatch.setattr(
        AgentProfileLoader,
        "load_active",
        classmethod(lambda cls, k, environment=None: _fake_load_active(k, environment)),
    )
    yield _build
    AgentProfileLoader._cache.clear()


@pytest.fixture
def test_fixture():
    """Read a portal-managed test fixture by name.

    Operator stores fixtures at /admin/test-fixtures, then exports a
    `.env.test` file via the same page. Source the file before running
    pytest: `set -a && source .env.test && pytest`.

    Usage:
        def test_foo(test_fixture):
            jwt = test_fixture("business_jwt")
            assert jwt
    """
    def _get(name: str, *, required: bool = True) -> str:
        key = f"WAJ_TEST_{name.upper()}"
        value = os.environ.get(key)
        if not value and required:
            pytest.skip(
                f"missing test fixture {name!r} — set it at "
                f"/admin/test-fixtures (name='{name}'), download .env.test, "
                f"and `source .env.test` before running pytest."
            )
        return value or ""
    return _get


@pytest.fixture
def reset_rate_limiters():
    """main.rate_limit_store / sales_rate_limit_store are module-level dicts
    shared across the whole test session (no per-test reset elsewhere). Gate
    probe test files fire a lot of requests at the same handful of paths
    from the same TestClient host, so left alone they eventually trip the
    ~100/min IP limiter — a false 429 that masks the gate's 404 in a full
    suite run (order-dependent flake, not a gate-logic bug). Clear both
    before every test, same precedent as tests/admin/test_login_rate_limit.py.

    Opt in with `pytestmark = pytest.mark.usefixtures("reset_rate_limiters")`
    (the deployment-gate test files do) rather
    than autouse, since it's only relevant to the handful of files that
    hammer main.app's gate through a shared TestClient.
    """
    import main

    main.rate_limit_store.clear()
    # A product plugin may keep limiter stores of its own (the sales chat's).
    for plugin_app in main._PLUGIN_APPS:
        store = getattr(plugin_app, "sales_rate_limit_store", None)
        if store is not None:
            store.clear()
    yield


@pytest.fixture(autouse=True)
def _reset_clickhouse_last_failure():
    """`core.lmnr_clickhouse` keeps the most recent query failure process-wide
    so the observability banner can name its cause. Clear it per test, or a
    failure one test provokes would rewrite another test's notice text."""
    from core import lmnr_clickhouse

    lmnr_clickhouse.clear_last_failure()
    yield
    lmnr_clickhouse.clear_last_failure()


@pytest.fixture
def client():
    """Plain TestClient over main.app. Deliberately NOT a context manager, so
    the lifespan (and its boot seeders) never runs."""
    from fastapi.testclient import TestClient

    import main

    return TestClient(main.app)


# Fixtures for product routes live in tests/product_fixtures.py, which ships
# only with builds that have those routes. Loaded by name so this file imports
# no product code; pytest picks the fixtures up as attributes of this module.
from core.plugins import optional_import as _optional_import  # noqa: E402

_product_fixtures = _optional_import("tests.product_fixtures")
if _product_fixtures is not None:
    globals().update({name: obj for name, obj in vars(_product_fixtures).items()
                      if not name.startswith("_") and name not in globals()})


# A fictional deployment catalog for tests of GENERIC logic that need some
# agent kinds, rubrics or runner kinds to exist: the engine names none of its
# own, so without one such a test would be testing nothing. Fictional on
# purpose: an engine test must not depend on any product's agents.
SAMPLE_AGENT = "sample_agent"          # a chat agent with suites and live chats
SAMPLE_OTHER = "sample_other"          # a second one
SAMPLE_SCORED = "sample_scored"        # a runner kind that grades itself
SAMPLE_TURNS = "sample_turns"          # a runner kind that plays whole chats
SAMPLE_KINDS = (SAMPLE_AGENT, SAMPLE_OTHER)
SAMPLE_EXPECTATIONS = {
    SAMPLE_AGENT: "The response should answer from the sample agent's own data.",
    SAMPLE_OTHER: "The response should move the visitor toward a clear next step.",
}


@pytest.fixture
def product_build(monkeypatch):
    """Install the fictional SAMPLE_* catalog (see above) in place of a
    plugin's: the agent kinds the pickers, suites and live-chat sampler
    accept, per-kind rubrics, and two plugin runner kinds. Also makes
    core.plugins.has_product() true. A test that needs the product's own
    code uses `tests.product.requires_product` instead."""
    from types import SimpleNamespace

    from admin import agent_kinds, eval_handlers, eval_runs_handlers, seed_agent_profiles
    from admin.services import eval_scorers

    monkeypatch.setattr("core.plugins.has_product", lambda: True)
    monkeypatch.setattr(agent_kinds, "BASE_AGENT_KINDS", SAMPLE_KINDS)
    from admin import _channel_picker, channel_handlers, kb_handlers, tool_handlers
    from admin.screens import live_analytics
    monkeypatch.setattr(channel_handlers, "_AGENT_KINDS_FOR_CHANNELS", SAMPLE_KINDS)
    monkeypatch.setattr(tool_handlers, "_AGENT_KINDS_FOR_TOOLS", SAMPLE_KINDS)
    monkeypatch.setattr(kb_handlers, "AGENT_KIND_OPTIONS", SAMPLE_KINDS)
    monkeypatch.setattr(_channel_picker, "AGENT_KINDS", SAMPLE_KINDS)
    monkeypatch.setattr(live_analytics, "AGENT_KINDS", SAMPLE_KINDS)
    monkeypatch.setattr(seed_agent_profiles, "_AGENT_KINDS", SAMPLE_KINDS)
    monkeypatch.setattr(eval_handlers, "_AGENT_KINDS_FOR_SUITES", SAMPLE_KINDS)
    monkeypatch.setattr(eval_handlers, "RUNNER_KINDS",
                        ("chat", "voice_live", SAMPLE_SCORED, SAMPLE_TURNS))
    monkeypatch.setattr(eval_runs_handlers, "_AGENT_KINDS", SAMPLE_KINDS)
    monkeypatch.setattr(eval_runs_handlers, "_DEFAULT_EXPECTATIONS_BY_KIND", dict(SAMPLE_EXPECTATIONS))
    monkeypatch.setitem(eval_scorers.RUNNER_DEFAULT_SCORER, SAMPLE_SCORED, "sample_ids")
    monkeypatch.setitem(eval_scorers.SCORERS, "sample_ids",
                        lambda output, reference: {"passed": True, "score": 1.0, "detail": {}})
    catalog = SimpleNamespace(
        extra_eval_profile_kinds=lambda: (SAMPLE_SCORED,),
        default_kind=lambda: (SAMPLE_AGENT,),
    )
    runners = SimpleNamespace(runners=lambda: {
        SAMPLE_SCORED: SimpleNamespace(default_scorer="sample_ids", label="Sample scored"),
        SAMPLE_TURNS: SimpleNamespace(label="Sample turns"),
    }, scorers=lambda: {})
    import core.plugins as plugins

    real_load_hook = plugins.load_hook
    fakes = {"agent_catalog": [catalog], "eval_runners": [runners]}
    monkeypatch.setattr(plugins, "load_hook",
                        lambda hook: fakes.get(hook) or ([] if hook in fakes else real_load_hook(hook)))
