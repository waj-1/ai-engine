"""The global-search registry covers every portal page, and partner builds
only see what they should.

``test_every_admin_page_is_searchable`` is the guard against a new screen
silently missing from Ctrl-K: every GET route under /admin that is a page
(not parameterised, not JSON/partial/download) needs a registry entry or a
reasoned line in ``ROUTE_EXCLUSIONS``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.scoped_build import use_orchard


def _page_routes():
    from main import app

    out = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None) or set()
        if "GET" not in methods or not path.startswith("/admin"):
            continue
        if "{" in path:                       # parameterised (one record)
            continue
        if path.endswith((".json", ".wav")):  # data endpoints / partials
            continue
        if path.startswith(("/admin/api/", "/admin/static", "/admin/help/media")):
            continue
        out.add(path)
    return out


def test_every_admin_page_is_searchable():
    from admin.search_registry import ROUTE_EXCLUSIONS, registry_paths

    covered = registry_paths()
    missing = sorted(p for p in _page_routes()
                     if (p.rstrip("/") or "/") not in covered and p not in ROUTE_EXCLUSIONS)
    assert not missing, (
        "These /admin pages have no global-search entry. Add them to admin/nav.py "
        "(sidebar/tabs/settings) or admin/search_registry.py EXTRA_PAGES, or list them in "
        f"ROUTE_EXCLUSIONS with a reason: {missing}"
    )


def test_exclusions_are_real_routes():
    """A stale exclusion would hide a future page with the same path."""
    from admin.search_registry import ROUTE_EXCLUSIONS

    routes = _page_routes()
    stale = sorted(p for p in ROUTE_EXCLUSIONS if p not in routes)
    assert not stale, f"ROUTE_EXCLUSIONS lists paths that are no longer routes: {stale}"


def test_registry_is_generated_from_nav():
    """Every sidebar item, section tab and Settings link appears — so a nav
    change shows up in search without touching the registry."""
    from admin import nav
    from admin.search_registry import all_entries

    labels = {e.label for e in all_entries()}
    for item in nav.PRIMARY + (nav.SETTINGS,):
        assert item.label in labels
    for tabs in nav.SECTION_TABS.values():
        for t in tabs:
            if t.key != "overview":
                assert t.label in labels, t.label
    for e in nav.SETTINGS_ENTRIES:
        assert e.title in labels
        for label, _href, _note in e.links:
            if len(e.links) > 1:
                assert label in labels, label


def test_every_agent_tab_has_an_entry():
    from admin.search_registry import AGENT_TABS, all_entries

    labels = {e.label for e in all_entries()}
    for t in AGENT_TABS:
        assert f"Agent › {t.label}" in labels


def test_agent_tabs_match_the_agent_page_tabs():
    """One search entry per agent-page tab, each a tab the page really has."""
    from admin.screens.agents_views import DETAIL_TABS
    from admin.search_registry import AGENT_TABS

    assert {t.tab for t in AGENT_TABS} == set(DETAIL_TABS)


def test_agent_tab_href_uses_profile_and_tab_params():
    from admin.search_registry import AGENT_TABS, agent_tab_href

    tab = next(t for t in AGENT_TABS if t.key == "instructions")
    assert agent_tab_href(tab, "p1", "prod") == "/admin/agents?env=prod&profile_id=p1&tab=instructions"


def test_diagnostics_strip_pages_are_all_searchable():
    """Every tab in the Settings › Diagnostics in-page strip has an entry."""
    from admin.search_registry import registry_paths

    tpl = Path(__file__).resolve().parents[2].joinpath(
        "admin/templates/_diagnostics_tabs.html").read_text()
    hrefs = re.findall(r"'(/admin/[^']+)'", tpl)
    assert hrefs
    assert set(hrefs) <= registry_paths()


# --- partner builds -------------------------------------------------------------


@pytest.fixture
def partner(monkeypatch):
    use_orchard(monkeypatch)
    yield


def _labels(client, q):
    r = client.post("/admin/search.json", json={"q": q, "env": "staging"})
    assert r.status_code == 200
    return r.json()["results"]


def test_partner_build_has_no_developer_docs(client, partner):
    for q in ("architecture", "data model", "clickhouse", "?promote", "?audit log", "api reference"):
        rows = _labels(client, q)
        assert not [r for r in rows if r["type"] == "Developer doc"], q
        assert not [r for r in rows if r["href"].startswith("/admin/docs")], q


def test_partner_build_hides_staff_screens_and_actions(client, partner):
    hrefs = [r["href"] for q in ("deploy", "variables", "drift", "promote", "pytest", ">")
             for r in _labels(client, q)]
    for staff in ("/admin/deployments", "/admin/env", "/admin/drift", "/admin/tests", "/admin/lmnr"):
        assert not [h for h in hrefs if h.startswith(staff)], staff


def test_partner_build_still_finds_its_own_screens(client, partner):
    labels = {r["label"] for r in _labels(client, "team")}
    assert "Team & access" in labels
    assert any(r["href"].startswith("/admin/help") for r in _labels(client, "?where things are"))


def test_full_build_finds_developer_docs(client):
    rows = _labels(client, "architecture")
    assert any(r["type"] == "Developer doc" for r in rows)


def test_partner_quick_actions_have_no_staff_entries(monkeypatch):
    use_orchard(monkeypatch)
    from admin.nav import search_quick_actions

    hrefs = [a["href"] for a in search_quick_actions("staging")]
    assert hrefs and not [h for h in hrefs if h.startswith(("/admin/env", "/admin/deployments", "/admin/tests"))]


# --- docs anchors -----------------------------------------------------------------


def test_heading_anchor_rule():
    from admin.search_docs import heading_anchor

    assert heading_anchor("Audit Log") == "audit-log"
    assert heading_anchor("`/admin/live` — Overview") == "adminlive-overview"
    assert heading_anchor("Nav rail → docs (8 items + Settings)") == "nav-rail-docs-8-items-settings"
    assert heading_anchor("[Link](x.md) **bold**") == "link-bold"
    assert heading_anchor("إعدادات الصوت") == "إعدادات-الصوت"


def test_duplicate_headings_get_numbered_anchors():
    from admin.search_docs import split_sections

    md = "# Page\n\n## Setup\nA\n\n## Setup\nB\n\n```\n## not a heading\n```\n"
    secs = split_sections(md, corpus="help", page_slug="p", page_title="Page", base_href="/admin/help/p")
    anchors = [s.anchor for s in secs if s.heading]
    assert anchors == ["setup", "setup-1"]


def test_heading_anchor_rule_matches_docs_template():
    """templates/docs.html assigns heading ids client-side; its slug rule
    must be the one heading_anchor implements."""
    tpl = Path(__file__).resolve().parents[2].joinpath("admin/templates/docs.html").read_text()
    m = re.search(r"function slugify\(text\) \{\s*return (.+?);\s*\}", tpl, re.S)
    assert m, "docs.html lost its slugify()"
    body = m.group(1)
    assert r"[^\p{L}\p{N}\p{M}_\s-]" in body and r"/\s+/g, '-'" in body and "toLowerCase" in body
    assert "seenSlugs[s] += 1" in tpl  # duplicate numbering, same as split_sections
