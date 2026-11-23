"""Unit tests for the rebuilt eval runner.

Focus on the deterministic helpers — `_extract_answer_chunks` (SSE
parsing), `_payload_for` (scenario → request body), and the
substring/judge composition in `_run_scenario`. Real HTTP and Laminar
clients are stubbed."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def test_extract_answer_chunks_concatenates_sse_answer_events():
    from admin.evals import _extract_answer_chunks

    body = (
        'data: {"type": "answer", "content": "Hello "}\n\n'
        'data: {"type": "thought", "content": "internal"}\n\n'
        'data: {"type": "answer", "content": "world"}\n\n'
        'data: [DONE]\n\n'
    )
    assert _extract_answer_chunks(body) == "Hello world"


def test_extract_answer_chunks_ignores_non_json_lines():
    from admin.evals import _extract_answer_chunks

    body = (
        'event: ping\n'
        'data: not-json\n\n'
        'data: {"type": "answer", "content": "only this"}\n\n'
    )
    assert _extract_answer_chunks(body) == "only this"


def test_extract_answer_chunks_returns_empty_for_pure_json_response():
    """A non-SSE response (e.g. a plain JSON endpoint) has no `data:`
    prefix so the extractor returns "" — the caller then falls back to
    using the raw body for substring matching."""
    from admin.evals import _extract_answer_chunks

    assert _extract_answer_chunks('{"plan": {"days": []}}') == ""


def test_payload_for_explicit_payload_wins():
    from admin.evals import _payload_for

    scenario = {"payload": {"foo": "bar"}, "question": "ignored"}
    assert _payload_for(scenario) == {"foo": "bar"}


def test_payload_for_synthesizes_chat_body_with_optional_context():
    from admin.evals import _payload_for

    body = _payload_for({
        "question": "Show me services",
        "locale": "ar",
        "business_id": "biz-1",
        "screen_context": {"flow": "home"},
    })
    assert body["message"] == "Show me services"
    assert body["locale"] == "ar"
    assert body["business_id"] == "biz-1"
    assert body["screen_context"] == {"flow": "home"}
    assert "session_id" in body  # synthesized uuid


def test_run_scenario_substring_pass(monkeypatch):
    """Endpoint returns content containing both expected fragments → pass."""
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ("Refunds take 5 business days to reach your card.", "", 200, 42),
    )
    result = evals._run_scenario(
        {
            "id": "refund1",
            "question": "how long does a refund take?",
            "expected_substrings": ["refunds", "card"],
        },
        profile_id="p1", base_url="http://x", endpoint="/agent/chat/stream",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is True
    assert result["missing_substrings"] == []
    assert result["actual"].startswith("Refunds take")


def test_run_scenario_substring_fail_with_missing_list(monkeypatch):
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ("Generic answer.", "", 200, 30),
    )
    result = evals._run_scenario(
        {
            "id": "refund1",
            "question": "how long does a refund take?",
            "expected_substrings": ["refunds", "card"],
        },
        profile_id="p1", base_url="http://x", endpoint="/x",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is False
    assert set(result["missing_substrings"]) == {"refunds", "card"}


def test_run_scenario_empty_actual_is_not_silent_pass(monkeypatch):
    """The 100%-score bug: an empty `actual_output` and no
    expected_substrings used to pass silently. The new runner counts
    empty actual as fail."""
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ("", "", 200, 10),
    )
    result = evals._run_scenario(
        {"id": "s", "question": "hi"},
        profile_id="p", base_url="http://x", endpoint="/x",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is False
    assert result["actual"] == ""


def test_run_scenario_expected_answer_without_judge_does_not_pass(monkeypatch):
    """The false-positive bug: a scenario that declares an `expected_answer`
    but has no judge available (the suite has no evaluator_model configured)
    used to fall through to "any non-empty 2xx passes". It must now FAIL with
    a grading_note that tells the operator to configure an evaluator."""
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ("A plausible but never-graded answer.", "", 200, 25),
    )
    result = evals._run_scenario(
        {
            "id": "s1",
            "question": "what is the refund window?",
            "expected_answer": "Refunds within 30 days; keep the receipt",
        },
        profile_id="p", base_url="http://x", endpoint="/x",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is False
    # Not judged (2026-11-23): no score, so it can't read as "Scored 0%".
    assert result["score"] is None and result["ungraded"] is True
    assert result["grading_note"] and "judge model" in result["grading_note"]


def test_run_scenario_pure_smoke_still_passes(monkeypatch):
    """No expected_answer and no substrings = a smoke check: any non-empty
    2xx body is a legitimate pass (the guard must not break this)."""
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ("hello there", "", 200, 12),
    )
    result = evals._run_scenario(
        {"id": "s", "question": "hi"},
        profile_id="p", base_url="http://x", endpoint="/x",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is True
    assert result["grading_note"] is None


def test_resolve_evaluator_model_reads_evaluator_model_id():
    """The judge model is resolved from agent_eval_suites.evaluator_model_id
    (FK into agent_models), NOT the legacy `evaluator_model` key — reading the
    wrong key is why the judge never ran and everything passed."""
    import types

    from admin import evals

    class _Q:
        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            # Resolver must use model_string (real model id), not the friendly
            # `name` ("…-default"), which the SDK rejects.
            return types.SimpleNamespace(data=[{
                "model_string": "gemini-3.5-flash",
            }])

    class _Client:
        def table(self, _name):
            return _Q()

    assert evals._resolve_evaluator_model(
        _Client(), {"evaluator_model_id": "m-1"}
    ) == "gemini-3.5-flash"
    # No evaluator configured (suite editor's "substring only" option) → None.
    assert evals._resolve_evaluator_model(_Client(), {}) is None
    # The legacy key is ignored — only evaluator_model_id is honoured.
    assert evals._resolve_evaluator_model(_Client(), {"evaluator_model": "x"}) is None


def test_run_scenario_judge_overrides_when_present(monkeypatch):
    """Judge says score=0.4 (fail), substring is None → judge decides."""
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ("vague response", "", 200, 50),
    )
    judge = MagicMock()
    with patch(
        "admin.e2e_runner._invoke_judge",
        return_value={"score": 0.4, "pass": False, "rationale": "off-topic"},
    ):
        result = evals._run_scenario(
            {
                "id": "j1",
                "question": "refund on a sale item?",
                "expected_answer": "card refund; sale items included",
            },
            profile_id="p", base_url="http://x", endpoint="/x",
            judge=judge, pass_threshold=0.7,
        )
    assert result["judge"]["score"] == 0.4
    assert result["passed"] is False
    assert result["score"] == 0.4


def test_run_scenario_judge_and_substring_must_both_pass(monkeypatch):
    """When both signals are configured, both must agree → pass."""
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ("card refund in 5 days", "", 200, 50),
    )
    with patch(
        "admin.e2e_runner._invoke_judge",
        return_value={"score": 0.9, "pass": True, "rationale": "good"},
    ):
        result = evals._run_scenario(
            {
                "id": "both",
                "question": "refund on a sale item?",
                "expected_answer": "refunds go back to the card",
                "expected_substrings": ["card"],
            },
            profile_id="p", base_url="http://x", endpoint="/x",
            judge=MagicMock(), pass_threshold=0.7,
        )
    assert result["passed"] is True
    assert result["judge"]["score"] == 0.9


def test_run_scenario_http_422_with_non_empty_body_still_fails(monkeypatch):
    """The bug the user saw: when neither expected_substrings nor a judge
    are configured, the fallback `actual non-empty → pass` rule used to
    let a 422 with `{"detail": "Field required"}` slip through. The 2xx
    precondition must override the fallback."""
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ('{"detail":[{"type":"missing","loc":["body","business_id"]}]}', "", 422, 40),
    )
    result = evals._run_scenario(
        {"id": "x", "question": "case study"},
        profile_id="p", base_url="http://x", endpoint="/agent/case/stream",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is False
    assert result["http_status"] == 422


def test_run_scenario_http_500_overrides_substring_pass(monkeypatch):
    """Even when the response body happens to contain every expected
    substring, a 5xx still fails — the endpoint didn't actually work."""
    from admin import evals

    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        lambda **k: ("refunds card", "", 500, 40),
    )
    result = evals._run_scenario(
        {
            "id": "x",
            "question": "refund?",
            "expected_substrings": ["refunds", "card"],
        },
        profile_id="p", base_url="http://x", endpoint="/x",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is False
    assert result["missing_substrings"] == []  # they're in the body
    assert result["http_status"] == 500


def test_run_context_is_merged_into_payload():
    """`run_context` from the trigger form should appear in the
    synthesized request body so the endpoint validator stops rejecting
    missing fields."""
    from admin.evals import _payload_for

    body = _payload_for(
        {"id": "s", "question": "What services?"},
        run_context={"business_id": "biz-1", "branch_id": "br-2"},
    )
    assert body["business_id"] == "biz-1"
    assert body["branch_id"] == "br-2"
    assert body["message"] == "What services?"


def test_scenario_context_wins_over_run_context():
    """Per-scenario context fields override the run-level defaults."""
    from admin.evals import _payload_for

    body = _payload_for(
        {"id": "s", "question": "hi", "business_id": "scen-biz"},
        run_context={"business_id": "run-biz", "customer_id": "p1"},
    )
    assert body["business_id"] == "scen-biz"
    assert body["customer_id"] == "p1"  # only on run_context, kept


def test_run_scenario_invoke_error_records_error_and_fails(monkeypatch):
    """Network exception → scenario marked failed with error captured."""
    from admin import evals

    def _boom(**k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(evals, "_invoke_endpoint", _boom)
    result = evals._run_scenario(
        {"id": "x", "question": "q"},
        profile_id="p", base_url="http://x", endpoint="/x",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is False
    assert "connection refused" in result["error"]
    assert result["actual"] == ""


# --- repeats (pass^k attempt count) resolution from the trigger context ---
# The queued run path (/admin/evals/run -> start_tracked_job) never passes
# `repeats` as a kwarg; the form packs it into run.context. The runner must
# read it from there, else every scenario runs exactly once and pass^k is
# meaningless. These pin that contract.

def test_resolve_repeats_reads_context_value():
    from admin import evals
    assert evals._resolve_repeats({"repeats": 5}, fallback=1) == 5
    assert evals._resolve_repeats({"repeats": 3}, fallback=1) == 3


def test_resolve_repeats_coerces_strings_and_clamps():
    from admin import evals
    assert evals._resolve_repeats({"repeats": "5"}, fallback=1) == 5   # form sends strings
    assert evals._resolve_repeats({"repeats": 999}, fallback=1) == evals._MAX_REPEATS  # ceiling
    assert evals._resolve_repeats({"repeats": 0}, fallback=2) == 2     # 0/invalid -> fallback


def test_resolve_repeats_falls_back_when_absent_or_bad():
    from admin import evals
    assert evals._resolve_repeats({}, fallback=1) == 1
    assert evals._resolve_repeats(None, fallback=1) == 1
    assert evals._resolve_repeats({"repeats": "abc"}, fallback=1) == 1
