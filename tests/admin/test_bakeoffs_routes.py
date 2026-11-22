"""Route tests for Tab 3 of /admin/evals — bake-off list/create wizard +
detail (Task 3.3).

Uses the session-scoped `client` fixture (tests/admin/conftest.py:
TestClient + require_superuser bypass + null Supabase client) — there is no
`admin_client` fixture in this repo.
"""
from __future__ import annotations

import pytest

from unittest.mock import MagicMock, patch

from admin.eval_runs_handlers import build_bakeoffs_context



# Bake-off ids are uuids; the routes 404 a malformed one before any DB call.
BO = "0b0b0b0b-0000-4000-8000-0000000000b1"

def test_bakeoff_create_redirects(client):
    """A successful create redirects (302/303) to the new bake-off's detail
    page — the wizard's parallel variant_label/variant_profile_version_id/
    variant_model_id arrays parse into 2 variants, suite_ids via getlist."""
    with patch(
        "admin.services.bakeoffs.create_bakeoff",
        return_value={"bakeoff_id": "b1", "run_ids": ["r1", "r2"]},
    ) as mock_create:
        r = client.post(
            "/admin/evals/bakeoffs",
            data={
                "name": "bo",
                "profile_id": "p1",
                "variant_label": ["A", "B"],
                "variant_profile_version_id": ["v1", "v2"],
                "variant_model_id": ["", ""],
                "suite_ids": ["s1"],
                "repeats": "1",
            },
            follow_redirects=False,
        )
    assert r.status_code in (302, 303)
    assert "/admin/evals/bakeoffs/b1" in r.headers["location"]

    assert mock_create.call_count == 1
    kwargs = mock_create.call_args.kwargs
    assert kwargs["name"] == "bo"
    assert kwargs["profile_id"] == "p1"
    assert kwargs["suite_ids"] == ["s1"]
    assert kwargs["repeats"] == 1
    assert [v["label"] for v in kwargs["variants"]] == ["A", "B"]
    assert kwargs["variants"][0]["profile_version_id"] == "v1"
    assert kwargs["variants"][1]["profile_version_id"] == "v2"
    # Empty model_id values are dropped, not persisted as "".
    assert "model_id" not in kwargs["variants"][0]
    assert "model_id" not in kwargs["variants"][1]


def test_bakeoff_create_parses_gen_config_per_variant(client):
    """Gen-config fields are parallel arrays too; only provided keys land in
    each variant's gen_config, and an all-empty gen-config row omits the key
    entirely (Task 4.2's MCP tool mirrors this exact parsing contract)."""
    with patch(
        "admin.services.bakeoffs.create_bakeoff",
        return_value={"bakeoff_id": "b2", "run_ids": []},
    ) as mock_create:
        client.post(
            "/admin/evals/bakeoffs",
            data={
                "name": "bo2",
                "profile_id": "p1",
                "variant_label": ["A", "B"],
                "variant_profile_version_id": ["", ""],
                "variant_model_id": ["m1", ""],
                "variant_temperature": ["0.2", ""],
                "variant_thinking_level": ["high", ""],
                "variant_max_output_tokens": ["", ""],
                "variant_top_p": ["", ""],
                "suite_ids": ["s1", "s2"],
                "repeats": "3",
            },
            follow_redirects=False,
        )
    kwargs = mock_create.call_args.kwargs
    assert kwargs["repeats"] == 3
    assert kwargs["suite_ids"] == ["s1", "s2"]
    variant_a, variant_b = kwargs["variants"]
    assert variant_a["model_id"] == "m1"
    assert variant_a["gen_config"] == {"temperature": 0.2, "thinking_level": "high"}
    # Second variant has no overrides at all -> no gen_config / model_id keys.
    assert "gen_config" not in variant_b
    assert "model_id" not in variant_b


def test_bakeoff_create_with_fewer_than_two_variants_errors_without_500(client):
    """A ValueError from create_bakeoff (e.g. the < 2 variants guard)
    re-renders the wizard with the error surfaced, not a 500."""
    with patch(
        "admin.services.bakeoffs.create_bakeoff",
        side_effect=ValueError("a bake-off needs at least 2 variants"),
    ):
        r = client.post(
            "/admin/evals/bakeoffs",
            data={
                "name": "bo",
                "profile_id": "p1",
                "variant_label": ["A"],
                "variant_profile_version_id": [""],
                "variant_model_id": [""],
                "suite_ids": ["s1"],
                "repeats": "1",
            },
            follow_redirects=False,
        )
    assert r.status_code == 200
    assert "a bake-off needs at least 2 variants" in r.text
    assert "Model comparison" in r.text
    # The rejected form re-opens in its drawer so the error is visible.
    assert 'id="bakeoff-form-error"' in r.text


