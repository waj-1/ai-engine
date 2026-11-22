from unittest.mock import patch, MagicMock

import pytest

from admin.services import bakeoffs


def _make_client(suite_rows, version_rows, bakeoff_insert_data=None):
    """Build a MagicMock Supabase client whose `.table(name)` returns a
    dedicated per-table mock (agent_bakeoffs / agent_eval_suites /
    agent_profile_versions), each pre-wired for the query shape
    `create_bakeoff` issues against it. Returns (client, bakeoff_table) so
    tests can assert on `bakeoff_table.insert` directly.
    """
    bakeoff_table = MagicMock()
    bakeoff_table.insert.return_value.execute.return_value.data = (
        bakeoff_insert_data if bakeoff_insert_data is not None else [{"bakeoff_id": "b1"}]
    )
    suites_table = MagicMock()
    suites_table.select.return_value.in_.return_value.execute.return_value.data = suite_rows
    versions_table = MagicMock()
    versions_table.select.return_value.in_.return_value.execute.return_value.data = version_rows

    tables = {
        "agent_bakeoffs": bakeoff_table,
        "agent_eval_suites": suites_table,
        "agent_profile_versions": versions_table,
    }
    client = MagicMock()
    client.table.side_effect = lambda name: tables[name]
    return client, bakeoff_table


def test_create_bakeoff_spawns_variant_times_suite_runs():
    client, _ = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": "jm"},
                    {"suite_id": "s2", "evaluator_model_id": "jm"}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "p1"}],
    )
    started = []

    def fake_start(**kw):
        started.append(kw)
        return f"run-{len(started)}"

    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run", side_effect=fake_start):
        out = bakeoffs.create_bakeoff(
            name="bo", profile_id="p1",
            variants=[{"label": "A", "profile_version_id": "v1"},
                      {"label": "B", "profile_version_id": "v2", "model_id": "m9"}],
            suite_ids=["s1", "s2"], repeats=1,
            actor_user_id="u1", user_token="jwt")
    assert out["bakeoff_id"] == "b1"
    assert len(out["run_ids"]) == 4  # 2 variants x 2 suites
    assert all(k["bakeoff_id"] == "b1" for k in started)
    assert {k["profile_version_id"] for k in started} == {"v1", "v2"}


def test_create_bakeoff_requires_at_least_two_variants():
    with pytest.raises(ValueError):
        bakeoffs.create_bakeoff(
            name="bo", profile_id="p1",
            variants=[{"label": "A", "profile_version_id": "v1"}],
            suite_ids=["s1"], repeats=1,
            actor_user_id="u1", user_token="jwt")


def test_create_bakeoff_requires_at_least_one_suite():
    with pytest.raises(ValueError):
        bakeoffs.create_bakeoff(
            name="bo", profile_id="p1",
            variants=[{"label": "A", "profile_version_id": "v1"},
                      {"label": "B", "profile_version_id": "v2"}],
            suite_ids=[], repeats=1,
            actor_user_id="u1", user_token="jwt")


def test_create_bakeoff_raises_pre_insert_when_suite_missing_evaluator_model():
    client, bakeoff_table = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": "jm"},
                    {"suite_id": "s2", "evaluator_model_id": None}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "p1"}],
    )
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run") as mock_start:
        with pytest.raises(ValueError, match="evaluator model"):
            bakeoffs.create_bakeoff(
                name="bo", profile_id="p1",
                variants=[{"label": "A", "profile_version_id": "v1"},
                          {"label": "B", "profile_version_id": "v2"}],
                suite_ids=["s1", "s2"], repeats=1,
                actor_user_id="u1", user_token="jwt")
    bakeoff_table.insert.assert_not_called()
    mock_start.assert_not_called()


