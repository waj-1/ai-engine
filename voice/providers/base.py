"""The VoiceProvider contract. STT and TTS are resolved independently, so a
provider may implement only one side; capability flags declare what works."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional


class VoiceProviderError(RuntimeError):
    """Raised when a provider call fails (surfaced to the endpoint as 502)."""


class UnsupportedAudioError(VoiceProviderError):
    """The caller's audio format is one this provider cannot accept.

    Separate from its parent because the two need different HTTP answers: a
    generic provider failure is a 502 the caller can do nothing about, while
    this is a 415 they can FIX by transcoding. Its message is written to be
    shown to the caller (it names the accepted formats) — unlike other
    provider errors, whose text may echo a vendor response body and is kept
    server-side.
    """


@dataclass
class TranscriptResult:
    text: str
    model: str
    language: Optional[str] = None
    is_final: bool = True


@dataclass
class AudioChunk:
    data: bytes
    mime: str
    seq: int
    sample_rate: int


@dataclass
class VoiceCaps:
    supports_stt: bool = False
    supports_tts: bool = False
    supports_streaming_stt: bool = False


class VoiceProvider(ABC):
    name: str = "base"
    caps: VoiceCaps = VoiceCaps()

    @abstractmethod
    async def transcribe(self, audio: bytes, mime: str, *, language: Optional[str] = None) -> TranscriptResult:
        ...

    @abstractmethod
    async def synthesize_stream(
        self,
        text_chunks: AsyncIterator[str],
        *,
        voice: str,
        language: Optional[str],
        sample_rate: int,
    ) -> AsyncIterator[AudioChunk]:
        ...

    async def synthesize(self, text: str, *, voice: str, language: Optional[str], sample_rate: int) -> bytes:
        """Convenience: collect synthesize_stream into a single buffer."""
        async def _one() -> AsyncIterator[str]:
            yield text

        parts: list[bytes] = []
        async for chunk in self.synthesize_stream(_one(), voice=voice, language=language, sample_rate=sample_rate):
            parts.append(chunk.data)
        return b"".join(parts)
