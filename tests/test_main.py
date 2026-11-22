"""
Unit tests for ai_agent/main.py startup-time helpers.

These pin the engine's own required env vars, with no plugin's additions
(a plugin's requirements are tested with the plugin).
"""

import pytest


@pytest.fixture(autouse=True)
def no_plugin_apps(monkeypatch):
    import main

    monkeypatch.setattr(main, "_PLUGIN_APPS", [])


class TestValidateEnvOrDie:
    """Tests for _validate_env_or_die: startup-time config hardening."""

    def test_accepts_complete_env(self):
        from main import _validate_env_or_die
        env = {
            "ENVIRONMENT": "production",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_ANON_KEY": "x",
            "GOOGLE_API_KEY": "y",
        }
        _validate_env_or_die(env)  # should not raise

    def test_raises_on_missing_single_var(self):
        from main import _validate_env_or_die
        env = {
            "ENVIRONMENT": "production",
            "SUPABASE_URL": "https://example.supabase.co",
            "GOOGLE_API_KEY": "y",
            # SUPABASE_ANON_KEY missing
        }
        with pytest.raises(RuntimeError, match="SUPABASE_ANON_KEY"):
            _validate_env_or_die(env)

    def test_raises_on_missing_multiple_vars(self):
        from main import _validate_env_or_die
        env = {"ENVIRONMENT": "production"}
        with pytest.raises(RuntimeError) as excinfo:
            _validate_env_or_die(env)
        msg = str(excinfo.value)
        # Every remaining required var should be named
        assert "SUPABASE_URL" in msg
        assert "SUPABASE_ANON_KEY" in msg

    def test_treats_empty_string_as_missing(self):
        """Missing vs empty-string env var — both must fail."""
        from main import _validate_env_or_die
        env = {
            "ENVIRONMENT": "",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_ANON_KEY": "x",
            "GOOGLE_API_KEY": "y",
        }
        with pytest.raises(RuntimeError, match="ENVIRONMENT"):
            _validate_env_or_die(env)

    def test_treats_whitespace_only_as_missing(self):
        """A value of '   ' is functionally empty and must fail."""
        from main import _validate_env_or_die
        env = {
            "ENVIRONMENT": "   ",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_ANON_KEY": "x",
            "GOOGLE_API_KEY": "y",
        }
        with pytest.raises(RuntimeError, match="ENVIRONMENT"):
            _validate_env_or_die(env)
