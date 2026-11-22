"""Unit tests for the azure_voice_live provider against a fake transport.

No network, no aiohttp socket: _open_transport is patched to return a fake
ws whose receive() serves scripted protocol events and whose send_str calls
are captured for assertion.
"""
import asyncio
import base64
import json

import pytest

import voice.live.providers.azure_voice_live as azure_mod
from voice.live.base import RealtimeProviderError, ToolResultMode
from voice.live.config import LiveVoiceConfig
from voice.live.providers.azure_voice_live import (
    AzureVoiceLiveSession,
    _session_payload,
    _voice_payload,
    _ws_url,
)
from voice.live.testing import assert_realtime_contract


class _FakeWSMsg:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = data


class _FakeWS:
    def __init__(self):
        import aiohttp
        self._aiohttp = aiohttp
        self.sent = []            # decoded JSON payloads, in order
        self._queue = asyncio.Queue()
        self.closed = False

    def feed(self, evt: dict):
        self._queue.put_nowait(
            _FakeWSMsg(self._aiohttp.WSMsgType.TEXT, json.dumps(evt))
        )

    async def receive(self):
        return await self._queue.get()

    async def send_str(self, data: str):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True
        self._queue.put_nowait(_FakeWSMsg(self._aiohttp.WSMsgType.CLOSED))


class _FakeHTTP:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _cfg(**over):
    cfg = LiveVoiceConfig()
    cfg.model_string = "gpt-4.1-mini"
    cfg.extra_config = {"endpoint": "https://voice-live-example.services.ai.azure.com"}
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def fake_ws(monkeypatch):
    ws = _FakeWS()

    async def _fake_open(cfg):
        return _FakeHTTP(), ws

    monkeypatch.setattr(azure_mod, "_open_transport", _fake_open)
    return ws


async def _started(ws) -> AzureVoiceLiveSession:
    session = AzureVoiceLiveSession(_cfg(), persona="probe persona")
    ws.feed({"type": "session.updated"})
    await session.start()
    return session


def test_ws_url_and_missing_endpoint():
    assert _ws_url(_cfg()) == (
        "wss://voice-live-example.services.ai.azure.com/voice-live/realtime"
        "?api-version=2026-04-10&model=gpt-4.1-mini"
    )
    with pytest.raises(RealtimeProviderError):
        _ws_url(_cfg(extra_config={}))
    # http:// or a bare hostname must be rejected BY NAME, not become a
    # non-wss URL that fails with an opaque connect error.
    with pytest.raises(RealtimeProviderError, match="https://"):
        _ws_url(_cfg(extra_config={"endpoint": "http://voice-live-example.services.ai.azure.com"}))


def test_session_payload_ignores_non_list_phrase_list(caplog):
    # Raw operator JSON: a bare string would list()-iterate into single
    # CHARACTERS and silently degrade recognition. Ignored + warned instead.
    import logging
    cfg = _cfg(phrase_list="Northwind")
    with caplog.at_level(logging.WARNING):
        s = _session_payload(cfg, "p")["session"]
    assert "phrase_list" not in s["input_audio_transcription"]
    assert any("phrase_list" in r.message for r in caplog.records)


def test_session_payload_shape():
    cfg = _cfg(language="ar-SA", phrase_list=["Northwind", "IBAN"],
               voice_by_language={"ar": "ar-SA-HamedNeural"})
    payload = _session_payload(cfg, "PERSONA")
    s = payload["session"]
    assert payload["type"] == "session.update"
    assert s["instructions"] == "PERSONA"
    assert s["turn_detection"]["type"] == "azure_semantic_vad"
    assert s["turn_detection"]["interrupt_response"] is True
    assert s["input_audio_transcription"] == {
        "model": "azure-speech", "language": "ar-SA",
        "phrase_list": ["Northwind", "IBAN"],
    }
    assert s["voice"] == {"name": "ar-SA-HamedNeural", "type": "azure-standard"}
    assert s["tools"][0]["name"] == "ask_agent"


def test_session_payload_turn_detection_override():
    cfg = _cfg(extra_config={"endpoint": "https://x", "turn_detection": {"silence_duration_ms": 700}})
    td = _session_payload(cfg, "p")["session"]["turn_detection"]
    assert td["silence_duration_ms"] == 700
    assert td["type"] == "azure_semantic_vad"  # defaults survive the merge


def test_session_payload_text_only_modality():
    cfg = _cfg(text_only=True)
    session = _session_payload(cfg, "persona")["session"]
    assert session["modalities"] == ["text"]
    assert "interim_response" not in session


def test_session_payload_text_only_omits_server_echo_cancellation():
    """The service REJECTS the whole session.update if a text-only session
    asks for server-side echo cancellation ("Server side echo cancellation is
    not supported when modalities is text-only" — live, staging 2026-08-10),
    and a rejected session.update kills the session outright."""
    session = _session_payload(_cfg(text_only=True), "persona")["session"]
    assert "input_audio_echo_cancellation" not in session
    # The mic side is untouched: we still hear the clinician.
    assert session["input_audio_noise_reduction"] == {
        "type": "azure_deep_noise_suppression"
    }
    assert session["input_audio_transcription"]["model"] == "azure-speech"


def test_session_payload_audio_mode_keeps_server_echo_cancellation():
    """Audio sessions must be untouched — the model hearing its own speech
    back through the caller's speaker is what this suppresses."""
    session = _session_payload(_cfg(), "persona")["session"]
    assert session["input_audio_echo_cancellation"] == {
        "type": "server_echo_cancellation"
    }


def test_session_payload_default_modalities_unchanged():
    cfg = _cfg()
    session = _session_payload(cfg, "persona")["session"]
    assert session["modalities"] == ["text", "audio"]


def test_translate_response_text_delta_and_done():
    session = AzureVoiceLiveSession(_cfg(text_only=True), persona="p")
    deltas = session._translate_nosend({"type": "response.text.delta", "delta": "Plan: "})
    assert [(e.type, e.role, e.text, e.final) for e in deltas] == [
        ("transcript", "assistant", "Plan: ", False)
    ]
    done = session._translate_nosend({"type": "response.text.done"})
    assert [(e.type, e.role, e.text, e.final) for e in done] == [
        ("transcript", "assistant", "", True)
    ]


def test_translate_response_text_suppressed_out_of_band():
    session = AzureVoiceLiveSession(_cfg(text_only=True), persona="p")
    session._response_is_oob = True
    assert session._translate_nosend({"type": "response.text.delta", "delta": "x"}) == []
    assert session._translate_nosend({"type": "response.text.done"}) == []


async def test_start_sends_session_update_and_awaits_ack(fake_ws):
    session = await _started(fake_ws)
    assert fake_ws.sent[0]["type"] == "session.update"
    await session.close()


async def test_start_surfaces_rejection(fake_ws, monkeypatch):
    http = _FakeHTTP()

    async def _fake_open(cfg):
        return http, fake_ws

    monkeypatch.setattr(azure_mod, "_open_transport", _fake_open)
    session = AzureVoiceLiveSession(_cfg(), persona="p")
    fake_ws.feed({"type": "error", "error": {"message": "bad voice"}})
    with pytest.raises(RealtimeProviderError, match="bad voice"):
        await session.start()
    # Review finding: start() failure paths must not leak the transport --
    # the endpoint never reaches session.close() for a session whose
    # start() itself raised.
    assert fake_ws.closed is True
    assert http.closed is True


async def test_early_events_before_ack_are_replayed_first(fake_ws):
    """Events arriving between session.update and its ack are buffered by
    start() and must surface from events() BEFORE anything live -- dropping
    them would lose the opening moments of the call."""
    session = AzureVoiceLiveSession(_cfg(), persona="p")
    fake_ws.feed({"type": "response.audio_transcript.delta", "delta": "early"})
    fake_ws.feed({"type": "session.updated"})
    await session.start()
    fake_ws.feed({"type": "response.audio_transcript.delta", "delta": "late"})
    events = await _drain(session, n=2)
    assert [(e.text, e.role) for e in events] == [
        ("early", "assistant"), ("late", "assistant"),
    ]
    await session.close()


async def test_start_fails_without_key(monkeypatch):
    monkeypatch.delenv("AZURE_VOICE_LIVE_API_KEY", raising=False)
    session = AzureVoiceLiveSession(_cfg(), persona="p")
    with pytest.raises(RealtimeProviderError, match="AZURE_VOICE_LIVE_API_KEY"):
        await session.start()


async def test_send_audio_and_text_turn(fake_ws):
    session = await _started(fake_ws)
    await session.send_audio(b"\x01\x02")
    assert fake_ws.sent[-1] == {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(b"\x01\x02").decode(),
    }
    await session.send_text_turn("hello")
    assert fake_ws.sent[-2]["item"]["content"][0]["text"] == "hello"
    assert fake_ws.sent[-1] == {"type": "response.create"}
    await session.close()


async def _drain(session, n=10):
    """Collect up to n events, stopping at queue-exhaustion via timeout."""
    out = []
    it = session.events()
    for _ in range(n):
        try:
            out.append(await asyncio.wait_for(it.__anext__(), timeout=0.2))
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    return out


# 24 frames of silence, base64'd -- a throwaway but well-formed
# response.audio.delta payload for tests that only care about the `reply`
# flag, not the PCM content.
_B64 = base64.b64encode(b"\x00\x00" * 24).decode()


async def _feed_events(session, ws, events):
    """Feed scripted server events and drain them through events().

    For confirmed-barge-in tests that only care about resulting STATE (the
    commit-seq stamps, _response_active) rather than the individual emitted
    RealtimeEvents -- same draining as test_translation_table above, just
    named for what the caller wants out of it. `n` is generous (more than
    any one event can possibly emit) since _drain stops at the first
    timeout regardless of how high it's set.
    """
    for evt in events:
        ws.feed(evt)
    await _drain(session, n=len(events) * 2 + 5)


