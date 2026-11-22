from unittest.mock import patch, MagicMock

import pytest

from admin.services import bakeoffs


def _runs():
    return [
        {"run_id": "r1", "suite_id": "s1", "status": "passed", "score": 1.0,
         "context": {"variant_label": "A"}, "results": [{"score": 1.0, "passed": True}]},
        {"run_id": "r2", "suite_id": "s1", "status": "failed", "score": 0.0,
         "context": {"variant_label": "B"}, "results": [{"score": 0.0, "passed": False}]},
    ]


def test_get_bakeoff_status_complete_when_terminal():
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"bakeoff_id": "b1", "name": "bo", "profile_id": "p1", "variants": [],
         "suite_ids": ["s1"], "status": "running", "repeats": 1}]
    client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = _runs()
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.get_bakeoff("b1")
    assert out["status"] == "complete"
    assert "by_variant" in out["matrix"]


# ---------------------------------------------------------------------------
# Per-table dispatcher mirroring test_bakeoffs_service.py's _make_client, so
# get_bakeoff/list_bakeoffs' multi-table queries (agent_bakeoffs,
# agent_eval_runs, agent_profiles) don't collide on a single shared mock.

def _make_read_client(*, bakeoff_row=None, bakeoff_rows=None, runs=None, profile_rows=None):
    bakeoffs_table = MagicMock()
    bakeoffs_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [bakeoff_row] if bakeoff_row is not None else []
    )
    bakeoffs_table.select.return_value.order.return_value.limit.return_value.execute.return_value.data = (
        bakeoff_rows if bakeoff_rows is not None else []
    )

    runs_table = MagicMock()
    runs_table.select.return_value.eq.return_value.execute.return_value.data = runs or []

    profiles_table = MagicMock()
    profiles_table.select.return_value.in_.return_value.execute.return_value.data = profile_rows or []

    tables = {
        "agent_bakeoffs": bakeoffs_table,
        "agent_eval_runs": runs_table,
        "agent_profiles": profiles_table,
    }
    client = MagicMock()
    client.table.side_effect = lambda name: tables[name]
    return client, bakeoffs_table, runs_table, profiles_table


def _bakeoff_row(**overrides):
    row = {"bakeoff_id": "b1", "name": "bo", "profile_id": "p1", "variants": [],
           "suite_ids": ["s1", "s2"], "status": "running", "repeats": 1}
    row.update(overrides)
    return row


def test_get_bakeoff_running_when_any_run_nonterminal():
    runs = _runs() + [{"run_id": "r3", "suite_id": "s1", "status": "running",
                        "context": {"variant_label": "A"}, "results": []}]
    client, bakeoffs_table, _, _ = _make_read_client(
        bakeoff_row=_bakeoff_row(), runs=runs)
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.get_bakeoff("b1")
    assert out["status"] == "running"
    assert out["bakeoff"]["status"] == "running"
    bakeoffs_table.update.assert_not_called()


def test_get_bakeoff_errored_status_sticky_even_with_terminal_runs():
    # A bake-off already marked 'errored' (create_bakeoff's mid-spawn guard)
    # must NOT flip back to 'complete' just because its already-spawned runs
    # all finished naturally — the error signal must survive the recompute.
    client, bakeoffs_table, _, _ = _make_read_client(
        bakeoff_row=_bakeoff_row(status="errored"), runs=_runs())
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.get_bakeoff("b1")
    assert out["status"] == "errored"
    assert out["bakeoff"]["status"] == "errored"
    bakeoffs_table.update.assert_not_called()


def test_get_bakeoff_zero_runs_keeps_stored_status():
    client, bakeoffs_table, _, _ = _make_read_client(
        bakeoff_row=_bakeoff_row(status="errored"), runs=[])
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.get_bakeoff("b1")
    assert out["status"] == "errored"
    bakeoffs_table.update.assert_not_called()


