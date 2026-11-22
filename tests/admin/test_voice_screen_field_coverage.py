"""A duplex-voice key cannot ship invisible.

This is the test the voice-config unification spec asks for by name, and the
reason the whole D2 exercise exists. The spec counted **fourteen**
operator-honoured `voice_config` keys with no UI at all; two of them —
`barge_in_enabled` and `answer_watchdog_sec` — are the levers `CLAUDE.md`
marks as *silent when wrong*, and one was pulled during incident #38 and left
disengaged for days precisely because no screen showed it.

Every one of those fourteen arrived the same way: someone added a field to
`LiveVoiceConfig`, wired it into `_VOICE_FIELDS`, taught the endpoint to read
it — and the admin form, a hand-maintained mapping in a different file, simply
did not grow a row. No test failed, because no test was written *about the new
key*.

So the assertion here is deliberately **generative**, modelled on
a bilingual-parity test's rule-count check: it fires for a key
nobody has written a pattern for. `_VOICE_FIELDS` is partitioned — every key is
either a control on the screen or a named entry in `_NO_UI_BY_DESIGN`, and the
two never overlap. Adding a key to the runtime and nothing else fails this
file with the key's own name in the message.
"""
from __future__ import annotations

import pytest

from admin.screens.agent_voice import (VOICE_FIELDS, VOICE_SECTIONS,
                                       _NO_UI_BY_DESIGN, describe_effective_overrides,
                                       field_defaults, provider_capabilities,
                                       render_values, voice_config_from_form)
from voice.live.config import LiveVoiceConfig, _VOICE_FIELDS


def _form(**over):
    """A stand-in for the voice screen's POST: the `_voice_form=1` marker plus
    whatever `vc_*` fields the caller names."""
    form = {"_voice_form": "1"}
    form.update({f"vc_{k}": v for k, v in over.items()})
    return form


# --------------------------------------------------------------------------- #
# The generative partition
# --------------------------------------------------------------------------- #
def test_every_honoured_voice_key_has_a_field_or_a_stated_exemption():
    """THE test. A key in `_VOICE_FIELDS` is honoured by the runtime, so it
    must be visible to an operator or excluded on purpose."""
    with_field = {f.key for f in VOICE_FIELDS}
    covered = with_field | set(_NO_UI_BY_DESIGN)
    missing = set(_VOICE_FIELDS) - covered
    assert not missing, (
        "voice_config keys the runtime honours with no control on "
        "/admin/agents/{id}/voice and no entry in _NO_UI_BY_DESIGN: "
        f"{sorted(missing)}. Add a VoiceField for each, or add it to "
        "_NO_UI_BY_DESIGN with the reason written next to it. An operator "
        "cannot restore a lever they cannot see — that is how barge_in_enabled "
        "stayed off for days after incident #38."
    )


def test_no_field_exists_for_a_key_the_runtime_does_not_read():
    """The other direction: a control for a key `_VOICE_FIELDS` does not carry
    edits nothing. That is the same defect from the opposite side — the screen
    asserting a value the session will never use."""
    stale = {f.key for f in VOICE_FIELDS} - set(_VOICE_FIELDS)
    assert not stale, (
        f"voice screen renders controls for keys the resolver ignores: "
        f"{sorted(stale)}"
    )


def test_the_partition_does_not_overlap():
    """A key cannot be both rendered and exempt — that combination reads as
    'documented as invisible' while being perfectly visible, and the next
    person to trust the exemption list is misled."""
    both = {f.key for f in VOICE_FIELDS} & set(_NO_UI_BY_DESIGN)
    assert not both, sorted(both)


def test_exemptions_name_only_real_keys():
    assert set(_NO_UI_BY_DESIGN) <= set(_VOICE_FIELDS)


def test_the_two_incident_levers_are_rendered_specifically():
    """Named explicitly, not just covered by the partition above: these two are
    the reason the spec was written, and a future refactor that moved them into
    `_NO_UI_BY_DESIGN` would satisfy the generative test while re-creating the
    exact defect."""
    rendered = {f.key for f in VOICE_FIELDS}
    assert "barge_in_enabled" in rendered
    assert "answer_watchdog_sec" in rendered


