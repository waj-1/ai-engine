"""Partner Help (/admin/help) with section folders.

docs/partner/NN-section/NN-page.md → /admin/help/section/page: grouped
sidebar in numeric order with prefixes stripped, relative links across
folders, media from subfolder pages, previous/next links, partner access.
"""
from __future__ import annotations

import json
import re

import pytest

from admin import router
from tests.scoped_build import use_agents_demo


@pytest.fixture
def help_tree(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text(
        "# Help\n\nStart with [the welcome](01-getting-started/01-welcome.md).\n", encoding="utf-8")
    gs = tmp_path / "01-getting-started"
    gs.mkdir()
    (gs / "01-welcome.md").write_text(
        "# Welcome to the portal\n\n"
        "![Sidebar](media/getting-started/tour.png)\n\n"
        "![Again](../media/getting-started/tour.png)\n\n"
        '<video src="media/getting-started/first.mp4" controls muted playsinline width="100%"></video>\n\n'
        "See [testing](../02-agents/08-test-your-agent.md#chat), "
        "[next](02-first-15-minutes.md), [home](../README.md), "
        "[web](https://example.com/x.md) and [here](#top).\n\n"
        "```\n[not a link](../02-agents/08-test-your-agent.md)\n```\n",
        encoding="utf-8")
    (gs / "02-first-15-minutes.md").write_text("# Your first 15 minutes\n", encoding="utf-8")
    ag = tmp_path / "02-agents"
    ag.mkdir()
    (ag / "08-test-your-agent.md").write_text("# Test your agent\n", encoding="utf-8")
    (ag / "02-instructions.md").write_text("# Change what your agent says\n", encoding="utf-8")
    ts = tmp_path / "08-team-and-settings"
    ts.mkdir()
    (ts / "01-team.md").write_text("# Team and access\n", encoding="utf-8")
    media = tmp_path / "media" / "getting-started"
    media.mkdir(parents=True)
    (media / "tour.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "media" / "loose.png").write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(router, "HELP_DIR", tmp_path)
    return tmp_path


def _markdown(html: str) -> str:
    m = re.search(r'<script id="doc-md" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_pages_grouped_in_numeric_order_with_prefixes_stripped(help_tree):
    pages = router.help_pages()
    assert [p["slug"] for p in pages] == [
        "index",
        "getting-started/welcome", "getting-started/first-15-minutes",
        "agents/instructions", "agents/test-your-agent",
        "team-and-settings/team",
    ]
    assert [p["group"] for p in pages] == [
        "Start here", "Getting started", "Getting started", "Agents", "Agents", "Team & settings"]
    assert pages[0]["title"] == "Help home"
    # Media folder is never a section.
    assert not any(p["slug"].startswith("media") for p in pages)


def test_sidebar_shows_section_headings_and_section_urls(client, help_tree):
    r = client.get("/admin/help/getting-started/welcome")
    assert r.status_code == 200
    html = r.text
    for heading in ("Getting started", "Agents", "Team &amp; settings"):
        assert heading in html
    headings = re.findall(r'<div class="eyebrow">([^<]+)</div>', html)
    assert headings == ["Start here", "Getting started", "Agents", "Team &amp; settings"]
    assert 'href="/admin/help/agents/instructions"' in html
    # Breadcrumb names the section.
    assert "Getting started › Welcome to the portal" in html


def test_section_page_slugs_are_unique_and_serve(client, help_tree):
    slugs = [p["slug"] for p in router.help_pages()]
    assert len(slugs) == len(set(slugs))
    for slug in slugs:
        path = router.help_url(slug)
        assert client.get(path).status_code == 200, path
    assert client.get("/admin/help/agents/nope").status_code == 404
    assert client.get("/admin/help/02-agents/02-instructions").status_code == 404
    assert client.get("/admin/help/..%2F..%2Fetc/passwd").status_code == 404


def test_relative_links_resolve_across_folders(client, help_tree):
    md = _markdown(client.get("/admin/help/getting-started/welcome").text)
    assert "](/admin/help/agents/test-your-agent#chat)" in md
    assert "](/admin/help/getting-started/first-15-minutes)" in md
    assert "[home](/admin/help)" in md
    # Absolute URLs and anchors are untouched; code blocks are not rewritten.
    assert "](https://example.com/x.md)" in md and "](#top)" in md
    assert "[not a link](../02-agents/08-test-your-agent.md)" in md
    # The Help home links into a section folder.
    home = _markdown(client.get("/admin/help").text)
    assert "](/admin/help/getting-started/welcome)" in home


def test_media_resolves_from_subfolder_pages(client, help_tree):
    md = _markdown(client.get("/admin/help/getting-started/welcome").text)
    assert md.count("](/admin/help/media/getting-started/tour.png)") == 2
    assert 'src="/admin/help/media/getting-started/first.mp4"' in md
    # The viewer allows <video> through the sanitizer.
    assert "ADD_TAGS: ['video', 'source']" in client.get("/admin/help").text


def test_two_segment_media_path_is_served_and_confined(client, help_tree):
    r = client.get("/admin/help/media/loose.png")
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert client.get("/admin/help/media/..%2FREADME.md").status_code == 404


def test_previous_and_next_links(client, help_tree):
    html = client.get("/admin/help/getting-started/first-15-minutes").text
    assert 'rel="prev" href="/admin/help/getting-started/welcome"' in html
    assert 'rel="next" href="/admin/help/agents/instructions"' in html
    first = client.get("/admin/help").text
    assert 'rel="prev"' not in first
    assert 'rel="next" href="/admin/help/getting-started/welcome"' in first
    last = client.get("/admin/help/team-and-settings/team").text
    assert 'rel="next"' not in last and 'rel="prev" href="/admin/help/agents/test-your-agent"' in last


def test_partner_build_can_read_section_pages(client, help_tree, monkeypatch):
    use_agents_demo(monkeypatch)
    r = client.get("/admin/help/agents/instructions")
    assert r.status_code == 200
    assert "Change what your agent says" in r.text
    # Search lists section pages with their portal URL.
    res = client.get("/admin/search.json?q=Change%20what&env=staging").json()["results"]
    assert any(row["href"] == "/admin/help/agents/instructions" for row in res)


def test_real_partner_help_tree_serves_every_page(client):
    """Every page actually shipped in docs/partner renders, and every relative
    .md link in it resolves to a Help page."""
    pages = router.help_pages()
    files = {p["file"]: p["slug"] for p in pages}
    for p in pages:
        r = client.get(router.help_url(p["slug"]))
        assert r.status_code == 200, p["file"]
        md = (router.HELP_DIR / p["file"]).read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)\s#]+\.md)(?:#[^)]*)?\)", md):
            if re.match(r"^[a-z]+:", target):
                continue
            assert router._help_resolve(target, p["file"], files), f"{p['file']}: broken link {target}"
