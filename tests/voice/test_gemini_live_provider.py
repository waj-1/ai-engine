from types import SimpleNamespace

import pytest

from voice.live.base import ASK_AGENT_TOOL_SPEC, ToolResultMode
from voice.live.config import LiveVoiceConfig
from voice.live.providers.gemini_live import (
    ASK_AGENT_DECLARATION,
    GeminiLiveSession,
    _connect,
    _scheduling_for,
    _translate,
)


def test_ask_agent_declaration_is_built_from_the_shared_contract():
    """BLOCKER 6: the name/argument must come FROM voice/live/base.py's
    ASK_AGENT_TOOL_SPEC, not an independently hardcoded literal that could
    drift from it."""
    assert ASK_AGENT_DECLARATION["name"] == ASK_AGENT_TOOL_SPEC.name
    assert ASK_AGENT_TOOL_SPEC.query_arg in ASK_AGENT_DECLARATION["parameters"]["properties"]


def test_gemini_session_tool_spec_matches_the_shared_contract():
    assert GeminiLiveSession.tool_spec == ASK_AGENT_TOOL_SPEC


def _message(*, input_transcription=None, output_transcription=None, interrupted=False):
    return SimpleNamespace(
        server_content=SimpleNamespace(
            interrupted=interrupted,
            input_transcription=input_transcription,
            output_transcription=output_transcription,
            model_turn=None,
            turn_complete=False,
        ),
        tool_call=None,
    )


def test_ask_agent_declaration_exposes_a_single_query_argument():
    assert ASK_AGENT_DECLARATION["name"] == "ask_agent"
    props = ASK_AGENT_DECLARATION["parameters"]["properties"]
    assert list(props) == ["query"]
    assert ASK_AGENT_DECLARATION["parameters"]["required"] == ["query"]


def test_tool_result_modes_map_onto_gemini_scheduling():
    from google.genai import types as genai_types

    assert _scheduling_for(ToolResultMode.INTERRUPT) == genai_types.FunctionResponseScheduling.INTERRUPT
    assert _scheduling_for(ToolResultMode.WHEN_IDLE) == genai_types.FunctionResponseScheduling.WHEN_IDLE
    assert _scheduling_for(ToolResultMode.SILENT) == genai_types.FunctionResponseScheduling.SILENT


def test_audio_format_is_gemini_native_rates():
    session = GeminiLiveSession(LiveVoiceConfig(model_string="m", voice_id="Kore"), persona="p")
    assert session.audio_format.input_sample_rate == 16000
    assert session.audio_format.output_sample_rate == 24000


async def test_send_audio_before_start_is_a_provider_error():
    session = GeminiLiveSession(LiveVoiceConfig(model_string="m", voice_id="Kore"), persona="p")
    from voice.live.base import RealtimeProviderError

    with pytest.raises(RealtimeProviderError):
        await session.send_audio(b"\x00")


def _capture_connect_config(monkeypatch):
    """Patch google.genai.Client so `_connect` runs with no network, and
    capture the LiveConnectConfig it builds."""
    import voice.live.providers.gemini_live as mod

    captured = {}

    class _FakeLive:
        def connect(self, *, model, config):
            captured["model"] = model
            captured["config"] = config
            return object()  # never __aenter__'d in this test

    class _FakeAio:
        live = _FakeLive()

    class _FakeClient:
        def __init__(self, api_key=None):
            self.aio = _FakeAio()

    monkeypatch.setattr(mod.genai, "Client", _FakeClient)
    return captured


def test_connect_wires_configured_language_into_speech_config(monkeypatch):
    """Blocker 3: voice_config.live.language was resolved but never reached
    the provider -- an operator setting language="ar" got silence (provider
    default), not the requested language, and no error either. SpeechConfig
    has a `language_code` field in the pinned SDK (verified above the fix);
    `_connect` must now pass it through."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore", language="ar")

    _connect(cfg, "persona text")

    assert captured["config"].speech_config.language_code == "ar"


def test_connect_leaves_language_code_unset_when_not_configured(monkeypatch):
    """No language configured -> no language_code override, provider default
    behaviour is unchanged (additive fix, not a behaviour change for tenants
    that never set voice_config.live.language)."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore", language=None)

    _connect(cfg, "persona text")

    assert captured["config"].speech_config.language_code is None


