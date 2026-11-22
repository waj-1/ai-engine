"""Smoke tests for every admin portal route.

One test per route. Each asserts a 200 plus a small DOM substring that
proves the right template rendered.
"""
from __future__ import annotations

import re

import pytest


def test_login_renders(client) -> None:
    from admin.nav import brand

    r = client.get("/admin/login")
    assert r.status_code == 200
    assert brand()["title"] in r.text  # "WAJ Admin" or "WAJ AI Engine": tests/admin/test_portal_nav.py


# Every screen the sidebar reaches (directly or via section tabs).
ROUTES = [
    ("/admin/live", "Overview"),
    ("/admin/observability", "Conversations"),
    ("/admin/agents", "Agents"),
    ("/admin/tools", "Tools"),
    ("/admin/channels", "Channels"),
    ("/admin/kb", "Knowledge bases"),
    ("/admin/evals", "Testing"),
    ("/admin/evals/suites", "Test suites"),
    ("/admin/evals/runs", "Runs"),
    ("/admin/tests", "Engineering tests"),
    ("/admin/env", "Releases"),
    ("/admin/deployments", "Deploy"),
    ("/admin/users", "Team &amp; access"),
    ("/admin/audit", "Activity"),
    ("/admin/docs", "Developer docs"),
    ("/admin/settings", "Settings"),
    ("/admin/help", "Help"),
    ("/admin/skills", "Skills"),
    ("/admin/memory", "Memory"),
    ("/admin/voice-scenarios", "Voice"),
    ("/admin/drift", "Drift"),
    ("/admin/diagnostics/runtime", "Runtime environment"),
]


