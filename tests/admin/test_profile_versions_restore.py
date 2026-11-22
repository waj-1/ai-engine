# ai_agent/tests/admin/test_profile_versions_restore.py
"""restore_version(): copies a version's config back onto the live draft
row, filtering id-references that no longer exist in this env (and never
failing over it) — the data-integrity-sensitive half of the profile-version
lifecycle. See admin/services/profile_versions.py's module-level
_IDENTITY_FIELDS / _CONFIG_FIELDS / _REF_CATEGORIES comments for the exact
_SHALLOW_FIELDS split this exercises."""
from unittest.mock import patch, MagicMock

from admin.services import profile_versions as pv

# PK column per reference table, used only to shape the mocked existence-
# probe rows realistically. Verified against
# supabase/migrations/20260603120000_baseline_agent_schema.sql — note
# agent_data_sources' PK is source_id, not data_source_id.
_PK_BY_TABLE = {
    "agent_tools": "tool_id",
    "agent_data_sources": "source_id",
    "agent_mcp_servers": "mcp_server_id",
    "agent_models": "model_id",
    "agent_prompt_versions": "prompt_version_id",
}


def _restore_client(snapshot, existing, *, profile_id="p1", version_id="v1",
                     environment="staging", agent_kind="sales"):
    """MagicMock Supabase client wired for restore_version:

    - `agent_profile_versions` .select(...).eq("version_id", ...).limit(1)
      .execute() returns the version row `{version_id, profile_id,
      environment, snapshot}`.
    - `agent_profiles` .select(...).eq("profile_id", ...).limit(1).execute()
      returns the live profile row (just enough for the agent_kind lookup);
      `.update(payload)...execute()` records `payload` onto
      `client._last_profile_update` instead of touching a real table.
    - Every other `agent_*` table named in `existing` answers
      `.select(...).in_(...).execute()` with the surviving ids from
      `existing[table]`, shaped as `[{pk: id}, ...]`.
    """
    client = MagicMock()
    client._last_profile_update = None

    def _update(payload):
        client._last_profile_update = payload
        result = MagicMock()
        result.eq.return_value.execute.return_value.data = [{}]
        return result

    def _table(name):
        m = MagicMock()
        if name == "agent_profile_versions":
            m.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"version_id": version_id, "profile_id": profile_id,
                 "environment": environment, "snapshot": snapshot}
            ]
        elif name == "agent_profiles":
            m.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"profile_id": profile_id, "agent_kind": agent_kind}
            ]
            m.update.side_effect = _update
        elif name in existing:
            pk = _PK_BY_TABLE[name]
            rows = [{pk: i} for i in existing[name]]
            m.select.return_value.in_.return_value.execute.return_value.data = rows
        return m

    client.table.side_effect = _table
    return client


def test_restore_copies_config_and_drops_missing_refs():
    snapshot = {"model_main_id": "m1", "prompt_version_ids": {"system": "pv1"},
                "tool_ids": ["t1", "t2"], "data_source_ids": ["d1_keep", "d2_gone"],
                "mcp_server_ids": ["mc1"], "memory_config": {"enabled": True},
                "prompt_contents": {"system": "hello"},
                "agent_kind": "sales", "slug": "default"}  # identity — must be skipped
    # existence probes: t2 and d2_gone deleted since; m1, t1, mc1, pv1, d1_keep still exist
    existing = {"agent_tools": ["t1"], "agent_data_sources": ["d1_keep"],
                "agent_mcp_servers": ["mc1"], "agent_models": ["m1"],
                "agent_prompt_versions": ["pv1"]}
    client = _restore_client(snapshot, existing)
    with patch("admin.services.profile_versions.admin_service_client", return_value=client), \
         patch("admin.services.profile_versions.AgentProfileLoader.invalidate"):
        out = pv.restore_version("p1", "staging", "v1", actor_user_id="u1")
    # tool_ids restored to only the surviving t1; t2 dropped
    upd = client._last_profile_update
    assert upd["tool_ids"] == ["t1"]
    # data_source_ids restored to only the surviving d1_keep; d2_gone dropped
    # (this assertion catches if the column probe name is mutated to the wrong column)
    assert upd["data_source_ids"] == ["d1_keep"]
    assert "agent_kind" not in upd and "slug" not in upd   # identity skipped
    assert "display_name" not in upd and "description" not in upd
    assert out["dropped"]["tools"] == ["t2"]
    assert out["dropped"]["data_sources"] == ["d2_gone"]
    # surviving refs are kept, not dropped
    assert out["dropped"]["mcp_servers"] == []
    assert out["dropped"]["model"] is False
    assert out["dropped"]["prompts"] == []
    assert upd["model_main_id"] == "m1"
    assert upd["prompt_version_ids"] == {"system": "pv1"}
    # config fields present in the snapshot copy straight through
    assert upd["memory_config"] == {"enabled": True}


