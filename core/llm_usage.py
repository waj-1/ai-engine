"""
Helper for extracting token usage from LangChain LLM output.

LangChain's `usage_metadata` is typed as a `UsageMetadata` TypedDict. At
runtime TypedDicts are plain dicts, so `getattr(usage, "input_tokens", 0)`
always returns the default (dicts do not have instance attributes).

This module provides `extract_usage()` — the one and only correct way to
read token counts from an AIMessage / AIMessageChunk / LLM output object.
"""

from typing import Any


def extract_usage(output: Any) -> tuple[int, int, int]:
    """Return (input_tokens, output_tokens, total_tokens) for an LLM output.

    Returns (0, 0, 0) when:
    - `output` is None
    - `output` has no `usage_metadata` attribute
    - `usage_metadata` is None or a falsy value (including the empty dict `{}`)

    Handles the common dict case and fills in `total_tokens` from the sum
    of input + output when absent from the dict.
    """
    if output is None:
        return (0, 0, 0)

    usage = getattr(output, "usage_metadata", None)
    if not usage:
        return (0, 0, 0)

    # usage is a dict (TypedDict at runtime); use .get, not getattr.
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return (input_tokens, output_tokens, total_tokens)


def extract_cache_tokens(output: Any) -> tuple[int, int]:
    """Return (cache_read_tokens, cache_creation_tokens) for an LLM output.

    LangChain normalizes every provider's prompt-cache counts into
    ``usage_metadata["input_token_details"]["cache_read"]`` and
    ``["cache_creation"]``. Both are SUBSETS of ``input_tokens`` (they are not
    additive). Returns (0, 0) when output/usage/details are missing.
    """
    if output is None:
        return (0, 0)
    usage = getattr(output, "usage_metadata", None)
    if not usage:
        return (0, 0)
    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read") or 0
    cache_creation = details.get("cache_creation") or 0
    return (int(cache_read), int(cache_creation))
