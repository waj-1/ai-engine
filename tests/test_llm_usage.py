"""
Unit tests for the llm_usage module.

Guards against regression of the "getattr-on-TypedDict-returns-default" bug
that caused `usage` SSE events to always report 0 tokens.
"""

from unittest.mock import Mock


class TestExtractUsage:
    """Unit tests for extract_usage()."""

    def test_returns_zeros_when_output_is_none(self):
        from core.llm_usage import extract_usage
        assert extract_usage(None) == (0, 0, 0)

    def test_returns_zeros_when_no_usage_metadata_attr(self):
        from core.llm_usage import extract_usage
        output = Mock(spec=[])  # Mock with no attributes
        assert extract_usage(output) == (0, 0, 0)

    def test_returns_zeros_when_usage_metadata_is_none(self):
        from core.llm_usage import extract_usage
        output = Mock()
        output.usage_metadata = None
        assert extract_usage(output) == (0, 0, 0)

    def test_returns_zeros_when_usage_metadata_is_empty_dict(self):
        """An empty dict is falsy and treated as 'no usage info'."""
        from core.llm_usage import extract_usage
        output = Mock()
        output.usage_metadata = {}
        assert extract_usage(output) == (0, 0, 0)

    def test_extracts_from_dict_usage_metadata(self):
        """LangChain's UsageMetadata is a TypedDict; at runtime it is a plain dict.

        This is the bug we are fixing: getattr on a dict returns the default
        because dicts do not have instance attributes.
        """
        from core.llm_usage import extract_usage
        output = Mock()
        output.usage_metadata = {
            "input_tokens": 120,
            "output_tokens": 45,
            "total_tokens": 165,
        }
        assert extract_usage(output) == (120, 45, 165)

    def test_handles_dict_missing_total_tokens(self):
        """If total_tokens is absent, fall back to input + output."""
        from core.llm_usage import extract_usage
        output = Mock()
        output.usage_metadata = {
            "input_tokens": 50,
            "output_tokens": 30,
        }
        assert extract_usage(output) == (50, 30, 80)

    def test_handles_dict_with_zero_values(self):
        from core.llm_usage import extract_usage
        output = Mock()
        output.usage_metadata = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        assert extract_usage(output) == (0, 0, 0)

    def test_handles_dict_with_none_values(self):
        """Some streaming chunks can emit None for individual counts."""
        from core.llm_usage import extract_usage
        output = Mock()
        output.usage_metadata = {
            "input_tokens": None,
            "output_tokens": 42,
            "total_tokens": None,
        }
        assert extract_usage(output) == (0, 42, 42)
