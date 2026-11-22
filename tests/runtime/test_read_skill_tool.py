"""render_skill_catalog + the synthetic read_skill tool."""
from __future__ import annotations

from runtime.prompts._catalog import render_skill_catalog
from runtime.tools.skills import READ_SKILL_TOOL_NAME, build_read_skill_tool

_ENTRIES = [
    {"skill": {"slug": "refund-note", "description": "format a  refund\nnote",
               "always_on": False},
     "version": {"version_number": 2, "content": "Refund body with {braces}"},
     "resources": [{"filename": "examples.md", "content_type": "text/markdown",
                    "content": "example text"}],
     "linked_data_sources": [{"source_id": "ds1", "name": "Policies KB"}],
     "position": 0, "scope": "platform"},
    {"skill": {"slug": "no-shows", "description": "our no-show policy", "always_on": True},
     "version": {"version_number": 1, "content": "tenant body"},
     "resources": [], "linked_data_sources": [], "position": 1000, "scope": "tenant"},
]


def test_render_skill_catalog_lines():
    out = render_skill_catalog(_ENTRIES)
    assert "- `refund-note` — format a refund note" in out  # whitespace collapsed
    assert "- `no-shows` — our no-show policy" in out


def test_render_skill_catalog_empty():
    assert render_skill_catalog([]) == ""


def test_read_skill_returns_body_files_and_kb_pointer():
    tool = build_read_skill_tool(_ENTRIES)
    assert tool.name == READ_SKILL_TOOL_NAME
    out = tool.func("refund-note")
    assert "Refund body with {braces}" in out          # braces survive verbatim
    assert "examples.md" in out
    assert "Policies KB" in out


def test_read_skill_reads_bundled_file():
    tool = build_read_skill_tool(_ENTRIES)
    assert tool.func("refund-note", "examples.md") == "example text"


def test_read_skill_unknown_slug_and_file_are_model_recoverable():
    tool = build_read_skill_tool(_ENTRIES)
    out = tool.func("nope")
    assert "Unknown skill" in out and "refund-note" in out
    out2 = tool.func("refund-note", "missing.md")
    assert "no file" in out2 and "examples.md" in out2