async def _collect_events(session, ws, events):
    """Same as _feed_events, but returns what events() emitted instead of
    discarding it -- for tests that assert on the events themselves (e.g.
    the `reply` flag on audio events)."""
    for evt in events:
        ws.feed(evt)
    return await _drain(session, n=len(events) * 2 + 5)


def _sent_types(ws):
    """The `type` of everything we sent, in order, reduced to just that key
    -- lets a test assert `{"type": "response.cancel"} in _sent_types(ws)`
    without hand-picking the rest of the payload shape each time."""
    return [{"type": f.get("type")} for f in ws.sent]


async def test_translation_table(fake_ws):
    session = await _started(fake_ws)
    pcm = base64.b64encode(b"\x00\x01").decode()
    for evt in [
        {"type": "response.created"},
        {"type": "response.audio.delta", "delta": pcm},
        {"type": "response.audio_transcript.delta", "delta": "Hel"},
        {"type": "response.audio_transcript.done", "transcript": "Hello"},
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": "u1", "transcript": "hi there"},
        {"type": "input_audio_buffer.speech_started"},   # active -> interrupted
        {"type": "response.done"},
        {"type": "input_audio_buffer.speech_started"},   # idle -> counted, not acted on
        {"type": "input_audio_buffer.committed"},        # real utterance -> EOT + flush
        {"type": "error", "error": {"message": "boom"}},
    ]:
        fake_ws.feed(evt)
    events = await _drain(session)
    kinds = [(e.type, e.role, e.final) for e in events]
    assert ("audio", None, False) in kinds
    assert ("transcript", "assistant", False) in kinds
    assert ("transcript", "assistant", True) in kinds
    # assistant final closes the bubble with EMPTY text (deltas carried it)
    assert [e for e in events if e.type == "transcript" and e.role == "assistant" and e.final][0].text == ""
    assert [e for e in events if e.role == "user"][0].text == "hi there"
    # A real barge-in (speech_started while she was speaking) is the ONLY
    # thing that reports `interrupted`. The idle speech_started does NOT —
    # that one is usually her own voice through the speaker, and reacting to
    # it made her cut herself off mid-sentence. It IS reported separately, as
    # a metrics-only `speech_started_idle`, so a session summary can show
    # whether the service detects the caller's speech at all.
    assert sum(1 for e in events if e.type == "interrupted") == 1
    assert sum(1 for e in events if e.type == "speech_started_idle") == 1
    # A committed utterance still flushes playback (the service streams faster
    # than realtime, so the tail of the previous answer must not play over the
    # next question) but is NOT a barge-in — see the playback_flush test below.
    assert sum(1 for e in events if e.type == "playback_flush") == 1
    assert sum(1 for e in events if e.type == "turn_end") == 1
    # The committed utterance is ALSO the caller's end-of-turn signal, emitted
    # BEFORE its playback_flush twin so the metrics collector stamps EOT ahead of
    # any flush handling. speech_started never produces one — it isn't an EOT.
    assert sum(1 for e in events if e.type == "user_turn_end") == 1
    types = [e.type for e in events]
    assert types[types.index("user_turn_end") + 1] == "playback_flush"
    assert events[-1].type == "error" and "boom" in events[-1].message
    await session.close()


async def test_committed_utterance_flushes_playback_but_is_not_a_barge_in(fake_ws):
    """The bug this splits apart: a committed utterance was reported as
    `interrupted`, the endpoint bumps its barge-in epoch on every one of
    those, and a bumped epoch downgrades the pending delegated answer to
    SILENT. Since a caller saying anything at all during the ~10s delegated
    turn commits an utterance, an "mhm" lost the answer entirely — it was
    never spoken. Playback still has to flush; that is what playback_flush is.
    """
    session = await _started(fake_ws)
    fake_ws.feed({"type": "input_audio_buffer.committed"})
    events = await _drain(session)
    # EOT is stamped first (it is the metrics measurement point), then the
    # flush. Crucially NEITHER is `interrupted`.
    assert [e.type for e in events] == ["user_turn_end", "playback_flush"]
    await session.close()


async def test_speech_started_while_she_is_speaking_is_a_real_barge_in(fake_ws):
    session = await _started(fake_ws)
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "input_audio_buffer.speech_started"})
    events = await _drain(session)
    assert [e.type for e in events] == ["interrupted"]
    await session.close()


async def test_user_transcript_streamed_then_completed_no_double(fake_ws):
    session = await _started(fake_ws)
    fake_ws.feed({"type": "conversation.item.input_audio_transcription.delta",
                  "item_id": "u2", "delta": "hi "})
    fake_ws.feed({"type": "conversation.item.input_audio_transcription.completed",
                  "item_id": "u2", "transcript": "hi there"})
    events = await _drain(session)
    user = [e for e in events if e.role == "user"]
    assert [e.text for e in user] == ["hi ", ""]  # completed closes, never repeats
    assert user[-1].final is True
    await session.close()


async def test_tool_call_parsing_and_dedupe(fake_ws):
    session = await _started(fake_ws)
    fake_ws.feed({"type": "response.function_call_arguments.done",
                  "call_id": "c1", "name": "ask_agent",
                  "arguments": json.dumps({"query": "sales today"})})
    fake_ws.feed({"type": "response.output_item.done",
                  "item": {"type": "function_call", "call_id": "c1",
                           "name": "ask_agent", "arguments": "{}"}})
    fake_ws.feed({"type": "response.function_call_arguments.done",
                  "call_id": "c2", "name": "ask_agent", "arguments": "NOT JSON"})
    events = await _drain(session)
    calls = [e for e in events if e.type == "tool_call"]
    assert len(calls) == 2  # c1 deduped across the two event shapes
    assert calls[0].arguments == {"query": "sales today"}
    assert calls[1].arguments == {}  # malformed args never crash
    await session.close()


async def test_tool_result_modes(fake_ws):
    session = await _started(fake_ws)
    # SILENT: item only, no response.create
    await session.send_tool_result("s1", {"spoken": "x"}, mode=ToolResultMode.SILENT)
    assert fake_ws.sent[-1]["item"]["type"] == "function_call_output"
    # INTERRUPT while idle: item + response.create, no cancel
    await session.send_tool_result("i1", {"spoken": "y"}, mode=ToolResultMode.INTERRUPT)
    assert [p["type"] for p in fake_ws.sent[-2:]] == ["conversation.item.create", "response.create"]
    # INTERRUPT while active: cancel first
    fake_ws.feed({"type": "response.created"})
    await _drain(session, n=1)
    await session.send_tool_result("i2", {"spoken": "z"}, mode=ToolResultMode.INTERRUPT)
    assert [p["type"] for p in fake_ws.sent[-3:]] == ["response.cancel", "conversation.item.create", "response.create"]
    await session.close()


async def test_when_idle_queues_until_response_done(fake_ws):
    session = await _started(fake_ws)
    fake_ws.feed({"type": "response.created"})
    await _drain(session, n=1)
    await session.send_tool_result("w1", {"spoken": "later"}, mode=ToolResultMode.WHEN_IDLE)
    assert all(p.get("item", {}).get("call_id") != "w1" for p in fake_ws.sent)
    fake_ws.feed({"type": "response.done"})
    await _drain(session, n=2)
    sent_types = [p["type"] for p in fake_ws.sent[-2:]]
    assert sent_types == ["conversation.item.create", "response.create"]
    assert fake_ws.sent[-2]["item"]["call_id"] == "w1"
    await session.close()


async def test_second_result_for_same_call_rejected(fake_ws):
    session = await _started(fake_ws)
    await session.send_tool_result("dup", {"spoken": "1"}, mode=ToolResultMode.INTERRUPT)
    with pytest.raises(RealtimeProviderError, match="one tool result per call"):
        await session.send_tool_result("dup", {"spoken": "2"}, mode=ToolResultMode.INTERRUPT)
    await session.close()


async def test_mode_type_enforced_and_closed_drop(fake_ws):
    session = await _started(fake_ws)
    with pytest.raises(TypeError):
        await session.send_tool_result("c", {}, mode="interrupt")
    await session.close()
    sent_before = len(fake_ws.sent)
    await session.send_tool_result("late", {"spoken": "x"}, mode=ToolResultMode.INTERRUPT)
    assert len(fake_ws.sent) == sent_before  # dropped silently
    await session.close()  # idempotent


async def test_conformance_contract(monkeypatch):
    ws = _FakeWS()

    async def _fake_open(cfg):
        return _FakeHTTP(), ws

    monkeypatch.setattr(azure_mod, "_open_transport", _fake_open)

    def make():
        ws.feed({"type": "session.updated"})
        return AzureVoiceLiveSession(_cfg(), persona="p")

    await assert_realtime_contract(make)


def test_native_voice_row_changes_payload_shape():
    """A native-audio row (gpt-realtime) takes a bare voice string and an
    OpenAI recognizer; an Azure TTS row takes the {name,type} object and
    azure-speech. Sending the wrong shape is rejected at session.update, and
    phrase_list must be dropped for a recognizer that rejects it."""
    cfg = _cfg(
        language="ar-SA",
        voice_by_language={"ar": "marin"},
        phrase_list=["Northwind", "IBAN"],
        extra_config={
            "endpoint": "https://voice-live-example.services.ai.azure.com",
            "voice_type": "native",
            "transcription_model": "gpt-4o-transcribe",
        },
    )
    s = _session_payload(cfg, "p")["session"]
    assert s["voice"] == "marin"                       # bare string, not an object
    assert s["input_audio_transcription"]["model"] == "gpt-4o-transcribe"
    assert "phrase_list" not in s["input_audio_transcription"]

    # The cascaded default is unchanged: object voice, azure-speech, phrase list.
    cascaded = _cfg(language="ar-SA", voice_by_language={"ar": "ar-SA-HamedNeural"},
                    phrase_list=["Northwind"])
    c = _session_payload(cascaded, "p")["session"]
    assert c["voice"] == {"name": "ar-SA-HamedNeural", "type": "azure-standard"}
    assert c["input_audio_transcription"]["model"] == "azure-speech"
    assert c["input_audio_transcription"]["phrase_list"] == ["Northwind"]


