"""Can this realtime model row take a call on THIS server?

A realtime provider needs a credential the engine does not ship with. Without
one the provider fails inside `session.start()`, which the caller sees as a
bare "Voice provider unavailable." and an operator sees only in the logs.
`realtime_problem` answers the question before any connection is opened, in
words fit for a WebSocket close reason and for the voice settings page.

It reads what each provider reads and nothing else, so it cannot disagree
with them: `azure_voice_live` takes its key from the env var the row names
(`extra_config.auth_env_var`), Gemini Live from the row's own `api_key` or
the Google API key in the environment.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

_AZURE_DEFAULT_AUTH_ENV = "AZURE_VOICE_LIVE_API_KEY"
_GOOGLE_KEY_ENVS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")


def realtime_problem(provider: Optional[str], extra_config: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why a call on this provider row cannot start here, or None when it can.

    Short on purpose: a WebSocket close reason holds about 120 bytes.
    """
    from voice.live.factory import _BUILDERS

    slug = (provider or "").strip().lower()
    extra = extra_config if isinstance(extra_config, dict) else {}
    if not slug:
        return "No realtime voice model is set for this agent."
    if slug == "simulated":
        return None
    if slug == "azure_openai":
        return "azure_openai realtime voice is not available: use an azure_voice_live model."
    if slug not in _BUILDERS:
        return f"Realtime provider '{slug}' is not supported by this server."
    if slug == "azure_voice_live":
        if not str(extra.get("endpoint") or "").strip():
            return "The realtime model has no endpoint set."
        env_name = str(extra.get("auth_env_var") or _AZURE_DEFAULT_AUTH_ENV)
        if not os.getenv(env_name):
            return f"Realtime model key missing: set {env_name} on the server."
    elif slug == "google_genai":
        if not extra.get("api_key") and not any(os.getenv(n) for n in _GOOGLE_KEY_ENVS):
            return "Realtime model key missing: set GOOGLE_API_KEY on the server."
    return None


def config_problem(cfg: Any) -> Optional[str]:
    """`realtime_problem` for a resolved `LiveVoiceConfig`."""
    return realtime_problem(getattr(cfg, "provider", None), getattr(cfg, "extra_config", None))
