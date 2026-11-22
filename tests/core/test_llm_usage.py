from types import SimpleNamespace

from core.llm_usage import extract_cache_tokens


def _msg(usage):
    return SimpleNamespace(usage_metadata=usage)


def test_extract_cache_tokens_present():
    out = _msg({"input_tokens": 1000, "output_tokens": 50,
                "input_token_details": {"cache_read": 800, "cache_creation": 120}})
    assert extract_cache_tokens(out) == (800, 120)


def test_extract_cache_tokens_partial_details():
    out = _msg({"input_tokens": 1000, "input_token_details": {"cache_read": 300}})
    assert extract_cache_tokens(out) == (300, 0)


def test_extract_cache_tokens_no_details():
    out = _msg({"input_tokens": 1000, "output_tokens": 50})
    assert extract_cache_tokens(out) == (0, 0)


def test_extract_cache_tokens_none_and_empty():
    assert extract_cache_tokens(None) == (0, 0)
    assert extract_cache_tokens(_msg(None)) == (0, 0)
    assert extract_cache_tokens(_msg({})) == (0, 0)
