"""Resolve a LiveVoiceConfig to a concrete RealtimeSession.

Mirrors `voice/factory.py` and `runtime/llm_factory.py`: the provider slug
comes from a config-DB row, and this module is the single place that decides
what actually works. Switching clouds is an `agent_models` row edit, not a
deploy.

Adding a provider: implement RealtimeSession, then call
register_realtime_provider(<slug>, <builder>) at import time from
voice/live/providers/<name>.py.
"""
from __future__ import annotations

from typing import Callable, Dict

from voice.live.base import RealtimeSession
from voice.live.config import LiveVoiceConfig


class UnsupportedRealtimeProviderError(RuntimeError):
    """The config names a realtime provider with no working builder."""


# (config, persona) -> session. The persona is an explicit argument rather
# than a field smuggled through cfg: it is per-request (resolved from the
# prompt row at handshake) while LiveVoiceConfig is per-tenant config, and
# mutating a config object to pass a parameter is how those two lifetimes get
# confused.
_BUILDERS: Dict[str, Callable[[LiveVoiceConfig, str], RealtimeSession]] = {}


def register_realtime_provider(
    slug: str, builder: Callable[[LiveVoiceConfig, str], RealtimeSession]
) -> None:
    _BUILDERS[slug.lower()] = builder


def build_realtime_session(cfg: LiveVoiceConfig, *, persona: str) -> RealtimeSession:
    builder = _BUILDERS.get((cfg.provider or "").lower())
    if builder is None:
        raise UnsupportedRealtimeProviderError(
            f"unknown realtime provider '{cfg.provider}'. "
            f"Supported: {sorted(_BUILDERS)}"
        )
    return builder(cfg, persona)
