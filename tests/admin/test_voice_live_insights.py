"""Tests for the Dashboard's duplex-voice latency rollup.

`load_voice_live_insights` is a thin shaping layer over one ClickHouse
query — these tests stub the `_ch_trace_query` seam and check the shape,
the env scoping, and the degrade-gracefully contract (ClickHouse down must
render dashes, never 500 the Dashboard).
"""
from __future__ import annotations

import pytest

import admin.screens.observability as obs
from admin.screens.observability import load_voice_live_insights


def test_rows_are_shaped_for_the_card(monkeypatch):
    captured = {}

    def _fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{
            "sessions": "3", "turns": "17", "tool_calls": "6", "barge_ins": "2",
            "eot_p50": 740.5, "eot_p95": 1450.0,
            "answer_p50": 9200.0, "answer_p95": 14100.0,
            "delegate_p50": 8800.0, "delegate_p95": 13000.0,
        }]

    monkeypatch.setattr(obs, "_ch_trace_query", _fake_query)
    out = load_voice_live_insights(past_hours=24, env="prod")

    assert out["ch_ok"] is True
    assert out["sessions"] == 3
    assert out["turns"] == 17
    assert out["tool_calls"] == 6
    assert out["barge_ins"] == 2
    assert out["eot_first_audio"] == {"p50_ms": 740, "p95_ms": 1450}
    assert out["answer"] == {"p50_ms": 9200, "p95_ms": 14100}
    assert out["delegate"] == {"p50_ms": 8800, "p95_ms": 13000}
    # Env scoping travels as a ClickHouse param on span ATTRIBUTES (spans
    # table), same convention as the TTFT tile — never inlined into the SQL.
    assert captured["params"]["env"] == "prod"
    assert "waj.env" in captured["sql"]
    assert "waj.voice.live.op" in captured["sql"]


def test_no_rows_is_ok_but_empty(monkeypatch):
    monkeypatch.setattr(obs, "_ch_trace_query", lambda sql, params=None: [])
    out = load_voice_live_insights(past_hours=24, env=None)
    assert out["ch_ok"] is True
    assert out["turns"] == 0
    assert out["eot_first_audio"] == {"p50_ms": None, "p95_ms": None}


def test_clickhouse_down_degrades_with_notice(monkeypatch):
    from core.lmnr_clickhouse import ClickHouseUnavailable

    def _boom(sql, params=None):
        raise ClickHouseUnavailable("connection refused")

    monkeypatch.setattr(obs, "_ch_trace_query", _boom)
    out = load_voice_live_insights(past_hours=24, env="prod")
    assert out["ch_ok"] is False
    assert "connection refused" in (out["notice"] or "")
    # Shape intact for the template either way.
    assert out["answer"] == {"p50_ms": None, "p95_ms": None}


def test_brain_llm_stage_is_aggregated_off_the_agent_turn_span(monkeypatch):
    """`waj.voice.live.stage.llm_ms` rides the `voice.agent_turn` root span,
    which carries no `waj.voice.live.op` — the query must widen its filter by
    span NAME or the stage rows are invisible to the card."""
    captured = {}

    def _fake_query(sql, params=None):
        captured["sql"] = sql
        return [{
            "sessions": 1, "turns": 2, "tool_calls": 1, "barge_ins": 0,
            "eot_p50": None, "eot_p95": None,
            "answer_p50": None, "answer_p95": None,
            "delegate_p50": 2498.0, "delegate_p95": 4100.0,
            "brain_llm_p50": 1542.0,
        }]

    monkeypatch.setattr(obs, "_ch_trace_query", _fake_query)
    out = load_voice_live_insights(past_hours=24, env="prod")
    assert out["brain_llm"] == {"p50_ms": 1542}
    assert "waj.voice.live.stage.llm_ms" in captured["sql"]
    assert "voice.agent_turn" in captured["sql"]


def test_brain_llm_defaults_to_none_when_absent(monkeypatch):
    monkeypatch.setattr(obs, "_ch_trace_query", lambda sql, params=None: [])
    out = load_voice_live_insights(past_hours=24)
    assert out["brain_llm"] == {"p50_ms": None}


def test_nan_quantiles_read_as_none(monkeypatch):
    """quantileIf over zero matching rows comes back NaN/None — the card must
    show a dash, not 'nan'."""
    monkeypatch.setattr(obs, "_ch_trace_query", lambda sql, params=None: [{
        "sessions": 1, "turns": 2, "tool_calls": 0, "barge_ins": 0,
        "eot_p50": "nan", "eot_p95": None,
        "answer_p50": None, "answer_p95": None,
        "delegate_p50": None, "delegate_p95": None,
    }])
    out = load_voice_live_insights(past_hours=24)
    assert out["eot_first_audio"] == {"p50_ms": None, "p95_ms": None}
    assert out["turns"] == 2
