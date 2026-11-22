"""Tests for the caching_analytics observe-domain MCP tool. Follows the
test_tools_observe.py convention: monkeypatch the screen functions on their
SOURCE module (admin.screens.live_analytics) since tools_observe imports them
lazily inside the tool, and monkeypatch client_for_env on the tool module."""
from __future__ import annotations

import admin.screens.live_analytics as la_mod
import admin_mcp.tools_observe as to


def _analytics_payload(hours):
    return {
        "ok": True, "notice": None, "window_hours": hours,
        "totals": {"cache_read_tokens": 800, "tokens_in": 1000,
                   "cache_hit_rate": 80.0, "volume": 5, "cost_usd": 1.0},
        "buckets": ["b1"],
        "series": {"tokens_cached": [800], "tokens_uncached": [200], "volume": [5]},
        "per_agent": [{"agent_kind": "business", "cache_read_tokens": 800,
                       "tokens_in": 1000, "cache_hit_rate": 80.0, "volume": 5}],
    }


def test_caching_analytics_shape_clamp_and_savings(monkeypatch):
    captured = {}

    def fake_load_analytics(*, hours, agent_kind, environment):
        captured.update(hours=hours, agent_kind=agent_kind, environment=environment)
        return _analytics_payload(hours)

    monkeypatch.setattr(la_mod, "load_analytics", fake_load_analytics)
    monkeypatch.setattr(la_mod, "estimate_cache_savings_usd", lambda client, tok: 1.23)
    monkeypatch.setattr(to, "client_for_env", lambda env: object())

    out = to.caching_analytics(hours=999999, agent_kind="business",
                               environment="prod", env="prod")

    # hours clamped, only cache-relevant kwargs forwarded (no channel/status)
    assert captured == {"hours": 8760, "agent_kind": "business", "environment": "prod"}
    assert out["ok"] is True
    assert out["totals"] == {"cache_read_tokens": 800, "input_tokens": 1000,
                             "cache_hit_rate": 80.0, "cache_saved_usd": 1.23}
    assert out["series"] == {"tokens_cached": [800], "tokens_uncached": [200]}
    assert out["buckets"] == ["b1"]
    assert out["per_agent"] == [{"agent_kind": "business", "cache_read_tokens": 800,
                                 "input_tokens": 1000, "cache_hit_rate": 80.0}]
    assert out["filters"] == {"agent_kind": "business", "environment": "prod"}


def test_caching_analytics_rejects_bad_env_before_query(monkeypatch):
    called = {"load": False}

    def boom_load(**k):
        called["load"] = True
        return _analytics_payload(24)

    monkeypatch.setattr(la_mod, "load_analytics", boom_load)
    # validate_env(env) runs FIRST and raises ValueError, which @tool_errors
    # converts to {"error", "hint"} — the expensive load_analytics never runs.
    out = to.caching_analytics(env="bogus")
    assert "error" in out and "env" in out["error"]
    assert called["load"] is False  # failed fast, before the ClickHouse query


def test_caching_analytics_empty_when_clickhouse_unavailable(monkeypatch):
    empty = {"ok": False, "notice": "Analytics source (ClickHouse) is unavailable.",
             "window_hours": 24, "totals": {"cache_read_tokens": 0, "tokens_in": 0,
             "cache_hit_rate": 0.0}, "buckets": [], "series": {}, "per_agent": []}
    monkeypatch.setattr(la_mod, "load_analytics", lambda **k: empty)
    monkeypatch.setattr(la_mod, "estimate_cache_savings_usd", lambda client, tok: 0.0)
    monkeypatch.setattr(to, "client_for_env", lambda env: object())

    out = to.caching_analytics(env="staging")
    assert out["ok"] is False
    assert out["totals"]["cache_read_tokens"] == 0
    assert out["totals"]["cache_saved_usd"] == 0.0
    assert out["series"] == {"tokens_cached": [], "tokens_uncached": []}
    assert out["per_agent"] == []