# ---------------------------------------------------------------------------
# BLOCKER 5: honouring Transcription.finished instead of hardcoding final=True
# on every fragment -- previously one spoken question rendered as several
# user bubbles because every incremental Live-API fragment was marked final.
# ---------------------------------------------------------------------------


def test_translate_marks_interim_fragment_non_final():
    msg = _message(input_transcription=SimpleNamespace(text="how much", finished=False))
    events = _translate(msg)
    assert len(events) == 1
    assert events[0].type == "transcript"
    assert events[0].role == "user"
    assert events[0].text == "how much"
    assert events[0].final is False


def test_translate_marks_last_fragment_final():
    msg = _message(input_transcription=SimpleNamespace(text=" did I make today?", finished=True))
    events = _translate(msg)
    assert len(events) == 1
    assert events[0].final is True
    assert events[0].text == " did I make today?"


def test_translate_emits_finished_close_even_with_no_extra_text():
    """A final chunk with no additional delta must still close out the
    client's interim bubble -- dropping it (the old `if text:` guard) would
    leave the bubble marked interim forever."""
    msg = _message(input_transcription=SimpleNamespace(text="", finished=True))
    events = _translate(msg)
    assert len(events) == 1
    assert events[0].final is True
    assert events[0].text == ""


def test_translate_output_transcription_also_honours_finished():
    msg = _message(output_transcription=SimpleNamespace(text="Your total is", finished=False))
    events = _translate(msg)
    assert len(events) == 1
    assert events[0].role == "assistant"
    assert events[0].final is False


def test_translate_defaults_missing_finished_attribute_to_non_final():
    """A transcription object with no `finished` attribute at all (a stub or
    an older SDK shape) must not be silently treated as final."""
    msg = _message(input_transcription=SimpleNamespace(text="hi"))
    events = _translate(msg)
    assert events[0].final is False


async def test_provider_satisfies_the_contract_against_a_stub_transport(monkeypatch):
    """The conformance gate. Azure must pass this same suite."""
    from voice.live.testing import assert_realtime_contract
    import voice.live.providers.gemini_live as mod

    class _StubUpstream:
        async def send_realtime_input(self, **kwargs):
            return None

        async def send_client_content(self, **kwargs):
            return None

        async def send_tool_response(self, **kwargs):
            return None

        async def receive(self):
            if False:
                yield None
            return

        async def close(self):
            return None

    class _StubConnect:
        async def __aenter__(self):
            return _StubUpstream()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mod, "_connect", lambda cfg, persona: _StubConnect())

    await assert_realtime_contract(
        lambda: GeminiLiveSession(LiveVoiceConfig(model_string="m", voice_id="Kore"), persona="p")
    )


async def test_repeated_results_for_one_call_id_all_reach_the_transport():
    """Backs GeminiLiveSession.supports_streamed_tool_results = True: streamed
    sentences and the progress line both deliver SEVERAL results under ONE
    call id, and every one must reach the SDK. The old conformance loop
    incidentally covered this (one shared call id across modes); its per-mode
    parameterization removed that, so this is now the only test of the
    promise the endpoint's streaming gate relies on."""
    from voice.live.providers.gemini_live import GeminiLiveSession
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    sent = []

    class _RecordingUpstream:
        async def send_tool_response(self, **kwargs):
            sent.append(kwargs["function_responses"])

    session = GeminiLiveSession(LiveVoiceConfig(model_string="m", voice_id="Kore"), persona="p")
    session._upstream = _RecordingUpstream()
    await session.send_tool_result("call-1", {"spoken": "one."}, mode=ToolResultMode.WHEN_IDLE)
    await session.send_tool_result("call-1", {"spoken": "two."}, mode=ToolResultMode.WHEN_IDLE)
    assert len(sent) == 2
    assert [fr[0].response["spoken"] for fr in sent] == ["one.", "two."]
    assert all(fr[0].id == "call-1" for fr in sent)


# ---------------------------------------------------------------------------
# Task 2: TEXT modality for text_only sessions (voice_config.live.text_only).
# ---------------------------------------------------------------------------


