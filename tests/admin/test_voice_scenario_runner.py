"""admin/voice_scenario_runner.py — the pure parts.

The WS-driving path needs a live engine + provider key and stays in the
scenario runs themselves (status=skipped without a key); what pytest owns is
the assertion engine and the target/scenario plumbing that must not regress
silently.
"""
import os
from unittest import mock

from admin.voice_scenario_runner import (
    STEP_TYPES,
    default_target,
    evaluate_pattern,
    run_scenario,
)


def test_step_types_cover_the_documented_set():
    assert set(STEP_TYPES) == {
        "say", "expect_transcript", "expect_frame", "http_check", "wait",
    }


def test_evaluate_pattern_matches_role_and_regex_after_the_mark():
    frames = [
        {"type": "transcript", "role": "assistant", "text": "hello from before"},
        {"type": "transcript", "role": "user", "text": "what is orchard"},
        {"type": "transcript", "role": "assistant", "text": "Orchard is a payments company."},
    ]
    # After index 1: the assistant's answer matches.
    assert evaluate_pattern(frames, since_index=1, frame_type="transcript",
                            role="assistant", pattern="orchard")
    # Role filter: the user's own words must not satisfy an assistant check.
    assert not evaluate_pattern(frames, since_index=1, frame_type="transcript",
                                role="assistant", pattern="what is")
    # since_index excludes earlier frames.
    assert not evaluate_pattern(frames, since_index=1, frame_type="transcript",
                                role="assistant", pattern="before")


def test_evaluate_pattern_empty_pattern_means_any_nonempty_text():
    frames = [
        {"type": "transcript", "role": "assistant", "text": "   "},
        {"type": "transcript", "role": "assistant", "text": "مرحبا"},
    ]
    assert evaluate_pattern(frames, since_index=0, frame_type="transcript",
                            role="assistant", pattern="")
    assert not evaluate_pattern(frames[:1], since_index=0, frame_type="transcript",
                                role="assistant", pattern="")


def test_evaluate_pattern_other_frame_types_search_the_json():
    frames = [{"type": "display", "content": "Your IBAN ends in 1234"}]
    assert evaluate_pattern(frames, since_index=0, frame_type="display",
                            role=None, pattern="ends in 1234")
    assert not evaluate_pattern(frames, since_index=0, frame_type="chart",
                                role=None, pattern="ends in 1234")


def test_default_target_prefers_env_override_and_upgrades_http():
    with mock.patch.dict(os.environ, {"VOICE_SCENARIO_TARGET": "https://demo.example"}):
        assert default_target("/orchard/voice/live") == "wss://demo.example/orchard/voice/live"
    with mock.patch.dict(os.environ, {"VOICE_SCENARIO_TARGET": "", "PORT": "9000"}):
        assert default_target("/voice/live") == "ws://127.0.0.1:9000/voice/live"


async def test_run_scenario_skips_without_steps_or_tts_key():
    empty = await run_scenario({"slug": "s", "agent_kind": "orchard", "steps": []})
    assert empty["status"] == "skipped"

    with mock.patch.dict(os.environ, {"AZURE_SPEECH_KEY": ""}):
        no_key = await run_scenario(
            {"slug": "s", "agent_kind": "orchard",
             "steps": [{"type": "say", "text": "hi"}]}
        )
    assert no_key["status"] == "skipped"
    assert "AZURE_SPEECH_KEY" in no_key["reason"]
