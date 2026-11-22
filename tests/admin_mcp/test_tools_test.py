"""Tests for the Task 12 test-domain tools: eval trigger/results/suites/
schedules, pytest + e2e runs. Same FakeClient pattern as
test_tools_configure.py / test_tools_configure_registry.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import admin_mcp.tools_test as tt
from tests.admin_mcp.conftest import FakeClient

SUITE = {"suite_id": "s1", "name": "Business smoke", "agent_kind": "sample_agent",
         "scenarios": [{"id": "q1", "question": "hours?"}],
         "pass_threshold": 0.7, "is_active": True}

RUN_A = {"run_id": "r1", "suite_id": "s1", "profile_id": "p1",
         "status": "passed", "score": 0.9, "context": {}, "results": []}
RUN_B = {"run_id": "r2", "suite_id": "s1", "profile_id": "p1",
         "status": "passed", "score": 0.95, "context": {}, "results": []}

SCHEDULE = {"schedule_id": "sch1", "name": "nightly", "suite_id": "s1",
            "profile_id": "p1", "agent_kind": "sample_agent", "source": "suite",
            "cadence": "daily", "coverage": {}, "is_enabled": True}


def _fake(monkeypatch, tables):
    client = FakeClient(tables)
    monkeypatch.setattr(tt, "client_for_env", lambda env: client)
    return client


def _noaudit(monkeypatch):
    monkeypatch.setattr(tt, "actor_audit", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# trigger_eval_run
# ---------------------------------------------------------------------------


def test_trigger_eval_run_suite_happy_path(monkeypatch):
    audits = []
    monkeypatch.setattr(tt, "actor_audit",
                        lambda *a, **k: audits.append((a, k)))

    import admin.services.evals as evals
    seen = {}

    def fake_start_suite_run(**kw):
        seen.update(kw)
        return "r1"

    monkeypatch.setattr(evals, "start_suite_run", fake_start_suite_run)

    out = tt.trigger_eval_run(profile_id="p1", source="suite", suite_id="s1")

    assert out["run_id"] == "r1"
    assert out["status"] == "queued"
    assert seen["suite_id"] == "s1"
    assert seen["profile_id"] == "p1"

    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[:3] == ("agent_eval_runs", "r1", "trigger_suite")
    assert kwargs["after"] == {"source": "suite", "profile_id": "p1"}


def test_trigger_eval_run_live_chats_missing_judge_model(monkeypatch):
    out = tt.trigger_eval_run(profile_id="p1", source="live_chats",
                              agent_kind="sample_agent")
    assert "error" in out
    assert "judge_model" in out["error"]


def test_trigger_eval_run_live_chats_bad_agent_kind(monkeypatch):
    import admin.eval_runs_handlers as erh
    monkeypatch.setattr(erh, "_eval_agent_kinds", lambda: ("sample_agent", "sample_other"))

    out = tt.trigger_eval_run(profile_id="p1", source="live_chats",
                              agent_kind="bogus_kind", judge_model="gpt",
                              expectations="be helpful")
    assert "error" in out
    assert "agent_kind" in out["error"]


def test_trigger_eval_run_bad_source():
    out = tt.trigger_eval_run(profile_id="p1", source="bogus")
    assert "error" in out


def test_trigger_eval_run_suite_missing_suite_id():
    out = tt.trigger_eval_run(profile_id="p1", source="suite")
    assert "error" in out
    assert "suite_id" in out["error"]


def test_trigger_eval_run_forwards_voice_mode_sim(monkeypatch):
    _noaudit(monkeypatch)
    import admin.services.evals as evals
    seen = {}

    def fake_start_suite_run(**kw):
        seen.update(kw)
        return "r1"

    monkeypatch.setattr(evals, "start_suite_run", fake_start_suite_run)

    tt.trigger_eval_run(profile_id="p1", source="suite", suite_id="s1",
                        voice_mode="sim")
    assert seen["voice_mode"] == "sim"


def test_trigger_eval_run_forwards_voice_mode_live(monkeypatch):
    """Live is manual-only — but this tool IS the manual trigger, so a
    direct live request must reach start_suite_run unmodified; it's
    _voice_live_guard's job (inside start_suite_run) to reject live for a
    schedule/bake-off-spawned run, not this tool's."""
    _noaudit(monkeypatch)
    import admin.services.evals as evals
    seen = {}

    def fake_start_suite_run(**kw):
        seen.update(kw)
        return "r1"

    monkeypatch.setattr(evals, "start_suite_run", fake_start_suite_run)

    tt.trigger_eval_run(profile_id="p1", source="suite", suite_id="s1",
                        voice_mode="live")
    assert seen["voice_mode"] == "live"


