import pytest

from voice.live.config import LiveVoiceConfig, hydrate_live_model, resolve_live_config, resolve_session_voice


def test_live_is_disabled_by_default():
    cfg = resolve_live_config(profile_voice=None, channel=None)
    assert cfg.enabled is False


def test_profile_live_block_is_applied():
    cfg = resolve_live_config(
        profile_voice={"live": {"enabled": True, "voice_id": "Puck", "model_id": "m-1"}},
        channel=None,
    )
    assert cfg.enabled is True
    assert cfg.voice_id == "Puck"
    assert cfg.model_id == "m-1"


def test_channel_layer_no_longer_overrides_the_profile():
    """The agent_channels.config.voice.live layer was removed 2026-08-17 —
    a channel value must never reach the resolved config."""
    cfg = resolve_live_config(
        profile_voice={"enabled": True, "voice_id": "Puck", "language": "en"},
        channel={"config": {"voice": {"live": {"voice_id": "Kore", "language": "ar"}}}},
    )
    assert cfg.voice_id == "Puck"
    assert cfg.language == "en"
    assert cfg.enabled is True


def test_a_null_override_does_not_clobber():
    """apply_overrides' null-safety: an explicit `None` in profile_voice must
    not overwrite the dataclass default it would otherwise replace — the
    same rule that used to protect the profile layer from a null channel
    override, now exercised on the one layer that's left."""
    cfg = resolve_live_config(
        profile_voice={"enabled": True, "voice_id": None},
        channel=None,
    )
    assert cfg.voice_id == "Kore"       # dataclass default survives the null
    assert cfg.enabled is True          # the real override still lands


def test_turn_based_voice_keys_are_ignored_by_the_live_resolver():
    cfg = resolve_live_config(
        profile_voice={"voice_id": "Charon", "live": {"enabled": True}}, channel=None
    )
    assert cfg.voice_id == "Kore"       # the live block's own default


def test_hydrate_fills_provider_and_model_string(monkeypatch):
    from core.agent_profile import AgentProfileLoader

    monkeypatch.setattr(
        AgentProfileLoader,
        "fetch_model",
        staticmethod(
            lambda client, model_id, environment=None: {
                "model_id": model_id,
                "provider": "google_genai",
                "model_string": "gemini-2.5-flash-native-audio-preview",
                "extra_config": {"endpoint": "https://example"},
            }
        ),
    )
    cfg = LiveVoiceConfig(enabled=True, model_id="m-1")
    out = hydrate_live_model(cfg, client=object())
    assert out.provider == "google_genai"
    assert out.model_string == "gemini-2.5-flash-native-audio-preview"
    assert out.extra_config == {"endpoint": "https://example"}


def test_hydrate_raises_when_the_model_row_is_missing(monkeypatch):
    from core.agent_profile import AgentProfileLoader

    monkeypatch.setattr(
        AgentProfileLoader,
        "fetch_model",
        staticmethod(lambda client, model_id, environment=None: None),
    )
    with pytest.raises(LookupError):
        hydrate_live_model(LiveVoiceConfig(enabled=True, model_id="missing"), client=object())


def test_hydrate_raises_when_no_model_is_configured():
    with pytest.raises(LookupError):
        hydrate_live_model(LiveVoiceConfig(enabled=True, model_id=None), client=object())


def test_live_config_new_knobs_resolve_from_profile():
    cfg = resolve_live_config(
        profile_voice={"phrase_list": ["Northwind", "IBAN"], "voice_by_language": {"ar": "ar-SA-HamedNeural"}},
        channel=None,
    )
    assert cfg.phrase_list == ["Northwind", "IBAN"]
    assert cfg.voice_by_language == {"ar": "ar-SA-HamedNeural"}
    # the channel layer no longer applies — removed 2026-08-17
    cfg2 = resolve_live_config(
        profile_voice={"phrase_list": ["Northwind"]},
        channel={"config": {"voice": {"live": {"phrase_list": ["mada"]}}}},
    )
    assert cfg2.phrase_list == ["Northwind"]