async def test_out_of_band_filler_is_audio_only(fake_ws):
    """A native-audio model rejects ["text","audio"] on a response, which
    errored the holding line mid-turn live. The filler is speech: audio only."""
    session = await _started(fake_ws)
    await session.speak_out_of_band("Still working on that.")
    sent = fake_ws.sent[-1]
    assert sent["type"] == "response.create"
    assert sent["response"]["modalities"] == ["audio"]
    assert sent["response"]["conversation"] == "none"
    assert "Still working on that." in sent["response"]["instructions"]
    await session.close()


async def test_out_of_band_speech_is_not_context_free(fake_ws):
    """`input: []` was tried (2026-07-31) and reverted the SAME day: this
    service doesn't treat per-response instructions as a clean override, so a
    context-free out-of-band line looked like the first reply of a fresh call
    and the persona re-greeted the caller mid-conversation, in a different
    register — heard live as "it keeps restarting and the voice changes".
    Out-of-band speech keeps the conversation as context (and is used only
    for short standalone lines, never answer sentences — the endpoint now
    delivers answers as the single in-conversation tool result)."""
    session = await _started(fake_ws)
    await session.speak_out_of_band("Still working on that.")
    sent = fake_ws.sent[-1]
    assert sent["response"]["conversation"] == "none"
    assert "input" not in sent["response"]
    await session.close()


async def test_active_response_rejection_is_absorbed_not_surfaced(fake_ws):
    """The documented two-response.create race: the service rejects the
    duplicate with "Conversation already has an active response" — captured
    verbatim in a live call on 2026-07-31, where it rendered to the caller as
    a raw error line. Bookkeeping, never a caller-visible fault."""
    session = await _started(fake_ws)
    it = session.events()
    fake_ws.feed({"type": "error", "error": {
        "message": "Conversation already has an active response"}})
    fake_ws.feed({"type": "error", "error": {"message": "boom"}})
    events = []
    for _ in range(3):
        try:
            events.append(await asyncio.wait_for(it.__anext__(), timeout=0.1))
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1 and "boom" in errors[0].message
    await session.close()


async def test_oob_transcript_stays_out_of_the_log(fake_ws):
    """The endpoint logs out-of-band speech with its own explicit frame; the
    service's transcript of the same audio interleaved a second delta stream
    into whatever bubble the client had open (the garbled answer seen live).
    The response still ends the turn, and a normal response still streams."""
    session = await _started(fake_ws)
    await session.speak_out_of_band("One.")
    # `response.created` for OUR out-of-band line reports it as committed to
    # speech (the endpoint logs the transcript from this, rather than
    # assuming an enqueued line was spoken) -- but the SERVICE's transcript
    # of that same audio must still be swallowed, which is what the deltas
    # below assert.
    started = session._translate_nosend({"type": "response.created"})
    assert [(e.type, e.text) for e in started] == [("out_of_band_started", "One.")]
    assert session._translate_nosend(
        {"type": "response.audio_transcript.delta", "delta": "One."}) == []
    assert session._translate_nosend(
        {"type": "response.audio_transcript.done"}) == []
    end = session._translate_nosend({"type": "response.done"})
    assert [e.type for e in end] == ["turn_end"]
    # An in-conversation response (no pending out-of-band create) streams.
    session._translate_nosend({"type": "response.created"})
    deltas = session._translate_nosend(
        {"type": "response.audio_transcript.delta", "delta": "Hi"})
    assert [(e.type, e.text) for e in deltas] == [("transcript", "Hi")]
    await session.close()


async def test_tool_result_for_a_dropped_call_is_absorbed_not_surfaced(fake_ws):
    """Posting the closing tool result after a committed utterance superseded
    that call's response gets "Tool call ID ... not found in conversation" —
    bookkeeping that reached a live caller as three raw API-error bubbles."""
    session = await _started(fake_ws)
    it = session.events()
    fake_ws.feed({"type": "error", "error": {
        "message": "Tool call ID 'call_DEo9dg1YZkEYWXBv' not found in conversation."}})
    fake_ws.feed({"type": "error", "error": {"message": "boom"}})
    events = []
    for _ in range(3):
        try:
            events.append(await asyncio.wait_for(it.__anext__(), timeout=0.1))
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1 and "boom" in errors[0].message
    await session.close()


def test_default_turn_detection_lets_a_human_interrupt():
    """Measured against the live service: the *_assistant_speaking guards make
    a real interruption undetectable (the agent talks over the caller), while
    plain thresholds still reject noise. They were needed for the CASCADED
    model and belong on that row, never in the default."""
    td = _session_payload(_cfg(), "p")["session"]["turn_detection"]
    for guard in ("require_vowel_assistant_speaking",
                  "distinct_ci_phones_assistant_speaking",
                  "window_size_assistant_speaking"):
        assert guard not in td, f"{guard} in the default blocks human barge-in"
    assert td["interrupt_response"] is True and td["auto_truncate"] is True
    # Fast enough to feel instant, with a little margin over the bare defaults.
    assert td["threshold"] <= 0.7 and td["speech_duration_ms"] <= 300
    # A row may still opt INTO the strict guards (the cascaded one does).
    strict = _cfg(extra_config={"endpoint": "https://x", "turn_detection": {
        "require_vowel_assistant_speaking": True}})
    assert _session_payload(strict, "p")["session"]["turn_detection"][
        "require_vowel_assistant_speaking"] is True


def test_row_override_can_unset_a_default_with_null():
    """dict.update can add and change but never REMOVE, so a row could not
    switch a baked-in default off — the barge-in guards survived a row edit
    that meant to drop them, and only a redeploy could have fixed it. A null
    now unsets."""
    cfg = _cfg(extra_config={"endpoint": "https://x", "turn_detection": {
        "remove_filler_words": None,      # drop a default
        "threshold": 0.5,                  # change another
        "eagerness": "high",              # add a new one
    }})
    td = _session_payload(cfg, "p")["session"]["turn_detection"]
    assert "remove_filler_words" not in td
    assert td["threshold"] == 0.5
    assert td["eagerness"] == "high"
    assert td["type"] == "azure_semantic_vad"   # untouched defaults survive


async def test_her_own_echo_does_not_cut_her_off(fake_ws):
    """speech_started while NO response is active is almost always her own
    voice coming back through the caller's speaker. Interrupting on it flushed
    playback mid-sentence — "it keeps interrupting itself"."""
    session = await _started(fake_ws)
    fake_ws.feed({"type": "input_audio_buffer.speech_started"})   # idle
    events = await _drain(session, n=3)
    assert not [e for e in events if e.type == "interrupted"]
    await session.close()


async def test_idle_speech_started_is_counted_without_changing_behaviour(fake_ws):
    """Live sessions reported barge_ins=0 while callers said they had
    interrupted, and idle detections were dropped on the floor — so a zero
    could equally mean "the service never hears the caller" or "it hears
    them, just never mid-response". The idle branch now emits a countable
    event. It must stay behaviourally inert: no `interrupted`, no
    `playback_flush`, no EOT."""
    session = await _started(fake_ws)
    fake_ws.feed({"type": "input_audio_buffer.speech_started"})   # idle
    fake_ws.feed({"type": "input_audio_buffer.speech_started"})   # idle again
    events = await _drain(session, n=4)
    assert [e.type for e in events] == ["speech_started_idle", "speech_started_idle"]
    await session.close()


# ---------------------------------------------------------------------------
# Out-of-band queue (per-sentence streaming rides this)
# ---------------------------------------------------------------------------


def _oob_creates(ws):
    return [
        f for f in ws.sent
        if f.get("type") == "response.create"
        and (f.get("response") or {}).get("conversation") == "none"
    ]


async def test_oob_speaks_immediately_when_idle_and_queues_behind_itself(fake_ws):
    session = await _started(fake_ws)
    await session.speak_out_of_band("One.")
    # Idle line: first sentence goes out at once.
    creates = _oob_creates(fake_ws)
    assert len(creates) == 1 and "One." in creates[0]["response"]["instructions"]
    # Second sentence must WAIT — our own create is in flight, and sending a
    # second response.create before the first finishes is the live
    # "Conversation already has an active response" incident.
    await session.speak_out_of_band("Two.")
    assert len(_oob_creates(fake_ws)) == 1
    it = session.events()
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.done"})
    # `response.created` now yields out_of_band_started before response.done
    # yields turn_end; drain until turn_end, which is what pumps the queue.
    first = await asyncio.wait_for(it.__anext__(), timeout=0.2)
    assert first.type == "out_of_band_started"
    await asyncio.wait_for(it.__anext__(), timeout=0.2)  # turn_end drains
    creates = _oob_creates(fake_ws)
    assert len(creates) == 2 and "Two." in creates[1]["response"]["instructions"]
    await session.close()


async def test_oob_queues_while_a_response_is_active(fake_ws):
    session = await _started(fake_ws)
    it = session.events()
    pcm = base64.b64encode(b"\x00\x00").decode()
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "a1", "delta": pcm})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)  # audio; response active
    await session.speak_out_of_band("Queued.")
    assert _oob_creates(fake_ws) == []  # she is talking — hold the line
    fake_ws.feed({"type": "response.done"})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)  # turn_end drains
    creates = _oob_creates(fake_ws)
    assert len(creates) == 1 and "Queued." in creates[0]["response"]["instructions"]
    await session.close()


