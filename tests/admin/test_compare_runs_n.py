from admin import eval_handlers as eh


def _run(run_id, suite_id, label, results, model_label=None):
    ctx = {"variant_label": label}
    if model_label:
        ctx["model_label"] = model_label
    return {"run_id": run_id, "suite_id": suite_id, "profile_id": "p", "context": ctx,
            "results": results, "score": None}


def test_variant_label_prefers_explicit():
    assert eh._variant_label_for_run({"context": {"variant_label": "X"}}) == "X"
    assert eh._variant_label_for_run({"context": {"model_label": "Gemini"}}) == "Gemini"
    assert eh._variant_label_for_run({"context": {}}) == "baseline"


def test_compare_two_variants_one_suite_pass_rate_and_cost():
    a = _run("a", "s1", "Gemini", [
        {"scenario_id": "1", "passed": True, "score": 1.0, "ttft_ms": 100,
         "total_ms": 1000, "cost_usd": 0.01, "input_tokens": 10, "output_tokens": 5,
         "judge": {"score": 1.0}},
        {"scenario_id": "2", "passed": False, "score": 0.0, "ttft_ms": 200,
         "total_ms": 2000, "cost_usd": 0.02, "input_tokens": 20, "output_tokens": 10,
         "judge": {"score": 0.0}},
    ])
    b = _run("b", "s1", "Claude", [
        {"scenario_id": "1", "passed": True, "score": 1.0, "ttft_ms": 50,
         "total_ms": 500, "cost_usd": 0.05, "input_tokens": 10, "output_tokens": 5,
         "judge": {"score": 1.0}},
        {"scenario_id": "2", "passed": True, "score": 1.0, "ttft_ms": 60,
         "total_ms": 600, "cost_usd": 0.06, "input_tokens": 20, "output_tokens": 10,
         "judge": {"score": 1.0}},
    ])
    out = eh._compare_runs_n([a, b])
    assert [c["label"] for c in out["columns"]] == ["Gemini", "Claude"]
    suite = out["suites"][0]
    pr = next(r for r in suite["rows"] if r["metric"] == "pass_rate")
    assert pr["values"] == [0.5, 1.0]
    assert pr["best_index"] == 1            # higher pass rate wins
    cost = next(r for r in out["global_rows"] if r["metric"] == "cost_total")
    assert cost["values"] == [0.03, 0.11]
    assert cost["best_index"] == 0          # lower cost wins (lower_is_better)


