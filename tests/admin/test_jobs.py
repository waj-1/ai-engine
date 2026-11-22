"""Tests for the admin background-jobs tracker.

Covers:
  * `start_tracked_job` writes a queued row, spawns the daemon thread,
    and transitions running → succeeded with duration_ms stamped.
  * Crashed worker → status='failed' + error captures the exception
    class name + the traceback in metadata.
  * `cancel_job` flips status='cancelled' and a second call returns False
    (terminal-state guard).
  * `is_cancelled` reads from DB.
  * `list_recent_jobs` ordering + filters + allow-list guards.
  * GET /admin/jobs 307s to the merged screen (/admin/live?tab=jobs),
    which renders the Jobs tab.
  * POST /admin/jobs/{id}/cancel flips status and redirects.

The supabase client is mocked with a stateful fake that supports the
chain calls our tracker uses (insert, update().eq(), select().eq()
.limit(), select().eq().order().limit()). Threads are real but we wait
for them with a tight ``join`` poll because the tracker thread is
short — the fake supabase calls return instantly.
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
# Stateful supabase fake — minimal surface that supports our tracker calls.
# ---------------------------------------------------------------------------


class _FakeJobsTable:
    """One table's worth of in-memory rows. Mutable across queries so
    the tracker thread's `update().eq()` and the test's later
    `select().eq()` see the same row."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        # Pending filters/limits accumulated by the fluent chain.
        self._filters: Dict[str, Any] = {}
        self._limit: Optional[int] = None
        self._order_desc: Optional[bool] = None
        self._order_key: Optional[str] = None
        # Whether the current chain is a write op (insert/update).
        self._pending_insert: Optional[Dict[str, Any]] = None
        self._pending_update: Optional[Dict[str, Any]] = None

    def _reset_chain(self) -> None:
        self._filters = {}
        self._limit = None
        self._order_desc = None
        self._order_key = None
        self._pending_insert = None
        self._pending_update = None

    # Fluent API ----------------------------------------------------------

    def select(self, _cols: str = "*") -> "_FakeJobsTable":
        return self

    def eq(self, col: str, val: Any) -> "_FakeJobsTable":
        self._filters[col] = val
        return self

    def limit(self, n: int) -> "_FakeJobsTable":
        self._limit = n
        return self

    def order(self, key: str, desc: bool = False) -> "_FakeJobsTable":
        self._order_key = key
        self._order_desc = desc
        return self

    def insert(self, payload: Dict[str, Any]) -> "_FakeJobsTable":
        self._pending_insert = dict(payload)
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeJobsTable":
        self._pending_update = dict(payload)
        return self

    def execute(self) -> Any:
        # Insert path
        if self._pending_insert is not None:
            row = dict(self._pending_insert)
            row.setdefault("job_id", f"job-{len(self.rows) + 1}")
            row.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
            self.rows.append(row)
            self._reset_chain()
            return _FakeResult([dict(row)])
        # Update path
        if self._pending_update is not None:
            updated: List[Dict[str, Any]] = []
            for r in self.rows:
                if all(r.get(k) == v for k, v in self._filters.items()):
                    r.update(self._pending_update)
                    updated.append(dict(r))
            self._reset_chain()
            return _FakeResult(updated)
        # Select path
        matched = [
            dict(r) for r in self.rows
            if all(r.get(k) == v for k, v in self._filters.items())
        ]
        if self._order_key:
            matched.sort(
                key=lambda r: r.get(self._order_key) or "",
                reverse=bool(self._order_desc),
            )
        if self._limit is not None:
            matched = matched[: self._limit]
        self._reset_chain()
        return _FakeResult(matched)


class _FakeResult:
    def __init__(self, data: List[Dict[str, Any]]) -> None:
        self.data = data


class _FakeClient:
    """Routes `.table('admin_jobs')` to a shared _FakeJobsTable instance.

    Other tables (agent_audit_log used by the cancel route's record_change)
    get a separate no-op table that swallows inserts so the test doesn't
    have to know about them."""

    def __init__(self) -> None:
        self.jobs = _FakeJobsTable()
        self.audit_inserts: List[Dict[str, Any]] = []

    def table(self, name: str) -> Any:
        if name == "admin_jobs":
            return self.jobs
        # Audit log + anything else — capture inserts, ignore reads.
        return _NoopTable(self.audit_inserts)


