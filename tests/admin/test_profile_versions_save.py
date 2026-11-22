# ai_agent/tests/admin/test_profile_versions_save.py
"""save_version(): delegates the actual snapshot write to
runtime.compiler.compile_profile_snapshot (the same writer promote uses),
then reads version_number back from the inserted row — because
compile_profile_snapshot's real return value is a bare version_id str, not
{version_id, version_number}.

Also covers the FIX for the "save_version never invalidates" finding: in
production, `AgentProfileLoader` serves the HIGHEST `version_number` row
absent a `served_version_id` pin (`load_active_snapshot`,
runtime/compiler.py) — and this call just wrote a new highest one — so
save_version now calls `AgentProfileLoader.invalidate(agent_kind,
environment)` for deterministic pickup, consistent with promote."""
from unittest.mock import patch, MagicMock

from admin.services import profile_versions as pv


def _client(version_row, profile_row=None):
    versions = MagicMock()
    # save_version's version_number lookup:
    # .select(...).eq(version_id).limit(1).execute()
    versions.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        version_row
    ]
    profiles = MagicMock()
    # save_version's agent_kind lookup:
    # .select("agent_kind").eq(profile_id).limit(1).execute()
    profiles.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [profile_row] if profile_row else []
    )
    client = MagicMock()
    client.table.side_effect = lambda n: {
        "agent_profile_versions": versions,
        "agent_profiles": profiles,
    }.get(n, MagicMock())
    return client


def test_save_version_snapshots_draft():
    client = _client(
        {"version_id": "v3", "version_number": 3},
        profile_row={"agent_kind": "sales"},
    )
    with patch(
        "admin.services.profile_versions.admin_service_client", return_value=client
    ), patch(
        "admin.services.profile_versions.compile_profile_snapshot",
        return_value="v3",
    ) as comp, patch(
        "admin.services.profile_versions.AgentProfileLoader.invalidate"
    ) as inv:
        out = pv.save_version("p1", "staging", actor_user_id="u1", notes="tuned model")

    assert out == {"version_id": "v3", "version_number": 3}
    kw = comp.call_args.kwargs
    assert kw["profile_id"] == "p1"
    assert kw["environment"] == "staging"
    assert kw["notes"] == "tuned model"
    assert kw["promoted_by"] == "u1"
    # FIX: save_version now invalidates the profile-loader cache for this
    # profile's (agent_kind, environment) — see module docstring for why
    # this matters in prod even though the pin (served_version_id) is
    # untouched.
    inv.assert_called_once_with("sales", "staging")


def test_save_version_skips_invalidate_when_agent_kind_unresolvable():
    """If the profile row can't be re-fetched (deleted mid-flight / DB
    hiccup), save_version still returns the version info — it just skips
    the invalidate rather than crashing on a None agent_kind."""
    client = _client(
        {"version_id": "v4", "version_number": 4},
        profile_row=None,
    )
    with patch(
        "admin.services.profile_versions.admin_service_client", return_value=client
    ), patch(
        "admin.services.profile_versions.compile_profile_snapshot",
        return_value="v4",
    ), patch(
        "admin.services.profile_versions.AgentProfileLoader.invalidate"
    ) as inv:
        out = pv.save_version("p1", "prod", actor_user_id="u1")

    assert out == {"version_id": "v4", "version_number": 4}
    inv.assert_not_called()
