"""
Centralized runtime configuration for the WAJ AI agents.

Every tunable (model names, sampling temperatures, tool-loop bounds, truncation
limits, rate limits, similarity thresholds) lives here so we have a single
source of truth and every knob is overridable via environment variables.

Rule of thumb: if you find yourself about to hardcode a number or model name
in agent code, add a field here and read it from AGENT_CONFIG instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import FrozenSet

# Safe at module level today: core/embedding_provider.py imports nothing from
# this package (stdlib only; the provider SDKs are imported lazily inside its
# functions), so there is no cycle. Keep it that way — if that module ever
# needs AGENT_CONFIG, move this to a function-local import rather than
# duplicating the EMBED_PROVIDER string check here.
from core.embedding_provider import active_provider


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw else default


# A similarity threshold is only meaningful relative to the embedding space
# that produced the vectors it is compared against — the same query/document
# pair scores differently under Gemini `gemini-embedding-001` and Azure
# `text-embedding-3-small`, so one number cannot serve both. The default
# therefore follows whichever provider is actually in force
# (`core.embedding_provider.active_provider()`, i.e. EMBED_PROVIDER); an
# explicit KB_MATCH_THRESHOLD still wins over both. The OpenAI-compatible
# provider has no entry: its scores depend on whichever model the operator
# serves, which no table here can know, so it gets the fallback and the
# operator pins KB_MATCH_THRESHOLD if their model scores lower.
_KB_MATCH_THRESHOLD_BY_PROVIDER: dict[str, float] = {
    # Azure text-embedding-3-small scores systematically lower: measured on a
    # client demo VM (2026-08-06) on-topic queries land 0.52-0.75 but
    # borderline-relevant ones ~0.365, which a 0.4 floor turned into "No
    # relevant documentation". Re-measured 2026-08-27 after a live call got
    # "No relevant documentation" for mada: SHORT and ARABIC queries score
    # lower still — "mada" hit its right page at 0.25 and «قائمة مدى» at
    # 0.243, both filtered by the 0.3 floor, while the nearest WRONG page sat
    # at 0.214. The model then answered from its own patterns — the
    # hallucination shape the KB exists to prevent, silently. 0.22 sits in
    # the measured gap between right (0.24+) and wrong (0.21-).
    "azure": 0.22,
}
# Gemini (`google`, the EMBED_PROVIDER default) — the historical, calibrated
# value. PR #87 shipped 0.3 flat, which paired the Azure number with the
# Gemini default and quietly loosened KB search on every un-pinned stack.
_KB_MATCH_THRESHOLD_FALLBACK = 0.4


def _kb_match_threshold() -> float:
    provider = active_provider()
    return _float(
        "KB_MATCH_THRESHOLD",
        _KB_MATCH_THRESHOLD_BY_PROVIDER.get(provider, _KB_MATCH_THRESHOLD_FALLBACK),
    )


SUPPORTED_LOCALES: FrozenSet[str] = frozenset({"en", "ar", "tr", "ru", "de"})


@dataclass(frozen=True)
class AgentConfig:
    """All tunables the engine itself reads (models, loop bounds, limits).

    Access via the module-level `AGENT_CONFIG` singleton. Values are resolved
    from environment variables once at import time; tests can monkeypatch
    the singleton or construct a fresh AgentConfig().
    """

    # --- Models ------------------------------------------------------------
    main_model: str = field(default_factory=lambda: _str("AGENT_MAIN_MODEL", "gemini-3-flash-preview"))
    light_model: str = field(default_factory=lambda: _str("AGENT_LIGHT_MODEL", "gemini-3.5-flash"))

    # --- Sampling ----------------------------------------------------------
    main_temperature: float = field(default_factory=lambda: _float("AGENT_MAIN_TEMPERATURE", 1.0))
    light_temperature: float = field(default_factory=lambda: _float("AGENT_LIGHT_TEMPERATURE", 1.0))

    # --- Tool-loop bounds --------------------------------------------------
    # Per-turn agent loop cap. Guards against runaway tool-calling.
    max_iterations_main: int = field(default_factory=lambda: _int("AGENT_MAX_ITERATIONS", 10))

    # Sub-agent delegation (ask_<slug> tools). Bounded loop + wall-clock
    # budget for one delegated request; depth is always 1 (not configurable).
    delegate_max_iterations: int = field(default_factory=lambda: _int("AGENT_DELEGATE_MAX_ITERATIONS", 6))
    delegate_timeout_seconds: int = field(default_factory=lambda: _int("AGENT_DELEGATE_TIMEOUT_SECONDS", 120))

    # Safety brake on per-request token spend. When exceeded the loop aborts
    # rather than continuing to burn tokens on a stuck chain.
    per_request_token_budget: int = field(default_factory=lambda: _int("AGENT_TOKEN_BUDGET", 200_000))
    per_request_token_warn_ratio: float = field(
        default_factory=lambda: _float("AGENT_TOKEN_BUDGET_WARN_RATIO", 0.8)
    )

    # --- Truncation / context shaping -------------------------------------
    # Large tool payloads are truncated before being appended to the LLM
    # context. The truncation marker is user-visible to the LLM so it knows
    # data was cut and can ask for a narrower query instead of confabulating.
    tool_result_char_limit: int = field(default_factory=lambda: _int("AGENT_TOOL_RESULT_CHARS", 8000))
    # Catalog lookups (a services or staff list), already slimmed to the
    # fields a reply needs. A whole menu or team must fit, or the model only knows the
    # first few (a 56-service, 40-staff business slims to ~18k / ~17k chars).
    catalog_result_char_limit: int = field(default_factory=lambda: _int("AGENT_CATALOG_RESULT_CHARS", 20000))
    history_message_char_limit: int = field(default_factory=lambda: _int("AGENT_HISTORY_MSG_CHARS", 4000))
    history_message_limit_main: int = field(default_factory=lambda: _int("AGENT_HISTORY_MSG_COUNT", 10))
    history_message_limit_sales: int = field(default_factory=lambda: _int("SALES_HISTORY_MSG_COUNT", 6))
    # Duplex-voice delegate turns (voice/live/delegate.py -> run_turn with
    # voice_turn=True) clamp the profile's history window to this many
    # messages: every history row is prompt tokens on ~2 sequential model
    # calls per spoken answer, and the delegate must stay under the realtime
    # service's ~8s tool-drop ceiling. A clamp, not an override — a profile
    # window smaller than this stays smaller. Text chat is unaffected.
    voice_history_msg_count: int = field(default_factory=lambda: _int("VOICE_HISTORY_MSG_COUNT", 12))

    # Tools that return >csv_export_threshold chars get a CSV sidecar.
    csv_export_threshold: int = field(default_factory=lambda: _int("AGENT_CSV_EXPORT_THRESHOLD", 1000))

    # --- Misc tunables -----------------------------------------------------
    docs_match_threshold: float = field(default_factory=lambda: _float("DOCS_MATCH_THRESHOLD", 0.55))
    # Cosine-similarity floor for the knowledge-base searches that call
    # `match_kb_embeddings` directly. The default is NOT a constant — it follows
    # EMBED_PROVIDER, because a threshold is only meaningful relative to the
    # embedding space that produced the vectors: 0.3 under Azure
    # text-embedding-3-small, 0.4 under Gemini (the EMBED_PROVIDER default).
    # See _kb_match_threshold() above for the measurements behind each.
    # KB_MATCH_THRESHOLD pins it explicitly and wins over both.
    kb_match_threshold: float = field(default_factory=_kb_match_threshold)
    suggestion_max: int = field(default_factory=lambda: _int("AGENT_SUGGESTION_MAX", 3))
    title_max_chars: int = field(default_factory=lambda: _int("AGENT_TITLE_MAX_CHARS", 60))


    # --- Voice (STT/TTS edge module) --------------------------------------
    # Defaults; per-agent profile + channel override these (see voice/config.py).
    #
    # AZURE by default, matching the duplex edge (voice/live/providers/
    # azure_voice_live.py) and the completed GCP->Azure migration: the engine
    # runs on Azure, so its ears and mouth should not be the one thing still
    # calling Google. Azure Speech selects its synthesizer by VOICE NAME (the
    # profile's `voice_config.voice_id`, e.g. "ar-SA-HamedNeural") and its
    # recognizer by LOCALE -- it has no model string, so the *_model defaults
    # below are inert for `azure` and apply only to a tenant that pins `gemini`.
    #
    # Needs AZURE_SPEECH_KEY (+ AZURE_SPEECH_REGION, default uaenorth). A
    # deployment without those must set VOICE_STT_PROVIDER back to `gemini` --
    # the provider fails loudly at request time rather than silently falling
    # back, so the misconfiguration names itself.
    # No `voice_enabled` / `voice_default_voice` / `voice_max_chars` /
    # `voice_tts_provider` here any more: the turn-based /voice/* routes and the
    # Agents screen's turn-based Voice fieldset were their only consumers, and
    # both are gone (2026-08-17). They were deleted rather than left
    # documented-as-dead because `VOICE_ENABLED=false` reads exactly like a
    # voice kill switch and did nothing — an operator-visible knob with no
    # reader is the same invisible-config failure this module's env-var
    # contract exists to avoid. Duplex voice is per-profile config:
    # voice_config.enabled / .voice_id / .voice_by_language
    # (voice/live/config.py).
    #
    # `voice_tts_provider` went the same way (2026-08-18): nothing calls
    # `voice/factory.py::build_tts_provider` any more. `transcribe_voice_config()`
    # builds the STT side only, and the one component that still SYNTHESIZES
    # speech — admin/voice_scenario_runner.py, the simulated caller — constructs
    # `AzureSpeechVoiceProvider` directly rather than resolving a provider
    # string, so `VOICE_TTS_PROVIDER=gemini` moved nothing. `voice_tts_model`
    # below is NOT dead by the same test: transcribe_voice_config() carries it
    # because both registered providers read it on the way to an instance.
    voice_stt_provider: str = field(default_factory=lambda: _str("VOICE_STT_PROVIDER", "azure"))
    voice_stt_model: str = field(default_factory=lambda: _str("VOICE_STT_MODEL", _str("AGENT_LIGHT_MODEL", "gemini-3.5-flash")))
    voice_tts_model: str = field(default_factory=lambda: _str("VOICE_TTS_MODEL", _str("TTS_MODEL", "gemini-2.5-flash-preview-tts")))
    voice_sample_rate: int = field(default_factory=lambda: _int("VOICE_SAMPLE_RATE", 24000))


    # --- Rate limiting -----------------------------------------------------
    rate_limit_requests: int = field(default_factory=lambda: _int("RATE_LIMIT_REQUESTS", 100))
    rate_limit_window_sec: int = field(default_factory=lambda: _int("RATE_LIMIT_WINDOW", 60))


AGENT_CONFIG = AgentConfig()


TRUNCATION_MARKER_TMPL = (
    "\n...[truncated: {omitted} chars omitted. If you need the rest, narrow"
    " the query (add filters, date range, or IDs) rather than re-fetching.]"
)


def truncate_for_llm(text: str, limit: int | None = None) -> str:
    """Truncate a long string for LLM context, with a marker that signals the
    cut to the model so it can ask for a narrower query instead of hallucinating
    the missing tail.
    """
    cap = limit if limit is not None else AGENT_CONFIG.tool_result_char_limit
    if len(text) <= cap:
        return text
    omitted = len(text) - cap
    return text[:cap] + TRUNCATION_MARKER_TMPL.format(omitted=omitted)
