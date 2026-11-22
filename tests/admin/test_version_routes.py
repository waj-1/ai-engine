# ai_agent/tests/admin/test_version_routes.py
"""Routes for the profile version lifecycle (Task 1.3):

  POST /admin/agents/{profile_id}/versions        — save a checkpoint
  POST /admin/agents/{profile_id}/pin-version      — pin/clear the served version
  GET  /admin/agents/{profile_id}/versions/{version_id}/diff — version vs draft

Route bodies are thin delegations to admin.services.profile_versions
(already unit-tested in test_profile_versions_save.py /
test_profile_versions_pin.py) plus the existing compute_profile_diff
builder — these tests only exercise the HTTP wiring: the right service
function gets called with the right args, and the response redirects
(save/pin) or renders (diff)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.admin.conftest import client  # noqa: F401 — existing superuser TestClient fixture


def test_save_version_route(client):  # noqa: F811
    with patch(
        "admin.services.profile_versions.save_version",
        return_value={"version_id": "v3", "version_number": 3},
    ) as save:
        r = client.post(
            "/admin/agents/p1/versions", data={"notes": "checkpoint"},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/admin/agents?profile_id=p1"
    save.assert_called_once()
    args, kwargs = save.call_args
    assert args[0] == "p1"
    assert kwargs["actor_user_id"] == "00000000-0000-0000-0000-000000000001"
    assert kwargs["notes"] == "checkpoint"


def test_save_version_route_blank_notes_becomes_none(client):  # noqa: F811
    """notes="" (the Form default) must reach save_version as None, not ''."""
    with patch(
        "admin.services.profile_versions.save_version",
        return_value={"version_id": "v1", "version_number": 1},
    ) as save:
        r = client.post("/admin/agents/p1/versions", data={}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert save.call_args.kwargs["notes"] is None


def test_pin_version_route(client):  # noqa: F811
    with patch(
        "admin.services.profile_versions.pin_served_version",
        return_value={"served_version_id": "v2"},
    ) as pin:
        r = client.post(
            "/admin/agents/p1/pin-version", data={"version_id": "v2"},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/admin/agents?profile_id=p1"
    pin.assert_called_once_with(
        "p1", "staging", "v2", actor_user_id="00000000-0000-0000-0000-000000000001",
    )


def test_pin_version_route_empty_clears(client):  # noqa: F811
    """An empty version_id form field means 'clear the pin' — must reach
    pin_served_version as None, not ''."""
    with patch(
        "admin.services.profile_versions.pin_served_version",
        return_value={"served_version_id": None},
    ) as pin:
        r = client.post(
            "/admin/agents/p1/pin-version", data={"version_id": ""},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303)
    pin.assert_called_once_with(
        "p1", "staging", None, actor_user_id="00000000-0000-0000-0000-000000000001",
    )


def test_pin_version_route_value_error_redirects_with_error_marker(client):  # noqa: F811
    """A mismatched version_id (pin_served_version's ownership guard) must
    surface as a redirect with `?error=`, not a 500 — same convention as
    profile_mcp_attach_handlers._err / profile_skill_attach_handlers._err."""
    with patch(
        "admin.services.profile_versions.pin_served_version",
        side_effect=ValueError("profile version v9 does not belong to profile p1"),
    ):
        r = client.post(
            "/admin/agents/p1/pin-version", data={"version_id": "v9"},
            follow_redirects=False,
        )
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("/admin/agents?profile_id=p1&error=")
    assert "does+not+belong" in location or "does%20not%20belong" in location


def test_version_diff_route_returns_200(client):  # noqa: F811
    """The diff route loads the version snapshot + the live draft, shapes
    the snapshot into an agent_profiles-row-like dict, and renders
    agents_diff.html via the existing compute_profile_diff builder."""
    fake_entry = {
        "profile_id": "p1",
        "environment": "staging",
        "snapshot": {
            "agent_kind": "sales",
            "slug": "sales",
            "display_name": "Sales (saved v2)",
            "description": "old description",
            "model_main_id": "m-old",
            "tool_ids": [],
            "data_source_ids": [],
            "mcp_server_ids": [],
            "downstream_profile_ids": [],
        },
    }
    fake_live_row = {
        "profile_id": "p1",
        "agent_kind": "sales",
        "slug": "sales",
        "display_name": "Sales (live)",
        "description": "new description",
        "model_main_id": "m-new",
        "environment": "staging",
        "status": "active",
        "tool_ids": [],
        "data_source_ids": [],
        "mcp_server_ids": [],
        "downstream_profile_ids": [],
    }
    fake_diff = {
        "scalars": [
            {"field": "display_name", "a": "Sales (saved v2)",
             "b": "Sales (live)", "equal": False},
        ],
        "tools": {"added_in_b": [], "removed_in_b": [], "shared": []},
        "data_sources": {"added_in_b": [], "removed_in_b": [], "shared": []},
        "mcp_servers": {"added_in_b": [], "removed_in_b": [], "shared": []},
        "sub_agents": {"added_in_b": [], "removed_in_b": [], "shared": []},
        "prompts": {"kind_mismatch": False, "shared": []},
    }

    agent_profiles_builder = MagicMock()
    agent_profiles_builder.select.return_value = agent_profiles_builder
    agent_profiles_builder.eq.return_value = agent_profiles_builder
    agent_profiles_builder.limit.return_value = agent_profiles_builder
    agent_profiles_builder.execute.return_value.data = [fake_live_row]

    fake_client = MagicMock()
    fake_client.table.return_value = agent_profiles_builder

    with patch(
        "runtime.compiler.load_snapshot_by_version", return_value=fake_entry,
    ), patch(
        "admin.router.admin_service_client", return_value=fake_client,
    ), patch(
        "admin.profile_fork_handlers.compute_profile_diff", return_value=fake_diff,
    ):
        r = client.get("/admin/agents/p1/versions/v2/diff")

    assert r.status_code == 200
    assert b"Sales (saved v2)" in r.content or b"Sales (live)" in r.content


def _make_diff_client(live_row):
    """Fake supabase client good enough to drive the REAL compute_profile_diff:

    - `agent_profiles` table calls return `live_row` — covers both the
      route's own live-row fetch and compute_profile_diff's downstream-
      profile label lookup.
    - Every other table (`agent_tools`, `agent_data_sources`,
      `agent_mcp_servers`, `agent_prompts`, `agent_prompt_versions`) returns
      no rows — fine here since the test's tool/data-source/mcp/downstream
      id lists are empty (so _resolve_labels short-circuits) and the two
      profiles share an agent_kind with no prompt rows to enumerate.
    """
    def _builder(rows):
        b = MagicMock()
        b.select.return_value = b
        b.eq.return_value = b
        b.limit.return_value = b
        b.order.return_value = b
        b.in_.return_value = b
        b.execute.return_value.data = rows
        return b

    agent_profiles_builder = _builder([live_row])
    empty_builder = _builder([])

    fake_client = MagicMock()
    fake_client.table.side_effect = (
        lambda name: agent_profiles_builder if name == "agent_profiles" else empty_builder
    )
    return fake_client


def test_version_diff_route_no_phantom_drift_on_non_snapshot_fields(client):  # noqa: F811
    """Regression test for the version-diff phantom-drift bug: `status`,
    `parent_profile_id`, and `model_light_id` are SCALAR_FIELDS the
    snapshot never carries (see runtime.compiler._SHALLOW_FIELDS), so a
    realistic snapshot omits them while the live row always has them. The
    route must splice the live values into profile_a so these come back
    `equal=True` from the REAL compute_profile_diff — not a mock — while a
    genuine config difference (display_name here) still surfaces.
    """
    fake_entry = {
        "profile_id": "p1",
        "environment": "staging",
        "snapshot": {
            "agent_kind": "sales",
            "slug": "sales",
            "display_name": "Sales (saved v2)",
            "description": "same description",
            "model_main_id": "m1",
            "endpoint_path": "/sales",
            "tool_ids": [],
            "data_source_ids": [],
            "mcp_server_ids": [],
            "downstream_profile_ids": [],
            # Deliberately no status / parent_profile_id / model_light_id —
            # that's what a real version snapshot looks like.
        },
    }
    fake_live_row = {
        "profile_id": "p1",
        "agent_kind": "sales",
        "slug": "sales",
        "display_name": "Sales (live)",
        "description": "same description",
        "model_main_id": "m1",
        "endpoint_path": "/sales",
        "environment": "staging",
        "status": "active",
        "parent_profile_id": "parent-999",
        "model_light_id": "light-model-1",
        "tool_ids": [],
        "data_source_ids": [],
        "mcp_server_ids": [],
        "downstream_profile_ids": [],
    }

    fake_client = _make_diff_client(fake_live_row)

    from admin.profile_fork_handlers import compute_profile_diff as real_compute_profile_diff

    captured: dict = {}

    def _spy(client_, profile_a, profile_b):
        result = real_compute_profile_diff(client_, profile_a, profile_b)
        captured["diff"] = result
        return result

    with patch(
        "runtime.compiler.load_snapshot_by_version", return_value=fake_entry,
    ), patch(
        "admin.router.admin_service_client", return_value=fake_client,
    ), patch(
        "admin.profile_fork_handlers.compute_profile_diff", side_effect=_spy,
    ):
        r = client.get("/admin/agents/p1/versions/v2/diff")

    assert r.status_code == 200
    scalars_by_field = {s["field"]: s for s in captured["diff"]["scalars"]}

    for field in ("status", "parent_profile_id", "model_light_id"):
        row = scalars_by_field[field]
        assert row["equal"] is True, f"phantom drift on non-snapshot field {field!r}: {row}"
        assert row["a"] == row["b"]

    # A genuine config difference must still surface correctly — the fix
    # must not mask real drift, only the bookkeeping fields above.
    assert scalars_by_field["display_name"]["equal"] is False


def test_version_diff_route_404_when_version_belongs_to_other_profile(client):  # noqa: F811
    fake_entry = {
        "profile_id": "OTHER_PROFILE",
        "environment": "staging",
        "snapshot": {"agent_kind": "sales"},
    }
    with patch(
        "runtime.compiler.load_snapshot_by_version", return_value=fake_entry,
    ):
        r = client.get("/admin/agents/p1/versions/v2/diff")
    assert r.status_code == 404


def test_version_diff_route_404_when_version_missing(client):  # noqa: F811
    with patch("runtime.compiler.load_snapshot_by_version", return_value=None):
        r = client.get("/admin/agents/p1/versions/v2/diff")
    assert r.status_code == 404