def test_bakeoff_create_requires_profile(client):
    """No profile_id at all is caught before create_bakeoff is even called."""
    with patch("admin.services.bakeoffs.create_bakeoff") as mock_create:
        r = client.post(
            "/admin/evals/bakeoffs",
            data={"name": "bo", "profile_id": "", "repeats": "1"},
            follow_redirects=False,
        )
    assert r.status_code == 200
    assert "Choose an agent." in r.text
    mock_create.assert_not_called()


def test_bakeoff_detail_renders(client):
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        return_value={
            "bakeoff": {
                "bakeoff_id": "b1", "name": "bo",
                "suite_ids": ["s1"], "variants": [],
            },
            "matrix": {"by_variant": {}},
            "status": "running",
        },
    ):
        r = client.get(f"/admin/evals/bakeoffs/{BO}")
    assert r.status_code == 200
    assert "bo" in r.text
    # Still-running note (status != complete).
    assert "Still running" in r.text


def test_bakeoff_detail_renders_matrix_with_winner_highlight(client):
    """Aggregated matrix (no ?suite_id=) renders matrix.by_variant, with the
    higher-mean_score variant highlighted as the winner."""
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        return_value={
            "bakeoff": {
                "bakeoff_id": "b1", "name": "bo", "profile_id": "p1",
                "suite_ids": ["s1"], "variants": [{"label": "A"}, {"label": "B"}],
                "created_at": "2026-07-23T10:00:00+00:00",
            },
            "matrix": {
                "by_variant": {
                    "A": {"model_label": "Sonnet", "mean_score": 0.9, "pass_rate": 0.8,
                          "p50_latency_ms": 500, "total_cost_usd": 0.01, "mean_cost_usd": 0.001},
                    "B": {"model_label": "Haiku", "mean_score": 0.6, "pass_rate": 0.5,
                          "p50_latency_ms": 200, "total_cost_usd": 0.002, "mean_cost_usd": 0.0002},
                },
            },
            "status": "complete",
        },
    ):
        r = client.get(f"/admin/evals/bakeoffs/{BO}")
    assert r.status_code == 200
    assert "90%" in r.text  # variant A's mean score, as a percent
    # Verdict first: the recommendation banner names the winner (A passed
    # more cases), and the per-column winner is highlighted.
    assert "Recommended model" in r.text
    assert r.text.index("Recommended model") < r.text.index("Results")
    assert "metric-best" in r.text


def test_bakeoff_detail_suite_focus_query_param(client):
    """?suite_id= is forwarded to get_bakeoff as suite_focus."""
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        return_value={
            "bakeoff": {"bakeoff_id": "b1", "name": "bo", "suite_ids": ["s1", "s2"], "variants": []},
            "matrix": {"rows": []},
            "status": "complete",
        },
    ) as mock_get:
        r = client.get(f"/admin/evals/bakeoffs/{BO}?suite_id=s1")
    assert r.status_code == 200
    assert mock_get.call_args.kwargs.get("suite_focus") == "s1"


def test_bakeoff_detail_not_found_is_404(client):
    with patch(
        "admin.services.bakeoffs.get_bakeoff",
        side_effect=ValueError("bake-off nope not found"),
    ):
        r = client.get("/admin/evals/bakeoffs/nope")
    assert r.status_code == 404


def test_build_bakeoffs_context_has_expected_keys():
    """build_bakeoffs_context() returns the keys the wizard template needs."""
    with patch(
        "admin.services.bakeoffs.list_bakeoffs",
        return_value=[{"bakeoff_id": "b1", "n_variants": 2, "n_suites": 1,
                        "_profile_name": "Sales", "status": "complete", "name": "bo"}],
    ):
        ctx = build_bakeoffs_context()
    for key in ("bakeoffs", "profiles", "profile_versions_by_profile",
                "suites_by_profile", "models"):
        assert key in ctx
    assert ctx["bakeoffs"][0]["bakeoff_id"] == "b1"


