import pytest

from voice.live.base import (
    ASK_AGENT_TOOL_SPEC,
    AudioFormat,
    RealtimeEvent,
    RealtimeSession,
    ToolResultMode,
    ToolSpec,
)
from voice.live.testing import FakeRealtimeSession, assert_realtime_contract


async def test_fake_provider_satisfies_the_contract():
    await assert_realtime_contract(lambda: FakeRealtimeSession())


async def test_scripted_events_are_replayed_in_order():
    session = FakeRealtimeSession(
        script=[
            RealtimeEvent(type="transcript", role="user", text="hello", final=True),
            RealtimeEvent(type="audio", audio=b"PCM"),
            RealtimeEvent(type="turn_end"),
        ]
    )
    await session.start()
    seen = [e.type async for e in session.events()]
    assert seen == ["transcript", "audio", "turn_end"]


async def test_sent_audio_is_recorded():
    session = FakeRealtimeSession()
    await session.start()
    await session.send_audio(b"aaa")
    await session.send_audio(b"bbb")
    assert session.sent_audio == [b"aaa", b"bbb"]


async def test_tool_results_record_their_mode():
    session = FakeRealtimeSession()
    await session.start()
    await session.send_tool_result("call-1", {"spoken": "hi"}, mode=ToolResultMode.SILENT)
    assert session.tool_results == [("call-1", {"spoken": "hi"}, ToolResultMode.SILENT)]


async def test_audio_format_is_declared_not_assumed():
    session = FakeRealtimeSession()
    assert isinstance(session.audio_format, AudioFormat)
    assert session.audio_format.input_sample_rate > 0
    assert session.audio_format.output_sample_rate > 0


async def test_send_tool_result_rejects_a_non_mode():
    session = FakeRealtimeSession()
    await session.start()
    with pytest.raises(TypeError):
        await session.send_tool_result("call-1", {}, mode="interrupt")


async def test_events_before_start_fails_loudly_instead_of_hanging():
    session = FakeRealtimeSession(script=[RealtimeEvent(type="turn_end")])
    with pytest.raises(RuntimeError):
        async for _ in session.events():
            pass


# ---------------------------------------------------------------------------
# BLOCKER 6: the tool contract (name="ask_agent", one arg="query") is a real
# cross-provider requirement -- endpoints/voice_live.py's _handle_tool_call
# reads that exact argument key with no fallback, and the persona prompt
# names `ask_agent` for every provider. Before this fix nothing in base.py or
# the conformance suite checked a provider's OWN declared tool matched, so a
# provider naming its tool/argument anything else would pass this suite
# cleanly and then silently deliver an empty query on every real call.
# ---------------------------------------------------------------------------


def test_ask_agent_tool_spec_is_the_fixed_contract():
    assert ASK_AGENT_TOOL_SPEC == ToolSpec(name="ask_agent", query_arg="query")


async def test_fake_provider_declares_the_shared_tool_spec():
    """RealtimeSession.tool_spec has no default (see base.py) -- unlike a
    real provider, FakeRealtimeSession exists to model a CONFORMING session,
    so it sets `tool_spec` explicitly to the canonical contract rather than
    relying on any inherited value."""
    session = FakeRealtimeSession()
    assert session.tool_spec == ASK_AGENT_TOOL_SPEC


async def test_contract_rejects_a_provider_with_a_different_tool_name():
    """The concrete failure mode the finding describes: an Azure-shaped
    provider that declares `ask_assistant(question=...)` instead of the
    shared contract must fail assert_realtime_contract, not pass it."""

    class _WrongToolNameSession(FakeRealtimeSession):
        tool_spec = ToolSpec(name="ask_assistant", query_arg="query")

    with pytest.raises(AssertionError, match="ask_assistant"):
        await assert_realtime_contract(lambda: _WrongToolNameSession())


async def test_contract_rejects_a_provider_with_a_different_argument_name():
    class _WrongArgSession(FakeRealtimeSession):
        tool_spec = ToolSpec(name="ask_agent", query_arg="question")

    with pytest.raises(AssertionError, match="question"):
        await assert_realtime_contract(lambda: _WrongArgSession())


async def test_contract_rejects_a_provider_that_forgot_to_override_tool_spec():
    """The likelier real mistake, per the base.py finding: `tool_spec` used to
    default to the canonical contract on `RealtimeSession` itself, so a
    provider that declares a genuinely different tool to its own SDK (e.g.
    `ask_assistant(question=...)`) but simply never overrides
    `tool_spec` at all would silently inherit ASK_AGENT_TOOL_SPEC, pass this
    suite, and then deliver `query=''` to the agent on every real call. This
    subclasses `RealtimeSession` directly (not `FakeRealtimeSession`, which
    now sets `tool_spec` itself) with no `tool_spec` mentioned anywhere --
    exactly the omission a provider author would make -- to prove the base
    class no longer papers over it with a default."""

    class _ForgotToOverrideSession(RealtimeSession):
        provider = "forgot_to_override"
        audio_format = AudioFormat(input_sample_rate=16000, output_sample_rate=24000)
        # tool_spec deliberately never set -- this omission is the bug under test.

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
                yield  # pragma: no cover -- makes this an async generator

            return _empty()

        async def close(self) -> None:
            pass

    with pytest.raises(AssertionError, match="tool_spec"):
        await assert_realtime_contract(lambda: _ForgotToOverrideSession())


async def test_contract_requires_streamed_capability_flag():
    """RealtimeSession.supports_streamed_tool_results has no default (see
    base.py) -- same forget-to-override rationale as tool_spec. A provider
    that never declares it must fail the contract instead of silently
    letting the endpoint guess. This subclasses RealtimeSession directly
    (not FakeRealtimeSession, which sets the flag itself) with the flag
    deliberately never mentioned -- exactly the omission a provider author
    would make. (A subclass of FakeRealtimeSession can't simulate this: the
    attribute lives on the parent, so `del` on the subclass raises
    AttributeError instead of masking it.)"""

    class _NoFlagSession(RealtimeSession):
        provider = "no_flag"
        audio_format = AudioFormat(input_sample_rate=16000, output_sample_rate=24000)
        tool_spec = ASK_AGENT_TOOL_SPEC
        # supports_streamed_tool_results deliberately never set -- this
        # omission is the bug under test.

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
                yield  # pragma: no cover -- makes this an async generator

            return _empty()

        async def close(self) -> None:
            pass

    with pytest.raises(AssertionError, match="supports_streamed_tool_results"):
        await assert_realtime_contract(lambda: _NoFlagSession())


async def test_contract_rejects_confirmation_without_finalized_transcripts():
    """The confirmation contract IS a final user transcript per utterance —
    that final is the verdict the endpoint waits for. So a provider cannot
    declare supports_barge_in_confirmation while leaving
    finalizes_user_transcripts False: whichever of the two is wrong, the
    endpoint either waits for a verdict that never arrives or refuses relay
    custody it could actually take."""

    class _ConfirmsButNeverFinalizes(FakeRealtimeSession):
        provider = "confirms_but_never_finalizes"
        supports_barge_in_confirmation = True
        finalizes_user_transcripts = False

    with pytest.raises(AssertionError, match="finalizes_user_transcripts"):
        await assert_realtime_contract(lambda: _ConfirmsButNeverFinalizes())