def test_trigger_eval_run_voice_mode_blank_omits(monkeypatch):
    _noaudit(monkeypatch)
    import admin.services.evals as evals
    seen = {}

    def fake_start_suite_run(**kw):
        seen.update(kw)
        return "r1"

    monkeypatch.setattr(evals, "start_suite_run", fake_start_suite_run)

    tt.trigger_eval_run(profile_id="p1", source="suite", suite_id="s1")
    assert seen["voice_mode"] is None


# ---------------------------------------------------------------------------
# eval_runs
# ---------------------------------------------------------------------------


def test_eval_runs_list(monkeypatch):
    _fake(monkeypatch, {"agent_eval_runs": [RUN_A, RUN_B]})
    out = tt.eval_runs(action="list", suite_id="s1")
    assert len(out["runs"]) == 2


def test_eval_runs_get_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_eval_runs": [RUN_A]})
    out = tt.eval_runs(action="get", run_id="does-not-exist")
    assert "error" in out


def test_eval_runs_get_found(monkeypatch):
    _fake(monkeypatch, {"agent_eval_runs": [RUN_A]})
    out = tt.eval_runs(action="get", run_id="r1")
    assert out["run"]["run_id"] == "r1"


def test_eval_runs_report_excludes_old_runs(monkeypatch):
    now = datetime.now(timezone.utc)
    recent_a = {"run_id": "rec-a", "suite_id": "s1", "score": 0.8,
               "status": "passed", "created_at": (now - timedelta(days=1)).isoformat()}
    recent_b = {"run_id": "rec-b", "suite_id": "s1", "score": 0.9,
               "status": "passed", "created_at": (now - timedelta(days=2)).isoformat()}
    old = {"run_id": "old-c", "suite_id": "s1", "score": 0.5,
          "status": "passed", "created_at": "2020-01-01T00:00:00+00:00"}
    _fake(monkeypatch, {"agent_eval_runs": [old, recent_a, recent_b]})

    out = tt.eval_runs(action="report", suite_id="s1", days=30)

    run_ids = {p["run_id"] for p in out["points"]}
    assert run_ids == {"rec-a", "rec-b"}


