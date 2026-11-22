"""prompt-vs-bound-tools guard: flag tools a prompt mandates but binding omits.

Regression cover for #1032 (twice): the business system prompt hard-codes
"you MUST call `search_documentation`" but the resolved profile did not bind
that tool, so the model called it and dispatch returned "Tool not found".
"""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel


class _NoArgs(BaseModel):
    pass


def _tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda: "ok", name=name, description=name, args_schema=_NoArgs)


# --------------------------------------------------------------------------
# prompt_mandated_unbound_tools
# --------------------------------------------------------------------------

def test_flags_mandated_tool_that_is_not_bound():
    from runtime.prompts._tool_guard import prompt_mandated_unbound_tools
    prompt = "For any HOW-TO question you MUST call `search_documentation` FIRST."
    known = ["search_documentation", "get_bookings"]
    bound = ["get_bookings"]  # docs tool filtered out
    assert prompt_mandated_unbound_tools(prompt, known, bound) == ["search_documentation"]


def test_no_violation_when_mandated_tool_is_bound():
    from runtime.prompts._tool_guard import prompt_mandated_unbound_tools
    prompt = "You MUST call `search_documentation` FIRST."
    known = ["search_documentation", "get_bookings"]
    bound = ["search_documentation", "get_bookings"]
    assert prompt_mandated_unbound_tools(prompt, known, bound) == []


def test_no_violation_when_prompt_mentions_no_known_tool():
    from runtime.prompts._tool_guard import prompt_mandated_unbound_tools
    prompt = "Answer helpfully and concisely."
    assert prompt_mandated_unbound_tools(prompt, ["get_bookings"], []) == []


def test_accepts_basetool_objects_not_just_names():
    from runtime.prompts._tool_guard import prompt_mandated_unbound_tools
    prompt = "Call `search_documentation`."
    known = [_tool("search_documentation"), _tool("get_bookings")]
    bound = [_tool("get_bookings")]
    assert prompt_mandated_unbound_tools(prompt, known, bound) == ["search_documentation"]


def test_whole_word_match_avoids_substring_false_positive():
    from runtime.prompts._tool_guard import prompt_mandated_unbound_tools
    # prompt references the plural bound tool; the singular known name must NOT match
    prompt = "Use `get_bookings` to list bookings."
    known = ["get_booking"]  # singular — not actually referenced
    bound = []
    assert prompt_mandated_unbound_tools(prompt, known, bound) == []


def test_matches_bare_mention_without_backticks():
    from runtime.prompts._tool_guard import prompt_mandated_unbound_tools
    prompt = "You can ONLY provide documentation using search_documentation."
    assert prompt_mandated_unbound_tools(prompt, ["search_documentation"], []) == [
        "search_documentation"]


def test_returns_sorted_unique_violations():
    from runtime.prompts._tool_guard import prompt_mandated_unbound_tools
    prompt = "Call `list_staff` then `get_clients` then `list_staff` again."
    known = ["list_staff", "get_clients", "calculator"]
    bound = ["calculator"]
    assert prompt_mandated_unbound_tools(prompt, known, bound) == ["get_clients", "list_staff"]


# --------------------------------------------------------------------------
# bound_tool_names_from_attachments (static resolution of a frozen snapshot)
# --------------------------------------------------------------------------

def _member(python_name):
    return {"tool": {"tool_id": f"id-{python_name}", "python_name": python_name,
                     "kind": "python"}}


def _bundle(members, tool_subset=None, endpoint_only=False, source="bundle"):
    return {
        "mcp_server": {"mcp_server_id": "m1", "name": "b", "source": source,
                       "endpoint_only": endpoint_only},
        "tool_subset": tool_subset,
        "member_tools": members,
    }


def test_bound_names_subset_filters_members():
    from runtime.tools._mcp_resolver import bound_tool_names_from_attachments
    att = _bundle([_member("get_bookings"), _member("search_documentation")],
                  tool_subset=["get_bookings"])  # docs omitted from allowlist
    assert bound_tool_names_from_attachments([att]) == {"get_bookings"}


def test_bound_names_no_subset_binds_all_members():
    from runtime.tools._mcp_resolver import bound_tool_names_from_attachments
    att = _bundle([_member("get_bookings"), _member("search_documentation")])
    assert bound_tool_names_from_attachments([att]) == {"get_bookings", "search_documentation"}


def test_bound_names_skips_endpoint_only_server():
    from runtime.tools._mcp_resolver import bound_tool_names_from_attachments
    att = _bundle([_member("x")], endpoint_only=True)
    assert bound_tool_names_from_attachments([att]) == set()


def test_bound_names_unions_multiple_bundles():
    from runtime.tools._mcp_resolver import bound_tool_names_from_attachments
    a = _bundle([_member("a")])
    b = _bundle([_member("b")], tool_subset=["b"])
    assert bound_tool_names_from_attachments([a, b]) == {"a", "b"}
