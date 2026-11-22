"""Gemini STT (audio->text) + TTS (text->PCM, wrapped as WAV).

Restored from the original /tts endpoint (commit 61087577) and the batch
transcribe handler, refactored behind the VoiceProvider interface.
synthesize_stream synthesizes one Gemini TTS call per inbound text chunk
(sentence), which is how streaming-as-tokens-arrive is realized: short chunks
mean the first audio packet leaves quickly.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import AsyncIterator, Optional

from google import genai
from google.genai import types as genai_types

from voice.audio import pcm_to_wav
from voice.providers.base import AudioChunk, TranscriptResult, VoiceCaps, VoiceProvider, VoiceProviderError

logger = logging.getLogger(__name__)

_TRANSCRIBE_PROMPT = (
    "Transcribe the audio verbatim. Return only the spoken text in the "
    "original language (Arabic or English are both expected). Do not add "
    "commentary, translations, headings, or punctuation that wasn't spoken."
)


class GeminiVoiceProvider(VoiceProvider):
    name = "gemini"
    caps = VoiceCaps(supports_stt=True, supports_tts=True, supports_streaming_stt=False)

    def __init__(self, *, stt_model: str, tts_model: str, api_key: Optional[str] = None) -> None:
        self._stt_model = stt_model
        self._tts_model = tts_model
        self._client = genai.Client(api_key=api_key or os.getenv("GOOGLE_API_KEY", ""))

    async def transcribe(self, audio: bytes, mime: str, *, language: Optional[str] = None) -> TranscriptResult:
        try:
            resp = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._stt_model,
                contents=[
                    _TRANSCRIBE_PROMPT,
                    genai_types.Part.from_bytes(data=audio, mime_type=mime),
                ],
            )
        except Exception as exc:
            logger.error("Gemini transcribe failed: %s: %s", type(exc).__name__, exc, exc_info=True)
            raise VoiceProviderError("transcription provider error") from exc
        text = (getattr(resp, "text", "") or "").strip()
        return TranscriptResult(text=text, model=self._stt_model, language=language)

    async def synthesize_stream(
        self, text_chunks: AsyncIterator[str], *, voice: str, language: Optional[str], sample_rate: int
    ) -> AsyncIterator[AudioChunk]:
        seq = 0
        async for text in text_chunks:
            if not text.strip():
                continue
            pcm = await self._synthesize_pcm(text, voice=voice)
            yield AudioChunk(data=pcm_to_wav(pcm, sample_rate=sample_rate), mime="audio/wav", seq=seq, sample_rate=sample_rate)
            seq += 1

    async def _synthesize_pcm(self, text: str, *, voice: str) -> bytes:
        config = genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        )
        try:
            resp = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._tts_model,
                contents=text,
                config=config,
            )
        except Exception as exc:
            logger.error("Gemini TTS failed: %s: %s", type(exc).__name__, exc, exc_info=True)
            raise VoiceProviderError("speech synthesis provider error") from exc
        pcm = _extract_pcm_bytes(resp)
        if not pcm:
            raise VoiceProviderError("TTS provider returned no audio")
        return pcm


def _extract_pcm_bytes(resp) -> bytes:
    """Pull the first inline audio payload out of a generate_content response."""
    for cand in getattr(resp, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data is None:
                continue
            return data if isinstance(data, bytes) else base64.b64decode(data)
    return b""