@pytest.mark.parametrize("barge_evt", [
    {"type": "input_audio_buffer.speech_started"},   # barge-in over her speech
    {"type": "input_audio_buffer.committed"},        # committed new utterance
])
async def test_oob_queue_is_cleared_when_the_caller_speaks(fake_ws, barge_evt):
    session = await _started(fake_ws)
    it = session.events()
    pcm = base64.b64encode(b"\x00\x00").decode()
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "a1", "delta": pcm})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)
    await session.speak_out_of_band("Stale one.")
    await session.speak_out_of_band("Stale two.")
    fake_ws.feed(barge_evt)
    fake_ws.feed({"type": "response.done"})
    # Drain whatever neutral events come out (interrupted / user_turn_end /
    # playback_flush / turn_end) — the assertion is on the WIRE: no stale
    # out-of-band create goes out after the caller spoke.
    for _ in range(4):
        try:
            await asyncio.wait_for(it.__anext__(), timeout=0.1)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    assert _oob_creates(fake_ws) == []
    await session.close()


async def test_send_text_turn_clears_the_queued_holding_line(fake_ws):
    """A relayed answer (send_text_turn) ends the wait the holding line was
    filling — same rule as a delivered tool result. Without the clear, a
    still-queued "still working on that" drains on the ANSWER's own
    response.done and is spoken right after it (live 2026-08-03, relay
    custody with the new relay-path holding line)."""
    session = await _started(fake_ws)
    it = session.events()
    pcm = base64.b64encode(b"\x00\x00").decode()
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "a1", "delta": pcm})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)   # response active
    await session.speak_out_of_band("Still working on that.")
    assert _oob_creates(fake_ws) == []                    # queued, not spoken
    await session.send_text_turn("Say exactly this to the caller: done.")
    fake_ws.feed({"type": "response.done"})
    for _ in range(3):
        try:
            await asyncio.wait_for(it.__anext__(), timeout=0.1)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    assert _oob_creates(fake_ws) == []                    # never drained
    await session.close()


def test_voice_params_merge_into_the_azure_voice_object():
    """extra_config.voice_params carries HD-voice expressiveness knobs
    (temperature, style, pitch, ...) into the azure-standard voice object —
    operator config, not source constants. A native (bare-string) voice has
    no object to hang keys off, so params are dropped there rather than
    breaking session.update."""
    cfg = _cfg(extra_config={
        "endpoint": "https://x",
        "voice_params": {"temperature": 0.8, "style": "calm"},
    })
    v = _voice_payload(cfg)
    assert v["temperature"] == 0.8 and v["style"] == "calm"
    assert v["name"] and v["type"] == "azure-standard"   # base shape intact

    native = _cfg(extra_config={
        "endpoint": "https://x", "voice_type": "native",
        "voice_params": {"temperature": 0.8},
    })
    assert isinstance(_voice_payload(native), str)        # bare string, no keys

    junk = _cfg(extra_config={"endpoint": "https://x", "voice_params": "oops"})
    assert "temperature" not in _voice_payload(junk)      # raw JSON tolerance


# ---------------------------------------------------------------------------
# Server-side interim responses (Azure's purpose-built tool-call filler)
# ---------------------------------------------------------------------------


def test_session_payload_omits_interim_response_by_default():
    """Opt-in per model row: the block only exists when the operator set
    extra_config.interim_response — an unexpected key rejected at
    session.update would kill every call for the tenant."""
    assert "interim_response" not in _session_payload(_cfg(), "p")["session"]


def test_session_payload_builds_interim_response_from_progress_config():
    """The operator opts in with the mechanism only; the WORDS come from the
    same progress_spoken(_by_language) knobs the endpoint's holding line
    uses, so the bilingual wording is configured once, not twice."""
    cfg = _cfg(
        language="en-US",
        progress_after_sec=7.0,
        progress_spoken="Hold on.",
        progress_spoken_by_language={"en": ["Hold on.", "One sec."]},
        extra_config={"endpoint": "https://x",
                      "interim_response": {"triggers": ["tool"]}},
    )
    ir = _session_payload(cfg, "p")["session"]["interim_response"]
    assert ir["type"] == "static_interim_response"
    assert ir["triggers"] == ["tool"]
    assert ir["texts"] == ["Hold on.", "One sec."]
    assert ir["latency_threshold_ms"] == 7000


def test_session_payload_interim_response_operator_values_win():
    cfg = _cfg(
        progress_spoken="Ignored.",
        progress_after_sec=8.0,
        extra_config={"endpoint": "https://x",
                      "interim_response": {"type": "static_interim_response",
                                           "texts": ["Own text."],
                                           "latency_threshold_ms": 1500}},
    )
    ir = _session_payload(cfg, "p")["session"]["interim_response"]
    assert ir["texts"] == ["Own text."]
    assert ir["latency_threshold_ms"] == 1500


def test_interim_response_not_sent_for_a_native_voice_row():
    """Documented: "Realtime audio models don't support interim responses" —
    only cascaded mode takes the block, so a native row must not send it."""
    cfg = _cfg(extra_config={"endpoint": "https://x", "voice_type": "native",
                             "interim_response": {"triggers": ["tool"]}})
    assert "interim_response" not in _session_payload(cfg, "p")["session"]


def test_session_declares_the_interim_capability():
    """The endpoint skips its own holding line when the session's upstream
    fills the wait itself — the flag must track exactly when the block is
    actually sent (so a native row keeps the endpoint's holding line)."""
    on = AzureVoiceLiveSession(
        _cfg(extra_config={"endpoint": "https://x",
                           "interim_response": {"triggers": ["tool"]}}),
        persona="p",
    )
    assert on.handles_interim_response is True
    off = AzureVoiceLiveSession(_cfg(), persona="p")
    assert off.handles_interim_response is False
    native = AzureVoiceLiveSession(
        _cfg(extra_config={"endpoint": "https://x", "voice_type": "native",
                           "interim_response": {"triggers": ["tool"]}}),
        persona="p",
    )
    assert native.handles_interim_response is False


def test_text_only_never_declares_the_interim_capability():
    """text_only sessions never get an interim_response block from
    _session_payload (no audio to fill dead air with) — the flag must agree,
    or the endpoint would wrongly believe Azure has it covered and skip its
    own filler too, leaving no filler at all for that turn."""
    text_only_with_block = AzureVoiceLiveSession(
        _cfg(text_only=True,
             extra_config={"endpoint": "https://x",
                           "interim_response": {"triggers": ["tool"]}}),
        persona="p",
    )
    assert text_only_with_block.handles_interim_response is False
    # Default (audio) mode with the same block still reports True.
    default_with_block = AzureVoiceLiveSession(
        _cfg(extra_config={"endpoint": "https://x",
                           "interim_response": {"triggers": ["tool"]}}),
        persona="p",
    )
    assert default_with_block.handles_interim_response is True


# ---------------------------------------------------------------------------
# The answer supersedes the holding line (fill-the-silence fixes)
# ---------------------------------------------------------------------------


async def test_interrupt_result_clears_queued_holding_lines(fake_ws):
    """A holding line still queued when the answer arrives must never be
    spoken. Live symptom: the caller hears "Still working on that" AFTER
    (or instead of) the answer — the queue survived the delivery, and the
    cancel that INTERRUPT sends opens an idle window (the cancelled
    response's response.done) that _pump_oob immediately claims."""
    session = await _started(fake_ws)
    fake_ws.feed({"type": "response.created"})   # she is announcing the lookup
    await _drain(session, n=1)
    await session.speak_out_of_band("Still working on that.")
    assert _oob_creates(fake_ws) == []           # queued behind the announce

    deliver = asyncio.create_task(session.send_tool_result(
        "c1", {"spoken": "Answer."}, mode=ToolResultMode.INTERRUPT))
    await asyncio.sleep(0.05)                    # cancel sent; waiting for idle
    fake_ws.feed({"type": "response.done"})      # the cancelled announce ends
    await _drain(session, n=1)
    await asyncio.wait_for(deliver, timeout=2)
    # The answer's own turn runs and finishes...
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.done"})
    await _drain(session, n=2)
    # ...and the stale holding line never went out — not into the idle window
    # the cancel opened, and not after the answer.
    assert _oob_creates(fake_ws) == []
    await session.close()


async def test_when_idle_result_clears_queued_holding_lines(fake_ws):
    """Same staleness bug via the WHEN_IDLE path: the queued answer flushes on
    the first response.done, and without the clear the holding line would
    drain on the SECOND (the answer's own) — spoken right after the answer."""
    session = await _started(fake_ws)
    fake_ws.feed({"type": "response.created"})
    await _drain(session, n=1)
    await session.speak_out_of_band("Hold on.")
    await session.send_tool_result(
        "c1", {"spoken": "Answer."}, mode=ToolResultMode.WHEN_IDLE)
    fake_ws.feed({"type": "response.done"})      # idle flush delivers the answer
    await _drain(session, n=1)
    assert fake_ws.sent[-1] == {"type": "response.create"}
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.done"})      # the answer finishes
    await _drain(session, n=2)
    assert _oob_creates(fake_ws) == []
    await session.close()


