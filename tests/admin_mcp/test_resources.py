"""Tests for admin_mcp.resources (admin-portal docs as MCP resources)."""
from __future__ import annotations

from admin_mcp.resources import get_doc, _PAGES
from admin.router import DOC_PAGES
from tests.admin_mcp.conftest import rpc


class TestGetDoc:
    """Test the get_doc resource function."""

    def test_get_doc_index_returns_readme_content(self):
        """get_doc('index') returns README.md content with expected text."""
        content = get_doc("index")
        # Use a stable substring from README.md (checked in brief verification).
        assert "# WAJ AI Admin Portal — Documentation" in content
        assert "source of truth" in content
        assert "docs/admin-portal/" in content

    def test_get_doc_operate_returns_content(self):
        """get_doc('operate') returns markdown content."""
        content = get_doc("operate")
        assert len(content) > 0
        assert isinstance(content, str)

    def test_get_doc_unknown_slug_returns_helpful_message(self):
        """Unknown slug returns message listing available slugs."""
        content = get_doc("unknown_slug")
        assert "Unknown doc slug 'unknown_slug'" in content
        assert "Available:" in content
        # Verify all known slugs are listed.
        for slug in sorted(_PAGES.keys()):
            assert slug in content

    def test_get_doc_empty_slug_returns_helpful_message(self):
        """Empty slug returns message listing available slugs."""
        content = get_doc("")
        assert "Unknown doc slug ''" in content
        assert "Available:" in content

    def test_get_doc_all_pages_readable(self):
        """All documented pages in _PAGES can be read."""
        for slug in _PAGES.keys():
            content = get_doc(slug)
            # Should not error and should return a non-empty string.
            assert isinstance(content, str)
            assert len(content) > 0
            # Should not be the "unknown slug" message.
            assert "Unknown doc slug" not in content


class TestPagesDriftGuard:
    """Test that _PAGES stays in sync with admin.router.DOC_PAGES."""

    def test_pages_matches_doc_pages_slugs(self):
        """_PAGES has exactly the same slugs as DOC_PAGES."""
        doc_slugs = {p["slug"] for p in DOC_PAGES}
        pages_slugs = set(_PAGES.keys())
        assert pages_slugs == doc_slugs, (
            f"Slug mismatch: _PAGES has {pages_slugs}, DOC_PAGES has {doc_slugs}"
        )

    def test_pages_matches_doc_pages_files(self):
        """_PAGES file paths match DOC_PAGES file paths for each slug."""
        doc_by_slug = {p["slug"]: p["file"] for p in DOC_PAGES}
        for slug, file_path in _PAGES.items():
            expected_file = doc_by_slug.get(slug)
            assert expected_file == file_path, (
                f"Slug {slug!r}: _PAGES has {file_path!r}, "
                f"DOC_PAGES has {expected_file!r}"
            )

    def test_pages_order_matches_doc_pages(self):
        """_PAGES keys appear in the same order as DOC_PAGES."""
        doc_slugs = [p["slug"] for p in DOC_PAGES]
        pages_slugs = list(_PAGES.keys())
        assert pages_slugs == doc_slugs, (
            f"Order mismatch: _PAGES order is {pages_slugs}, "
            f"DOC_PAGES order is {doc_slugs}"
        )


class TestMCPResourceProtocol:
    """Test MCP resource protocol: resources/read works for wajdocs:// URIs."""

    def test_resources_read_index(self, mcp_client):
        """resources/read retrieves wajdocs://index (README.md)."""
        resp = rpc(mcp_client, "resources/read", {
            "uri": "wajdocs://index"
        })
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert "contents" in result
        # Verify content is markdown text with the README header.
        content_text = result["contents"][0]["text"]
        assert "# WAJ AI Admin Portal — Documentation" in content_text

    def test_resources_read_operate(self, mcp_client):
        """resources/read retrieves wajdocs://operate."""
        resp = rpc(mcp_client, "resources/read", {
            "uri": "wajdocs://operate"
        })
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert "contents" in result
        content_text = result["contents"][0]["text"]
        # Should be valid markdown (non-empty).
        assert len(content_text) > 0