def test_connect_text_only_uses_text_modality(monkeypatch):
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore", text_only=True)

    _connect(cfg, "persona")

    assert captured["config"].response_modalities == ["TEXT"]


def test_connect_default_keeps_audio_modality(monkeypatch):
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore")

    _connect(cfg, "persona")

    assert captured["config"].response_modalities == ["AUDIO"]


def test_connect_text_only_omits_speech_config_and_output_transcription(monkeypatch):
    """No audio ever plays or is produced in text_only mode, so there is
    nothing to voice or transcribe -- sending a voice config alongside TEXT
    modality is the kind of thing a preview API rejects whole-request."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore", text_only=True)

    _connect(cfg, "persona")

    assert captured["config"].speech_config is None
    assert captured["config"].output_audio_transcription is None
    # These stay unconditional even in text mode.
    assert captured["config"].input_audio_transcription is not None
    assert captured["config"].session_resumption is not None


def test_connect_default_keeps_speech_config_and_output_transcription(monkeypatch):
    """Regression guard: the default (voice) path is byte-for-byte unchanged."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore")

    _connect(cfg, "persona")

    assert captured["config"].speech_config is not None
    assert captured["config"].output_audio_transcription is not None


def test_translate_model_turn_text_part_becomes_assistant_transcript():
    message = SimpleNamespace(
        server_content=SimpleNamespace(
            interrupted=False,
            input_transcription=None,
            output_transcription=None,
            model_turn=SimpleNamespace(
                parts=[SimpleNamespace(inline_data=None, text="Noted: BP 120/80.")]
            ),
            turn_complete=False,
        ),
        tool_call=None,
    )
    events = _translate(message)
    assert [(e.type, e.role, e.text, e.final) for e in events] == [
        ("transcript", "assistant", "Noted: BP 120/80.", False)
    ]


def test_audio_mode_inline_data_part_emits_audio_only():
    """AUDIO-mode messages carry inline_data (bytes) and no text on the
    model_turn part -- lock that this yields ONLY an `audio` event, never a
    spurious transcript event from the text-part branch."""
    message = SimpleNamespace(
        server_content=SimpleNamespace(
            interrupted=False,
            input_transcription=None,
            output_transcription=None,
            model_turn=SimpleNamespace(
                parts=[SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x01"), text=None)]
            ),
            turn_complete=False,
        ),
        tool_call=None,
    )
    events = _translate(message)
    assert [(e.type, e.audio) for e in events] == [("audio", b"\x00\x01")]


async def test_tool_result_after_close_is_dropped_not_raised():
    """A delegated turn takes ~10s; a caller who hangs up mid-turn leaves a
    task that still has a result to deliver. That race is expected, and it
    used to log an ERROR traceback per hang-up -- which would bury a genuine
    tool-response failure."""
    from voice.live.providers.gemini_live import GeminiLiveSession
    from voice.live.config import LiveVoiceConfig
    from voice.live.base import ToolResultMode

    session = GeminiLiveSession(LiveVoiceConfig(model_string="m", voice_id="Kore"), persona="p")
    await session.close()  # never started, already closed
    # Must not raise, and must not need an upstream connection.
    await session.send_tool_result("call-1", {"spoken": "hi"}, mode=ToolResultMode.INTERRUPT)


# ---------------------------------------------------------------------------
# End-of-speech detection (extra_config.turn_detection -> AutomaticActivityDetection)
#
# Before this existed `_connect` sent no realtime_input_config at all, so the
# window paid before EVERY caller turn was whatever the provider chose —
# measured at ~1392ms end-of-speech -> first audio, against 1114ms for an
# explicit 600 (manual_tests/probe_gemini_turn_detection.py). It was also not
# an operator knob, unlike the Azure provider's, so nobody could tune it
# without a deploy.
# ---------------------------------------------------------------------------


def test_default_turn_detection_pins_the_measured_silence_window(monkeypatch):
    """The whole point of the change: an explicit window reaches the wire.

    Asserts the VALUE, not merely that something was sent — 600 is the
    measured knee (the fastest value that still held a 500ms think-pause) and
    drifting off it costs ~280ms on every turn or a split utterance, neither
    of which raises anything."""
    captured = _capture_connect_config(monkeypatch)

    _connect(LiveVoiceConfig(model_string="m", voice_id="Kore"), "persona")

    detection = captured["config"].realtime_input_config.automatic_activity_detection
    assert detection.silence_duration_ms == 600


def test_operator_override_wins_over_the_default(monkeypatch):
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m",
        voice_id="Kore",
        extra_config={"turn_detection": {"silence_duration_ms": 900, "prefix_padding_ms": 120}},
    )

    _connect(cfg, "persona")

    detection = captured["config"].realtime_input_config.automatic_activity_detection
    assert detection.silence_duration_ms == 900
    assert detection.prefix_padding_ms == 120