async def test_interrupt_delivery_cancels_an_inflight_holding_line_by_id(fake_ws):
    """The answer landing inside the one-RTT window while a holding line's
    response.create is unacknowledged must not race it: the collision gets the
    ANSWER's create rejected ("Conversation already has an active response",
    absorbed as bookkeeping), and the caller hears the filler and then
    silence, forever.

    It must not merely WAIT the line out either, which is what it did until
    2026-08-05: there is no id to name yet, and a `response.cancel` that names
    nothing does not touch an out-of-band response (live-probed: 2.34s more
    audio, response.done COMPLETED). So the delivery waits the one RTT for
    `response.created`, then cancels the line the caller is about to hear.
    """
    session = await _started(fake_ws)
    await session.speak_out_of_band("Hold on.")   # idle -> create sent at once
    assert len(_oob_creates(fake_ws)) == 1        # in flight, unacknowledged

    deliver = asyncio.create_task(session.send_tool_result(
        "c1", {"spoken": "Answer."}, mode=ToolResultMode.INTERRUPT))
    await asyncio.sleep(0.05)
    # No second response.create while the holding line's is unacknowledged --
    # and no useless nameless cancel either.
    assert sum(1 for f in fake_ws.sent if f["type"] == "response.create") == 1
    assert not [f for f in fake_ws.sent if f["type"] == "response.cancel"]

    fake_ws.feed({"type": "response.created",     # the line's create lands...
                  "response": {"id": "resp_hold"}})
    await _drain(session, n=1)
    await asyncio.sleep(0.05)
    # ...and NOW the cancel goes out, naming it.
    assert {"type": "response.cancel", "response_id": "resp_hold"} in fake_ws.sent

    fake_ws.feed({"type": "response.done"})       # the line is cancelled/ends
    await _drain(session, n=1)
    await asyncio.wait_for(deliver, timeout=2)
    assert fake_ws.sent[-2]["item"]["call_id"] == "c1"
    assert fake_ws.sent[-1] == {"type": "response.create"}
    await session.close()


async def test_rejected_answer_create_is_retried_once_idle(fake_ws):
    """If the answer's response.create is still rejected (a response raced
    ours), the answer item is already in the conversation but nothing would
    ever speak it — dead air with no error. Re-issue response.create when the
    racing response finishes."""
    session = await _started(fake_ws)
    await session.send_tool_result(
        "c1", {"spoken": "Answer."}, mode=ToolResultMode.INTERRUPT)
    assert fake_ws.sent[-1] == {"type": "response.create"}
    n_before = sum(1 for f in fake_ws.sent if f["type"] == "response.create")
    fake_ws.feed({"type": "error", "error": {
        "message": "Conversation already has an active response"}})
    fake_ws.feed({"type": "response.done"})       # the racing response ends
    await _drain(session, n=2)
    assert (
        sum(1 for f in fake_ws.sent if f["type"] == "response.create")
        == n_before + 1
    )
    await session.close()


async def test_text_turn_supersedes_the_answer_create_retry_marker(fake_ws):
    """Live 2026-07-31 (triple OTP announcement): in a relay-heavy session a
    say-exactly TEXT turn's response.create can collide with the model's
    auto-response. If a stale answer-create marker is still armed, the
    absorbed rejection schedules a retry that re-reads the LAST answer — the
    caller hears the same line twice. Any later create that is not an answer
    create must disarm the marker."""
    session = await _started(fake_ws)
    await session.send_tool_result(
        "c1", {"spoken": "Answer."}, mode=ToolResultMode.INTERRUPT)
    n_before = sum(1 for f in fake_ws.sent if f["type"] == "response.create")
    # A relayed say-exactly delivery follows — its create is NOT an answer
    # create and must supersede the marker.
    await session.send_text_turn("Say exactly this to the caller: hi.")
    fake_ws.feed({"type": "error", "error": {
        "message": "Conversation already has an active response"}})
    fake_ws.feed({"type": "response.done"})
    await _drain(session, n=3)
    # +1 for the text turn's own create; NO retry create on top of it.
    assert (
        sum(1 for f in fake_ws.sent if f["type"] == "response.create")
        == n_before + 1
    )
    await session.close()


async def test_cancellation_failed_is_absorbed_not_surfaced(fake_ws):
    """INTERRUPT sends response.cancel whenever a response MAY be in flight;
    if it already ended, the service answers "Cancellation failed: no active
    response" — Microsoft's own function-calling sample swallows exactly this
    error as benign bookkeeping, and surfacing it painted a raw error bubble."""
    session = await _started(fake_ws)
    it = session.events()
    fake_ws.feed({"type": "error", "error": {
        "message": "Cancellation failed: no active response found"}})
    fake_ws.feed({"type": "error", "error": {"message": "boom"}})
    events = []
    for _ in range(3):
        try:
            events.append(await asyncio.wait_for(it.__anext__(), timeout=0.1))
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1 and "boom" in errors[0].message
    await session.close()


async def test_rejected_answer_create_not_retried_after_a_new_utterance(fake_ws):
    """The commit guard: when the racing response was the model answering the
    caller's NEW committed utterance, re-creating a response for the old
    answer would speak a non-sequitur behind it. The retry only fires when no
    utterance committed since the answer's create was sent."""
    session = await _started(fake_ws)
    await session.send_tool_result(
        "c1", {"spoken": "Answer."}, mode=ToolResultMode.INTERRUPT)
    n_before = sum(1 for f in fake_ws.sent if f["type"] == "response.create")
    fake_ws.feed({"type": "input_audio_buffer.committed"})  # caller moved on
    fake_ws.feed({"type": "error", "error": {
        "message": "Conversation already has an active response"}})
    fake_ws.feed({"type": "response.done"})
    await _drain(session, n=4)
    assert (
        sum(1 for f in fake_ws.sent if f["type"] == "response.create")
        == n_before
    )
    await session.close()


# ---------------------------------------------------------------------------
# report_playback -> conversation.item.truncate
# ---------------------------------------------------------------------------


def _truncates(ws):
    return [f for f in ws.sent if f.get("type") == "conversation.item.truncate"]


async def _drain_iter(it) -> None:
    """Pull events until the pump has nothing left, so provider state
    (`_response_active`, `_active_item_id`) reflects every fed frame."""
    while True:
        try:
            await asyncio.wait_for(it.__anext__(), timeout=0.05)
        except (asyncio.TimeoutError, StopAsyncIteration):
            return


def _ms_of_pcm(ms: int) -> str:
    # 24kHz mono pcm16 -> 48 bytes per ms.
    return base64.b64encode(b"\x00" * (48 * ms)).decode()


async def test_report_playback_truncates_the_heard_item(fake_ws):
    session = await _started(fake_ws)
    it = session.events()
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "i1", "delta": _ms_of_pcm(100)})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)
    fake_ws.feed({"type": "response.done"})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)

    await session.report_playback(40)
    truncs = _truncates(fake_ws)
    assert truncs == [{
        "type": "conversation.item.truncate",
        "item_id": "i1",
        "content_index": 0,
        "audio_end_ms": 40,
    }]
    # Same item never truncated twice — the second (later) report would only
    # extend it, which the service rejects anyway.
    await session.report_playback(60)
    assert len(_truncates(fake_ws)) == 1
    await session.close()


async def test_report_playback_skips_oob_audio_and_full_playback(fake_ws):
    session = await _started(fake_ws)
    it = session.events()
    # Out-of-band response first: its audio is not a conversation item.
    await session.speak_out_of_band("Hold on.")
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "oob-x", "delta": _ms_of_pcm(100)})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)
    fake_ws.feed({"type": "response.done"})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)
    # Then a REAL conversation item (a server-created response — no pending
    # OOB flag, so its audio is ledgered under its item id).
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "i2", "delta": _ms_of_pcm(100)})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)
    fake_ws.feed({"type": "response.done"})
    # Drain rather than count: the OOB response.created also yields an
    # `out_of_band_started`, so a fixed number of __anext__ calls leaves the
    # pump one event behind and the session still reading as mid-response.
    # Harmless while report_playback ignored live state; now that it defers a
    # truncate for the item still generating, an under-drained pump would
    # fake that condition here.
    await _drain_iter(it)

    await session.report_playback(50)      # inside the OOB segment
    assert _truncates(fake_ws) == []       # nothing to truncate
    await session.report_playback(150)     # 50ms into i2
    truncs = _truncates(fake_ws)
    assert len(truncs) == 1 and truncs[0]["item_id"] == "i2"
    assert truncs[0]["audio_end_ms"] == 50
    # Heard everything -> nothing to repair.
    fake_ws.sent.clear()
    await session.report_playback(10_000)
    assert _truncates(fake_ws) == []
    await session.close()


async def test_report_playback_defers_truncate_while_the_item_is_generating(fake_ws):
    """Truncating the item a response is STILL GENERATING stops that response
    mid-word — the service simply ends it, and reports `status: completed`.

    Live A/B against gpt-realtime, 2026-08-06, same session shape and the same
    turn_detection as a live client row:

        control (no report_playback)  30150ms audio, counted 1..20
        one report_playback at 1.0s    1750ms audio, transcript dies at
        (200ms behind the stream)      "اثنان"

    Both `status: completed`, which is why the 04:13 call's engine log carried
    no flush, no truncate, no cancel and no barge-in while the caller heard
    two replies stop mid-word. Every pre-existing report_playback test fed
    `response.done` BEFORE reporting, so this window was never covered.
    """
    session = await _started(fake_ws)
    it = session.events()
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "i1", "delta": _ms_of_pcm(400)})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)

    # The browser flushed 200ms into a reply the service is 4x ahead on.
    await session.report_playback(200)
    assert _truncates(fake_ws) == [], "truncate sent into a live response"

    # A later report for the same item refines the offset upward (more heard).
    await session.report_playback(250)
    assert _truncates(fake_ws) == []

    # response.done: the repair is now free, and lands before anything else
    # the drain does.
    fake_ws.feed({"type": "response.done"})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)
    assert _truncates(fake_ws) == [{
        "type": "conversation.item.truncate",
        "item_id": "i1",
        "content_index": 0,
        "audio_end_ms": 250,
    }]
    # And it is spent: a later report cannot re-truncate the same item.
    await session.report_playback(260)
    assert len(_truncates(fake_ws)) == 1
    await session.close()