def test_live_config_new_knobs_default_empty():
    cfg = resolve_live_config(profile_voice=None, channel=None)
    assert cfg.phrase_list == []
    assert cfg.voice_by_language == {}


def test_agent_profile_slug_resolves_from_profile():
    """Serving a call from a different profile of the same kind is operator
    config, resolved from the profile's flat voice_config. The channel layer
    that used to override this was removed 2026-08-17."""
    cfg = resolve_live_config(
        profile_voice={"agent_profile_slug": "sample_voice_specialist"}, channel=None
    )
    assert cfg.agent_profile_slug == "sample_voice_specialist"

    cfg2 = resolve_live_config(
        profile_voice={"agent_profile_slug": "sample_voice_specialist"},
        channel={"config": {"voice": {"live": {"agent_profile_slug": "sample_voice_ar"}}}},
    )
    assert cfg2.agent_profile_slug == "sample_voice_specialist"


def test_agent_profile_slug_defaults_unset():
    """Unset means "serve the kind's active profile" — the pre-feature
    behaviour, which is also the rollback position."""
    assert resolve_live_config(profile_voice=None, channel=None).agent_profile_slug is None


def test_resolve_session_voice_prefers_language_map():
    cfg = resolve_live_config(profile_voice={"live": {
        "language": "ar-SA",
        "voice_id": "en-US-AvaMultilingualNeural",
        "voice_by_language": {"ar": "ar-SA-HamedNeural", "en": "en-US-AvaMultilingualNeural"},
    }}, channel=None)
    assert resolve_session_voice(cfg) == "ar-SA-HamedNeural"


def test_resolve_session_voice_tolerates_non_dict_map():
    # Raw operator JSON: a string/list voice_by_language must fall back to
    # voice_id, not AttributeError at session build.
    cfg = resolve_live_config(profile_voice={"live": {
        "language": "ar-SA", "voice_id": "Kore", "voice_by_language": "oops",
    }}, channel=None)
    assert resolve_session_voice(cfg) == "Kore"


def test_resolve_session_voice_falls_back_to_voice_id():
    cfg = resolve_live_config(profile_voice={"live": {"language": "fr-FR", "voice_id": "Kore",
                                                      "voice_by_language": {"ar": "x"}}}, channel=None)
    assert resolve_session_voice(cfg) == "Kore"
    cfg2 = resolve_live_config(profile_voice={"live": {"voice_id": "Kore"}}, channel=None)
    assert resolve_session_voice(cfg2) == "Kore"  # no language at all


def test_resolve_progress_spoken_prefers_the_session_language():
    """An English holding line landed mid-call in an Arabic session on the
    demo VM — the holding line must follow the language being spoken."""
    from voice.live.config import resolve_progress_spoken

    cfg = resolve_live_config(profile_voice={"live": {
        "language": "ar-SA",
        "progress_spoken": "Still working on that, one moment.",
        "progress_spoken_by_language": {"ar": "لحظة، أتأكد لك الحين."},
    }}, channel=None)
    assert resolve_progress_spoken(cfg) == "لحظة، أتأكد لك الحين."

    # Unmapped language, no language, and a non-dict map all fall back.
    for live in (
        {"language": "fr-FR", "progress_spoken": "X",
         "progress_spoken_by_language": {"ar": "Y"}},
        {"progress_spoken": "X", "progress_spoken_by_language": {"ar": "Y"}},
        {"language": "ar-SA", "progress_spoken": "X",
         "progress_spoken_by_language": "oops"},
    ):
        assert resolve_progress_spoken(
            resolve_live_config(profile_voice={"live": live}, channel=None)
        ) == "X"