class _NoopTable:
    def __init__(self, capture: List[Dict[str, Any]]) -> None:
        self._capture = capture
        self._pending_insert: Optional[Dict[str, Any]] = None

    def select(self, *_a, **_kw): return self
    def eq(self, *_a, **_kw): return self
    def limit(self, *_a, **_kw): return self
    def order(self, *_a, **_kw): return self
    def update(self, *_a, **_kw): return self

    def insert(self, payload: Dict[str, Any]) -> "_NoopTable":
        self._pending_insert = dict(payload)
        return self

    def execute(self) -> _FakeResult:
        if self._pending_insert is not None:
            self._capture.append(self._pending_insert)
            row = dict(self._pending_insert)
            self._pending_insert = None
            return _FakeResult([row])
        return _FakeResult([])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_client():
    """A fresh in-memory supabase fake per test."""
    return _FakeClient()


@pytest.fixture
def patch_jobs_client(fake_client):
    """Patch `admin_service_client` inside `admin._jobs` so the tracker
    uses our fake. We patch the binding inside the module (not the
    core.auth source) because the module did
    ``from core.auth import admin_service_client`` at import time."""
    with patch("admin._jobs.admin_service_client", return_value=fake_client):
        yield fake_client


def _wait_for_status(fake_client: _FakeClient, job_id: str, status: str,
                     timeout: float = 2.0) -> Dict[str, Any]:
    """Spin-wait until the tracker thread has flipped the row.

    Real DB round-trips would dominate this; against the in-memory fake
    the thread completes in microseconds and the wait essentially
    returns on the first iteration."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        for r in fake_client.jobs.rows:
            if r.get("job_id") == job_id and r.get("status") == status:
                return r
        time.sleep(0.01)
    raise AssertionError(
        f"job {job_id} never reached status={status!r}; "
        f"rows={fake_client.jobs.rows}"
    )


# ---------------------------------------------------------------------------
# start_tracked_job
# ---------------------------------------------------------------------------


def test_start_tracked_job_inserts_queued_row_and_transitions_to_succeeded(
    patch_jobs_client,
):
    from admin._jobs import start_tracked_job

    def worker(**_kw) -> None:
        # Worker is essentially synchronous against the fake DB; sleep
        # a hair so duration_ms is non-zero in at least some runs.
        time.sleep(0.01)

    job_id = start_tracked_job(
        worker,
        kind="eval_run",
        label="eval:test",
        actor_user_id="u-1",
        related_entity_type="agent_eval_runs",
        related_entity_id="run-1",
        metadata={"suite_id": "s-1"},
    )

    final = _wait_for_status(patch_jobs_client, job_id, "succeeded")
    assert final["kind"] == "eval_run"
    assert final["label"] == "eval:test"
    assert final["related_entity_type"] == "agent_eval_runs"
    assert final["related_entity_id"] == "run-1"
    assert final["triggered_by"] == "u-1"
    assert final["metadata"] == {"suite_id": "s-1"}
    assert final["started_at"]
    assert final["finished_at"]
    assert isinstance(final["duration_ms"], int)
    assert final["duration_ms"] >= 0


def test_start_tracked_job_failing_worker_marks_failed_with_traceback(
    patch_jobs_client,
):
    from admin._jobs import start_tracked_job

    def worker(**_kw) -> None:
        raise ValueError("kaboom")

    job_id = start_tracked_job(
        worker,
        kind="eval_run",
        label="eval:boom",
        actor_user_id="u-1",
    )

    final = _wait_for_status(patch_jobs_client, job_id, "failed")
    assert "ValueError" in (final.get("error") or "")
    assert "kaboom" in (final.get("error") or "")
    tb = (final.get("metadata") or {}).get("traceback") or ""
    assert "ValueError" in tb
    assert final["finished_at"]


def test_start_tracked_job_rejects_unknown_kind(patch_jobs_client):
    from admin._jobs import start_tracked_job

    with pytest.raises(ValueError, match="unknown kind"):
        start_tracked_job(
            lambda **_: None,
            kind="not_a_kind",
            label="x",
            actor_user_id="u-1",
        )


# ---------------------------------------------------------------------------
# cancel_job / is_cancelled
# ---------------------------------------------------------------------------


def test_cancel_job_flips_status_and_second_call_returns_false(patch_jobs_client):
    from admin._jobs import cancel_job, is_cancelled

    # Seed a queued row directly so the cancel races no worker thread.
    row = {
        "job_id": "j-1",
        "kind": "eval_run",
        "label": "x",
        "status": "queued",
        "metadata": {},
    }
    patch_jobs_client.jobs.rows.append(row)

    assert is_cancelled("j-1") is False
    assert cancel_job("j-1") is True
    assert is_cancelled("j-1") is True
    # Second call: terminal state already, must return False.
    assert cancel_job("j-1") is False


def test_cancel_job_missing_row_returns_false(patch_jobs_client):
    from admin._jobs import cancel_job

    assert cancel_job("does-not-exist") is False


def test_cancel_job_terminal_states_return_false(patch_jobs_client):
    from admin._jobs import cancel_job

    for status in ("succeeded", "failed"):
        patch_jobs_client.jobs.rows = [
            {"job_id": f"j-{status}", "kind": "eval_run", "label": "x",
             "status": status, "metadata": {}}
        ]
        assert cancel_job(f"j-{status}") is False


def test_cancelled_before_finish_preserves_cancelled_status(patch_jobs_client):
    """If the operator cancels mid-flight, the worker's natural finish
    must NOT clobber the 'cancelled' status with 'succeeded'."""
    from admin._jobs import cancel_job, start_tracked_job

    barrier = {"go": False}

    def slow_worker(**_kw) -> None:
        # Wait until the test has flipped status to 'cancelled' before
        # returning, so we exercise the cancellation-preservation branch.
        deadline = time.perf_counter() + 1.0
        while not barrier["go"] and time.perf_counter() < deadline:
            time.sleep(0.005)

    job_id = start_tracked_job(
        slow_worker, kind="eval_run", label="slow", actor_user_id="u-1"
    )
    # Give the thread a moment to flip to 'running'.
    _wait_for_status(patch_jobs_client, job_id, "running")
    assert cancel_job(job_id) is True
    barrier["go"] = True
    final = _wait_for_status(patch_jobs_client, job_id, "cancelled")
    assert final["status"] == "cancelled"


# ---------------------------------------------------------------------------
# list_recent_jobs
# ---------------------------------------------------------------------------


def test_list_recent_jobs_orders_newest_first_and_filters(patch_jobs_client):
    from admin._jobs import list_recent_jobs

    patch_jobs_client.jobs.rows = [
        {"job_id": "a", "kind": "eval_run", "label": "old", "status": "succeeded",
         "metadata": {}, "created_at": "2026-05-19T10:00:00+00:00"},
        {"job_id": "b", "kind": "kb_upload", "label": "mid", "status": "running",
         "metadata": {}, "created_at": "2026-05-20T10:00:00+00:00"},
        {"job_id": "c", "kind": "eval_run", "label": "new", "status": "queued",
         "metadata": {}, "created_at": "2026-05-21T10:00:00+00:00"},
    ]

    out = list_recent_jobs(limit=10)
    assert [r["job_id"] for r in out] == ["c", "b", "a"]

    out_filtered_kind = list_recent_jobs(kind="eval_run", limit=10)
    assert {r["job_id"] for r in out_filtered_kind} == {"a", "c"}

    out_filtered_status = list_recent_jobs(status="running", limit=10)
    assert [r["job_id"] for r in out_filtered_status] == ["b"]


def test_list_recent_jobs_rejects_unknown_filters(patch_jobs_client):
    """Unknown status / kind shortcuts to [] without hitting the DB."""
    from admin._jobs import list_recent_jobs

    patch_jobs_client.jobs.rows = [
        {"job_id": "a", "kind": "eval_run", "label": "x", "status": "queued",
         "metadata": {}, "created_at": "2026-05-21T10:00:00+00:00"},
    ]
    assert list_recent_jobs(status="bogus") == []
    assert list_recent_jobs(kind="bogus") == []


# ---------------------------------------------------------------------------
# UI route + cancel route
# ---------------------------------------------------------------------------


def _make_app(fake_client):
    """Spin up a TestClient with require_superuser bypassed and the
    sidecar's supabase client patched."""
    from fastapi.testclient import TestClient
    from admin import auth, jobs_handlers, _jobs
    from main import app

    def _fake_super():
        return {"user_token": "tok", "user_id": "u-1", "email": "a@waj.test"}

    app.dependency_overrides[auth.require_superuser] = _fake_super
    patches = [
        patch.object(_jobs, "admin_service_client", return_value=fake_client),
        patch.object(jobs_handlers, "record_change"),
    ]
    for p in patches:
        p.start()
    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


