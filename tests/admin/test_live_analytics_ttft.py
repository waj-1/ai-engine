from admin.screens import live_analytics as la
from datetime import timedelta

def test_bucket_series_has_ttft_ttlt():
    rows=[{"created_at":"2026-06-17T10:00:00","latency_ms":100,"ttft_ms":40,"ttlt_ms":90,"agent_kind":"orchard"},
          {"created_at":"2026-06-17T10:00:01","latency_ms":200,"ttft_ms":60,"ttlt_ms":180,"agent_kind":"orchard"}]
    out=la.bucket_series(rows, timedelta(hours=1), {})
    assert "ttft_p50" in out["series"] and "ttlt_p50" in out["series"]
    assert out["series"]["ttft_p50"][0] is not None
