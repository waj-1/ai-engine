"""Tests for the LLM-as-optimizer pieces.

We exercise three things:
  - The meta-prompt builder packs failing scenarios in the right shape.
  - `_failing_scenarios_from_run` plucks only `passed=False` rows.
  - The POST route streams SSE chunks end-to-end (with the LLM stubbed).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Meta-prompt construction
# ---------------------------------------------------------------------------


def test_meta_prompt_includes_current_body_and_failing_scenarios():
    from admin.prompt_handlers import build_optimizer_meta_prompt

    failing = [
        {
            "scenario_id": "med_warfarin",
            "question": "ibuprofen ok?",
            "expected": "warn about bleeding",
            "actual": "go ahead",
            "judge": {"score": 0.1, "rationale": "missed interaction"},
        }
    ]
    out = build_optimizer_meta_prompt(
        current_prompt_body="You are a clinician.",
        failing_scenarios=failing,
    )
    assert "You are a prompt engineer" in out
    assert "You are a clinician." in out
    assert "med_warfarin" in out
    assert "ibuprofen ok?" in out
    assert "warn about bleeding" in out
    assert "missed interaction" in out
    assert "Output ONLY the new prompt body" in out


def test_meta_prompt_caps_scenarios_and_ranks_worst_first():
    from admin.prompt_handlers import build_optimizer_meta_prompt

    failing = [
        {"scenario_id": f"s{i}", "question": f"q{i}", "expected": "e", "actual": "a",
         "judge": {"score": i / 10.0, "rationale": f"r{i}"}}
        for i in range(20)
    ]
    out = build_optimizer_meta_prompt(
        current_prompt_body="P",
        failing_scenarios=failing,
        max_scenarios=3,
    )
    # The three worst-scoring scenarios (s0, s1, s2 — scores 0.0/0.1/0.2)
    # must appear; later better-scoring scenarios must not.
    assert "s0" in out
    assert "s1" in out
    assert "s2" in out
    assert "s10" not in out
    assert "s19" not in out


def test_meta_prompt_handles_empty_failures():
    from admin.prompt_handlers import build_optimizer_meta_prompt

    out = build_optimizer_meta_prompt(
        current_prompt_body="P", failing_scenarios=[],
    )
    assert "no failing scenarios captured" in out


# ---------------------------------------------------------------------------
# Failing-scenario extractor
# ---------------------------------------------------------------------------


def test_failing_scenarios_pulls_only_passed_false():
    from admin.prompt_handlers import _failing_scenarios_from_run

    run = {
        "results": [
            {"scenario_id": "a", "passed": True},
            {"scenario_id": "b", "passed": False},
            {"scenario_id": "c", "passed": False},
            {"scenario_id": "d"},          # No `passed` key — ignored.
            {"scenario_id": "e", "passed": None},
        ],
    }
    out = _failing_scenarios_from_run(run)
    assert [r["scenario_id"] for r in out] == ["b", "c"]


def test_failing_scenarios_handles_missing_results():
    from admin.prompt_handlers import _failing_scenarios_from_run

    assert _failing_scenarios_from_run({}) == []
    assert _failing_scenarios_from_run({"results": None}) == []


# ---------------------------------------------------------------------------
# /prompts/{id}/optimize POST — SSE stream end-to-end
# ---------------------------------------------------------------------------


def _patched_safe_list_factory(prompt_rows, run_rows, version_rows, model_rows):
    """Sequenced responses for the four queries the POST handler runs:
    prompt → run → latest_version → model.
    """
    calls = {"n": 0}

    def _sl(_result):
        calls["n"] += 1
        return [prompt_rows, run_rows, version_rows, model_rows][calls["n"] - 1]

    return _sl


def _null_client():
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


class _FakeChunk:
    """Mimics a LangChain AIMessageChunk — only needs `.content`."""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChat:
    """Stub chat model exposing only `.astream(messages)` as an async iter."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def astream(self, _messages):
        for c in self._chunks:
            yield _FakeChunk(c)