def test_progress_spoken_accepts_a_list_of_variants():
    """A caller who hears the identical holding line every lookup clocks it
    as fake. Operators may store a LIST (flat or per-language); one variant
    is picked per resolution, and the full list is exposed for Azure's
    server-side interim_response texts."""
    from voice.live.config import (
        resolve_progress_spoken,
        resolve_progress_spoken_variants,
    )

    cfg = LiveVoiceConfig(progress_spoken=["A.", "B."], language="en-US")
    assert resolve_progress_spoken_variants(cfg) == ["A.", "B."]
    assert resolve_progress_spoken(cfg) in ("A.", "B.")

    per_lang = LiveVoiceConfig(
        progress_spoken="Flat.",
        progress_spoken_by_language={"ar": ["أ", "ب"]},
        language="ar-SA",
    )
    assert resolve_progress_spoken_variants(per_lang) == ["أ", "ب"]
    assert resolve_progress_spoken(per_lang) in ("أ", "ب")


def test_progress_variants_fall_back_to_the_flat_string():
    from voice.live.config import resolve_progress_spoken_variants

    cfg = LiveVoiceConfig(language="en-US")
    assert resolve_progress_spoken_variants(cfg) == [
        "Still working on that, one moment."
    ]
    # Blank config -> no variants (progress line disabled).
    assert resolve_progress_spoken_variants(
        LiveVoiceConfig(progress_spoken="")
    ) == []


def test_interrupt_confirm_window_resolves_and_defaults():
    """The false-barge-in recovery window is an operator knob like every
    other live key: profile/channel override, 0 = legacy behaviour."""
    assert LiveVoiceConfig().interrupt_confirm_window_sec == 2.0
    cfg = resolve_live_config(
        profile_voice={"live": {"interrupt_confirm_window_sec": 0}}, channel=None
    )
    assert cfg.interrupt_confirm_window_sec == 0


def test_stream_sentences_defaults_on():
    # Sentence streaming is the measured 10s -> ~3s time-to-first-speech win.
    # Providers that can't take several tool results per call id are still
    # gated off structurally (supports_streamed_tool_results in
    # endpoints/voice_live.py), so the default only activates where safe.
    cfg = resolve_live_config(profile_voice=None, channel=None)
    assert cfg.stream_sentences is True


def test_stream_sentences_explicit_false_still_wins():
    # apply_overrides skips only None: an operator's explicit false must keep
    # turning streaming OFF now that the default is on.
    cfg = resolve_live_config(
        profile_voice={"live": {"stream_sentences": False}}, channel=None
    )
    assert cfg.stream_sentences is False


def test_operator_can_set_the_spoken_fallbacks_per_language():
    from voice.live.config import (
        resolve_failure_spoken,
        resolve_live_config,
        resolve_on_screen_spoken,
    )

    cfg = resolve_live_config(
        profile_voice={"live": {
            "failure_spoken": "Sorry about that.",
            "failure_spoken_by_language": {"ar": "أعتذر منك."},
            "on_screen_spoken": "It's on screen.",
            "on_screen_spoken_by_language": {"ar": "ظاهر على الشاشة."},
        }},
        channel=None,
    )
    cfg.language = "ar-SA"
    assert resolve_failure_spoken(cfg) == "أعتذر منك."
    assert resolve_on_screen_spoken(cfg) == "ظاهر على الشاشة."
    cfg.language = "en-US"
    assert resolve_failure_spoken(cfg) == "Sorry about that."
    assert resolve_on_screen_spoken(cfg) == "It's on screen."


def test_unset_fallbacks_resolve_empty_so_the_builtin_wins():
    """Blank must fall through to the built-in per-language wording, not to
    silence — a caller hearing nothing is worse than hearing English."""
    from voice.live.config import (
        resolve_failure_spoken,
        resolve_live_config,
        resolve_on_screen_spoken,
    )

    cfg = resolve_live_config(profile_voice=None, channel=None)
    cfg.language = "ar-SA"
    assert resolve_failure_spoken(cfg) == ""
    assert resolve_on_screen_spoken(cfg) == ""


