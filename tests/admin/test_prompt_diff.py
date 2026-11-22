"""Tests for the prompt diff helper + route rendering.

The helper (`build_unified_diff`) is the meat — we exercise it directly
to keep tests fast and free of Supabase / FastAPI plumbing. The route
test rides on the shared `client` fixture from conftest.py and the
default null-Supabase mock to confirm the template parses cleanly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# build_unified_diff — pure-Python helper
# ---------------------------------------------------------------------------


def test_diff_identical_inputs_produces_no_rows():
    from admin.prompt_handlers import build_unified_diff

    rows = build_unified_diff("hello\nworld", "hello\nworld")
    # difflib emits no output when the two inputs are identical.
    assert rows == []


def test_diff_classifies_additions_and_deletions():
    from admin.prompt_handlers import build_unified_diff

    a = "alpha\nbeta\ngamma"
    b = "alpha\nBETA\ngamma\ndelta"
    rows = build_unified_diff(a, b, label_a="v1", label_b="v2")
    kinds = [r["kind"] for r in rows]
    assert "add" in kinds
    assert "del" in kinds
    assert "meta" in kinds  # the +++ / --- header rows.
    # The deleted "beta" must appear as a del row, "BETA"/"delta" as adds.
    del_texts = [r["text"] for r in rows if r["kind"] == "del"]
    add_texts = [r["text"] for r in rows if r["kind"] == "add"]
    assert "beta" in del_texts
    assert "BETA" in add_texts
    assert "delta" in add_texts


def test_diff_strips_trailing_newline_per_row():
    from admin.prompt_handlers import build_unified_diff

    rows = build_unified_diff("a", "b")
    for r in rows:
        assert not r["text"].endswith("\n"), r


def test_diff_labels_are_threaded_into_header():
    from admin.prompt_handlers import build_unified_diff

    rows = build_unified_diff("a", "b", label_a="v7", label_b="v9")
    metas = [r["text"] for r in rows if r["kind"] == "meta"]
    assert any("v7" in m for m in metas)
    assert any("v9" in m for m in metas)


def test_diff_handles_empty_first_version():
    from admin.prompt_handlers import build_unified_diff

    rows = build_unified_diff("", "hello\nworld")
    add_texts = [r["text"] for r in rows if r["kind"] == "add"]
    assert "hello" in add_texts
    assert "world" in add_texts


# ---------------------------------------------------------------------------
# Route — relies on the shared `client` fixture from conftest.py
# ---------------------------------------------------------------------------


def _patched_safe_list_factory(prompt_rows, version_rows_by_number):
    """Return a substitute for `admin.router._safe_list` that satisfies
    the diff handler's three sequential queries: prompt lookup, version
    A lookup, version B lookup.

    Sequencing matters — we hand back results in the order the handler
    asks for them.
    """
    calls = {"n": 0}

    def _sl(_result):
        calls["n"] += 1
        if calls["n"] == 1:
            return prompt_rows
        # The next two calls are the version-A and version-B fetches.
        if calls["n"] == 2:
            return version_rows_by_number.get("a", [])
        return version_rows_by_number.get("b", [])

    return _sl


def _null_client():
    """A MagicMock that returns empty data for every supabase call."""
    mock_result = MagicMock()
    mock_result.data = []
    builder = MagicMock()
    builder.execute.return_value = mock_result
    for m in ("select", "eq", "order", "limit", "range", "gte", "in_",
              "insert", "update", "delete", "neq"):
        getattr(builder, m).return_value = builder
    c = MagicMock()
    c.table.return_value = builder
    return c


def test_prompt_diff_route_404_when_prompt_missing(client):
    """Empty prompt result → 404 from the route handler."""
    with patch("admin.prompt_handlers.get_service_client", return_value=_null_client()):
        r = client.get("/admin/prompts/00000000-0000-0000-0000-000000000999/diff?a=1&b=2")
    assert r.status_code == 404


def test_prompt_diff_route_renders_when_both_versions_present(client):
    """End-to-end: handler loads two versions, template renders diff rows."""
    prompt_rows = [{
        "prompt_id": "p-1", "agent_kind": "support_bot", "slug": "system",
    }]
    version_rows = {
        "a": [{
            "version_number": 1, "content": "old text",
            "created_at": "2026-05-20T12:00:00Z", "created_by": "alice",
        }],
        "b": [{
            "version_number": 2, "content": "new text and more",
            "created_at": "2026-05-20T12:30:00Z", "created_by": "bob",
        }],
    }
    sl = _patched_safe_list_factory(prompt_rows, version_rows)
    with patch("admin.prompt_handlers._safe_list", side_effect=sl), \
         patch("admin.prompt_handlers.get_service_client", return_value=_null_client()):
        r = client.get("/admin/prompts/p-1/diff?a=1&b=2")
    assert r.status_code == 200, r.text[:300]
    # Template renders metadata + diff rows.
    assert "Prompt diff" in r.text
    assert "v1" in r.text
    assert "v2" in r.text
    # The diff payload mentions added words from the new version.
    assert "more" in r.text


def test_prompt_diff_route_rejects_invalid_query(client):
    """`a` and `b` are required ge=1 — missing or zero is a 4xx."""
    with patch("admin.prompt_handlers.get_service_client", return_value=_null_client()):
        r = client.get("/admin/prompts/p-1/diff")
        assert r.status_code in (400, 422)
        r = client.get("/admin/prompts/p-1/diff?a=0&b=1")
        assert r.status_code in (400, 422)
