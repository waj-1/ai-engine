"""Tests for rubric-based partial-credit judge (Task 5).

The judge LLM is mocked — no network calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


class _FakeJudge:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, msgs):
        return type("R", (), {"content": json.dumps(self.payload)})()


def test_rubric_judge_partial_credit():
    """Weighted mean excludes 'unknown' criteria; score=(1.0+0.5)/2=0.75."""
    from admin import e2e_runner as er

    payload = {
        "criteria": [
            {"id": "correct", "verdict": "pass", "score": 1.0, "rationale": "ok"},
            {"id": "cites_tool", "verdict": "partial", "score": 0.5, "rationale": "partial"},
            {"id": "safety", "verdict": "unknown", "score": 0.0, "rationale": "n/a"},
        ],
        "overall_score": 0.75,
        "overall": "pass",
        "rationale": "good",
    }

    out = er._invoke_judge(
        _FakeJudge(payload),
        "q",
        "ref",
        "ans",
        0.7,
        rubric=[
            {"id": "correct", "criterion": "correct"},
            {"id": "cites_tool", "criterion": "cites the tool"},
            {"id": "safety", "criterion": "safe"},
        ],
    )

    # unknown is excluded from the weighted mean: (1.0 + 0.5) / 2 = 0.75
    assert round(out["score"], 3) == 0.75
    assert out["pass"] is True
    assert len(out["criteria"]) == 3


def test_rubric_judge_no_rubric_back_compat():
    """Without a rubric the existing {score, pass, rationale} path is unchanged."""
    from admin import e2e_runner as er

    payload = {"score": 0.9, "pass": True, "rationale": "x"}
    out = er._invoke_judge(_FakeJudge(payload), "q", "ref", "ans", 0.7)
    assert out["score"] == 0.9 and out["pass"] is True


def test_rubric_judge_all_unknown_returns_zero():
    """All unknown criteria → score=0, pass=False (no evidence to grade)."""
    from admin import e2e_runner as er

    payload = {
        "criteria": [
            {"id": "c1", "verdict": "unknown", "score": 0.0, "rationale": "n/a"},
        ],
        "overall_score": 0.0,
        "overall": "fail",
        "rationale": "no evidence",
    }

    out = er._invoke_judge(
        _FakeJudge(payload),
        "q",
        "ref",
        "ans",
        0.7,
        rubric=[{"id": "c1", "criterion": "some criterion"}],
    )
    assert out["score"] == 0.0
    assert out["pass"] is False
    assert len(out["criteria"]) == 1


def test_rubric_judge_weighted_mean_respects_weights():
    """Weighted mean uses rubric[i].weight when present."""
    from admin import e2e_runner as er

    # criterion A: score=1.0, weight=3; criterion B: score=0.0, weight=1
    # weighted mean = (1.0*3 + 0.0*1) / (3+1) = 0.75
    payload = {
        "criteria": [
            {"id": "A", "verdict": "pass", "score": 1.0, "rationale": "great"},
            {"id": "B", "verdict": "fail", "score": 0.0, "rationale": "bad"},
        ],
        "overall_score": 0.5,
        "overall": "partial",
        "rationale": "mixed",
    }

    out = er._invoke_judge(
        _FakeJudge(payload),
        "q",
        "ref",
        "ans",
        0.7,
        rubric=[
            {"id": "A", "criterion": "criterion A", "weight": 3},
            {"id": "B", "criterion": "criterion B", "weight": 1},
        ],
    )
    assert round(out["score"], 3) == 0.75
    assert out["pass"] is True