def test_restore_recreates_deleted_prompt_version():
    snapshot = {"prompt_version_ids": {"system": "gonepv"},
                "prompt_contents": {"system": "restored text"},
                "tool_ids": [], "data_source_ids": [], "mcp_server_ids": [],
                "model_main_id": "m1", "agent_kind": "sales", "slug": "default"}
    existing = {"agent_prompt_versions": [], "agent_tools": [], "agent_data_sources": [],
                "agent_mcp_servers": [], "agent_models": ["m1"]}
    client = _restore_client(snapshot, existing)
    with patch("admin.services.profile_versions.admin_service_client", return_value=client), \
         patch("admin.services.profile_versions._recreate_prompt_version",
               return_value="newpv") as rec, \
         patch("admin.services.profile_versions.AgentProfileLoader.invalidate"):
        out = pv.restore_version("p1", "staging", "v1", actor_user_id="u1")
    rec.assert_called_once()  # prompt content re-created because the version was gone
    assert client._last_profile_update["prompt_version_ids"]["system"] == "newpv"
    # the slug is still reported dropped: its *original* version is gone,
    # even though the content survived under a freshly minted version id.
    assert out["dropped"]["prompts"] == ["system"]


def test_restore_missing_model_keeps_live_model_and_warns():
    snapshot = {"model_main_id": "ghost-model", "prompt_version_ids": {},
                "tool_ids": [], "data_source_ids": [], "mcp_server_ids": [],
                "agent_kind": "sales", "slug": "default"}
    # ghost-model no longer exists in agent_models
    existing = {"agent_models": [], "agent_tools": [], "agent_data_sources": [],
                "agent_mcp_servers": [], "agent_prompt_versions": []}
    client = _restore_client(snapshot, existing)
    with patch("admin.services.profile_versions.admin_service_client", return_value=client), \
         patch("admin.services.profile_versions.AgentProfileLoader.invalidate"):
        out = pv.restore_version("p1", "staging", "v1", actor_user_id="u1")
    # model_main_id must NOT appear in the update payload — the live row's
    # current model is left untouched, never nulled.
    assert "model_main_id" not in client._last_profile_update
    assert out["dropped"]["model"] is True


def test_restore_rejects_version_of_another_profile():
    snapshot = {"agent_kind": "sales", "slug": "default"}
    client = _restore_client(snapshot, {}, profile_id="OTHER_PROFILE")
    with patch("admin.services.profile_versions.admin_service_client", return_value=client):
        try:
            pv.restore_version("p1", "staging", "v1", actor_user_id="u1")
            assert False, "expected ValueError"
        except ValueError as e:
            assert "does not belong" in str(e)
    # nothing should have been written
    assert client._last_profile_update is None


def test_restore_missing_version_raises():
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    with patch("admin.services.profile_versions.admin_service_client", return_value=client):
        try:
            pv.restore_version("p1", "staging", "does-not-exist", actor_user_id="u1")
            assert False, "expected ValueError"
        except ValueError as e:
            assert "not found" in str(e)


def test_recreate_prompt_version_finds_prompt_and_inserts_next_number():
    """Unpatched unit test of the helper itself: resolves (agent_kind, slug)
    -> prompt_id, computes next version_number, inserts, returns the new id."""
    client = MagicMock()
    tables = {}  # memoize so repeated client.table(name) calls share state

    def _table(name):
        if name in tables:
            return tables[name]
        m = MagicMock()
        if name == "agent_prompts":
            m.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"prompt_id": "prompt-1"}
            ]
        elif name == "agent_prompt_versions":
            m.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
                {"version_number": 3}
            ]
            m.insert.return_value.execute.return_value.data = [
                {"prompt_version_id": "new-pv-id"}
            ]
        tables[name] = m
        return m

    client.table.side_effect = _table
    new_id = pv._recreate_prompt_version(client, "sales", "system", "hello again")
    assert new_id == "new-pv-id"
    inserted_payload = tables["agent_prompt_versions"].insert.call_args.args[0]
    assert inserted_payload["prompt_id"] == "prompt-1"
    assert inserted_payload["version_number"] == 4
    assert inserted_payload["content"] == "hello again"
