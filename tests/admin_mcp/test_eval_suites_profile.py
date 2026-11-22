"""Task 4.1 — MCP parity for eval_suites (profile_id/runner_kind binding),
trigger_eval_run (profile_version_id), eval_runs (new linkage fields), and
the eval_schedules live_chats profile binding + expectation defaulting
carried over from Task 2.4's portal review. Same FakeClient pattern as
test_tools_test.py; `admin_mcp.tool()` returns the plain function in this
repo's FastMCP version (verified: no `.fn` unwrap needed), so tools are
called directly, matching every other admin_mcp test module."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import admin_mcp.tools_test as tt
from tests.admin_mcp.conftest import FakeClient, FakeTable

SUITE = {"suite_id": "s1", "name": "Business smoke", "agent_kind": "sample_agent",
         "scenarios": [{"id": "q1", "question": "hours?"}],
         "pass_threshold": 0.7, "is_active": True}

RUN_A = {"run_id": "r1", "suite_id": "s1", "profile_id": "p1",
         "status": "passed", "score": 0.9, "context": {}, "results": []}

SCHEDULE = {"schedule_id": "sch1", "name": "nightly", "suite_id": "s1",
            "profile_id": "p1", "agent_kind": "sample_agent", "source": "suite",
            "cadence": "daily", "coverage": {}, "is_enabled": True}


def _fake(monkeypatch, tables):
    client = FakeClient(tables)
    monkeypatch.setattr(tt, "client_for_env", lambda env: client)
    return client


def _noaudit(monkeypatch):
    monkeypatch.setattr(tt, "actor_audit", lambda *a, **k: None)


class _SpyTable(FakeTable):
    """FakeTable that also records every .select(...) call's raw args, so a
    test can assert on the select STRING itself (FakeTable ignores select
    for filtering — it always returns full rows — so asserting on output
    alone can't prove a select list was actually widened)."""

    def __init__(self, data):
        super().__init__(data)
        self.select_calls: list = []

    def select(self, *a, **k):
        self.select_calls.append(a)
        return super().select(*a, **k)


class _SpyClient:
    def __init__(self, tables):
        self.tables = {name: _SpyTable(data) for name, data in tables.items()}

    def table(self, name):
        return self.tables.setdefault(name, _SpyTable([]))


# ---------------------------------------------------------------------------
# eval_suites — profile_id binding + runner_kind
# ---------------------------------------------------------------------------


def test_eval_suites_create_syncs_agent_kind_from_profile(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(tt, "client_for_env", lambda env: client)
    monkeypatch.setattr(tt, "actor_audit", lambda *a, **k: None)
    monkeypatch.setattr(tt, "rows", lambda resp: [{"suite_id": "s1"}])

    with patch("admin.eval_handlers._sync_agent_kind", return_value="sample_other") as sync:
        out = tt.eval_suites(action="create", name="n", profile_id="p1", env="staging")

    sync.assert_called_once_with(client, "p1")
    assert out.get("created") is True
    assert out["suite"]["suite_id"] == "s1"


def test_eval_suites_create_unknown_profile_id_errors(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": [], "agent_profiles": []})
    out = tt.eval_suites(action="create", name="n", profile_id="does-not-exist")
    assert "error" in out
    assert "profile_id" in out["error"]


def test_eval_suites_create_profile_id_sets_payload_fields(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_eval_suites": [],
        "agent_profiles": [{"profile_id": "p1", "agent_kind": "sample_other"}],
    })
    out = tt.eval_suites(action="create", name="n", profile_id="p1")
    assert out["created"] is True
    inserted = client.table("agent_eval_suites").inserted[0]
    assert inserted["profile_id"] == "p1"
    assert inserted["agent_kind"] == "sample_other"


def test_eval_suites_create_without_profile_id_still_requires_agent_kind(monkeypatch):
    # Back-compat: the legacy (no-profile) create path is unchanged.
    _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="n")
    assert "error" in out
    assert "agent_kind" in out["error"]


def test_eval_suites_create_runner_kind_defaults_chat(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="n", agent_kind="sample_agent")
    assert out["created"] is True
    inserted = client.table("agent_eval_suites").inserted[0]
    assert inserted["runner_kind"] == "chat"


def test_eval_suites_create_runner_kind_scored(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="n", agent_kind="sample_agent",
                         runner_kind="sample_scored")
    assert out["created"] is True
    inserted = client.table("agent_eval_suites").inserted[0]
    assert inserted["runner_kind"] == "sample_scored"


def test_eval_suites_create_runner_kind_turns(monkeypatch):
    # The portal accepted whole-chat runner suites; the MCP kept its own list and refused.
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="n", agent_kind="sample_other",
                         runner_kind="sample_turns")
    assert out["created"] is True
    assert client.table("agent_eval_suites").inserted[0]["runner_kind"] == "sample_turns"


