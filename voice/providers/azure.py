"""Azure AI Speech STT (audio->text) + TTS (text->PCM, wrapped as WAV).

The turn-based sibling of `voice/live/providers/azure_voice_live.py`. Both
speak to Azure Speech in `uaenorth`; this one over the plain REST surface
(short-audio recognition, and the `cognitiveservices/v1` synthesis endpoint)
rather than the Voice Live websocket, because a turn-based request is exactly
one utterance in and one utterance out.

Spoken over `httpx` rather than the `azure-cognitiveservices-speech` SDK: the
two REST calls below are the whole integration, `httpx` is already the
engine's outbound HTTP client everywhere else, and the SDK would be a new
(large, native-binary) dependency for no gain. Same no-new-deps reasoning as
`azure_voice_live.py` choosing raw `aiohttp` over the Azure SDK.

Credentials come from the environment, never from the config DB: the key is a
secret, and the region is a deployment property. Everything an operator
legitimately tunes per tenant — the voice, the language, the models, the
sample rate — stays in `voice_config`, resolved by `voice/config.py`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator, Dict, Optional
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

import httpx

from voice.audio import pcm_to_wav
from voice.providers.base import (
    AudioChunk,
    TranscriptResult,
    UnsupportedAudioError,
    VoiceCaps,
    VoiceProvider,
    VoiceProviderError,
)

logger = logging.getLogger(__name__)

# Matches the Voice Live deployment (see docs/superpowers/specs/
# 2026-07-28-azure-voice-live-provider-design.md) so turn-based and duplex
# voice bill to the same Gulf region and share its data-residency posture.
_DEFAULT_REGION = "uaenorth"
# What we report as the `model` of a transcript. Azure Speech has no model
# string -- the recognizer is chosen by LOCALE -- so echoing the configured
# `stt_model` back (a Gemini model id, inert on this provider) told the caller
# something untrue about what transcribed their audio. This is the same
# recognizer name the duplex provider names in its session config.
_RECOGNIZER = "azure-speech"
_KEY_ENV = "AZURE_SPEECH_KEY"
_REGION_ENV = "AZURE_SPEECH_REGION"

# What the raw-PCM synthesis formats are actually called. Azure only offers
# this fixed set, so a voice_config.sample_rate outside it cannot be honoured
# -- and silently substituting a neighbouring rate is exactly the bug the
# turn-based edge already had (a header that claimed a rate the PCM wasn't in,
# so everything played at the wrong speed). Refuse instead.
_PCM_FORMATS: Dict[int, str] = {
    8000: "raw-8khz-16bit-mono-pcm",
    16000: "raw-16khz-16bit-mono-pcm",
    22050: "raw-22050hz-16bit-mono-pcm",
    24000: "raw-24khz-16bit-mono-pcm",
    44100: "raw-44100hz-16bit-mono-pcm",
    48000: "raw-48khz-16bit-mono-pcm",
}

# Content types the short-audio REST endpoint accepts. WebM/Opus is NOT in
# this list: it is supported by the Speech SDK but not by this REST surface,
# and sending it yields an opaque 400. Browsers default to WebM from
# MediaRecorder, so the failure has to name itself -- see transcribe().
_STT_CONTENT_TYPES: Dict[str, str] = {
    "audio/wav": "audio/wav; codecs=audio/pcm; samplerate=16000",
    "audio/x-wav": "audio/wav; codecs=audio/pcm; samplerate=16000",
    "audio/wave": "audio/wav; codecs=audio/pcm; samplerate=16000",
    "audio/ogg": "audio/ogg; codecs=opus",
    "audio/opus": "audio/ogg; codecs=opus",
}

_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# One client for the process, not one per call. The factory builds a provider
# per REQUEST and synthesize_stream makes a call per SENTENCE, so a per-call
# client meant a fresh TLS handshake for every sentence -- on the exact path
# that exists to get the first audio out quickly. Created lazily so importing
# this module never opens sockets, and deliberately never closed: it lives as
# long as the process, like the SDK clients the other providers hold.
_client: Optional[httpx.AsyncClient] = None
# The loop `_client` was built on; see `_http()`.
_client_loop: Optional[asyncio.AbstractEventLoop] = None


def _http() -> httpx.AsyncClient:
    """Process-wide pooled client, keyed to the event loop that built it.

    An `httpx.AsyncClient` binds its connection pool to the running loop, and
    `is_closed` reports only whether the CLIENT was closed — it knows nothing
    about its loop being torn down. So a cached client handed to a second loop
    raises `RuntimeError: Event loop is closed` from inside anyio, which this
    module's callers wrap as `VoiceProviderError("speech synthesis provider
    error")` — an Azure-shaped message for a bug that is entirely ours.

    That is exactly what happened: `admin/voice_eval_runner.py` calls
    `asyncio.run()` PER SCENARIO, so every voice-eval scenario after the first
    failed to synthesize the caller's speech, recorded `turns=0 / ws_closed`,
    and was scored 0 for never running. It silently corrupted every arm of the
    2026-08-18 duplex bake-off.

    Rebuilding only when the loop CHANGES keeps connection reuse where it
    matters — the duplex voice path runs on one long-lived loop, and a fresh
    TLS handshake per utterance would land on the exact path where latency is
    the failure mode. The superseded client is dropped rather than closed:
    its loop is already gone, so `aclose()` would raise; its transport is dead
    and GC reclaims it.
    """
    global _client, _client_loop
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        # Called outside a loop. Nothing to bind to, so don't invalidate a
        # perfectly good cached client on account of it.
        loop = _client_loop
    if _client is None or _client.is_closed or loop is not _client_loop:
        _client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        _client_loop = loop
    return _client


def _region() -> str:
    return (os.getenv(_REGION_ENV) or _DEFAULT_REGION).strip()


def _key() -> str:
    key = os.getenv(_KEY_ENV)
    if not key:
        raise VoiceProviderError(
            f"azure voice: {_KEY_ENV} is not set (region {_region()!r}). Set it, "
            f"or point voice_config.stt_provider/tts_provider at another "
            f"registered provider."
        )
    return key


def _base_mime(mime: str) -> str:
    """`audio/wav; codecs=1` -> `audio/wav`, lowercased."""
    return (mime or "").split(";")[0].strip().lower()


class AzureSpeechVoiceProvider(VoiceProvider):
    name = "azure"
    caps = VoiceCaps(supports_stt=True, supports_tts=True, supports_streaming_stt=False)

    def __init__(
        self,
        *,
        stt_model: str = "",
        tts_model: str = "",
        region: Optional[str] = None,
        speaking_rate: Optional[str] = None,
    ) -> None:
        # stt_model / tts_model are accepted for interface parity with the
        # Gemini provider and for logging. Azure Speech selects its recognizer
        # by locale and its synthesizer by VOICE name (voice_config.voice_id,
        # e.g. "ar-SA-HamedNeural"), not by a model string -- so unlike Gemini
        # there is no model id to pass. Kept visible rather than dropped so a
        # tenant that set them can be told they are inert here.
        self._stt_model = stt_model
        self._tts_model = tts_model
        self._region = (region or _region()).strip()
        # voice_config.speaking_rate -- an Azure prosody rate string
        # ("+10%" / "-20%" / "0.9"). None means "send no <prosody> wrapper at
        # all", not "send rate=default" -- see _ssml.
        self._speaking_rate = speaking_rate

    # ---- STT ---------------------------------------------------------

    async def transcribe(
        self, audio: bytes, mime: str, *, language: Optional[str] = None
    ) -> TranscriptResult:
        if not audio:
            raise VoiceProviderError("no audio to transcribe")
        base = _base_mime(mime)
        content_type = _STT_CONTENT_TYPES.get(base)
        if content_type is None:
            raise UnsupportedAudioError(
                f"azure voice: unsupported audio type {base or mime!r}. The "
                f"short-audio REST API accepts "
                f"{sorted(set(_STT_CONTENT_TYPES))}; WebM/Opus (a browser "
                f"MediaRecorder default) is not among them -- transcode to WAV "
                f"or Ogg/Opus before calling."
            )
        # Azure defaults to en-US when no locale is given and does NOT
        # auto-detect on this endpoint, so an unset language silently
        # transcribes Arabic speech as English word-salad. Default to the
        # deployment's primary locale instead of the service's.
        locale = _locale(language)
        url = (
            f"https://{self._region}.stt.speech.microsoft.com"
            f"/speech/recognition/conversation/cognitiveservices/v1"
        )
        try:
            resp = await _http().post(
                url,
                params={"language": locale},
                headers={
                    "Ocp-Apim-Subscription-Key": _key(),
                    "Content-Type": content_type,
                    "Accept": "application/json",
                },
                content=audio,
            )
        except VoiceProviderError:
            raise
        except Exception as exc:
            logger.error(
                "azure voice: transcribe transport failed: %s: %s",
                type(exc).__name__, exc, exc_info=True,
            )
            raise VoiceProviderError("transcription provider error") from exc

        if resp.status_code >= 400:
            # Body may carry the real reason (bad codec, bad locale, throttling),
            # but it is provider text -- log it, don't hand it to the caller.
            logger.error(
                "azure voice: transcribe HTTP %s: %s", resp.status_code, resp.text[:500]
            )
            raise VoiceProviderError("transcription provider error")

        try:
            payload = resp.json()
        except ValueError as exc:
            logger.error("azure voice: transcribe returned non-JSON")
            raise VoiceProviderError("transcription provider error") from exc

        status = payload.get("RecognitionStatus")
        if status == "NoMatch" or status == "InitialSilenceTimeout":
            # Silence is not a failure: the caller said nothing intelligible.
            # An empty transcript is the honest answer, and the endpoint
            # already handles it.
            return TranscriptResult(text="", model=_RECOGNIZER, language=locale)
        if status != "Success":
            logger.error("azure voice: transcribe status %r", status)
            raise VoiceProviderError("transcription provider error")

        return TranscriptResult(
            text=(payload.get("DisplayText") or "").strip(),
            model=_RECOGNIZER,
            language=locale,
        )

    # ---- TTS ---------------------------------------------------------

    async def synthesize_stream(
        self,
        text_chunks: AsyncIterator[str],
        *,
        voice: str,
        language: Optional[str],
        sample_rate: int,
    ) -> AsyncIterator[AudioChunk]:
        fmt = _PCM_FORMATS.get(int(sample_rate))
        if fmt is None:
            raise VoiceProviderError(
                f"azure voice: sample_rate {sample_rate} is not one of Azure's "
                f"raw-PCM formats {sorted(_PCM_FORMATS)}. Set voice_config."
                f"sample_rate to a supported rate -- substituting a nearby one "
                f"would play every response at the wrong speed."
            )
        seq = 0
        async for text in text_chunks:
            if not text.strip():
                continue
            pcm = await self._synthesize_pcm(
                text, voice=voice, language=language, fmt=fmt
            )
            yield AudioChunk(
                data=pcm_to_wav(pcm, sample_rate=sample_rate),
                mime="audio/wav",
                seq=seq,
                sample_rate=sample_rate,
            )
            seq += 1

    async def synthesize_pcm(
        self, text: str, *, voice: str, language: Optional[str], sample_rate: int
    ) -> bytes:
        """One utterance -> raw PCM at `sample_rate` (no WAV header).

        Public single-shot flavour of `synthesize_stream` for callers that
        need bare PCM to FEED a realtime session rather than WAV to play —
        today the voice-scenario runner (admin/voice_scenario_runner.py),
        which streams synthesized caller speech into WS /voice/live.
        """
        fmt = _PCM_FORMATS.get(int(sample_rate))
        if fmt is None:
            raise VoiceProviderError(
                f"azure voice: sample_rate {sample_rate} is not one of Azure's "
                f"raw-PCM formats {sorted(_PCM_FORMATS)}"
            )
        return await self._synthesize_pcm(text, voice=voice, language=language, fmt=fmt)

    async def _synthesize_pcm(
        self, text: str, *, voice: str, language: Optional[str], fmt: str
    ) -> bytes:
        url = f"https://{self._region}.tts.speech.microsoft.com/cognitiveservices/v1"
        try:
            resp = await _http().post(
                url,
                headers={
                    "Ocp-Apim-Subscription-Key": _key(),
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": fmt,
                    "User-Agent": "waj-ai-engine",
                },
                content=_ssml(
                    text, voice=voice, language=language,
                    speaking_rate=self._speaking_rate,
                ).encode("utf-8"),
            )
        except VoiceProviderError:
            raise
        except Exception as exc:
            logger.error(
                "azure voice: TTS transport failed: %s: %s",
                type(exc).__name__, exc, exc_info=True,
            )
            raise VoiceProviderError("speech synthesis provider error") from exc

        if resp.status_code >= 400:
            logger.error(
                "azure voice: TTS HTTP %s: %s", resp.status_code, resp.text[:500]
            )
            raise VoiceProviderError("speech synthesis provider error")
        pcm = resp.content
        if not pcm:
            raise VoiceProviderError("TTS provider returned no audio")
        return pcm


def _locale(language: Optional[str]) -> str:
    """A full BCP-47 locale for the recognizer.

    Azure's short-audio endpoint wants a REGIONAL tag ("ar-SA"), not a bare
    subtag ("ar") -- a bare one is rejected. Mirrors the duplex provider's
    locale handling, and the same Arabic default (ar-SA): the callers it was
    tuned for speak Gulf Arabic, and a generic Arabic recognizer mis-hears
    the dialect.
    """
    tag = (language or "").strip()
    if not tag:
        return "en-US"
    if "-" in tag:
        return tag
    return {"ar": "ar-SA", "en": "en-US"}.get(tag.lower(), tag)


def _ssml(
    text: str, *, voice: str, language: Optional[str],
    speaking_rate: Optional[str] = None,
) -> str:
    """Wrap `text` in SSML for `voice`.

    Escaping is load-bearing, not hygiene: the text here is agent output that
    can contain `&`, `<` or `>` (a figure like "P&L", an inequality in a
    threshold). Unescaped, those either break the document -- the service
    returns 400 and the caller hears nothing -- or, with attacker-influenced
    content, inject SSML elements that change what is spoken.

    Note the split: `escape()` for the TEXT node, `quoteattr()` for the two
    ATTRIBUTES. `escape()` deliberately does not touch quotes, so using it on
    an attribute would let a voice name containing `"` close the attribute and
    inject markup -- and `voice_id`/`language` are operator-editable
    config-DB values, not source constants. `quoteattr` supplies its own
    surrounding quotes, which is why there are none in the f-string.

    `speaking_rate` (voice_config.speaking_rate, e.g. "+10%" / "-20%" / "0.9")
    wraps the voice's content in a <prosody rate="..."> element when set --
    same operator-editable-value posture as voice/language, so the attribute
    is quoteattr'd too. Unset (None) produces byte-identical SSML to before
    this field existed: no <prosody> element at all, not one with a default
    rate -- a provider that never receives the element can't reject it.
    """
    locale = _locale(language)
    content = xml_escape(text)
    if speaking_rate:
        content = f"<prosody rate={quoteattr(speaking_rate)}>{content}</prosody>"
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f"xml:lang={quoteattr(locale)}>"
        f"<voice name={quoteattr(voice)}>{content}</voice>"
        f"</speak>"
    )
