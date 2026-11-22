"""Tests for the Task 4.2 `bakeoffs` MCP tool (create/list/get) —
admin_mcp.tools_test.bakeoffs delegating to admin.services.bakeoffs."""
from __future__ import annotations

from unittest.mock import patch

import admin_mcp.tools_test as tt

BAKEOFF_ROW = {"bakeoff_id": "b1", "name": "bo", "profile_id": "p1",
              "variants": [{"label": "A"}, {"label": "B"}],
              "suite_ids": ["s1"], "status": "complete"}


def _noaudit(monkeypatch):
    monkeypatch.setattr(tt, "actor_audit", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_bakeoffs_create_delegates():
    """Brief's own binding test (verbatim shape): create delegates to
    admin.services.bakeoffs.create_bakeoff with the current actor's
    user_id/user_token, inside use_env, and returns bakeoff_id/run_ids."""
    with patch("admin.services.bakeoffs.create_bakeoff",
               return_value={"bakeoff_id": "b1", "run_ids": ["r1"]}), \
         patch("admin_mcp.tools_test.current_actor",
               return_value={"user_id": "u1", "user_token": "jwt"}), \
         patch("admin_mcp.tools_test.actor_audit"), \
         patch("admin_mcp.tools_test.use_env"):
        out = tt.bakeoffs(action="create", name="bo", profile_id="p1",
                          variants=[{"label": "A", "profile_version_id": "v1"},
                                    {"label": "B", "profile_version_id": "v2"}],
                          suite_ids=["s1"], env="staging")
    assert out["bakeoff_id"] == "b1"
    assert out["run_ids"] == ["r1"]


def test_bakeoffs_create_passes_actor_creds_and_env(monkeypatch):
    _noaudit(monkeypatch)
    monkeypatch.setattr(tt, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt-token"})
    seen_env = {}

    class _FakeUseEnv:
        def __init__(self, env):
            seen_env["env"] = env

        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tt, "use_env", _FakeUseEnv)

    seen = {}

    def fake_create_bakeoff(*, name, profile_id, variants, suite_ids, repeats,
                            actor_user_id, user_token):
        seen.update(name=name, profile_id=profile_id, variants=variants,
                    suite_ids=suite_ids, repeats=repeats,
                    actor_user_id=actor_user_id, user_token=user_token)
        return {"bakeoff_id": "b1", "run_ids": ["r1", "r2"]}

    import admin.services.bakeoffs as svc
    monkeypatch.setattr(svc, "create_bakeoff", fake_create_bakeoff)

    out = tt.bakeoffs(action="create", name="bo", profile_id="p1",
                      variants=[{"label": "A"}, {"label": "B"}],
                      suite_ids=["s1", "s2"], repeats=3, env="prod")

    assert out == {"bakeoff_id": "b1", "run_ids": ["r1", "r2"]}
    assert seen["actor_user_id"] == "u1"
    assert seen["user_token"] == "jwt-token"
    assert seen["profile_id"] == "p1"
    assert seen["suite_ids"] == ["s1", "s2"]
    assert seen["repeats"] == 3
    assert seen_env["env"] == "prod"


def test_bakeoffs_create_audits_after_success(monkeypatch):
    monkeypatch.setattr(tt, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt"})
    monkeypatch.setattr(tt, "use_env", lambda env: _NullCtx())

    import admin.services.bakeoffs as svc
    monkeypatch.setattr(svc, "create_bakeoff",
                        lambda **kw: {"bakeoff_id": "b1", "run_ids": ["r1"]})

    audits = []
    monkeypatch.setattr(tt, "actor_audit",
                        lambda *a, **k: audits.append((a, k)))

    tt.bakeoffs(action="create", name="bo", profile_id="p1",
               variants=[{"label": "A"}, {"label": "B"}], suite_ids=["s1"])

    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[:3] == ("agent_bakeoffs", "b1", "create")
    assert kwargs["after"] == {"name": "bo", "n_variants": 2, "n_suites": 1}


def test_bakeoffs_create_value_error_becomes_err(monkeypatch):
    """create_bakeoff's pre-flight/label ValueErrors surface as err(), not
    a raised exception — via the @tool_errors decorator."""
    monkeypatch.setattr(tt, "current_actor",
                        lambda: {"user_id": "u1", "user_token": "jwt"})
    monkeypatch.setattr(tt, "use_env", lambda env: _NullCtx())

    import admin.services.bakeoffs as svc

    def boom(**kw):
        raise ValueError("comparison runs require a model judge on every suite")

    monkeypatch.setattr(svc, "create_bakeoff", boom)

    out = tt.bakeoffs(action="create", name="bo", profile_id="p1",
                      variants=[{"label": "A"}, {"label": "B"}],
                      suite_ids=["s1"])
    assert "error" in out
    assert "judge" in out["error"]


def test_bakeoffs_create_requires_name():
    out = tt.bakeoffs(action="create", profile_id="p1",
                      variants=[{"label": "A"}, {"label": "B"}],
                      suite_ids=["s1"])
    assert "error" in out
    assert "name" in out["error"]


def test_bakeoffs_create_requires_profile_id():
    out = tt.bakeoffs(action="create", name="bo",
                      variants=[{"label": "A"}, {"label": "B"}],
                      suite_ids=["s1"])
    assert "error" in out
    assert "profile_id" in out["error"]


def test_bakeoffs_create_requires_variants():
    out = tt.bakeoffs(action="create", name="bo", profile_id="p1",
                      suite_ids=["s1"])
    assert "error" in out
    assert "variants" in out["error"]


def test_bakeoffs_create_requires_suite_ids():
    out = tt.bakeoffs(action="create", name="bo", profile_id="p1",
                      variants=[{"label": "A"}, {"label": "B"}])
    assert "error" in out
    assert "suite_ids" in out["error"]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_bakeoffs_list_delegates(monkeypatch):
    monkeypatch.setattr(tt, "use_env", lambda env: _NullCtx())

    import admin.services.bakeoffs as svc
    seen = {}

    def fake_list_bakeoffs(limit=25):
        seen["limit"] = limit
        return [dict(BAKEOFF_ROW)]

    monkeypatch.setattr(svc, "list_bakeoffs", fake_list_bakeoffs)

    out = tt.bakeoffs(action="list", limit=10, env="prod")
    assert out["bakeoffs"][0]["bakeoff_id"] == "b1"
    assert seen["limit"] == 10


def test_bakeoffs_list_uses_env(monkeypatch):
    seen_env = {}

    class _FakeUseEnv:
        def __init__(self, env):
            seen_env["env"] = env

        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tt, "use_env", _FakeUseEnv)

    import admin.services.bakeoffs as svc
    monkeypatch.setattr(svc, "list_bakeoffs", lambda limit=25: [])

    tt.bakeoffs(action="list", env="prod")
    assert seen_env["env"] == "prod"


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_bakeoffs_get_delegates_with_suite_focus(monkeypatch):
    monkeypatch.setattr(tt, "use_env", lambda env: _NullCtx())

    import admin.services.bakeoffs as svc
    seen = {}

    def fake_get_bakeoff(bakeoff_id, suite_focus=None):
        seen["bakeoff_id"] = bakeoff_id
        seen["suite_focus"] = suite_focus
        return {"bakeoff": dict(BAKEOFF_ROW), "matrix": {"rows": []},
               "status": "complete"}

    monkeypatch.setattr(svc, "get_bakeoff", fake_get_bakeoff)

    out = tt.bakeoffs(action="get", bakeoff_id="b1", suite_id="s1")
    assert out["status"] == "complete"
    assert seen["bakeoff_id"] == "b1"
    assert seen["suite_focus"] == "s1"  # suite_id passed through as suite_focus


def test_bakeoffs_get_no_suite_id_passes_none(monkeypatch):
    monkeypatch.setattr(tt, "use_env", lambda env: _NullCtx())

    import admin.services.bakeoffs as svc
    seen = {}

    def fake_get_bakeoff(bakeoff_id, suite_focus=None):
        seen["suite_focus"] = suite_focus
        return {"bakeoff": dict(BAKEOFF_ROW), "matrix": {}, "status": "running"}

    monkeypatch.setattr(svc, "get_bakeoff", fake_get_bakeoff)

    tt.bakeoffs(action="get", bakeoff_id="b1")
    assert seen["suite_focus"] is None


def test_bakeoffs_get_not_found_becomes_err(monkeypatch):
    """get_bakeoff raises ValueError for a missing id; @tool_errors turns
    that into the standard err() payload."""
    monkeypatch.setattr(tt, "use_env", lambda env: _NullCtx())

    import admin.services.bakeoffs as svc

    def fake_get_bakeoff(bakeoff_id, suite_focus=None):
        raise ValueError(f"bake-off {bakeoff_id} not found")

    monkeypatch.setattr(svc, "get_bakeoff", fake_get_bakeoff)

    out = tt.bakeoffs(action="get", bakeoff_id="does-not-exist")
    assert "error" in out
    assert "not found" in out["error"]


# ---------------------------------------------------------------------------
# unknown action
# ---------------------------------------------------------------------------


def test_bakeoffs_unknown_action():
    out = tt.bakeoffs(action="bogus")
    assert "error" in out


class _NullCtx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