def test_eval_suites_update_runner_kind_turns(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    out = tt.eval_suites(action="update", suite_id="s1", runner_kind="sample_turns")
    assert out["updated"] is True
    assert client.table("agent_eval_suites")._data[0]["runner_kind"] == "sample_turns"


def test_mcp_runner_kinds_match_the_portal():
    # The tool's typed parameter is a separate list (FastMCP needs a Literal);
    # it must offer exactly the kinds the portal and the runner accept.
    # Both are built from admin/services/eval_runners.runner_kinds().
    from typing import get_args, get_type_hints
    tool = getattr(tt.eval_suites, "__wrapped__", tt.eval_suites)
    hint = get_type_hints(tool)["runner_kind"]
    assert hint is tt.RunnerKind
    assert get_args(hint)[:3] == ("", "chat", "voice_live")


def test_eval_suites_create_runner_kind_invalid_errors(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": []})
    out = tt.eval_suites(action="create", name="n", agent_kind="sample_agent",
                         runner_kind="bogus")
    assert "error" in out
    assert "runner_kind" in out["error"]


def test_eval_suites_update_profile_id_syncs_agent_kind(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_eval_suites": [dict(SUITE)],
        "agent_profiles": [{"profile_id": "p1", "agent_kind": "sample_other"}],
    })
    out = tt.eval_suites(action="update", suite_id="s1", profile_id="p1")
    assert out["updated"] is True
    updated = client.table("agent_eval_suites")._data[0]
    assert updated["profile_id"] == "p1"
    assert updated["agent_kind"] == "sample_other"


def test_eval_suites_update_unknown_profile_id_errors(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)], "agent_profiles": []})
    out = tt.eval_suites(action="update", suite_id="s1", profile_id="does-not-exist")
    assert "error" in out
    assert "profile_id" in out["error"]


def test_eval_suites_update_clear_profile_clears_profile_id_leaves_agent_kind(monkeypatch):
    # Review finding M3: the portal's edit form can unbind a suite's profile
    # (Profile <select> -> "— none —") by submitting profile_id="" while
    # still touching the payload; MCP had no way to express that at all
    # (profile_id="" was indistinguishable from "not supplied"). clear_profile
    # is the MCP-side sentinel for the same unbind — profile_id -> NULL,
    # agent_kind left exactly as it was (suite goes back to manually-managed).
    _noaudit(monkeypatch)
    suite = dict(SUITE)
    suite["profile_id"] = "p1"
    suite["agent_kind"] = "sample_other"
    client = _fake(monkeypatch, {"agent_eval_suites": [suite]})

    out = tt.eval_suites(action="update", suite_id="s1", clear_profile=True)

    assert out["updated"] is True
    updated = client.table("agent_eval_suites")._data[0]
    assert updated["profile_id"] is None
    assert updated["agent_kind"] == "sample_other"  # untouched