def test_create_bakeoff_raises_pre_insert_when_variant_version_not_in_profile():
    client, bakeoff_table = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": "jm"}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "OTHER-PROFILE"}],
    )
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run") as mock_start:
        with pytest.raises(ValueError, match="profile"):
            bakeoffs.create_bakeoff(
                name="bo", profile_id="p1",
                variants=[{"label": "A", "profile_version_id": "v1"},
                          {"label": "B", "profile_version_id": "v2"}],
                suite_ids=["s1"], repeats=1,
                actor_user_id="u1", user_token="jwt")
    bakeoff_table.insert.assert_not_called()
    mock_start.assert_not_called()


def test_create_bakeoff_clamps_repeats_upper_bound():
    # Review finding M2: create_bakeoff must clamp repeats to <=20 itself —
    # start_suite_run has no upper bound of its own, so a raw MCP/POST call
    # with repeats=500 would otherwise fan out 500 x variants x suites runs.
    client, bakeoff_table = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": "jm"}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "p1"}],
    )
    started = []

    def fake_start(**kw):
        started.append(kw)
        return f"run-{len(started)}"

    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run", side_effect=fake_start):
        bakeoffs.create_bakeoff(
            name="bo", profile_id="p1",
            variants=[{"label": "A", "profile_version_id": "v1"},
                      {"label": "B", "profile_version_id": "v2"}],
            suite_ids=["s1"], repeats=500,
            actor_user_id="u1", user_token="jwt")

    # Persisted row is clamped.
    (payload,), _ = bakeoff_table.insert.call_args
    assert payload["repeats"] == 20

    # Every start_suite_run call received the clamped value, not 500.
    assert started
    assert all(kw["repeats"] == 20 for kw in started)


def test_create_bakeoff_raises_valueerror_for_none_name():
    # Robustness fix (3.1 review): name=None must raise a clean ValueError,
    # not an AttributeError out of name.strip() deep in the insert payload.
    with pytest.raises(ValueError, match="name is required"):
        bakeoffs.create_bakeoff(
            name=None, profile_id="p1",
            variants=[{"label": "A", "profile_version_id": "v1"},
                      {"label": "B", "profile_version_id": "v2"}],
            suite_ids=["s1"], repeats=1,
            actor_user_id="u1", user_token="jwt")


def test_create_bakeoff_dedupes_duplicate_variant_labels():
    # Review finding (3.3): build_variant_matrix groups by exact
    # variant_label, so two variants sharing a label would silently merge
    # their scenario data into one matrix row. create_bakeoff must dedupe
    # before persisting the variants jsonb and before spawning runs, so both
    # agree on the same normalized labels.
    client, bakeoff_table = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": "jm"}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "p1"}],
    )
    started = []

    def fake_start(**kw):
        started.append(kw)
        return f"run-{len(started)}"

    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run", side_effect=fake_start):
        bakeoffs.create_bakeoff(
            name="bo", profile_id="p1",
            variants=[{"label": "X", "profile_version_id": "v1"},
                      {"label": "X", "profile_version_id": "v2"}],
            suite_ids=["s1"], repeats=1,
            actor_user_id="u1", user_token="jwt")

    # Runs spawned with distinct labels.
    assert [kw["variant_label"] for kw in started] == ["X", "X (2)"]

    # Persisted variants jsonb carries the same deduped labels, in order.
    (payload,), _ = bakeoff_table.insert.call_args
    assert [v["label"] for v in payload["variants"]] == ["X", "X (2)"]


def test_create_bakeoff_normalizes_empty_variant_labels():
    client, bakeoff_table = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": "jm"}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "p1"}],
    )
    started = []

    def fake_start(**kw):
        started.append(kw)
        return f"run-{len(started)}"

    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run", side_effect=fake_start):
        bakeoffs.create_bakeoff(
            name="bo", profile_id="p1",
            variants=[{"label": "", "profile_version_id": "v1"},
                      {"label": "   ", "profile_version_id": "v2"}],
            suite_ids=["s1"], repeats=1,
            actor_user_id="u1", user_token="jwt")

    assert [kw["variant_label"] for kw in started] == ["variant 1", "variant 2"]

    (payload,), _ = bakeoff_table.insert.call_args
    assert [v["label"] for v in payload["variants"]] == ["variant 1", "variant 2"]


