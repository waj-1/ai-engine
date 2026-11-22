# ai_agent/tests/admin/test_restore_route.py
"""POST /admin/agents/{profile_id}/versions/{version_id}/restore (Task 2.2).

The route is a thin delegation to
`admin.services.profile_versions.restore_version` (already unit-tested in
`test_profile_versions_restore.py`) — these tests only exercise the HTTP
wiring: the right service function gets called with the right args, the
response redirects with the `edited_from` + `dropped_*` query markers the
template reads (see `agents.html`'s version-bar block), and a `ValueError`
(missing version / ownership mismatch) redirects with `?error=` instead of
raising, matching the pin-version route's convention.
"""
from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from tests.admin.conftest import client  # noqa: F401 — existing superuser TestClient fixture


def test_restore_route_redirects_with_notice(client):  # noqa: F811
    with patch(
        "admin.services.profile_versions.restore_version",
        return_value={
            "restored": {},
            "dropped": {
                "tools": ["t2"], "data_sources": [], "mcp_servers": [],
                "model": False, "prompts": [],
            },
        },
    ):
        r = client.post("/admin/agents/p1/versions/v1/restore", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "restored" in r.headers["location"].lower() or "edited_from" in r.headers["location"]


def test_restore_route_delegates_with_right_args(client):  # noqa: F811
    with patch(
        "admin.services.profile_versions.restore_version",
        return_value={
            "restored": {},
            "dropped": {
                "tools": [], "data_sources": [], "mcp_servers": [],
                "model": False, "prompts": [],
            },
        },
    ) as restore:
        r = client.post("/admin/agents/p1/versions/v1/restore", follow_redirects=False)
    assert r.status_code == 303
    restore.assert_called_once_with(
        "p1", "staging", "v1", actor_user_id="00000000-0000-0000-0000-000000000001",
    )


def test_restore_route_redirect_carries_edited_from_and_zero_dropped(client):  # noqa: F811
    with patch(
        "admin.services.profile_versions.restore_version",
        return_value={
            "restored": {},
            "dropped": {
                "tools": [], "data_sources": [], "mcp_servers": [],
                "model": False, "prompts": [],
            },
        },
    ):
        r = client.post("/admin/agents/p1/versions/v1/restore", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    parsed = urlparse(location)
    assert parsed.path == "/admin/agents"
    qs = parse_qs(parsed.query)
    assert qs["profile_id"] == ["p1"]
    assert qs["edited_from"] == ["v1"]
    assert qs["dropped_tools"] == ["0"]
    assert qs["dropped_kb"] == ["0"]
    assert qs["dropped_mcp"] == ["0"]
    assert qs["dropped_model"] == ["0"]
    assert qs["dropped_prompts"] == ["0"]


def test_restore_route_redirect_carries_nonzero_dropped_counts(client):  # noqa: F811
    with patch(
        "admin.services.profile_versions.restore_version",
        return_value={
            "restored": {},
            "dropped": {
                "tools": ["t1", "t2"],
                "data_sources": ["d1"],
                "mcp_servers": [],
                "model": True,
                "prompts": ["system"],
            },
        },
    ):
        r = client.post("/admin/agents/p1/versions/v1/restore", follow_redirects=False)
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert qs["dropped_tools"] == ["2"]
    assert qs["dropped_kb"] == ["1"]
    assert qs["dropped_mcp"] == ["0"]
    assert qs["dropped_model"] == ["1"]
    assert qs["dropped_prompts"] == ["1"]


def test_restore_route_value_error_redirects_with_error_marker(client):  # noqa: F811
    """A mismatched version_id (restore_version's ownership guard) must
    surface as a redirect with `?error=`, not a 500 — same convention as
    the pin-version route (`test_pin_version_route_value_error_redirects_with_error_marker`)."""
    with patch(
        "admin.services.profile_versions.restore_version",
        side_effect=ValueError("profile version v9 does not belong to profile p1"),
    ):
        r = client.post("/admin/agents/p1/versions/v9/restore", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("/admin/agents?profile_id=p1&error=")
    assert "does+not+belong" in location or "does%20not%20belong" in location