def test_every_field_lands_in_a_declared_section():
    section_ids = {sid for sid, _t, _b in VOICE_SECTIONS}
    orphans = {f.key: f.section for f in VOICE_FIELDS if f.section not in section_ids}
    assert not orphans, orphans


def test_every_section_has_at_least_one_field():
    used = {f.section for f in VOICE_FIELDS}
    empty = [sid for sid, _t, _b in VOICE_SECTIONS if sid not in used]
    assert not empty, empty


def test_every_field_declares_a_known_kind_and_some_help():
    known = {"bool", "number", "int", "text", "lines", "variants", "json", "model"}
    for f in VOICE_FIELDS:
        assert f.kind in known, (f.key, f.kind)
        # Help text is not decoration on this screen: several of these values
        # are only safe to change if you know what the default protects.
        assert f.help.strip(), f.key


def test_field_names_are_unique_and_prefixed():
    names = [f.field_name for f in VOICE_FIELDS]
    assert len(names) == len(set(names))
    assert all(n.startswith("vc_") for n in names)


def test_defaults_come_from_the_dataclass_not_a_second_copy():
    """The "unset resolves to X" line under each control must be the value the
    resolver actually falls back to. A hand-typed default that drifts from
    `LiveVoiceConfig` is the screen lying about the runtime again."""
    cfg = LiveVoiceConfig()
    for key, shown in field_defaults().items():
        assert shown == getattr(cfg, key), key


# --------------------------------------------------------------------------- #
# Write path — this screen owns every key
# --------------------------------------------------------------------------- #
def test_unmarked_submission_writes_nothing():
    assert voice_config_from_form({"vc_enabled": "on"}) is None


def test_round_trip_preserves_every_kind():
    out = voice_config_from_form(_form(
        enabled="on", model_id="m-1", voice_id="Kore", language="ar-SA",
        persona_prompt_slug="orchard_voice_persona",
        agent_profile_slug="orchard_voice", greeting_trigger="أهلاً",
        barge_in_enabled="on", answer_watchdog_sec="4.5",
        stall_recovery_topic_terms="sales\nbranch",
        progress_spoken="one moment\nlahza",
        voice_by_language='{"ar": "ar-SA-HamedNeural"}',
        relay_max_turns="9", max_concurrent_per_ip="12",
    ), existing={})
    assert out["enabled"] is True
    assert out["model_id"] == "m-1"
    assert out["answer_watchdog_sec"] == 4.5
    assert out["stall_recovery_topic_terms"] == ["sales", "branch"]
    assert out["progress_spoken"] == ["one moment", "lahza"]
    assert out["voice_by_language"] == {"ar": "ar-SA-HamedNeural"}
    assert out["relay_max_turns"] == 9
    assert out["max_concurrent_per_ip"] == 12
    # Unchecked booleans persist a real False — this screen renders every
    # control, so absence is a decision.
    assert out["text_only"] is False
    assert out["stream_sentences"] is False


def test_a_single_line_holding_line_stays_a_string():
    """`progress_spoken` is string-OR-list. One line must round-trip as the
    string the runtime and every existing row already carry, not as a
    one-element list, or a save silently rewrites the shape of the value."""
    out = voice_config_from_form(_form(progress_spoken="one moment"), existing={})
    assert out["progress_spoken"] == "one moment"


def test_blank_clears_a_field_back_to_the_default():
    out = voice_config_from_form(
        _form(voice_id="", answer_watchdog_sec=""),
        existing={"voice_id": "Charon", "answer_watchdog_sec": 0},
    )
    assert "voice_id" not in out
    assert "answer_watchdog_sec" not in out


