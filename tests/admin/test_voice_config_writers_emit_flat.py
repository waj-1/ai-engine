"""No writer may emit a `live` sub-block.

voice_config is flat as of the 2026-08-17 unification. The read shim in
voice/live/config.py::_voice_block resolves a nested block ALONE — ignoring
top-level keys — because that exactly preserves behaviour for an unmigrated
row. The consequence is that any writer which re-creates a `live` key on a
MIGRATED profile makes every flat key invisible: the tenant's real voice
config silently stops being read. This test is the guard against that, and it
is why it asserts on absence rather than on a value.

The portal writer under test changed on 2026-08-26: the Agents mapping form's
fieldset (`admin/screens/agents.py::build_voice_config_from_form`) is deleted,
and the voice cockpit's `admin/screens/agent_voice.py::voice_config_from_form`
is the single portal writer of the column — so the flat-shape contract is now
pinned against it.
"""
from admin.screens.agent_voice import voice_config_from_form


def _form(**over):
    form = {"_voice_form": "1"}
    form.update(over)
    return form


def test_portal_save_emits_no_live_key():
    out = voice_config_from_form(
        _form(vc_enabled="1", vc_model_id="m-1"), existing={},
    )
    assert "live" not in out, out
    assert out["enabled"] is True
    assert out["model_id"] == "m-1"


def test_portal_save_flattens_an_unmigrated_existing_row():
    """A profile saved before the migration still carries `live`. Saving it
    must MIGRATE it, not preserve the nested key alongside flat ones."""
    out = voice_config_from_form(
        _form(vc_enabled="1"),
        existing={"live": {"model_id": "m-old", "greeting_trigger": "hi"}},
    )
    assert "live" not in out, out
    assert out["model_id"] == "m-old"       # carried up, not dropped
    assert out["greeting_trigger"] == "hi"


def test_unmanaged_keys_still_survive_a_save():
    out = voice_config_from_form(
        _form(vc_enabled="1"),
        existing={"_retired_turn_based": {"voice_id": "rachel"},
                  "some_future_key": "kept"},
    )
    assert out["_retired_turn_based"] == {"voice_id": "rachel"}
    assert out["some_future_key"] == "kept"


def test_the_retired_turn_based_fields_are_not_written_any_more():
    """The eight turn-based keys were read only by the `/voice/*` routes,
    retired 2026-08-17. The writer must not emit them: a stored
    `enabled`/`voice_id` from that mapping would land in the DUPLEX
    namespace, where `enabled: true` would turn a tenant's realtime voice on
    and `voice_id` would hand an Azure neural voice name to a realtime model."""
    out = voice_config_from_form(
        _form(voice_enabled="on", voice_id="rachel",
              voice_stt_provider="gemini", voice_sample_rate="16000"),
        existing={},
    )
    assert "sample_rate" not in out
    assert "stt_provider" not in out
    assert out.get("voice_id") in (None, "")   # only vc_voice_id is honoured
    assert out.get("enabled") in (None, False)  # only vc_enabled is honoured


def test_no_voice_inputs_survive_in_the_agents_template():
    """The other half: a control that edits nothing (or the wrong row) is the
    defect the cockpit removes, so the mapping form carries NO voice inputs at
    all any more — neither the turn-based `voice_*` names nor the duplex
    `live_*` ones. The cockpit's own template renders its `vc_*` fields from
    the registry, pinned in test_voice_screen_routes.py."""
    from pathlib import Path

    html = Path("admin/templates/agents.html").read_text(encoding="utf-8")
    for field in ("voice_enabled", "voice_stt_provider", "voice_stt_model",
                  "voice_tts_provider", "voice_tts_model", "voice_language",
                  "voice_sample_rate", "voice_id"):
        assert f'name="{field}"' not in html, field
    assert 'name="live_' not in html