def test_explicit_null_hands_the_window_back_to_the_provider(monkeypatch):
    """The same "a null REMOVES a default" rule the Azure provider's merge
    has. Config that cannot express "unset" is config you need a deploy to
    fix."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m",
        voice_id="Kore",
        extra_config={"turn_detection": {"silence_duration_ms": None}},
    )

    _connect(cfg, "persona")

    assert captured["config"].realtime_input_config is None


def test_azure_shaped_turn_detection_block_does_not_take_the_session_down(monkeypatch):
    """`turn_detection` is deliberately the same operator key on both
    providers, so an Azure row's block WILL be copied onto a Gemini row. Its
    fields are not Gemini's: forwarded verbatim they reach
    AutomaticActivityDetection as unknown kwargs and raise at connect time,
    failing the whole call rather than one setting. The honoured subset must
    survive and the rest must be dropped."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m",
        voice_id="Kore",
        extra_config={
            "turn_detection": {
                "type": "azure_semantic_vad",
                "threshold": 0.7,
                "speech_duration_ms": 300,
                "interrupt_response": True,
                "auto_truncate": True,
                "remove_filler_words": True,
                "silence_duration_ms": 650,
            }
        },
    )

    _connect(cfg, "persona")

    detection = captured["config"].realtime_input_config.automatic_activity_detection
    assert detection.silence_duration_ms == 650


def test_sensitivity_accepts_a_bare_operator_word(monkeypatch):
    from google.genai import types as genai_types

    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m",
        voice_id="Kore",
        extra_config={"turn_detection": {"end_of_speech_sensitivity": "high"}},
    )

    _connect(cfg, "persona")

    detection = captured["config"].realtime_input_config.automatic_activity_detection
    assert detection.end_of_speech_sensitivity == genai_types.EndSensitivity.END_SENSITIVITY_HIGH


def test_an_unusable_value_is_dropped_rather_than_raised(monkeypatch):
    """A typo in one tuning key must not be able to take a live call down."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m",
        voice_id="Kore",
        extra_config={
            "turn_detection": {
                "silence_duration_ms": "not-a-number",
                "end_of_speech_sensitivity": "medium",
                "prefix_padding_ms": 80,
            }
        },
    )

    _connect(cfg, "persona")

    detection = captured["config"].realtime_input_config.automatic_activity_detection
    assert detection.prefix_padding_ms == 80
    assert detection.silence_duration_ms is None
    assert detection.end_of_speech_sensitivity is None


# ---------------------------------------------------------------------------
# The synthesized user-transcript `final`.
#
# Gemini Live never sets Transcription.finished on the INPUT side (probed
# 2026-08-19: every input_transcription came back finished=None, the last one
# included). The endpoint handles the caller's committed utterance ENTIRELY
# inside `if event.final:` — last_commit, the three stall-recovery tiers,
# _arm_silent_recovery, the supersede epoch bump, intent-triggered relay
# custody, and closing the client's interim bubble. None of it had ever run on
# this provider.
# ---------------------------------------------------------------------------


def _model_audio_message(*, input_transcription=None, turn_complete=False,
                         interrupted=False, generation_complete=False, audio=b"\x00\x01"):
    parts = [SimpleNamespace(inline_data=SimpleNamespace(data=audio), text=None)] if audio else []
    return SimpleNamespace(
        server_content=SimpleNamespace(
            interrupted=interrupted,
            input_transcription=input_transcription,
            output_transcription=None,
            model_turn=SimpleNamespace(parts=parts) if parts else None,
            turn_complete=turn_complete,
            generation_complete=generation_complete,
        ),
        tool_call=None,
    )


def test_user_transcript_is_finalized_when_the_model_takes_the_floor():
    """The probed shape: the caller's transcript and the model's first audio
    arrive in the SAME message, to the millisecond (+1089/+1089). That instant
    IS the commit, so the final must be emitted there — and BEFORE the audio,
    so the endpoint's commit handling runs ahead of the reply it triggered."""
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    events = _translate(
        _model_audio_message(
            input_transcription=SimpleNamespace(text="how much did I make", finished=False)
        ),
        state,
    )
    kinds = [(e.type, e.role, e.final) for e in events]
    assert kinds == [
        ("transcript", "user", False),
        ("transcript", "user", True),
        ("audio", None, False),
    ], kinds
    # Empty text on the synthetic final: the endpoint accumulates the deltas
    # and reads its own buffer, so repeating the words here doubles them.
    assert events[1].text == ""


