"""Tests for Task 2.5 — Production-monitoring tab (Tab 2): trend, indicator
tiles, and the "Recent scheduled runs" table + compare.

Covers:
  * _compute_schedule_indicators — the brief's binding test, plus drift /
    empty-input edge cases
  * build_scheduling_context() returns `scheduled_runs` (only schedule_id-
    bearing agent_eval_runs rows, newest first, decorated like Tab 1's
    recent_runs) + `indicators` (the _compute_schedule_indicators aggregate
    over that same list)
  * GET /admin/eval-runs/report.json: `scheduled_only=1` narrows points to
    schedule_id-bearing runs, `schedule_id=` narrows to one schedule, and
    neither param present is byte-for-byte the pre-2.5 behavior

Uses the local scriptable-supabase fake + TestClient pattern established in
tests/admin/test_eval_runs.py (each admin test file defines its own small
fake rather than sharing one across files). tests/admin/conftest.py's
autouse `_route_admin_service_client_to_get_service_client` fixture makes
`eval_runs_handlers.admin_service_client()` follow a patched
`eval_runs_handlers.get_service_client` even with no admin env selected —
see that fixture's docstring — so patching `get_service_client` (not
`admin_service_client`) is the right, established knob here.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

import admin.eval_runs_handlers as h


# ---------------------------------------------------------------------------
# _compute_schedule_indicators — binding test (brief §Step 1) + edge cases
# ---------------------------------------------------------------------------


def test_indicators_from_runs():
    """Brief's binding test, transcribed verbatim."""
    from admin.eval_runs_handlers import _compute_schedule_indicators

    runs = [{"score": 0.8, "results": [{"passed": True}, {"passed": False}]},
            {"score": 0.6, "results": [{"passed": True}]}]
    ind = _compute_schedule_indicators(runs)
    assert ind["latest_score"] == 0.8
    assert 0 <= ind["pass_rate"] <= 1
    assert ind["samples"] == 3


def test_indicators_drift_needs_two_scored_runs():
    assert h._compute_schedule_indicators([])["drift"] is None
    assert h._compute_schedule_indicators([{"score": 0.9, "results": []}])["drift"] is None
    ind = h._compute_schedule_indicators(
        [{"score": 0.9, "results": []}, {"score": 0.7, "results": []}]
    )
    assert ind["drift"] == 0.2


def test_indicators_empty_runs_is_safe():
    ind = h._compute_schedule_indicators([])
    assert ind == {"latest_score": None, "pass_rate": None, "samples": 0, "drift": None}


# ---------------------------------------------------------------------------
# Scriptable supabase fake — mirrors tests/admin/test_eval_runs.py's surface.
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = list(rows)

    def select(self, *a, **kw):
        return self

    def eq(self, *a, **kw):
        return self

    def order(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    def gte(self, *a, **kw):
        return self

    def lte(self, *a, **kw):
        return self

    def in_(self, *a, **kw):
        return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _FakeClient:
    def __init__(self, rows_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self._rows_by_table = rows_by_table or {}
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name: str) -> _FakeQuery:
        if name not in self.queries:
            self.queries[name] = _FakeQuery(self._rows_by_table.get(name, []))
        return self.queries[name]


# ---------------------------------------------------------------------------
# build_scheduling_context(): scheduled_runs + indicators
# ---------------------------------------------------------------------------


def test_build_scheduling_context_returns_scheduled_runs_and_indicators():
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            # newest first, mirroring the real order("created_at", desc=True)
            {"run_id": "run-sched-2", "profile_id": "p-1", "status": "passed",
             "score": 0.8, "results": [{"passed": True}, {"passed": False}],
             "created_at": "2026-07-20T00:00:00Z", "started_at": "2026-07-20T00:00:00Z",
             "finished_at": "2026-07-20T00:01:00Z", "schedule_id": "sched-1"},
            {"run_id": "run-sched-1", "profile_id": "p-1", "status": "passed",
             "score": 0.6, "results": [{"passed": True}],
             "created_at": "2026-07-19T00:00:00Z", "started_at": "2026-07-19T00:00:00Z",
             "finished_at": "2026-07-19T00:01:00Z", "schedule_id": "sched-1"},
            # NOT a scheduled run (schedule_id NULL) — must be excluded, even
            # though it's newer than both scheduled runs above.
            {"run_id": "run-ondemand", "profile_id": "p-1", "status": "passed",
             "score": 0.99, "results": [{"passed": True}],
             "created_at": "2026-07-21T00:00:00Z", "schedule_id": None},
        ],
        "agent_eval_schedules": [
            {"schedule_id": "sched-1", "name": "Nightly live", "source": "live_chats",
             "cadence": "daily", "coverage": {}, "is_enabled": True},
        ],
        "agent_profiles": [{"profile_id": "p-1", "display_name": "Biz prod",
                            "agent_kind": "business", "endpoint_path": "/business/chat"}],
        "agent_models": [],
    })
    with patch.object(h, "get_service_client", return_value=fake):
        ctx = h.build_scheduling_context()

    assert "scheduled_runs" in ctx
    assert "indicators" in ctx
    run_ids = [r["run_id"] for r in ctx["scheduled_runs"]]
    assert run_ids == ["run-sched-2", "run-sched-1"]
    assert "run-ondemand" not in run_ids

    # decorated like Tab 1's recent_runs
    r0 = ctx["scheduled_runs"][0]
    assert r0["_profile"] == "Biz prod"
    assert r0["_pass_rate"] == 0.5
    assert r0["_started"] == "2026-07-20T00:00:00"
    assert r0["_finished"] == "2026-07-20T00:01:00"

    ind = ctx["indicators"]
    assert ind["latest_score"] == 0.8
    assert ind["samples"] == 3
    assert ind["drift"] == 0.2


