"""Route smoke tests for the /admin/evals tab surface (Task 2.1).

Suites & Runs (/admin/evals) already has coverage elsewhere (test_smoke.py,
test_evals_list_combined.py). This file covers the two new tabs —
Production monitoring (/admin/evals/scheduling) and Bake-offs
(/admin/evals/bakeoffs) — plus the shared tab bar rendering the right
active tab on each page.

Uses the session-scoped `client` fixture from tests/admin/conftest.py
(TestClient + require_superuser bypass). There is no `admin_client`
fixture in this repo — `client` is the established name.
"""
from __future__ import annotations

import re

import pytest


def _active_tab_href(html: str) -> str | None:
    """Extract the href of the single Testing section tab marked current.

    The evals screens' own tab bar folded into the Testing area's section
    tabs (_components/section_tabs.html, rendered by _base.html from
    admin/nav.py). The macro spreads each anchor's attributes across lines,
    so match across whitespace.
    """
    m = re.search(
        r'<a class="section-tab active"\s*aria-current="true"\s*href="([^"]+)"',
        html,
    )
    return m.group(1) if m else None


def test_scheduling_route(client):
    r = client.get("/admin/evals/scheduling")
    assert r.status_code == 200
    assert "Schedules" in r.text


def test_bakeoffs_route(client):
    r = client.get("/admin/evals/bakeoffs")
    assert r.status_code == 200
    assert "Model comparison" in r.text


def test_scheduling_tab_bar_marks_scheduling_active(client):
    r = client.get("/admin/evals/scheduling")
    assert r.status_code == 200
    assert 'class="section-tabs"' in r.text
    # Exactly one tab is marked active/current, and it's the scheduling one.
    assert r.text.count('aria-current="true"') == 1
    assert _active_tab_href(r.text) == "/admin/evals/scheduling"


def test_bakeoffs_tab_bar_marks_bakeoffs_active(client):
    r = client.get("/admin/evals/bakeoffs")
    assert r.status_code == 200
    assert r.text.count('aria-current="true"') == 1
    assert _active_tab_href(r.text) == "/admin/evals/bakeoffs"


def test_overview_tab_bar_marks_overview_active(client):
    """/admin/evals is the Testing Overview — its tab is the current one."""
    r = client.get("/admin/evals")
    assert r.status_code == 200
    assert 'class="section-tabs"' in r.text
    assert r.text.count('aria-current="true"') == 1
    assert _active_tab_href(r.text) == "/admin/evals"


@pytest.mark.parametrize("path", ["/admin/evals/suites", "/admin/evals/runs"])
def test_suites_and_runs_tabs_marked_active(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.text.count('aria-current="true"') == 1
    assert _active_tab_href(r.text) == path


def test_old_combined_page_links_land_on_runs(client):
    """/admin/evals?suite_id=… (the old Suites & Runs page with filters) now
    lands on the Runs tab with the same filters."""
    r = client.get("/admin/evals?suite_id=abc&status=failed", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].startswith("/admin/evals/runs?")
    assert "suite_id=abc" in r.headers["location"]


def test_all_three_tabs_link_to_each_other(client):
    """Each of the three pages renders links to the other two tabs, so an
    operator can navigate the whole surface from any one of them."""
    for path in ("/admin/evals", "/admin/evals/suites", "/admin/evals/runs",
                 "/admin/evals/scheduling", "/admin/evals/bakeoffs"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert 'href="/admin/evals"' in r.text
        assert 'href="/admin/evals/suites"' in r.text
        assert 'href="/admin/evals/runs"' in r.text
        assert 'href="/admin/evals/scheduling"' in r.text
        assert 'href="/admin/evals/bakeoffs"' in r.text