async def test_report_playback_still_truncates_a_finished_item_immediately(fake_ws):
    """The deferral is scoped to the ACTIVE item only. An older item — the
    greeting the caller talked over, which is the case this mechanism exists
    for — is truncated on the spot, with no response.done needed."""
    session = await _started(fake_ws)
    it = session.events()
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "i1", "delta": _ms_of_pcm(400)})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)
    fake_ws.feed({"type": "response.done"})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)
    # New response, new item, still generating.
    fake_ws.feed({"type": "response.created"})
    fake_ws.feed({"type": "response.audio.delta", "item_id": "i2", "delta": _ms_of_pcm(400)})
    await asyncio.wait_for(it.__anext__(), timeout=0.2)

    # Offset lands inside the FINISHED item — nothing is generating it.
    await session.report_playback(100)
    truncs = _truncates(fake_ws)
    assert len(truncs) == 1 and truncs[0]["item_id"] == "i1"
    assert truncs[0]["audio_end_ms"] == 100
    await session.close()


async def test_response_done_status_is_logged_when_not_completed(fake_ws, caplog):
    """`status` used to be discarded, so a service-side end of the response was
    invisible. It is not the 04:13 defect (that one says `completed`) — it is
    how the other family gets ruled out in one line."""
    session = await _started(fake_ws)
    it = session.events()
    with caplog.at_level("INFO", logger="voice.live.providers.azure_voice_live"):
        fake_ws.feed({"type": "response.done", "response": {
            "status": "incomplete",
            "status_details": {"type": "incomplete", "reason": "content_filter"},
        }})
        await asyncio.wait_for(it.__anext__(), timeout=0.2)
    assert [r for r in caplog.records if "response ended" in r.getMessage()
            and "status=incomplete" in r.getMessage()
            and "content_filter" in r.getMessage()]
    caplog.clear()
    with caplog.at_level("INFO", logger="voice.live.providers.azure_voice_live"):
        fake_ws.feed({"type": "response.done", "response": {"status": "completed"}})
        await asyncio.wait_for(it.__anext__(), timeout=0.2)
    assert not [r for r in caplog.records if "response ended" in r.getMessage()]
    await session.close()


async def test_truncate_rejection_is_absorbed_not_surfaced(fake_ws):
    """A truncate racing the service's own auto_truncate gets rejected —
    expected bookkeeping, never an error bubble in front of a caller."""
    session = await _started(fake_ws)
    it = session.events()
    fake_ws.feed({"type": "error", "error": {
        "message": "audio_end_ms is greater than the actual audio duration"}})
    # Third live wording of the same rejection (reached a caller 2026-07-31):
    fake_ws.feed({"type": "error", "error": {
        "message": "Audio content of 1450ms is already shorter than 2309ms"}})
    fake_ws.feed({"type": "error", "error": {"message": "boom"}})
    events = []
    for _ in range(4):
        try:
            events.append(await asyncio.wait_for(it.__anext__(), timeout=0.1))
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1 and "boom" in errors[0].message
    await session.close()


# --- speaking_rate (voice_config.live.speaking_rate) -------------------


def test_voice_payload_adds_rate_on_the_azure_standard_object_form():
    cfg = _cfg(voice_by_language={"ar": "ar-SA-HamedNeural"}, language="ar-SA",
               speaking_rate="+10%")
    payload = _voice_payload(cfg)
    assert payload == {"name": "ar-SA-HamedNeural", "type": "azure-standard", "rate": "+10%"}


def test_voice_payload_has_no_rate_key_when_speaking_rate_is_unset():
    """Byte-identical to the pre-speaking-rate payload shape."""
    cfg = _cfg(voice_by_language={"ar": "ar-SA-HamedNeural"}, language="ar-SA")
    payload = _voice_payload(cfg)
    assert payload == {"name": "ar-SA-HamedNeural", "type": "azure-standard"}
    assert "rate" not in payload


def test_voice_payload_ignores_speaking_rate_on_the_native_bare_string_form():
    """A native realtime model's voice is a bare string -- there is no object
    to hang a `rate` key off, and sending an unrecognised shape is rejected
    at session.update. speaking_rate is silently inert here, not an error."""
    cfg = _cfg(
        voice_by_language={"ar": "marin"},
        language="ar-SA",
        speaking_rate="+10%",
        extra_config={
            "endpoint": "https://voice-live-example.services.ai.azure.com",
            "voice_type": "native",
        },
    )
    payload = _voice_payload(cfg)
    assert payload == "marin"


def test_session_payload_carries_the_rate_through_to_the_voice_field():
    """End-to-end: _session_payload's `voice` key reflects _voice_payload's
    rate addition, not just the unit-tested helper in isolation."""
    cfg = _cfg(voice_by_language={"ar": "ar-SA-HamedNeural"}, language="ar-SA",
               speaking_rate="0.9")
    s = _session_payload(cfg, "p")["session"]
    assert s["voice"] == {"name": "ar-SA-HamedNeural", "type": "azure-standard", "rate": "0.9"}


async def test_cancel_active_response_cancels_only_when_active(fake_ws):
    """Relay custody silences the model's in-flight reply; with nothing
    active there is nothing to cancel and nothing must be sent."""
    session = await _started(fake_ws)
    sent_before = len(fake_ws.sent)
    await session.cancel_active_response()
    assert len(fake_ws.sent) == sent_before          # idle -> no-op
    session._translate_nosend({"type": "response.created"})
    fake_ws.feed({"type": "response.done"})           # lets _await_idle settle
    import asyncio as _a
    it = session.events()
    task = _a.ensure_future(it.__anext__())
    await session.cancel_active_response()
    cancels = [f for f in fake_ws.sent if f.get("type") == "response.cancel"]
    assert len(cancels) == 1
    task.cancel()
    await session.close()


async def test_cancel_barged_in_response_spares_the_callers_reply(fake_ws):
    """The guard: cancel only a response created BEFORE the caller's speech
    onset (the one they talked over). After their utterance commits, the
    service auto-creates the REPLY — a confirmed-barge-in cancel arriving
    late must not kill it.

    Deliberately NO `_await_idle` stub: the elapsed-time assertion below is
    the regression pin for the settle this method must never do (see the
    comment on cancel_barged_in_response). The endpoint awaits it from
    inside the pump body, where `_response_active` cannot change — a settle
    would burn the full 1.5s timeout on every real barge-in.
    """
    import asyncio as _a

    session = await _started(fake_ws)
    # She responds (created at commit_seq 0), caller starts talking over it.
    await _feed_events(session, fake_ws, [
        {"type": "response.created"},
        {"type": "input_audio_buffer.speech_started"},
    ])
    _t0 = _a.get_running_loop().time()
    await session.cancel_barged_in_response()
    elapsed = _a.get_running_loop().time() - _t0
    assert {"type": "response.cancel"} in _sent_types(fake_ws)
    # Returned while the response is still marked active — i.e. it sent and
    # left, rather than polling for a flag only the pump can clear.
    assert session._response_active is True
    assert elapsed < 0.5, f"guarded cancel settled ({elapsed:.2f}s) — pump freeze"

    # Now the utterance commits and the service creates the caller's reply:
    # a second (stale) barge-in cancel must be a no-op.
    fake_ws.sent.clear()
    await _feed_events(session, fake_ws, [
        {"type": "response.done"},
        {"type": "input_audio_buffer.committed"},
        {"type": "response.created"},
    ])
    await session.cancel_barged_in_response()
    assert {"type": "response.cancel"} not in _sent_types(fake_ws)


async def test_suppress_spurious_reply_only_hits_the_post_commit_reply(fake_ws):
    """The mirror guard: suppress only a response created AFTER the caller's
    speech onset (the reply to noise). Her own in-flight response — the
    exact answer the held flush is protecting — must survive.

    No `_await_idle` stub here either — same regression pin as
    test_cancel_barged_in_response_spares_the_callers_reply: this one is
    awaited from the pump body too, so it must send and return.
    """
    import asyncio as _a

    session = await _started(fake_ws)
    await _feed_events(session, fake_ws, [
        {"type": "response.created"},                       # her answer
        {"type": "input_audio_buffer.speech_started"},      # noise onset
    ])
    await session.suppress_spurious_reply()
    assert {"type": "response.cancel"} not in _sent_types(fake_ws)

    await _feed_events(session, fake_ws, [
        {"type": "response.done"},
        {"type": "input_audio_buffer.committed"},           # noise committed
        {"type": "response.created"},                       # spurious reply
    ])
    fake_ws.sent.clear()
    _t0 = _a.get_running_loop().time()
    await session.suppress_spurious_reply()
    elapsed = _a.get_running_loop().time() - _t0
    assert {"type": "response.cancel"} in _sent_types(fake_ws)
    assert session._response_active is True
    assert elapsed < 0.5, f"guarded suppress settled ({elapsed:.2f}s) — pump freeze"

    # A THIRD case: an out-of-band holding line created after the same onset
    # must survive suppression -- it is never "the reply to a caller turn"
    # (conversation:"none"), and may legally postdate the onset since the
    # endpoint's holding-line timer runs on its own clock, independent of
    # speech-onset timing. Without the OOB guard this would be
    # indistinguishable, by stamp alone, from the spurious reply just
    # cancelled above.
    await _feed_events(session, fake_ws, [{"type": "response.done"}])  # settles the cancelled reply
    await session.speak_out_of_band("one moment")
    await _feed_events(session, fake_ws, [{"type": "response.created"}])
    fake_ws.sent.clear()
    await session.suppress_spurious_reply()
    assert {"type": "response.cancel"} not in _sent_types(fake_ws)