def test_relay_mode_defaults_on_and_overrides_off():
    """Custody is the CORRECT default (three live failure modes in one day);
    the per-tenant false is the rollback lever, same posture as
    stream_sentences."""
    from voice.live.config import LiveVoiceConfig, resolve_live_config
    assert LiveVoiceConfig().relay_mode is True
    cfg = resolve_live_config(
        profile_voice={"live": {"relay_mode": False}}, channel=None
    )
    assert cfg.relay_mode is False


def test_relay_intent_terms_default_covers_iban_spellings():
    from voice.live.config import LiveVoiceConfig
    terms = LiveVoiceConfig().relay_intent_terms
    assert "iban" in terms and "آيبان" in terms
    # ASR garbles the prompts already enumerate ("E band", "eye ban") must be
    # in the DEFAULT list: a garbled opening beat that misses the intent
    # match never opens custody, and the realtime model then runs the
    # protected flow itself — observed live 2026-07-31 (custody never opened;
    # the model asked for the new IBAN with no OTP).
    assert "e band" in terms and "eye ban" in terms


# ---------------------------------------------------------------------------
# Raw-operator-JSON coercion for the numeric live fields. `voice_config.live`
# reaches us unvalidated over the admin MCP / a direct DB edit, and several of
# these are read on the hot path (handshake concurrency check, relay ceilings
# inside the pump loop) where a raise kills the whole call.
# ---------------------------------------------------------------------------


def test_numeric_live_fields_survive_garbage_operator_json():
    cfg = resolve_live_config(
        profile_voice={"live": {
            "max_concurrent_sessions": "eight",   # unparseable
            "relay_max_turns": None,              # null
            "max_session_sec": "not-a-number",
            "relay_quiet_grace_turns": [],        # wrong type entirely
        }},
        channel=None,
    )
    # Falls back to the dataclass defaults rather than raising later.
    assert cfg.max_concurrent_sessions == 3
    assert cfg.relay_max_turns == 8
    assert cfg.max_session_sec == 900.0
    assert cfg.relay_quiet_grace_turns == 2


def test_numeric_live_fields_accept_json_numeric_strings_and_floats():
    cfg = resolve_live_config(
        profile_voice={"live": {
            "max_concurrent_sessions": "5",   # numeric string from a form/JSON
            "relay_max_turns": 6.0,           # float for an int field
            "relay_max_sec": "240",
            "handshake_charge_sec": 15,
        }},
        channel=None,
    )
    assert cfg.max_concurrent_sessions == 5
    assert cfg.relay_max_turns == 6
    assert cfg.relay_max_sec == 240.0
    assert cfg.handshake_charge_sec == 15.0


def test_numeric_ceilings_are_floored_so_a_typo_cannot_brick_the_tenant():
    """The handshake concurrency check is a bare `active >= cap`, so a typo'd
    -1 makes the FIRST connection over-limit and every call for the tenant
    dies CLOSE_RATE_LIMITED; relay_max_turns/-sec <= 0 close custody the
    moment it opens (the 2026-07-31 incident mode, silently). Zero has no
    legitimate meaning through these knobs — `enabled` / `relay_mode` are the
    off-switches — so the floor cannot change a real config."""
    cfg = resolve_live_config(
        profile_voice={"live": {
            "max_concurrent_sessions": -1,
            "max_concurrent_per_ip": 0,
            "relay_max_turns": -1,
            "relay_max_sec": 0,
        }},
        channel=None,
    )
    assert cfg.max_concurrent_sessions == 1
    assert cfg.max_concurrent_per_ip == 1
    assert cfg.relay_max_turns == 1
    assert cfg.relay_max_sec == 1.0
    # Legitimate configured ceilings pass through untouched.
    cfg2 = resolve_live_config(
        profile_voice={"live": {"max_concurrent_sessions": 5, "relay_max_turns": 12}},
        channel=None,
    )
    assert cfg2.max_concurrent_sessions == 5
    assert cfg2.relay_max_turns == 12


