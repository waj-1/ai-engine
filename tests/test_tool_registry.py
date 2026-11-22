"""Unit tests for core.tool_registry agent-kind accumulation.

Regression guard for the bug where `search_documentation` — built by two
agents' tool factories — ended up tagged with the second agent's kind only
(last registration wins), so the first agent's profile seed never picked it
up and its docs RAG silently returned "Tool not found".
"""
import core.tool_registry as reg
from core.tool_registry import ToolSpec, register_tool, get_spec


def _spec(name: str, kind: str) -> ToolSpec:
    return ToolSpec(
        python_name=name,
        display_name=name.replace("_", " ").title(),
        description="",
        category=kind,
        scope="read",
        source_file=f"runtime/{kind}.py",
        agent_kind=kind,
    )


def _cleanup(*names: str) -> None:
    for n in names:
        reg._REGISTRY.pop(n, None)


def test_single_registration_has_no_additional_kinds():
    name = "_t_single_tool"
    _cleanup(name)
    try:
        register_tool(_spec(name, "business"))
        s = get_spec(name)
        assert s.agent_kind == "business"
        assert s.additional_agent_kinds == ()
    finally:
        _cleanup(name)


def test_tool_registered_under_two_kinds_accumulates():
    name = "_t_shared_tool"
    _cleanup(name)
    try:
        register_tool(_spec(name, "business"))   # first
        register_tool(_spec(name, "sales"))      # clobbers primary, keeps business
        s = get_spec(name)
        assert s.agent_kind == "sales"
        assert "business" in s.additional_agent_kinds
        # The tool now belongs to BOTH agents.
        assert set([s.agent_kind, *s.additional_agent_kinds]) == {"business", "sales"}
    finally:
        _cleanup(name)


def test_three_kinds_accumulate_without_duplicates():
    name = "_t_triple_tool"
    _cleanup(name)
    try:
        register_tool(_spec(name, "business"))
        register_tool(_spec(name, "sales"))
        register_tool(_spec(name, "support"))
        register_tool(_spec(name, "support"))  # idempotent re-register
        s = get_spec(name)
        kinds = {s.agent_kind, *s.additional_agent_kinds}
        assert kinds == {"business", "sales", "support"}
        # no duplicates in the additional list
        assert len(s.additional_agent_kinds) == len(set(s.additional_agent_kinds))
        assert s.agent_kind not in s.additional_agent_kinds
    finally:
        _cleanup(name)
