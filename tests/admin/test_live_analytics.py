"""Tests for the Live Monitor analytics layer.

Two halves:

* The **pure builders** (bucketing, percentiles, rollups, histograms) take plain
  trace-row dicts and are tested with no I/O at all.
* **`load_analytics`** is tested with the ClickHouse client fully mocked, so we
  pin the payload contract and the graceful-degradation path without a live DB.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from admin.screens import live_analytics as la
from core.lmnr_clickhouse import ClickHouseUnavailable


# --------------------------------------------------------------------------- #
# Pure builders
# --------------------------------------------------------------------------- #
def test_choose_bucket_scales_with_window():
    assert la.choose_bucket(1) == timedelta(minutes=5)
    assert la.choose_bucket(24) == timedelta(hours=1)
    assert la.choose_bucket(24 * 30) == timedelta(days=1)


def test_percentile_empty_and_values():
    assert la.percentile([], 0.5) is None
    assert la.percentile([10, 20, 30, 40], 0.5) in (20, 30)
    assert la.percentile([5], 0.95) == 5


def test_cost_for_row_prefers_precomputed():
    # ClickHouse rows carry precomputed cost_usd; cost_map is irrelevant.
    row = {"cost_usd": 0.0042, "input_tokens": 999, "model_id": "x"}
    assert la.cost_for_row(row, {"x": (1.0, 1.0)}) == pytest.approx(0.0042)


def test_cost_for_row_falls_back_to_cost_map():
    row = {"input_tokens": 1000, "output_tokens": 2000, "model_id": "gpt"}
    # 1000/1000*0.01 + 2000/1000*0.03 = 0.01 + 0.06 = 0.07
    assert la.cost_for_row(row, {"gpt": (0.01, 0.03)}) == pytest.approx(0.07)


def test_parse_ts_handles_clickhouse_nanoseconds():
    # ClickHouse DateTime64(9) renders 9 fractional digits + space separator.
    ts = la._parse_ts("2026-05-21 10:59:56.067467000")
    assert ts is not None
    assert ts.year == 2026 and ts.minute == 59 and ts.second == 56


def test_bucket_series_buckets_and_aggregates(product_build):
    rows = [
        {"created_at": "2026-05-01 00:00:00", "agent_kind": "sample_agent",
         "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01,
         "latency_ms": 200, "iteration_count": 2, "error": False},
        {"created_at": "2026-05-01 00:02:00", "agent_kind": "sample_other",
         "input_tokens": 200, "output_tokens": 80, "cost_usd": 0.02,
         "latency_ms": 400, "iteration_count": 4, "error": True},
    ]
    out = la.bucket_series(rows, timedelta(minutes=5), {})
    assert len(out["buckets"]) == 1            # both land in one 5-min bucket
    s = out["series"]
    assert s["volume"] == [2]
    assert s["tokens_in"] == [300]
    assert s["cost_usd"] == [pytest.approx(0.03)]
    assert s["error_rate"] == [50.0]
    assert out["volume_by_agent"]["sample_agent"] == [1]
    assert out["volume_by_agent"]["sample_other"] == [1]


def test_per_agent_rollup_sorted_desc():
    rows = [
        {"agent_kind": "business", "cost_usd": 0.01, "latency_ms": 100,
         "input_tokens": 10, "output_tokens": 5, "error": False},
        {"agent_kind": "business", "cost_usd": 0.02, "latency_ms": 300,
         "input_tokens": 20, "output_tokens": 8, "error": True},
        {"agent_kind": "support_bot", "cost_usd": 0.05, "latency_ms": 50,
         "input_tokens": 5, "output_tokens": 2, "error": False},
    ]
    out = la.per_agent_rollup(rows, {})
    assert [r["agent_kind"] for r in out] == ["business", "support_bot"]
    business = out[0]
    assert business["volume"] == 2
    assert business["error_rate"] == 50.0
    assert business["cost_usd"] == pytest.approx(0.03)


def test_cost_share_totals_per_agent():
    rows = [
        {"agent_kind": "business", "cost_usd": 0.01},
        {"agent_kind": "business", "cost_usd": 0.03},
        {"agent_kind": "support_bot", "cost_usd": 0.10},
    ]
    out = la.cost_share(rows, {})
    assert out[0] == {"agent_kind": "support_bot", "cost_usd": pytest.approx(0.10)}
    assert out[1]["agent_kind"] == "business"
    assert out[1]["cost_usd"] == pytest.approx(0.04)


def test_nice_step_rounds_to_human_numbers():
    # Rough widths snap up to a 1/2/2.5/5 × 10ⁿ "nice" value.
    assert la._nice_step(0) == 1.0
    assert la._nice_step(80) == 100
    assert la._nice_step(120) == 200
    assert la._nice_step(300) == 500
    assert la._nice_step(900) == 1000
    assert la._nice_step(1100) == 2000


def test_latency_histogram_is_data_driven_with_outlier_bucket():
    # A cluster of fast traces plus a few slow outliers. Edges must derive from
    # the data (p95 cutoff), NOT a fixed table, and the slow tail collapses into
    # a single open-ended ">cutoff" bucket so it can't flatten the main bars.
    fast = [{"latency_ms": v} for v in range(50, 850, 10)]  # ~80 rows, <850ms
    slow = [{"latency_ms": v} for v in (9000, 12000, 30000)]  # outliers
    hist = la.latency_histogram(fast + slow)

    # An outlier bucket exists and is the trailing label "≥ <cutoff>".
    assert hist["outlier"] is True
    assert hist["cutoff_ms"] is not None
    assert hist["labels"][-1].startswith("≥")
    # The cutoff is data-driven (near p95 of the distribution), well below the
    # 30s max — proving the range is NOT fixed at 30s.
    assert hist["cutoff_ms"] < 5000
    # Edges are nice round numbers, ascending, and bound the main (non-tail) bins.
    assert hist["edges"] == sorted(hist["edges"])
    assert all(e > 0 for e in hist["edges"])
    # Every value is counted exactly once, and the 3 outliers land in the tail.
    assert sum(hist["counts"]) == len(fast) + len(slow)
    assert hist["counts"][-1] == len(slow)
    # One label per bucket (finite bins + the outlier bucket).
    assert len(hist["labels"]) == len(hist["counts"]) == len(hist["edges"]) + 1


def test_latency_histogram_no_outlier_when_tight_distribution():
    # When the max does not exceed the p95 cutoff (no real tail), there is no
    # outlier bucket and the full range is binned. A tight body with the top
    # values clustered at the max keeps p95 == max.
    rows = [{"latency_ms": v} for v in range(100, 500, 25)]
    rows += [{"latency_ms": 500} for _ in range(6)]  # cluster at the max
    hist = la.latency_histogram(rows)
    assert hist["outlier"] is False
    assert hist["cutoff_ms"] is None
    assert not any(lbl.startswith("≥") for lbl in hist["labels"])
    assert sum(hist["counts"]) == len(rows)
    assert len(hist["labels"]) == len(hist["counts"]) == len(hist["edges"])


def test_latency_histogram_empty_is_clean():
    # No data → empty lists, stable shape, no crash.
    empty = la.latency_histogram([])
    assert empty["labels"] == []
    assert empty["counts"] == []
    assert empty["edges"] == []
    assert empty["outlier"] is False
    assert empty["cutoff_ms"] is None


def test_latency_histogram_skips_none_latency():
    rows = [{"latency_ms": 100}, {"latency_ms": 200}, {"latency_ms": None}]
    hist = la.latency_histogram(rows)
    assert sum(hist["counts"]) == 2  # the None row is skipped


def test_latency_histogram_identical_values():
    # All-identical (or all-zero) latencies degrade to a single bucket cleanly.
    rows = [{"latency_ms": 0} for _ in range(5)]
    hist = la.latency_histogram(rows)
    assert sum(hist["counts"]) == 5
    assert hist["outlier"] is False


# --------------------------------------------------------------------------- #
# load_analytics — ClickHouse mocked
# --------------------------------------------------------------------------- #
_TRACE_ROWS = [
    {"created_at": "2026-05-01 00:00:00.000000000", "agent_kind": "business",
     "channel_slug": "waj_app", "input_tokens": 100, "output_tokens": 50,
     "cost_usd": 0.01, "latency_ms": 200.0, "iteration_count": 2, "error": 0},
    {"created_at": "2026-05-01 00:01:00.000000000", "agent_kind": "support_bot",
     "channel_slug": "mobile_app", "input_tokens": 300, "output_tokens": 90,
     "cost_usd": 0.05, "latency_ms": 900.0, "iteration_count": 5, "error": 1},
]
_TOOL_ROWS = [
    {"name": "search_orders", "calls": 108},
    {"name": "create_ticket", "calls": 72},
]


def test_load_analytics_assembles_payload():
    with patch.object(la, "ch_query", side_effect=[_TRACE_ROWS, _TOOL_ROWS]):
        payload = la.load_analytics(hours=24)

    assert payload["ok"] is True
    assert payload["window_hours"] == 24
    assert payload["totals"]["volume"] == 2
    assert payload["totals"]["tokens_in"] == 400
    assert payload["totals"]["cost_usd"] == pytest.approx(0.06)
    # two agents → two per-agent rollup rows + two cost-share rows
    assert {r["agent_kind"] for r in payload["per_agent"]} == {"business", "support_bot"}
    assert payload["top_tools"][0] == {"name": "search_orders", "calls": 108}
    # series arrays line up with the bucket axis
    assert len(payload["series"]["volume"]) == len(payload["buckets"])


def test_load_analytics_surfaces_latency_percentile_stats():
    with patch.object(la, "ch_query", side_effect=[_TRACE_ROWS, _TOOL_ROWS]):
        payload = la.load_analytics(hours=24)
    totals = payload["totals"]
    # p50/p95/p99 latency tiles are computed over the two rows (200ms, 900ms).
    assert totals["p50_ms"] in (200.0, 900.0)
    assert totals["p95_ms"] == 900.0
    assert totals["p99_ms"] == 900.0
    # data-driven histogram travels in the payload with its new shape keys.
    assert "outlier" in payload["latency_hist"]
    assert "cutoff_ms" in payload["latency_hist"]


def test_empty_payload_includes_p99_and_hist_shape():
    with patch.object(la, "ch_query", side_effect=ClickHouseUnavailable("down")):
        payload = la.load_analytics(hours=24)
    assert payload["totals"]["p99_ms"] is None
    assert payload["latency_hist"] == {
        "labels": [], "counts": [], "edges": [], "outlier": False, "cutoff_ms": None,
    }


def test_load_analytics_status_filter_keeps_only_errors():
    with patch.object(la, "ch_query", side_effect=[_TRACE_ROWS, _TOOL_ROWS]):
        payload = la.load_analytics(hours=24, status="error")
    assert payload["totals"]["volume"] == 1
    assert payload["per_agent"][0]["agent_kind"] == "support_bot"


def test_load_analytics_degrades_when_clickhouse_down():
    with patch.object(la, "ch_query", side_effect=ClickHouseUnavailable("down")):
        payload = la.load_analytics(hours=24)
    assert payload["ok"] is False
    assert payload["notice"]
    assert payload["totals"]["volume"] == 0
    assert payload["buckets"] == []
    assert payload["top_tools"] == []


def test_load_analytics_passes_filters_to_clickhouse():
    calls = []

    def _capture(sql, params=None, **kw):
        calls.append({"sql": sql, "params": params or {}})
        return [] if "LIKE '%.tool'" in sql else []

    with patch.object(la, "ch_query", side_effect=_capture):
        la.load_analytics(hours=6, agent_kind="business", channel="waj_app")

    trace_call, tools_call = calls[0], calls[1]
    assert trace_call["params"]["agent_kind"] == "business"
    assert trace_call["params"]["channel"] == "waj_app"
    assert trace_call["params"]["hours"] == 6
    # filters are parameterised, never concatenated as raw values
    assert "business" not in trace_call["sql"]
    # the tools query is scoped by the same window + filters
    assert tools_call["params"]["agent_kind"] == "business"
    assert tools_call["params"]["channel"] == "waj_app"
    assert tools_call["params"]["hours"] == 6
    assert "waj_app" not in tools_call["sql"]


def test_load_analytics_filters_by_environment():
    calls = []

    def _capture(sql, params=None, **kw):
        calls.append({"sql": sql, "params": params or {}})
        return []

    with patch.object(la, "ch_query", side_effect=_capture):
        la.load_analytics(hours=24, environment="staging")

    trace_call = calls[0]
    # env is parameterised on the top span's waj.env attribute, never inlined.
    assert trace_call["params"]["environment"] == "staging"
    assert "waj.env" in trace_call["sql"]
    assert "staging" not in trace_call["sql"]
    # filters echo back in the payload so the client can reflect them.


def test_dev_env_filter_includes_untagged_traces():
    """Historical traces predate env tagging (waj.env is ''); the dev view must
    still include them, while staging/prod match only explicitly-tagged traces.

    The env clause filters the per-trace `sagg.env` aggregate (pulled from the
    trace's tagged span), not `top_span_id`."""
    staging_sql, _ = la._filter_clauses(None, None, environment="staging")
    assert "= ''" in staging_sql and "sagg.env" in staging_sql  # untagged counted as staging
    prod_sql, _ = la._filter_clauses(None, None, environment="prod")
    assert "= ''" not in prod_sql  # prod is exact-match only


def test_load_analytics_no_env_filter_when_unset():
    calls = []

    def _capture(sql, params=None, **kw):
        calls.append({"sql": sql, "params": params or {}})
        return []

    with patch.object(la, "ch_query", side_effect=_capture):
        payload = la.load_analytics(hours=24)

    assert "environment" not in calls[0]["params"]
    # The base SQL always extracts waj.env into the sagg aggregate; what must be
    # absent when env is unset is the env *filter* clause (sagg.env = ...).
    assert "sagg.env =" not in calls[0]["sql"]
    assert payload["filters"]["environment"] is None


def test_load_analytics_warns_on_truncation(monkeypatch):
    monkeypatch.setattr(la, "_ROW_LIMIT", 2)
    with patch.object(la, "ch_query", side_effect=[_TRACE_ROWS, _TOOL_ROWS]):
        payload = la.load_analytics(hours=720)
    assert payload["ok"] is True
    assert payload["notice"] and "most recent" in payload["notice"]


def test_parse_ts_returns_none_on_garbage():
    assert la._parse_ts("not-a-timestamp") is None
    assert la._parse_ts(None) is None
    assert la._parse_ts("") is None