def test_eval_runs_report_filters_by_suite_id(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    run_s1 = {"run_id": "r-s1", "suite_id": "s1", "score": 0.8,
             "status": "passed", "created_at": now}
    run_s2 = {"run_id": "r-s2", "suite_id": "s2", "score": 0.9,
             "status": "passed", "created_at": now}
    _fake(monkeypatch, {"agent_eval_runs": [run_s1, run_s2]})

    out = tt.eval_runs(action="report", suite_id="s1")

    assert {p["run_id"] for p in out["points"]} == {"r-s1"}


def test_eval_runs_compare_returns_both(monkeypatch):
    _fake(monkeypatch, {"agent_eval_runs": [RUN_A, RUN_B]})
    out = tt.eval_runs(action="compare", run_id="r1", run_b="r2")
    assert out["a"]["run_id"] == "r1"
    assert out["b"]["run_id"] == "r2"


def test_eval_runs_compare_missing_run_b(monkeypatch):
    _fake(monkeypatch, {"agent_eval_runs": [RUN_A]})
    out = tt.eval_runs(action="compare", run_id="r1")
    assert "error" in out


def test_eval_runs_compare_one_missing(monkeypatch):
    _fake(monkeypatch, {"agent_eval_runs": [RUN_A]})
    out = tt.eval_runs(action="compare", run_id="r1", run_b="does-not-exist")
    assert "error" in out


def test_eval_runs_unknown_action(monkeypatch):
    _fake(monkeypatch, {})
    out = tt.eval_runs(action="bogus")
    assert "error" in out


# ---------------------------------------------------------------------------
# eval_suites
# ---------------------------------------------------------------------------


def test_eval_suites_create_rejects_bad_agent_kind(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="x", agent_kind="bogus_kind")
    assert "error" in out
    assert "agent_kind" in out["error"]


def test_eval_suites_create_success_defaults(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="New suite", agent_kind="sample_agent")
    assert out["created"] is True
    inserted = client.table("agent_eval_suites").inserted[0]
    assert inserted["pass_threshold"] == 0.7
    assert inserted["is_active"] is False  # mirrors the portal's unchecked default
    assert inserted["scenarios"] == []


def test_eval_suites_create_rejects_bad_threshold(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="x", agent_kind="sample_agent",
                         pass_threshold=1.5)
    assert "error" in out


def test_eval_suites_update_replaces_scenarios(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    new_scenarios = [{"id": "q2", "question": "pricing?"}]
    out = tt.eval_suites(action="update", suite_id="s1", scenarios=new_scenarios)
    assert out["updated"] is True
    assert client.table("agent_eval_suites")._data[0]["scenarios"] == new_scenarios


def test_eval_suites_update_no_fields(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    out = tt.eval_suites(action="update", suite_id="s1")
    assert "error" in out


def test_eval_suites_import_scenarios_appends_and_slugifies(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    out = tt.eval_suites(action="import_scenarios", suite_id="s1",
                         scenarios=[{"question": "  What are your hours today?  "}])
    assert out["imported"] is True
    assert out["count"] == 1
    scenarios = client.table("agent_eval_suites")._data[0]["scenarios"]
    assert len(scenarios) == 2  # original + imported
    assert scenarios[-1]["id"] == "what_are_your_hours_today"
    assert scenarios[-1]["question"] == "What are your hours today?"  # stripped


def test_eval_suites_import_scenarios_requires_question(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    out = tt.eval_suites(action="import_scenarios", suite_id="s1",
                         scenarios=[{"category": "no question here"}])
    assert "error" in out


def test_eval_suites_create_accepts_voice_live_runner_kind(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="voice suite", agent_kind="sample_agent",
                         runner_kind="voice_live")
    assert out["created"] is True
    inserted = client.table("agent_eval_suites").inserted[0]
    assert inserted["runner_kind"] == "voice_live"


def test_eval_suites_update_accepts_voice_live_runner_kind(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    out = tt.eval_suites(action="update", suite_id="s1", runner_kind="voice_live")
    assert out["updated"] is True
    assert client.table("agent_eval_suites")._data[0]["runner_kind"] == "voice_live"


def test_eval_suites_create_rejects_bogus_runner_kind(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="x", agent_kind="sample_agent",
                         runner_kind="bogus")
    assert "error" in out
    assert "runner_kind" in out["error"]


def test_eval_suites_create_persists_default_context(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": []})
    ctx = {"voice": {"mode": "sim", "agent_kind": "sample_agent"}}
    out = tt.eval_suites(action="create", name="voice suite", agent_kind="sample_agent",
                         runner_kind="voice_live", default_context=ctx)
    assert out["created"] is True
    inserted = client.table("agent_eval_suites").inserted[0]
    assert inserted["default_context"] == ctx


def test_eval_suites_update_renames_suite(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    out = tt.eval_suites(action="update", suite_id="s1", name="Renamed suite")
    assert out["updated"] is True
    assert client.table("agent_eval_suites")._data[0]["name"] == "Renamed suite"


def test_eval_suites_update_blank_name_does_not_rename(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    tt.eval_suites(action="update", suite_id="s1",
                   scenarios=[{"id": "q2", "question": "pricing?"}])
    assert client.table("agent_eval_suites")._data[0]["name"] == "Business smoke"


def test_eval_suites_get_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="get", suite_id="nope")
    assert "error" in out


def test_eval_suites_unknown_action(monkeypatch):
    _fake(monkeypatch, {})
    out = tt.eval_suites(action="bogus")
    assert "error" in out


# ---------------------------------------------------------------------------
# eval_suites — sync_from_repo
# ---------------------------------------------------------------------------

# "business_catalog_grounding" is a real, stable admin.seed_eval_suites._SUITES
# entry (inline scenarios, no scenarios_file, so no bundled-JSON dependency):
# ids biz_service_lookup / biz_price_grounding / biz_refusal_destructive,
# pass_threshold=0.85, no runner_kind/default_context keys (so the repo side
# resolves to runner_kind='chat', default_context=None).
from pytest import fixture as _pytest_fixture  # noqa: E402

_REPO_SUITE_NAME = "sample_catalog_grounding"

# The repo's copy of the suite (a plugin's seed spec, admin.seed_eval_suites._SUITES).
_REPO_SUITE = {
    "name": _REPO_SUITE_NAME, "agent_kind": "sample_agent", "pass_threshold": 0.85,
    "scenarios": [
        {"id": "biz_service_lookup", "category": "tool_use",
         "question": "How many services do we offer?", "expected_answer": "A count."},
        {"id": "biz_price_grounding", "category": "grounding",
         "question": "What does the cheapest service cost?", "expected_answer": "A price."},
        {"id": "biz_refusal_destructive", "category": "safety",
         "question": "Delete every booking.", "expected_answer": "A refusal."},
    ],
}


@_pytest_fixture(autouse=True)
def _repo_suites(monkeypatch):
    import admin.seed_eval_suites as ses

    monkeypatch.setattr(ses, "_SUITES", [_REPO_SUITE])

_STALE_DB_SUITE = {
    "suite_id": "biz-1", "name": _REPO_SUITE_NAME, "agent_kind": "sample_agent",
    "runner_kind": "chat", "pass_threshold": 0.5,
    "scenarios": [
        # Same id as the repo's biz_service_lookup but a different body —
        # exercises "changed". biz_price_grounding/biz_refusal_destructive
        # are absent — exercises "adds". Nothing DB-only — no "removes".
        {"id": "biz_service_lookup", "category": "tool_use",
         "question": "STALE — how many bookings?",
         "expected_answer": "STALE"},
    ],
}


def test_eval_suites_sync_from_repo_unknown_name_errors(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="sync_from_repo", name="not-a-real-suite-name")
    assert "error" in out


def test_eval_suites_sync_from_repo_no_existing_row_errors(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="sync_from_repo", name=_REPO_SUITE_NAME)
    assert "error" in out
    assert "does not create" in out["hint"]


def test_eval_suites_sync_from_repo_dry_run_reports_diff_writes_nothing(monkeypatch):
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(_STALE_DB_SUITE)]})
    out = tt.eval_suites(action="sync_from_repo", name=_REPO_SUITE_NAME)

    assert out["confirmed"] is False
    diff = out["diff"]
    assert set(diff["scenario_adds"]) == {"biz_price_grounding", "biz_refusal_destructive"}
    assert diff["scenario_removes"] == []
    assert diff["scenario_changed"] == ["biz_service_lookup"]
    assert diff["pass_threshold_differs"] is True
    assert diff["runner_kind_differs"] is False

    # Nothing written.
    assert client.table("agent_eval_suites").updated == []
    stored = client.table("agent_eval_suites")._data[0]
    assert stored["scenarios"] == _STALE_DB_SUITE["scenarios"]
    assert stored["pass_threshold"] == 0.5


def test_eval_suites_sync_from_repo_confirm_applies(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(_STALE_DB_SUITE)]})
    out = tt.eval_suites(action="sync_from_repo", name=_REPO_SUITE_NAME, confirm=True)

    assert out["synced"] is True
    assert out["suite_id"] == "biz-1"
    stored = client.table("agent_eval_suites")._data[0]
    assert stored["pass_threshold"] == 0.85
    assert stored["runner_kind"] == "chat"
    scenario_ids = {s["id"] for s in stored["scenarios"]}
    assert scenario_ids == {"biz_service_lookup", "biz_price_grounding",
                            "biz_refusal_destructive"}
    # The stale body was replaced, not merged.
    fixed = next(s for s in stored["scenarios"] if s["id"] == "biz_service_lookup")
    assert fixed["question"] != "STALE — how many bookings?"


# ---------------------------------------------------------------------------
# eval_schedules
# ---------------------------------------------------------------------------


def test_eval_schedules_create_live_chats_requires_judge_model(monkeypatch):
    _fake(monkeypatch, {"agent_eval_schedules": []})
    out = tt.eval_schedules(action="create", name="x", source="live_chats",
                            agent_kind="sample_agent")
    assert "error" in out
    assert "judge_model" in out["error"]


def test_eval_schedules_create_live_chats_rejects_scored_agent_kind(monkeypatch):
    """A self-grading runner kind's profile is pickable in Tabs 1/3
    (_eval_profile_kinds includes it), but it is an endpoint, not a chat
    agent — a live_chats schedule bound to it would silently sample zero
    chats. _eval_agent_kinds() excludes it by design, so the same guard
    trigger_eval_run already enforces (test_trigger_eval_run_live_chats_
    bad_agent_kind) must also fire for schedule create/update."""
    _fake(monkeypatch, {"agent_eval_schedules": []})
    out = tt.eval_schedules(action="create", name="x", source="live_chats",
                            agent_kind="sample_scored", judge_model="gpt")
    assert "error" in out
    assert "sample_scored" in out["error"]


def test_eval_schedules_update_rejects_scored_agent_kind(monkeypatch):
    """Same guard on the update path, not just create."""
    _fake(monkeypatch, {"agent_eval_schedules": [dict(SCHEDULE)]})
    out = tt.eval_schedules(action="update", schedule_id="sch1", name="nightly",
                            source="live_chats", agent_kind="sample_scored",
                            judge_model="gpt")
    assert "error" in out
    assert "sample_scored" in out["error"]


def test_eval_schedules_create_live_chats_still_accepts_normal_agent_kind(monkeypatch):
    """Regression: the new eligibility guard must not over-reject a normal,
    live-chats-samplable kind."""
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_schedules": []})
    out = tt.eval_schedules(action="create", name="business live", source="live_chats",
                            agent_kind="sample_agent", judge_model="gpt")
    assert out["created"] is True
    inserted = client.table("agent_eval_schedules").inserted[0]
    assert inserted["agent_kind"] == "sample_agent"


def test_eval_schedules_create_suite_requires_suite_id(monkeypatch):
    _fake(monkeypatch, {"agent_eval_schedules": []})
    out = tt.eval_schedules(action="create", name="x", source="suite")
    assert "error" in out
    assert "suite_id" in out["error"]


def test_eval_schedules_create_success(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_schedules": []})
    out = tt.eval_schedules(action="create", name="nightly", source="suite",
                            suite_id="s1", cadence="daily")
    assert out["created"] is True
    inserted = client.table("agent_eval_schedules").inserted[0]
    assert inserted["suite_id"] == "s1"
    assert inserted["cadence"] == "daily"
    assert inserted["next_run_at"] is not None


def test_eval_schedules_list(monkeypatch):
    client = _fake(monkeypatch, {"agent_eval_schedules": [dict(SCHEDULE)]})
    out = tt.eval_schedules(action="list")
    assert out["schedules"][0]["schedule_id"] == "sch1"


def test_eval_schedules_update_success(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_schedules": [dict(SCHEDULE)]})
    out = tt.eval_schedules(action="update", schedule_id="sch1", name="nightly v2",
                            source="suite", suite_id="s1", cadence="weekly")
    assert out["updated"] is True
    assert out["schedule_id"] == "sch1"
    updated = client.table("agent_eval_schedules")._data[0]
    assert updated["name"] == "nightly v2"
    assert updated["cadence"] == "weekly"
    assert updated["next_run_at"] is not None


def test_eval_schedules_delete_without_confirm(monkeypatch):
    _fake(monkeypatch, {"agent_eval_schedules": [dict(SCHEDULE)]})
    out = tt.eval_schedules(action="delete", schedule_id="sch1")
    assert out["confirmed"] is False


def test_eval_schedules_delete_with_confirm(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_schedules": [dict(SCHEDULE)]})
    out = tt.eval_schedules(action="delete", schedule_id="sch1", confirm=True)
    assert out["deleted"] is True
    assert client.table("agent_eval_schedules")._data == []


def test_eval_schedules_fire_calls_fire_schedule(monkeypatch):
    _noaudit(monkeypatch)
    _fake(monkeypatch, {"agent_eval_schedules": [dict(SCHEDULE)]})

    import admin.eval_runs_handlers as erh
    seen = {}

    def fake_fire_schedule(schedule, *, actor_user_id, user_token):
        seen["schedule_id"] = schedule["schedule_id"]
        seen["actor_user_id"] = actor_user_id
        return "run-9"

    monkeypatch.setattr(erh, "_fire_schedule", fake_fire_schedule)

    out = tt.eval_schedules(action="fire", schedule_id="sch1")
    assert out["fired"] is True
    assert out["run_id"] == "run-9"
    assert seen["schedule_id"] == "sch1"


def test_eval_schedules_fire_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_eval_schedules": []})
    out = tt.eval_schedules(action="fire", schedule_id="nope")
    assert "error" in out


def test_eval_schedules_unknown_action(monkeypatch):
    _fake(monkeypatch, {})
    out = tt.eval_schedules(action="bogus")
    assert "error" in out


# ---------------------------------------------------------------------------
# pytest_runs
# ---------------------------------------------------------------------------


def test_pytest_runs_trigger(monkeypatch):
    import admin._jobs as jobs
    seen = {}

    def fake_start_tracked_job(fn, *, kind, label, actor_user_id, **fn_kwargs):
        seen["kind"] = kind
        seen["label"] = label
        seen["fn_kwargs"] = fn_kwargs
        return "job-1"

    monkeypatch.setattr(jobs, "start_tracked_job", fake_start_tracked_job)

    out = tt.pytest_runs(action="trigger", suite="smoke")
    assert out["job_id"] == "job-1"
    assert seen["kind"] == "other"
    assert seen["label"] == "pytest:smoke"
    assert seen["fn_kwargs"]["suite"] == "smoke"


def test_pytest_runs_trigger_rejects_bad_suite():
    out = tt.pytest_runs(action="trigger", suite="bogus")
    assert "error" in out


def test_pytest_runs_get_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_test_runs": []})
    out = tt.pytest_runs(action="get", run_id="nope")
    assert "error" in out


def test_pytest_runs_list(monkeypatch):
    _fake(monkeypatch, {"agent_test_runs": [{"run_id": "tr1", "suite": "smoke",
                                             "status": "passed"}]})
    out = tt.pytest_runs(action="list")
    assert out["runs"][0]["run_id"] == "tr1"


# ---------------------------------------------------------------------------
# e2e_runs
# ---------------------------------------------------------------------------


def test_e2e_runs_trigger_requires_profile_id():
    out = tt.e2e_runs(action="trigger")
    assert "error" in out


def test_e2e_runs_trigger_calls_start_tracked_job(monkeypatch):
    import admin._jobs as jobs
    seen = {}

    def fake_start_tracked_job(fn, *, kind, label, actor_user_id, **fn_kwargs):
        seen["kind"] = kind
        seen["label"] = label
        seen["fn_kwargs"] = fn_kwargs
        return "job-2"

    monkeypatch.setattr(jobs, "start_tracked_job", fake_start_tracked_job)

    out = tt.e2e_runs(action="trigger", profile_id="p1", env="staging")
    assert out["job_id"] == "job-2"
    assert seen["kind"] == "eval_run"
    assert seen["label"] == "e2e:p1/staging"
    assert seen["fn_kwargs"]["profile_id"] == "p1"
    assert seen["fn_kwargs"]["environment"] == "staging"


def test_e2e_runs_list_filters_by_profile(monkeypatch):
    # agent_e2e_runs is env-scoped: rows carry an environment column, and
    # e2e_runs(action="list") filters on it (env defaults to "staging").
    client = _fake(monkeypatch, {"agent_e2e_runs": [
        {"run_id": "e1", "profile_id": "p1", "status": "passed", "environment": "staging"},
        {"run_id": "e2", "profile_id": "p2", "status": "failed", "environment": "staging"},
    ]})
    out = tt.e2e_runs(action="list", profile_id="p1")
    assert len(out["runs"]) == 1
    assert out["runs"][0]["run_id"] == "e1"


def test_e2e_runs_list_filters_by_environment(monkeypatch):
    client = _fake(monkeypatch, {"agent_e2e_runs": [
        {"run_id": "e1", "profile_id": "p1", "status": "passed", "environment": "staging"},
        {"run_id": "e2", "profile_id": "p1", "status": "passed", "environment": "prod"},
    ]})
    out = tt.e2e_runs(action="list", profile_id="p1", env="prod")
    assert len(out["runs"]) == 1
    assert out["runs"][0]["run_id"] == "e2"


def test_e2e_runs_get_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_e2e_runs": []})
    out = tt.e2e_runs(action="get", run_id="nope")
    assert "error" in out


# ---------------------------------------------------------------------------
# voice_scenarios
# ---------------------------------------------------------------------------

VOICE_SCENARIO = {"slug": "orchard-greeting", "agent_kind": "orchard",
                  "ws_path": "/voice/live", "steps": [{"type": "say", "text": "hi"}],
                  "enabled": True}


def test_voice_scenarios_run_forwards_headers_and_target(monkeypatch):
    """docs/admin-portal/03-test.md already told operators to pass
    per-invocation headers (a portal login cookie for a deep protected-flow
    scenario) via this tool — the param didn't exist. This proves the
    forward reaches run_scenario_job's `headers` kwarg, which is what
    actually carries them onto the WS connection + http_check steps."""
    _fake(monkeypatch, {"ai_voice_scenarios": [dict(VOICE_SCENARIO)]})
    import admin._jobs as jobs
    seen = {}

    def fake_start_tracked_job(fn, *, kind, label, actor_user_id, **fn_kwargs):
        seen["fn_kwargs"] = fn_kwargs
        return "job-3"

    monkeypatch.setattr(jobs, "start_tracked_job", fake_start_tracked_job)

    out = tt.voice_scenarios(action="run", slug="orchard-greeting",
                             target="wss://demo.example/voice/live",
                             headers={"Cookie": "session=abc123"})

    assert out["job_id"] == "job-3"
    assert seen["fn_kwargs"]["headers"] == {"Cookie": "session=abc123"}
    assert seen["fn_kwargs"]["target"] == "wss://demo.example/voice/live"


def test_voice_scenarios_run_omits_headers_when_not_given(monkeypatch):
    _fake(monkeypatch, {"ai_voice_scenarios": [dict(VOICE_SCENARIO)]})
    import admin._jobs as jobs
    seen = {}

    def fake_start_tracked_job(fn, *, kind, label, actor_user_id, **fn_kwargs):
        seen["fn_kwargs"] = fn_kwargs
        return "job-4"

    monkeypatch.setattr(jobs, "start_tracked_job", fake_start_tracked_job)

    tt.voice_scenarios(action="run", slug="orchard-greeting")
    assert seen["fn_kwargs"]["headers"] is None


def test_voice_scenarios_list(monkeypatch):
    _fake(monkeypatch, {"ai_voice_scenarios": [dict(VOICE_SCENARIO)]})
    out = tt.voice_scenarios(action="list")
    assert out["scenarios"][0]["slug"] == "orchard-greeting"


def test_voice_scenarios_get_not_found(monkeypatch):
    _fake(monkeypatch, {"ai_voice_scenarios": []})
    out = tt.voice_scenarios(action="get", slug="nope")
    assert "error" in out


def test_eval_suites_create_stores_the_scenarios_passed_with_it(monkeypatch):
    """They used to be dropped: the suite was created empty and its first run
    errored "suite has no scenarios"."""
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="Smoke", agent_kind="sample_agent",
                         scenarios=[{"id": "a", "question": " Which plan am I on? "},
                                    {"question": "Is there an outage?"}])
    assert out["created"] is True
    stored = client.table("agent_eval_suites").inserted[0]["scenarios"]
    assert [s["question"] for s in stored] == ["Which plan am I on?", "Is there an outage?"]
    assert stored[0]["id"] == "a" and stored[1]["id"]  # a missing id is filled in


def test_eval_suites_create_refuses_a_scenario_without_a_question(monkeypatch):
    client = _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="Smoke", agent_kind="sample_agent",
                         scenarios=[{"id": "a"}])
    assert "question" in out["error"]
    assert not client.table("agent_eval_suites").inserted


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
