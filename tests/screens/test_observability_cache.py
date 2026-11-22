from admin.screens import observability as ob


def test_overview_cache_aggregation(monkeypatch):
    # Two LLM spans (span_type=1) with cache_read; a non-LLM tool span (span_type=3)
    # with a deliberately non-zero cache_read_tokens (999) so the assertion below
    # only holds if load_trace_overview_json truly filters to span_type == 1 —
    # dropping that filter would sum in the 999 and push the total to 1799.
    fake_spans = [
        {"span_id": "a", "name": "llm", "span_type": 1, "cache_read_tokens": 500,
         "cache_creation_tokens": 0, "has_error": 0, "input": "", "output": ""},
        {"span_id": "b", "name": "llm", "span_type": 1, "cache_read_tokens": 300,
         "cache_creation_tokens": 0, "has_error": 0, "input": "", "output": ""},
        {"span_id": "c", "name": "search.tool", "span_type": 3, "cache_read_tokens": 999,
         "cache_creation_tokens": 0, "has_error": 0, "input": "{}", "output": "{}"},
    ]
    monkeypatch.setattr(ob, "_clickhouse_spans", lambda _tid: fake_spans)
    # No trace aggregate row — the cache numbers come purely from the spans.
    monkeypatch.setattr(ob, "ch_get_trace", lambda _tid: None)
    out = ob.load_trace_overview_json("trace-1")
    assert out["trace"]["cache_read_tokens"] == 800
    assert out["trace"]["cache_creation_tokens"] == 0