def test_user_transcript_is_finalized_across_messages():
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    first = _translate(
        _message(input_transcription=SimpleNamespace(text="how much ", finished=False)), state
    )
    second = _translate(
        _message(input_transcription=SimpleNamespace(text="did I make", finished=False)), state
    )
    assert [e.final for e in first + second] == [False, False]

    third = _translate(_model_audio_message(), state)
    assert [(e.type, e.final) for e in third] == [("transcript", True), ("audio", False)]


def test_a_tool_call_finalizes_the_utterance_that_triggered_it():
    """A turn whose first model output is the ask_agent call, with no filler
    spoken first — the commit must still close, or last_commit stays empty and
    the blank-ask_agent recovery has nothing to recover."""
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    _translate(_message(input_transcription=SimpleNamespace(text="settlement?", finished=False)), state)
    events = _translate(
        SimpleNamespace(
            server_content=None,
            tool_call=SimpleNamespace(
                function_calls=[SimpleNamespace(id="c1", name="ask_agent", args={"query": "q"})]
            ),
        ),
        state,
    )
    assert [(e.type, e.final) for e in events] == [("transcript", True), ("tool_call", False)]


def test_audio_of_a_response_already_in_flight_never_finalizes_a_barge_in():
    """The caller speaking OVER the assistant would otherwise be 'finalized'
    by the next audio frame of the very speech she is interrupting — cutting
    her utterance in half and superseding her own question."""
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    _translate(_model_audio_message(), state)          # assistant has the floor
    _translate(_message(input_transcription=SimpleNamespace(text="no wait", finished=False)), state)
    mid = _translate(_model_audio_message(), state)    # same response, still speaking
    assert [(e.type, e.final) for e in mid] == [("audio", False)]

    # The provider closes the response, and only THEN does the next floor-
    # taking event finalize her utterance.
    _translate(_message(interrupted=True), state)
    after = _translate(_model_audio_message(), state)
    assert [(e.type, e.final) for e in after] == [("transcript", True), ("audio", False)]


def test_generation_complete_reopens_the_floor_not_turn_complete():
    """turn_complete is paced to the AUDIO timeline, not to generation: probed
    on one turn it arrived 7.0s after the model stopped producing (+10904ms vs
    +3881ms). Keying the floor on it would leave a response 'active' for
    seconds after it ended and swallow the next utterance's finalize."""
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    _translate(_model_audio_message(generation_complete=True), state)
    _translate(_message(input_transcription=SimpleNamespace(text="and today?", finished=False)), state)
    events = _translate(_model_audio_message(), state)
    assert [(e.type, e.final) for e in events] == [("transcript", True), ("audio", False)]


def test_a_real_provider_final_is_never_doubled():
    """If a future SDK starts setting Transcription.finished, that final must
    close the utterance on its own. A second, synthesized one would reach the
    endpoint with an empty buffer and be classified as a noise verdict —
    which cancels the reply to the question she just asked."""
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    _translate(_message(input_transcription=SimpleNamespace(text="how much", finished=True)), state)
    events = _translate(_model_audio_message(), state)
    assert [(e.type, e.final) for e in events] == [("audio", False)]


