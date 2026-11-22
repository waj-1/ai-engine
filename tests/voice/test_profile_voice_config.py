from core.agent_profile import Profile, AgentProfileLoader
from runtime import compiler


def test_profile_default_voice_config_is_empty_dict():
    p = Profile(profile_id=None, agent_kind="custom", slug="s", environment="dev")
    assert p.voice_config == {}


def test_snapshot_path_carries_voice_config():
    row = {"profile_id": "p1", "agent_kind": "custom", "slug": "s"}
    snapshot = {
        "agent_kind": "custom",
        "slug": "s",
        "voice_config": {"tts_provider": "elevenlabs", "voice_id": "rachel"},
    }
    p = AgentProfileLoader._profile_from_snapshot(row, snapshot, "prod")
    assert p.voice_config == {"tts_provider": "elevenlabs", "voice_id": "rachel"}


def test_compiler_includes_voice_config_and_bumps_schema():
    assert "voice_config" in compiler._SHALLOW_FIELDS
    # >= not ==: voice_config entered at v3; later features (v5 skills) bump it.
    assert compiler._SNAPSHOT_SCHEMA_VERSION >= 4
