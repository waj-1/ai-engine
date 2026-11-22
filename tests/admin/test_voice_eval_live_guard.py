"""Live voice eval runs are manual-only (spec §4): schedules and bake-offs
must refuse a voice_live suite whose effective mode is live."""
import pytest

from admin.services.evals import _voice_live_guard


def test_manual_live_run_allowed():
    _voice_live_guard(runner_kind="voice_live", suite_default_context={},
                      voice_mode="live", schedule_id=None, bakeoff_id=None)


def test_scheduled_live_run_refused():
    with pytest.raises(ValueError, match="manual-only"):
        _voice_live_guard(runner_kind="voice_live", suite_default_context={},
                          voice_mode="live", schedule_id="s1", bakeoff_id=None)


def test_bakeoff_suite_default_live_refused():
    with pytest.raises(ValueError, match="manual-only"):
        _voice_live_guard(
            runner_kind="voice_live",
            suite_default_context={"voice": {"mode": "live"}},
            voice_mode=None, schedule_id=None, bakeoff_id="b1",
        )


def test_scheduled_sim_run_allowed():
    _voice_live_guard(runner_kind="voice_live",
                      suite_default_context={"voice": {"mode": "sim"}},
                      voice_mode=None, schedule_id="s1", bakeoff_id=None)


def test_non_voice_suite_untouched():
    _voice_live_guard(runner_kind="chat", suite_default_context={},
                      voice_mode=None, schedule_id="s1", bakeoff_id="b1")


def test_voice_live_requires_judge_by_default():
    # No code change needed — voice_live has no RUNNER_DEFAULT_SCORER entry,
    # so the existing mandatory-judge rule covers it. Pin that with a test
    # so a future scorer registration can't silently drop the judge.
    from admin.services.eval_scorers import runner_kind_needs_judge

    assert runner_kind_needs_judge("voice_live") is True
