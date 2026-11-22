"""Static guards for the Test-agent chat widget (admin/static/test-chat.js).

The widget injects its own <style> block, so portal.css dark-mode tokens
only apply if the inline CSS references them. Hardcoded light surfaces
(e.g. background:#fff) render light-on-light in dark mode — that bug
shipped once; these tests pin the fix. Assistant replies render as
markdown through the same marked + DOMPurify pair used by transcripts.
"""
from __future__ import annotations

import re
from pathlib import Path

WIDGET = Path(__file__).resolve().parents[2] / "admin" / "static" / "test-chat.js"


def _src() -> str:
    return WIDGET.read_text()


def test_no_hardcoded_surface_backgrounds():
    src = _src()
    # Literal background hexes/whites are only allowed as var() fallbacks,
    # e.g. var(--surface,#fff). A bare `background:#fff;` breaks dark mode.
    bare = re.findall(r"background:\s*#[0-9A-Fa-f]{3,8}\b", src)
    assert not bare, f"hardcoded background colors break dark mode: {bare}"


def test_text_colors_use_semantic_tokens():
    src = _src()
    # Bubble/test colors must come from the token system so they flip with
    # the theme alongside the backgrounds above.
    bare_color = re.findall(r"color:\s*#[0-9A-Fa-f]{3,8}\b", src)
    assert not bare_color, f"hardcoded text colors break dark mode: {bare_color}"
    for token in ("--fg-1", "--accent-fg", "--surface", "--bg", "--error-soft"):
        assert f"var({token}" in src, f"widget CSS should reference {token}"


def test_assistant_replies_render_markdown():
    src = _src()
    assert "renderAssistantMarkdown" in src
    assert "DOMPurify.sanitize" in src
    assert "marked.parse" in src
    # Streaming path re-renders the accumulated text, not textContent.
    assert "renderAssistantMarkdown(bubble, acc)" in src
    # User input must stay plain text — never parsed as HTML/markdown.
    assert 'appendMessage("user", text)' in src


def test_markdown_bubble_resets_prewrap():
    # .waj-tc-bubble uses pre-wrap for plain text; rendered markdown brings
    # its own block spacing and must switch back to normal whitespace.
    assert ".waj-tc-bubble.markdown-body{white-space:normal" in _src()
