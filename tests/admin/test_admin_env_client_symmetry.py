"""Admin reads and writes must resolve the SAME environment branch.

The env-switcher middleware parks the selected env in the request-scoped
`core.auth._ADMIN_ENV` ContextVar, screens read through `client_for(env)` /
`admin_service_client()` — and, before the symmetry sweep, a handful of write
and background paths still called the bare `get_service_client()`, which
always targets the LOCAL branch. Latent while the cross-env vars are unset
(both factories collapse to the same DB), structural on any cross-env
control-plane: the write lands where the screen never reads.

These tests pin WHICH factory each converted path calls. They discriminate:
with the fix reverted, the module no longer has an `admin_service_client`
attribute, so the patch itself fails — and the bare-factory bomb would fire.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimal fluent PostgREST chain recording inserts/updates per table."""

    def __init__(self, rows: List[Dict[str, Any]], insert_extra: Optional[Dict[str, Any]] = None):
        self._rows = rows
        self._insert_extra = insert_extra or {}
        self.inserts: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []
        self._pending: Optional[List[Dict[str, Any]]] = None

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, payload):
        self.inserts.append(payload)
        self._pending = [{**payload, **self._insert_extra}]
        return self

    def update(self, payload):
        self.updates.append(payload)
        self._pending = [dict(payload)]
        return self

    def execute(self):
        if self._pending is not None:
            pending, self._pending = self._pending, None
            return _Result(pending)
        return _Result(list(self._rows))