def test_get_bakeoff_persists_complete_status_when_changed():
    client, bakeoffs_table, _, _ = _make_read_client(
        bakeoff_row=_bakeoff_row(status="running"), runs=_runs())
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.get_bakeoff("b1")
    assert out["status"] == "complete"
    bakeoffs_table.update.assert_called_once()
    (payload,), _ = bakeoffs_table.update.call_args
    assert payload["status"] == "complete"
    bakeoffs_table.update.return_value.eq.assert_called_once_with("bakeoff_id", "b1")


def test_get_bakeoff_no_write_when_already_complete():
    # Already 'complete' and stays 'complete' -> nothing changed, no write.
    client, bakeoffs_table, _, _ = _make_read_client(
        bakeoff_row=_bakeoff_row(status="complete"), runs=_runs())
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.get_bakeoff("b1")
    assert out["status"] == "complete"
    bakeoffs_table.update.assert_not_called()


def test_get_bakeoff_suite_focus_filters_rows_and_omits_by_variant():
    runs = [
        {"run_id": "r1", "suite_id": "s1", "status": "passed",
         "context": {"variant_label": "A"}, "results": [{"score": 1.0, "passed": True}]},
        {"run_id": "r2", "suite_id": "s2", "status": "passed",
         "context": {"variant_label": "A"}, "results": [{"score": 1.0, "passed": True}]},
    ]
    client, _, _, _ = _make_read_client(bakeoff_row=_bakeoff_row(), runs=runs)
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.get_bakeoff("b1", suite_focus="s1")
    assert "by_variant" not in out["matrix"]
    assert [r["suite_id"] for r in out["matrix"]["rows"]] == ["s1"]


def test_get_bakeoff_not_found_raises():
    client, _, _, _ = _make_read_client(bakeoff_row=None, runs=[])
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        with pytest.raises(ValueError):
            bakeoffs.get_bakeoff("missing")


def test_list_bakeoffs_decorates_counts_and_profile_name_without_n_plus_1():
    bakeoff_rows = [
        {"bakeoff_id": "b2", "name": "second", "profile_id": "p1",
         "variants": [{"label": "A"}, {"label": "B"}], "suite_ids": ["s1"],
         "status": "complete", "repeats": 1, "created_at": "2026-07-23T00:00:00Z"},
        {"bakeoff_id": "b1", "name": "first", "profile_id": "p2",
         "variants": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
         "suite_ids": ["s1", "s2"], "status": "running", "repeats": 1,
         "created_at": "2026-07-22T00:00:00Z"},
    ]
    profile_rows = [
        {"profile_id": "p1", "display_name": "Sales Agent"},
        {"profile_id": "p2", "display_name": "Support Agent"},
    ]
    client, bakeoffs_table, _, profiles_table = _make_read_client(
        bakeoff_rows=bakeoff_rows, profile_rows=profile_rows)
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.list_bakeoffs(limit=25)

    # Order preserved as returned by the (already newest-first) query.
    assert [r["bakeoff_id"] for r in out] == ["b2", "b1"]
    assert out[0]["n_variants"] == 2
    assert out[0]["n_suites"] == 1
    assert out[0]["_profile_name"] == "Sales Agent"
    assert out[1]["n_variants"] == 3
    assert out[1]["n_suites"] == 2
    assert out[1]["_profile_name"] == "Support Agent"

    # One batched profile lookup for the whole page, not one per row.
    profiles_table.select.return_value.in_.assert_called_once()
    call_column, call_ids = profiles_table.select.return_value.in_.call_args.args
    assert call_column == "profile_id"
    assert set(call_ids) == {"p1", "p2"}

    bakeoffs_table.select.return_value.order.assert_called_once_with("created_at", desc=True)
    bakeoffs_table.select.return_value.order.return_value.limit.assert_called_once_with(25)


def test_list_bakeoffs_empty_skips_profile_lookup():
    client, _, _, profiles_table = _make_read_client(bakeoff_rows=[])
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        out = bakeoffs.list_bakeoffs()
    assert out == []
    profiles_table.select.assert_not_called()
