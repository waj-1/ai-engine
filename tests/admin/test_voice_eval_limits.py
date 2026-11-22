"""The mechanical half of voice-eval grading: limits a rubric judge cannot do.

A judge reading a transcript cannot tell a cut-off reply from a short one —
the truncated text still matches the pattern for the answer, so a
`no_truncation` criterion scores a fragment as a pass. That is how the
2026-08-06 04:13 Arabic call ("she stops mid-word, nothing in the logs")
would have sailed through the existing endurance suites.

The fixture below IS that call, verbatim.
"""
from admin.voice_eval_runner import _check_limits, _ends_like_a_finished_utterance

# The four assistant turns of the 2026-08-06 04:13 UTC client demo call, in order.
# Turns 1 and 2 are the two the caller reported as cut with nobody speaking
# over them; turns 0 and 3 completed. (Turn 0 was interrupted BY the caller —
# an interruption is not a defect, and the sentence itself is complete.)
_CALL_0413 = [
    "حياك الله، معك ليلى، المساعدة الرقمية من البستان. كيف أقدر أخدمك اليوم؟",
    "مساء النور، تفضل، كيف أقدر أ",
    "طيب — مبيعات شهر يوليو وصلت 405,341.84 ريال. تبي تفصيل حسب الفرع ولا حسب الطرفية",
    "لحظة، أتأكد لك الحين.",
]


def _turns(texts, **extra):
    return [dict({"turn": i, "text": t, "latency_ms": 100, "turn_ms": 1000,
                  "turn_end_seen": True}, **extra)
            for i, t in enumerate(texts)]


def test_the_0413_call_breaches_on_exactly_the_two_replies_that_were_cut():
    breaches = _check_limits({"require_complete_utterances": True},
                             _turns(_CALL_0413))
    assert [b["turn"] for b in breaches] == [1, 2]
    assert {b["kind"] for b in breaches} == {"incomplete_utterance"}
    # The evidence in the breach is the tail the caller never heard finish.
    assert breaches[0]["value"].endswith("كيف أقدر أ")


def test_the_same_call_passes_every_pre_existing_limit():
    """Proof the new limit discriminates: the metrics the suite already had —
    latency, turn duration, turn_end — are all healthy on this call. The
    engine's own session line said the same thing (barge_ins=0,
    answers_dropped_upstream=0, no flush/truncate/cancel logged). Nothing
    that existed before could see the defect."""
    assert _check_limits(
        {"max_latency_ms": 20000, "max_turn_ms": 45000, "require_turn_end": True},
        _turns(_CALL_0413),
    ) == []


def test_complete_utterances_across_the_scripts_this_edge_speaks():
    for text in (
        "مساء النور، تفضل، كيف أقدر أخدمك؟",
        "Your approved sales for July were 405,341.84 SAR.",
        "Sure — here is the breakdown:",
        "تمام!",
        'She said "one moment."',
        "تبي تفصيل حسب الفرع ولا حسب الطرفية؟",
    ):
        assert _ends_like_a_finished_utterance(text), text
    for text in (
        "كيف أقدر أ",
        "تبي تفصيل حسب الفرع ولا حسب الطرفية",
        "Your approved sales for July were",
        # A trailing Arabic comma is mid-sentence, never an ending.
        "تمام، فتحت،",
    ):
        assert not _ends_like_a_finished_utterance(text), text


def test_incomplete_utterance_is_opt_in_and_skips_silent_turns():
    # Not requested -> never evaluated, whatever the text looks like.
    assert _check_limits({"max_turn_ms": 45000}, _turns(_CALL_0413)) == []
    # A turn with no reply at all is `require_turn_end`'s business, not this
    # limit's — an empty text must not manufacture a second breach.
    assert _check_limits({"require_complete_utterances": True},
                         _turns(["", "   "])) == []


def test_max_answer_ms_gates_the_answer_and_skips_null_values():
    """max_latency_ms clocks the FIRST assistant transcript — on tool turns
    the holding line at progress_after_sec — so a 15s answer behind a 2.5s
    holding line passes it. max_answer_ms reads `answer_ms` (first
    non-out-of-band transcript) and is the one that can see it. A turn with
    no answer at all (answer_ms None) is skipped, same as every other null
    metric."""
    turns = _turns(["Your approved sales for July were 405,341.84 SAR."],
                   answer_ms=15000)
    turns[0]["latency_ms"] = 2500
    assert _check_limits({"max_latency_ms": 20000}, turns) == []
    breaches = _check_limits({"max_answer_ms": 12000}, turns)
    assert breaches == [{"turn": 0, "kind": "max_answer_ms",
                         "value": 15000, "limit": 12000}]
    assert _check_limits({"max_answer_ms": 12000},
                         _turns(["…"], answer_ms=None)) == []


def test_played_audio_ratio_catches_audio_a_flush_threw_away():
    """The other truncation family: the text is COMPLETE and only the sound
    is missing, because a playback flush discarded an answer the model
    believes it delivered (#70). `require_complete_utterances` is blind to
    it; this limit is not."""
    turns = _turns(["Your approved sales for July were 405,341.84 SAR."])
    turns[0].update({"audio_ms": 10000, "audio_discarded_ms": 7400})
    assert _check_limits({"require_complete_utterances": True}, turns) == []
    breaches = _check_limits({"min_played_audio_ratio": 0.9}, turns)
    assert [b["kind"] for b in breaches] == ["min_played_audio_ratio"]
    assert breaches[0]["value"] == 0.26

    # Heard in full -> no breach. A turn that sent no audio is skipped rather
    # than dividing by zero into a false 0.0 ratio.
    turns[0].update({"audio_ms": 10000, "audio_discarded_ms": 0})
    assert _check_limits({"min_played_audio_ratio": 0.9}, turns) == []
    turns[0].update({"audio_ms": 0, "audio_discarded_ms": 0})
    assert _check_limits({"min_played_audio_ratio": 0.9}, turns) == []
