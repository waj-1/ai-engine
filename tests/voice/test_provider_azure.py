"""Azure Speech turn-based provider (voice/providers/azure.py).

Every HTTP call is stubbed: these assert the REQUEST we build and how we read
the response, which is the whole integration surface.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from voice.factory import build_stt_provider, build_tts_provider
from voice.providers.azure import AzureSpeechVoiceProvider, _ssml
from voice.providers.base import VoiceProviderError


class _FakeResponse:
    def __init__(self, *, status_code=200, json_body=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_body
        self.content = content
        self.text = text or ""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeClient:
    """Captures the single POST each call makes."""

    def __init__(self, response, sink):
        self._response = response
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self._sink.append({"url": url, **kwargs})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
def azure_key(monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "test-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "uaenorth")


@pytest.fixture(autouse=True)
def _reset_shared_client(monkeypatch):
    """`voice.providers.azure` caches ONE AsyncClient per process (so streaming
    TTS doesn't pay a TLS handshake per sentence). That cache is a module
    global, so without this a client built in one test — real or fake — leaks
    into the next one."""
    import voice.providers.azure as mod

    monkeypatch.setattr(mod, "_client", None)


def _patch_http(monkeypatch, response):
    """Replace the shared-client accessor, not httpx itself: patching
    `httpx.AsyncClient` would let the fake be cached in the module global and
    then fail the `is_closed` check on the next call."""
    calls: list = []
    fake = _FakeClient(response, calls)
    monkeypatch.setattr("voice.providers.azure._http", lambda: fake)
    return calls


async def _chunks(*texts):
    for t in texts:
        yield t


# --- factory wiring ---------------------------------------------------


def _cfg(**over) -> SimpleNamespace:
    # See tests/voice/test_factory.py::_cfg — `build_*_provider` is duck-typed
    # since voice.config.VoiceConfig was retired with the /voice/* routes.
    base = dict(
        enabled=True, stt_provider="azure", stt_model="", tts_provider="azure",
        tts_model="", voice_id="ar-SA-HamedNeural", language="ar", sample_rate=24000,
        speaking_rate=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_factory_builds_azure_for_both_directions():
    assert isinstance(build_stt_provider(_cfg()), AzureSpeechVoiceProvider)
    assert isinstance(build_tts_provider(_cfg()), AzureSpeechVoiceProvider)


def test_factory_accepts_the_azure_speech_alias():
    cfg = _cfg(stt_provider="azure_speech", tts_provider="AZURE_SPEECH")
    assert isinstance(build_stt_provider(cfg), AzureSpeechVoiceProvider)
    assert isinstance(build_tts_provider(cfg), AzureSpeechVoiceProvider)


def test_azure_is_the_default_provider():
    """The engine runs on Azure; its voice edge should too.

    Only the STT half is an AGENT_CONFIG field now: `voice_tts_provider` was
    deleted 2026-08-18 as a dead knob whose ONLY reader was the line that used
    to sit here. Nothing calls `build_tts_provider`, and the simulated caller
    that does synthesize speech builds `AzureSpeechVoiceProvider` directly — so
    the assertion was pinning a value no runtime path consumed. Asserting the
    field is gone keeps a future author from wiring the env var back up in the
    belief it still selects a synthesizer.
    """
    from core.config import AgentConfig

    fresh = AgentConfig()
    assert fresh.voice_stt_provider == "azure"
    assert not hasattr(fresh, "voice_tts_provider")


# --- STT --------------------------------------------------------------


async def test_transcribe_posts_wav_with_a_regional_locale(monkeypatch, azure_key):
    calls = _patch_http(
        monkeypatch,
        _FakeResponse(json_body={"RecognitionStatus": "Success", "DisplayText": "مرحبا"}),
    )
    result = await AzureSpeechVoiceProvider().transcribe(
        b"RIFFwav", "audio/wav", language="ar"
    )

    assert result.text == "مرحبا"
    call = calls[0]
    assert "uaenorth.stt.speech.microsoft.com" in call["url"]
    # A bare "ar" is rejected by this endpoint -- it must be widened.
    assert call["params"]["language"] == "ar-SA"
    assert call["headers"]["Ocp-Apim-Subscription-Key"] == "test-key"
    assert call["headers"]["Content-Type"].startswith("audio/wav")
    assert call["content"] == b"RIFFwav"


async def test_transcribe_defaults_to_english_when_no_language_given(monkeypatch, azure_key):
    calls = _patch_http(
        monkeypatch, _FakeResponse(json_body={"RecognitionStatus": "Success", "DisplayText": "hi"})
    )
    await AzureSpeechVoiceProvider().transcribe(b"RIFF", "audio/wav")
    assert calls[0]["params"]["language"] == "en-US"


async def test_shared_client_is_reused_across_calls(monkeypatch, azure_key):
    """A provider is built per REQUEST and synthesize_stream calls once per
    SENTENCE, so a per-call client meant a TLS handshake per sentence on the
    very path that exists for low latency."""
    import voice.providers.azure as mod

    created = []
    real = mod.httpx.AsyncClient

    def _counting(**kw):
        created.append(1)
        return real(**kw)

    monkeypatch.setattr(mod, "_client", None)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _counting)
    c1, c2 = mod._http(), mod._http()
    assert c1 is c2
    assert len(created) == 1


async def test_transcribe_rejects_webm_by_name(azure_key):
    """A browser MediaRecorder default that this REST surface cannot accept."""
    with pytest.raises(VoiceProviderError, match="WebM"):
        await AzureSpeechVoiceProvider().transcribe(b"x", "audio/webm;codecs=opus")


async def test_transcribe_treats_silence_as_an_empty_transcript(monkeypatch, azure_key):
    _patch_http(monkeypatch, _FakeResponse(json_body={"RecognitionStatus": "NoMatch"}))
    result = await AzureSpeechVoiceProvider().transcribe(b"RIFF", "audio/wav")
    assert result.text == ""


async def test_transcribe_hides_provider_body_from_the_caller(monkeypatch, azure_key):
    _patch_http(
        monkeypatch,
        _FakeResponse(status_code=401, text="secret-ish provider detail"),
    )
    with pytest.raises(VoiceProviderError) as exc:
        await AzureSpeechVoiceProvider().transcribe(b"RIFF", "audio/wav")
    assert "secret-ish" not in str(exc.value)


async def test_missing_key_names_the_env_var(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    with pytest.raises(VoiceProviderError, match="AZURE_SPEECH_KEY"):
        await AzureSpeechVoiceProvider().transcribe(b"RIFF", "audio/wav")


# --- TTS --------------------------------------------------------------


async def test_synthesize_requests_the_matching_pcm_format(monkeypatch, azure_key):
    calls = _patch_http(monkeypatch, _FakeResponse(content=b"\x01\x02" * 16))
    provider = AzureSpeechVoiceProvider()

    out = [
        c
        async for c in provider.synthesize_stream(
            _chunks("hello"), voice="ar-SA-HamedNeural", language="ar", sample_rate=24000
        )
    ]

    assert len(out) == 1
    assert out[0].sample_rate == 24000
    assert out[0].data.startswith(b"RIFF")
    assert calls[0]["headers"]["X-Microsoft-OutputFormat"] == "raw-24khz-16bit-mono-pcm"


async def test_synthesize_refuses_an_unsupported_sample_rate(azure_key):
    """Substituting a nearby rate is what made every response play at the
    wrong speed on the Gemini path -- refuse instead."""
    provider = AzureSpeechVoiceProvider()
    with pytest.raises(VoiceProviderError, match="sample_rate"):
        async for _ in provider.synthesize_stream(
            _chunks("hi"), voice="v", language="en", sample_rate=12345
        ):
            pass


async def test_synthesize_skips_blank_chunks(monkeypatch, azure_key):
    calls = _patch_http(monkeypatch, _FakeResponse(content=b"\x00\x00"))
    provider = AzureSpeechVoiceProvider()
    out = [
        c
        async for c in provider.synthesize_stream(
            _chunks("  ", "\n", "real"), voice="v", language="en", sample_rate=16000
        )
    ]
    assert len(out) == 1
    assert len(calls) == 1


async def test_synthesize_raises_when_the_service_returns_no_audio(monkeypatch, azure_key):
    _patch_http(monkeypatch, _FakeResponse(content=b""))
    provider = AzureSpeechVoiceProvider()
    with pytest.raises(VoiceProviderError, match="no audio"):
        async for _ in provider.synthesize_stream(
            _chunks("hi"), voice="v", language="en", sample_rate=16000
        ):
            pass


# --- SSML escaping ----------------------------------------------------


def test_ssml_escapes_agent_text():
    """Agent answers contain & and < (P&L, thresholds). Unescaped, the service
    400s and the caller hears nothing."""
    doc = _ssml("Profit & loss < 5%", voice="v", language="en")
    assert "Profit &amp; loss &lt; 5%" in doc
    assert "<voice name=\"v\">" in doc


def test_ssml_quotes_an_operator_voice_name_that_contains_a_quote():
    """voice_id is operator-editable config. escape() leaves `"` alone, so an
    attribute built with it would let the value close the attribute and inject
    markup -- quoteattr is what makes this safe."""
    import xml.etree.ElementTree as ET

    doc = _ssml("hi", voice='v" xml:lang="zz', language="en")
    # The real assertion: it still parses, and the voice name survives whole
    # as a single attribute value rather than becoming extra markup.
    root = ET.fromstring(doc)
    voice_el = root.find("{http://www.w3.org/2001/10/synthesis}voice")
    assert voice_el.get("name") == 'v" xml:lang="zz'


def test_ssml_uses_a_regional_locale():
    assert 'xml:lang="ar-SA"' in _ssml("مرحبا", voice="ar-SA-HamedNeural", language="ar")


def test_ssml_wraps_prosody_when_speaking_rate_is_set():
    doc = _ssml("hello", voice="v", language="en", speaking_rate="+10%")
    assert '<prosody rate="+10%">hello</prosody>' in doc
    assert "<voice name=\"v\"><prosody rate=\"+10%\">hello</prosody></voice>" in doc


def test_ssml_has_no_prosody_tag_when_speaking_rate_is_unset():
    """Byte-identical to the pre-speaking-rate SSML -- a provider that never
    receives a <prosody> element can't reject it."""
    doc = _ssml("hello", voice="v", language="en")
    assert "<prosody" not in doc
    assert doc == (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="en-US"><voice name="v">hello</voice></speak>'
    )


def test_ssml_prosody_rate_attribute_is_quoted_safely():
    """speaking_rate is operator-editable config, same posture as voice_id --
    quoteattr must protect the attribute the same way."""
    import xml.etree.ElementTree as ET

    doc = _ssml("hi", voice="v", language="en", speaking_rate='10% xml:lang="zz"')
    root = ET.fromstring(doc)
    voice_el = root.find("{http://www.w3.org/2001/10/synthesis}voice")
    prosody_el = voice_el.find("{http://www.w3.org/2001/10/synthesis}prosody")
    assert prosody_el.get("rate") == '10% xml:lang="zz"'


async def test_synthesize_sends_prosody_when_provider_has_a_speaking_rate(monkeypatch, azure_key):
    """End-to-end: the constructor's speaking_rate reaches the SSML body sent
    over the wire, not just the pure _ssml() helper."""
    calls = _patch_http(monkeypatch, _FakeResponse(content=b"\x01\x02" * 16))
    provider = AzureSpeechVoiceProvider(speaking_rate="-20%")

    out = [
        c
        async for c in provider.synthesize_stream(
            _chunks("hello"), voice="ar-SA-HamedNeural", language="ar", sample_rate=24000
        )
    ]

    assert len(out) == 1
    body = calls[0]["content"].decode("utf-8")
    assert '<prosody rate="-20%">hello</prosody>' in body


async def test_synthesize_omits_prosody_when_provider_has_no_speaking_rate(monkeypatch, azure_key):
    calls = _patch_http(monkeypatch, _FakeResponse(content=b"\x01\x02" * 16))
    provider = AzureSpeechVoiceProvider()

    async for _ in provider.synthesize_stream(
        _chunks("hello"), voice="ar-SA-HamedNeural", language="ar", sample_rate=24000
    ):
        pass

    body = calls[0]["content"].decode("utf-8")
    assert "<prosody" not in body


async def test_transcribe_reports_the_recognizer_not_the_inert_stt_model(monkeypatch, azure_key):
    """Azure picks its recognizer by LOCALE and has no model string, so echoing
    the configured `stt_model` (a Gemini id, inert here) back to the caller
    described something that never ran."""
    _patch_http(
        monkeypatch,
        _FakeResponse(json_body={"RecognitionStatus": "Success", "DisplayText": "hi"}),
    )
    provider = AzureSpeechVoiceProvider(stt_model="gemini-3.5-flash")
    result = await provider.transcribe(b"RIFF", "audio/wav", language="en")
    assert result.model == "azure-speech"
    assert "gemini" not in result.model


async def test_unsupported_type_raises_the_typed_error(azure_key):
    """Typed so the endpoint can answer 415 rather than a blanket 502."""
    from voice.providers.base import UnsupportedAudioError

    with pytest.raises(UnsupportedAudioError):
        await AzureSpeechVoiceProvider().transcribe(b"x", "audio/webm;codecs=opus")
