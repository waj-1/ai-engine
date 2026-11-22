"""
Unit tests for ai_agent/config.py.

Guards the env-var resolution and the truncation helper that the agents use
when shaping tool results for the LLM context.
"""

import os
import pytest


class TestAgentConfigDefaults:
    """AGENT_CONFIG should resolve to sensible defaults with no env set."""

    def test_models_have_defaults(self):
        from core.config import AgentConfig
        cfg = AgentConfig()
        assert cfg.main_model
        assert cfg.light_model

    def test_tool_loop_bounds_sane(self):
        from core.config import AgentConfig
        cfg = AgentConfig()
        assert cfg.max_iterations_main > 0
        assert cfg.delegate_max_iterations > 0

    def test_token_budget_positive(self):
        from core.config import AgentConfig
        cfg = AgentConfig()
        assert cfg.per_request_token_budget > 0
        assert 0 < cfg.per_request_token_warn_ratio <= 1.0

    def test_truncation_limits_positive(self):
        from core.config import AgentConfig
        cfg = AgentConfig()
        assert cfg.tool_result_char_limit > 0
        assert cfg.history_message_char_limit > 0
        assert cfg.history_message_limit_main > 0


class TestEnvOverrides:
    """Env vars should override defaults on fresh instantiation."""

    def test_main_model_overridable(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAIN_MODEL", "gemini-test")
        from core.config import AgentConfig
        cfg = AgentConfig()
        assert cfg.main_model == "gemini-test"

    def test_invalid_int_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_ITERATIONS", "not-a-number")
        from core.config import AgentConfig
        cfg = AgentConfig()
        assert cfg.max_iterations_main == 10  # default

    def test_invalid_float_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAIN_TEMPERATURE", "abc")
        from core.config import AgentConfig
        cfg = AgentConfig()
        assert cfg.main_temperature == 1.0


class TestKbMatchThresholdFollowsEmbedProvider:
    """The KB similarity floor is only meaningful inside one embedding space.

    0.3 was calibrated on Azure text-embedding-3-small; 0.4 on Gemini, which is
    what EMBED_PROVIDER defaults to. Shipping 0.3 flat (PR #87) paired the
    Azure number with the Gemini default and quietly loosened KB search on
    every stack that had not pinned the env var.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        # The developer's own .env / shell must not decide the assertions.
        monkeypatch.delenv("KB_MATCH_THRESHOLD", raising=False)
        monkeypatch.delenv("EMBED_PROVIDER", raising=False)

    def test_defaults_to_gemini_calibration_when_provider_unset(self):
        """EMBED_PROVIDER unset == Gemini embeddings, so the floor is 0.4."""
        from core.config import AgentConfig
        assert AgentConfig().kb_match_threshold == 0.4

    def test_explicit_google_provider_gets_gemini_calibration(self, monkeypatch):
        monkeypatch.setenv("EMBED_PROVIDER", "google")
        from core.config import AgentConfig
        assert AgentConfig().kb_match_threshold == 0.4

    def test_azure_provider_gets_azure_calibration(self, monkeypatch):
        monkeypatch.setenv("EMBED_PROVIDER", "azure")
        from core.config import AgentConfig
        assert AgentConfig().kb_match_threshold == 0.22

    def test_provider_string_is_normalized_like_embedding_provider(self, monkeypatch):
        """active_provider() strips + lowercases; the threshold must agree."""
        monkeypatch.setenv("EMBED_PROVIDER", "  Azure ")
        from core.config import AgentConfig
        assert AgentConfig().kb_match_threshold == 0.22

    def test_unknown_provider_falls_back_to_gemini_calibration(self, monkeypatch):
        """A typo'd provider embeds via the Gemini path, so it scores like one."""
        monkeypatch.setenv("EMBED_PROVIDER", "azrue")
        from core.config import AgentConfig
        assert AgentConfig().kb_match_threshold == 0.4

    @pytest.mark.parametrize("provider", [None, "google", "azure"])
    def test_explicit_env_var_wins_over_provider_default(self, monkeypatch, provider):
        if provider is not None:
            monkeypatch.setenv("EMBED_PROVIDER", provider)
        monkeypatch.setenv("KB_MATCH_THRESHOLD", "0.62")
        from core.config import AgentConfig
        assert AgentConfig().kb_match_threshold == 0.62

    def test_config_default_matches_embedding_provider_default(self):
        """Pins the coupling itself: whatever active_provider() returns with no
        env set is the key the threshold table must be read with."""
        from core.config import (
            _KB_MATCH_THRESHOLD_BY_PROVIDER,
            _KB_MATCH_THRESHOLD_FALLBACK,
            AgentConfig,
        )
        from core.embedding_provider import active_provider

        expected = _KB_MATCH_THRESHOLD_BY_PROVIDER.get(
            active_provider(), _KB_MATCH_THRESHOLD_FALLBACK
        )
        assert AgentConfig().kb_match_threshold == expected


class TestTruncateForLLM:
    """truncate_for_llm must emit a marker the LLM can read when cutting."""

    def test_short_text_untouched(self):
        from core.config import truncate_for_llm
        assert truncate_for_llm("hello", 100) == "hello"

    def test_long_text_truncated_with_marker(self):
        from core.config import truncate_for_llm
        text = "x" * 50
        out = truncate_for_llm(text, 10)
        assert out.startswith("xxxxxxxxxx")
        # Marker must tell the LLM data was cut AND how much was omitted.
        assert "truncated" in out
        assert "40" in out  # omitted count

    def test_truncation_uses_config_default_when_unspecified(self):
        from core.config import AGENT_CONFIG, truncate_for_llm
        limit = AGENT_CONFIG.tool_result_char_limit
        text = "a" * (limit + 500)
        out = truncate_for_llm(text)
        assert len(out.split("\n")[0]) == limit  # first line is the prefix
        assert "truncated" in out


class TestSupportedLocales:
    def test_supported_locales_are_frozen(self):
        from core.config import SUPPORTED_LOCALES
        assert "en" in SUPPORTED_LOCALES
        assert "ar" in SUPPORTED_LOCALES
        assert "tr" in SUPPORTED_LOCALES
        assert "ru" in SUPPORTED_LOCALES