def test_create_bakeoff_marks_errored_and_reraises_on_mid_spawn_failure():
    # Robustness fix (3.1 review): a genuine failure partway through the
    # variant x suite spawn loop (2 variants x 2 suites = 4 spawns; this one
    # fails on the 2nd) must not leave the row stuck at 'running' forever —
    # it should flip to 'errored' AND the original exception must still
    # propagate (never swallowed).
    client, bakeoff_table = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": "jm"},
                    {"suite_id": "s2", "evaluator_model_id": "jm"}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "p1"}],
    )
    calls = []

    def fake_start(**kw):
        calls.append(kw)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return f"run-{len(calls)}"

    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run", side_effect=fake_start):
        with pytest.raises(RuntimeError, match="boom"):
            bakeoffs.create_bakeoff(
                name="bo", profile_id="p1",
                variants=[{"label": "A", "profile_version_id": "v1"},
                          {"label": "B", "profile_version_id": "v2"}],
                suite_ids=["s1", "s2"], repeats=1,
                actor_user_id="u1", user_token="jwt")

    # 2 spawns happened before the failure; the loop doesn't swallow it.
    assert len(calls) == 2
    bakeoff_table.update.assert_called_once()
    (payload,), _ = bakeoff_table.update.call_args
    assert payload["status"] == "errored"
    bakeoff_table.update.return_value.eq.assert_called_once_with("bakeoff_id", "b1")


# ---------------------------------------------------------------------------
# Judge-free runner kinds (self-scoring) are exempt from the evaluator
# pre-flight — fix/bakeoff-prod-usability.
# ---------------------------------------------------------------------------


def test_create_bakeoff_allows_self_scored_suite_without_evaluator():
    """A suite whose runner_kind grades itself (an item-id scorer) needs no
    evaluator model — the pre-flight must not block it."""
    client, bakeoff_table = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": None,
                     "runner_kind": "sample_scored"}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "p1"}],
    )
    started = []
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run",
               side_effect=lambda **kw: (started.append(kw) or f"r{len(started)}")):
        out = bakeoffs.create_bakeoff(
            name="rec", profile_id="p1",
            variants=[{"label": "A", "profile_version_id": "v1"},
                      {"label": "B", "profile_version_id": "v2"}],
            suite_ids=["s1"], repeats=1, actor_user_id="u1", user_token="jwt")
    assert out["bakeoff_id"] == "b1"
    assert bakeoff_table.insert.called
    assert len(started) == 2  # 2 variants x 1 suite


def test_create_bakeoff_still_requires_evaluator_for_chat_suite():
    """A judged (chat) suite with no evaluator is still rejected pre-insert."""
    client, bakeoff_table = _make_client(
        suite_rows=[{"suite_id": "s1", "evaluator_model_id": None,
                     "runner_kind": "chat"}],
        version_rows=[{"version_id": "v1", "profile_id": "p1"},
                      {"version_id": "v2", "profile_id": "p1"}],
    )
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client), \
         patch("admin.services.bakeoffs.start_suite_run"):
        with pytest.raises(ValueError, match="require a model judge"):
            bakeoffs.create_bakeoff(
                name="c", profile_id="p1",
                variants=[{"label": "A", "profile_version_id": "v1"},
                          {"label": "B", "profile_version_id": "v2"}],
                suite_ids=["s1"], repeats=1, actor_user_id="u1", user_token="jwt")
    assert not bakeoff_table.insert.called


@pytest.fixture(autouse=True)
def _sample_kinds(product_build):
    """Generic logic, fictional agent kinds (tests/conftest.py::product_build)."""
