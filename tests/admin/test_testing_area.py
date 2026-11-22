"""Testing area redesign (B3): verdict-first runs, Fixed/Broke/Still failing,
one number format, unknown-not-zero, names instead of ids, 404 pages.

Pure helpers are tested directly; screens through the session `client`
(tests/admin/conftest.py) or a small scripted fake where data matters.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from admin import testing_view as tv
from tests.scoped_build import full_build


# ---------------------------------------------------------------------------
# One number format, one status vocabulary, unknown is "—"
# ---------------------------------------------------------------------------


def test_percent_and_points():
    assert tv.pct(0.789) == "79%"
    assert tv.pct(1) == "100%"
    assert tv.pct(None) == "—"
    assert tv.pts(-0.03) == "−3 pts"
    assert tv.pts(0.04) == "+4 pts"
    assert tv.pts(0) == "±0 pts"
    assert tv.pts(-0.11, signed=False) == "11 pts"


def test_unknown_cost_is_a_dash_not_zero():
    assert tv.money(None) == "—"
    assert tv.money(0.0102) == "$0.0102"
    assert tv.ms(None) == "—"
    assert tv.ms(7300) == "7.3 s"
    assert tv.count(None) == "—"


@pytest.mark.parametrize("raw,label,variant", [
    ("passed", "Passed", "ok"), ("failed", "Failed", "err"), ("errored", "Error", "err"),
    ("error", "Error", "err"), ("running", "Running", "info"), ("cancelled", "Cancelled", "warn"),
    ("skipped", "Skipped", "neutral"), (None, "Not run", "neutral"), ("PASSED", "Passed", "ok"),
])
def test_one_status_vocabulary(raw, label, variant):
    assert tv.status_label(raw) == label
    assert tv.status_variant(raw) == variant


def test_humanize_slugs():
    assert tv.humanize("sales_hallucination_traps") == "Sales hallucination traps"
    assert tv.humanize("month_review_ar") == "Month review AR"
    assert tv.humanize("Already a name") == "Already a name"


def test_verdict_against_target():
    assert tv.verdict(0.79, 0.9) == "needs_work"
    assert tv.verdict(0.9, 0.9) == "ready"
    assert tv.verdict(None, 0.9) == "untested"


def test_when_is_relative():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    assert tv.when("2026-10-11T11:55:00+00:00", now=now) == "5 min ago"
    assert tv.when("2026-10-10T11:00:00+00:00", now=now) == "yesterday"
    assert tv.when(None, now=now) == "—"


# ---------------------------------------------------------------------------
# Why did a case fail — one plain line
# ---------------------------------------------------------------------------


def test_case_reason_prefers_judge_then_failed_check():
    assert tv.case_reason({"passed": False, "reason": "Wrong price"}) == "Wrong price"
    assert tv.case_reason({"passed": False, "judge": {"reason": "Off-topic"}}) == "Off-topic"
    voice = {"passed": False, "scorer_detail": {"checks": [
        {"ok": True, "type": "no_uuid"},
        {"ok": False, "type": "tools_any", "detail": "want 0.0"},
    ]}}
    # The failed check, in words — and the raw "want" reads as "expected".
    assert tv.case_reason(voice) == "Didn't look up the right data — expected 0.0"


def test_a_plugins_check_types_label_themselves(monkeypatch):
    from types import SimpleNamespace

    hook = SimpleNamespace(check_labels=lambda: {"stock": "Quoted the wrong stock level"})
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [hook] if name == "eval_checks" else [])
    assert tv.check_label("stock") == "Quoted the wrong stock level"
    assert tv.check_label("regex") == "Didn't say what was expected"  # the engine's own win
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [])
    assert tv.check_label("stock") == "Stock"


def test_case_reason_limit_breach_and_missing_words():
    breach = {"passed": False, "scorer_detail": {"limit_breaches": [
        {"turn": 2, "kind": "max_latency_ms", "value": 16000, "limit": 15000}]}}
    assert tv.case_reason(breach).startswith("Took too long to start replying on turn 2")
    assert tv.case_reason({"passed": False, "missing_substrings": ["9am"]}) == "Answer was missing: “9am”"
    assert tv.case_reason({"passed": True}) == ""


# ---------------------------------------------------------------------------
# Run detail: failed first, broke / fixed against the previous run
# ---------------------------------------------------------------------------


def _r(sid: str, passed: bool, **kw: Any) -> Dict[str, Any]:
    return {"scenario_id": sid, "passed": passed, "score": 1.0 if passed else 0.0, **kw}


def test_build_case_rows_failed_first_with_broke_and_fixed():
    from admin.eval_handlers import build_case_rows

    prev = [_r("a", True), _r("b", False), _r("c", True), _r("d", False)]
    now = [_r("a", True), _r("b", True), _r("c", False, reason="Said Monday"), _r("d", False)]
    rows = build_case_rows(now, prev)
    order = [r["scenario_id"] for r in rows]
    # Failures first; the one that BROKE since the previous run leads them.
    assert order[:2] == ["c", "d"]
    by = {r["scenario_id"]: r for r in rows}
    assert by["c"]["broke"] and not by["d"]["broke"]
    assert by["b"]["fixed"]
    assert by["c"]["reason"] == "Said Monday"
    assert by["a"]["outcome"] == "passed" and by["c"]["outcome"] == "failed"


def test_build_case_rows_repeats_are_flaky():
    from admin.eval_handlers import build_case_rows

    rows = build_case_rows([_r("x", True), _r("x", False), _r("y", True)])
    by = {r["scenario_id"]: r for r in rows}
    assert by["x"]["outcome"] == "flaky" and by["x"]["n"] == 2
    assert rows[0]["scenario_id"] == "x"


def test_run_kpis_unknown_cost_and_tokens_are_none_not_zero():
    """Voice runs record no cost/tokens: the KPI must be None ('—'), never a
    confident $0 / 0 tokens."""
    from admin.eval_handlers import run_detail_kpis

    k = run_detail_kpis([{"scenario_id": "a", "passed": True, "cost_usd": None,
                          "input_tokens": 0, "output_tokens": 0}])
    assert k["cost_usd"] is None
    assert k["tokens_total"] is None
    priced = run_detail_kpis([{"scenario_id": "a", "passed": True, "cost_usd": 0.002,
                               "input_tokens": 10, "output_tokens": 5}])
    assert priced["cost_usd"] == 0.002
    assert priced["tokens_total"] == 15


def test_previous_run_skips_bakeoff_and_override_runs():
    from admin.eval_runs_handlers import previous_run_map

    runs = [  # newest first
        {"run_id": "r3", "suite_id": "s", "profile_id": "p", "status": "failed", "score": 0.7},
        {"run_id": "rb", "suite_id": "s", "profile_id": "p", "status": "passed", "score": 1.0,
         "bakeoff_id": "b"},
        {"run_id": "ro", "suite_id": "s", "profile_id": "p", "status": "passed", "score": 0.95,
         "context": {"model_override": {"model_string": "x"}}},
        {"run_id": "r1", "suite_id": "s", "profile_id": "p", "status": "passed", "score": 0.8},
    ]
    prev = previous_run_map(runs)
    assert prev["r3"]["run_id"] == "r1"
    assert "rb" not in prev and "ro" not in prev


# ---------------------------------------------------------------------------
# Bake-off: recommendation + unknown cost
# ---------------------------------------------------------------------------


def test_bakeoff_recommendation_and_unknown_cost():
    from admin.eval_runs_handlers import _bakeoff_recommendation, _blank_unknowns

    rows = [_blank_unknowns(r) for r in [
        {"variant_label": "Flash", "n_scenarios": 8, "pass_rate": 1.0, "mean_score": 1.0,
         "mean_cost_usd": 0.001, "total_cost_usd": 0.008, "priced_scenarios": 8, "p50_latency_ms": 2800},
        {"variant_label": "Mini", "n_scenarios": 8, "pass_rate": 0.875, "mean_score": 0.9,
         "mean_cost_usd": 0.002, "total_cost_usd": 0.016, "priced_scenarios": 8, "p50_latency_ms": 3300},
        {"variant_label": "Lite", "n_scenarios": 8, "pass_rate": 0.625, "mean_score": 0.7,
         "mean_cost_usd": 0.0, "total_cost_usd": 0.0, "priced_scenarios": 0, "p50_latency_ms": 2000},
    ]]
    assert rows[2]["mean_cost_usd"] is None  # never priced -> unknown, not $0
    rec = _bakeoff_recommendation(rows)
    assert rec["best"]["variant_label"] == "Flash"
    assert rec["second"]["variant_label"] == "Mini"
    assert rec["cheapest"] is True
    assert round(rec["gap"], 3) == 0.125


# ---------------------------------------------------------------------------
# Bad ids -> the portal 404 page (not 500, not JSON)
# ---------------------------------------------------------------------------


HTML = {"accept": "text/html"}


@pytest.mark.parametrize("path", [
    "/admin/evals/runs/not-a-uuid",
    "/admin/evals/suites/not-a-uuid",
    "/admin/evals/bakeoffs/not-a-uuid",
    "/admin/evals/compare?runs=x,y",
    "/admin/tests/not-a-uuid",
    "/admin/e2e/runs/not-a-uuid",
])
def test_bad_ids_render_the_portal_404_page(client, path):
    r = client.get(path, headers=HTML)
    assert r.status_code == 404
    assert "error-page" in r.text
    assert "Back to Testing" in r.text or "Back to Agents" in r.text


def test_compare_with_one_run_is_a_plain_400_page(client):
    r = client.get("/admin/evals/compare", headers=HTML)
    assert r.status_code == 400
    assert "error-page" in r.text
    assert "Pick at least two runs" in r.text


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class _Q:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def __getattr__(self, name):  # select / order / limit / eq / in_ / neq …
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": list(self._rows)})()


class _Fake:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self._t = tables

    def table(self, name):
        return _Q(self._t.get(name, []))


def _overview_fake() -> _Fake:
    return _Fake({
        "agent_profiles": [
            {"profile_id": "p-biz", "display_name": "Front desk assistant", "agent_kind": "business",
             "slug": "biz", "environment": "staging"},
            {"profile_id": "p-sales", "display_name": "Sales helper", "agent_kind": "sales",
             "slug": "sales", "environment": "staging"},
        ],
        "agent_eval_suites": [
            {"suite_id": "s1", "name": "booking_questions", "profile_id": "p-biz",
             "agent_kind": "business", "pass_threshold": 0.9, "scenarios": [{}, {}]},
            {"suite_id": "s2", "name": "pricing", "profile_id": "p-sales",
             "agent_kind": "sales", "pass_threshold": 0.8, "scenarios": [{}]},
            {"suite_id": "s3", "name": "orphan", "profile_id": None, "agent_kind": "business",
             "pass_threshold": 0.7, "scenarios": []},
        ],
        "agent_eval_runs": [
            {"run_id": "r2", "suite_id": "s1", "profile_id": "p-biz", "status": "failed",
             "score": 0.79, "created_at": "2026-10-10T09:00:00Z", "finished_at": "2026-10-10T09:05:00Z",
             "context": {}, "results": [{"passed": True}, {"passed": False}]},
            {"run_id": "r1", "suite_id": "s1", "profile_id": "p-biz", "status": "failed",
             "score": 0.82, "created_at": "2026-10-09T09:00:00Z", "finished_at": "2026-10-09T09:05:00Z",
             "context": {}, "results": [{"passed": True}, {"passed": True}]},
        ],
        "agent_models": [],
    })


def test_overview_one_card_per_agent_verdict_first(monkeypatch):
    full_build(monkeypatch)
    from admin import eval_runs_handlers as h

    with patch.object(h, "admin_service_client", return_value=_overview_fake()), \
            patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]):
        ctx = h.build_testing_overview_context()
    cards = {c["name"]: c for c in ctx["cards"]}
    biz = cards["Front desk assistant"]
    assert biz["verdict"] == "needs_work"            # 79% < 90% target
    assert biz["focus"]["latest"]["run_id"] == "r2"
    assert round(biz["focus"]["delta"], 2) == -0.03  # −3 pts since the run before
    assert biz["series"] == [82.0, 79.0]
    assert cards["Sales helper"]["verdict"] == "untested"
    assert ctx["cards"][0]["name"] == "Front desk assistant"  # needs work sorts first
    assert ctx["summary"]["unlinked_suites"] == 1


def test_overview_page_renders_cards(client, monkeypatch):
    full_build(monkeypatch)
    from admin import eval_runs_handlers as h

    with patch.object(h, "admin_service_client", return_value=_overview_fake()):
        r = client.get("/admin/evals")
    assert r.status_code == 200
    assert "Front desk assistant" in r.text
    assert "Needs work" in r.text and "Not tested" in r.text
    assert "79%" in r.text and "target 90%" in r.text.lower()
    assert "−3 pts" in r.text
    assert "/admin/evals/runs/r2?show=failed" in r.text


def test_runs_compare_button_starts_disabled(client):
    from admin import eval_runs_handlers as h

    with patch.object(h, "admin_service_client", return_value=_overview_fake()):
        r = client.get("/admin/evals/runs")
    assert r.status_code == 200
    assert 'id="cmp-go" class="btn" disabled aria-disabled="true"' in r.text
    # Percent everywhere, never a raw 0.79 fraction.
    assert "79%" in r.text and "0.79" not in r.text
    # Agent names, not raw ids.
    assert "Front desk assistant" in r.text


def test_suites_list_has_run_button_and_percent_target(client):
    from admin import eval_runs_handlers as h

    with patch.object(h, "admin_service_client", return_value=_overview_fake()):
        r = client.get("/admin/evals/suites")
    assert r.status_code == 200
    assert "Booking questions" in r.text            # humanised name
    assert "target 90%" in r.text                    # not "0.9"
    assert 'data-open-run data-profile="p-biz" data-suite="s1"' in r.text


class _UuidStrictQuery(_Q):
    """Like PostgREST: a non-uuid value in a uuid filter is a server error."""

    def eq(self, col, value):
        import uuid
        uuid.UUID(str(value))  # raises ValueError -> 500 if a route doesn't guard
        return self


class _UuidStrict(_Fake):
    def table(self, name):
        return _UuidStrictQuery(self._t.get(name, []))


def test_malformed_run_id_is_404_before_any_query(client):
    """Before B3 a malformed id reached PostgREST's uuid cast and 500ed."""
    from admin import router as router_mod

    with patch.object(router_mod, "client_for", return_value=_UuidStrict({})):
        r = client.get("/admin/evals/runs/not-a-uuid", headers=HTML)
    assert r.status_code == 404


def test_humanize_takes_acronyms_from_the_plugin_hook(monkeypatch):
    import types

    import core.plugins as plugins

    hook = types.SimpleNamespace(display_acronyms=lambda: ("crm",))
    real = plugins.load_hook
    monkeypatch.setattr(plugins, "load_hook",
                        lambda name: [hook] if name == "portal_display" else real(name))
    monkeypatch.setattr(tv, "_acronym_cache", {})
    assert tv.humanize("crm_sync_check") == "CRM sync check"
    monkeypatch.setattr(tv, "_acronym_cache", {})