class _FakeClient:
    def __init__(
        self,
        rows_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        insert_extra_by_table: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.queries: Dict[str, _Query] = {}
        self._rows_by_table = rows_by_table or {}
        self._insert_extra_by_table = insert_extra_by_table or {}

    def table(self, name: str) -> _Query:
        if name not in self.queries:
            self.queries[name] = _Query(
                self._rows_by_table.get(name, []),
                self._insert_extra_by_table.get(name),
            )
        return self.queries[name]


def _bomb(name: str):
    def _fail(*_a, **_k):
        raise AssertionError(f"{name} must not be called on this path")
    return _fail


# ---------------------------------------------------------------------------
# voice_scenario_runner.run_scenario_job — env-pinned, env-aware client
# ---------------------------------------------------------------------------


def test_run_scenario_job_uses_admin_service_client_and_pins_environment(monkeypatch):
    """The scenario lookup + run row must go through `admin_service_client()`
    with the passed `environment` pinned — the MCP `run` action's explicit
    `env=` cannot ride the job-spawn ContextVar capture (/mcp requests never
    run the /admin env middleware), so the kwarg is the wire."""
    import core.auth as core_auth
    from admin import voice_scenario_runner as vsr

    fake = _FakeClient({
        "ai_voice_scenarios": [{
            "scenario_id": "sc-1", "slug": "greeting", "agent_kind": "orchard",
            "ws_path": "/voice/live", "steps": [{"type": "wait", "sec": 0}],
            "enabled": False,  # disabled → skipped, no real WS call
        }],
    })
    seen_envs: List[Optional[str]] = []

    def _factory():
        seen_envs.append(core_auth.get_admin_env())
        return fake

    monkeypatch.setattr(vsr, "admin_service_client", _factory)
    monkeypatch.setattr(core_auth, "get_service_client", _bomb("get_service_client"))

    # run_scenario_job pins _ADMIN_ENV in the calling context (in production
    # that context is a dedicated job thread) — restore around the call.
    token = core_auth.set_admin_env(None)
    try:
        result = vsr.run_scenario_job(slug="greeting", environment="prod")
    finally:
        core_auth.reset_admin_env(token)

    assert result["status"] == "skipped"
    assert result["environment"] == "prod"
    # Factory used at least once, and every resolution saw the pinned env.
    assert seen_envs and set(seen_envs) == {"prod"}
    # The run row landed through the same client.
    assert fake.queries["ai_voice_scenario_runs"].inserts, "run row not written"


def test_run_scenario_job_missing_scenario_reports_error_from_selected_branch(monkeypatch):
    import core.auth as core_auth
    from admin import voice_scenario_runner as vsr

    fake = _FakeClient({"ai_voice_scenarios": []})
    monkeypatch.setattr(vsr, "admin_service_client", lambda: fake)
    monkeypatch.setattr(core_auth, "get_service_client", _bomb("get_service_client"))

    token = core_auth.set_admin_env(None)
    try:
        result = vsr.run_scenario_job(slug="ghost", environment="staging")
    finally:
        core_auth.reset_admin_env(token)

    assert result["status"] == "error"
    assert "ghost" in result["reason"]


# ---------------------------------------------------------------------------
# _jobs.start_tracked_job — a worker's own env pin must not reach the tracker
# ---------------------------------------------------------------------------


def test_tracked_job_tracker_touches_all_resolve_the_captured_env(monkeypatch):
    """Integration-shaped: spawn via start_tracked_job with a worker that
    pins a DIFFERENT env (as voice_scenario_runner.run_scenario_job does on
    the MCP path) and assert all four tracker touches — queued insert,
    running update, terminal status fetch, terminal update — resolve the
    CAPTURED env. Without `_run` executing the worker inside a context copy,
    the pin leaks and the terminal update lands on another branch: a silent
    zero-row PATCH, job stuck "running" forever."""
    import time as _time

    import core.auth as core_auth
    from admin import _jobs

    fake = _FakeClient(
        rows_by_table={"admin_jobs": [{"job_id": "j-sym", "status": "running"}]},
        insert_extra_by_table={"admin_jobs": {"job_id": "j-sym"}},
    )
    seen_envs: List[Optional[str]] = []

    def _factory():
        seen_envs.append(core_auth.get_admin_env())
        return fake

    monkeypatch.setattr(_jobs, "admin_service_client", _factory)

    def _worker():
        # A worker pinning its own, DIFFERENT env — must stay contained.
        core_auth.set_admin_env("staging")

    token = core_auth.set_admin_env("prod")  # the operator's captured env
    try:
        job_id = _jobs.start_tracked_job(
            _worker, kind="other", label="env-pin-containment",
            actor_user_id="u-1",
        )
    finally:
        core_auth.reset_admin_env(token)

    assert job_id == "j-sym"
    # Wait for the daemon worker to reach its terminal update.
    q = fake.queries["admin_jobs"]
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if any(u.get("status") in ("succeeded", "failed") for u in q.updates):
            break
        _time.sleep(0.01)
    else:  # pragma: no cover - only on regression
        raise AssertionError(f"worker never reached a terminal update: {q.updates}")

    # queued insert + running update + terminal status fetch + terminal
    # update = four client resolutions, every one on the CAPTURED env.
    assert len(seen_envs) >= 4
    assert set(seen_envs) == {"prod"}, seen_envs
    assert [u["status"] for u in q.updates][:1] == ["running"]
    assert q.updates[-1]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# test_runner.run_suite — env-aware client
# ---------------------------------------------------------------------------


def test_run_suite_writes_rows_through_admin_service_client(monkeypatch):
    import subprocess

    import core.auth as core_auth
    from admin import test_runner as tr

    fake = _FakeClient()
    monkeypatch.setattr(tr, "admin_service_client", lambda: fake)
    monkeypatch.setattr(core_auth, "get_service_client", _bomb("get_service_client"))

    class _Proc:
        returncode = 0
        stdout = "2 passed in 0.01s\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())

    result = tr.run_suite(suite="unit_admin", triggered_by="u-1")

    assert result["status"] == "passed"
    q = fake.queries["agent_test_runs"]
    assert q.inserts and q.inserts[0]["status"] == "running"
    assert q.updates and q.updates[0]["status"] == "passed"


# ---------------------------------------------------------------------------
# channel_handlers create — representative write handler
# ---------------------------------------------------------------------------


def test_channel_create_writes_through_admin_service_client(client):
    """POST /admin/channels must insert via the env-switcher client, not the
    bare local one — the /admin/channels list reads `client_for(env)`."""
    from admin import channel_handlers

    fake = _FakeClient(insert_extra_by_table={"agent_channels": {"channel_id": "ch-1"}})
    with patch.object(channel_handlers, "admin_service_client", return_value=fake), \
            patch.object(channel_handlers, "record_change"), \
            patch.object(channel_handlers, "_invalidate_channels_cache"), \
            patch("core.auth.get_service_client", _bomb("get_service_client")):
        r = client.post(
            "/admin/channels",
            data={
                "slug": "sym_test",
                "name": "Symmetry Test",
                "kind": "other",
                "rate_limit_rpm": "60",
            },
            follow_redirects=False,
        )

    assert r.status_code == 303, r.text[:400]
    inserts = fake.queries["agent_channels"].inserts
    assert inserts and inserts[0]["slug"] == "sym_test"


# ---------------------------------------------------------------------------
# env_vars / test_fixtures create — the row names its own environment
# ---------------------------------------------------------------------------


def test_env_var_create_targets_the_forms_environment_branch(client):
    """The Variables tab reads each env group through THAT env's branch
    client (`fetch_env_vars_by_env`), so create must write via
    `client_for(<form environment>)` — not the switcher env, and never the
    bare local client."""
    from admin import env_vars_handlers as evh

    fake = _FakeClient()
    seen_envs: List[str] = []

    def _client_for(env):
        seen_envs.append(env)
        return fake

    with patch.object(evh, "client_for", side_effect=_client_for), \
            patch.object(evh, "record_change"), \
            patch("core.auth.get_service_client", _bomb("get_service_client")):
        r = client.post(
            "/admin/env-vars",
            data={
                "environment": "prod",
                "name": "SYMMETRY_TEST",
                "value": "1",
                "source": "db",
            },
            follow_redirects=False,
        )

    assert r.status_code == 302, r.text[:400]
    assert seen_envs == ["prod"]
    inserts = fake.queries["agent_env_vars"].inserts
    assert inserts and inserts[0]["environment"] == "prod"


def test_test_fixture_create_targets_the_forms_environment_branch(client):
    from admin import test_fixtures_handlers as tfh

    fake = _FakeClient()
    seen_envs: List[str] = []

    def _client_for(env):
        seen_envs.append(env)
        return fake

    with patch.object(tfh, "client_for", side_effect=_client_for), \
            patch.object(tfh, "record_change"), \
            patch("core.auth.get_service_client", _bomb("get_service_client")):
        r = client.post(
            "/admin/test-fixtures",
            data={
                "environment": "prod",
                "name": "symmetry_probe",
                "kind": "string",
                "value": "x",
            },
            follow_redirects=False,
        )

    assert r.status_code == 302, r.text[:400]
    assert seen_envs == ["prod"]
    inserts = fake.queries["test_fixtures"].inserts
    assert inserts and inserts[0]["environment"] == "prod"
