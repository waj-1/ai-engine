from core import pricing


def test_pricing_mode_defaults_to_auto():
    assert pricing.pricing_mode({"extra_config": {}}) == "auto"
    assert pricing.pricing_mode({"extra_config": {"pricing_mode": "manual"}}) == "manual"
    assert pricing.pricing_mode({}) == "auto"


def test_resolve_tokencost_key_uses_override_then_model_string():
    assert pricing.resolve_tokencost_key(
        {"model_string": "gemini-3-flash", "extra_config": {"tokencost_model": "gpt-4o"}}
    ) == "gpt-4o"
    # strips everything before the last "/" + an @version suffix
    assert pricing.resolve_tokencost_key(
        {"model_string": "publishers/anthropic/models/claude-3-5-sonnet@20240620"}
    ) == "claude-3-5-sonnet"
    # The slash split outlives the Vertex removal: `_build_openai` can point a
    # row at an OpenAI-compatible endpoint (OpenRouter etc.) whose ids are
    # `<vendor>/<model>`, so this normalization is still load-bearing for cost.
    assert pricing.resolve_tokencost_key(
        {"model_string": "anthropic/claude-3-5-sonnet"}
    ) == "claude-3-5-sonnet"
    assert pricing.resolve_tokencost_key({"model_string": ""}) is None


def test_cost_usd_auto_known_model(monkeypatch):
    # 1000 input @ $0.000001/tok = $0.001 ; 500 output @ $0.000002/tok = $0.001
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {
        "gpt-4o": {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}
    })
    model = {"model_string": "gpt-4o", "extra_config": {"pricing_mode": "auto"}}
    assert round(pricing.cost_usd(model, 1000, 500), 6) == 0.002


def test_cost_usd_auto_unknown_model_falls_back_to_manual(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {})
    model = {"model_string": "brand-new-model",
             "extra_config": {"pricing_mode": "auto",
                              "cost_per_1k_in": 1.0, "cost_per_1k_out": 2.0}}
    # 1000 in / 1000 * 1.0 + 1000 out / 1000 * 2.0 = 3.0
    assert pricing.cost_usd(model, 1000, 1000) == 3.0


def test_cost_usd_auto_unknown_no_manual_is_none(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {})
    model = {"model_string": "x", "extra_config": {"pricing_mode": "auto"}}
    assert pricing.cost_usd(model, 100, 100) is None


def test_cost_usd_manual_mode_ignores_tokencost(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {
        "gpt-4o": {"input_cost_per_token": 9.9, "output_cost_per_token": 9.9}
    })
    model = {"model_string": "gpt-4o",
             "extra_config": {"pricing_mode": "manual",
                              "cost_per_1k_in": 1.0, "cost_per_1k_out": 1.0}}
    assert pricing.cost_usd(model, 1000, 1000) == 2.0


def test_effective_rates_auto(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {
        "gpt-4o": {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}
    })
    rin, rout = pricing.effective_rates({"model_string": "gpt-4o", "extra_config": {}})
    assert round(rin, 6) == 0.001 and round(rout, 6) == 0.002


# A fake tokencost row: $1/1M input, $2/1M output, $0.25/1M cache-read.
_FAKE = {
    "input_cost_per_token": 0.000001,
    "output_cost_per_token": 0.000002,
    "cache_read_input_token_cost": 0.00000025,
}
_MODEL = {"model_string": "fake-model", "extra_config": {"pricing_mode": "auto"}}


def test_cost_usd_backward_compatible(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"fake-model": _FAKE})
    # Old 2-arg call: 1000 in, 500 out → 1000*1e-6 + 500*2e-6 = 0.002
    assert abs(pricing.cost_usd(_MODEL, 1000, 500) - 0.002) < 1e-12


def test_cost_usd_discounts_cache_read(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"fake-model": _FAKE})
    # 1000 total in, 800 cache_read, 0 creation, 500 out:
    # uncached=200 → 200*1e-6 + 800*0.25e-6 + 500*2e-6 = 0.0002+0.0002+0.001 = 0.0014
    got = pricing.cost_usd(_MODEL, 1000, 500, cache_read_tokens=800)
    assert abs(got - 0.0014) < 1e-12


def test_cache_savings_usd(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"fake-model": _FAKE})
    # 800 * (1e-6 - 0.25e-6) = 800 * 0.75e-6 = 0.0006
    assert abs(pricing.cache_savings_usd(_MODEL, 800) - 0.0006) < 1e-12


