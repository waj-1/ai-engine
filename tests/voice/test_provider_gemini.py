import base64
import pytest
from voice.providers.base import VoiceProviderError
from voice.providers.gemini import GeminiVoiceProvider


class _FakePart:
    def __init__(self, data, mime="audio/L16;rate=24000"):
        self.inline_data = type("X", (), {"data": data, "mime_type": mime})()


class _FakeResp:
    def __init__(self, *, text=None, pcm=None):
        self.text = text
        if pcm is not None:
            cand = type("C", (), {})()
            cand.content = type("Ct", (), {"parts": [_FakePart(pcm)]})()
            self.candidates = [cand]
        else:
            self.candidates = []


class _FakeModels:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self._resp


class _FakeClient:
    def __init__(self, resp):
        self.models = _FakeModels(resp)


@pytest.mark.asyncio
async def test_transcribe_returns_text_and_model(monkeypatch):
    p = GeminiVoiceProvider(stt_model="gemini-x", tts_model="tts-x", api_key="k")
    monkeypatch.setattr(p, "_client", _FakeClient(_FakeResp(text="  hello  ")))
    out = await p.transcribe(b"audiobytes", "audio/wav")
    assert out.text == "hello"
    assert out.model == "gemini-x"


@pytest.mark.asyncio
async def test_synthesize_stream_yields_wav_per_chunk(monkeypatch):
    p = GeminiVoiceProvider(stt_model="gemini-x", tts_model="tts-x", api_key="k")
    monkeypatch.setattr(p, "_client", _FakeClient(_FakeResp(pcm=b"\x01\x02" * 50)))

    async def _chunks():
        yield "First sentence."
        yield "Second sentence."

    seqs, mimes = [], []
    async for ch in p.synthesize_stream(_chunks(), voice="Kore", language=None, sample_rate=24000):
        seqs.append(ch.seq)
        mimes.append(ch.mime)
        assert ch.data[:4] == b"RIFF"
    assert seqs == [0, 1]
    assert mimes == ["audio/wav", "audio/wav"]


@pytest.mark.asyncio
async def test_synthesize_raises_when_no_pcm(monkeypatch):
    p = GeminiVoiceProvider(stt_model="gemini-x", tts_model="tts-x", api_key="k")
    monkeypatch.setattr(p, "_client", _FakeClient(_FakeResp(pcm=None)))

    async def _chunks():
        yield "hello"

    with pytest.raises(VoiceProviderError):
        async for _ in p.synthesize_stream(_chunks(), voice="Kore", language=None, sample_rate=24000):
            pass
