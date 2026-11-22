"""Smoke tests for the in-portal docs renderer (/admin/docs).

The same markdown that lives in ai_agent/docs/admin-portal/ is rendered in the
portal. These tests assert the routes serve, that the markdown payload is
embedded for client-side rendering, that the nav is built, and that bad slugs
404 (and can't reach the filesystem).
"""
from __future__ import annotations

import pytest

from admin.router import DOC_PAGES


def test_docs_index_renders(client):
    r = client.get("/admin/docs")
    assert r.status_code == 200
    # Sidebar groups + breadcrumb root.
    assert "Sections" in r.text and "Reference" in r.text
    assert "doc-content" in r.text
    # The Overview markdown is embedded for marked.js to render.
    assert "WAJ AI Admin Portal" in r.text


@pytest.mark.parametrize("slug,marker", [
    ("operate", "Operate"),
    ("configure", "Configure"),
    ("test", "Test"),
    ("govern", "Govern"),
    ("deploy", "Deploy"),
    ("mcp", "MCP Server"),
    ("architecture", "Architecture"),
    ("data-model", "Data Model"),
])
def test_docs_pages_render(client, slug, marker):
    r = client.get(f"/admin/docs/{slug}")
    assert r.status_code == 200, r.text[:200]
    assert marker in r.text
    assert "doc-content" in r.text


def test_every_doc_page_has_a_file_and_serves(client):
    for page in DOC_PAGES:
        slug = page["slug"]
        path = "/admin/docs" if slug == "index" else f"/admin/docs/{slug}"
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        # The page's own markdown file must exist (no "is missing" fallback).
        assert "is missing" not in r.text, f"{page['file']} not found on disk"


def test_docs_unknown_slug_404s(client):
    r = client.get("/admin/docs/not-a-page")
    assert r.status_code == 404


def test_docs_slug_cannot_traverse(client):
    # slug is only ever a dict key; traversal attempts must 404, never read a file.
    r = client.get("/admin/docs/..%2F..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 404
