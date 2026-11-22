"""Tests for the run-vs-run compare data shaping in `admin.eval_handlers`.

We pulled the join + classification logic out of the route handler into
`_compare_runs(run_a, run_b)` so it can be tested without booting FastAPI.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(sid, passed, score=None, elapsed_ms=None):
    out = {"scenario_id": sid, "passed": passed}
    if score is not None:
        out["score"] = score
    if elapsed_ms is not None:
        out["elapsed_ms"] = elapsed_ms
    return out


def _run(run_id, results, total_score=None):
    return {"run_id": run_id, "results": results, "score": total_score}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_compare_runs_all_held_pass():
    from admin.eval_handlers import _compare_runs

    run_a = _run("a", [_result("s1", True, 0.9, 100), _result("s2", True, 0.85, 200)])
    run_b = _run("b", [_result("s1", True, 0.9, 100), _result("s2", True, 0.85, 200)])
    out = _compare_runs(run_a, run_b)
    rows = out["joined"]
    assert len(rows) == 2
    for row in rows:
        assert row["result"] == "held_pass"
        assert row["delta_score"] == 0
    assert out["summary"]["delta_passes"] == 0


def test_compare_runs_regression():
    from admin.eval_handlers import _compare_runs

    run_a = _run("a", [_result("s1", True, 0.9, 100)])
    run_b = _run("b", [_result("s1", False, 0.3, 100)])
    out = _compare_runs(run_a, run_b)
    row = out["joined"][0]
    assert row["result"] == "regressed"
    assert row["delta_score"] is not None
    assert row["delta_score"] < 0
    assert out["summary"]["delta_passes"] == -1


def test_compare_runs_gain():
    from admin.eval_handlers import _compare_runs

    run_a = _run("a", [_result("s1", False, 0.2, 100)])
    run_b = _run("b", [_result("s1", True, 0.95, 100)])
    out = _compare_runs(run_a, run_b)
    row = out["joined"][0]
    assert row["result"] == "gained"
    assert row["delta_score"] > 0
    assert out["summary"]["delta_passes"] == 1


def test_compare_runs_only_a_when_scenario_dropped():
    from admin.eval_handlers import _compare_runs

    run_a = _run("a", [_result("s1", True, 0.9), _result("s2", True, 0.7)])
    run_b = _run("b", [_result("s1", True, 0.9)])
    out = _compare_runs(run_a, run_b)
    by_sid = {r["scenario_id"]: r for r in out["joined"]}
    assert by_sid["s2"]["result"] == "only_a"
    assert by_sid["s2"]["b"] is None
    assert by_sid["s2"]["delta_score"] is None


def test_compare_runs_only_b_when_scenario_added():
    from admin.eval_handlers import _compare_runs

    run_a = _run("a", [_result("s1", True, 0.9)])
    run_b = _run("b", [_result("s1", True, 0.9), _result("s_new", True, 0.8)])
    out = _compare_runs(run_a, run_b)
    by_sid = {r["scenario_id"]: r for r in out["joined"]}
    assert by_sid["s_new"]["result"] == "only_b"
    assert by_sid["s_new"]["a"] is None


def test_compare_runs_sort_largest_delta_first():
    from admin.eval_handlers import _compare_runs

    run_a = _run(
        "a",
        [
            _result("small", True, 0.80),
            _result("big", True, 0.90),
            _result("zero", True, 0.50),
        ],
    )
    run_b = _run(
        "b",
        [
            _result("small", True, 0.78),  # delta = -0.02
            _result("big", False, 0.10),   # delta = -0.80
            _result("zero", True, 0.50),   # delta =  0
        ],
    )
    out = _compare_runs(run_a, run_b)
    ordered_ids = [r["scenario_id"] for r in out["joined"]]
    # "big" has the largest |delta| so it should sort first.
    assert ordered_ids[0] == "big"
    # "zero" delta=0 sorts after non-zero deltas.
    assert ordered_ids[-1] == "zero"


def test_compare_runs_summary_mean_latency_and_passes():
    from admin.eval_handlers import _compare_runs

    run_a = _run(
        "a",
        [
            _result("s1", True, 0.9, 100),
            _result("s2", True, 0.8, 200),
            _result("s3", False, 0.2, 300),
        ],
        total_score=0.6,
    )
    run_b = _run(
        "b",
        [
            _result("s1", True, 0.9, 50),
            _result("s2", True, 0.85, 100),
            _result("s3", True, 0.8, 150),
        ],
        total_score=0.85,
    )
    out = _compare_runs(run_a, run_b)
    summary = out["summary"]
    assert summary["passes_a"] == 2
    assert summary["passes_b"] == 3
    assert summary["delta_passes"] == 1
    # Mean latencies should be (100+200+300)/3 = 200 and (50+100+150)/3 = 100
    assert summary["mean_latency_a"] == pytest.approx(200.0)
    assert summary["mean_latency_b"] == pytest.approx(100.0)
    assert summary["delta_mean_latency_ms"] == pytest.approx(-100.0)
    assert summary["delta_score"] == pytest.approx(0.25)


def test_compare_runs_held_fail():
    from admin.eval_handlers import _compare_runs

    run_a = _run("a", [_result("s1", False, 0.1)])
    run_b = _run("b", [_result("s1", False, 0.2)])
    out = _compare_runs(run_a, run_b)
    assert out["joined"][0]["result"] == "held_fail"
