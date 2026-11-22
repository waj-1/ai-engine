"""Tests for cooperative-cancel in the eval runner + the `_job_id`
signature-sniffing convention in `admin._jobs.start_tracked_job`.

Background: Python threads can't be safely killed mid-stride, so admin
jobs implement cancel as a flag on `admin_jobs.status`. The eval
runner polls the flag between scenarios and bails out cleanly. The
plumbing (job_id → worker) is opt-in via an `_job_id` keyword argument
so legacy callers stay back-compat.

These tests pin both layers:
  * `start_tracked_job` threads `_job_id` only when `fn` declares it
    (or accepts `**kwargs`) — otherwise it falls through to the legacy
    call shape.
  * `run_eval_suite_sync` honors the cancel poll between scenarios,
    finalizes the run with status='cancelled' + an error message
    naming the scenario boundary, and skips work that would otherwise
    have run.
  * `_finalize` is race-safe — a worker that finishes naturally after
    the operator cancels mid-flight must NOT overwrite the cancelled
    row with 'passed'/'failed'/'errored'.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Stateful supabase fake — supports the chains start_tracked_job + the
# eval runner use against admin_jobs and agent_eval_runs.
# ---------------------------------------------------------------------------


class _FakeTable:
    def __init__(self, name: str, rows: List[Dict[str, Any]]) -> None:
        self.name = name
        self.rows = rows
        self._filters: Dict[str, Any] = {}
        self._limit: Optional[int] = None
        self._order_key: Optional[str] = None
        self._order_desc: Optional[bool] = None
        self._pending_insert: Optional[Dict[str, Any]] = None
        self._pending_update: Optional[Dict[str, Any]] = None
        self._select_cols: Optional[str] = None

    def _reset(self) -> None:
        self._filters = {}
        self._limit = None
        self._order_key = None
        self._order_desc = None
        self._pending_insert = None
        self._pending_update = None
        self._select_cols = None

    def select(self, cols: str = "*") -> "_FakeTable":
        self._select_cols = cols
        return self

    def eq(self, col: str, val: Any) -> "_FakeTable":
        self._filters[col] = val
        return self

    def neq(self, col: str, val: Any) -> "_FakeTable":
        # Stored separately just so multiple eq calls don't collide.
        self._filters[f"__neq__{col}"] = val
        return self

    def limit(self, n: int) -> "_FakeTable":
        self._limit = n
        return self

    def order(self, key: str, desc: bool = False) -> "_FakeTable":
        self._order_key = key
        self._order_desc = desc
        return self

    def insert(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._pending_insert = dict(payload)
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._pending_update = dict(payload)
        return self

    def _row_matches(self, row: Dict[str, Any]) -> bool:
        for k, v in self._filters.items():
            if k.startswith("__neq__"):
                if row.get(k[len("__neq__"):]) == v:
                    return False
            else:
                if row.get(k) != v:
                    return False
        return True

    def execute(self) -> Any:
        if self._pending_insert is not None:
            row = dict(self._pending_insert)
            row.setdefault("job_id", f"job-{len(self.rows) + 1}")
            row.setdefault("created_at",
                           time.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
            self.rows.append(row)
            self._reset()
            return _FakeResult([dict(row)])
        if self._pending_update is not None:
            updated: List[Dict[str, Any]] = []
            for r in self.rows:
                if self._row_matches(r):
                    r.update(self._pending_update)
                    updated.append(dict(r))
            self._reset()
            return _FakeResult(updated)
        matched = [dict(r) for r in self.rows if self._row_matches(r)]
        if self._order_key:
            matched.sort(
                key=lambda r: r.get(self._order_key) or "",
                reverse=bool(self._order_desc),
            )
        if self._limit is not None:
            matched = matched[: self._limit]
        self._reset()
        return _FakeResult(matched)


class _FakeResult:
    def __init__(self, data: List[Dict[str, Any]]) -> None:
        self.data = data


class _FakeClient:
    def __init__(self) -> None:
        self.tables: Dict[str, _FakeTable] = {
            "admin_jobs": _FakeTable("admin_jobs", []),
            "agent_eval_runs": _FakeTable("agent_eval_runs", []),
            "agent_eval_suites": _FakeTable("agent_eval_suites", []),
            "agent_profiles": _FakeTable("agent_profiles", []),
            "agent_deployment_targets": _FakeTable(
                "agent_deployment_targets", []),
            "agent_audit_log": _FakeTable("agent_audit_log", []),
        }

    def table(self, name: str) -> _FakeTable:
        if name not in self.tables:
            self.tables[name] = _FakeTable(name, [])
        return self.tables[name]


@pytest.fixture
def fake_client():
    return _FakeClient()


# ---------------------------------------------------------------------------
# Signature-detection tests for start_tracked_job
# ---------------------------------------------------------------------------


def test_start_tracked_job_threads_job_id_when_fn_accepts_it(fake_client):
    """A worker that declares `_job_id` gets the fresh admin_jobs.job_id
    passed in as a kwarg. Without this the cancel poll has nothing to
    poll on."""
    from admin import _jobs

    captured: Dict[str, Any] = {}

    def worker(*, run_id: str, _job_id: Optional[str] = None) -> None:
        captured["run_id"] = run_id
        captured["_job_id"] = _job_id

    with patch.object(_jobs, "admin_service_client", return_value=fake_client):
        job_id = _jobs.start_tracked_job(
            worker,
            kind="eval_run",
            label="t",
            actor_user_id="u-1",
            run_id="r-1",
        )
        # Wait for the daemon thread to run.
        _wait_until(lambda: captured.get("_job_id") is not None, 2.0)

    assert captured["run_id"] == "r-1"
    assert captured["_job_id"] == job_id


def test_start_tracked_job_back_compat_does_not_pass_job_id(fake_client):
    """A legacy worker that doesn't declare `_job_id` (and has no
    **kwargs) must NOT receive it — otherwise we'd break every existing
    background worker the day this lands."""
    from admin import _jobs

    captured: Dict[str, Any] = {}

    def legacy_worker(*, run_id: str) -> None:
        captured["run_id"] = run_id
        captured["called"] = True

    with patch.object(_jobs, "admin_service_client", return_value=fake_client):
        _jobs.start_tracked_job(
            legacy_worker,
            kind="eval_run",
            label="t",
            actor_user_id="u-1",
            run_id="r-legacy",
        )
        _wait_until(lambda: captured.get("called") is True, 2.0)

    assert captured["run_id"] == "r-legacy"
    assert "_job_id" not in captured


def test_start_tracked_job_passes_job_id_to_var_kwargs_fn(fake_client):
    """A worker with `**kwargs` accepts everything — the tracker should
    forward `_job_id` so opportunistic uses (logging, debugging) can
    pick it up without explicitly declaring it."""
    from admin import _jobs

    captured: Dict[str, Any] = {}

    def kwargs_worker(**kwargs) -> None:
        captured.update(kwargs)

    with patch.object(_jobs, "admin_service_client", return_value=fake_client):
        job_id = _jobs.start_tracked_job(
            kwargs_worker,
            kind="eval_run",
            label="t",
            actor_user_id="u-1",
            run_id="r-kw",
        )
        _wait_until(lambda: "run_id" in captured, 2.0)

    assert captured["run_id"] == "r-kw"
    assert captured["_job_id"] == job_id


# ---------------------------------------------------------------------------
# Eval-runner cancellation behavior
# ---------------------------------------------------------------------------


def _seed_eval_run(
    client: _FakeClient,
    *,
    run_id: str = "run-1",
    suite_id: str = "suite-1",
    profile_id: str = "prof-1",
    scenarios: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Set up enough rows for `run_eval_suite_sync` to walk through the
    happy path: suite with scenarios, profile with endpoint, base URL."""
    scenarios = scenarios or [
        {"id": "s1", "question": "q1", "expected_substrings": ["a"]},
        {"id": "s2", "question": "q2", "expected_substrings": ["b"]},
        {"id": "s3", "question": "q3", "expected_substrings": ["c"]},
    ]
    client.tables["agent_eval_suites"].rows.append({
        "suite_id": suite_id,
        "name": "test_suite",
        "agent_kind": "support_bot",
        "scenarios": scenarios,
        "pass_threshold": 0.7,
        "evaluator_model": None,
        "default_context": {},
    })
    client.tables["agent_profiles"].rows.append({
        "profile_id": profile_id,
        "agent_kind": "support_bot",
        "endpoint_path": "/agent/test",
        "environment": "dev",
    })
    # The eval runner joins eval_runs with eval_suites via the
    # `agent_eval_suites(*)` foreign-key shorthand. Our fake doesn't
    # implement joins, so we embed the suite directly on the run row —
    # the runner's `run.get("agent_eval_suites")` then picks it up.
    client.tables["agent_eval_runs"].rows.append({
        "run_id": run_id,
        "suite_id": suite_id,
        "profile_id": profile_id,
        "status": "queued",
        "context": {},
        "agent_eval_suites": {
            "suite_id": suite_id,
            "name": "test_suite",
            "agent_kind": "support_bot",
            "scenarios": scenarios,
            "pass_threshold": 0.7,
            "evaluator_model": None,
            "default_context": {},
        },
    })


