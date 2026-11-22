"""Tests for cooperative-cancel in the E2E runner.

Mirrors `test_eval_cancellation.py` but for `admin.e2e_runner.run_e2e`.
The E2E runner has its own scenario loop and its own results table
(`agent_e2e_runs`), so the cancel plumbing is duplicated rather than
shared — these tests pin the E2E side specifically.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Reuse the fake-supabase from the eval cancellation test by importing it.
# Avoids two copies of the same fluent-chain mock.
# ---------------------------------------------------------------------------

from tests.admin.test_eval_cancellation import _FakeClient  # noqa: E402


def _seed_e2e_run(
    client: _FakeClient,
    *,
    profile_id: str = "prof-1",
    scenarios: Optional[List[Dict[str, Any]]] = None,
) -> None:
    scenarios = scenarios or [
        {"id": "s1", "question": "q1"},
        {"id": "s2", "question": "q2"},
        {"id": "s3", "question": "q3"},
    ]
    client.tables["agent_profiles"].rows.append({
        "profile_id": profile_id,
        "agent_kind": "support_bot",
        "endpoint_path": "/agent/test",
        "environment": "dev",
    })
    client.tables["agent_eval_suites"].rows.append({
        "suite_id": "suite-1",
        "name": "e2e",
        "agent_kind": "support_bot",
        "scenarios": scenarios,
        "pass_threshold": 0.7,
        "evaluator_model": None,
        "is_active": True,
    })
    # Pre-create the agent_e2e_runs table so the runner's insert/update
    # roundtrips land somewhere.
    client.tables.setdefault("agent_e2e_runs", client.table("agent_e2e_runs"))


@pytest.fixture
def fake_client():
    return _FakeClient()


def test_e2e_runner_stops_at_cancel_flag(fake_client, monkeypatch):
    """Cancel flag flips between scenario 1 and 2 → only scenario 1
    runs, the row finalizes with status='cancelled' and an error message
    naming the scenario boundary."""
    from admin import e2e_runner

    _seed_e2e_run(fake_client)
    monkeypatch.setattr(e2e_runner, "get_service_client",
                        lambda: fake_client)
    monkeypatch.setattr(e2e_runner, "_base_url_for",
                        lambda env: "http://stub")
    # Cheap, deterministic single-scenario stub — we only care about
    # the loop / cancel semantics, not the judge logic.
    monkeypatch.setattr(
        e2e_runner, "_run_one_scenario",
        lambda *a, **kw: {
            "id": "s1", "question": "q1",
            "http_status": 200, "latency_ms": 5,
            "body_preview": "ok", "passed": True, "judge": None,
        },
    )

    poll_state = {"calls": 0}

    def fake_cancel(job_id: str) -> bool:
        poll_state["calls"] += 1
        return poll_state["calls"] >= 2  # cancel before scenario 2

    monkeypatch.setattr(e2e_runner, "_is_job_cancelled", fake_cancel)

    result = e2e_runner.run_e2e(
        profile_id="prof-1",
        environment="dev",
        triggered_by="u-1",
        _job_id="job-xyz",
    )

    assert result["status"] == "cancelled"
    assert result["cancelled_at"] == 2
    assert result["total_scenarios"] == 3

    # Row in agent_e2e_runs reflects the same.
    rows = fake_client.tables["agent_e2e_runs"].rows
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"
    assert "cancelled by operator" in (rows[0].get("error") or "")
    assert "2/3" in rows[0]["error"]
    # Only scenario 1's outcome made it into results.
    assert len(rows[0].get("results") or []) == 1


def test_e2e_runner_no_job_id_runs_to_completion(fake_client, monkeypatch):
    """No `_job_id` → cancel poll never fires; all 3 scenarios run."""
    from admin import e2e_runner

    _seed_e2e_run(fake_client)
    monkeypatch.setattr(e2e_runner, "get_service_client",
                        lambda: fake_client)
    monkeypatch.setattr(e2e_runner, "_base_url_for",
                        lambda env: "http://stub")
    monkeypatch.setattr(
        e2e_runner, "_run_one_scenario",
        lambda *a, **kw: {
            "id": "s", "question": "q",
            "http_status": 200, "latency_ms": 5,
            "body_preview": "ok", "passed": True, "judge": None,
        },
    )

    def explode(_j: str) -> bool:
        raise AssertionError("is_cancelled polled without _job_id")

    monkeypatch.setattr(e2e_runner, "_is_job_cancelled", explode)

    result = e2e_runner.run_e2e(
        profile_id="prof-1", environment="dev", triggered_by="u-1",
    )
    assert result["status"] == "passed"
    assert result["pass_count"] == 3


def test_e2e_runner_accepts_job_id_via_start_tracked_job(fake_client):
    """End-to-end check: `start_tracked_job` should detect `_job_id` on
    `run_e2e`'s signature and thread it through. We don't run the
    actual worker here — just confirm the kwarg is accepted at the
    introspection level so future signature changes don't silently
    regress the cancel wiring."""
    import inspect

    from admin.e2e_runner import run_e2e

    sig = inspect.signature(run_e2e)
    assert "_job_id" in sig.parameters
    # And it's optional (defaults to None) so legacy callers stay green.
    assert sig.parameters["_job_id"].default is None


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out after {timeout}s")
