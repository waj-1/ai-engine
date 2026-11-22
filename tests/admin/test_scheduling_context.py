"""Tests for the Production-monitoring tab (Task 2.4):

  * build_scheduling_context() returns the prod-chat picker keys
  * profile_id -> agent_kind derivation on schedule create/update
    (_schedule_payload_from_form)
  * expectation defaults to _DEFAULT_EXPECTATIONS when omitted (no longer
    a 400 — see tests/admin/test_eval_realdata_expectations.py for the
    updated companion test)
  * 'Save & run now' (run_now=on) fires the freshly created schedule via
    the same _fire_schedule the manual Fire button uses

Uses the local scriptable-supabase fake + TestClient pattern established in
tests/admin/test_eval_runs.py (each admin test file defines its own small
fake rather than sharing one across files).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

import admin.eval_runs_handlers as h


# ---------------------------------------------------------------------------
# build_scheduling_context()
# ---------------------------------------------------------------------------


def test_build_scheduling_context_has_prod_chat_fields():
    with patch("admin.eval_runs_handlers.admin_service_client", return_value=MagicMock()):
        ctx = h.build_scheduling_context()
    for key in ("schedules", "profiles", "models", "businesses", "cadences"):
        assert key in ctx


def test_build_scheduling_context_default_expectation_is_the_constant():
    """Task 2.6 will refactor this into a per-agent_kind default; for now
    every profile/kind prefills the same generic rubric constant."""
    with patch("admin.eval_runs_handlers.admin_service_client", return_value=MagicMock()):
        ctx = h.build_scheduling_context()
    assert ctx["default_expectation"] == h._DEFAULT_EXPECTATIONS


# ---------------------------------------------------------------------------
# _schedule_payload_from_form: profile_id -> agent_kind derivation
# ---------------------------------------------------------------------------


def test_schedule_payload_derives_agent_kind_from_profile_id():
    """Tab 2's create form has no agent_kind field of its own — profile_id
    is the single source of truth, mirroring eval_handlers._sync_agent_kind's
    rule for suites."""
    fake_client = MagicMock()
    (fake_client.table.return_value.select.return_value.eq.return_value
     .limit.return_value.execute.return_value.data) = [
        {"profile_id": "prof-42", "agent_kind": "sample_other"}
    ]
    form = {
        "name": "sales-live", "source": "live_chats", "cadence": "daily",
        "profile_id": "prof-42",
        "judge_model": "gemini-2.5-pro",
        "expectations": "Grade helpfully.",
    }
    payload = h._schedule_payload_from_form(form, client=fake_client)
    assert payload["profile_id"] == "prof-42"
    assert payload["agent_kind"] == "sample_other"


def test_schedule_payload_keeps_form_agent_kind_when_profile_unresolved():
    """A stale/deleted profile_id (no matching row) must not wipe out an
    agent_kind the form otherwise supplied."""
    fake_client = MagicMock()
    (fake_client.table.return_value.select.return_value.eq.return_value
     .limit.return_value.execute.return_value.data) = []
    form = {
        "name": "legacy", "source": "live_chats", "cadence": "manual",
        "profile_id": "deleted-profile",
        "agent_kind": "sample_agent",
        "judge_model": "gemini-2.5-pro",
        "expectations": "Grade helpfully.",
    }
    payload = h._schedule_payload_from_form(form, client=fake_client)
    assert payload["agent_kind"] == "sample_agent"


# ---------------------------------------------------------------------------
# _schedule_payload_from_form: live_chats eligibility guard (a self-grading
# runner kind's profile is real and pickable per C1 — _load_picker_data
# widening — but it is an endpoint, not a chat agent, so a live_chats schedule
# bound to it would silently sample zero chats. Guard rejects it loudly.)
# ---------------------------------------------------------------------------


def test_schedule_payload_rejects_scored_agent_kind_via_profile():
    """profile_id resolves to a self-grading kind's profile -> its agent_kind
    is derived, then rejected by the live_chats eligibility guard (that kind
    is excluded from _eval_agent_kinds())."""
    fake_client = MagicMock()
    (fake_client.table.return_value.select.return_value.eq.return_value
     .limit.return_value.execute.return_value.data) = [
        {"profile_id": "prof-rec", "agent_kind": "sample_scored"}
    ]
    form = {
        "name": "scored-live", "source": "live_chats", "cadence": "daily",
        "profile_id": "prof-rec",
        "judge_model": "gemini-2.5-pro",
        "expectations": "Grade helpfully.",
    }
    with pytest.raises(HTTPException) as exc_info:
        h._schedule_payload_from_form(form, client=fake_client)
    assert exc_info.value.status_code == 400
    assert "sample_scored" in exc_info.value.detail


def test_schedule_payload_rejects_scored_agent_kind_direct():
    """Same guard fires when that agent_kind is supplied directly
    on the form (no profile_id involved)."""
    form = {
        "name": "scored-live-direct", "source": "live_chats", "cadence": "manual",
        "agent_kind": "sample_scored",
        "judge_model": "gemini-2.5-pro",
        "expectations": "Grade helpfully.",
    }
    with pytest.raises(HTTPException) as exc_info:
        h._schedule_payload_from_form(form, client=MagicMock())
    assert exc_info.value.status_code == 400


def test_schedule_payload_still_accepts_normal_live_chats_agent_kind():
    """Regression: the new eligibility guard must not over-reject a normal,
    live-chats-samplable kind."""
    form = {
        "name": "business-live", "source": "live_chats", "cadence": "daily",
        "agent_kind": "sample_agent",
        "judge_model": "gemini-2.5-pro",
        "expectations": "Grade helpfully.",
    }
    payload = h._schedule_payload_from_form(form, client=MagicMock())
    assert payload["agent_kind"] == "sample_agent"


# ---------------------------------------------------------------------------
# Route-level: create + 'Save & run now' via TestClient
# ---------------------------------------------------------------------------


class _ExecResult:
    def __init__(self, data: List[Dict[str, Any]]):
        self.data = data


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = list(rows)
        self.last_insert: Optional[Dict[str, Any]] = None

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

    def update(self, payload):
        return self

    def delete(self):
        return self

    def insert(self, payload):
        self.last_insert = payload
        self._rows = [{**payload, "schedule_id": "sched-run-now"}]
        return self

    def execute(self):
        return _ExecResult(self._rows)


class _FakeClient:
    def __init__(self, rows_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self._rows_by_table = rows_by_table or {}
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name: str) -> _FakeQuery:
        if name not in self.queries:
            self.queries[name] = _FakeQuery(self._rows_by_table.get(name, []))
        return self.queries[name]


def _make_app(fake_client: _FakeClient):
    from fastapi.testclient import TestClient
    from admin import auth
    from main import app

    def _fake_superuser():
        return {"user_token": "fake-jwt", "user_id": "u-1", "email": "t@waj.local"}

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(h, "get_service_client", return_value=fake_client),
        patch.object(h, "record_change"),
    ]
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


def test_create_with_run_now_fires_schedule():
    fake = _FakeClient()
    client, teardown = _make_app(fake)
    try:
        form = {
            "name": "run-now sched", "source": "live_chats", "cadence": "manual",
            "profile_id": "", "agent_kind": "sample_other",
            "judge_model": "gemini-2.5-pro", "expectations": "Grade helpfully.",
            "is_enabled": "on", "run_now": "on",
        }
        with patch.object(h, "_fire_schedule") as mock_fire:
            r = client.post("/admin/eval-runs/schedules", data=form, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        assert mock_fire.called
        fired_schedule = mock_fire.call_args.args[0]
        assert fired_schedule["schedule_id"] == "sched-run-now"
        assert fired_schedule["agent_kind"] == "sample_other"
    finally:
        teardown()


def test_create_without_run_now_does_not_fire():
    fake = _FakeClient()
    client, teardown = _make_app(fake)
    try:
        form = {
            "name": "no-fire sched", "source": "live_chats", "cadence": "manual",
            "profile_id": "", "agent_kind": "sample_other",
            "judge_model": "gemini-2.5-pro", "expectations": "Grade helpfully.",
            "is_enabled": "on",
        }
        with patch.object(h, "_fire_schedule") as mock_fire:
            r = client.post("/admin/eval-runs/schedules", data=form, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        assert not mock_fire.called
    finally:
        teardown()


def test_create_route_binds_profile_and_derives_agent_kind():
    """End-to-end through the actual POST route (not just the payload
    builder): profile_id resolves via agent_profiles and its agent_kind
    lands on the inserted schedule row."""
    fake = _FakeClient(rows_by_table={
        "agent_profiles": [{"profile_id": "prof-99", "agent_kind": "sample_agent"}],
    })
    client, teardown = _make_app(fake)
    try:
        form = {
            "name": "business live", "source": "live_chats", "cadence": "weekly",
            "profile_id": "prof-99",
            "judge_model": "gemini-2.5-pro", "expectations": "Grade helpfully.",
            "is_enabled": "on",
        }
        r = client.post("/admin/eval-runs/schedules", data=form, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        payload = fake.queries["agent_eval_schedules"].last_insert
        assert payload["profile_id"] == "prof-99"
        assert payload["agent_kind"] == "sample_agent"
    finally:
        teardown()


def test_create_route_rejects_scored_kind_profile():
    """End-to-end through the actual POST route: Tab 2's profile picker
    now offers a self-grading kind's profile (C1), but submitting a
    live_chats schedule bound to it must 400 instead of silently inserting a
    dead schedule (that kind is an endpoint, not a chat agent)."""
    fake = _FakeClient(rows_by_table={
        "agent_profiles": [{"profile_id": "prof-rec", "agent_kind": "sample_scored"}],
    })
    client, teardown = _make_app(fake)
    try:
        form = {
            "name": "scored live", "source": "live_chats", "cadence": "daily",
            "profile_id": "prof-rec",
            "judge_model": "gemini-2.5-pro", "expectations": "Grade helpfully.",
            "is_enabled": "on",
        }
        r = client.post("/admin/eval-runs/schedules", data=form, follow_redirects=False)
        assert r.status_code == 400, r.text[:400]
        assert "sample_scored" in r.text
        assert "agent_eval_schedules" not in fake.queries \
            or fake.queries["agent_eval_schedules"].last_insert is None
    finally:
        teardown()


def test_create_redirects_to_scheduling_tab():
    fake = _FakeClient()
    client, teardown = _make_app(fake)
    try:
        form = {
            "name": "redirect check", "source": "live_chats", "cadence": "manual",
            "agent_kind": "sample_other",
            "judge_model": "gemini-2.5-pro", "expectations": "Grade helpfully.",
            "is_enabled": "on",
        }
        r = client.post("/admin/eval-runs/schedules", data=form, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/admin/evals/scheduling"
    finally:
        teardown()


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
