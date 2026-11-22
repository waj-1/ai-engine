"""Resolve a voice-settings object -> concrete providers. STT and TTS are built
independently, so a tenant may pair one provider's ears with another's mouth
(Azure STT + Gemini TTS is a valid, if unusual, configuration).

REGISTERED PROVIDERS: `azure` (the default, and what the deployed engine runs
since the GCP->Azure migration) and `gemini` (the original, kept working).
Nothing else exists -- the admin docs previously advertised `elevenlabs` and
`self_hosted`, which were never implemented and fail at request time.

Adding a provider: implement VoiceProvider, then register a builder in
_STT_BUILDERS and/or _TTS_BUILDERS keyed by the provider slug.

`cfg` is duck-typed (`Any`) rather than the old `voice.config.VoiceConfig`,
which went away with the turn-based `/voice/*` routes. Any object carrying
`stt_provider` / `tts_provider` plus `stt_model`, `tts_model` and
`speaking_rate` works — today that is `main.transcribe_voice_config()`'s
namespace. Note both builders read BOTH models: one provider object serves
ears and mouth, so an stt-only caller must still supply `tts_model` (and
`speaking_rate` for azure) or it gets an AttributeError.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from voice.providers.azure import AzureSpeechVoiceProvider
from voice.providers.base import VoiceProvider
from voice.providers.gemini import GeminiVoiceProvider


class UnsupportedVoiceProviderError(RuntimeError):
    """Raised when a voice-settings object names a provider with no builder."""


def _gemini(cfg: Any) -> VoiceProvider:
    return GeminiVoiceProvider(stt_model=cfg.stt_model, tts_model=cfg.tts_model)


def _azure(cfg: Any) -> VoiceProvider:
    return AzureSpeechVoiceProvider(
        stt_model=cfg.stt_model,
        tts_model=cfg.tts_model,
        speaking_rate=cfg.speaking_rate,
    )


# `azure_speech` is accepted as an alias so an operator who wrote the fuller
# name in voice_config doesn't get an opaque "unknown voice provider".
_STT_BUILDERS: Dict[str, Callable[[Any], VoiceProvider]] = {
    "azure": _azure,
    "azure_speech": _azure,
    "gemini": _gemini,
}
_TTS_BUILDERS: Dict[str, Callable[[Any], VoiceProvider]] = {
    "azure": _azure,
    "azure_speech": _azure,
    "gemini": _gemini,
}


def _build(builders: Dict[str, Callable[[Any], VoiceProvider]], provider: str, cfg: Any) -> VoiceProvider:
    builder = builders.get((provider or "").lower())
    if builder is None:
        raise UnsupportedVoiceProviderError(
            f"unknown voice provider '{provider}'. Supported: {sorted(builders)}"
        )
    return builder(cfg)


def build_stt_provider(cfg: Any) -> VoiceProvider:
    return _build(_STT_BUILDERS, cfg.stt_provider, cfg)


def build_tts_provider(cfg: Any) -> VoiceProvider:
    return _build(_TTS_BUILDERS, cfg.tts_provider, cfg)
