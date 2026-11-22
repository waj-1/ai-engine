"""pass@k and pass^k over an eval run's repeated attempts.

A run with `repeats = n` holds n attempts per scenario. For one scenario with
c passing attempts out of n, the unbiased estimates for k <= n are:

  pass@k = 1 - C(n - c, k) / C(n, k)   # at least one of k sampled attempts passes
  pass^k = C(c, k) / C(n, k)           # all k sampled attempts pass

With k == n these are exact: pass@k is 1 iff any attempt passed, pass^k is 1
iff every attempt passed. The run-level number is the mean over scenarios, so
"pass@3 = 0.92" reads "92% of scenarios passed at least once in 3 attempts"
and "pass^3 = 0.62" reads "62% passed all 3 times". The gap between the two is
how flaky the agent is: pass@k says what it CAN do, pass^k what it does
RELIABLY — and every real customer gets one attempt.

A scenario with fewer attempts than k (a cancelled run) is measured at its
own attempt count rather than dropped, so a partial run still reports.
"""
from __future__ import annotations

from math import comb
from typing import Any, Dict, Iterable, List, Optional, Tuple


def pass_at_k(c: int, n: int, k: int) -> float:
    k = min(k, n)
    if k <= 0:
        return 0.0
    # Subtract in integers before dividing: `1.0 - a/b` rounds (1 - 4/5 comes
    # out 0.19999…), which can put pass@k a hair under pass^k for k = 1.
    total = comb(n, k)
    return (total - comb(n - c, k)) / total


def pass_hat_k(c: int, n: int, k: int) -> float:
    k = min(k, n)
    if k <= 0:
        return 0.0
    return comb(c, k) / comb(n, k)


def _scenario_key(r: Dict[str, Any]) -> Any:
    return r.get("scenario_id") or r.get("name") or r.get("question") or id(r)


def scenario_counts(results: Iterable[Dict[str, Any]]) -> Dict[Any, Tuple[int, int, Optional[str]]]:
    """scenario key -> (passes c, attempts n, category), first-seen order."""
    out: Dict[Any, List[Any]] = {}
    for r in results or []:
        if not isinstance(r, dict):
            continue
        entry = out.setdefault(_scenario_key(r), [0, 0, r.get("category")])
        entry[1] += 1
        if r.get("passed"):
            entry[0] += 1
    return {key: (c, n, cat) for key, (c, n, cat) in out.items()}


def k_for(results: Iterable[Dict[str, Any]]) -> int:
    """The k a run supports: the attempts per scenario (the most common count,
    so one cancelled scenario doesn't drag k down for the rest)."""
    counts = [n for _c, n, _cat in scenario_counts(results).values()]
    if not counts:
        return 0
    return max(set(counts), key=lambda n: (counts.count(n), n))


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 3) if values else None


def pass_k_summary(results: Iterable[Dict[str, Any]], k: Optional[int] = None) -> Dict[str, Any]:
    """{"k", "scenarios", "pass_at_k", "passhat_k", "by_category": [...]}.

    `by_category` rows: {"category", "scenarios", "pass_at_k", "passhat_k"},
    sorted by category name ("uncategorised" last)."""
    results = [r for r in (results or []) if isinstance(r, dict)]
    counts = scenario_counts(results)
    k = k or k_for(results)
    at: List[float] = []
    hat: List[float] = []
    by_cat: Dict[str, Tuple[List[float], List[float]]] = {}
    for c, n, cat in counts.values():
        a, h = pass_at_k(c, n, k), pass_hat_k(c, n, k)
        at.append(a)
        hat.append(h)
        bucket = by_cat.setdefault(cat or "uncategorised", ([], []))
        bucket[0].append(a)
        bucket[1].append(h)
    rows = [
        {"category": cat, "scenarios": len(a), "pass_at_k": _mean(a), "passhat_k": _mean(h)}
        for cat, (a, h) in sorted(by_cat.items(), key=lambda kv: (kv[0] == "uncategorised", kv[0]))
    ]
    return {
        "k": k,
        "scenarios": len(counts),
        "pass_at_k": _mean(at),
        "passhat_k": _mean(hat),
        "by_category": rows,
    }