def test_live_jobs_tab_redirects_to_the_jobs_screen():
    """The Jobs tab moved from the Overview to Settings › Diagnostics
    (/admin/jobs). The old URL 307s there, filters carried over."""
    from urllib.parse import parse_qs, urlsplit

    fake = _FakeClient()
    client, teardown = _make_app(fake)
    try:
        resp = client.get("/admin/live?tab=jobs&kind=eval_run&status=running",
                          follow_redirects=False)
        assert resp.status_code == 307
        parts = urlsplit(resp.headers["location"])
        assert parts.path == "/admin/jobs"
        assert parse_qs(parts.query) == {"kind": ["eval_run"], "status": ["running"]}
    finally:
        teardown()


def test_jobs_screen_lives_under_settings_diagnostics():
    fake = _FakeClient()
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/jobs")
        assert r.status_code == 200
        assert "Background jobs" in r.text
        # Diagnostics strip with Background jobs active.
        assert 'href="/admin/jobs"' in r.text
        assert 'action="/admin/jobs"' in r.text
    finally:
        teardown()


def test_jobs_list_route_renders_recent_rows():
    fake = _FakeClient()
    fake.jobs.rows = [
        {"job_id": "j-1", "kind": "eval_run", "label": "eval:abc",
         "status": "running", "started_at": "2026-05-20T10:00:00+00:00",
         "created_at": "2026-05-20T10:00:00+00:00", "metadata": {},
         "duration_ms": None, "error": None, "triggered_by": "u-1"},
        {"job_id": "j-2", "kind": "kb_upload", "label": "kb:docs",
         "status": "succeeded", "duration_ms": 1234,
         "started_at": "2026-05-20T09:00:00+00:00",
         "finished_at": "2026-05-20T09:01:00+00:00",
         "created_at": "2026-05-20T09:00:00+00:00", "metadata": {},
         "error": None, "triggered_by": "u-1"},
    ]
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/jobs")
        assert r.status_code == 200
        body = r.text
        # Tab strip renders with Jobs active
        assert 'class="tab-strip"' in body
        assert 'aria-current="page"' in body
        assert "eval:abc" in body
        assert "kb:docs" in body
        # In-flight job → in-place poller wired (replaced the old
        # <meta http-equiv="refresh">, which yanked the operator to the top
        # of the page every 5s).
        assert 'http-equiv="refresh"' not in body
        assert "jobs-poll.js" in body
    finally:
        teardown()


