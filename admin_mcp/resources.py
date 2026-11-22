"""Admin-portal operator docs exposed as MCP resources (read-only)."""
from __future__ import annotations

from pathlib import Path

from admin_mcp.server import mcp

_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs" / "admin-portal"

# Local duplicate of admin.router.DOC_PAGES (slug → file pairs).
# This avoids importing admin.router at resource-call time, which would load
# heavy templates. Tested for drift in test_resources.py.
_PAGES = {
    "index": "README.md",
    "operate": "01-operate.md",
    "configure": "02-configure.md",
    "test": "03-test.md",
    "govern": "04-govern.md",
    "deploy": "05-deploy.md",
    "mcp": "06-mcp.md",
    "architecture": "00-architecture.md",
    "data-model": "data-model.md",
}


@mcp.resource("wajdocs://{slug}")
def get_doc(slug: str) -> str:
    """Admin-portal operator documentation page (markdown).

    Args:
        slug: Document slug (e.g., 'index', 'operate', 'configure').

    Returns:
        Markdown content of the doc, or a helpful message if slug is unknown.
    """
    if slug not in _PAGES:
        known = ", ".join(sorted(_PAGES.keys()))
        return f"Unknown doc slug {slug!r}. Available: {known}"

    file_path = _DOCS_DIR / _PAGES[slug]
    try:
        return file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"Documentation file for slug {slug!r} (`{_PAGES[slug]}`) is missing."
