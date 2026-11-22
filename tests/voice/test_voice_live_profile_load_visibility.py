"""A tenant whose voice profile never loaded must be findable in the logs.

`_active_profile_for` is fail-open by design: a DB blip returns None and the
call proceeds on built-in defaults rather than dropping. It logged that at
`logger.debug` — off in every deployed stack — so a misconfigured tenant
degraded in total silence, which is the same invisible-config failure the
voice_config unification exists to remove: /admin shows one configuration and
the call ran on another, with nothing above debug to say so.

These assert the record exists at WARNING and that the two causes stay
distinguishable, because they need different operator responses: seed/promote a
profile, versus go look at the AI Supabase connection.
"""
import logging

import pytest

from core.agent_profile import MissingProfileError, ProfileLoadError
from endpoints import voice_live


def _fail_with(monkeypatch, exc):
    from core import agent_profile

    def _boom(agent_kind, environment=None):
        raise exc

    monkeypatch.setattr(agent_profile.AgentProfileLoader, "load_active", _boom)


def test_a_missing_profile_is_reported_at_warning(monkeypatch, caplog):
    _fail_with(monkeypatch, MissingProfileError(
        "no active agent_profiles row for kind=sample_voice env=prod; run the seeders"))

    with caplog.at_level(logging.WARNING, logger=voice_live.logger.name):
        assert voice_live._active_profile_for("sample_voice") is None

    record = caplog.records[-1]
    assert record.levelno == logging.WARNING, (
        "debug is off in every deployed stack — the whole point is that this is "
        "greppable after 'voice sounds wrong for this tenant'")
    message = record.getMessage()
    assert "sample_voice" in message
    assert "env=prod" in message, "the exception carries the environment; keep it"
    assert "defaults" in message.lower(), (
        "the operator's actual symptom is that the tenant's voice_config never "
        "applied — say so, do not just name the exception")


def test_a_load_failure_is_distinguishable_from_a_missing_profile(monkeypatch, caplog):
    """Same fail-open outcome, different fix. A DB/transport error is not
    something to go looking for in /admin."""
    _fail_with(monkeypatch, ProfileLoadError("Failed to load agent_profile: timeout"))

    with caplog.at_level(logging.WARNING, logger=voice_live.logger.name):
        assert voice_live._active_profile_for("sample_voice") is None

    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None, (
        "the unexpected branch keeps its traceback; the missing-profile branch "
        "deliberately does not")
    assert "FAILED" in record.getMessage()


def test_the_missing_profile_branch_carries_no_traceback(monkeypatch, caplog):
    _fail_with(monkeypatch, MissingProfileError("kind=sample_voice env=prod"))

    with caplog.at_level(logging.WARNING, logger=voice_live.logger.name):
        voice_live._active_profile_for("sample_voice")

    assert caplog.records[-1].exc_info is None, (
        "a known config state is not a crash; a stack trace here is noise that "
        "makes the real transport failures harder to spot")


@pytest.mark.parametrize("exc", [MissingProfileError("x"), RuntimeError("x")])
def test_the_connect_still_fails_open(monkeypatch, exc):
    """Louder, not fatal: a transient DB error must never take voice down."""
    _fail_with(monkeypatch, exc)

    assert voice_live._active_profile_for("sample_voice") is None