def test_keys_with_no_control_survive_a_save():
    """`_retired_turn_based` is the real one: the unify migration parks the
    pre-duplex block there for one release, and a save from this screen must
    not be what deletes an operator's archived turn-based tuning."""
    out = voice_config_from_form(
        _form(enabled="on"),
        existing={"_retired_turn_based": {"voice_id": "rachel"},
                  "some_future_key": "kept"},
    )
    assert out["_retired_turn_based"] == {"voice_id": "rachel"}
    assert out["some_future_key"] == "kept"


def test_a_legacy_nested_row_is_migrated_by_a_save():
    """Part B input #7: the read shim stays until every stack has migrated, so
    this screen must render and save through it. Flat keys written beside a
    surviving `live` key are invisible — `_voice_block` resolves the nested
    block alone."""
    out = voice_config_from_form(
        _form(enabled="on"),
        existing={"voice_id": "rachel",
                  "live": {"model_id": "m-old", "greeting_trigger": "hi"}},
    )
    assert "live" not in out
    assert out["model_id"] == "m-old"
    assert out["greeting_trigger"] == "hi"
    assert out["_retired_turn_based"] == {"voice_id": "rachel"}


def test_unparsable_input_keeps_the_prior_value_rather_than_resetting_it():
    """A typo must not silently re-arm a lever the operator deliberately set.
    Falling through the blank-clears path would reset `answer_watchdog_sec` to
    its 4s default — turning a fat-fingered keystroke into a config change
    nobody asked for."""
    out = voice_config_from_form(
        _form(answer_watchdog_sec="fuor", voice_by_language="{oops"),
        existing={"answer_watchdog_sec": 0, "voice_by_language": {"ar": "x"}},
    )
    assert out["answer_watchdog_sec"] == 0
    assert out["voice_by_language"] == {"ar": "x"}


def test_render_values_show_the_dataclass_default_for_an_unset_checkbox():
    """A checkbox has no "unset" position, so an absent default-True key must
    render CHECKED — rendering it unchecked would be the screen asserting the
    opposite of what the session resolves."""
    values = render_values({})
    assert values["stream_sentences"] is True
    assert values["relay_mode"] is True
    assert values["enabled"] is False
    assert values["barge_in_enabled"] is True


def test_render_values_do_not_prefill_a_load_bearing_empty_default():
    """`stall_recovery_topic_terms` renders EMPTY even though its default is a
    meaningful `[]`. Prefilling would make "nobody configured this" and
    "someone set it to empty" indistinguishable, and the effective-value note
    below is what carries the meaning instead."""
    assert render_values({})["stall_recovery_topic_terms"] == ""


# --------------------------------------------------------------------------- #
# Effective-value notes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider,level,capability", [
    # Cannot take several results per call id at all — streaming really is OFF.
    ("azure_voice_live", "off", "supports_streamed_tool_results"),
    # Takes them and SPEAKS them, so streaming is ON — but does not transcribe
    # them (probed live 2026-08-19: the caller heard every word of a streamed
    # answer and read the first two of each sentence), so the screen's copy
    # comes from the endpoint. An "off" note here would be a lie about a knob
    # that is doing its job, which is the exact failure this screen exists to
    # prevent.
    ("google_genai", "info", "transcribes_streamed_tool_results"),
])
def test_stream_sentences_note_follows_the_declared_provider_capability(
    provider, level, capability,
):
    """The spec's worst case, and the two capabilities do NOT mean the same
    thing: one disables streaming, the other only moves where the on-screen
    text comes from. Reading both flags off the provider class means this note
    cannot disagree with `endpoints/voice_live.py`."""
    caps = provider_capabilities(provider)
    assert caps is not None
    notes = describe_effective_overrides(
        {"enabled": True, "model_id": "m", "persona_prompt_slug": "p",
         "stream_sentences": True},
        capabilities=caps,
    )
    matching = [n for n in notes.get("stream_sentences", [])
                if n["level"] == level]
    assert matching, (
        f"{provider} has no {level!r} note on stream_sentences: "
        f"{notes.get('stream_sentences')}"
    )
    assert capability in matching[0]["text"], matching[0]["text"]
    if level == "info":
        assert not [n for n in notes["stream_sentences"] if n["level"] == "off"], (
            "streaming is ON here — an 'off' note would send an operator "
            "chasing a latency knob that is already engaged"
        )


