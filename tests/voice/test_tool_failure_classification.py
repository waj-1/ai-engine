"""Classifying a raw tool result as a FAILURE, not a shape a renderer declined.

`render_tool_result` (voice/live/rendering.py) has always returned None for
three different reasons a caller cannot tell apart: no renderer is
registered, the payload is not JSON, or a renderer looked at a normal shape
and declined. A live call on 2026-08-20 showed why that conflation is
dangerous for a SIDE-EFFECTING tool: `create_ticket` returned

    HTTP 422 error: {"detail": "branch is required when no known terminal_id
    is supplied"}

-- not valid JSON, so it fell through `render_tool_result` exactly like an
unparseable-but-harmless shape, and the model was free to phrase the
outcome. It phrased a fabricated success (an invented ticket number, branch,
and visit time).

`classify_tool_failure` recognises the three shapes this codebase actually
produces for a failed tool call (verified against runtime/tools/
_http_adapter.py and a product's hand-written tool wrappers) and exposes
enough structure — status code, raw detail, parsed JSON body when the detail
happens to be one — for a per-tool failure renderer to write a truthful,
specific decline. It does NOT replace render_tool_result's existing
contract: an ordinary non-error payload, or a renderer that declines on one,
is untouched by this module.
"""
from __future__ import annotations

import pytest

from voice.live.rendering import (
    ToolFailure,
    classify_tool_failure,
    register_failure_renderer,
    registered_failure_tools,
    render_tool_failure,
)


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    monkeypatch.setattr("voice.live.rendering._FAILURE_RENDERERS", {})


# -- classify_tool_failure: the three real failure shapes -------------------

def test_http_status_error_string_is_classified():
    # runtime/tools/_http_adapter.py:217 -- f"HTTP {status} error: {body[:500]}"
    raw = 'HTTP 422 error: {"detail": "branch is required when no known terminal_id is supplied"}'
    failure = classify_tool_failure(raw)
    assert failure is not None
    assert failure.kind == "http_status"
    assert failure.status_code == 422
    assert "branch is required" in failure.detail
    # The body happened to be JSON -- parsed for a renderer that wants it.
    assert failure.body == {"detail": "branch is required when no known terminal_id is supplied"}


def test_http_status_error_with_a_non_json_body_still_classifies():
    raw = "HTTP 500 error: internal server error"
    failure = classify_tool_failure(raw)
    assert failure is not None
    assert failure.kind == "http_status"
    assert failure.status_code == 500
    assert failure.detail == "internal server error"
    assert failure.body is None


def test_http_request_failed_string_is_classified():
    # runtime/tools/_http_adapter.py:211,213 -- f"HTTP request failed: {exc}"
    raw = "HTTP request failed: Connection timed out"
    failure = classify_tool_failure(raw)
    assert failure is not None
    assert failure.kind == "http_request_failed"
    assert failure.status_code is None
    assert "Connection timed out" in failure.detail


def test_wrapper_error_json_dict_is_classified():
    # a product's hand-written tool wrappers -- json.dumps({"error": str(exc)})
    raw = '{"error": "MCP call failed: timeout"}'
    failure = classify_tool_failure(raw)
    assert failure is not None
    assert failure.kind == "wrapper_error"
    assert failure.status_code is None
    assert "timeout" in failure.detail


def test_wrapper_error_as_a_bare_dict_is_classified():
    # A tool may hand back an already-parsed dict rather than a JSON string.
    failure = classify_tool_failure({"error": "boom"})
    assert failure is not None
    assert failure.kind == "wrapper_error"
    assert failure.detail == "boom"


# -- everything else is NOT a failure — the fail-open half ------------------

def test_a_normal_success_payload_is_not_a_failure():
    assert classify_tool_failure({"iban": "SA4420000001234567891234"}) is None
    assert classify_tool_failure('{"status": "sent", "to": "1234"}') is None


def test_unparseable_non_error_text_is_not_a_failure():
    # e.g. search_docs' prose fallback -- not one of the three shapes.
    assert classify_tool_failure("Documentation search is temporarily unavailable.") is None


def test_a_falsy_error_value_is_not_a_failure():
    assert classify_tool_failure({"error": ""}) is None
    assert classify_tool_failure({"error": None}) is None


def test_non_string_non_dict_is_not_a_failure():
    assert classify_tool_failure(None) is None
    assert classify_tool_failure(42) is None
    assert classify_tool_failure(["x"]) is None


# -- render_tool_failure: the parallel registry, same fail-open shape -------

def test_unregistered_tool_failure_renders_nothing():
    failure = ToolFailure(kind="wrapper_error", status_code=None, detail="boom", body=None)
    assert render_tool_failure("some_tool", failure, "en") is None


def test_registered_failure_renderer_is_used():
    register_failure_renderer("t", lambda failure, lang: f"{lang}:{failure.detail}")
    failure = ToolFailure(kind="wrapper_error", status_code=None, detail="x", body=None)
    assert render_tool_failure("t", failure, "ar") == "ar:x"


def test_a_raising_failure_renderer_falls_through():
    def _boom(failure, lang):
        raise KeyError("shape changed under us")
    register_failure_renderer("t", _boom)
    failure = ToolFailure(kind="wrapper_error", status_code=None, detail="x", body=None)
    assert render_tool_failure("t", failure, "en") is None


def test_a_failure_renderer_returning_none_falls_through():
    register_failure_renderer("t", lambda failure, lang: None)
    failure = ToolFailure(kind="wrapper_error", status_code=None, detail="x", body=None)
    assert render_tool_failure("t", failure, "en") is None


def test_a_failure_renderer_returning_blank_falls_through():
    register_failure_renderer("t", lambda failure, lang: "   ")
    failure = ToolFailure(kind="wrapper_error", status_code=None, detail="x", body=None)
    assert render_tool_failure("t", failure, "en") is None


def test_registered_failure_tools_is_introspectable():
    register_failure_renderer("a", lambda f, l: "x")
    register_failure_renderer("b", lambda f, l: "y")
    assert registered_failure_tools() == frozenset({"a", "b"})