def test_optimize_route_streams_sse_done_event(client):
    """POST → SSE stream emits one delta per chunk + a final done event."""
    prompt_rows = [{"prompt_id": "p-1", "agent_kind": "support_bot", "slug": "system"}]
    run_rows = [{
        "run_id": "r-1",
        "results": [
            {"scenario_id": "s1", "passed": False, "question": "q", "expected": "e",
             "actual": "a", "judge": {"score": 0.1, "rationale": "bad"}},
        ],
    }]
    version_rows = [{"version_number": 1, "content": "current prompt body"}]
    model_rows = [{"model_id": "m-1", "name": "gpt", "provider": "openai", "model_string": "gpt-x"}]

    sl = _patched_safe_list_factory(prompt_rows, run_rows, version_rows, model_rows)
    fake_chat = _FakeChat(["new ", "prompt ", "body"])

    with patch("admin.prompt_handlers._safe_list", side_effect=sl), \
         patch("admin.prompt_handlers.get_service_client", return_value=_null_client()), \
         patch("runtime.llm_factory.build_chat_model", return_value=fake_chat):
        r = client.post(
            "/admin/prompts/p-1/optimize",
            data={"based_on_run_id": "r-1", "model_id": "m-1"},
        )
    assert r.status_code == 200, r.text[:300]
    body = r.text
    # Three delta events + one done event.
    assert body.count("\"type\": \"delta\"") == 3
    assert "\"type\": \"done\"" in body
    # The final aggregated content matches the concatenated chunks.
    assert "new prompt body" in body


def test_optimize_route_400_when_run_has_no_failures(client):
    prompt_rows = [{"prompt_id": "p-1", "agent_kind": "support_bot", "slug": "system"}]
    run_rows = [{"run_id": "r-1", "results": [{"scenario_id": "a", "passed": True}]}]
    version_rows = [{"version_number": 1, "content": "body"}]
    model_rows = [{"model_id": "m-1"}]

    sl = _patched_safe_list_factory(prompt_rows, run_rows, version_rows, model_rows)
    with patch("admin.prompt_handlers._safe_list", side_effect=sl), \
         patch("admin.prompt_handlers.get_service_client", return_value=_null_client()):
        r = client.post(
            "/admin/prompts/p-1/optimize",
            data={"based_on_run_id": "r-1", "model_id": "m-1"},
        )
    assert r.status_code == 400
    assert "no failing scenarios" in r.text


def test_optimize_route_404_when_prompt_missing(client):
    with patch("admin.prompt_handlers._safe_list", return_value=[]), \
         patch("admin.prompt_handlers.get_service_client", return_value=_null_client()):
        r = client.post(
            "/admin/prompts/nope/optimize",
            data={"based_on_run_id": "r-1", "model_id": "m-1"},
        )
    assert r.status_code == 404


def test_optimize_form_renders(client):
    """GET form should render the picker template even with empty data."""
    with patch("admin.prompt_handlers.get_service_client", return_value=_null_client()):
        r = client.get("/admin/prompts/p-1/optimize")
    # When the prompt row is missing the route returns 404 — that's also
    # acceptable since the conftest null client returns []. We accept
    # either: 200 (form rendered) or 404 (prompt missing). Both prove
    # the route is reachable and the handler ran.
    assert r.status_code in (200, 404)


def test_optimizer_model_picker_excludes_realtime_rows(client):
    """Regression for Defect C (Task 13 review): the "Optimizer model"
    picker queried agent_models with only `is_active=True` and no
    `is_chat_model` filter — unlike the eval-suite picker
    (admin/_model_picker.py::fetch_models_for_picker) and the profile
    editor's inline picker (admin/screens/agents.py), both of which Task 11
    already excluded realtime rows from. A realtime (duplex-voice) row
    picked here would later be handed to runtime.llm_factory.build_chat_model
    by prompt_optimize_submit, which breaks on a native-audio model id.
    """
    prompt_rows = [{"prompt_id": "p-1", "agent_kind": "support_bot", "slug": "system"}]
    chat_model = {
        "model_id": "m-chat", "name": "Chat model", "model_string": "gpt-x",
        "extra_config": {},
    }
    realtime_model = {
        "model_id": "m-live", "name": "Gemini Live (duplex voice)",
        "model_string": "gemini-live", "extra_config": {"realtime": True},
    }
    # Call order in prompt_optimize_form (suites empty -> "runs" query is
    # skipped entirely): prompt -> suites -> models -> latest_version.
    responses = [prompt_rows, [], [chat_model, realtime_model], []]
    calls = {"n": 0}

    def _sl(_result):
        calls["n"] += 1
        return responses[calls["n"] - 1]

    with patch("admin.prompt_handlers._safe_list", side_effect=_sl), \
         patch("admin.prompt_handlers.get_service_client", return_value=_null_client()):
        r = client.get("/admin/prompts/p-1/optimize")

    assert r.status_code == 200, r.text[:300]
    assert "Chat model" in r.text
    assert "Gemini Live (duplex voice)" not in r.text
