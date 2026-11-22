"""Speak a tool result without asking the model to phrase it.

Removes the SECOND LLM call from a delegated voice turn for results whose
answer is structurally fixed — a code was sent, a ticket was opened, this is
the stored IBAN. Measured on a client demo VM 2026-08-19: that call is
1098ms p50 and 43% of voice tool calls are this shape.

It is a SAFETY mechanism before it is a latency one. PR #137 exists because a
live call read back `SA1235364241827182758214` — an IBAN matching neither the
merchant's record nor the number he had dictated minutes earlier. A renderer
emits the digits present in the tool result or returns None; it is
structurally incapable of inventing one.

Analytics results are deliberately NOT rendered here. The caller's question
varies ("compared to last week?", "which branch was worst?") and the model's
synthesis call is what adapts the data to the question actually asked. A
template there would be fast and non-responsive — the same failure as a fast
brain answering a hard question badly.

Every failure path of `render_tool_result` returns None and the caller falls
back to the model. These results come from an external MCP server whose
shape we do not own — and until 2026-08-20 that included a FAILED call: a
non-JSON `"HTTP 422 error: ..."` string fell through the same `except
(json.JSONDecodeError, TypeError): return None` as a harmless unparseable
shape, so a failed ticket-creation tool reached the model exactly like a
tool it had never called, free to narrate whatever outcome it liked. A live
call did: an invented ticket number, branch, and technician visit time, on
top of an HTTP 422 that opened nothing.

`classify_tool_failure` (below) exists to make that distinction visible to a
caller: "this tool FAILED" is now a positive signal, not a shade of "no
renderer / not parseable" that both degrade identically to model authorship.
It is deliberately a SEPARATE function from `render_tool_result` rather than
a change to its contract — every existing success-path caller is unaffected,
byte for byte.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# (result_mapping, language_primary_subtag) -> spoken text, or None to decline.
ToolResultRenderer = Callable[[Dict[str, Any], str], Optional[str]]

_RENDERERS: Dict[str, ToolResultRenderer] = {}


def register_renderer(tool_name: str, fn: ToolResultRenderer) -> None:
    """Register `fn` as the deterministic renderer for `tool_name`."""
    _RENDERERS[tool_name] = fn


def registered_tools() -> frozenset[str]:
    """Tool names with a renderer — for tests."""
    return frozenset(_RENDERERS)


def render_tool_result(
    tool_name: str, raw: Any, language: str
) -> Optional[str]:
    """Spoken text for `raw`, or None to let the model phrase it.

    None means "fall back", and it is returned for every unexpected input:
    no renderer, unparseable payload, non-mapping payload, an error payload,
    a renderer that declines, a renderer that returns blank, or a renderer
    that raises because the upstream shape changed.
    """
    fn = _RENDERERS.get(tool_name)
    if fn is None:
        return None

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        parsed = raw
    if not isinstance(parsed, dict):
        return None
    # An error payload has no fields to render, and the model apologises
    # better than a template does.
    if parsed.get("error"):
        return None

    primary = (language or "").split("-")[0].strip().lower() or "en"
    try:
        out = fn(parsed, primary)
    except Exception:
        logger.debug(
            "voice rendering: %s renderer declined on an unexpected shape",
            tool_name, exc_info=True,
        )
        return None
    if not isinstance(out, str) or not out.strip():
        return None
    return out.strip()


# --- Failure classification -------------------------------------------------
#
# `render_tool_result` answers "what does a SUCCESSFUL result say". This half
# answers a different question — "did the tool FAIL" — for the fixed set of
# shapes this codebase's tool-calling layer actually produces. It is
# deliberately tool-name-agnostic: the same three shapes come out of
# runtime/tools/_http_adapter.py (portal-configured HTTP tools, e.g. a
# create_ticket/get_iban/update_iban set) and a product's
# hand-written MCP wrappers (send_otp/verify_otp/the report tools) alike, so
# classification doesn't need to know which tool produced the string.

_HTTP_STATUS_ERROR = re.compile(r"^HTTP (\d+) error:\s*(.*)$", re.DOTALL)
_HTTP_REQUEST_FAILED = re.compile(r"^HTTP request failed:\s*(.*)$", re.DOTALL)


@dataclass(frozen=True)
class ToolFailure:
    """A raw tool result recognised as a FAILED call, not a payload shape.

    `detail` is raw upstream text — English prose from an HTTP error body or
    a Python exception's `str()`. It is a signal for a renderer's marker
    match (a verify_otp renderer reading the failure `reason` is the
    precedent), never something spoken to the caller directly: it can be
    arbitrary upstream text, not vetted for what a merchant should hear.
    `body` is `detail` parsed as a JSON object when it happens to be one
    (an HTTP error body commonly is), else None — convenience for a
    renderer that wants a structured field rather than substring-matching
    the raw text.
    """
    kind: str  # "http_status" | "http_request_failed" | "wrapper_error"
    status_code: Optional[int]
    detail: str
    body: Optional[Dict[str, Any]]


def _try_json_dict(text: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def classify_tool_failure(raw: Any) -> Optional[ToolFailure]:
    """`ToolFailure` if `raw` matches one of the three failure shapes this
    repo's tool-calling layer actually produces, else None.

    Recognised shapes (verified against the source, not guessed):
      - `_http_adapter.py`'s `f"HTTP {status} error: {body[:500]}"` — a
        portal-configured HTTP tool's non-2xx response.
      - `_http_adapter.py`'s `f"HTTP request failed: {exc}"` — a
        network-level failure (timeout, DNS, connection refused, …).
      - `json.dumps({"error": str(exc)})` — the shape every hand-written
        tool closure in a product's tool module returns from
        its `except` block; also accepted as an already-parsed dict, since
        a tool may hand one back directly rather than a JSON string.

    None means "not a failure by this classifier" — that covers a genuine
    success payload, an unparseable string that ISN'T one of the two HTTP
    prefixes above (e.g. a docs-search tool's prose fallback, which is a
    read-only tool's outage message, not a structured failure), and a dict
    with no truthy `error` key. None here is NOT "fall back to the model" —
    that verdict belongs to the caller, which for `render_tool_result`
    itself is unaffected: this function doesn't change what None from THAT
    one means.
    """
    if isinstance(raw, dict):
        if raw.get("error"):
            return ToolFailure(
                kind="wrapper_error", status_code=None,
                detail=str(raw["error"]).strip(), body=raw,
            )
        return None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    match = _HTTP_STATUS_ERROR.match(text)
    if match:
        detail = match.group(2).strip()
        return ToolFailure(
            kind="http_status", status_code=int(match.group(1)),
            detail=detail, body=_try_json_dict(detail),
        )
    match = _HTTP_REQUEST_FAILED.match(text)
    if match:
        detail = match.group(1).strip()
        return ToolFailure(
            kind="http_request_failed", status_code=None,
            detail=detail, body=_try_json_dict(detail),
        )
    parsed = _try_json_dict(text)
    if parsed is not None and parsed.get("error"):
        return ToolFailure(
            kind="wrapper_error", status_code=None,
            detail=str(parsed["error"]).strip(), body=parsed,
        )
    return None


# (ToolFailure, language_primary_subtag) -> spoken text, or None to decline.
ToolFailureRenderer = Callable[[ToolFailure, str], Optional[str]]

_FAILURE_RENDERERS: Dict[str, ToolFailureRenderer] = {}


def register_failure_renderer(tool_name: str, fn: ToolFailureRenderer) -> None:
    """Register `fn` as the deterministic FAILURE renderer for `tool_name`."""
    _FAILURE_RENDERERS[tool_name] = fn


def registered_failure_tools() -> frozenset[str]:
    """Tool names with a failure renderer — for tests."""
    return frozenset(_FAILURE_RENDERERS)


def render_tool_failure(
    tool_name: str, failure: ToolFailure, language: str
) -> Optional[str]:
    """Spoken text for a classified `failure`, or None.

    None here does NOT mean "let the model phrase it" — a caller reaching
    this function has already decided the tool failed and that the model
    must not author the outcome (a product agent's deterministic failure
    decline is the backstop this feeds). None means
    "no tool-specific wording exists for this failure" and the caller must
    fall back to its own fixed, generic truthful line — never to the model.
    """
    fn = _FAILURE_RENDERERS.get(tool_name)
    if fn is None:
        return None
    primary = (language or "").split("-")[0].strip().lower() or "en"
    try:
        out = fn(failure, primary)
    except Exception:
        logger.debug(
            "voice rendering: %s failure renderer declined on an "
            "unexpected shape", tool_name, exc_info=True,
        )
        return None
    if not isinstance(out, str) or not out.strip():
        return None
    return out.strip()
