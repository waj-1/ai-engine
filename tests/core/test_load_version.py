from unittest.mock import MagicMock, patch

from core.agent_profile import AgentProfileLoader

SNAP = {"agent_kind": "sales", "slug": "s1", "model_main": {"model_string": "m"},
        "prompt_contents": {"system": "hi"}, "tool_python_names": [],
        "data_source_ids": [], "mcp_server_ids": [], "mcp_attachments": []}


def test_load_version_builds_profile_from_snapshot():
    fake = MagicMock()
    # version row lookup → snapshot + profile_id
    ver_exec = MagicMock()
    ver_exec.data = [{"version_id": "v1", "profile_id": "p1",
                       "environment": "staging", "snapshot": SNAP}]
    prof_exec = MagicMock()
    prof_exec.data = [{"profile_id": "p1", "agent_kind": "sales", "slug": "s1"}]

    def table(name):
        t = MagicMock()
        t.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
            ver_exec if name == "agent_profile_versions" else prof_exec)
        return t

    fake.table.side_effect = table
    with patch("runtime.compiler.get_service_client", return_value=fake), \
         patch("core.agent_profile.get_service_client", return_value=fake):
        prof = AgentProfileLoader.load_version("v1", "staging")
    assert prof.agent_kind == "sales"
    assert prof.model_main == {"model_string": "m"}
    assert prof.resolved_prompts == {"system": "hi"}
    assert prof.is_fallback is False
