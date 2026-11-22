"""A backchannel must be able to wake a stalled call.

`_is_backchannel` exists so a listening caller ("mm-hm", "ايوه") does not cut
the assistant off mid-answer. The drop also skips `last_commit`, which is what
arms stall recovery and the silent-turn watchdog — correct while the assistant
is speaking, and exactly wrong once it has gone silent.

Live call, 2026-08-18: the assistant said "خلني أشوف لك" and never came back.
The caller said "ألو... ألو ألو ألو" — and "ألو" is in this tenant's
`backchannel_tokens`, so every one of those was discarded before it could arm
anything. The call stayed dead until the caller hung up.
"""
import pytest

from endpoints.voice_live import (
    _BACKCHANNEL_WAKE_AFTER_SEC,
    _utterance_should_arm_recovery,
)


def test_a_substantive_utterance_always_arms():
    """Unconditional: the backchannel rule must never gate a real question."""
    assert _utterance_should_arm_recovery(
        is_backchannel=False, response_in_flight=True, since_last_audio_sec=0.0)


def test_a_backchannel_while_the_assistant_speaks_is_still_dropped():
    """The behaviour the drop exists for — a caller saying 'mm-hm' over an
    answer must not be treated as a new turn."""
    assert not _utterance_should_arm_recovery(
        is_backchannel=True, response_in_flight=False, since_last_audio_sec=0.2)


def test_a_backchannel_with_a_response_in_flight_is_dropped():
    """Audio may not have started yet, but something IS coming — the caller
    just cannot hear it yet. Waking here would double up on an answer already
    on its way."""
    assert not _utterance_should_arm_recovery(
        is_backchannel=True, response_in_flight=True, since_last_audio_sec=99.0)


def test_a_backchannel_on_a_silent_floor_wakes_the_call():
    """The regression. Nothing in flight, nothing said for a while: 'ألو' means
    'are you there?' and has to arm recovery."""
    assert _utterance_should_arm_recovery(
        is_backchannel=True, response_in_flight=False,
        since_last_audio_sec=_BACKCHANNEL_WAKE_AFTER_SEC + 0.1)


def test_a_call_that_never_produced_audio_counts_as_silent():
    """A stall before the first word — `last_reply_audio` is still zero, which
    the call site passes as infinite silence."""
    assert _utterance_should_arm_recovery(
        is_backchannel=True, response_in_flight=False,
        since_last_audio_sec=float("inf"))


@pytest.mark.parametrize("gap", [0.0, 0.5, 1.0, 1.49])
def test_the_idle_threshold_is_not_trigger_happy(gap):
    """Below the threshold the assistant is mid-delivery between sentences;
    waking there would cut its own answer off."""
    assert not _utterance_should_arm_recovery(
        is_backchannel=True, response_in_flight=False, since_last_audio_sec=gap)
