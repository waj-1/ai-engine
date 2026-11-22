import json

from admin import evals


# ---------------------------------------------------------------------------
# New tests: exact iterations/tool_count from usage event (agents that track them)
# ---------------------------------------------------------------------------

def test_usage_event_exact_counts_preferred_over_proxy():
    """When the usage event carries iterations/tool_count the runner must
    report those exact values, NOT the proxy-counted values."""
    # The SSE body only has thought events (no tool/tool_call/tool_result/action),
    # so the proxy would return tool_count=0, iterations=1.
    # But the usage event says iterations=3, tool_count=2 — those must win.
    body = (
        'data: {"type":"thought","content":"using get_transactions"}\n\n'
        'data: {"type":"thought","content":"retrieved data"}\n\n'
        'data: {"type":"thought","content":"using render_chart"}\n\n'
        'data: {"type":"thought","content":"retrieved data"}\n\n'
        'data: {"type":"answer","content":"Here is the chart"}\n\n'
        'data: {"type":"usage","input_tokens":500,"output_tokens":120,'
        '"iterations":3,"tool_count":2}\n\n'
        'data: {"type":"done"}\n\n'
    )
    # Proxy would get tool_count=0 (no tool/tool_call/tool_result/action events).
    proxy = evals._extract_run_metrics(body)
    assert proxy["tool_count"] == 0, "precondition: proxy undercounts thought events"

    # _extract_usage_from_sse should surface the exact values.
    usage = evals._extract_usage_from_sse(body)
    assert usage["iterations"] == 3
    assert usage["tool_count"] == 2

    # Simulate what _invoke_endpoint builds for metrics["runmetrics"].
    _proxy = evals._extract_run_metrics(body)
    runmetrics = {
        "iterations": usage.get("iterations", _proxy["iterations"]),
        "tool_count": usage.get("tool_count", _proxy["tool_count"]),
    }
    assert runmetrics["iterations"] == 3
    assert runmetrics["tool_count"] == 2


def test_usage_event_no_exact_counts_falls_back_to_proxy():
    """When the usage event does NOT carry iterations/tool_count the runner
    must fall back to the SSE-proxy — back-compatible with existing agents."""
    body = (
        'data: {"type":"tool_call","tool":"search_products"}\n\n'
        'data: {"type":"tool_result","tool":"search_products"}\n\n'
        'data: {"type":"answer","content":"Found items"}\n\n'
        'data: {"type":"usage","input_tokens":100,"output_tokens":30}\n\n'
        'data: {"type":"done"}\n\n'
    )
    usage = evals._extract_usage_from_sse(body)
    # No exact counts in usage event.
    assert "iterations" not in usage
    assert "tool_count" not in usage

    _proxy = evals._extract_run_metrics(body)
    runmetrics = {
        "iterations": usage.get("iterations", _proxy["iterations"]),
        "tool_count": usage.get("tool_count", _proxy["tool_count"]),
    }
    # Proxy sees 1 tool_call + 1 tool_result = 2 events → tool_rounds=(2+1)//2=1
    assert runmetrics["tool_count"] == 1
    assert runmetrics["iterations"] == 2


# ---------------------------------------------------------------------------
# Original tests (unchanged)
# ---------------------------------------------------------------------------

def test_extract_run_metrics_counts_tools_and_iterations():
    body = (
        'data: {"type":"thought","content":"using get_transactions"}\n\n'
        'data: {"type":"tool_result","tool":"get_transactions"}\n\n'
        'data: {"type":"answer","content":"hi"}\n\n'
        'data: {"type":"done"}\n\n'
    )
    m = evals._extract_run_metrics(body)
    assert m["tool_count"] == 1
    assert m["iterations"] == 2   # 1 tool round-trip + the final answer pass


def test_extract_run_metrics_no_tools():
    body = 'data: {"type":"answer","content":"x"}\n\ndata: {"type":"done"}\n\n'
    assert evals._extract_run_metrics(body) == {"iterations": 1, "tool_count": 0}


def test_extract_usage_from_sse_body():
    body = (
        'data: {"type":"answer","content":"hi"}\n\n'
        'data: {"type":"usage","input_tokens":120,"output_tokens":34}\n\n'
        'data: {"type":"done"}\n\n'
    )
    usage = evals._extract_usage_from_sse(body)
    assert usage == {"input_tokens": 120, "output_tokens": 34}


def test_extract_usage_absent_returns_zeros():
    assert evals._extract_usage_from_sse('data: {"type":"answer","content":"x"}\n\n') == \
        {"input_tokens": 0, "output_tokens": 0}


def test_run_scenario_captures_metrics_and_cost(monkeypatch):
    # Mock _invoke_endpoint: fill the metrics out-param + return the unchanged 4-tuple.
    def fake_invoke(metrics=None, **k):
        if metrics is not None:
            metrics["ttft_ms"] = 50
            metrics["usage"] = {"input_tokens": 10, "output_tokens": 5}
        return ("Hello there", "", 200, 42)
    monkeypatch.setattr(evals, "_invoke_endpoint", fake_invoke)
    monkeypatch.setattr(
        "core.pricing.TOKEN_COSTS",
        {"claude-x": {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002}},
        raising=False,
    )
    result = evals._run_scenario(
        {"id": "s1", "question": "hi", "expected_substrings": ["Hello"]},
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
        model_override={"model_string": "claude-x", "extra_config": {"pricing_mode": "auto"}},
    )
    assert result["ttft_ms"] == 50
    assert result["input_tokens"] == 10 and result["output_tokens"] == 5
    assert round(result["cost_usd"], 6) == 0.02   # 10*0.001 + 5*0.002
    assert "total_ms" in result


def test_run_scenario_prices_baseline_against_profile_model(monkeypatch):
    """Baseline runs (no model_override) must still compute cost_usd from the
    profile's model row — spec §5.4 (FIX 2)."""
    def fake_invoke(metrics=None, **k):
        if metrics is not None:
            metrics["ttft_ms"] = 30
            metrics["usage"] = {"input_tokens": 10, "output_tokens": 5}
        return ("Hello there", "", 200, 30)

    monkeypatch.setattr(evals, "_invoke_endpoint", fake_invoke)
    monkeypatch.setattr(
        "core.pricing.TOKEN_COSTS",
        {"claude-x": {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002}},
        raising=False,
    )
    result = evals._run_scenario(
        {"id": "s2", "question": "hi", "expected_substrings": ["Hello"]},
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
        model_override=None,  # baseline — no override
        profile_model={"model_string": "claude-x", "extra_config": {"pricing_mode": "auto"}},
    )
    # 10*0.001 + 5*0.002 == 0.02 — same arithmetic, priced against profile_model
    assert result["cost_usd"] is not None, "baseline run must have cost_usd, got None"
    assert round(result["cost_usd"], 6) == 0.02