def test_jobs_list_route_no_inflight_skips_auto_refresh():
    fake = _FakeClient()
    fake.jobs.rows = [
        {"job_id": "j-1", "kind": "eval_run", "label": "eval:done",
         "status": "succeeded", "duration_ms": 100,
         "started_at": "2026-05-20T10:00:00+00:00",
         "finished_at": "2026-05-20T10:00:01+00:00",
         "created_at": "2026-05-20T10:00:00+00:00",
         "metadata": {}, "error": None, "triggered_by": "u-1"},
    ]
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/jobs")
        assert r.status_code == 200
        assert 'http-equiv="refresh"' not in r.text
        # No in-flight jobs → the poller isn't even loaded.
        assert "jobs-poll.js" not in r.text
    finally:
        teardown()


def test_jobs_cancel_route_flips_status_and_redirects():
    fake = _FakeClient()
    fake.jobs.rows = [
        {"job_id": "j-1", "kind": "eval_run", "label": "x",
         "status": "running", "metadata": {},
         "started_at": "2026-05-20T10:00:00+00:00",
         "created_at": "2026-05-20T10:00:00+00:00"},
    ]
    client, teardown = _make_app(fake)
    try:
        r = client.post("/admin/jobs/j-1/cancel", follow_redirects=False)
        assert r.status_code == 302
        assert fake.jobs.rows[0]["status"] == "cancelled"
    finally:
        teardown()
