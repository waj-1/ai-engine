from unittest.mock import patch, MagicMock
from core.agent_profile import AgentProfileLoader

SNAP = {"agent_kind": "sales", "slug": "default", "model_main": {"model_string": "m"},
        "prompt_contents": {"system": "hi"}, "tool_python_names": [],
        "data_source_ids": [], "mcp_server_ids": [], "mcp_attachments": []}


def test_served_pin_serves_that_version():
    """A row with served_version_id set → _build_profile returns that snapshot,
    regardless of environment, without touching the live/prod path."""
    row = {"profile_id": "p1", "agent_kind": "sales", "slug": "default",
           "served_version_id": "ver-7"}
    client = MagicMock()
    with patch("runtime.compiler.load_snapshot_by_version",
               return_value={"profile_id": "p1", "environment": "staging", "snapshot": SNAP}) as lsv:
        prof = AgentProfileLoader._build_profile(client, row, "staging")
    lsv.assert_called_once_with("ver-7")
    assert prof.agent_kind == "sales"
    assert prof.model_main == {"model_string": "m"}


def test_null_pin_uses_default_path():
    """served_version_id absent/None → the pin branch is skipped entirely
    (load_snapshot_by_version never called); default resolution runs."""
    row = {"profile_id": "p1", "agent_kind": "sales", "slug": "default",
           "environment": "staging", "served_version_id": None,
           "model_main_id": None, "tool_ids": [], "data_source_ids": [],
           "mcp_server_ids": [], "prompt_version_ids": {}}
    client = MagicMock()
    # staging → live-row path; stub the model fetch + tool lookup to empties
    client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []
    with patch("runtime.compiler.load_snapshot_by_version") as lsv, \
         patch.object(AgentProfileLoader, "_fetch_model", return_value=None):
        prof = AgentProfileLoader._build_profile(client, row, "staging")
    lsv.assert_not_called()
    assert prof.agent_kind == "sales"


def test_pinned_version_belonging_to_other_profile_falls_through():
    """A served_version_id that resolves but belongs to a DIFFERENT profile
    (manual DB edit / future setter bug) must NOT be served — falls through
    to the default resolution path, never raises."""
    row = {"profile_id": "p1", "agent_kind": "sales", "slug": "default",
           "environment": "staging", "served_version_id": "ver-9",
           "model_main_id": None, "tool_ids": [], "data_source_ids": [],
           "mcp_server_ids": [], "prompt_version_ids": {}}
    client = MagicMock()
    client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []
    other_profile_snapshot = {"profile_id": "p2", "environment": "staging", "snapshot": SNAP}
    with patch("runtime.compiler.load_snapshot_by_version",
               return_value=other_profile_snapshot) as lsv, \
         patch.object(AgentProfileLoader, "_fetch_model", return_value=None):
        prof = AgentProfileLoader._build_profile(client, row, "staging")
    lsv.assert_called_once_with("ver-9")
    assert prof.agent_kind == "sales"  # built via default path, not the mismatched snapshot
    assert prof.profile_id == "p1"


def test_missing_pinned_version_falls_through():
    """A dangling served_version_id (version pruned) → warn + fall through to
    default, never raise."""
    row = {"profile_id": "p1", "agent_kind": "sales", "slug": "default",
           "environment": "staging", "served_version_id": "gone",
           "model_main_id": None, "tool_ids": [], "data_source_ids": [],
           "mcp_server_ids": [], "prompt_version_ids": {}}
    client = MagicMock()
    client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []
    with patch("runtime.compiler.load_snapshot_by_version", return_value=None), \
         patch.object(AgentProfileLoader, "_fetch_model", return_value=None):
        prof = AgentProfileLoader._build_profile(client, row, "staging")
    assert prof.agent_kind == "sales"  # fell through, no raise
