"""Shared coercion helpers for the duplex voice-config resolver.

This module used to resolve a whole turn-based `VoiceConfig` (env defaults ->
agent-profile `voice_config` -> channel `config.voice`). The turn-based
`/voice/*` routes are retired, so that resolver is gone; what remains are the
two primitives `voice/live/config.py` imports to apply the SAME precedence
rule to `LiveVoiceConfig` — an explicitly-null override never clobbers a
lower-precedence value, and a stringy boolean means what the operator wrote.

The one non-duplex caller left (a product's batch transcribe route) never
used the profile or channel layer at all; it reads env defaults directly via
`transcribe_voice_config()`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def coerce_bool(value: Any, default: bool) -> bool:
    """Coerce a raw-operator-JSON boolean, shared with the live resolver.

    `voice_config` blocks reach the resolvers unvalidated — the portal form
    emits real JSON booleans, but `update_profile(voice_config=...)` over the
    admin MCP (and a direct DB edit) does not, so a stringy "false" can land
    here. A non-empty string is TRUTHY in Python, which means every
    `if not cfg.<flag>` gate downstream would read the operator's explicit
    OFF as ON — silently defeating exactly the flags that exist as rollback
    levers. Real bools pass through untouched; the usual string spellings map
    to their meaning (case/whitespace-insensitive, "" counts as off); any
    other string is garbage and falls back to `default`, same posture as the
    numeric coercion in voice/live/config.py; non-string scalars (a JSON
    0/1) keep plain truthiness.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("false", "0", "no", "off", ""):
            return False
        if token in ("true", "1", "yes", "on"):
            return True
        return default
    try:
        return bool(value)
    except Exception:
        return default


def apply_overrides(cfg: Any, overrides: Optional[Dict[str, Any]], fields: tuple) -> None:
    """Copy non-None values for `fields` from `overrides` onto `cfg`.

    Shared with the live-voice resolver (`voice/live/config.py`) so both
    layers honour the same env -> profile -> channel precedence rule: an
    explicitly-null override never clobbers a lower-precedence value.
    """
    if not overrides:
        return
    for key in fields:
        if key in overrides and overrides[key] is not None:
            setattr(cfg, key, overrides[key])
