"""Tests for the combined /admin/evals page context builder (Task 8).

Asserts build_evals_page_context() returns both suites (with _recent_runs)
and all the runs-tab keys (recent_runs, schedules, profiles, models, etc.).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Fake supabase client (similar to test_eval_runs.py)
# ---------------------------------------------------------------------------

class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def select(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def gte(self, *a, **kw): return self
    def lte(self, *a, **kw): return self
    def order(self, *a, **kw): return self
    def limit(self, *a, **kw): return self
    def is_(self, *a, **kw): return self
    def in_(self, *a, **kw): return self

    def execute(self):
        r = MagicMock()
        r.data = list(self._rows)
        return r


class _FakeClient:
    def __init__(self, rows_by_table=None):
        self._rows = rows_by_table or {}

    def table(self, name):
        return _FakeQuery(self._rows.get(name, []))

    def schema(self, _name):
        return self


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

SUITES = [
    {"suite_id": "s1", "name": "Booking Suite", "agent_kind": "business",
     "pass_threshold": 0.7, "is_active": True, "scenarios": [{"id": "sc1"}, {"id": "sc2"}],
     "evaluator_model_id": None},
    {"suite_id": "s2", "name": "Support Suite", "agent_kind": "support_bot",
     "pass_threshold": 0.8, "is_active": True, "scenarios": [],
     "evaluator_model_id": None},
]

RUNS = [
    {"run_id": "r1", "suite_id": "s1", "profile_id": "p1", "status": "passed",
     "score": 0.9, "created_at": "2026-06-01T10:00:00Z", "results": [{"passed": True}],
     "context": {}},
    {"run_id": "r2", "suite_id": "s1", "profile_id": "p1", "status": "failed",
     "score": 0.5, "created_at": "2026-06-01T09:00:00Z", "results": [{"passed": False}],
     "context": {}},
    {"run_id": "r3", "suite_id": "s2", "profile_id": "p1", "status": "passed",
     "score": 0.85, "created_at": "2026-06-01T08:00:00Z", "results": [{"passed": True}],
     "context": {}},
]

PROFILES = [{"profile_id": "p1", "display_name": "Test Profile", "agent_kind": "business",
              "endpoint_path": "/chat"}]
MODELS = [{"model_id": "m1", "name": "GPT-4", "provider": "openai", "is_active": True}]
SCHEDULES = []
BUSINESSES = [{"id": "b1", "name": "Test Biz", "subdomain": "test"}]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _make_client():
    return _FakeClient({
        "agent_eval_suites": SUITES,
        "agent_eval_runs": RUNS,
        "agent_eval_schedules": SCHEDULES,
        "agent_profiles": PROFILES,
        "agent_models": MODELS,
    })


def test_build_evals_page_context_has_suites_and_runs_keys():
    """Combined context has both suites and runs-tab keys."""
    client = _make_client()

    def _fake_businesses(c):
        return BUSINESSES

    with patch("admin.eval_runs_handlers.get_service_client", return_value=client), \
         patch("admin._business_picker.fetch_businesses_for_picker", side_effect=_fake_businesses):
        from admin.eval_runs_handlers import build_evals_page_context
        ctx = build_evals_page_context()

    # Must have suites
    assert "suites" in ctx
    assert len(ctx["suites"]) == 2

    # Must have runs-tab keys
    assert "recent_runs" in ctx
    assert "schedules" in ctx
    assert "profiles" in ctx
    assert "models" in ctx
    assert "agent_kinds" in ctx
    assert "filters" in ctx


def test_suites_decorated_with_recent_runs():
    """Each suite has _recent_runs populated from the runs list."""
    client = _make_client()

    def _fake_businesses(c):
        return BUSINESSES

    with patch("admin.eval_runs_handlers.get_service_client", return_value=client), \
         patch("admin._business_picker.fetch_businesses_for_picker", side_effect=_fake_businesses):
        from admin.eval_runs_handlers import build_evals_page_context
        ctx = build_evals_page_context()

    suites_by_id = {s["suite_id"]: s for s in ctx["suites"]}

    # s1 has 2 runs
    s1 = suites_by_id["s1"]
    assert "_recent_runs" in s1
    assert len(s1["_recent_runs"]) == 2
    run_ids = {r["run_id"] for r in s1["_recent_runs"]}
    assert run_ids == {"r1", "r2"}

    # s2 has 1 run
    s2 = suites_by_id["s2"]
    assert len(s2["_recent_runs"]) == 1
    assert s2["_recent_runs"][0]["run_id"] == "r3"


def test_suites_decorated_with_details():
    """Suites carry judge model name, last run, categories, and schedules so
    the list can show details instead of a redundant Run button."""
    suites = [
        {"suite_id": "s1", "name": "Booking", "agent_kind": "business",
         "pass_threshold": 0.7, "is_active": True, "evaluator_model_id": "m1",
         "scenarios": [{"id": "a", "category": "analytics"},
                       {"id": "b", "category": "traps"},
                       {"id": "c", "category": "analytics"}]},
    ]
    runs = [
        {"run_id": "r1", "suite_id": "s1", "profile_id": "p1", "status": "passed",
         "score": 0.9, "created_at": "2026-06-02T10:00:00Z", "results": [{"passed": True}], "context": {}},
        {"run_id": "r0", "suite_id": "s1", "profile_id": "p1", "status": "failed",
         "score": 0.5, "created_at": "2026-06-01T10:00:00Z", "results": [{"passed": False}], "context": {}},
    ]
    schedules = [{"schedule_id": "sch1", "suite_id": "s1", "name": "nightly",
                  "cadence": "daily", "is_enabled": True, "source": "suite",
                  "next_run_at": "2026-06-03T00:00:00Z", "coverage": {}}]
    client = _FakeClient({
        "agent_eval_suites": suites, "agent_eval_runs": runs,
        "agent_eval_schedules": schedules, "agent_profiles": PROFILES,
        "agent_models": MODELS,
    })
    with patch("admin.eval_runs_handlers.get_service_client", return_value=client), \
         patch("admin._business_picker.fetch_businesses_for_picker", side_effect=lambda c: BUSINESSES):
        from admin.eval_runs_handlers import build_evals_page_context
        s1 = build_evals_page_context()["suites"][0]

    assert s1["_judge_model"] == "GPT-4"                       # resolved from evaluator_model_id
    assert s1["_last_run"] and s1["_last_run"]["run_id"] == "r1"  # most recent
    assert s1["_categories"] == ["analytics", "traps"]        # distinct, sorted
    assert len(s1["_schedules"]) == 1 and s1["_schedules"][0]["cadence"] == "daily"


def test_suite_details_handle_missing_gracefully():
    """No judge / no runs / no categories / no schedule → safe empty values."""
    client = _make_client()  # SUITES have evaluator_model_id None, SCHEDULES empty
    with patch("admin.eval_runs_handlers.get_service_client", return_value=client), \
         patch("admin._business_picker.fetch_businesses_for_picker", side_effect=lambda c: BUSINESSES):
        from admin.eval_runs_handlers import build_evals_page_context
        s2 = {s["suite_id"]: s for s in build_evals_page_context()["suites"]}["s2"]
    assert s2["_judge_model"] is None
    assert s2["_categories"] == []
    assert s2["_schedules"] == []
    assert s2["_last_run"]["run_id"] == "r3"  # s2 has one run


def test_recent_runs_capped_at_five():
    """_recent_runs is limited to 5 per suite."""
    # 6 runs for suite s1
    many_runs = [
        {"run_id": f"r{i}", "suite_id": "s1", "profile_id": "p1",
         "status": "passed", "score": 0.9,
         "created_at": f"2026-06-0{i}T10:00:00Z",
         "results": [{"passed": True}], "context": {}}
        for i in range(1, 7)
    ]
    client = _FakeClient({
        "agent_eval_suites": SUITES,
        "agent_eval_runs": many_runs,
        "agent_eval_schedules": [],
        "agent_profiles": PROFILES,
        "agent_models": MODELS,
    })

    def _fake_businesses(c):
        return BUSINESSES

    with patch("admin.eval_runs_handlers.get_service_client", return_value=client), \
         patch("admin._business_picker.fetch_businesses_for_picker", side_effect=_fake_businesses):
        from admin.eval_runs_handlers import build_evals_page_context
        ctx = build_evals_page_context()

    s1 = next(s for s in ctx["suites"] if s["suite_id"] == "s1")
    assert len(s1["_recent_runs"]) == 5


def test_full_suites_returned_not_minimal():
    """Suites in context have full fields (scenarios, is_active), not just suite_id/name/agent_kind."""
    client = _make_client()

    def _fake_businesses(c):
        return BUSINESSES

    with patch("admin.eval_runs_handlers.get_service_client", return_value=client), \
         patch("admin._business_picker.fetch_businesses_for_picker", side_effect=_fake_businesses):
        from admin.eval_runs_handlers import build_evals_page_context
        ctx = build_evals_page_context()

    s1 = next(s for s in ctx["suites"] if s["suite_id"] == "s1")
    assert "scenarios" in s1
    assert "is_active" in s1
    assert "pass_threshold" in s1
