"""Route tests for GET /admin/evals/bakeoffs/{id}/data.json (Task 3.4) — the
JSON feed evals-bakeoff-chart.js fetches/polls for the grouped-bar + cost-vs-
quality charts on the bake-off detail page.

Uses the session-scoped `client` fixture (tests/admin/conftest.py: TestClient
+ require_superuser bypass + null Supabase client) — there is no
`admin_client` fixture in this repo (see tests/admin/test_bakeoffs_routes.py
for the same convention).
"""
from __future__ import annotations

from unittest.mock import patch



# Bake-off ids are uuids; the routes 404 a malformed one before any DB call.
BO = "0b0b0b0b-0000-4000-8000-0000000000b1"

def test_bakeoff_data_json(client):
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        return_value={
            "bakeoff": {"bakeoff_id": "b1"},
            "matrix": {"by_variant": {"A": {"mean_score": 1.0}}},
            "status": "complete",
        },
    ):
        r = client.get(f"/admin/evals/bakeoffs/{BO}/data.json")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "complete"
    assert body["matrix"]["by_variant"]["A"]["mean_score"] == 1.0
    # Only {status, matrix} — no bakeoff row leaked into the JSON payload.
    assert set(body.keys()) == {"status", "matrix"}


def test_bakeoff_data_json_passes_suite_id_as_suite_focus(client):
    """?suite_id= is forwarded to get_bakeoff as suite_focus= (not a
    positional / differently-named kwarg) — this is what the chart JS relies
    on to narrow the matrix to one suite's rows."""
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        return_value={
            "bakeoff": {"bakeoff_id": "b1"},
            "matrix": {"rows": []},
            "status": "running",
        },
    ) as mock_get:
        r = client.get(f"/admin/evals/bakeoffs/{BO}/data.json?suite_id=s1")
    assert r.status_code == 200
    assert mock_get.call_args.kwargs.get("suite_focus") == "s1"
    assert mock_get.call_args.args[0] == BO


def test_bakeoff_data_json_no_suite_id_passes_none(client):
    """Blank/absent ?suite_id= must reach get_bakeoff as suite_focus=None,
    not the empty string — get_bakeoff's `if suite_focus:` guard treats ''
    and None the same, but the MCP/route contract is explicitly None."""
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        return_value={"bakeoff": {}, "matrix": {"by_variant": {}}, "status": "complete"},
    ) as mock_get:
        r = client.get(f"/admin/evals/bakeoffs/{BO}/data.json")
    assert r.status_code == 200
    assert mock_get.call_args.kwargs.get("suite_focus") is None


def test_bakeoff_data_json_unknown_id_is_404_not_500(client):
    """get_bakeoff raises ValueError for a missing row (see
    admin/services/bakeoffs.py) — the route must turn that into a clean 404,
    matching how /eval-runs/{run_id}/status.json handles a missing run."""
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        side_effect=ValueError("bake-off nope not found"),
    ):
        r = client.get("/admin/evals/bakeoffs/nope/data.json")
    assert r.status_code == 404


def test_bakeoff_data_json_running_status(client):
    """A still-running bake-off reports status='running' verbatim so the
    poller keeps going."""
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        return_value={"bakeoff": {}, "matrix": {"rows": [], "by_variant": {}}, "status": "running"},
    ):
        r = client.get(f"/admin/evals/bakeoffs/{BO}/data.json")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