def test_model_speech_with_no_caller_utterance_is_left_alone():
    """The greeting, a holding line, and the answer's own sentences all arrive
    with no user transcript open. Synthesizing a commit for any of them would
    bump the supersede epoch and silence the answer in flight."""
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    events = _translate(_model_audio_message(), state)
    assert [(e.type, e.final) for e in events] == [("audio", False)]


def test_turn_detection_is_configured_for_text_only_sessions_too(monkeypatch):
    """text_only drops the OUTPUT-side blocks; end-of-speech detection is an
    INPUT-side setting and an ambient-scribe session commits caller turns
    exactly like a phone one does."""
    captured = _capture_connect_config(monkeypatch)

    _connect(LiveVoiceConfig(model_string="m", voice_id="Kore", text_only=True), "persona")

    assert captured["config"].speech_config is None
    detection = captured["config"].realtime_input_config.automatic_activity_detection
    assert detection.silence_duration_ms == 600


# ---------------------------------------------------------------------------
# Context-window compression — the long-call cliff (live 2026-08-27).
# Without a sliding window a session accumulates every audio token until the
# model's ceiling: tool-calling discipline collapsed first (eleven consecutive
# announce-then-stall recoveries, 08:05-08:12), then the service killed the
# connection (`receive failed`, 08:12:58). These tests pin that _connect
# always bounds the context unless an operator explicitly opts out.


def test_connect_enables_context_compression_by_default(monkeypatch):
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore")

    _connect(cfg, "persona text")

    cwc = captured["config"].context_window_compression
    assert cwc is not None
    assert cwc.trigger_tokens == 25600
    assert cwc.sliding_window.target_tokens == 12800


