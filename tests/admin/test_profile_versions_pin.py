# ai_agent/tests/admin/test_profile_versions_pin.py
"""pin_served_version()/list_versions(): the write path behind the agent
config screen's version bar — pin sets + invalidates the loader cache,
rejects a version_id belonging to a different profile, and list_versions
decorates is_served against the profile's current pin."""
from unittest.mock import patch, MagicMock

from admin.services import profile_versions as pv


def _client(profile_row, version_rows):
    profiles = MagicMock()
    profiles.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [profile_row]
    profiles.update.return_value.eq.return_value.execute.return_value.data = [profile_row]
    versions = MagicMock()
    # list_versions: .select(...).eq(profile_id).eq(environment).order(...).execute()
    versions.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value.data = version_rows
    # pin_served_version's ownership guard: .select(...).eq(version_id).eq(profile_id).limit(1).execute()
    versions.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [v for v in version_rows if v["version_id"] == "v2"])
    client = MagicMock()
    client.table.side_effect = lambda n: {"agent_profiles": profiles,
                                          "agent_profile_versions": versions}.get(n, MagicMock())
    return client


def test_pin_sets_served_version_and_invalidates():
    client = _client({"profile_id": "p1", "agent_kind": "sales"},
                     [{"version_id": "v2", "profile_id": "p1", "version_number": 2}])
    with patch("admin.services.profile_versions.admin_service_client", return_value=client), \
         patch("admin.services.profile_versions.AgentProfileLoader.invalidate") as inv:
        out = pv.pin_served_version("p1", "staging", "v2", actor_user_id="u1")
    assert out["served_version_id"] == "v2"
    inv.assert_called_once_with("sales", "staging")


def test_pin_rejects_version_of_another_profile():
    client = _client({"profile_id": "p1", "agent_kind": "sales"},
                     [])  # no v2 belonging to p1
    with patch("admin.services.profile_versions.admin_service_client", return_value=client):
        try:
            pv.pin_served_version("p1", "staging", "v2", actor_user_id="u1")
            assert False, "expected ValueError"
        except ValueError as e:
            assert "does not belong" in str(e)


def test_unpin_clears_served_version_and_invalidates():
    """version_id=None clears the pin (served_version_id -> None) rather
    than pinning a specific snapshot — AgentProfileLoader falls back to its
    default per-env resolution on the next request."""
    client = _client({"profile_id": "p1", "agent_kind": "sales", "served_version_id": "v2"},
                     [{"version_id": "v2", "profile_id": "p1", "version_number": 2}])
    with patch("admin.services.profile_versions.admin_service_client", return_value=client), \
         patch("admin.services.profile_versions.AgentProfileLoader.invalidate") as inv:
        out = pv.pin_served_version("p1", "staging", None, actor_user_id="u1")
    assert out == {"served_version_id": None}
    inv.assert_called_once_with("sales", "staging")


def test_list_versions_marks_served():
    client = _client({"profile_id": "p1", "agent_kind": "sales", "served_version_id": "v2"},
                     [{"version_id": "v2", "version_number": 2, "promoted_at": "t", "notes": None},
                      {"version_id": "v1", "version_number": 1, "promoted_at": "t0", "notes": "x"}])
    with patch("admin.services.profile_versions.admin_service_client", return_value=client):
        out = pv.list_versions("p1", "staging")
    served = {v["version_id"]: v["is_served"] for v in out}
    assert served == {"v2": True, "v1": False}
