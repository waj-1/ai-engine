"""Tests for runtime/prompts/_catalog.py — pure tool-catalog renderer."""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel


class _NoArgs(BaseModel):
    pass


def _tool(name, desc):
    return StructuredTool.from_function(func=lambda: "x", name=name, description=desc, args_schema=_NoArgs)


def test_renders_one_bullet_per_tool_in_input_order():
    from runtime.prompts._catalog import render_tool_catalog
    out = render_tool_catalog([_tool("get_b", "Bee tool"), _tool("get_a", "Ay tool")])
    assert out == "- `get_b` — Bee tool\n- `get_a` — Ay tool"


def test_collapses_multiline_description_to_single_line():
    from runtime.prompts._catalog import render_tool_catalog
    out = render_tool_catalog([_tool("t", "line one\n  line two\n\nline three")])
    assert out == "- `t` — line one line two line three"


def test_empty_tools_returns_empty_string():
    from runtime.prompts._catalog import render_tool_catalog
    assert render_tool_catalog([]) == ""


def test_tool_without_description_falls_back_to_name():
    from runtime.prompts._catalog import render_tool_catalog
    out = render_tool_catalog([_tool("bare", "")])
    assert out == "- `bare` — bare"