def test_empty_topic_terms_say_both_residual_tiers_are_disabled():
    """The load-bearing default. Empty is not "unconfigured" — it is two stall
    recovery tiers switched off, and an empty textarea says that to nobody."""
    notes = describe_effective_overrides({"enabled": True, "model_id": "m",
                                          "persona_prompt_slug": "p"})
    text = " ".join(n["text"] for n in notes["stall_recovery_topic_terms"])
    assert "DISABLED" in text
    # …and the two tiers it gates say so on their own controls too.
    assert any("gated on the topic terms" in n["text"]
               for n in notes["stall_recovery_unannounced_sec"])
    assert any("gated on the topic terms" in n["text"]
               for n in notes["stall_recovery_silent_sec"])


def test_the_incident_levers_warn_when_disengaged():
    notes = describe_effective_overrides({
        "enabled": True, "model_id": "m", "persona_prompt_slug": "p",
        "barge_in_enabled": False, "answer_watchdog_sec": 0,
        "deterministic_render_enabled": False,
    })
    assert any(n["level"] == "warn" for n in notes["barge_in_enabled"])
    assert any(n["level"] == "warn" for n in notes["answer_watchdog_sec"])
    assert any(n["level"] == "warn" for n in notes["deterministic_render_enabled"])


def test_native_voice_row_says_the_speaking_rate_is_dropped():
    notes = describe_effective_overrides(
        {"enabled": True, "model_id": "m", "persona_prompt_slug": "p",
         "speaking_rate": "+10%"},
        model_row={"extra_config": {"voice_type": "native"}},
        capabilities=provider_capabilities("azure_voice_live"),
    )
    assert any(n["level"] == "off" for n in notes["speaking_rate"])


def test_text_only_reports_what_it_force_disables():
    notes = describe_effective_overrides({
        "enabled": True, "model_id": "m", "persona_prompt_slug": "p",
        "text_only": True, "progress_after_sec": 8, "answer_watchdog_sec": 4,
        "greeting_trigger": "hello",
    })
    assert any(n["level"] == "off" for n in notes["progress_after_sec"])
    assert any(n["level"] == "off" for n in notes["answer_watchdog_sec"])
    assert any(n["level"] == "off" for n in notes["greeting_trigger"])


def test_gemini_says_the_phrase_list_and_voice_map_are_ignored():
    notes = describe_effective_overrides(
        {"enabled": True, "model_id": "m", "persona_prompt_slug": "p",
         "phrase_list": ["Orchard"], "voice_by_language": {"ar": "x"}},
        capabilities=provider_capabilities("google_genai"),
    )
    assert any(n["level"] == "off" for n in notes["phrase_list"])
    assert any(n["level"] == "off" for n in notes["voice_by_language"])


def test_notes_survive_raw_operator_json_in_a_numeric_field():
    """`voice_config` is raw operator JSON — the admin MCP's `update_profile`
    and a direct DB edit both bypass this form's float(). A note generator that
    raised on "eight" would 500 the screen for exactly the misconfigured tenant
    it exists to explain."""
    notes = describe_effective_overrides({
        "enabled": True, "model_id": "m", "persona_prompt_slug": "p",
        "answer_watchdog_sec": "eight", "interrupt_confirm_window_sec": None,
    })
    assert isinstance(notes, dict)


def test_unknown_provider_slug_has_no_capabilities_and_does_not_raise():
    assert provider_capabilities("azure_openai") is None
    assert provider_capabilities(None) is None
    notes = describe_effective_overrides(
        {"enabled": True, "model_id": "m", "persona_prompt_slug": "p",
         "stream_sentences": True},
        capabilities=None,
    )
    # Unknown provider = unknown gate. Say so rather than asserting "on".
    assert any(n["level"] == "info" for n in notes["stream_sentences"])
