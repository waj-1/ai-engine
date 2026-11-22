from voice.live.base import AudioFormat
from voice.live.protocol import (
    CLOSE_FORBIDDEN,
    CLOSE_PROVIDER_ERROR,
    CLOSE_RATE_LIMITED,
    chart_frame,
    display_frame,
    error_frame,
    interrupted_frame,
    ready_frame,
    transcript_frame,
)


def test_ready_frame_carries_the_negotiated_audio_format():
    frame = ready_frame(AudioFormat(input_sample_rate=16000, output_sample_rate=24000), "sess-1")
    assert frame["type"] == "ready"
    assert frame["audio"] == {"input_sample_rate": 16000, "output_sample_rate": 24000, "encoding": "pcm16"}
    assert frame["session_id"] == "sess-1"


def test_transcript_frame_shape():
    assert transcript_frame("user", "hello", final=True) == {
        "type": "transcript", "role": "user", "text": "hello", "final": True,
    }


def test_transcript_frame_out_of_band_marker_is_additive():
    """A holding line carries `out_of_band: true`; a normal answer frame
    carries NO such key at all (absent == in-conversation), so old clients
    and stored transcripts are byte-identical to before."""
    assert "out_of_band" not in transcript_frame("assistant", "hi", final=True)
    assert "out_of_band" not in transcript_frame(
        "assistant", "hi", final=True, out_of_band=False
    )
    frame = transcript_frame(
        "assistant", "Still working on that, one moment.",
        final=True, out_of_band=True,
    )
    assert frame["out_of_band"] is True
    assert frame["type"] == "transcript" and frame["role"] == "assistant"


def test_display_frame_carries_the_authoritative_text():
    assert display_frame("AED 1,240") == {"type": "display", "text": "AED 1,240"}


def test_chart_frame_passes_the_agent_event_through_untouched():
    agent_event = {"type": "chart", "chart_id": "c1", "title": "Net", "caption": "", "spec": {"mark": "bar"}}
    assert chart_frame(agent_event) == agent_event


def test_interrupted_and_error_frames():
    # Reason-less stays byte-identical to the legacy frame — old callers and
    # old clients meet in the middle on exactly this shape.
    assert interrupted_frame() == {"type": "interrupted"}
    assert error_frame("nope") == {"type": "error", "message": "nope"}


def test_interrupted_frame_carries_the_flush_reason():
    assert interrupted_frame("barge_in") == {"type": "interrupted", "reason": "barge_in"}
    assert interrupted_frame("input_end") == {"type": "interrupted", "reason": "input_end"}
    assert interrupted_frame("answer_ready") == {"type": "interrupted", "reason": "answer_ready"}
    assert interrupted_frame("speech_detected") == {"type": "interrupted", "reason": "speech_detected"}


def test_close_codes_are_in_the_application_range():
    for code in (CLOSE_FORBIDDEN, CLOSE_RATE_LIMITED, CLOSE_PROVIDER_ERROR):
        assert 4000 <= code <= 4999
    assert len({CLOSE_FORBIDDEN, CLOSE_RATE_LIMITED, CLOSE_PROVIDER_ERROR}) == 3


def test_turn_end_frame_shape():
    from voice.live.protocol import turn_end_frame

    assert turn_end_frame() == {"type": "turn_end"}


def test_proposal_frame_is_verbatim_passthrough():
    from voice.live.protocol import proposal_frame

    event = {"type": "proposal", "proposal": {"type": "lab_order", "test_name": "CBC"}}
    assert proposal_frame(event) is event


def test_agent_status_frame_shape():
    from voice.live.protocol import agent_status_frame

    assert agent_status_frame("Using tool: Search Documentation") == {
        "type": "agent_status",
        "text": "Using tool: Search Documentation",
    }


def test_turn_end_frame_reply_key_is_additive():
    """`reply` is present only when the provider knows it; None keeps the
    legacy frame byte-identical for old clients."""
    from voice.live.protocol import turn_end_frame

    assert turn_end_frame(reply=None) == {"type": "turn_end"}
    assert "reply" not in turn_end_frame(None)
    assert turn_end_frame(reply=True) == {"type": "turn_end", "reply": True}
    assert turn_end_frame(reply=False) == {"type": "turn_end", "reply": False}