def test_build_scheduling_context_empty_scheduled_runs():
    fake = _FakeClient(rows_by_table={"agent_eval_runs": []})
    with patch.object(h, "get_service_client", return_value=fake):
        ctx = h.build_scheduling_context()
    assert ctx["scheduled_runs"] == []
    assert ctx["indicators"] == {
        "latest_score": None, "pass_rate": None, "samples": 0, "drift": None,
    }


def test_build_scheduling_context_handles_missing_agent_eval_runs_fixture():
    """Regression guard: fixtures that predate Task 2.5 (no 'agent_eval_runs'
    key at all — see test_scheduling_screen_renders_schedules in
    test_eval_runs.py) must not crash now that this context also queries
    that table."""
    fake = _FakeClient(rows_by_table={
        "agent_eval_schedules": [
            {"schedule_id": "sched-1", "name": "Nightly live", "source": "live_chats",
             "cadence": "daily", "coverage": {"sample_size": 10}, "is_enabled": True},
        ],
    })
    with patch.object(h, "get_service_client", return_value=fake):
        ctx = h.build_scheduling_context()
    assert ctx["scheduled_runs"] == []
    assert ctx["indicators"]["samples"] == 0


# ---------------------------------------------------------------------------
# GET /admin/eval-runs/report.json — scheduled_only / schedule_id filter
# ---------------------------------------------------------------------------


def _make_app(fake_client):
    from fastapi.testclient import TestClient
    from admin import auth
    from main import app

    def _fake_superuser():
        return {"user_token": "fake-jwt", "user_id": "u-1", "email": "t@waj.local"}

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [patch.object(h, "get_service_client", return_value=fake_client)]
    for p in patches:
        p.start()
    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        if prior is not None:
            app.dependency_overrides[auth.require_superuser] = prior
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


_REPORT_RUNS = [
    {"run_id": "run-a", "suite_id": None, "status": "passed", "score": 0.9,
     "results": [{"passed": True}], "created_at": "2026-07-20T00:00:00Z",
     "context": {}, "schedule_id": "sched-1"},
    {"run_id": "run-b", "suite_id": None, "status": "passed", "score": 0.5,
     "results": [{"passed": True}], "created_at": "2026-07-21T00:00:00Z",
     "context": {}, "schedule_id": None},
    {"run_id": "run-c", "suite_id": None, "status": "passed", "score": 0.7,
     "results": [{"passed": True}], "created_at": "2026-07-20T00:05:00Z",
     "context": {}, "schedule_id": "sched-2"},
]


def test_report_json_scheduled_only_narrows_to_schedule_runs():
    fake = _FakeClient(rows_by_table={"agent_eval_runs": _REPORT_RUNS, "agent_eval_suites": []})
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/eval-runs/report.json?days=60&scheduled_only=1")
        assert r.status_code == 200, r.text[:400]
        run_ids = sorted(p["run_id"] for p in r.json()["points"])
        assert run_ids == ["run-a", "run-c"]
    finally:
        teardown()


