"""
Unit tests for ai_agent/base_agent.py.

Covers the helpers that used to be duplicated across the agent modules —
now consolidated into the shared
BaseStreamingAgent class plus a handful of free functions.
"""

import pytest
from unittest.mock import Mock


class TestNormalizeContent:
    def test_none_returns_empty(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.normalize_content(None) == ""

    def test_string_passes_through(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.normalize_content("hello") == "hello"

    def test_list_of_strings_joined(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.normalize_content(["a", "b", "c"]) == "abc"

    def test_list_of_text_dicts_joined(self):
        from core.base_agent import BaseStreamingAgent
        parts = [{"type": "text", "text": "hi "}, {"type": "text", "text": "there"}]
        assert BaseStreamingAgent.normalize_content(parts) == "hi there"

    def test_metadata_only_dicts_skipped(self):
        from core.base_agent import BaseStreamingAgent
        parts = [{"type": "text", "text": "body"}, {"signature": "sig", "text": ""}]
        assert BaseStreamingAgent.normalize_content(parts) == "body"

    def test_mixed_types_coerced(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.normalize_content(["x", 42, {"text": "y"}]) == "x42y"


class TestFormatToolName:
    def test_strips_get_prefix(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.format_tool_name("get_business_performance") == "business performance"

    def test_strips_list_prefix(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.format_tool_name("list_staff") == "staff"

    def test_strips_search_prefix(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.format_tool_name("search_orders") == "orders"

    def test_no_prefix_keeps_name(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.format_tool_name("calculator") == "calculator"


class _DummyAgent:
    """Minimal class to exercise instance methods without instantiating real agents."""
    def __init__(self):
        from core.base_agent import BaseStreamingAgent
        # Mix in helpers via a fresh instance of the base.
        self._base = BaseStreamingAgent()


class TestBuildMessages:
    def test_current_message_is_last(self):
        from core.base_agent import BaseStreamingAgent
        from langchain_core.messages import HumanMessage
        agent = BaseStreamingAgent()
        out = agent.build_messages([], "hello")
        assert isinstance(out[-1], HumanMessage)
        assert out[-1].content == "hello"

    def test_history_limited(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        history = [{"role": "user", "content": f"m{i}"} for i in range(50)]
        out = agent.build_messages(history, "now", history_limit=5)
        assert len(out) == 6  # 5 history + 1 current

    def test_long_messages_truncated(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        history = [{"role": "user", "content": "x" * 10_000}]
        out = agent.build_messages(history, "now", per_message_limit=100)
        assert "truncated" in out[0].content
        assert len(out[0].content) < 200

    def test_message_prefix_prepended_to_current(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        out = agent.build_messages([], "what now?", message_prefix="[CTX: test]")
        assert out[-1].content.startswith("[CTX: test]")
        assert "what now?" in out[-1].content


class TestTokenBudget:
    def test_warning_at_threshold(self):
        from core.base_agent import BaseStreamingAgent
        totals = {"input_tokens": 0, "output_tokens": 850}  # 85% of 1000
        warn = BaseStreamingAgent.check_token_budget(totals, budget=1000, warn_ratio=0.8)
        assert warn is not None
        assert "budget" in warn.lower()

    def test_warning_emitted_only_once(self):
        from core.base_agent import BaseStreamingAgent
        totals = {"input_tokens": 0, "output_tokens": 850}
        first = BaseStreamingAgent.check_token_budget(totals, budget=1000, warn_ratio=0.8)
        second = BaseStreamingAgent.check_token_budget(totals, budget=1000, warn_ratio=0.8)
        assert first is not None
        assert second is None

    def test_under_threshold_returns_none(self):
        from core.base_agent import BaseStreamingAgent
        totals = {"input_tokens": 10, "output_tokens": 20}
        assert BaseStreamingAgent.check_token_budget(totals, budget=1000) is None

    def test_hard_cap_raises(self):
        from core.base_agent import BaseStreamingAgent, TokenBudgetExceeded
        totals = {"input_tokens": 600, "output_tokens": 600}
        with pytest.raises(TokenBudgetExceeded):
            BaseStreamingAgent.check_token_budget(totals, budget=1000)

    def test_update_usage_handles_none_output(self):
        from core.base_agent import BaseStreamingAgent
        totals = {"input_tokens": 0, "output_tokens": 0}
        BaseStreamingAgent.update_usage(totals, None)
        assert totals == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }

    def test_update_usage_accumulates(self):
        from core.base_agent import BaseStreamingAgent
        totals = {"input_tokens": 5, "output_tokens": 10}
        output = Mock()
        output.usage_metadata = {"input_tokens": 20, "output_tokens": 30, "total_tokens": 50}
        BaseStreamingAgent.update_usage(totals, output)
        assert totals == {
            "input_tokens": 25,
            "output_tokens": 40,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }


class TestTruncateToolResult:
    def test_short_unchanged(self):
        from core.base_agent import BaseStreamingAgent
        assert BaseStreamingAgent.truncate_tool_result("hi", 10) == "hi"

    def test_long_cut_with_marker(self):
        from core.base_agent import BaseStreamingAgent
        out = BaseStreamingAgent.truncate_tool_result("y" * 100, 10)
        assert out.startswith("yyyyyyyyyy")
        assert "truncated" in out


class TestParseSuggestions:
    def test_numbered_list(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        text = "1. Question one?\n2. Question two?\n3. Question three?"
        out = agent.parse_suggestions(text)
        assert out == ["Question one?", "Question two?", "Question three?"]

    def test_bullet_list(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        text = "- Alpha beta\n* Gamma delta\n• Epsilon zeta"
        out = agent.parse_suggestions(text)
        assert out == ["Alpha beta", "Gamma delta", "Epsilon zeta"]

    def test_caps_at_limit(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        text = "\n".join(f"Question {i}?" for i in range(10))
        out = agent.parse_suggestions(text, limit=3)
        assert len(out) == 3

    def test_filters_tiny_lines(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        text = "ok\nThis is a real follow-up?"
        out = agent.parse_suggestions(text)
        assert out == ["This is a real follow-up?"]


class TestCleanTitle:
    def test_strips_quotes_and_whitespace(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        assert agent.clean_title('"Hello world"') == "Hello world"

    def test_collapses_newlines(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        assert agent.clean_title("Line\nbreak\there") == "Line break here"

    def test_empty_uses_fallback(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        assert agent.clean_title("   ", fallback="fallback") == "fallback"


class TestBuildHistorySummary:
    def test_includes_user_and_response(self):
        from core.base_agent import build_history_summary
        out = build_history_summary([], "question?", "answer.")
        assert "question?" in out
        assert "answer" in out

    def test_truncates_long_history_items(self):
        from core.base_agent import build_history_summary
        history = [{"role": "user", "content": "x" * 5000}]
        out = build_history_summary(history, "q")
        # 100-char cap per message + role prefix
        assert len(out.split("\n")[0]) < 200

    def test_respects_max_history(self):
        from core.base_agent import build_history_summary
        history = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        out = build_history_summary(history, "now", max_history=2)
        # Only m8 and m9 should be included
        assert "m9" in out
        assert "m8" in out
        assert "m0" not in out


class TestLocaleNormalization:
    def test_known_locale_preserved(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        assert agent._normalize_locale("ar") == "ar"

    def test_unknown_locale_falls_back_to_en(self):
        from core.base_agent import BaseStreamingAgent
        agent = BaseStreamingAgent()
        assert agent._normalize_locale("zz") == "en"
