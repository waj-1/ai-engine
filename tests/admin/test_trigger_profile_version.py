"""Task 2.3: Run-now gains a Profile-version picker.

- `build_eval_runs_context` assembles `profile_versions_by_profile` (map
  profile_id -> [{version_id, version_number, promoted_at}], newest first,
  capped) from ONE `agent_profile_versions` query scoped to the profiles
  already in the (scope-narrowed) list -- `.in_("profile_id", ids)` --
  grouped in Python, no N+1, no `snapshot` column in the select.
- `POST /admin/eval-runs/trigger` reads `profile_version_id` from the
  suite-mode form and forwards it to `start_suite_run`; omitted (None) when
  blank, so every existing caller stays back-compat.

Uses the TestClient + auth-override + scriptable-supabase pattern from
tests/admin/test_eval_runs.py._make_app.
"""
from __future__ import annotations

import pytest

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import eval_runs_handlers as h  # noqa: E402


# ---------------------------------------------------------------------------
# build_eval_runs_context: profile_versions_by_profile
# ---------------------------------------------------------------------------


def _fake_context_client(profile_rows, version_rows, captured):
    """Minimal scriptable client covering the tables build_eval_runs_context
    touches. Only agent_profiles / agent_profile_versions carry real rows;
    everything else returns empty so the context builds cleanly."""

    def table(name):
        q = MagicMock()
        q.select.return_value = q
        q.order.return_value = q
        q.limit.return_value = q
        q.eq.return_value = q
        q.gte.return_value = q
        if name == "agent_profiles":
            q.execute.return_value = MagicMock(data=profile_rows)
        elif name == "agent_profile_versions":
            def _in_(col, vals):
                captured["in_"] = (col, list(vals))
                return q
            q.in_.side_effect = _in_
            q.execute.return_value = MagicMock(data=version_rows)
        else:
            q.execute.return_value = MagicMock(data=[])
        return q

    client = MagicMock()
    client.table.side_effect = table
    return client


def test_profile_versions_by_profile_grouped_and_ordered():
    profiles = [
        {"profile_id": "p1", "display_name": "Sales", "agent_kind": "sample_other",
         "endpoint_path": "/sales/chat"},
        {"profile_id": "p2", "display_name": "Biz", "agent_kind": "sample_agent",
         "endpoint_path": "/business/chat"},
    ]
    # Deliberately out of per-profile order in the raw rows -- the query's
    # own ORDER BY version_number desc is what the code relies on, so hand
    # the fake rows back already sorted that way (a real Postgres query
    # would return them sorted; the fake doesn't re-sort by itself).
    version_rows = [
        {"version_id": "v1-3", "profile_id": "p1", "version_number": 3,
         "promoted_at": "2026-07-01T00:00:00Z"},
        {"version_id": "v2-2", "profile_id": "p2", "version_number": 2,
         "promoted_at": "2026-06-01T00:00:00Z"},
        {"version_id": "v1-2", "profile_id": "p1", "version_number": 2,
         "promoted_at": "2026-05-01T00:00:00Z"},
        {"version_id": "v1-1", "profile_id": "p1", "version_number": 1,
         "promoted_at": "2026-04-01T00:00:00Z"},
    ]
    captured: dict = {}
    fake = _fake_context_client(profiles, version_rows, captured)

    with patch.object(h, "get_service_client", return_value=fake), \
         patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]):
        ctx = h.build_eval_runs_context()

    by_profile = ctx["profile_versions_by_profile"]
    assert [v["version_number"] for v in by_profile["p1"]] == [3, 2, 1]
    assert [v["version_id"] for v in by_profile["p1"]] == ["v1-3", "v1-2", "v1-1"]
    assert [v["version_number"] for v in by_profile["p2"]] == [2]

    # ONE query scoped to exactly the profiles in play -- no N+1.
    assert captured["in_"][0] == "profile_id"
    assert set(captured["in_"][1]) == {"p1", "p2"}

    # Lean select: no snapshot column leaked into the picker payload.
    for versions in by_profile.values():
        for v in versions:
            assert set(v.keys()) == {"version_id", "version_number", "promoted_at"}


def test_profile_versions_by_profile_empty_when_no_profiles():
    captured: dict = {}
    fake = _fake_context_client([], [], captured)
    with patch.object(h, "get_service_client", return_value=fake), \
         patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]):
        ctx = h.build_eval_runs_context()
    assert ctx["profile_versions_by_profile"] == {}
    # No profiles -> the .in_ query must never fire.
    assert "in_" not in captured


# ---------------------------------------------------------------------------
# POST /admin/eval-runs/trigger forwards profile_version_id
# ---------------------------------------------------------------------------


def _make_trigger_app():
    from fastapi.testclient import TestClient
    from admin import auth
    from main import app

    def _fake_superuser():
        return {"user_token": "fake-jwt", "user_id": "u-1", "email": "t@waj.local"}

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [patch.object(h, "record_change")]
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


def test_trigger_forwards_profile_version_id():
    client, teardown = _make_trigger_app()
    try:
        with patch.object(h, "_start_suite_run", return_value="run-1") as mock_start:
            form = {
                "source": "suite", "profile_id": "p1", "suite_id": "s1",
                "profile_version_id": "v9",
            }
            r = client.post("/admin/eval-runs/trigger", data=form, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        assert mock_start.called
        kwargs = mock_start.call_args.kwargs
        assert kwargs["profile_version_id"] == "v9"
    finally:
        teardown()


def test_trigger_without_profile_version_id_is_back_compat():
    client, teardown = _make_trigger_app()
    try:
        with patch.object(h, "_start_suite_run", return_value="run-2") as mock_start:
            form = {"source": "suite", "profile_id": "p1", "suite_id": "s1"}
            r = client.post("/admin/eval-runs/trigger", data=form, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        assert mock_start.called
        kwargs = mock_start.call_args.kwargs
        assert kwargs["profile_version_id"] is None
    finally:
        teardown()


def test_trigger_surfaces_profile_version_value_error_as_400():
    """The Task 1.5 guard (`start_suite_run` refuses a version-pinned run
    with no evaluator model) must surface exactly like any other trigger
    validation error -- HTTPException(400, str(exc)), not a 500."""
    client, teardown = _make_trigger_app()
    try:
        with patch.object(
            h, "_start_suite_run",
            side_effect=ValueError("comparison runs require a model judge — set an evaluator model on the suite"),
        ):
            form = {
                "source": "suite", "profile_id": "p1", "suite_id": "s1",
                "profile_version_id": "v9",
            }
            r = client.post("/admin/eval-runs/trigger", data=form, follow_redirects=False)
        assert r.status_code == 400
        assert "evaluator model" in r.text.lower()
    finally:
        teardown()


@pytest.fixture(autouse=True)
def _sample_kinds(product_build):
    """Generic logic, fictional agent kinds (tests/conftest.py::product_build)."""
