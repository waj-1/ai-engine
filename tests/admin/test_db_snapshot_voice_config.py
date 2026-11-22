"""`db_snapshot restore` must not re-import a legacy `voice_config` shape.

Every other `voice_config` writer in the engine flattens through
`voice/live/config.py::promote_legacy_voice_config` — the portal save
(`admin/screens/agents.py`), the admin MCP (`admin_mcp/tools_configure.py`), the
seeders, the promote compiler. Restore was the one that upserted the exported
row verbatim, and it is the writer where that matters most: the documented
workflow is `export -> supabase db reset -> restore`, so the unify migration has
already applied to an EMPTY database by the time these rows land. Anything
nested that arrives afterwards is a shape no migration will ever look at again —
the read shim carries it until it is deleted "one release after the migration",
at which point that tenant's duplex config resolves to all-defaults.

These drive `_promote_voice_config` directly for the shapes, and `_restore_table`
through a recording stub for the wiring (a fix in the helper that the write path
never calls would pass every shape test and still ship the bug).
"""
from types import SimpleNamespace

from admin.db_snapshot import _promote_voice_config, _restore_table

NESTED = {
    "voice_id": "ar-SA-HamedNeural",          # turn-based, retired
    "live": {"enabled": True, "model_id": "m-1", "filler_enabled": True},
}
FLATTENED = {
    "enabled": True,
    "model_id": "m-1",
    "_retired_turn_based": {"voice_id": "ar-SA-HamedNeural"},
}


class _Recorder:
    """Minimal stand-in for the supabase client `_write_row` drives.

    `select(...).eq(...).limit(1).execute().data` is empty, so every row takes
    the INSERT branch and lands in `self.inserted`.
    """

    def __init__(self):
        self.inserted = []
        self._pending_insert = None

    def table(self, name):
        self._pending_insert = None
        return self

    def select(self, *_a, **_kw):
        return self

    def eq(self, *_a, **_kw):
        return self

    def limit(self, *_a, **_kw):
        return self

    def insert(self, row):
        self._pending_insert = row
        return self

    def execute(self):
        if self._pending_insert is not None:
            self.inserted.append(self._pending_insert)
            self._pending_insert = None
        return SimpleNamespace(data=[])


def test_profile_row_is_flattened_on_the_way_in():
    out = _promote_voice_config(
        "agent_profiles",
        {"agent_kind": "orchard", "slug": "orchard_default", "voice_config": NESTED},
    )

    assert out["voice_config"] == FLATTENED
    assert "live" not in out["voice_config"]
    assert out["slug"] == "orchard_default", "unrelated columns must ride through"


def test_a_restored_profile_row_lands_flat():
    """The wiring: `_restore_table` is what the CLI actually calls."""
    client = _Recorder()
    written = _restore_table(
        client, "agent_profiles",
        [{"agent_kind": "orchard", "slug": "orchard_default", "environment": "dev",
          "voice_config": NESTED}],
    )

    assert written == 1
    assert client.inserted[0]["voice_config"] == FLATTENED


def test_a_restored_version_snapshot_lands_flat():
    """The half prod actually reads: `_profile_from_snapshot` resolves
    voice_config from `agent_profile_versions.snapshot`, not the live row."""
    client = _Recorder()
    _restore_table(
        client, "agent_profile_versions",
        [{"version_id": "v-1", "snapshot": {"agent_kind": "orchard",
                                            "voice_config": NESTED}}],
    )

    snapshot = client.inserted[0]["snapshot"]
    assert snapshot["voice_config"] == FLATTENED
    assert snapshot["agent_kind"] == "orchard", "the rest of the snapshot is untouched"


def test_a_pre_duplex_turn_based_row_is_archived_not_reimported_flat():
    """The `live`-less shape statements 2b / 4b handle. Landing it flat resolves
    duplex-ON with no model_id, which closes every connect CLOSE_FORBIDDEN."""
    stored = {"enabled": True, "voice_id": "ar-SA-HamedNeural", "stt_provider": "azure"}
    out = _promote_voice_config("agent_profiles", {"voice_config": stored})

    assert out["voice_config"] == {"_retired_turn_based": stored}


def test_rows_without_voice_config_are_returned_untouched():
    row = {"python_name": "list_bookings", "is_deprecated": False}
    assert _promote_voice_config("agent_tools", row) is row
    assert _promote_voice_config("agent_profiles", row) is row
    assert _promote_voice_config("agent_profile_versions", {"version_id": "v"}) == {
        "version_id": "v"
    }


def test_a_malformed_voice_config_is_left_alone_not_rewritten():
    """`promote_legacy_voice_config` would raise on a non-dict. A restore is not
    the place to silently repair (or drop) a malformed column."""
    row = {"slug": "x", "voice_config": "not-a-dict"}
    assert _promote_voice_config("agent_profiles", row)["voice_config"] == "not-a-dict"
