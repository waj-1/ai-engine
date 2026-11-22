"""A renderer may never guess. Anything unexpected returns None and the turn
falls back to the model's own synthesis call.

These tool results come from an EXTERNAL MCP server whose shape we do not own
— the mock server today, a real partner integration tomorrow. Fail-open is
the whole safety argument: the failure mode is "we didn't save a round trip",
never "we said something wrong".
"""
import pytest

from voice.live.rendering import (
    register_renderer, render_tool_result, registered_tools,
)


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    monkeypatch.setattr("voice.live.rendering._RENDERERS", {})


def test_unregistered_tool_renders_nothing():
    assert render_tool_result("get_sales_summary", {"total": 5}, "en") is None


def test_registered_renderer_is_used():
    register_renderer("t", lambda result, lang: f"{lang}:{result['v']}")
    assert render_tool_result("t", {"v": "x"}, "ar") == "ar:x"


def test_json_string_results_are_parsed():
    register_renderer("t", lambda result, lang: result.get("v"))
    assert render_tool_result("t", '{"v": "parsed"}', "en") == "parsed"


def test_unparseable_result_renders_nothing():
    register_renderer("t", lambda result, lang: "never")
    assert render_tool_result("t", "not json at all", "en") is None


def test_non_mapping_result_renders_nothing():
    register_renderer("t", lambda result, lang: "never")
    assert render_tool_result("t", "[1, 2, 3]", "en") is None


def test_a_renderer_returning_none_falls_through():
    register_renderer("t", lambda result, lang: None)
    assert render_tool_result("t", {"v": 1}, "en") is None


def test_a_renderer_returning_blank_falls_through():
    register_renderer("t", lambda result, lang: "   ")
    assert render_tool_result("t", {"v": 1}, "en") is None


def test_a_raising_renderer_falls_through():
    def _boom(result, lang):
        raise KeyError("shape changed under us")
    register_renderer("t", _boom)
    assert render_tool_result("t", {"v": 1}, "en") is None


def test_a_tool_result_carrying_an_error_renders_nothing():
    # The model is better at apologising than a template is, and an error
    # payload has no fields for a template to read anyway.
    register_renderer("t", lambda result, lang: "should not be reached")
    assert render_tool_result("t", {"error": "missing token"}, "en") is None


def test_registered_tools_is_introspectable():
    register_renderer("a", lambda r, l: "x")
    register_renderer("b", lambda r, l: "y")
    assert registered_tools() == frozenset({"a", "b"})


def test_a_renderer_returning_a_truthy_int_falls_through():
    # Non-string return from a renderer falls back; .strip() would raise.
    register_renderer("t", lambda result, lang: 5)
    assert render_tool_result("t", {"v": 1}, "en") is None


def test_a_renderer_returning_a_list_falls_through():
    # Non-string return from a renderer falls back; .strip() would raise.
    register_renderer("t", lambda result, lang: ["x"])
    assert render_tool_result("t", {"v": 1}, "en") is None


def test_a_renderer_returning_true_falls_through():
    # Non-string return from a renderer falls back; .strip() would raise.
    register_renderer("t", lambda result, lang: True)
    assert render_tool_result("t", {"v": 1}, "en") is None
