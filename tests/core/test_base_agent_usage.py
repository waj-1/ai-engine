from types import SimpleNamespace

from core.base_agent import BaseStreamingAgent


def _msg(usage):
    return SimpleNamespace(usage_metadata=usage)


def test_update_usage_accumulates_cache_tokens():
    totals = {"input_tokens": 0, "output_tokens": 0}
    BaseStreamingAgent.update_usage(totals, _msg({
        "input_tokens": 1000, "output_tokens": 40,
        "input_token_details": {"cache_read": 700, "cache_creation": 90}}))
    assert totals["input_tokens"] == 1000
    assert totals["output_tokens"] == 40
    assert totals["cache_read_tokens"] == 700
    assert totals["cache_creation_tokens"] == 90


def test_update_usage_no_cache_keys_when_absent():
    totals = {"input_tokens": 0, "output_tokens": 0}
    BaseStreamingAgent.update_usage(totals, _msg({"input_tokens": 10, "output_tokens": 5}))
    assert totals["cache_read_tokens"] == 0
    assert totals["cache_creation_tokens"] == 0
