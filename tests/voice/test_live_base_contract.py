"""Tests for voice/live/base.py and voice/live/testing.py — the realtime-voice contract."""
import pytest

from voice.live.base import (
    ASK_AGENT_TOOL_SPEC,
    AudioFormat,
    RealtimeEvent,
    RealtimeSession,
)


async def test_base_cancel_barged_in_response_delegates_to_coarse_cancel():
    """Base default: cancel_barged_in_response() delegates to
    cancel_active_response(). Verify this by using a minimal concrete
    subclass that does NOT override the new methods — only overrides
    cancel_active_response to record calls."""

    class _MinimalRealtimeSession(RealtimeSession):
        """Minimal concrete subclass that exercises base defaults."""

        provider = "minimal"
        audio_format = AudioFormat(input_sample_rate=16000, output_sample_rate=24000)
        tool_spec = ASK_AGENT_TOOL_SPEC
        supports_streamed_tool_results = False
        supports_out_of_band_speech = False

        def __init__(self):
            self.coarse_cancels = 0

        async def start(self) -> None:
            pass

        async def send_audio(self, pcm: bytes) -> None:
            pass

        async def send_text_turn(self, text: str) -> None:
            pass

        async def send_tool_result(self, call_id, payload, *, mode):
            pass

        def events(self):
            async def _empty():
                return
                yield  # pragma: no cover

            return _empty()

        async def close(self) -> None:
            pass

        # Override ONLY the coarse cancel to record calls.
        async def cancel_active_response(self) -> None:
            self.coarse_cancels += 1

        # Do NOT override the two new methods — rely on base defaults.

    session = _MinimalRealtimeSession()
    await session.start()

    # cancel_barged_in_response() should delegate to cancel_active_response().
    await session.cancel_barged_in_response()
    assert session.coarse_cancels == 1, "base default must delegate to cancel_active_response"

    # suppress_spurious_reply() is a base no-op — returns None, changes nothing.
    result = await session.suppress_spurious_reply()
    assert result is None, "base default must be pure no-op"
    assert (
        session.coarse_cancels == 1
    ), "suppress_spurious_reply must not call cancel_active_response"


async def test_fake_records_both_cancel_paths():
    """FakeRealtimeSession overrides both methods and records calls
    independently. This lets endpoint tests assert the exact cancel path."""
    from voice.live.testing import FakeRealtimeSession

    session = FakeRealtimeSession()
    await session.start()

    # Both counters start at 0.
    assert session.barge_in_cancels == 0
    assert session.spurious_suppressions == 0

    # Calling cancel_barged_in_response increments only barge_in_cancels.
    await session.cancel_barged_in_response()
    assert session.barge_in_cancels == 1
    assert session.spurious_suppressions == 0

    # Calling suppress_spurious_reply increments only spurious_suppressions.
    await session.suppress_spurious_reply()
    assert session.barge_in_cancels == 1
    assert session.spurious_suppressions == 1


def test_audio_events_carry_a_reply_flag_default_false():
    """RealtimeEvent.reply defaults False; can be set True explicitly."""
    assert RealtimeEvent(type="audio", audio=b"x").reply is False
    assert RealtimeEvent(type="audio", audio=b"x", reply=True).reply is True


def test_barge_in_confirmation_defaults_off_and_the_fake_declares_it():
    """Default False = legacy path: a provider that never delivers a user-
    transcript verdict (gemini_live never finalizes input transcription)
    would otherwise never flush on a real barge-in. FakeRealtimeSession
    models the fully-capable provider (Azure) and declares True."""
    from voice.live.base import RealtimeSession
    from voice.live.testing import FakeRealtimeSession

    assert RealtimeSession.supports_barge_in_confirmation is False
    assert FakeRealtimeSession.supports_barge_in_confirmation is True


def test_finalizing_user_transcripts_is_declared_not_inferred():
    """Relay custody suppresses the model's own ask_agent and dispatches its
    replacement from a FINAL user transcript. A provider that never sends one
    can only ever suppress — custody becomes a black hole (live 2026-08-19:
    the caller's OTP code reached nobody on Gemini Live) — so the capability
    is declared per provider and the endpoint refuses to open custody without
    it.

    Not folded into `supports_barge_in_confirmation`: that flag demands MORE
    (playback_flush + non-settling cancel overrides), and the simulated
    provider finalizes while declaring it False. The two are asserted together
    here so the distinction cannot quietly collapse."""
    from voice.live.base import RealtimeSession
    from voice.live.providers.gemini_live import GeminiLiveSession
    from voice.live.simulated import SimulatedRealtimeSession
    from voice.live.testing import FakeRealtimeSession

    assert RealtimeSession.finalizes_user_transcripts is False
    assert FakeRealtimeSession.finalizes_user_transcripts is True
    # True since 2026-08-19 — NOT because the service finalizes anything
    # (it still never sets `Transcription.finished`), but because
    # `UserTurnState` synthesizes the boundary from the model taking the
    # floor. `supports_barge_in_confirmation` stays False, which is the
    # distinction this test exists to keep from collapsing: the synthesized
    # final deliberately does not fire while a response is in flight, and
    # that is precisely when a barge-in verdict would be needed.
    assert GeminiLiveSession.finalizes_user_transcripts is True
    assert GeminiLiveSession.supports_barge_in_confirmation is False
    assert GeminiLiveSession.supports_barge_in_confirmation is False
    # The provider that finalizes WITHOUT the full confirmation contract —
    # the reason these are two flags and not one.
    assert SimulatedRealtimeSession.finalizes_user_transcripts is True
    assert SimulatedRealtimeSession.supports_barge_in_confirmation is False
