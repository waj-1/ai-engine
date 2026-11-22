"""Tests for _live_kpis TTFT/TTLT tile wiring.

Covers the mapping from insights["ttft"] / insights["ttlt"] → the KPI dict
that the live monitor tiles consume, and the graceful-degradation path when
those keys are absent (ClickHouse down / no data).
"""
from __future__ import annotations

from unittest.mock import MagicMock


def _make_null_db():
    """Minimal fluent Supabase mock that returns empty data for every query."""
    result = MagicMock()
    result.data = []
    builder = MagicMock()
    builder.execute.return_value = result
    for m in ("select", "eq", "order", "limit", "gte"):
        getattr(builder, m).return_value = builder
    client = MagicMock()
    client.table.return_value = builder
    return client


def _get_live_kpis():
    from admin import router as r
    return r._live_kpis


def test_ttft_ttlt_forwarded_from_insights():
    """When insights carries ttft/ttlt, _live_kpis passes the p50 values through."""
    _live_kpis = _get_live_kpis()
    insights = {
        "lmnr_ok": True,
        "top_tools": [],
        "latency": {"p50_ms": 800, "p95_ms": 1500},
        "tokens": {"in_1h": 0, "out_1h": 0, "in_24h": 1000, "out_24h": 500},
        "ttft": {"p50_ms": 120, "p95_ms": 340},
        "ttlt": {"p50_ms": 950, "p95_ms": 2100},
    }
    kpis = _live_kpis(_make_null_db(), "staging", insights)
    assert kpis["ttft_p50_ms"] == 120
    assert kpis["ttlt_p50_ms"] == 950


def test_ttft_ttlt_dash_when_keys_absent():
    """When insights has no ttft/ttlt (ClickHouse down), tiles show '—'."""
    _live_kpis = _get_live_kpis()
    insights = {
        "lmnr_ok": True,
        "top_tools": [],
        "latency": {"p50_ms": 800, "p95_ms": 1500},
        "tokens": {"in_1h": 0, "out_1h": 0, "in_24h": 1000, "out_24h": 500},
        # no "ttft" or "ttlt" keys
    }
    kpis = _live_kpis(_make_null_db(), "staging", insights)
    assert kpis["ttft_p50_ms"] == "—"
    assert kpis["ttlt_p50_ms"] == "—"


def test_ttft_ttlt_dash_when_lmnr_not_ok():
    """When lmnr_ok is False the whole KPI dict short-circuits to '—'."""
    _live_kpis = _get_live_kpis()
    insights = {"lmnr_ok": False}
    kpis = _live_kpis(_make_null_db(), "staging", insights)
    assert kpis["ttft_p50_ms"] == "—"
    assert kpis["ttlt_p50_ms"] == "—"


def test_ttft_ttlt_none_value_shows_dash():
    """When p50_ms is present but None (no data), _live_kpis shows '—'."""
    _live_kpis = _get_live_kpis()
    insights = {
        "lmnr_ok": True,
        "top_tools": [],
        "latency": {"p50_ms": None, "p95_ms": None},
        "tokens": {"in_1h": 0, "out_1h": 0, "in_24h": 0, "out_24h": 0},
        "ttft": {"p50_ms": None, "p95_ms": None},
        "ttlt": {"p50_ms": None, "p95_ms": None},
    }
    kpis = _live_kpis(_make_null_db(), "staging", insights)
    # None coerces to "—" via `or "—"` in _live_kpis
    assert kpis["ttft_p50_ms"] == "—"
    assert kpis["ttlt_p50_ms"] == "—"