def test_cache_savings_usd_unknown_model(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {})
    assert pricing.cache_savings_usd(_MODEL, 800) == 0.0


# Explicit 0.0 cache-read rate (free cache reads, e.g. some real Gemini models)
# must be honored as-is, NOT treated as "missing" and fall back to the input rate.
_FAKE_FREE_CACHE = {
    "input_cost_per_token": 0.000001,
    "output_cost_per_token": 0.000002,
    "cache_read_input_token_cost": 0.0,
}
_MODEL_FREE_CACHE = {"model_string": "free-cache-model", "extra_config": {"pricing_mode": "auto"}}

# Same model but the key is OMITTED entirely (genuinely unknown to tokencost for
# this field) -> should fall back to the input rate (no discount).
_FAKE_NO_CACHE_KEY = {
    "input_cost_per_token": 0.000001,
    "output_cost_per_token": 0.000002,
}
_MODEL_NO_CACHE_KEY = {"model_string": "no-cache-key-model", "extra_config": {"pricing_mode": "auto"}}


def test_cost_usd_explicit_zero_cache_read_rate_is_free(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"free-cache-model": _FAKE_FREE_CACHE})
    # 1000 total in, 800 cache_read, 0 creation, 500 out:
    # uncached=200 -> 200*1e-6 + 800*0.0 + 500*2e-6 = 0.0002 + 0 + 0.001 = 0.0012
    got = pricing.cost_usd(_MODEL_FREE_CACHE, 1000, 500, cache_read_tokens=800)
    assert abs(got - 0.0012) < 1e-12


def test_cache_savings_usd_explicit_zero_cache_read_rate_is_full_input_rate(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"free-cache-model": _FAKE_FREE_CACHE})
    # cache_read_input_token_cost is explicitly 0.0 -> full savings = cr * input_rate
    # 800 * 1e-6 = 0.0008 (NOT 0.0, which the buggy `or` fallback would produce)
    got = pricing.cache_savings_usd(_MODEL_FREE_CACHE, 800)
    assert abs(got - 0.0008) < 1e-12


def test_cost_usd_missing_cache_read_key_falls_back_to_input_rate(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"no-cache-key-model": _FAKE_NO_CACHE_KEY})
    # cache_read_input_token_cost key is OMITTED -> falls back to input rate (no discount).
    # uncached=200 -> 200*1e-6 + 800*1e-6 (fallback) + 500*2e-6 = 0.0002+0.0008+0.001 = 0.002
    got = pricing.cost_usd(_MODEL_NO_CACHE_KEY, 1000, 500, cache_read_tokens=800)
    assert abs(got - 0.002) < 1e-12


def test_cache_savings_usd_missing_cache_read_key_is_zero(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"no-cache-key-model": _FAKE_NO_CACHE_KEY})
    # No cache_read_input_token_cost key -> rcr falls back to rin -> savings = 0.
    got = pricing.cache_savings_usd(_MODEL_NO_CACHE_KEY, 800)
    assert abs(got - 0.0) < 1e-12


# Cache-creation path: distinct rate from input, currently uncovered.
_FAKE_WITH_CREATION = {
    "input_cost_per_token": 0.000001,
    "output_cost_per_token": 0.000002,
    "cache_read_input_token_cost": 0.00000025,
    "cache_creation_input_token_cost": 0.0000015,
}
_MODEL_WITH_CREATION = {"model_string": "creation-model", "extra_config": {"pricing_mode": "auto"}}


def test_cost_usd_prices_cache_creation_at_its_own_rate(monkeypatch):
    monkeypatch.setattr(pricing, "TOKEN_COSTS", {"creation-model": _FAKE_WITH_CREATION})
    # 1000 total in, 300 cache_read, 200 cache_creation, 500 out:
    # uncached = 1000-300-200 = 500
    # 500*1e-6 + 300*0.25e-6 + 200*1.5e-6 + 500*2e-6
    # = 0.0005 + 0.000075 + 0.0003 + 0.001 = 0.001875
    got = pricing.cost_usd(
        _MODEL_WITH_CREATION, 1000, 500, cache_read_tokens=300, cache_creation_tokens=200
    )
    assert abs(got - 0.001875) < 1e-12