async def test_suppress_spurious_reply_spares_an_in_flight_answer_create(fake_ws):
    """A delegated answer's own `response.create` is stamped with the SAME
    commit counter as the service's auto-reply to that commit — by stamp
    alone the two are indistinguishable. When one is in flight, suppression
    must stand down: cancelling here eats the answer the caller waited
    through the filler for (the exact silent-answer bug), and the verdict
    racing an answer is rare enough that losing a suppression is the cheaper
    of the two mistakes."""
    session = await _started(fake_ws)
    await _feed_events(session, fake_ws, [
        {"type": "response.created"},                       # her answer
        {"type": "input_audio_buffer.speech_started"},      # noise onset
        {"type": "response.done"},
        {"type": "input_audio_buffer.committed"},           # noise committed
        {"type": "response.created"},                       # post-commit reply
    ])
    # An answer create is in flight at this same commit level.
    session._answer_create_seq = session._commit_seq
    fake_ws.sent.clear()
    await session.suppress_spurious_reply()
    assert {"type": "response.cancel"} not in _sent_types(fake_ws)
    assert session._response_active is True


async def test_reply_audio_is_flagged(fake_ws):
    """Audio from a response created after the caller's speech onset carries
    reply=True; audio from the response they talked over stays reply=False."""
    session = await _started(fake_ws)
    events = await _collect_events(session, fake_ws, [
        {"type": "response.created"},
        {"type": "response.audio.delta", "delta": _B64, "item_id": "i1"},
        {"type": "input_audio_buffer.speech_started"},
        {"type": "response.audio.delta", "delta": _B64, "item_id": "i1"},
        {"type": "response.done"},
        {"type": "input_audio_buffer.committed"},
        {"type": "response.created"},
        {"type": "response.audio.delta", "delta": _B64, "item_id": "i2"},
    ])
    audio = [e for e in events if e.type == "audio"]
    assert [a.reply for a in audio] == [False, False, True]


async def test_upstream_close_logs_the_code_and_reason(fake_ws, caplog):
    """A clean upstream close ends events() by returning None — correct, but it
    used to drop the code/reason the service closed WITH on the floor, and that
    is the only evidence of WHY a call ended. Combined with the endpoint's
    equally silent teardown, "did Azure hang up mid-call?" was unanswerable
    from the logs (2026-08-04: cost a full reproduction to rule out)."""
    import logging

    session = await _started(fake_ws)
    with caplog.at_level(
        logging.INFO, logger="voice.live.providers.azure_voice_live"
    ):
        await fake_ws.close()          # queues the CLOSED frame
        events = await _drain(session)
    assert events == []                # the stream ends, quietly, as before
    messages = [r.getMessage() for r in caplog.records]
    assert any("upstream closed" in m for m in messages), messages


async def test_every_cancel_names_the_response_it_means_to_stop(fake_ws):
    """A bare `response.cancel` cancels the DEFAULT conversation's response.
    An out-of-band holding line (`conversation: "none"`) is not in that
    conversation, so the bare frame simply does not reach it — live-probed
    2026-08-05 against gpt-realtime, cancelling the same line 0.6s in:

        response.cancel                 2.34s MORE audio, done status=completed
        response.cancel + response_id   0.03s, done status=CANCELLED
                                        (status_details client_cancelled)

    That one missing field is what made an answer delivered during a holding
    line wait out the whole line: `_await_idle` burned its full 1.5s (measured
    live: 1318ms / 1519ms), the answer's create came back "Conversation
    already has an active response", and the caller heard the filler and then
    a long nothing. Every cancel path in this provider must carry the id.
    """
    session = await _started(fake_ws)
    await _feed_events(session, fake_ws, [
        {"type": "response.created", "response": {"id": "resp_1"}},
    ])
    assert session._active_response_id == "resp_1"

    # 1. the relay/recovery/watchdog path
    fake_ws.sent.clear()
    session._await_idle = _noop_await_idle          # no settle in a unit test
    await session.cancel_active_response()
    assert {"type": "response.cancel", "response_id": "resp_1"} in fake_ws.sent

    # 2. the confirmed-barge-in cut — the one the CALLER feels
    fake_ws.sent.clear()
    await _feed_events(session, fake_ws, [
        {"type": "input_audio_buffer.speech_started"},
    ])
    await session.cancel_barged_in_response()
    assert {"type": "response.cancel", "response_id": "resp_1"} in fake_ws.sent

    # 3. the spurious-reply suppression
    fake_ws.sent.clear()
    await _feed_events(session, fake_ws, [
        {"type": "response.done"},
        {"type": "input_audio_buffer.speech_started"},
        {"type": "input_audio_buffer.committed"},
        {"type": "response.created", "response": {"id": "resp_2"}},
    ])
    await session.suppress_spurious_reply()
    assert {"type": "response.cancel", "response_id": "resp_2"} in fake_ws.sent

    # ...and the id is released when the response ends, so a later cancel can
    # never name a response that is already gone.
    await _feed_events(session, fake_ws, [{"type": "response.done"}])
    assert session._active_response_id is None
    await session.close()


async def _noop_await_idle(*_a, **_kw):
    return True


async def test_response_in_flight_reports_what_the_wire_is_doing(fake_ws):
    """The endpoint's answer watchdog asks this before re-delivering an
    answer (see RealtimeSession.response_in_flight). All three of these mean
    "audio is coming": an acknowledged response, a create of ours the service
    has not acknowledged yet, and a create this provider has promised to
    issue on the next response.done."""
    session = await _started(fake_ws)
    assert session.response_in_flight is False

    await _feed_events(session, fake_ws, [{"type": "response.created"}])
    assert session.response_in_flight is True
    await _feed_events(session, fake_ws, [{"type": "response.done"}])
    assert session.response_in_flight is False

    # our own create, not yet acknowledged
    await session.speak_out_of_band("Hold on.")
    assert session._oob_inflight is True
    assert session.response_in_flight is True
    await _feed_events(session, fake_ws, [
        {"type": "response.created"}, {"type": "response.done"},
    ])
    assert session.response_in_flight is False

    # an answer create the provider owes on the next response.done
    session._retry_answer_create = True
    assert session.response_in_flight is True
    await session.close()


# ---------------------------------------------------------------------------
# _await_idle wake-up latency (event-driven settle after a named cancel)
# ---------------------------------------------------------------------------
#
# A named cancel completes in ~33ms (live-probed 2026-08-05, see
# `_cancel_frame`), but until 2026-08-06 `_await_idle` slept in 50ms poll
# ticks — pure wake-up latency inside every interrupt-mode tool-result
# delivery. The wait must resolve the moment the cancelled response's
# `response.done` clears the flags, and must still burn its FULL ceiling
# when the done never comes (a no-op cancel): that ceiling is what stands
# between us and "Conversation already has an active response".


async def test_await_idle_wakes_on_response_done_not_on_a_poll_tick(fake_ws):
    """`response.done` lands mid-wait -> the waiter resolves on the event
    itself, not on the next poll wake-up.

    Discrimination: the done is injected 55ms in — between the old loop's
    50ms ticks — so the polling implementation cannot see it before its
    100ms wake-up (~45ms of added latency). The <30ms bound fails polling
    and passes the event wake (sub-millisecond) with margin on both sides.
    """
    session = await _started(fake_ws)
    loop = asyncio.get_running_loop()
    session._translate_nosend(
        {"type": "response.created", "response": {"id": "resp_1"}})

    done_at = None

    async def _finish():
        nonlocal done_at
        await asyncio.sleep(0.055)
        done_at = loop.time()
        # The exact mutation site the pump drives: translation is sync and
        # sets the idle event the moment the flags clear.
        session._translate_nosend({"type": "response.done"})

    finisher = asyncio.create_task(_finish())
    t0 = loop.time()
    ok = await session._await_idle(timeout=1.5)
    resolved_at = loop.time()
    await finisher

    assert ok is True
    # Nowhere near the 1.5s ceiling — the fallback did not engage.
    assert resolved_at - t0 < 1.0
    # The discriminating bound: wake latency after the done, immune to any
    # overshoot in the injection sleep itself.
    wake_latency = resolved_at - done_at
    assert wake_latency < 0.03, (
        f"_await_idle took {wake_latency * 1000:.0f}ms to notice "
        "response.done — polling, not event-driven"
    )
    await session.close()


async def test_await_idle_still_burns_the_full_ceiling_when_done_never_comes(fake_ws):
    """A no-op cancel produces no `response.done` — nothing sets the idle
    event, and the wait MUST hold the line for its whole ceiling before
    giving up (returning False), never resolve early into a create the
    service will reject ("Conversation already has an active response")."""
    session = await _started(fake_ws)
    loop = asyncio.get_running_loop()
    session._translate_nosend(
        {"type": "response.created", "response": {"id": "resp_1"}})

    t0 = loop.time()
    ok = await session._await_idle(timeout=0.3)
    elapsed = loop.time() - t0

    assert ok is False
    assert elapsed >= 0.29, (
        f"_await_idle gave up after {elapsed * 1000:.0f}ms with the floor "
        "still busy — the ceiling is the no-op-cancel fallback, keep it"
    )
    await session.close()


async def test_await_idle_returns_immediately_on_an_idle_floor(fake_ws):
    """Nothing active, nothing of ours in flight — no wait at all."""
    session = await _started(fake_ws)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    ok = await session._await_idle(timeout=1.5)
    assert ok is True
    assert loop.time() - t0 < 0.03
    await session.close()


def _turn_ends(events):
    return [e.turn_reply for e in events if e.type == "turn_end"]


