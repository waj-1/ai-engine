"""Unit tests for the E2E runner's judge-mode scoring.

These focus on the deterministic helpers (`_parse_judge_json`,
`_invoke_judge` with a stubbed LLM, aggregate scoring). They do not
talk to a real Google/Anthropic API or to Supabase — those code paths
are integration-tested via the smoke harness.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def test_parse_judge_json_clean_object():
    from admin.e2e_runner import _parse_judge_json

    result = _parse_judge_json('{"score": 0.92, "pass": true, "rationale": "ok"}')
    assert result["score"] == 0.92
    assert result["pass"] is True
    assert result["rationale"] == "ok"


def test_parse_judge_json_extracts_from_markdown_wrapper():
    """Models often wrap JSON in code fences; the parser must dig it out."""
    from admin.e2e_runner import _parse_judge_json

    text = "Here is my verdict:\n```json\n{\"score\": 0.7, \"rationale\": \"close\"}\n```"
    result = _parse_judge_json(text)
    assert result["score"] == 0.7
    assert result["rationale"] == "close"


def test_parse_judge_json_falls_back_to_leading_decimal():
    """If the model only returned a number (legacy judge prompt),
    treat it as the score."""
    from admin.e2e_runner import _parse_judge_json

    result = _parse_judge_json("0.45")
    assert result["score"] == 0.45


def test_parse_judge_json_unparseable_returns_uncertain():
    from admin.e2e_runner import _parse_judge_json

    result = _parse_judge_json("nothing useful here")
    assert result["score"] == 0.5
    assert "unparseable" in result["rationale"]


def test_invoke_judge_clamps_score_and_derives_pass_from_threshold():
    """Pass/fail is the runner's call, not the judge's — even if the model
    returns score=1 and pass=false, threshold logic wins."""
    from admin.e2e_runner import _invoke_judge

    judge = MagicMock()
    msg = MagicMock()
    msg.content = '{"score": 1.5, "pass": false, "rationale": "out of range"}'
    judge.invoke.return_value = msg

    out = _invoke_judge(judge, "q", "expected", "actual", pass_threshold=0.7)
    assert out["score"] == 1.0  # clamped
    assert out["pass"] is True  # 1.0 >= 0.7 despite judge saying False
    assert out["rationale"] == "out of range"


def test_invoke_judge_below_threshold_marks_fail():
    from admin.e2e_runner import _invoke_judge

    judge = MagicMock()
    msg = MagicMock()
    msg.content = '{"score": 0.6, "rationale": "missed key point"}'
    judge.invoke.return_value = msg

    out = _invoke_judge(judge, "q", "expected", "actual", pass_threshold=0.7)
    assert out["score"] == 0.6
    assert out["pass"] is False


def test_run_one_scenario_smoke_only_when_no_judge():
    """Without a judge configured the runner falls back to HTTP 200 +
    non-empty body. The judge field stays None."""
    from admin.e2e_runner import _run_one_scenario

    with patch("admin.e2e_runner.httpx.Client") as MockClient:
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "hello"
        MockClient.return_value.__enter__.return_value.post.return_value = resp

        outcome = _run_one_scenario(
            base_url="http://x",
            endpoint="/chat",
            scenario={"id": "s1", "question": "q"},
            judge=None,
            pass_threshold=0.7,
        )

    assert outcome["passed"] is True
    assert outcome["judge"] is None
    assert outcome["http_status"] == 200


def test_run_one_scenario_uses_judge_when_expected_present():
    """When the scenario has an expected_answer AND a judge is wired,
    the pass/fail comes from the judge verdict, not from smoke."""
    from admin.e2e_runner import _run_one_scenario

    judge = MagicMock()
    judge_msg = MagicMock()
    judge_msg.content = '{"score": 0.4, "rationale": "off-topic"}'
    judge.invoke.return_value = judge_msg

    with patch("admin.e2e_runner.httpx.Client") as MockClient:
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "drink water"
        MockClient.return_value.__enter__.return_value.post.return_value = resp

        outcome = _run_one_scenario(
            base_url="http://x",
            endpoint="/chat",
            scenario={
                "id": "s1",
                "question": "drug interaction warfarin amiodarone?",
                "expected_answer": "INR may rise; reduce warfarin dose",
            },
            judge=judge,
            pass_threshold=0.7,
        )

    assert outcome["judge"] is not None
    assert outcome["judge"]["score"] == 0.4
    assert outcome["passed"] is False  # judge < threshold even though HTTP=200


def test_run_one_scenario_judge_error_falls_back_to_smoke_pass():
    """A judge crash mid-run should not lose the smoke signal.

    The runner records the judge_error in the verdict but still uses
    the judge's `pass=False` (since we couldn't grade it)."""
    from admin.e2e_runner import _run_one_scenario

    judge = MagicMock()
    judge.invoke.side_effect = RuntimeError("rate limited")

    with patch("admin.e2e_runner.httpx.Client") as MockClient:
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        MockClient.return_value.__enter__.return_value.post.return_value = resp

        outcome = _run_one_scenario(
            base_url="http://x",
            endpoint="/chat",
            scenario={"id": "s1", "question": "q", "expected_answer": "a"},
            judge=judge,
            pass_threshold=0.7,
        )

    assert outcome["judge"]["pass"] is False
    assert "judge_error" in outcome["judge"]["rationale"]