def test_connect_context_compression_operator_override_merges(monkeypatch):
    """Override one key, keep the other default — same merge semantics as
    extra_config.turn_detection."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m", voice_id="Kore",
        extra_config={"context_window_compression": {"trigger_tokens": 30000}},
    )

    _connect(cfg, "persona text")

    cwc = captured["config"].context_window_compression
    assert cwc.trigger_tokens == 30000
    assert cwc.sliding_window.target_tokens == 12800


def test_connect_context_compression_explicit_null_disables(monkeypatch):
    """An explicit null hands session limits back to the provider — the same
    opt-out contract turn_detection honours."""
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m", voice_id="Kore",
        extra_config={"context_window_compression": None},
    )

    _connect(cfg, "persona text")

    assert captured["config"].context_window_compression is None


def test_connect_context_compression_bad_type_falls_back_to_defaults(monkeypatch):
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m", voice_id="Kore",
        extra_config={"context_window_compression": "yes please"},
    )

    _connect(cfg, "persona text")

    cwc = captured["config"].context_window_compression
    assert cwc.trigger_tokens == 25600
    assert cwc.sliding_window.target_tokens == 12800


# ---------------------------------------------------------------------------
# Thinking models (gemini-2.5-flash-native-audio-*): thought summaries arrive
# as TEXT parts alongside the audio, and used to be forwarded as assistant
# transcript -- the model's internal monologue ("**Crafting the Opening
# Greeting** ...") rendered on the caller's screen (live on the staging sales
# tenant 2026-08-31, 4/4 probe calls). The rows opt into/out of thinking via
# extra_config.thinking_budget (0 disables it), and _translate must never
# surface a part marked `thought` regardless.
# ---------------------------------------------------------------------------


def _model_turn_message(parts):
    return SimpleNamespace(
        server_content=SimpleNamespace(
            interrupted=False,
            input_transcription=None,
            output_transcription=None,
            model_turn=SimpleNamespace(parts=parts),
            turn_complete=False,
        ),
        tool_call=None,
    )


def test_translate_drops_thought_parts():
    msg = _model_turn_message([
        SimpleNamespace(inline_data=None, text="**Crafting the Opening**\n\nI will say hi.", thought=True),
        SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x01"), text=None, thought=False),
        SimpleNamespace(inline_data=None, text="Hi, I'm the Waj AI assistant.", thought=False),
    ])
    events = _translate(msg)
    texts = [e.text for e in events if e.type == "transcript"]
    assert "Hi, I'm the Waj AI assistant." in texts
    assert not any("Crafting" in (t or "") for t in texts), (
        "a thought part leaked into the assistant transcript -- the caller's "
        "screen shows the model's internal monologue"
    )
    assert any(e.type == "audio" for e in events)


def test_connect_passes_thinking_budget_through(monkeypatch):
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(
        model_string="m", voice_id="Kore",
        extra_config={"thinking_budget": 0},
    )
    _connect(cfg, "persona text")
    assert captured["config"].thinking_config is not None
    assert captured["config"].thinking_config.thinking_budget == 0


def test_connect_omits_thinking_config_by_default(monkeypatch):
    captured = _capture_connect_config(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore")
    _connect(cfg, "persona text")
    assert captured["config"].thinking_config is None


# ---------------------------------------------------------------------------
# Language-code rejection retry: the native-audio line auto-detects the
# spoken language and refuses every explicit Arabic code with 1007
# "Unsupported language code" AT CONNECT — live on the staging sales tenant
# 2026-09-02, where it killed every Arabic-page call (en-US connected fine).
# start() must retry once without the code instead of failing the call.
# ---------------------------------------------------------------------------


def _patch_lang_rejecting_client(monkeypatch, rejected=("ar-SA", "ar")):
    """google.genai.Client whose live.connect fails __aenter__ when the
    config carries a rejected language_code, succeeds otherwise."""
    import voice.live.providers.gemini_live as mod

    attempts = []

    class _Ctx:
        def __init__(self, lang):
            self.lang = lang

        async def __aenter__(self):
            if self.lang in rejected:
                raise Exception(
                    f"1007 None. Unsupported language code '{self.lang}' "
                    f"for model models/native-audio-test"
                )
            return SimpleNamespace()

        async def __aexit__(self, *a):
            return False

    class _FakeLive:
        def connect(self, *, model, config):
            lang = config.speech_config.language_code if config.speech_config else None
            attempts.append(lang)
            return _Ctx(lang)

    class _FakeAio:
        live = _FakeLive()

    class _FakeClient:
        def __init__(self, api_key=None):
            self.aio = _FakeAio()

    monkeypatch.setattr(mod.genai, "Client", _FakeClient)
    return attempts


async def test_start_retries_without_a_rejected_language_code(monkeypatch):
    from voice.live.providers.gemini_live import GeminiLiveSession

    attempts = _patch_lang_rejecting_client(monkeypatch)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore", language="ar-SA")
    session = GeminiLiveSession(cfg, persona="p")

    await session.start()  # must not raise

    assert attempts == ["ar-SA", None], (
        f"expected one rejected attempt then a retry with no language_code, "
        f"got {attempts!r} — an Arabic caller gets 'service unavailable' "
        f"while English callers connect fine"
    )


async def test_start_does_not_mask_other_connect_failures(monkeypatch):
    """Only the unsupported-language rejection earns a retry — any other
    connect failure still raises immediately (no retry storm against a dead
    provider)."""
    import pytest
    from voice.live.base import RealtimeProviderError
    from voice.live.providers.gemini_live import GeminiLiveSession
    import voice.live.providers.gemini_live as mod

    attempts = []

    class _Ctx:
        async def __aenter__(self):
            raise Exception("503 service overloaded")

        async def __aexit__(self, *a):
            return False

    class _FakeLive:
        def connect(self, *, model, config):
            attempts.append(1)
            return _Ctx()

    class _FakeClient:
        def __init__(self, api_key=None):
            self.aio = SimpleNamespace(live=_FakeLive())

    monkeypatch.setattr(mod.genai, "Client", _FakeClient)
    cfg = LiveVoiceConfig(model_string="m", voice_id="Kore", language="ar-SA")
    session = GeminiLiveSession(cfg, persona="p")

    with pytest.raises(RealtimeProviderError):
        await session.start()
    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# V3 (voice E2E 2026-09-28): Gemini's turn_end carried no `reply`, so the app
# never merged a continuation and one answer became 3-4 chat bubbles. The
# verdict is taken when a response takes the floor and carried to ITS
# turn_end — turn_complete lags the audio timeline by seconds, so a verdict
# read at turn_end could already include the NEXT utterance.
# ---------------------------------------------------------------------------


def _turn_ends(events):
    return [e.turn_reply for e in events if e.type == "turn_end"]


def _tool_call_message(call_id="c1"):
    return SimpleNamespace(
        server_content=None,
        tool_call=SimpleNamespace(
            function_calls=[SimpleNamespace(id=call_id, name="ask_agent", args={"query": "q"})]
        ),
    )


def test_gemini_turn_reply_greeting_announce_holding_answer():
    """The live Arabic block-time turn from the E2E, frame by frame."""
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    ends = []
    # Greeting: no caller speech before it.
    ends += _turn_ends(_translate(_model_audio_message(), state))
    ends += _turn_ends(_translate(_model_audio_message(audio=None, turn_complete=True), state))
    # Caller speaks; transcript + first audio of the announce arrive together.
    ends += _turn_ends(_translate(_model_audio_message(
        input_transcription=SimpleNamespace(text="add a lunch break for Sara", finished=False)), state))
    ends += _turn_ends(_translate(_tool_call_message(), state))
    ends += _turn_ends(_translate(_model_audio_message(audio=None, turn_complete=True), state))
    # Holding line (WHEN_IDLE tool result) and two streamed answer sentences.
    for _ in range(3):
        ends += _turn_ends(_translate(_model_audio_message(), state))
        ends += _turn_ends(_translate(_model_audio_message(audio=None, turn_complete=True), state))
    assert ends == [False, True, False, False, False]


def test_gemini_turn_reply_is_not_stolen_by_the_next_utterance():
    """turn_complete of a continuation arrives AFTER the caller has started her
    next question (it is paced to the audio), so she talks over it and the
    service interrupts. The continuation keeps no verdict; her question's
    response is the reply."""
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    _translate(_model_audio_message(
        input_transcription=SimpleNamespace(text="revenue?", finished=False)), state)
    first = _turn_ends(_translate(_model_audio_message(audio=None, turn_complete=True), state))
    state.endpoint_requests += 1  # the answer, delivered as a tool result
    _translate(_model_audio_message(), state)  # the continuation starts
    _translate(_message(input_transcription=SimpleNamespace(text="and yesterday?", finished=False),
                        interrupted=True), state)
    second = _turn_ends(_translate(_model_audio_message(audio=None, turn_complete=True), state))
    third = _turn_ends(_translate(_model_audio_message(turn_complete=True), state))
    assert first == [True] and second == [None] and third == [True]


def test_gemini_echo_during_our_response_does_not_make_the_next_one_a_reply():
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    state.endpoint_requests += 1  # holding line
    _translate(_model_audio_message(), state)
    _translate(_message(input_transcription=SimpleNamespace(text="mm-hm", finished=False)), state)
    holding = _turn_ends(_translate(_model_audio_message(audio=None, turn_complete=True), state))
    state.endpoint_requests += 1  # the answer
    answer = _turn_ends(_translate(_model_audio_message(turn_complete=True), state))
    assert holding == [False] and answer == [False]


def test_gemini_interrupted_response_gets_no_guessed_verdict():
    from voice.live.providers.gemini_live import UserTurnState

    state = UserTurnState()
    _translate(_model_audio_message(), state)  # greeting in flight
    _translate(_message(input_transcription=SimpleNamespace(text="stop", finished=False),
                        interrupted=True), state)
    closing = _turn_ends(_translate(_model_audio_message(audio=None, turn_complete=True), state))
    reply = _turn_ends(_translate(_model_audio_message(turn_complete=True), state))
    assert closing == [None] and reply == [True]


def test_gemini_text_only_session_keeps_the_legacy_turn_end():
    """No audio and no tool call: nothing opens a response, no key is sent."""
    msg = SimpleNamespace(server_content=SimpleNamespace(
        interrupted=False, input_transcription=None, output_transcription=None,
        model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=None, text="Noted.")]),
        turn_complete=True), tool_call=None)
    assert _turn_ends(_translate(msg)) == [None]
