"""Unit tests for estimate_cache_savings_usd — the shared cache-savings
estimator used by the Live Monitor KPI and the caching_analytics MCP tool."""
import admin.screens.live_analytics as la
import core.pricing as pricing

# $1/1M input, $0.25/1M cache-read → a cache read saves $0.75/1M.
_FAKE = {"input_cost_per_token": 0.000001, "cache_read_input_token_cost": 0.00000025}


class _Query:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("Resp", (), {"data": self._data})()


class _Client:
    def __init__(self, data):
        self._data = data

    def table(self, *a, **k):
        return _Query(self._data)


def test_estimate_prices_with_primary_active_model(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"m": _FAKE})
    client = _Client([{"model_string": "m", "extra_config": {"pricing_mode": "auto"}}])
    # 1,000,000 * (1e-6 - 0.25e-6) = 1,000,000 * 0.75e-6 = 0.75
    assert la.estimate_cache_savings_usd(client, 1_000_000) == 0.75


def test_estimate_no_active_model_returns_zero(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"m": _FAKE})
    assert la.estimate_cache_savings_usd(_Client([]), 1_000_000) == 0.0


def test_estimate_swallows_client_error():
    class Boom:
        def table(self, *a, **k):
            raise RuntimeError("db down")

    assert la.estimate_cache_savings_usd(Boom(), 1_000_000) == 0.0


def test_estimate_zero_tokens_is_zero(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"m": _FAKE})
    client = _Client([{"model_string": "m", "extra_config": {"pricing_mode": "auto"}}])
    assert la.estimate_cache_savings_usd(client, 0) == 0.0


def test_estimate_returns_unrounded_subcent(monkeypatch):
    # Contract: the helper returns the RAW estimate (callers round for display),
    # so a sub-cent-but-nonzero saving survives as truthy — this is what makes
    # the Live Monitor's `round(saved, 2) if saved else "—"` show "0.0" (not "—")
    # for tiny savings, matching the pre-refactor behavior.
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"m": _FAKE})
    client = _Client([{"model_string": "m", "extra_config": {"pricing_mode": "auto"}}])
    got = la.estimate_cache_savings_usd(client, 4000)  # 4000 * 0.75e-6 = 0.003
    assert 0.0 < got < 0.005
