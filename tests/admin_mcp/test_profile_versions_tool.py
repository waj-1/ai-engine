"""Tests for the Task 3.1 `profile_versions` MCP tool
(admin_mcp.tools_configure.profile_versions) — list/view/save/restore/pin,
delegating to admin.services.profile_versions. Direct-call style (no
`.fn` unwrap), matching the eval-work MCP tests."""
from __future__ import annotations

from unittest.mock import patch

from admin_mcp import tools_configure as tc


class _NullCtx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def test_profile_versions_save_delegates():
    """Brief's own binding test (verbatim shape): save delegates to
    admin.services.profile_versions.save_version and returns its result."""
    with patch("admin.services.profile_versions.save_version",
               return_value={"version_id": "v3", "version_number": 3}), \
         patch("admin_mcp.tools_configure.current_actor",
               return_value={"user_id": "u1", "user_token": "jwt"}), \
         patch("admin_mcp.tools_configure.actor_audit"), \
         patch("admin_mcp.tools_configure.use_env"):
        out = tc.profile_versions(action="save", profile_id="p1", env="staging")
    assert out["version_id"] == "v3"
    assert out["version_number"] == 3


def test_profile_versions_save_passes_actor_and_notes(monkeypatch):
    monkeypatch.setattr(tc, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt"})
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import admin.services.profile_versions as svc
    seen = {}

    def fake_save_version(profile_id, environment, *, actor_user_id, notes=None):
        seen.update(profile_id=profile_id, environment=environment,
                    actor_user_id=actor_user_id, notes=notes)
        return {"version_id": "v9", "version_number": 9}

    monkeypatch.setattr(svc, "save_version", fake_save_version)

    out = tc.profile_versions(action="save", profile_id="p1", notes="checkpoint",
                              env="prod")
    assert out == {"version_id": "v9", "version_number": 9}
    assert seen["profile_id"] == "p1"
    assert seen["environment"] == "prod"
    assert seen["actor_user_id"] == "u1"
    assert seen["notes"] == "checkpoint"


def test_profile_versions_save_value_error_becomes_err(monkeypatch):
    monkeypatch.setattr(tc, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt"})
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import admin.services.profile_versions as svc

    def boom(profile_id, environment, *, actor_user_id, notes=None):
        raise ValueError(f"profile not found: {profile_id}")

    monkeypatch.setattr(svc, "save_version", boom)

    out = tc.profile_versions(action="save", profile_id="does-not-exist")
    assert "error" in out
    assert "not found" in out["error"]


# ---------------------------------------------------------------------------
# pin
# ---------------------------------------------------------------------------


def test_profile_versions_pin_delegates():
    """Brief's own binding test (verbatim shape): pin delegates to
    admin.services.profile_versions.pin_served_version and returns its
    result."""
    with patch("admin.services.profile_versions.pin_served_version",
               return_value={"served_version_id": "v2"}), \
         patch("admin_mcp.tools_configure.current_actor",
               return_value={"user_id": "u1", "user_token": "jwt"}), \
         patch("admin_mcp.tools_configure.actor_audit"), \
         patch("admin_mcp.tools_configure.use_env"):
        out = tc.profile_versions(action="pin", profile_id="p1", version_id="v2",
                                  env="staging")
    assert out["served_version_id"] == "v2"


def test_profile_versions_pin_clears_with_empty_version_id(monkeypatch):
    monkeypatch.setattr(tc, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt"})
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import admin.services.profile_versions as svc
    seen = {}

    def fake_pin(profile_id, environment, version_id, *, actor_user_id):
        seen["version_id"] = version_id
        return {"served_version_id": version_id}

    monkeypatch.setattr(svc, "pin_served_version", fake_pin)

    out = tc.profile_versions(action="pin", profile_id="p1", version_id="")
    assert seen["version_id"] is None
    assert out == {"served_version_id": None}


def test_profile_versions_pin_value_error_becomes_err(monkeypatch):
    monkeypatch.setattr(tc, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt"})
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import admin.services.profile_versions as svc

    def boom(profile_id, environment, version_id, *, actor_user_id):
        raise ValueError(f"profile version {version_id} does not belong to "
                         f"profile {profile_id}")

    monkeypatch.setattr(svc, "pin_served_version", boom)

    out = tc.profile_versions(action="pin", profile_id="p1", version_id="v-other")
    assert "error" in out
    assert "does not belong" in out["error"]


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_profile_versions_restore_delegates(monkeypatch):
    monkeypatch.setattr(tc, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt"})
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import admin.services.profile_versions as svc
    seen = {}

    def fake_restore(profile_id, environment, version_id, *, actor_user_id):
        seen.update(profile_id=profile_id, environment=environment,
                    version_id=version_id, actor_user_id=actor_user_id)
        return {"restored": {"tool_ids": ["t1"]},
                "dropped": {"tools": ["t2"], "data_sources": [],
                            "mcp_servers": [], "model": False, "prompts": []}}

    monkeypatch.setattr(svc, "restore_version", fake_restore)

    out = tc.profile_versions(action="restore", profile_id="p1", version_id="v1",
                              env="staging")
    assert out["restored"] == {"tool_ids": ["t1"]}
    assert out["dropped"]["tools"] == ["t2"]
    assert seen["profile_id"] == "p1"
    assert seen["environment"] == "staging"
    assert seen["version_id"] == "v1"
    assert seen["actor_user_id"] == "u1"


def test_profile_versions_restore_requires_version_id():
    out = tc.profile_versions(action="restore", profile_id="p1")
    assert "error" in out
    assert "version_id" in out["error"]


def test_profile_versions_restore_value_error_becomes_err(monkeypatch):
    monkeypatch.setattr(tc, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt"})
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import admin.services.profile_versions as svc

    def boom(profile_id, environment, version_id, *, actor_user_id):
        raise ValueError(f"version not found: {version_id}")

    monkeypatch.setattr(svc, "restore_version", boom)

    out = tc.profile_versions(action="restore", profile_id="p1", version_id="ghost")
    assert "error" in out
    assert "not found" in out["error"]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_profile_versions_list_delegates(monkeypatch):
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import admin.services.profile_versions as svc
    seen = {}

    def fake_list_versions(profile_id, environment):
        seen.update(profile_id=profile_id, environment=environment)
        return [{"version_id": "v2", "version_number": 2, "promoted_at": None,
                 "notes": None, "is_served": True},
                {"version_id": "v1", "version_number": 1, "promoted_at": None,
                 "notes": None, "is_served": False}]

    monkeypatch.setattr(svc, "list_versions", fake_list_versions)

    out = tc.profile_versions(action="list", profile_id="p1", env="prod")
    assert out["profile_id"] == "p1"
    assert out["env"] == "prod"
    assert len(out["versions"]) == 2
    assert out["versions"][0]["is_served"] is True
    assert seen["profile_id"] == "p1"
    assert seen["environment"] == "prod"


def test_profile_versions_list_value_error_becomes_err(monkeypatch):
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import admin.services.profile_versions as svc

    def boom(profile_id, environment):
        raise ValueError(f"profile not found: {profile_id}")

    monkeypatch.setattr(svc, "list_versions", boom)

    out = tc.profile_versions(action="list", profile_id="does-not-exist")
    assert "error" in out
    assert "not found" in out["error"]


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


def test_profile_versions_view_returns_snapshot(monkeypatch):
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import runtime.compiler as compiler

    def fake_load(version_id):
        assert version_id == "v1"
        return {"profile_id": "p1", "environment": "staging",
                "snapshot": {"model_main_id": "m1"}}

    monkeypatch.setattr(compiler, "load_snapshot_by_version", fake_load)

    out = tc.profile_versions(action="view", profile_id="p1", version_id="v1")
    assert out["version_id"] == "v1"
    assert out["snapshot"] == {"model_main_id": "m1"}


def test_profile_versions_view_requires_version_id():
    out = tc.profile_versions(action="view", profile_id="p1")
    assert "error" in out
    assert "version_id" in out["error"]


def test_profile_versions_view_not_found(monkeypatch):
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import runtime.compiler as compiler
    monkeypatch.setattr(compiler, "load_snapshot_by_version", lambda version_id: None)

    out = tc.profile_versions(action="view", profile_id="p1", version_id="ghost")
    assert "error" in out
    assert "not found" in out["error"]


def test_profile_versions_view_rejects_mismatched_profile(monkeypatch):
    monkeypatch.setattr(tc, "use_env", lambda env: _NullCtx())

    import runtime.compiler as compiler

    def fake_load(version_id):
        return {"profile_id": "OTHER_PROFILE", "environment": "staging",
                "snapshot": {}}

    monkeypatch.setattr(compiler, "load_snapshot_by_version", fake_load)

    out = tc.profile_versions(action="view", profile_id="p1", version_id="v1")
    assert "error" in out
    assert "does not belong" in out["error"]


# ---------------------------------------------------------------------------
# unknown action
# ---------------------------------------------------------------------------


def test_profile_versions_unknown_action():
    out = tc.profile_versions(action="bogus", profile_id="p1")
    assert "error" in out
