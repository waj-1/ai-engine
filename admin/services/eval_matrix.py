"""Roll a set of eval runs into a per-variant quality/speed/cost matrix.

Pure aggregation over the `results` list each `agent_eval_runs` row already
carries (per-scenario score/passed/ttft_ms/latency_ms/cost_usd/tokens). No DB
or pricing calls — `cost_usd` is precomputed by the suite runner. Used by the
`eval_runs action=matrix` MCP tool to answer 'compare performance and speed
and eval results' across model variants.
"""
from __future__ import annotations

import math
from statistics import mean
from typing import Any, Dict, List


def _percentile(values: List[float], p: float) -> int:
    """Nearest-rank percentile (int ms). Empty -> 0."""
    if not values:
        return 0
    s = sorted(values)
    k = max(1, math.ceil((p / 100.0) * len(s)))
    return int(s[min(k, len(s)) - 1])


def _agg(scenarios: List[Dict[str, Any]]) -> Dict[str, Any]:
    scores = [float(s.get("score") or 0.0) for s in scenarios]
    passed = [1 for s in scenarios if s.get("passed")]
    lat = [float(s["latency_ms"]) for s in scenarios if s.get("latency_ms")]
    ttft = [float(s["ttft_ms"]) for s in scenarios if s.get("ttft_ms")]
    costs = [float(s["cost_usd"]) for s in scenarios if s.get("cost_usd") is not None]
    tin = sum(int(s.get("input_tokens") or 0) for s in scenarios)
    tout = sum(int(s.get("output_tokens") or 0) for s in scenarios)
    n = len(scenarios)
    return {
        "n_scenarios": n,
        "errored": sum(1 for s in scenarios if s.get("error")),
        "mean_score": round(mean(scores), 4) if scores else 0.0,
        "pass_rate": round(len(passed) / n, 4) if n else 0.0,
        "p50_latency_ms": _percentile(lat, 50),
        "p95_latency_ms": _percentile(lat, 95),
        "p50_ttft_ms": _percentile(ttft, 50),
        "p95_ttft_ms": _percentile(ttft, 95),
        "total_input_tokens": tin,
        "total_output_tokens": tout,
        "total_cost_usd": round(sum(costs), 6),
        "mean_cost_usd": round(mean(costs), 6) if costs else 0.0,
        "priced_scenarios": len(costs),
    }


def build_variant_matrix(runs: List[Dict[str, Any]],
                         *, pass_threshold: float = 0.7) -> Dict[str, Any]:
    """Group runs by (suite_id, variant_label) and by variant, returning
    aggregated quality/speed/cost per cell and per variant."""
    by_cell: Dict[tuple, List[Dict[str, Any]]] = {}
    by_var: Dict[str, List[Dict[str, Any]]] = {}
    labels: Dict[str, str] = {}
    run_counts: Dict[tuple, int] = {}

    for run in runs:
        ctx = run.get("context") or {}
        variant = ctx.get("variant_label") or "—"
        model_label = ctx.get("model_label") or ""
        labels.setdefault(variant, model_label)
        suite_id = run.get("suite_id") or ""
        scns = run.get("results") or []
        if not isinstance(scns, list):
            continue
        by_cell.setdefault((suite_id, variant), []).extend(scns)
        by_var.setdefault(variant, []).extend(scns)
        run_counts[(suite_id, variant)] = run_counts.get((suite_id, variant), 0) + 1

    rows = []
    for (suite_id, variant), scns in sorted(by_cell.items()):
        agg = _agg(scns)
        rows.append({"suite_id": suite_id, "variant_label": variant,
                     "model_label": labels.get(variant, ""),
                     "n_runs": run_counts[(suite_id, variant)],
                     "pass_threshold": pass_threshold, **agg})

    by_variant = {}
    for variant, scns in by_var.items():
        agg = _agg(scns)
        by_variant[variant] = {"model_label": labels.get(variant, ""),
                               "n_scenarios": agg["n_scenarios"],
                               "mean_score": agg["mean_score"],
                               "pass_rate": agg["pass_rate"],
                               "p50_latency_ms": agg["p50_latency_ms"],
                               "p95_latency_ms": agg["p95_latency_ms"],
                               "p50_ttft_ms": agg["p50_ttft_ms"],
                               "p95_ttft_ms": agg["p95_ttft_ms"],
                               "total_input_tokens": agg["total_input_tokens"],
                               "total_output_tokens": agg["total_output_tokens"],
                               "total_cost_usd": agg["total_cost_usd"],
                               "mean_cost_usd": agg["mean_cost_usd"],
                               # 0 = cost was never captured for this variant
                               # (display layers show '—', not $0).
                               "priced_scenarios": agg["priced_scenarios"]}
    return {"rows": rows, "by_variant": by_variant}
