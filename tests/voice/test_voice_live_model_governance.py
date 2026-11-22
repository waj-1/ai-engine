"""The realtime model row must be governed like the brain model.

Two gaps this pins:
  1. hydrate_live_model was called with no `environment`, so
     AgentProfileLoader.fetch_model skipped env-sibling resolution for the
     ears/mouth while the brain path passed it.
  2. model_id was frozen at promote but the ROW was read live, so endpoint /
     voice_type / turn_detection / api_version / auth_env_var bypassed
     promote and rollback entirely.
"""
import pytest

from voice.live.config import LiveVoiceConfig, hydrate_live_model


class _NoQueries:
    """A client whose first QUERY fails with the message we mean.

    Deliberately an object, not a function: `fetch_model` reaches
    `client.table("agent_models")`, so a bare `def _boom(*a, **k): raise
    AssertionError(...)` never ran its own body — a regression that started
    calling fetch_model surfaced as `AttributeError: 'function' object has no
    attribute 'table'`, which passes the `pytest.raises`-free assertion path but
    reports the wrong cause to whoever has to fix it.
    """

    _MSG = "fetch_model must not run when a frozen row exists"

    def table(self, *_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError(self._MSG)

    def __getattr__(self, name):  # pragma: no cover - must not be called
        raise AssertionError(f"{self._MSG} (touched client.{name})")


def test_prefers_the_promote_frozen_row_over_a_live_read():
    """A snapshot-served profile must hear the model row it was promoted with."""
    frozen = {
        "provider": "azure_voice_live",
        "model_string": "gpt-realtime",
        "extra_config": {"voice_type": "native", "realtime": True},
    }
    cfg = LiveVoiceConfig(enabled=True, model_id="m-1")
    out = hydrate_live_model(cfg, client=_NoQueries(), model_row=frozen)

    assert out.provider == "azure_voice_live"
    assert out.model_string == "gpt-realtime"
    assert out.extra_config["voice_type"] == "native"


def test_falls_back_to_a_live_read_when_no_frozen_row(monkeypatch):
    seen = {}

    class _Loader:
        @staticmethod
        def fetch_model(client, model_id, environment):
            seen["model_id"] = model_id
            seen["environment"] = environment
            return {"provider": "azure_voice_live", "model_string": "gpt-4.1-mini",
                    "extra_config": {}}

    monkeypatch.setattr("core.agent_profile.AgentProfileLoader", _Loader)
    cfg = LiveVoiceConfig(enabled=True, model_id="m-2")
    hydrate_live_model(cfg, client=object(), environment="prod")

    assert seen == {"model_id": "m-2", "environment": "prod"}


def test_missing_model_id_still_raises_rather_than_defaulting():
    """A tenant with duplex enabled and no realtime model is a misconfiguration
    we surface, never silently default."""
    with pytest.raises(LookupError):
        hydrate_live_model(LiveVoiceConfig(enabled=True), client=object())


# ---------------------------------------------------------------------------
# The freeze must survive an UNMIGRATED profile row.
#
# runtime/compiler.py read voice_config.model_id flat-only. A profile on a stack
# that has not run 20260817120000 still stores {"live": {...}}, so the promote
# froze voice_model=None -- and nothing broke, because hydrate_live_model then
# falls back to a live read. That is precisely the failure worth pinning: gap 2
# above silently stops applying to that promoted version, with no symptom.
# ---------------------------------------------------------------------------


def _fake_client(profile_row, model_row):
    """Minimal Supabase-shaped fake (same pattern as
    tests/core/test_profile_skills.py::test_compiler_freezes_skill_attachments):
    filters are ignored, each table just answers with its canned rows and the
    version insert is captured."""
    inserted = []

    class _Exec:
        def __init__(self, data):
            self.data = data

    class _Query:
        def __init__(self, data):
            self._data = data

        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def in_(self, *_a, **_k):
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def insert(self, payload):
            inserted.append(payload)
            return self

        def execute(self):
            return _Exec(self._data)

    class _Client:
        def table(self, name):
            if name == "agent_profiles":
                return _Query([profile_row])
            if name == "agent_models":
                return _Query([model_row])
            return _Query([])

    return _Client(), inserted


_REALTIME_ROW = {
    "model_id": "m-realtime",
    "provider": "azure_voice_live",
    "model_string": "gpt-realtime",
    "extra_config": {"realtime": True, "voice_type": "native",
                     "endpoint": "https://voice-live-example.example"},
}


def _snapshot_for(voice_config):
    from runtime import compiler

    profile_row = {"profile_id": "p1", "agent_kind": "sample_voice", "slug": "sample_default",
                   "model_main_id": None, "voice_config": voice_config}
    client, inserted = _fake_client(profile_row, _REALTIME_ROW)
    try:
        # The version insert returns no rows, so compile raises after building
        # the snapshot — the same trade test_profile_skills.py makes; the
        # snapshot payload is what we're asserting on.
        compiler.compile_profile_snapshot(profile_id="p1", environment="prod",
                                          source_client=client)
    except Exception:
        pass
    assert inserted, "no version insert attempted"
    return inserted[-1]["snapshot"]


def test_promote_freezes_the_realtime_row_from_an_unmigrated_nested_voice_config():
    snap = _snapshot_for({"live": {"enabled": True, "model_id": "m-realtime"}})

    assert snap["voice_model"] is not None, (
        "a nested (pre-20260817120000) voice_config must still freeze the realtime "
        "row — a None here silently un-governs endpoint/voice_type/turn_detection "
        "for this promoted version"
    )
    assert snap["voice_model"]["model_string"] == "gpt-realtime"
    assert snap["voice_model"]["extra_config"]["voice_type"] == "native"
    # The stored column is left verbatim on purpose: the loader's read shim
    # resolves a nested block, so promoting must not rewrite the tenant's row.
    assert snap["voice_config"] == {"live": {"enabled": True, "model_id": "m-realtime"}}


def test_promote_still_freezes_the_realtime_row_from_a_flat_voice_config():
    snap = _snapshot_for({"enabled": True, "model_id": "m-realtime"})

    assert snap["voice_model"]["model_string"] == "gpt-realtime"