# ---------------------------------------------------------------------------
# Raw-operator-JSON coercion for the BOOLEAN live fields. A stringy "false"
# is a non-empty string — truthy — so without coercion every
# `if not cfg.<flag>` gate in the endpoint reads the operator's explicit OFF
# as ON, silently defeating the rollback levers (barge_in_enabled is the
# live-incident master switch, #38).
# ---------------------------------------------------------------------------


def test_stringy_false_actually_turns_the_flag_off():
    cfg = resolve_live_config(
        profile_voice={"live": {
            "barge_in_enabled": "false",
            "relay_mode": "0",
            "enabled": " OFF ",       # case/whitespace-insensitive
            "stream_sentences": "no",
        }},
        channel=None,
    )
    assert cfg.barge_in_enabled is False
    assert cfg.relay_mode is False
    assert cfg.enabled is False
    assert cfg.stream_sentences is False


def test_stringy_true_and_real_bools_resolve_true():
    cfg = resolve_live_config(
        profile_voice={"live": {
            "enabled": "true",
            # A digit string. `coerce_bool`'s "1" token had no other test left
            # once filler_enabled was deleted, and it is the shape a hand-edited
            # JSON blob (or a form that posts "1" for a checkbox) actually
            # produces. Asserted on a field whose default is FALSE on purpose:
            # dropping the token on a default-true field (barge_in_enabled,
            # relay_mode) falls back to True and the assertion still passes.
            "text_only": "1",
            "barge_in_enabled": True,
        }},
        channel=None,
    )
    assert cfg.enabled is True
    assert cfg.text_only is True
    assert cfg.barge_in_enabled is True


def test_garbage_bool_strings_fall_back_to_the_default():
    # "maybe" asserts nothing either way — falling back to the default is the
    # same posture the numeric pass takes on "eight".
    cfg = resolve_live_config(
        profile_voice={"live": {"barge_in_enabled": "maybe", "enabled": "banana"}},
        channel=None,
    )
    assert cfg.barge_in_enabled is True   # dataclass default
    assert cfg.enabled is False           # dataclass default


def test_json_numeric_bools_keep_plain_truthiness():
    cfg = resolve_live_config(
        profile_voice={"live": {"barge_in_enabled": 0, "relay_mode": 1}},
        channel=None,
    )
    assert cfg.barge_in_enabled is False
    assert cfg.relay_mode is True


def test_budget_charge_interval_resolves_and_defaults():
    """The periodic in-session budget charge is an operator knob: profile
    override, <= 0 = the legacy two-charge (handshake + teardown) scheme. The
    channel layer that used to override this was removed 2026-08-17."""
    assert LiveVoiceConfig().budget_charge_interval_sec == 60.0
    cfg = resolve_live_config(
        profile_voice={"budget_charge_interval_sec": "30"},
        channel=None,
    )
    assert cfg.budget_charge_interval_sec == 30.0


def test_budget_charge_interval_survives_garbage_operator_json():
    """It is in _NUMERIC_LIVE_FIELDS: raw operator JSON must coerce or fall
    back to the default, never raise on the session's hot path."""
    cfg = resolve_live_config(
        profile_voice={"budget_charge_interval_sec": "sixty"},
        channel=None,
    )
    assert cfg.budget_charge_interval_sec == 60.0


def test_text_only_defaults_false():
    cfg = resolve_live_config(profile_voice=None, channel=None)
    assert cfg.text_only is False


def test_text_only_set_from_profile():
    cfg = resolve_live_config(
        profile_voice={"live": {"text_only": True}}, channel=None
    )
    assert cfg.text_only is True


def test_text_only_stringy_false_is_false():
    # Raw operator JSON: "false" is a truthy non-empty string unless coerced.
    cfg = resolve_live_config(
        profile_voice={"live": {"text_only": "false"}}, channel=None
    )
    assert cfg.text_only is False


