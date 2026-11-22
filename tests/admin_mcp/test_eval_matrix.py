from admin.services.eval_matrix import build_variant_matrix


def _scn(score, passed, ttft, lat, cost, err=None, tin=100, tout=50):
    return {"score": score, "passed": passed, "ttft_ms": ttft,
            "latency_ms": lat, "cost_usd": cost, "error": err,
            "input_tokens": tin, "output_tokens": tout}


def _run(run_id, suite_id, variant, model_label, scns):
    return {"run_id": run_id, "suite_id": suite_id, "profile_id": "p1",
            "status": "passed",
            "context": {"variant_label": variant, "model_label": model_label},
            "results": scns}


def test_matrix_aggregates_quality_speed_cost_per_variant():
    runs = [
        _run("r1", "s1", "baseline", "gemini-3-flash-preview",
             [_scn(1.0, True, 200, 900, 0.0010),
              _scn(0.6, False, 400, 1500, 0.0020)]),
        _run("r2", "s1", "3.6-flash", "gemini-3.6-flash",
             [_scn(1.0, True, 150, 700, 0.0011),
              _scn(0.9, True, 250, 1100, 0.0013)]),
    ]
    m = build_variant_matrix(runs, pass_threshold=0.7)
    rows = {(r["suite_id"], r["variant_label"]): r for r in m["rows"]}

    base = rows[("s1", "baseline")]
    assert base["n_scenarios"] == 2
    assert base["mean_score"] == 0.8
    assert base["pass_rate"] == 0.5
    assert base["p50_latency_ms"] == 900  # nearest-rank median of [900,1500]
    assert round(base["total_cost_usd"], 4) == 0.0030

    fast = rows[("s1", "3.6-flash")]
    assert fast["mean_score"] == 0.95
    assert fast["pass_rate"] == 1.0
    assert fast["p95_ttft_ms"] == 250

    assert set(m["by_variant"]) == {"baseline", "3.6-flash"}
    assert m["by_variant"]["3.6-flash"]["pass_rate"] == 1.0

    # M1: the aggregated by_variant view must also carry token totals —
    # previously only the per-suite `rows` entries did, so the aggregated
    # matrix view silently showed "—" for tokens in/out.
    assert m["by_variant"]["baseline"]["total_input_tokens"] == 200  # 2 scenarios * 100
    assert m["by_variant"]["baseline"]["total_output_tokens"] == 100  # 2 scenarios * 50
    assert m["by_variant"]["3.6-flash"]["total_input_tokens"] == 200
    assert m["by_variant"]["3.6-flash"]["total_output_tokens"] == 100


def test_matrix_handles_null_cost_and_errored_scenarios():
    runs = [_run("r3", "s2", "lite", "gemini-3.5-flash-lite",
                 [_scn(0.0, False, 0, 0, None, err="boom"),
                  _scn(1.0, True, 100, 500, 0.0005)])]
    m = build_variant_matrix(runs)
    row = m["rows"][0]
    assert row["errored"] == 1
    assert row["priced_scenarios"] == 1          # null cost excluded from pricing
    assert round(row["total_cost_usd"], 4) == 0.0005
    assert row["n_scenarios"] == 2


def test_eval_runs_matrix_action(monkeypatch):
    import admin_mcp.tools_test as tt

    fake_runs = [
        {"run_id": "r1", "suite_id": "s1", "profile_id": "p1", "status": "passed",
         "context": {"variant_label": "baseline", "model_label": "old"},
         "results": [{"score": 0.8, "passed": True, "ttft_ms": 100,
                      "latency_ms": 500, "cost_usd": 0.001,
                      "input_tokens": 10, "output_tokens": 5, "error": None}]},
    ]

    class _Resp:
        def __init__(self, data):
            self.data = data

    class _Q:
        def __init__(self, data):
            self._data = data

        def select(self, *a, **k):
            return self

        def in_(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def order(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            return _Resp(self._data)

    class _Client:
        def table(self, name):
            return _Q(fake_runs)

    monkeypatch.setattr(tt, "client_for_env", lambda env: _Client())
    out = tt.eval_runs(action="matrix", run_ids=["r1"], env="prod")
    assert "matrix" in out
    assert out["matrix"]["by_variant"]["baseline"]["mean_score"] == 0.8
