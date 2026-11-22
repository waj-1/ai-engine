"""Portal IA foundation (PR A): sidebar, section tabs, Settings hub, Help,
partner-build gating, registry-driven branding, error page, secret masking.

Partner builds are simulated with a stand-in deployment scope
(tests/scoped_build.py) — every helper in admin/nav.py asks for it per call.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import env_vars_handlers as evh
from admin import nav
from tests.scoped_build import full_build, make_scope, use_orchard, use_scope

HTML = {"accept": "text/html,application/xhtml+xml"}
PRIMARY_LABELS = ["Overview", "Agents", "Conversations", "Knowledge", "Connections",
                  "Testing", "Activity", "Help"]


def _nav_labels(html: str) -> list[str]:
    return re.findall(r'class="nav-link[^"]*" href="[^"]+" title="([^"]+)"', html)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def test_sidebar_has_new_primary_items_and_settings_footer(client):
    r = client.get("/admin/live")
    assert r.status_code == 200
    assert _nav_labels(r.text) == PRIMARY_LABELS + ["Settings"]


def test_every_nav_item_has_a_unique_icon():
    icons = [i.icon for i in nav.PRIMARY + (nav.SETTINGS,)]
    assert len(icons) == len(set(icons))
    tpl = (AI_AGENT_DIR / "admin/templates/_components/icon.html").read_text()
    bodies = {}
    for name in icons:
        m = re.search(r"'%s':\s*'([^']+)'" % re.escape(name), tpl)
        assert m, f"icon {name!r} missing from icon.html"
        bodies[name] = m.group(1)
    assert len(set(bodies.values())) == len(bodies), "two nav items draw the same icon"


@pytest.mark.parametrize("path,key", [
    ("/admin/skills/abc", "knowledge"),
    ("/admin/kb/123", "knowledge"),
    ("/admin/mcp/connect", "connections"),
    ("/admin/channels/new", "connections"),
    ("/admin/evals/runs/x", "testing"),
    ("/admin/tests/abc", "testing"),
    ("/admin/env-vars/1", "settings"),
    ("/admin/lmnr/diagnose", "settings"),
    ("/admin/observability/explorer", "conversations"),
    ("/admin/agent-models/1", "agents"),
    ("/admin/environment", None),  # segment boundary: not /admin/env
])
def test_section_for_path(path, key):
    assert nav.section_for(path) == key


# ---------------------------------------------------------------------------
# Section tabs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,section,current", [
    ("/admin/kb", "knowledge", "/admin/kb"),
    ("/admin/memory", "knowledge", "/admin/memory"),
    ("/admin/channels", "connections", "/admin/channels"),
    ("/admin/voice-scenarios", "testing", "/admin/voice-scenarios"),
    ("/admin/users", "settings", "/admin/users"),
    ("/admin/settings", "settings", "/admin/settings"),
    ("/admin/deployments", "settings", "/admin/env"),
])
def test_member_pages_render_section_tabs(client, path, section, current):
    r = client.get(path)
    assert r.status_code == 200, r.text[:300]
    assert f'data-section="{section}"' in r.text
    m = re.search(r'class="section-tab active"\s*aria-current="true"\s*href="([^"]+)"', r.text)
    assert m and m.group(1) == current


def test_detail_pages_have_no_section_tabs():
    assert nav.section_tabs_for("/admin/tools/123") is None
    assert nav.section_tabs_for("/admin/live") is None


# ---------------------------------------------------------------------------
# Settings hub + Help
# ---------------------------------------------------------------------------


def test_settings_hub_lists_every_entry(client):
    r = client.get("/admin/settings")
    assert r.status_code == 200
    for title in ("Team &amp; access", "Releases", "Environment variables", "Voice budgets",
                  "Diagnostics", "Developer docs"):
        assert title in r.text
    for href in ("/admin/users", "/admin/env", "/admin/env?tab=variables", "/admin/voice-budgets",
                 "/admin/drift", "/admin/lmnr/diagnose", "/admin/jobs", "/admin/tests",
                 "/admin/docs"):
        assert f'href="{href}"' in r.text


def test_help_index_renders_partner_readme(client):
    r = client.get("/admin/help")
    assert r.status_code == 200
    assert "Start here" in r.text  # from docs/partner/README.md
    assert '"media": "/admin/help/media"' in r.text
    assert "/admin/docs/api" not in r.text  # no developer API links in Help


def test_help_unknown_slug_is_404(client):
    assert client.get("/admin/help/does-not-exist").status_code == 404
    assert client.get("/admin/help/..%2Fadmin-portal%2FREADME").status_code == 404


def test_help_picks_up_new_pages(client, tmp_path, monkeypatch):
    from admin import router

    (tmp_path / "README.md").write_text("# Help\n\nindex", encoding="utf-8")
    (tmp_path / "02-publish.md").write_text("# Publish your changes\n\nsteps", encoding="utf-8")
    monkeypatch.setattr(router, "HELP_DIR", tmp_path)
    pages = router.help_pages()
    assert [p["slug"] for p in pages] == ["index", "publish"]
    r = client.get("/admin/help/publish")
    assert r.status_code == 200
    assert "Publish your changes" in r.text


# ---------------------------------------------------------------------------
# Partner builds
# ---------------------------------------------------------------------------


@pytest.fixture
def partner(monkeypatch):
    use_orchard(monkeypatch)
    yield


def test_partner_settings_hub_hides_staff_entries(client, partner):
    r = client.get("/admin/settings")
    assert r.status_code == 200
    assert "Team &amp; access" in r.text and "Voice budgets" in r.text
    for hidden in ("Releases", "Environment variables", "Diagnostics", "Developer docs"):
        assert hidden not in r.text
    # Section tabs follow the same rule.
    assert 'href="/admin/drift"' not in r.text


def test_partner_testing_tabs_hide_engineering_tests(client, partner):
    r = client.get("/admin/evals")
    assert r.status_code == 200
    assert 'class="section-tab' in r.text and 'href="/admin/tests"' not in r.text
    assert "Voice checks" in r.text


def test_partner_build_gates_developer_docs(client, partner):
    for path in ("/admin/docs", "/admin/docs/architecture", "/admin/docs/api"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 307, path
        assert r.headers["location"] == "/admin/help"


def test_partner_search_hides_staff_entries(client, partner):
    def labels(q):
        r = client.get(f"/admin/search.json?q={q}&env=staging")
        return {row["label"] for row in r.json()["results"]}

    assert not ({"Architecture", "Data Model", "Developer docs"} & labels("architecture"))
    assert "Releases" not in labels("promote")
    assert "Config drift" not in labels("drift")
    assert "Environment Variables" not in labels("variables")
    assert "Team & access" in labels("team")


def test_full_build_search_still_finds_staff_entries(client):
    r = client.get("/admin/search.json?q=drift&env=staging")
    assert any(row["label"] == "Config drift" for row in r.json()["results"])


def test_branding_comes_from_the_scope(client, monkeypatch):
    use_scope(monkeypatch, make_scope("orchard", brand_short="Orchard", brand_mark="o",
                                      brand_accent="#2E7D32", brand_accent_dark="#66BB6A"))
    r = client.get("/admin/settings")
    assert 'data-brand="orchard"' in r.text
    assert "--accent:#2E7D32" in r.text          # light accent from the scope
    assert "--accent:#66BB6A" in r.text          # dark accent from the scope
    assert "WAJ Engine · Orchard" in r.text
    use_scope(monkeypatch, make_scope("plain", brand_short="Plain"))
    r = client.get("/admin/settings")
    assert 'data-brand="plain"' in r.text
    assert "WAJ Engine · Plain" in r.text
    assert "--accent:" not in r.text.split("</head>")[0].split("portal.css")[1]  # no accent → teal


def test_full_build_has_no_brand_override(client):
    from admin.nav import brand

    r = client.get("/admin/settings")
    assert "data-brand" not in r.text.split("<head>")[0]
    assert brand()["name"] in r.text


@pytest.mark.parametrize("product, name", [(True, "WAJ Admin"), (False, "WAJ AI Engine")])
def test_default_portal_name_follows_the_build(monkeypatch, client, product, name):
    """The WAJ build (a product plugin) keeps "WAJ Admin"; the engine on its
    own is "WAJ AI Engine" — on the shell, the page titles and the login page."""
    from admin import nav

    full_build(monkeypatch)
    monkeypatch.delenv("PORTAL_NAME", raising=False)
    monkeypatch.setattr(nav, "has_product", lambda: product)
    assert nav.brand()["name"] == nav.brand()["title"] == name
    other = "WAJ AI Engine" if product else "WAJ Admin"
    for path in ("/admin/login", "/admin/settings"):
        r = client.get(path)
        assert r.status_code == 200
        assert name in r.text and other not in r.text, path


@pytest.mark.parametrize("product", [True, False])
def test_portal_name_overrides_either_default(monkeypatch, client, product):
    from admin import nav

    full_build(monkeypatch)
    monkeypatch.setattr(nav, "has_product", lambda: product)
    monkeypatch.setenv("PORTAL_NAME", "Harbor Agents")
    r = client.get("/admin/login")
    assert "Harbor Agents" in r.text
    assert "WAJ Admin" not in r.text and "WAJ AI Engine" not in r.text


# ---------------------------------------------------------------------------
# Error page
# ---------------------------------------------------------------------------


def test_bad_env_param_renders_portal_error_page(client):
    r = client.get("/admin/agents?env=dev", headers=HTML)
    assert r.status_code == 422
    assert "text/html" in r.headers["content-type"]
    assert "This link isn" in r.text
    assert 'href="/admin/agents"' in r.text  # retry without the bad param
    assert 'class="nav-link active" href="/admin/agents"' in r.text


def test_bad_env_param_keeps_json_for_api_callers(client):
    r = client.get("/admin/agents?env=dev")  # TestClient sends Accept: */*
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")


def test_malformed_run_id_is_404_page_not_500(client):
    for path in ("/admin/tests/not-a-uuid", "/admin/e2e/runs/not-a-uuid"):
        r = client.get(path, headers=HTML)
        assert r.status_code == 404, path
        assert "Page not found" in r.text
        assert "Back to" in r.text


def test_unknown_admin_path_renders_error_page(client):
    r = client.get("/admin/nope", headers=HTML)
    assert r.status_code == 404
    assert "Page not found" in r.text


def test_json_routes_keep_json_errors(client):
    r = client.get("/admin/search.json?env=dev", headers=HTML)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# Secret masking (PLAN §2.12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row,masked", [
    ({"name": "OPENAI_API_KEY", "value": "sk-1", "is_secret": False}, "••••"),
    ({"name": "GITHUB_DEPLOY_TOKEN", "value": "ghp", "is_secret": False}, "••••"),
    ({"name": "DB_PASSWORD", "value": "pw", "is_secret": False}, "••••"),
    ({"name": "WEBHOOK_SECRET", "value": "s", "is_secret": False}, "••••"),
    ({"name": "AI_DATABASE_URL", "value": "postgres://postgres:hunter2@db:5432/x", "is_secret": False},
     "postgres://postgres:••••@db:5432/x"),
    ({"name": "REGION", "value": "uaenorth", "is_secret": False}, None),
    ({"name": "KEYBOARD_LAYOUT", "value": "us", "is_secret": False}, None),
    ({"name": "PUBLIC_URL", "value": "https://engine.company.example/x", "is_secret": False}, None),
])
def test_auto_masking_rules(row, masked):
    out = evh._mask(row)
    if masked is None:
        assert out == row
    else:
        assert out["value"] == masked
        assert out["_masked"] is True
        assert "hunter2" not in str(out)


def test_saving_the_masked_value_keeps_the_stored_secret(client):
    from tests.admin.test_env_vars_routes import _FakeClient

    stored = {"var_id": "var-9", "environment": "prod", "name": "OPENAI_API_KEY",
              "value": "sk-real", "source": "db", "is_secret": False}
    fake = _FakeClient(rows_by_table={"agent_env_vars": [stored]})
    with patch.object(evh, "admin_service_client", return_value=fake), \
            patch.object(evh, "record_change"):
        r = client.post("/admin/env-vars/var-9", data={
            "environment": "prod", "name": "OPENAI_API_KEY", "value": evh.SECRET_PLACEHOLDER,
            "source": "db", "description": ""}, follow_redirects=False)
    assert r.status_code == 302
    assert "value" not in fake.queries["agent_env_vars"].last_update


def test_portal_name_can_be_set_for_a_deployment_that_is_not_wajs(monkeypatch):
    from admin import nav

    full_build(monkeypatch)
    monkeypatch.delenv("PORTAL_NAME", raising=False)
    assert nav.brand()["name"] in ("WAJ Admin", "WAJ AI Engine")
    monkeypatch.setenv("PORTAL_NAME", "Harbor Agents")
    b = nav.brand()
    assert b["name"] == b["title"] == "Harbor Agents" and "WAJ" not in b["login_copy"]


def test_a_live_generic_agent_shows_its_api_channel(monkeypatch):
    """It stores no address, so the list said "Not connected" for an agent
    answering at /v1/agents/<slug>/chat."""
    from admin.screens.agents_present import channels_for

    agent = {"agent_kind": "custom", "slug": "support-desk", "status": "active"}
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    assert [c["label"] for c in channels_for(agent)] == ["API"]
    assert "/v1/agents/support-desk/chat" in channels_for(agent)[0]["description"]
    assert [c["key"] for c in channels_for({**agent, "status": "draft"})] == ["none"]
    monkeypatch.delenv("ENABLE_GENERIC_AGENTS")
    assert [c["key"] for c in channels_for(agent)] == ["none"]


def test_chosen_actions_on_a_connected_server_are_counted():
    """The list said "0 actions + external" for an agent with three chosen
    actions on an external server. A server with an open-ended list still
    shows as external, since the count is then only a floor."""
    from admin.screens.agents_views import _bound_count

    external = {"mcp_server_id": "s1", "source": "external", "tool_subset": None}
    pinned = _bound_count([{"mcp_server": external,
                            "tool_subset": ["lookup_customer", "check_outage", "open_ticket"]}])
    assert pinned["count"] == 3 and pinned["external"] is False
    server_pinned = _bound_count([{"mcp_server": {**external, "tool_subset": ["a", "b"]},
                                   "tool_subset": None}])
    assert server_pinned["count"] == 2 and server_pinned["external"] is False
    open_ended = _bound_count([{"mcp_server": external, "tool_subset": None}])
    assert open_ended["count"] == 0 and open_ended["external"] is True