def test_round_trip_reduction_defaults():
    cfg = resolve_live_config(profile_voice={})
    assert cfg.kb_prefetch_enabled is True
    assert cfg.kb_prefetch_match_count == 3
    assert cfg.deterministic_render_enabled is True


def test_round_trip_reduction_overrides_from_flat_block():
    cfg = resolve_live_config(profile_voice={
        "kb_prefetch_enabled": False,
        "kb_prefetch_match_count": 5,
        "deterministic_render_enabled": False,
    })
    assert cfg.kb_prefetch_enabled is False
    assert cfg.kb_prefetch_match_count == 5
    assert cfg.deterministic_render_enabled is False


def test_stringy_booleans_are_coerced():
    # A "false" written over the admin MCP or a direct DB edit is a NON-EMPTY
    # string — truthy — so every `if not cfg.<flag>` gate would silently invert.
    cfg = resolve_live_config(profile_voice={
        "kb_prefetch_enabled": "false",
        "deterministic_render_enabled": "false",
    })
    assert cfg.kb_prefetch_enabled is False
    assert cfg.deterministic_render_enabled is False


def test_match_count_garbage_falls_back_to_default_and_floors_at_one():
    assert resolve_live_config(
        profile_voice={"kb_prefetch_match_count": "banana"}
    ).kb_prefetch_match_count == 3
    assert resolve_live_config(
        profile_voice={"kb_prefetch_match_count": 0}
    ).kb_prefetch_match_count == 1


# ---------------------------------------------------------------------------
# greeting_barge_in_guard_sec — Fix 1's rollback lever. Default ON at 8.0
# (not 0) because the bug it fixes was breaking every single call, not an
# opt-in improvement; 0 is the documented off-switch and must stay 0 (no
# floor), same posture as budget_charge_interval_sec.
# ---------------------------------------------------------------------------
def test_greeting_barge_in_guard_defaults_to_eight_seconds():
    assert LiveVoiceConfig().greeting_barge_in_guard_sec == 8.0
    cfg = resolve_live_config(profile_voice=None, channel=None)
    assert cfg.greeting_barge_in_guard_sec == 8.0


def test_greeting_barge_in_guard_resolves_from_profile():
    cfg = resolve_live_config(
        profile_voice={"greeting_barge_in_guard_sec": "3.5"}, channel=None,
    )
    assert cfg.greeting_barge_in_guard_sec == 3.5


def test_greeting_barge_in_guard_zero_stays_zero_the_rollback_lever():
    cfg = resolve_live_config(
        profile_voice={"greeting_barge_in_guard_sec": 0}, channel=None,
    )
    assert cfg.greeting_barge_in_guard_sec == 0.0


def test_greeting_barge_in_guard_survives_garbage_operator_json():
    cfg = resolve_live_config(
        profile_voice={"greeting_barge_in_guard_sec": "not-a-number"}, channel=None,
    )
    assert cfg.greeting_barge_in_guard_sec == 8.0


# ---------------------------------------------------------------------------
# progress_on_dispatch — Fix 2's mode switch. Default OFF: today's exact
# timer behaviour is byte-identical until a tenant opts in.
# ---------------------------------------------------------------------------
def test_progress_on_dispatch_defaults_false():
    assert LiveVoiceConfig().progress_on_dispatch is False
    cfg = resolve_live_config(profile_voice=None, channel=None)
    assert cfg.progress_on_dispatch is False


def test_progress_on_dispatch_resolves_true_from_profile():
    cfg = resolve_live_config(
        profile_voice={"progress_on_dispatch": True}, channel=None,
    )
    assert cfg.progress_on_dispatch is True


def test_progress_on_dispatch_stringy_false_actually_turns_it_off():
    cfg = resolve_live_config(
        profile_voice={"progress_on_dispatch": "false"}, channel=None,
    )
    assert cfg.progress_on_dispatch is False
