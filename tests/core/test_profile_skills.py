"""Profile.skill_attachments wiring — live path, snapshot path, compiler freeze."""
from __future__ import annotations

from unittest.mock import patch

from core.agent_profile import AgentProfileLoader, Profile

_ENTRY = {"skill": {"skill_id": "s1", "slug": "refund-note", "always_on": False},
          "version": {"version_number": 2, "content": "body"},
          "resources": [], "linked_data_sources": [], "position": 0,
          "scope": "platform"}


def test_profile_dataclass_default():
    p = Profile(profile_id=None, agent_kind="support_bot", slug="x", environment="staging")
    assert p.skill_attachments == []


def test_profile_from_snapshot_reads_skill_attachments():
    row = {"profile_id": "p1", "agent_kind": "support_bot", "slug": "support_bot_default"}
    snapshot = {"agent_kind": "support_bot", "slug": "support_bot_default",
                "skill_attachments": [_ENTRY]}
    p = AgentProfileLoader._profile_from_snapshot(row, snapshot, "prod")
    assert p.skill_attachments == [_ENTRY]


def test_profile_from_snapshot_tolerates_legacy_snapshots():
    row = {"profile_id": "p1", "agent_kind": "support_bot", "slug": "support_bot_default"}
    p = AgentProfileLoader._profile_from_snapshot(row, {"agent_kind": "support_bot"}, "prod")
    assert p.skill_attachments == []


def test_compiler_freezes_skill_attachments():
    from runtime import compiler

    class _FakeExec:
        def __init__(self, data):
            self.data = data

    class _FakeQuery:
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
            self._insert_payload = payload
            _FakeClient.inserted.append(payload)
            return self

        def execute(self):
            return _FakeExec(self._data)

    class _FakeClient:
        inserted = []

        def table(self, name):
            if name == "agent_profiles":
                return _FakeQuery([{"profile_id": "p1", "agent_kind": "support_bot",
                                    "slug": "x", "model_main_id": None}])
            if name == "agent_profile_versions":
                return _FakeQuery([])
            return _FakeQuery([])

    with patch.object(compiler, "get_service_client", return_value=_FakeClient()), \
         patch("core.skills.fetch_skill_attachments", return_value=[_ENTRY]) as fsa:
        # insert returns no rows -> compile raises; we only assert the snapshot shape
        try:
            compiler.compile_profile_snapshot(profile_id="p1", environment="prod")
        except Exception:
            pass
        assert fsa.called
        assert _FakeClient.inserted, "no insert attempted"
        snap = _FakeClient.inserted[-1]["snapshot"]
        assert snap["_schema_version"] == 6
        assert snap["skill_attachments"] == [_ENTRY]
