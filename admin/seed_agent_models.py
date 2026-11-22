"""Seed the shared model catalog into agent_models.

Models are global (no agent_kind), so they seed on every build, scoped or not.
Idempotent on (name, environment) — admin edits via /admin/agents survive.

Providers seeded here: `google_genai` (Gemini — the DEFAULT chat brain, Gemini
API key), `azure_openai` (aoai-example deployments), and `azure_voice_live` (the
duplex voice ears-and-mouth rows). NOT `vertex_ai`: the deprecated
`ChatVertexAI` broke the agent's tool loop, and all three Vertex providers were
deleted outright on 2026-08-16 (they needed GCP Application Default Credentials,
absent on Azure compute — see the 2026-07-26 note below, which removed the Claude
rows for the same reason a fortnight earlier). Claude is reachable through the
`anthropic` provider (direct API, ANTHROPIC_API_KEY) if a row is added from
/admin/agents; no Claude row is seeded today.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)

_ENV = "staging"  # dev resolves to staging (core/agent_profile._default_environment)


def _m(name, provider, model_string, location, tokencost_model=None, temperature=0.2,
       cin=None, cout=None, omit_temperature=False, realtime=False,
       extra: Optional[Dict[str, Any]] = None):
    extra_config: Dict[str, Any] = {"location": location, "pricing_mode": "auto"}
    if omit_temperature:
        # Some models reject a non-default `temperature` and 400 the request:
        # Claude Opus 4.8+ deprecated it, and the gpt-5/o-series reasoning
        # models accept only the API default.
        extra_config["omit_temperature"] = True
    if tokencost_model:
        extra_config["tokencost_model"] = tokencost_model
    if cin is not None:
        extra_config["cost_per_1k_in"] = cin
    if cout is not None:
        extra_config["cost_per_1k_out"] = cout
    if realtime:
        # Ears-and-mouth model for duplex voice (WS <portal>/voice/live),
        # referenced by voice_config.live.model_id — never by an agent's
        # model_main_id. `admin/_model_picker.py::is_chat_model` (imported
        # by both agent_models pickers: fetch_models_for_picker and
        # admin/screens/agents.py's inline model_main_id dropdown query)
        # reads this flag to exclude the row from every chat-model picker.
        extra_config["realtime"] = True
    # Merge row-specific extra keys last so they override defaults
    extra_config.update(extra or {})
    return {
        "name": name, "provider": provider, "model_string": model_string,
        "environment": _ENV, "temperature": temperature, "is_active": True,
        "extra_config": extra_config,
    }


# PROVIDERS: Gemini runs via `google_genai` (Gemini API key) — NOT `vertex_ai`:
# the deprecated `ChatVertexAI` breaks the agent's tool-calling/streaming loop
# (a real cross-model eval surfaced this — Gemini-via-vertex_ai returned error
# turns while google_genai passes cleanly), and the demo already ships a
# GOOGLE_API_KEY. `vertex_ai` / `anthropic_vertex` / `vertex_maas` no longer
# exist as of 2026-08-16 — do not add a row for any of them, `build_chat_model`
# will raise and the /admin/agents form will reject the save.
# `tokencost_model` maps each (preview) id to its closest key in tokencost's
# price DB (which has no gemini-3.x / claude-4.x-named rows yet) so AUTO pricing
# actually resolves — the 2.5-series / claude-4 prices match these models. The
# cin/cout below stay as a manual fallback if tokencost ever drops the key.
MODELS: List[Dict[str, Any]] = [
    # Gemini flash catalog. INVOKE-VERIFIED 2026-07-22 (the model LIST lies —
    # advertised ids can 404 on invoke, so each is confirmed with a real
    # generateContent call): gemini-3.6-flash and gemini-3.5-flash-lite are the
    # current callable flash / flash-lite (bare ids; the -preview suffixes 404).
    # gemini-3.1-pro-preview remains the only callable Gemini Pro. The older
    # gemini-3-flash-preview / gemini-3.1-flash-lite-preview rows are kept for
    # rollback until the 3.6 / 3.5-lite promotion is verified in prod.
    _m("Gemini 3.1 Pro", "google_genai", "gemini-3.1-pro-preview", "global",
       tokencost_model="gemini-2.5-pro", cin=0.00125, cout=0.01),
    _m("Gemini 3.5 Flash", "google_genai", "gemini-3.5-flash", "global",
       tokencost_model="gemini-2.5-flash", cin=0.0003, cout=0.0025),
    _m("Gemini 3 Flash", "google_genai", "gemini-3-flash-preview", "global",
       tokencost_model="gemini-2.5-flash", cin=0.0003, cout=0.0025),
    # 2026-07 upgrade tier — the new callable flash / flash-lite. temperature 1.0
    # matches the current prod rows they replace (gemini-3-flash-preview /
    # gemini-3.1-flash-lite-preview). Pricing maps to the 2.5-series tokencost
    # keys until Google publishes 3.6/3.5-lite rates (cin/cout stay as fallback).
    _m("Gemini 3.6 Flash", "google_genai", "gemini-3.6-flash", "global",
       tokencost_model="gemini-2.5-flash", cin=0.0003, cout=0.0025, temperature=1.0),
    _m("Gemini 3.5 Flash-Lite", "google_genai", "gemini-3.5-flash-lite", "global",
       tokencost_model="gemini-2.5-flash-lite", cin=0.0001, cout=0.0004, temperature=1.0),
    # Azure-hosted chat brain (directive 2026-07-30: models/VMs/LLMs live in
    # Azure — see the voice phases 2-4 design spec). `model_string` is the
    # aoai-example DEPLOYMENT name, not an OpenAI model id (_build_azure_openai
    # passes it as azure_deployment); the endpoint/key come from
    # AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY env so one row serves every
    # deployment env. gpt-5-mini is a reasoning model: temperature is rejected
    # (omit_temperature, same escape hatch as Opus 4.8) and reasoning_effort
    # is pinned MINIMAL — this row's job is a low-latency conversational
    # brain, and the Phase-1 latency card is watching. pricing_mode manual:
    # $0.25/$2 per 1M retail; AUTO would depend on tokencost carrying the
    # bare deployment name, which is exactly the silent-wrong-rate trap the
    # voice rows document.
    _m("Azure GPT-5 Mini (uaenorth)", "azure_openai", "gpt-5-mini", "uaenorth",
       cin=0.00025, cout=0.002, omit_temperature=True,
       extra={"reasoning_effort": "minimal",
              "api_version": "2025-04-01-preview",
              "pricing_mode": "manual"}),
    # NOTE (2026-07-26, Azure migration): the three `anthropic_vertex` Claude rows
    # (Opus 4.8 / Sonnet 4.6 / Haiku 4.5) were REMOVED here. That provider mints a
    # token via `google.auth.default()` — GCP Application Default Credentials — which
    # does not exist on Azure compute, so those rows cannot build once the engine runs
    # outside GCP. They were staging-only and unused by any promoted profile.
    # FOLLOW-UP (2026-08-16): the `anthropic_vertex` BUILDER itself is now gone too,
    # so this is no longer just "don't seed these rows" — the provider is unbuildable
    # and unsaveable. Re-add Claude, if ever needed, on the `anthropic` provider.
    #
    # Removal order is load-bearing: `run()` below is insert-only and re-inserts any
    # MISSING row on every service startup, so this seeder change had to land and
    # deploy BEFORE the rows were deleted from the AI Supabase. Deleting first would
    # have restored them on the next boot.
    # Duplex voice (WS <portal>/voice/live). Referenced by
    # voice_config.live.model_id — NOT by any agent's model_main_id: this is
    # the ears-and-mouth model, not a chat brain. `realtime: True` is read by
    # `admin/_model_picker.py::is_chat_model` to keep this row OUT of both
    # chat-model pickers (the eval/prompt-optimizer picker and the agent
    # profile editor's model_main_id dropdown) — it stays `is_active: True`
    # (the duplex path resolves it via AgentProfileLoader.fetch_model, which
    # must keep seeing it) but is invisible where an operator picks a chat
    # brain. cin/cout mirror the other rows mapped to the same
    # tokencost_model — a manual fallback only; tokencost has no dedicated
    # native-audio rate yet and AUTO pricing is what's actually used.
    # Bake-off 2026-07-27, same synthesized audio through all three via the
    # Live API with ask_agent declared. Transcription accuracy vs ground truth:
    #   gemini-2.5-flash-native-audio-preview-09-2025   87%  "total sales lactic?"
    #   gemini-2.5-flash-native-audio-preview-12-2025   87%  "total sales lactic?"
    #   gemini-3.1-flash-live-preview                  100%  exact
    # The native-audio pair also dropped a store name outright ("breakdown for
    # Olaya Flagship" -> "breakdown for flagship"), which would silently send
    # the agent querying the wrong scope. The half-cascade `-live-` family
    # (native audio in, TTS out) is the better transcriber and tool-caller;
    # native-audio's nicer prosody is not worth mishearing the question.
    #
    # A NEW name rather than editing the old row's model_string: this seeder is
    # insert-only and idempotent on (name, environment), so an edit would never
    # reach a DB that already has the row -- a new name inserts cleanly
    # everywhere. Existing deployments keep the old row; repoint
    # voice_config.live.model_id (or delete it) to retire it.
    _m("Gemini 3.1 Flash Live (duplex voice)", "google_genai",
       "gemini-3.1-flash-live-preview", "global",
       tokencost_model="gemini-2.5-flash", cin=0.0003, cout=0.0025, realtime=True),
    # Duplex ears-and-mouth row for the Azure Voice Live provider; cascaded mode —
    # the model_string is Voice Live's fully-managed chat model, no Azure deployment
    # of ours. pricing_mode is pinned MANUAL, not auto: tokencost DOES carry a bare
    # "gpt-4.1-mini" key, but it's OpenAI's text-chat rate ($0.0004/$0.0016 per 1k) —
    # 16-27x below Voice Live's audio-token billing. AUTO would silently resolve to
    # that wrong rate; cin/cout here are the uaenorth Basic-tier audio-token retail
    # rates. NOTE for future rows: `extra` merges LAST in _m, so a key like
    # "realtime"/"location"/"pricing_mode" inside `extra` would clobber the flag-set
    # values — keep those out of `extra` unless clobbering is the point.
    # NATIVE-AUDIO duplex row. The cascaded row below reads replies through an
    # Azure TTS voice; Azure's ar-SA voices are MSA-trained, so they render
    # colloquial Saudi spellings wrong no matter how the text is written — the
    # ceiling that made the demo "not Saudi". gpt-realtime GENERATES the speech,
    # so the dialect comes from the model and actually sounds Saudi (verified
    # live: "حياك الله يا أخوي … تبغى نعدّله لك؟"). It is not offered in
    # uaenorth, hence a second Foundry resource in centralindia — the closest
    # region to the Gulf that carries it. Pro tier: ~$0.09/min vs ~$0.02
    # cascaded, which is the price of sounding right.
    #
    # THE CROSS-REGION HOP IS FORCED — DO NOT RE-INVESTIGATE. The engine runs
    # in uaenorth and this endpoint is in centralindia, so every audio frame
    # crosses regions and it looks like free latency lying on the table. It is
    # not. Re-verified live 2026-08-05 against the uaenorth resource
    # (`voice-live-example`), which is healthy and does serve Voice Live in
    # CASCADED mode — the positive control `gpt-4.1-mini` connects fine — but
    # rejects every native/realtime model by name at `session.update`:
    #   gpt-realtime, gpt-realtime-1.5, gpt-realtime-mini, gpt-4o-realtime-preview,
    #   gpt-4o-mini-realtime-preview, gpt-audio, gpt-audio-mini, azure-realtime,
    #   phi4-mm-realtime, phi4-mini, gpt-5-realtime
    #     -> "Model <name> is not supported in this region."
    # (gpt-5.4 fails differently — "Only Azure voice is supported" — i.e. it
    # routes, but cascaded-only, which is the same dead end for dialect.)
    # Microsoft's own region table agrees: uaenorth is not a Voice Live region
    # at all, and neither is qatarcentral (probed with a throwaway Foundry
    # resource 2026-08-05: the AI Foundry data plane has no DNS there).
    # centralindia is the NEAREST Voice Live region to the Gulf, measured by
    # TCP connect from the demo VM in uaenorth:
    #   centralindia 42ms | westeurope 100-123 | francecentral 109-120
    #   | swedencentral 141-147 | eastus2 192-203   (uaenorth itself 6ms)
    # And the hop is cheaper than it looks — it costs ~36ms round-trip on the
    # media path (~18ms one-way per audio frame) plus ~150ms one-time at
    # session setup. That is an order of magnitude below the knobs that
    # actually shape perceived latency here (turn_detection.silence_duration_ms
    # is ~950ms; a delegated agent turn is ~10s), so moving the region is not
    # the latency win it appears to be. The only ways out are moving the whole
    # engine into a Voice Live region — trading a 36ms media hop for a
    # cross-region hop on Supabase, the MCP tools and every other dependency —
    # or giving up native audio and with it the Saudi dialect.
    _m("Azure Voice Live Native (gpt-realtime)", "azure_voice_live", "gpt-realtime",
       "centralindia", cin=0.017, cout=0.064, realtime=True,
       extra={"endpoint": "https://voice-live-example.services.ai.azure.com",
              "api_version": "2026-04-10",
              "auth_env_var": "AZURE_VOICE_LIVE_NATIVE_API_KEY",
              "voice_type": "native",
              # azure-speech, NOT gpt-4o-transcribe, even though this is a
              # native-audio row. The model still HEARS the audio itself; this
              # setting only picks the recognizer that writes the DISPLAY
              # transcript. gpt-4o-transcribe is Whisper-family, so its
              # `language` is a hint that biases decoding rather than a
              # constraint: on silence or room noise it emits memorised
              # subtitle boilerplate, and on a client demo an ar-SA-pinned
              # session produced Bulgarian ("Довиждане"), German ("Das ist
              # auch ein Werbung"), Danish and Japanese in the transcript pane.
              # azure-speech is LOCKED to the configured locale and cannot
              # return another language at all, and it is the only recognizer
              # that honours `voice_config.live.phrase_list` (the domain-term
              # boost the gpt-4o family rejects). Live-probed 2026-07-30:
              # gpt-realtime accepts azure-speech, with a phrase list.
              "transcription_model": "azure-speech",
              "pricing_mode": "manual"}),
    _m("Azure Voice Live (gpt-4.1-mini)", "azure_voice_live", "gpt-4.1-mini",
       "uaenorth", cin=0.011, cout=0.026, realtime=True,
       extra={"endpoint": "https://voice-live-example.services.ai.azure.com",
              "api_version": "2026-04-10",
              "auth_env_var": "AZURE_VOICE_LIVE_API_KEY",
              "pricing_mode": "manual",
              # Cascaded ONLY. Measured live: this model treats a -42dBFS room
              # noise floor as speech and interrupt_response then kills the
              # reply ~0.2s in, every turn. The *_assistant_speaking guards fix
              # that here. They are NOT in the provider default because on the
              # native row the same guards stop a HUMAN interrupting at all
              # (see _session_payload's measurement table).
              "turn_detection": {
                  "type": "azure_semantic_vad",
                  "interrupt_response": True,
                  "auto_truncate": True,
                  "threshold": 0.8,
                  "speech_duration_ms": 600,
                  "silence_duration_ms": 700,
                  "remove_filler_words": True,
                  "require_vowel_assistant_speaking": True,
                  "distinct_ci_phones_assistant_speaking": 3,
                  "window_size_assistant_speaking": 300,
              }}),]

# Operative thinking levels per row (Gemini 3+ defaults to thinking=high,
# ~24s/turn). Set on the catalog rows via the admin MCP after eval bake-offs;
# mirrored here so a fresh seed matches the operative DB config:
# - 3.6 Flash at LOW: prod suite matrix showed low holds quality (>= baseline,
#   ~= high) at ~20-30% lower latency.
# - 3.5 Flash-Lite at MINIMAL: the lite-tier suite check (2026-07-23) scored
#   minimal ABOVE low (0.983 vs 0.959) and slightly faster — minimal is the
#   prod operative for the lite tier.
_THINKING_LEVELS = {
    "Gemini 3.6 Flash": "low",
    "Gemini 3.5 Flash-Lite": "minimal",
    # A client demo's operative chat brain (its AGENT_MAIN_MODEL =
    # gemini-3-flash-preview). At the provider default (high, ~24s/turn) a
    # duplex voice turn pays the thinking cost on TWO serial passes
    # (tool-call pass + answer pass); the same-family 3.6 matrix showed low
    # holds quality at ~20-30% lower latency. Seeder is insert-only — an
    # already-seeded env (a running demo) needs the same
    # extra_config edit applied to its live row via the portal/admin MCP.
    "Gemini 3 Flash": "low",
}
for _row in MODELS:
    _lvl = _THINKING_LEVELS.get(_row["name"])
    if _lvl:
        _row["extra_config"]["thinking_level"] = _lvl

_ENGINE_MODELS: List[Dict[str, Any]] = MODELS


def _plugin_model_rows() -> List[Dict[str, Any]]:
    """Model rows a plugin seeds for its own agents (hook "model_seeds"),
    built fresh on each call."""
    from core.plugins import call_hook, load_hook

    return [row for found in call_hook(load_hook("model_seeds"), "model_rows") for row in found]


# Every row this checkout knows, for readers that list the catalog.
MODELS = _ENGINE_MODELS + _plugin_model_rows()


# Rows that only mean something on the deployment they were written for:
# models tuned for one partner's agent, and Azure rows whose endpoint or
# deployment name is that deployment's own resource. A build with no product
# plugin (engine_plugins.PLUGINS empty) seeds the rest.
_DEPLOYMENT_ROWS = frozenset({
    "Azure GPT-5 Mini (uaenorth)",
    "Azure Voice Live Native (gpt-realtime)",
    "Azure Voice Live (gpt-4.1-mini)",
})


def rows_for_this_build() -> List[Dict[str, Any]]:
    from core.plugins import has_product

    if has_product():
        rows = list(_ENGINE_MODELS)
    else:
        rows = [m for m in _ENGINE_MODELS if m["name"] not in _DEPLOYMENT_ROWS]
    return rows + _plugin_model_rows()


def run() -> Dict[str, int]:
    client = get_service_client()
    inserted = skipped = 0
    for model in rows_for_this_build():
        existing = (
            client.table("agent_models").select("model_id")
            .eq("name", model["name"]).eq("environment", model["environment"])
            .limit(1).execute().data or []
        )
        if existing:
            skipped += 1
            continue
        client.table("agent_models").insert(model).execute()
        inserted += 1
    logger.info("seed_agent_models: inserted=%d skipped=%d", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped}