@pytest.mark.parametrize("path,marker", ROUTES)
def test_route_renders(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200, r.text[:200]
    assert marker in r.text


# Each screen highlights the sidebar item it now lives under (admin/nav.py):
# merged screens light up their area, not a link of their own.
NAV_ACTIVE = [
    ("/admin/live", "/admin/live"),
    ("/admin/observability", "/admin/observability"),
    ("/admin/agents", "/admin/agents"),
    ("/admin/tools", "/admin/tools"),
    ("/admin/channels", "/admin/tools"),          # Connections
    ("/admin/kb", "/admin/kb"),
    ("/admin/skills", "/admin/kb"),               # Knowledge
    ("/admin/memory", "/admin/kb"),               # Knowledge
    ("/admin/evals", "/admin/evals"),
    ("/admin/voice-scenarios", "/admin/evals"),   # Testing
    ("/admin/tests", "/admin/evals"),             # Testing
    ("/admin/audit", "/admin/audit"),
    ("/admin/help", "/admin/help"),
    ("/admin/settings", "/admin/settings"),
    ("/admin/env", "/admin/settings"),            # Settings › Releases
    ("/admin/deployments", "/admin/settings"),
    ("/admin/users", "/admin/settings"),          # Settings › Team & access
    ("/admin/drift", "/admin/settings"),          # Settings › Diagnostics
    ("/admin/diagnostics/runtime", "/admin/settings"),
    ("/admin/lmnr/diagnose", "/admin/settings"),
    ("/admin/docs", "/admin/settings"),           # Settings › Developer docs
]


@pytest.mark.parametrize("path,nav_href", NAV_ACTIVE)
def test_nav_highlights_own_link(client, path, nav_href):
    r = client.get(path)
    assert r.status_code == 200, r.text[:200]
    assert f'class="nav-link active" href="{nav_href}"' in r.text, (
        f"{path} should mark the {nav_href} sidebar link active"
    )
    # …and only that one.
    assert r.text.count('class="nav-link active"') == 1


REDIRECTS = [
    ("/admin/", "/admin/live"),
    ("/admin/profiles", "/admin/agents"),
    ("/admin/prompts", "/admin/agents"),
    ("/admin/data-sources", "/admin/kb"),
    ("/admin/agent-data-sources", "/admin/kb"),
    # The Sessions browser was retired — its conversation view is now the
    # explorer's Transcript tab; all session paths 302 into the explorer.
    ("/admin/sessions", "/admin/observability/explorer"),
    ("/admin/observability/sessions", "/admin/observability/explorer"),
]


@pytest.mark.parametrize("old,new", REDIRECTS)
def test_redirect(client, old, new):
    r = client.get(old, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == new


# Canonical contract for the IA consolidation: every legacy screen route 307s
# to its merged destination (method + query string preserved).
@pytest.mark.parametrize("old,new", [
    ("/admin/configure", "/admin/agents"),
    ("/admin/eval-runs", "/admin/evals/runs"),
    ("/admin/agent-models", "/admin/agents?tab=models"),
    ("/admin/integrations", "/admin/tools"),
    ("/admin/env-vars", "/admin/env?tab=variables"),
])
def test_legacy_screen_redirects(client, old, new):
    resp = client.get(old, follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == new


def test_live_renders_kpis(client):
    r = client.get("/admin/live")
    assert r.status_code == 200
    # Partner-facing outcome tiles up top…
    for label in ("Conversations", "Needs attention", "Handed to a person",
                  "Failed actions", "Avg response time"):
        assert label in r.text
    # …and every engineering KPI still there, under Technical details.
    assert "Technical details" in r.text
    for label in ("Tool-call success", "Latency P95", "Token spend", "Cache hit rate"):
        assert label in r.text


def test_agents_renders_the_agent_table(client):
    """/admin/agents with no agent picked is the Agents TABLE (B1) — it used
    to auto-open the first agent's 9,000px editor."""
    r = client.get("/admin/agents")
    assert r.status_code == 200
    assert "data-agents-table" in r.text or "No agents in" in r.text
    assert "New agent" in r.text
    # Area tabs: Agents · Models · Templates.
    assert "/admin/agents?tab=models" in r.text and "/admin/templates" in r.text


def test_integrations_lists_real_systems(client):
    # Integrations render as a section on the single MCP & Tools screen.
    r = client.get("/admin/tools")
    assert r.status_code == 200
    for name in ("Supabase", "Google AI", "GitBook", "Resend", "Twilio", "Stripe", "Sentry"):
        assert name in r.text


def test_integrations_redirect_carries_env(client):
    """Query params (e.g. env) survive the redirect to the merged screen."""
    from urllib.parse import parse_qs, urlsplit

    r = client.get("/admin/integrations?env=prod", follow_redirects=False)
    assert r.status_code == 307
    parts = urlsplit(r.headers["location"])
    assert parts.path == "/admin/tools"
    assert parse_qs(parts.query) == {"env": ["prod"]}


@pytest.mark.parametrize("old", ["/admin/mcp-servers", "/admin/agent-mcp-servers"])
def test_legacy_mcp_redirects_are_single_hop(client, old):
    """Pre-rename legacy paths point straight at the merged screen."""
    r = client.get(old, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/admin/tools"


def test_tabulator_assets_loaded(client):
    r = client.get("/admin/agents")
    assert r.status_code == 200
    assert "tabulator" in r.text.lower()
    assert "portal-table.js" in r.text


def test_tools_screen_is_mcp_and_tools(client):
    r = client.get("/admin/tools")
    assert r.status_code == 200
    assert "Tools &amp; integrations" in r.text or "Tools & integrations" in r.text
    # Compact server list + a searchable tool catalog collapsed per server.
    assert 'id="servers"' in r.text
    assert 'id="tools"' in r.text  # Tool catalog section
    assert "/admin/mcp/create" in r.text
    assert "/admin/mcp/connect" in r.text
    # Direct HTTP-tool creation stays retired (author inside a bundle).
    assert "New HTTP tool" not in r.text
    assert "/admin/tools/new" not in r.text


def test_kb_screen_agent_filter(client, product_build):
    r = client.get("/admin/kb")
    assert r.status_code == 200
    # One agent filter fed by the shared live agent list (admin/agent_kinds.py).
    assert '<select name="kind"' in r.text
    for k in ("sample_agent", "sample_other"):
        assert f'value="{k}"' in r.text


def test_models_screen_expanded_form(client):
    # Models merged into /admin/agents as the Models tab.
    r = client.get("/admin/agents?tab=models")
    assert r.status_code == 200
    for field in ("top_p", "top_k", "cost_per_1k_in", "cost_per_1k_out",
                  "google_genai", "azure_openai", "anthropic"):
        assert field in r.text
    # The Vertex providers were removed 2026-08-16 — offering them in the form
    # would let an operator save a row no builder can construct.
    assert "vertex_ai" not in r.text
    assert "vertex_maas" not in r.text
    # Tab strip renders with Models active.
    assert 'class="tab-strip"' in r.text
    assert 'aria-current="page"' in r.text


def test_models_screen_redirect_carries_env(client):
    """Query params (e.g. env) survive the redirect to the merged screen."""
    from urllib.parse import parse_qs, urlsplit

    r = client.get("/admin/agent-models?env=prod", follow_redirects=False)
    assert r.status_code == 307
    parts = urlsplit(r.headers["location"])
    assert parts.path == "/admin/agents"
    assert parse_qs(parts.query) == {"tab": ["models"], "env": ["prod"]}


def test_env_screen_has_no_runtime_card(client):
    """The runtime-environment card moved to Settings › Diagnostics; the
    Promote tab no longer shows process internals."""
    r = client.get("/admin/env")
    assert r.status_code == 200
    assert "STAGING_AI_SUPABASE_URL" not in r.text
    assert "Runtime environment" not in r.text
    r = client.get("/admin/diagnostics/runtime")
    assert r.status_code == 200
    assert "STAGING_AI_SUPABASE_URL" in r.text
    assert "GITHUB_DEPLOY_TOKEN" in r.text


def test_env_promote_tab_is_default(client):
    """/admin/env defaults to the Promote tab: the release overview renders,
    the env-vars registry doesn't, and the tab strip links to Variables."""
    r = client.get("/admin/env")
    assert r.status_code == 200
    assert 'id="promote-dialog"' in r.text
    # The lone "force" checkbox is gone — the dialog asks for a reason instead.
    assert 'name="force" value="true"' not in r.text
    assert 'name="reason"' in r.text
    # The env-vars registry now lives on the Variables tab only.
    assert "No env vars configured" not in r.text
    # Tab strip renders with Promote active.
    assert 'class="tab-strip"' in r.text
    assert 'aria-current="page"' in r.text
    assert "/admin/env?tab=variables" in r.text


def test_env_variables_tab_renders(client):
    """/admin/env?tab=variables renders the env-vars registry (add form +
    per-env tables) without the promote cards."""
    r = client.get("/admin/env?tab=variables")
    assert r.status_code == 200
    assert "Environment variables" in r.text
    assert "No env vars configured" in r.text
    # Promote content stays on the Promote tab.
    assert "How promote works" not in r.text
    # Settings section tabs render with Environment variables current.
    assert 'class="section-tabs"' in r.text
    assert re.search(r'class="section-tab active"\s*aria-current="true"\s*href="/admin/env\?tab=variables"', r.text)


def test_env_vars_redirect_carries_env(client):
    """Query params (e.g. env) survive the redirect to the merged screen."""
    from urllib.parse import parse_qs, urlsplit

    r = client.get("/admin/env-vars?env=prod", follow_redirects=False)
    assert r.status_code == 307
    parts = urlsplit(r.headers["location"])
    assert parts.path == "/admin/env"
    assert parse_qs(parts.query) == {"tab": ["variables"], "env": ["prod"]}


def test_deployments_screen_disabled_without_token(client, monkeypatch):
    """Without GITHUB_DEPLOY_TOKEN the one deploy form renders disabled with
    a plain explanation instead of a live primary button."""
    monkeypatch.delenv("GITHUB_DEPLOY_TOKEN", raising=False)
    r = client.get("/admin/deployments")
    assert r.status_code == 200
    assert "Deploying from the portal isn't set up" in r.text
    assert "Deploy to Production" in r.text
    assert "<fieldset disabled>" in r.text
    assert "GITHUB_DEPLOY_TOKEN" in r.text  # as the muted technical name


def test_deployments_screen_enabled_with_token(client, monkeypatch):
    monkeypatch.setenv("GITHUB_DEPLOY_TOKEN", "fake-pat")
    r = client.get("/admin/deployments")
    assert r.status_code == 200
    assert "Deploying from the portal isn't set up" not in r.text
    assert "<fieldset disabled>" not in r.text


def test_env_screen_no_longer_has_deploy_buttons(client):
    """Deploy actions live on the Deploy code tab; Promote keeps promote."""
    r = client.get("/admin/env")
    assert r.status_code == 200
    assert "Deploy to Production" not in r.text
    assert 'action="/admin/env/prod/deploy"' not in r.text
    assert 'action="/admin/env/promote"' in r.text


def test_deploy_endpoint_400_without_token(client, monkeypatch):
    """Without GITHUB_DEPLOY_TOKEN the handler must refuse with a 400 and a
    message that tells the admin how to configure it. Uses environment=prod
    since that's the only deployable target now (single-DB main-only) — the
    prod-only guard runs before the token check, so a staging target would
    trip that guard instead of this one."""
    monkeypatch.delenv("GITHUB_DEPLOY_TOKEN", raising=False)
    r = client.post("/admin/env/prod/deploy", follow_redirects=False)
    assert r.status_code == 400
    assert "GITHUB_DEPLOY_TOKEN" in r.text


def test_deploy_endpoint_rejects_unknown_env(client, monkeypatch):
    monkeypatch.setenv("GITHUB_DEPLOY_TOKEN", "fake-pat")
    # prod is the only valid deploy target now (single-DB main-only); staging
    # is an environment column, not a deployable service, so it's rejected
    # too — see test_env_consolidation.py::test_deploy_staging_now_rejected.
    r = client.post("/admin/env/qa/deploy", follow_redirects=False)
    assert r.status_code == 400
    assert "prod" in r.text.lower()


def test_users_one_table_one_control(client, monkeypatch):
    """Team & access: one table, one grant/revoke control per person (no
    toggle + revoke duplicate), service accounts hidden unless asked for."""
    from unittest.mock import MagicMock

    fake_rows = [
        {"id": "aaaa-1111", "email": "boss@company.example", "phone_number": None, "country_code": None,
         "role": "admin", "permission_level": "full", "permission": {"is_superuser": True},
         "business_id": None, "created_at": "2025-01-01T00:00:00"},
        {"id": "bbbb-2222", "email": "staff@company.example", "phone_number": None, "country_code": None,
         "role": "staff", "permission_level": "read", "permission": {"is_superuser": False},
         "business_id": "biz-0001", "created_at": "2025-02-01T00:00:00"},
        {"id": "cccc-3333", "email": "agent-slot1@e2e.test", "phone_number": None, "country_code": None,
         "role": "owner", "permission_level": "owner", "permission": {"is_superuser": False},
         "business_id": None, "created_at": "2025-03-01T00:00:00"},
    ]
    mock_result = MagicMock()
    mock_result.data = fake_rows
    builder = MagicMock()
    builder.execute.return_value = mock_result
    for method in ("select", "eq", "order", "limit", "in_"):
        getattr(builder, method).return_value = builder
    fake_client = MagicMock()
    fake_client.table.return_value = builder
    monkeypatch.setattr("admin.router.get_main_client", lambda: fake_client)

    r = client.get("/admin/users")
    assert r.status_code == 200
    html = r.text
    assert "boss@company.example" in html and "staff@company.example" in html
    assert "agent-slot1@e2e.test" not in html            # service account hidden
    assert "Show service accounts (1)" in html
    assert html.count("<table") == 1                       # one table
    # Exactly one control per person: Remove admin for the admin, Make admin otherwise.
    assert html.count(">Remove admin</button>") == 1
    assert html.count(">Make admin</button>") == 1
    assert 'type="checkbox"' not in html                   # no toggle switch
    # The control posts the unchanged contract: hidden grant=true|false.
    assert '<input type="hidden" name="grant" value="false">' in html
    assert '<input type="hidden" name="grant" value="true">' in html
    assert "Admin" in html and "No portal access" in html

    r = client.get("/admin/users?service=1")
    assert "agent-slot1@e2e.test" in r.text
    assert 'name="return_service" value="1"' in r.text