def test_eval_runner_stops_at_cancel_flag(fake_client, monkeypatch):
    """When `is_cancelled(_job_id)` flips True before scenario #2, the
    runner finishes scenario #1, bails out before #2, and stamps
    status='cancelled' on the eval row."""
    from admin import evals

    _seed_eval_run(fake_client)
    monkeypatch.setattr(evals, "get_service_client", lambda: fake_client)
    monkeypatch.setattr(evals, "_invoke_endpoint",
                        lambda **kw: ("a", "", 200, 10))

    # Cancel poll: False once (allows scenario 1), then True forever.
    poll_state = {"calls": 0}

    def fake_cancel(job_id: str) -> bool:
        poll_state["calls"] += 1
        # First poll = pre-scenario #1 → not cancelled.
        # Second poll = pre-scenario #2 → cancelled.
        return poll_state["calls"] >= 2

    monkeypatch.setattr(evals, "_is_job_cancelled", fake_cancel)

    evals.run_eval_suite_sync(run_id="run-1", _job_id="job-xyz")

    final = fake_client.tables["agent_eval_runs"].rows[0]
    assert final["status"] == "cancelled"
    assert "cancelled by operator" in (final.get("error") or "")
    # Stopped before scenario 2 → only scenario 1 ran.
    results = final.get("results") or []
    assert len(results) == 1
    assert results[0]["scenario_id"] == "s1"
    # Error message names the boundary the operator stopped us at.
    assert "2/3" in final["error"]