def test_compare_adds_pass_at_k_and_passhat_k():
    a = _run("a", "s1", "Gemini", [
        {"scenario_id": "1", "passed": True, "score": 1.0, "ttft_ms": 1, "total_ms": 1,
         "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        {"scenario_id": "2", "passed": False, "score": 0.0, "ttft_ms": 1, "total_ms": 1,
         "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
    ])
    out = eh._compare_runs_n([a])
    rows = {r["metric"] for s in out["suites"] for r in s["rows"]}
    assert "pass_at_1" in rows and "passhat_k" in rows


def test_columns_carry_model_and_prompt_version():
    a = {"run_id": "a", "suite_id": "s", "profile_id": "p",
         "context": {"variant_label": "V", "model_override": {"model_string": "claude-haiku-4-5"}, "prompt_version": "v3"},
         "results": [{"scenario_id": "1", "passed": True, "score": 1.0, "ttft_ms": 5, "total_ms": 9,
                      "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "iterations": 2, "tool_count": 1}]}
    b = dict(a, run_id="b")
    out = eh._compare_runs_n([a, b])
    assert out["columns"][0]["model"] == "claude-haiku-4-5"
    assert out["columns"][0]["prompt_version"] == "v3"
    metrics = {r["metric"] for r in out["global_rows"]}
    assert {"iterations", "tool_count"} <= metrics


def test_missing_suite_cell_is_none():
    a = _run("a", "s1", "Gemini", [{"scenario_id": "1", "passed": True, "score": 1.0,
                                    "ttft_ms": 1, "total_ms": 1, "cost_usd": 0.0,
                                    "input_tokens": 0, "output_tokens": 0}])
    b = _run("b", "s2", "Claude", [{"scenario_id": "1", "passed": True, "score": 1.0,
                                    "ttft_ms": 1, "total_ms": 1, "cost_usd": 0.0,
                                    "input_tokens": 0, "output_tokens": 0}])
    out = eh._compare_runs_n([a, b])
    suite_ids = {s["suite_id"] for s in out["suites"]}
    assert suite_ids == {"s1", "s2"}
    s1 = next(s for s in out["suites"] if s["suite_id"] == "s1")
    pr = next(r for r in s1["rows"] if r["metric"] == "pass_rate")
    assert pr["values"] == [1.0, None]      # Claude didn't run s1

def test_passhat_k_repeated_attempts_measures_consistency():
    # scenario 1 passes all 3 attempts; scenario 2 passes 2 of 3 (flaky)
    res = [
        {"scenario_id": "1", "passed": True, "score": 1.0},
        {"scenario_id": "1", "passed": True, "score": 1.0},
        {"scenario_id": "1", "passed": True, "score": 1.0},
        {"scenario_id": "2", "passed": True, "score": 1.0},
        {"scenario_id": "2", "passed": True, "score": 1.0},
        {"scenario_id": "2", "passed": False, "score": 0.0},
    ]
    out = eh._compare_runs_n([_run("a", "s1", "M", res)])
    rows = {r["metric"]: r["values"][0] for s in out["suites"] for r in s["rows"]}
    assert abs(rows["pass_rate"] - 5 / 6) < 1e-9      # fraction of ATTEMPTS passed
    assert rows["passhat_k"] == 0.5                   # scen1 reliable(1.0) + scen2 flaky(0.0) -> 0.5


def test_passhat_k_single_attempt_equals_pass_rate_not_zero():
    # one attempt per scenario: no consistency signal -> passhat_k == pass_rate,
    # NOT the old all-or-nothing 0.0 that zeroed the moment any scenario failed
    res = [
        {"scenario_id": "1", "passed": True, "score": 1.0},
        {"scenario_id": "2", "passed": False, "score": 0.0},
    ]
    out = eh._compare_runs_n([_run("a", "s1", "M", res)])
    rows = {r["metric"]: r["values"][0] for s in out["suites"] for r in s["rows"]}
    assert rows["pass_rate"] == 0.5
    assert rows["passhat_k"] == 0.5

def test_run_detail_kpis_aggregates_results():
    from admin.eval_handlers import run_detail_kpis
    res = [
        {"ttft_ms": 2000, "total_ms": 9000, "cost_usd": 0.05, "input_tokens": 1000,
         "output_tokens": 300, "iterations": 3, "tool_count": 1},
        {"ttft_ms": 3000, "total_ms": 12000, "cost_usd": 0.07, "input_tokens": 1200,
         "output_tokens": 400, "iterations": 2, "tool_count": 2},
    ]
    k = run_detail_kpis(res)
    assert k["scenarios"] == 2
    assert k["ttft_p50_ms"] == 2500 and k["ttlt_p50_ms"] == 10500
    assert k["cost_usd"] == 0.12
    assert k["tokens_in"] == 2200 and k["tokens_out"] == 700 and k["tokens_total"] == 2900
    assert k["avg_iterations"] == 2.5 and k["avg_tool_count"] == 1.5
    assert run_detail_kpis([]) is None


def test_run_detail_kpis_pass_hat_k_over_repeated_attempts():
    """With repeats>1 the detail KPIs expose pass^k (scenarios where EVERY
    attempt passed) distinct from pass@1 (attempt-level pass rate)."""
    from admin.eval_handlers import run_detail_kpis
    res = (
        [{"scenario_id": "A", "passed": True, "score": 1.0}] * 3 +   # all pass
        [{"scenario_id": "B", "passed": True, "score": 1.0}] * 2 +   # 2/3 pass
        [{"scenario_id": "B", "passed": False, "score": 0.0}]
    )
    k = run_detail_kpis(res)
    assert k["distinct_scenarios"] == 2
    assert k["total_attempts"] == 6
    assert k["repeated"] is True
    assert k["passhat_k"] == 0.5            # only A passes all attempts
    assert round(k["pass_at_1"], 3) == 0.833  # 5/6 attempts passed


def test_run_detail_kpis_single_attempt_passhat_equals_pass_rate():
    from admin.eval_handlers import run_detail_kpis
    res = [{"scenario_id": "A", "passed": True, "score": 1.0},
           {"scenario_id": "B", "passed": False, "score": 0.0}]
    k = run_detail_kpis(res)
    assert k["repeated"] is False
    assert k["passhat_k"] == 0.5 and k["pass_at_1"] == 0.5


def test_run_chart_data_is_minimal_and_json_safe():
    """Charts only need numeric fields. The projection must drop the heavy
    `actual`/`reason` markdown (whose embedded newlines broke JSON.parse and
    blanked the canvases) and stay strictly scalar."""
    import json
    from admin.eval_handlers import run_chart_data
    res = [{"scenario_id": "A", "passed": True, "score": 0.9,
            "latency_ms": 1200, "judge": {"score": 0.9},
            "actual": "line one\n\nline two **md**", "reason": "x\ny"}]
    data = run_chart_data(res)
    assert data == [{"label": "A", "passed": True, "score": 0.9,
                     "latency_ms": 1200, "judge_score": 0.9}]
    # Round-trips cleanly (no control chars, no heavy text).
    json.loads(json.dumps(data))


def test_group_results_by_scenario_collapses_attempts():
    from admin.eval_handlers import group_results_by_scenario
    res = [
        {"scenario_id": "A", "question": "q1", "passed": True, "score": 1.0, "attempt": 0},
        {"scenario_id": "A", "question": "q1", "passed": False, "score": 0.0, "attempt": 1},
        {"scenario_id": "B", "question": "q2", "passed": True, "score": 1.0, "attempt": 0},
    ]
    groups = group_results_by_scenario(res)
    assert len(groups) == 2
    a = next(g for g in groups if g["scenario_id"] == "A")
    assert a["n"] == 2 and a["passes"] == 1 and a["consistent"] is False
    assert len(a["attempts"]) == 2
    b = next(g for g in groups if g["scenario_id"] == "B")
    assert b["n"] == 1 and b["passes"] == 1 and b["consistent"] is True
