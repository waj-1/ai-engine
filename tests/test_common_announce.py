"""common.announce — the shared lookup-announcement detector.

Single source of truth for the announce token set + normalisation, used by
BOTH the voice endpoint's announce-then-stall tripwire
(endpoints/voice_live.py) and a client demo agent's announce-without-tool-call
guard (in the plugin). tests/voice/test_announce_detector_agreement.py
holds the persona prompts and the token set together; this file covers the
shared module's own contract.
"""
from __future__ import annotations

from common.announce import contains_announce, normalize_intent_text


def test_detects_the_live_failure_phrase():
    # The exact final answer a live caller was left hanging on (2026-08-06).
    assert contains_announce(
        "I'm pulling the troubleshooting guide for error 119 now. One moment."
    )


def test_detects_arabic_announcements_across_orthography():
    assert contains_announce("ثانية وحدة أشوف لك")
    assert contains_announce("ثانيه وحده اشوف لك")  # ta-marbuta/ha + alef variants


def test_ordinary_answers_do_not_match():
    # "second"/"moment"-adjacent words in ordinary answers must not arm the
    # guard (same reasoning as the endpoint's deliberately-not-added list).
    assert not contains_announce("The second branch did best last week.")
    assert not contains_announce("شكراً مرة ثانية، مع السلامة.")


def test_normalize_strips_spacing_hyphens_and_tashkeel():
    assert normalize_intent_text("I-BAN") == normalize_intent_text("i ban")
    assert normalize_intent_text("لَحْظَة") == normalize_intent_text("لحظه")


def test_endpoint_still_exposes_the_same_detector():
    # The endpoint's private aliases must keep pointing at the shared module —
    # a re-forked copy would let the two detectors drift apart again.
    from common import announce
    from endpoints import voice_live

    assert voice_live._ANNOUNCE_TOKENS is announce.ANNOUNCE_TOKENS
    assert voice_live._normalize_intent_text is announce.normalize_intent_text