def test_report_json_schedule_id_narrows_to_one_schedule():
    fake = _FakeClient(rows_by_table={"agent_eval_runs": _REPORT_RUNS, "agent_eval_suites": []})
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/eval-runs/report.json?days=60&schedule_id=sched-1")
        assert r.status_code == 200, r.text[:400]
        run_ids = [p["run_id"] for p in r.json()["points"]]
        assert run_ids == ["run-a"]
    finally:
        teardown()


def test_report_json_absent_scheduled_only_is_unchanged():
    """No scheduled_only/schedule_id param → identical to pre-2.5 behavior:
    every run in the window, scheduled or not."""
    fake = _FakeClient(rows_by_table={"agent_eval_runs": _REPORT_RUNS, "agent_eval_suites": []})
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/eval-runs/report.json?days=60")
        assert r.status_code == 200, r.text[:400]
        run_ids = sorted(p["run_id"] for p in r.json()["points"])
        assert run_ids == ["run-a", "run-b", "run-c"]
    finally:
        teardown()


# ---------------------------------------------------------------------------
# _fire_schedule — schedule_id attribution (Task 2.5 review test-gap)
# ---------------------------------------------------------------------------


def test_fire_schedule_live_chats_forwards_schedule_id():
    """_fire_schedule on a source='live_chats' schedule forwards schedule_id
    to start_live_chat_run as a kwarg."""
    schedule = {
        "schedule_id": "sched-test-lc",
        "source": "live_chats",
        "profile_id": "profile-123",
        "agent_kind": "business",
        "coverage": {
            "lookback_days": 30,
            "sample_size": 10,
            "judge_model": "Claude Haiku",
            "expectations": "Test expectations",
            "business_id": "biz-456",
        },
    }

    with patch.object(h, "_start_live_chat_run") as mock_start_lc, \
         patch.object(h, "admin_service_client") as mock_admin_client:

        mock_start_lc.return_value = "run-id-123"
        mock_admin_client_instance = MagicMock()
        mock_admin_client.return_value = mock_admin_client_instance
        mock_update_query = MagicMock()
        mock_admin_client_instance.table.return_value.update.return_value = mock_update_query
        mock_update_query.eq.return_value = mock_update_query

        run_id = h._fire_schedule(
            schedule,
            actor_user_id="user-123",
            user_token="token-abc",
        )

        # Verify schedule_id was passed to _start_live_chat_run
        mock_start_lc.assert_called_once()
        call_kwargs = mock_start_lc.call_args[1]
        assert call_kwargs["schedule_id"] == "sched-test-lc"
        assert call_kwargs["profile_id"] == "profile-123"
        assert call_kwargs["agent_kind"] == "business"
        assert run_id == "run-id-123"


def test_fire_schedule_suite_forwards_schedule_id():
    """_fire_schedule on a source='suite' schedule forwards schedule_id
    to start_suite_run as a kwarg."""
    schedule = {
        "schedule_id": "sched-test-suite",
        "source": "suite",
        "suite_id": "suite-789",
        "profile_id": "profile-456",
        "coverage": {
            "business_id": "biz-789",
        },
    }

    with patch.object(h, "_start_suite_run") as mock_start_suite, \
         patch.object(h, "admin_service_client") as mock_admin_client:

        mock_start_suite.return_value = "run-id-suite-456"
        mock_admin_client_instance = MagicMock()
        mock_admin_client.return_value = mock_admin_client_instance
        mock_update_query = MagicMock()
        mock_admin_client_instance.table.return_value.update.return_value = mock_update_query
        mock_update_query.eq.return_value = mock_update_query

        run_id = h._fire_schedule(
            schedule,
            actor_user_id="user-456",
            user_token="token-xyz",
        )

        # Verify schedule_id was passed to _start_suite_run
        mock_start_suite.assert_called_once()
        call_kwargs = mock_start_suite.call_args[1]
        assert call_kwargs["schedule_id"] == "sched-test-suite"
        assert call_kwargs["suite_id"] == "suite-789"
        assert call_kwargs["profile_id"] == "profile-456"
        assert run_id == "run-id-suite-456"