def test_eval_suites_update_profile_id_wins_over_clear_profile(monkeypatch):
    # A non-empty profile_id always takes priority — clear_profile only
    # fires on the "both empty" branch.
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_eval_suites": [dict(SUITE)],
        "agent_profiles": [{"profile_id": "p2", "agent_kind": "support_bot"}],
    })

    out = tt.eval_suites(action="update", suite_id="s1", profile_id="p2",
                         clear_profile=True)

    assert out["updated"] is True
    updated = client.table("agent_eval_suites")._data[0]
    assert updated["profile_id"] == "p2"
    assert updated["agent_kind"] == "support_bot"


def test_eval_suites_update_neither_profile_id_nor_clear_profile_leaves_untouched(monkeypatch):
    # Back-compat: omitting both leaves profile_id out of the payload
    # entirely (existing binding, if any, survives unchanged).
    _noaudit(monkeypatch)
    suite = dict(SUITE)
    suite["profile_id"] = "p1"
    client = _fake(monkeypatch, {"agent_eval_suites": [suite]})

    out = tt.eval_suites(action="update", suite_id="s1", runner_kind="sample_scored")

    assert out["updated"] is True
    updated = client.table("agent_eval_suites")._data[0]
    assert updated["profile_id"] == "p1"


def test_eval_suites_update_runner_kind_invalid_errors(monkeypatch):
    _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    out = tt.eval_suites(action="update", suite_id="s1", runner_kind="bogus")
    assert "error" in out
    assert "runner_kind" in out["error"]


def test_eval_suites_update_runner_kind_success(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_suites": [dict(SUITE)]})
    out = tt.eval_suites(action="update", suite_id="s1", runner_kind="sample_scored")
    assert out["updated"] is True
    updated = client.table("agent_eval_suites")._data[0]
    assert updated["runner_kind"] == "sample_scored"


# ---------------------------------------------------------------------------
# trigger_eval_run — profile_version_id
# ---------------------------------------------------------------------------


def test_trigger_eval_run_forwards_profile_version_id(monkeypatch):
    monkeypatch.setattr(tt, "actor_audit", lambda *a, **k: None)
    import admin.services.evals as evals
    seen = {}

    def fake_start_suite_run(**kw):
        seen.update(kw)
        return "r1"

    monkeypatch.setattr(evals, "start_suite_run", fake_start_suite_run)

    out = tt.trigger_eval_run(profile_id="p1", source="suite", suite_id="s1",
                              profile_version_id="v1")

    assert out["run_id"] == "r1"
    assert seen["profile_version_id"] == "v1"


def test_trigger_eval_run_without_profile_version_id_passes_none(monkeypatch):
    monkeypatch.setattr(tt, "actor_audit", lambda *a, **k: None)
    import admin.services.evals as evals
    seen = {}

    def fake_start_suite_run(**kw):
        seen.update(kw)
        return "r1"

    monkeypatch.setattr(evals, "start_suite_run", fake_start_suite_run)

    tt.trigger_eval_run(profile_id="p1", source="suite", suite_id="s1")

    assert seen["profile_version_id"] is None


def test_trigger_eval_run_value_error_from_start_suite_run_becomes_err(monkeypatch):
    import admin.services.evals as evals

    def fake_start_suite_run(**kw):
        raise ValueError("profile version does not belong to the selected profile")

    monkeypatch.setattr(evals, "start_suite_run", fake_start_suite_run)

    out = tt.trigger_eval_run(profile_id="p1", source="suite", suite_id="s1",
                              profile_version_id="v-wrong")

    assert "error" in out
    assert "profile version" in out["error"]


# ---------------------------------------------------------------------------
# eval_runs — widened select / returned fields
# ---------------------------------------------------------------------------


def test_eval_runs_list_select_includes_new_columns(monkeypatch):
    client = _SpyClient({"agent_eval_runs": [dict(RUN_A)]})
    monkeypatch.setattr(tt, "client_for_env", lambda env: client)

    tt.eval_runs(action="list")

    select_str = client.table("agent_eval_runs").select_calls[0][0]
    for col in ("profile_id", "schedule_id", "bakeoff_id", "profile_version_id"):
        assert col in select_str


def test_eval_runs_list_output_includes_new_fields(monkeypatch):
    row = dict(RUN_A)
    row.update({"schedule_id": "sch1", "bakeoff_id": "bo1", "profile_version_id": "v1"})
    _fake(monkeypatch, {"agent_eval_runs": [row]})

    out = tt.eval_runs(action="list")

    assert out["runs"][0]["schedule_id"] == "sch1"
    assert out["runs"][0]["bakeoff_id"] == "bo1"
    assert out["runs"][0]["profile_version_id"] == "v1"


def test_eval_runs_get_returns_new_fields(monkeypatch):
    row = dict(RUN_A)
    row.update({"schedule_id": "sch1", "bakeoff_id": "bo1", "profile_version_id": "v1"})
    _fake(monkeypatch, {"agent_eval_runs": [row]})

    out = tt.eval_runs(action="get", run_id="r1")

    assert out["run"]["schedule_id"] == "sch1"
    assert out["run"]["bakeoff_id"] == "bo1"
    assert out["run"]["profile_version_id"] == "v1"


# ---------------------------------------------------------------------------
# eval_schedules — live_chats profile_id binding + expectation defaulting
# (carried from Task 2.4 review: MCP now mirrors the portal handler)
# ---------------------------------------------------------------------------


def test_eval_schedules_live_chats_profile_id_derives_kind_and_defaults_expectation(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_eval_schedules": [],
        "agent_profiles": [{"profile_id": "p1", "agent_kind": "sample_other"}],
    })

    out = tt.eval_schedules(action="create", name="nightly-live", source="live_chats",
                            profile_id="p1", judge_model="gpt-4", cadence="daily")

    assert out["created"] is True
    inserted = client.table("agent_eval_schedules").inserted[0]
    assert inserted["agent_kind"] == "sample_other"

    from admin.eval_runs_handlers import default_expectation_for
    assert inserted["coverage"]["expectations"] == default_expectation_for("sample_other")
    assert inserted["coverage"]["expectations"] != ""