def test_build_bakeoffs_context_includes_self_scored_profile_and_suites():
    """C1: a self-scoring (judge-free) profile and any suite bound to it, e.g.
    an 'item_ids_validity' suite, must appear in the wizard's picker data —
    it is otherwise unreachable from the bake-off wizard."""
    fake = MagicMock()
    tables = {
        "agent_profiles": [
            {"profile_id": "p-1", "display_name": "Biz prod", "agent_kind": "sample_agent",
             "endpoint_path": "/business/chat"},
            {"profile_id": "p-rec", "display_name": "Item picker",
             "agent_kind": "sample_scored", "endpoint_path": "/recommendations"},
        ],
        "agent_eval_suites": [
            {"suite_id": "s-rec", "name": "item_ids_validity", "profile_id": "p-rec"},
        ],
    }

    def _table(name):
        builder = MagicMock()
        result = MagicMock()
        result.data = tables.get(name, [])
        builder.execute.return_value = result
        for method in ("select", "eq", "order", "limit", "in_", "gte"):
            getattr(builder, method).return_value = builder
        return builder

    fake.table.side_effect = _table

    with patch("admin.eval_runs_handlers.get_service_client", return_value=fake), \
         patch("admin.services.bakeoffs.list_bakeoffs", return_value=[]):
        ctx = build_bakeoffs_context()

    profile_ids = {p["profile_id"] for p in ctx["profiles"]}
    assert "p-rec" in profile_ids
    rec_suites = ctx["suites_by_profile"].get("p-rec") or []
    assert any(s.get("suite_id") == "s-rec" for s in rec_suites)


def test_bakeoffs_wizard_renders_wizard_scaffolding(client):
    """The GET wizard page renders the min-2 variant rows, the add-variant
    button, and the suites-step container — not the old placeholder."""
    r = client.get("/admin/evals/bakeoffs")
    assert r.status_code == 200
    assert "Coming in this PR" not in r.text
    assert r.text.count('class="variant-row"') == 2
    assert 'id="bo-add-variant"' in r.text
    assert 'id="bo-suites"' in r.text
    assert "Choose an agent first" in r.text  # suites list populated client-side, not server-rendered


def test_load_suites_by_profile_resolves_across_environments():
    """A suite bound to the STAGING profile row of (business, default) must be
    offered under the PROD profile row of the same (agent_kind, slug) — because
    suites bind to staging while version snapshots (what a bake-off compares)
    live on prod. A different slug of the same kind must NOT inherit it, and a
    NULL-profile suite appears nowhere."""
    from admin.eval_runs_handlers import _load_suites_by_profile

    suite_rows = [
        {"suite_id": "s-biz", "name": "business_grounding", "profile_id": "p-biz-staging"},
        {"suite_id": "s-rep", "name": "reporting_suite", "profile_id": "p-rep-staging"},
        {"suite_id": "s-orphan", "name": "orphan", "profile_id": None},
    ]
    bound_profiles = [
        {"profile_id": "p-biz-staging", "agent_kind": "sample_agent", "slug": "default"},
        {"profile_id": "p-rep-staging", "agent_kind": "sample_agent", "slug": "business_reporting"},
    ]

    def _table(name):
        b = MagicMock(); res = MagicMock()
        res.data = {"agent_eval_suites": suite_rows,
                    "agent_profiles": bound_profiles}.get(name, [])
        b.execute.return_value = res
        for m in ("select", "eq", "order", "limit", "in_", "gte"):
            getattr(b, m).return_value = b
        return b

    client = MagicMock(); client.table.side_effect = _table

    # picker list: the PROD rows (which carry versions), different profile_ids
    picker = [
        {"profile_id": "p-biz-prod", "agent_kind": "sample_agent", "slug": "default"},
        {"profile_id": "p-rep-prod", "agent_kind": "sample_agent", "slug": "business_reporting"},
    ]
    out = _load_suites_by_profile(client, picker)

    # prod business/default inherits the staging-bound business_grounding suite
    assert [s["suite_id"] for s in out["p-biz-prod"]] == ["s-biz"]
    # the reporting slug gets its own, NOT business_grounding
    assert [s["suite_id"] for s in out["p-rep-prod"]] == ["s-rep"]
    # orphan (NULL profile) appears under nobody
    all_suite_ids = {s["suite_id"] for lst in out.values() for s in lst}
    assert "s-orphan" not in all_suite_ids


def test_load_picker_data_carries_environment_and_slug():
    """_load_picker_data must expose environment + slug on each profile so the
    picker labels can disambiguate staging vs prod rows and Fix 2's logical
    matching has the slug it needs."""
    from admin.eval_runs_handlers import _load_picker_data

    profiles = [
        {"profile_id": "p1", "display_name": "Biz", "agent_kind": "sample_agent",
         "slug": "default", "environment": "prod", "endpoint_path": "/x"},
    ]

    def _table(name):
        b = MagicMock(); res = MagicMock()
        res.data = {"agent_profiles": profiles}.get(name, [])
        b.execute.return_value = res
        for m in ("select", "eq", "order", "limit", "in_"):
            getattr(b, m).return_value = b
        return b

    client = MagicMock(); client.table.side_effect = _table
    with patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]):
        out = _load_picker_data(client)
    assert out["profiles"][0]["environment"] == "prod"
    assert out["profiles"][0]["slug"] == "default"


@pytest.fixture(autouse=True)
def _sample_kinds(product_build):
    """Generic logic, fictional agent kinds (tests/conftest.py::product_build)."""