async def test_turn_end_reply_flag_greeting_reply_and_continuation(fake_ws):
    """turn_end's `turn_reply` (the client frame's `reply`): False for the
    greeting, True for the FIRST response after the caller's speech, False for
    a continuation of that same turn (the answer after an announce line's tool
    call), True again for the next utterance's response."""
    session = await _started(fake_ws)
    # Greeting: no caller speech yet.
    greeting = await _collect_events(session, fake_ws, [
        {"type": "response.created", "response": {"id": "r0"}},
        {"type": "response.audio.delta", "delta": _B64},
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    assert _turn_ends(greeting) == [False]

    # Caller speaks; the service's reply (the announce line) is a reply.
    announce = await _collect_events(session, fake_ws, [
        {"type": "input_audio_buffer.speech_started"},
        {"type": "input_audio_buffer.committed", "item_id": "u1"},
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.audio.delta", "delta": _B64},
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    assert _turn_ends(announce) == [True]

    # The answer response for the SAME utterance (created after the tool
    # result) continues that turn: not a reply.
    answer = await _collect_events(session, fake_ws, [
        {"type": "response.created", "response": {"id": "r2"}},
        {"type": "response.audio.delta", "delta": _B64},
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    assert _turn_ends(answer) == [False]

    # Completed-only STT: the caller's transcript lands AFTER the reply's
    # turn_end — the flag is what lets the client attach it.
    late = await _collect_events(session, fake_ws, [
        {"type": "input_audio_buffer.speech_started"},
        {"type": "input_audio_buffer.committed", "item_id": "u2"},
        {"type": "response.created", "response": {"id": "r3"}},
        {"type": "response.audio.delta", "delta": _B64},
        {"type": "response.done", "response": {"status": "completed"}},
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": "u2", "transcript": "late words"},
    ])
    kinds = [e.type for e in late]
    assert kinds.index("turn_end") < max(
        i for i, e in enumerate(late) if e.type == "transcript" and e.role == "user"
    )
    assert _turn_ends(late) == [True]
    await session.close()


async def test_turn_end_reply_flag_false_for_out_of_band_line(fake_ws):
    """An out-of-band holding line is never a reply, even when it is the
    first response after the caller's speech."""
    session = await _started(fake_ws)
    await _feed_events(session, fake_ws, [
        {"type": "input_audio_buffer.speech_started"},
        {"type": "input_audio_buffer.committed", "item_id": "u1"},
    ])
    session._pending_oob_create = True
    session._oob_inflight_text = "One moment."
    session._translate_nosend({"type": "response.created", "response": {"id": "o1"}})
    end = session._translate_nosend({"type": "response.done"})
    assert [(e.type, e.turn_reply) for e in end] == [("turn_end", False)]
    # The model's own reply afterwards is still the first real reply.
    session._translate_nosend({"type": "response.created", "response": {"id": "r1"}})
    end = session._translate_nosend({"type": "response.done"})
    assert [(e.type, e.turn_reply) for e in end] == [("turn_end", True)]
    await session.close()


async def test_suppressed_spurious_reply_is_not_a_reply(fake_ws):
    """A noise commit during the question's wait auto-creates a response the
    endpoint cancels as spurious. That cancelled response answers nothing, so
    its turn_end must be reply=False — and the delegated answer that follows
    for the ORIGINAL question stays a continuation (False) too, merging into
    the question's turn on the client."""
    session = await _started(fake_ws)
    first = await _collect_events(session, fake_ws, [
        {"type": "input_audio_buffer.speech_started"},
        {"type": "input_audio_buffer.committed", "item_id": "u1"},
        {"type": "response.created", "response": {"id": "r1"}},   # announce
        {"type": "response.done", "response": {"status": "completed"}},
        {"type": "input_audio_buffer.speech_started"},            # "mhm"
        {"type": "input_audio_buffer.committed", "item_id": "u2"},
        {"type": "response.created", "response": {"id": "r2"}},   # spurious
    ])
    assert _turn_ends(first) == [True]
    await session.suppress_spurious_reply()
    assert {"type": "response.cancel"} in _sent_types(fake_ws)
    rest = await _collect_events(session, fake_ws, [
        {"type": "response.done", "response": {"status": "cancelled"}},
        {"type": "response.created", "response": {"id": "r3"}},   # the answer
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    assert _turn_ends(rest) == [False, False]
    await session.close()


async def test_reprompt_and_barge_in_reply_flags(fake_ws):
    """A stall `reprompt()` continues the turn the first reply already
    answered (False). A barge-in onset during a response leaves THAT
    response's flag as stamped at creation, and the next utterance's reply is
    True."""
    session = await _started(fake_ws)
    first = await _collect_events(session, fake_ws, [
        {"type": "input_audio_buffer.speech_started"},
        {"type": "input_audio_buffer.committed", "item_id": "u1"},
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    assert _turn_ends(first) == [True]
    assert await session.reprompt() is True
    stall = await _collect_events(session, fake_ws, [
        {"type": "response.created", "response": {"id": "r2"}},
        # The caller talks over the reprompted response...
        {"type": "input_audio_buffer.speech_started"},
        {"type": "response.done", "response": {"status": "cancelled"}},
        {"type": "input_audio_buffer.committed", "item_id": "u2"},
        # ...and the service answers their new utterance.
        {"type": "response.created", "response": {"id": "r3"}},
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    assert _turn_ends(stall) == [False, True]
    await session.close()


async def test_transcription_failed_maps_to_user_transcript_failed(fake_ws):
    """A failed input transcription is surfaced (so a client's late-transcript
    window can close) as its own event — never as an empty user final, which
    the endpoint would read as a noise verdict."""
    session = await _started(fake_ws)
    events = await _collect_events(session, fake_ws, [
        {"type": "conversation.item.input_audio_transcription.failed",
         "item_id": "u1", "error": {"message": "boom"}},
    ])
    assert [e.type for e in events] == ["user_transcript_failed"]
    await session.close()


# ---- V9 (retest 2026-09-28): a native row with the business seed's voice_config
_NATIVE = {"endpoint": "https://x", "voice_type": "native"}


@pytest.mark.parametrize("mapping,voice_id,expected", [
    # The retest's first failure: neural names left in voice_by_language.
    ({"ar": "ar-SA-HamedNeural", "en": "en-US-AvaMultilingualNeural"}, "Kore", "marin"),
    # ...and its second: no mapping, the config default "Kore" (a Gemini voice).
    ({}, "Kore", "marin"),
    # A native voice_id is the fallback when the mapping is neural.
    ({"ar": "ar-SA-HamedNeural"}, "cedar", "cedar"),
    # A native mapping is used as is.
    ({"ar": "Marin"}, "Kore", "marin"),
])
def test_native_row_never_sends_a_non_native_voice(mapping, voice_id, expected):
    cfg = _cfg(language="ar-SA", voice_by_language=mapping, voice_id=voice_id,
               extra_config=dict(_NATIVE))
    assert _voice_payload(cfg) == expected


def test_native_fallback_voice_is_row_configurable():
    cfg = _cfg(language="en-US", voice_id="Kore",
               extra_config={**_NATIVE, "native_voice": "ash"})
    assert _voice_payload(cfg) == "ash"


def test_cascaded_row_keeps_neural_names():
    cfg = _cfg(language="ar-SA", voice_by_language={"ar": "ar-SA-HamedNeural"})
    assert _voice_payload(cfg)["name"] == "ar-SA-HamedNeural"


async def test_speech_stopped_is_reported_for_the_sustained_cut(fake_ws):
    """The endpoint's sustained-speech barge-in cut (barge_in_sustain_sec)
    stands down when the caller stops inside the window; without this event it
    could only stand down at the commit, ~650ms later."""
    session = await _started(fake_ws)
    fake_ws.feed({"type": "input_audio_buffer.speech_started"})
    fake_ws.feed({"type": "input_audio_buffer.speech_stopped"})
    events = await _drain(session, n=4)
    assert [e.type for e in events] == ["speech_started_idle", "speech_stopped"]


async def test_first_assistant_caption_carries_the_reply_verdict(fake_ws):
    """The client learns reply/continuation at the response's FIRST caption,
    not only at turn_end — decided at response.created, so no race."""
    session = await _started(fake_ws)
    fake_ws.feed({"type": "input_audio_buffer.speech_started"})
    fake_ws.feed({"type": "input_audio_buffer.committed"})
    fake_ws.feed({"type": "response.created", "response": {"id": "r1"}})
    fake_ws.feed({"type": "response.audio_transcript.delta", "delta": "Sure"})
    fake_ws.feed({"type": "response.audio_transcript.delta", "delta": " thing"})
    events = await _drain(session, n=8)
    captions = [e for e in events if e.type == "transcript" and e.role == "assistant"]
    assert [c.turn_reply for c in captions] == [True, None], [(c.text, c.turn_reply) for c in captions]


# ---- caller_languages (live 2026-10-02) -------------------------------------
def test_caller_languages_ask_azure_speech_to_auto_detect():
    """An English-UI merchant speaking Arabic was transcribed as English
    («Ili Bakkej minhail…», probed live) because the UI locale locked the
    recognizer to en-US. Several caller languages → one comma list."""
    cfg = _cfg(language="en-US", caller_languages=["ar-JO", "en-US", "ar-JO"])
    s = _session_payload(cfg, "p")["session"]
    assert s["input_audio_transcription"]["language"] == "ar-JO,en-US"


def test_one_caller_language_keeps_the_session_tag():
    cfg = _cfg(language="ar-JO", caller_languages=["en-US"])
    assert _session_payload(cfg, "p")["session"]["input_audio_transcription"]["language"] == "ar-JO"


def test_caller_languages_are_ignored_by_a_whisper_family_recognizer():
    cfg = _cfg(language="ar-JO", caller_languages=["ar-JO", "en-US"],
               extra_config={"endpoint": "https://x.services.ai.azure.com",
                             "transcription_model": "gpt-4o-transcribe"})
    assert _session_payload(cfg, "p")["session"]["input_audio_transcription"]["language"] == "ar"