def test_eval_schedules_live_chats_stale_profile_id_falls_back_to_form_agent_kind(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_eval_schedules": [],
        "agent_profiles": [],
    })

    out = tt.eval_schedules(action="create", name="x", source="live_chats",
                            profile_id="stale-profile", agent_kind="sample_agent",
                            judge_model="gpt-4")

    assert out["created"] is True
    inserted = client.table("agent_eval_schedules").inserted[0]
    assert inserted["agent_kind"] == "sample_agent"


def test_eval_schedules_live_chats_explicit_expectations_kept(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_eval_schedules": []})

    out = tt.eval_schedules(action="create", name="x", source="live_chats",
                            agent_kind="sample_agent", judge_model="gpt-4",
                            expectations="custom rubric")

    assert out["created"] is True
    inserted = client.table("agent_eval_schedules").inserted[0]
    assert inserted["coverage"]["expectations"] == "custom rubric"


def test_eval_schedules_live_chats_still_requires_judge_model(monkeypatch):
    _fake(monkeypatch, {"agent_eval_schedules": [], "agent_profiles": []})
    out = tt.eval_schedules(action="create", name="x", source="live_chats",
                            agent_kind="sample_agent")
    assert "error" in out
    assert "judge_model" in out["error"]


def test_eval_schedules_update_profile_id_derives_agent_kind(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {
        "agent_eval_schedules": [dict(SCHEDULE)],
        "agent_profiles": [{"profile_id": "p1", "agent_kind": "support_bot"}],
    })

    out = tt.eval_schedules(action="update", schedule_id="sch1", name="nightly",
                            source="suite", suite_id="s1", cadence="daily",
                            profile_id="p1")

    assert out["updated"] is True
    updated = client.table("agent_eval_schedules")._data[0]
    assert updated["agent_kind"] == "support_bot"


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