def test_eval_runner_no_job_id_runs_to_completion(fake_client, monkeypatch):
    """Legacy callers (no `_job_id`) skip the cancel poll entirely —
    every scenario runs and the row finalizes with passed/failed as
    usual. This is the back-compat invariant."""
    from admin import evals

    _seed_eval_run(fake_client)
    monkeypatch.setattr(evals, "get_service_client", lambda: fake_client)
    monkeypatch.setattr(evals, "_invoke_endpoint",
                        lambda **kw: ("abc", "", 200, 10))

    # If the runner accidentally polls when _job_id is None this would
    # be called — assert it isn't.
    def explode(_job_id: str) -> bool:
        raise AssertionError(
            "is_cancelled was polled despite _job_id being None")

    monkeypatch.setattr(evals, "_is_job_cancelled", explode)

    evals.run_eval_suite_sync(run_id="run-1")  # no _job_id

    final = fake_client.tables["agent_eval_runs"].rows[0]
    # Every scenario contains its own expected substring in the actual
    # output 'abc' → all passed.
    assert final["status"] == "passed"
    assert len(final.get("results") or []) == 3


def test_eval_runner_cancel_before_first_scenario(fake_client, monkeypatch):
    """Cancel set before any scenario starts → status='cancelled' with
    zero scenario results and the error message naming scenario 1/3."""
    from admin import evals

    _seed_eval_run(fake_client)
    monkeypatch.setattr(evals, "get_service_client", lambda: fake_client)
    monkeypatch.setattr(evals, "_invoke_endpoint",
                        lambda **kw: ("a", "", 200, 10))
    monkeypatch.setattr(evals, "_is_job_cancelled", lambda _j: True)

    evals.run_eval_suite_sync(run_id="run-1", _job_id="job-xyz")

    final = fake_client.tables["agent_eval_runs"].rows[0]
    assert final["status"] == "cancelled"
    assert "1/3" in (final.get("error") or "")
    assert (final.get("results") or []) == []


def test_finalize_does_not_overwrite_cancelled_row(fake_client, monkeypatch):
    """Race scenario: the operator cancels the eval row AFTER the
    runner started but BEFORE `_finalize` writes its terminal status.
    _finalize must read-then-write and bail out when the row is already
    cancelled, preserving the operator's intent."""
    from admin import evals

    # Seed an eval run that's already been flipped to 'cancelled' by
    # the cancel route (i.e. the race happened — operator beat the
    # worker to the write).
    fake_client.tables["agent_eval_runs"].rows.append({
        "run_id": "race-1",
        "status": "cancelled",
        "error": "cancelled by operator at scenario 2/3",
    })

    # _finalize tries to write 'passed' — must be a no-op.
    evals._finalize(
        fake_client, "race-1",
        status="passed",
        results=[{"scenario_id": "s1", "passed": True}],
        score=1.0,
    )

    row = fake_client.tables["agent_eval_runs"].rows[0]
    assert row["status"] == "cancelled"
    # The cancel error message is intact.
    assert "cancelled by operator" in row["error"]


def test_finalize_can_still_write_cancelled_status(fake_client):
    """_finalize must of course be ABLE to write status='cancelled'
    itself — that's the path the runner takes when it sees the flag."""
    from admin import evals

    fake_client.tables["agent_eval_runs"].rows.append({
        "run_id": "c-1",
        "status": "running",
    })

    evals._finalize(
        fake_client, "c-1",
        status="cancelled",
        results=[{"scenario_id": "s1", "passed": True}],
        error="cancelled by operator at scenario 2/3",
    )

    row = fake_client.tables["agent_eval_runs"].rows[0]
    assert row["status"] == "cancelled"
    assert "cancelled by operator" in row["error"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting on predicate after {timeout}s")
