from datetime import timedelta

from admin.screens import live_analytics as la


def _row(created_at, agent, input_tokens, cache_read):
    return {"created_at": created_at, "agent_kind": agent,
            "input_tokens": input_tokens, "output_tokens": 10,
            "cache_read_tokens": cache_read, "latency_ms": 100.0,
            "iteration_count": 1, "error": False, "ttft_ms": None, "ttlt_ms": None}


def test_totals_cache_hit_rate():
    rows = [_row("2026-07-08T10:00:00", "business", 1000, 800),
            _row("2026-07-08T10:01:00", "sales", 1000, 200)]
    t = la._totals(rows, {})
    assert t["cache_read_tokens"] == 1000
    # 1000 cache_read / 2000 input = 50%
    assert t["cache_hit_rate"] == 50.0


def test_bucket_series_has_cache_split():
    rows = [_row("2026-07-08T10:00:00", "business", 1000, 800)]
    s = la.bucket_series(rows, timedelta(hours=1), {})["series"]
    assert s["tokens_cached"] == [800]
    assert s["tokens_uncached"] == [200]


def test_per_agent_cache_hit_rate():
    rows = [_row("2026-07-08T10:00:00", "business", 1000, 750)]
    pa = la.per_agent_rollup(rows, {})
    assert pa[0]["cache_read_tokens"] == 750
    assert pa[0]["cache_hit_rate"] == 75.0
