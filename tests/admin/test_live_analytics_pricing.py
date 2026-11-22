from admin.screens import live_analytics


def test_cost_for_row_prefers_pricing_layer_over_laminar(monkeypatch):
    # auto-priced model → pricing layer wins even if Laminar precomputed a cost
    monkeypatch.setattr(live_analytics, "_model_pricing", {
        "m1": {"model_string": "gpt-4o", "extra_config": {"pricing_mode": "auto"}}
    })
    monkeypatch.setattr("core.pricing.TOKEN_COSTS", {
        "gpt-4o": {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}
    }, raising=False)
    row = {"model_id": "m1", "input_tokens": 1000, "output_tokens": 1000, "cost_usd": 99.0}
    # 0.001 + 0.002 = 0.003, NOT the laminar 99.0
    assert round(live_analytics.cost_for_row(row, {}), 6) == 0.003


def test_cost_for_row_falls_back_to_laminar_when_unpriced(monkeypatch):
    monkeypatch.setattr(live_analytics, "_model_pricing", {})
    row = {"model_id": "unknown", "input_tokens": 10, "output_tokens": 10, "cost_usd": 1.23}
    assert live_analytics.cost_for_row(row, {}) == 1.23
