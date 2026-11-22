import pytest

from admin_mcp import _common
from admin_mcp.context import MCP_ACTOR
from tests.admin_mcp.conftest import FakeClient


def test_validate_env():
    assert _common.validate_env("staging") == "staging"
    assert _common.validate_env("prod") == "prod"
    with pytest.raises(ValueError):
        _common.validate_env("production")


def test_use_env_pins_admin_env():
    from core.auth import get_admin_env
    assert get_admin_env() is None
    with _common.use_env("prod"):
        assert get_admin_env() == "prod"
    assert get_admin_env() is None


def test_use_env_resets_admin_env_on_exception():
    from core.auth import get_admin_env
    assert get_admin_env() is None
    with pytest.raises(RuntimeError):
        with _common.use_env("prod"):
            assert get_admin_env() == "prod"
            raise RuntimeError("boom")
    assert get_admin_env() is None


def test_client_for_env_falls_back_when_cross_env_not_configured(monkeypatch):
    import core.auth as auth

    sentinel = object()

    def raise_not_configured(env):
        raise auth.CrossEnvNotConfiguredError("not configured")

    monkeypatch.setattr(auth, "get_service_client_for_env", raise_not_configured)
    monkeypatch.setattr(auth, "get_service_client", lambda: sentinel)

    assert _common.client_for_env("staging") is sentinel


def test_client_for_env_uses_cross_env_client_when_available(monkeypatch):
    import core.auth as auth

    sentinel = object()

    monkeypatch.setattr(auth, "get_service_client_for_env", lambda env: sentinel)
    monkeypatch.setattr(
        auth, "get_service_client",
        lambda: (_ for _ in ()).throw(AssertionError("fallback should not be used")),
    )

    assert _common.client_for_env("staging") is sentinel


def test_actor_audit_uses_context_actor(monkeypatch):
    calls = {}
    monkeypatch.setattr(_common, "record_change",
                        lambda **kw: calls.update(kw))
    token = MCP_ACTOR.set({"user_id": "u1", "email": "a@waj.test",
                           "user_token": "t"})
    try:
        _common.actor_audit("agent_models", "m1", "update",
                            before={"a": 1}, after={"a": 2})
    finally:
        MCP_ACTOR.reset(token)
    assert calls["actor_user_id"] == "u1"
    assert calls["entity_type"] == "agent_models"
    assert calls["action"] == "update"


def test_not_confirmed_shape():
    out = _common.not_confirmed("delete model m1")
    assert out["confirmed"] is False
    assert "confirm" in out["hint"]


def test_err_shape():
    out = _common.err("boom", hint="try x", code=422)
    assert out == {"error": "boom", "hint": "try x", "code": 422}


def test_tool_errors_converts_value_error_to_err_dict():
    @_common.tool_errors
    def raises_on_bad_input(x: str) -> dict:
        if x == "bad":
            raise ValueError("x is bad")
        return {"ok": x}

    out = raises_on_bad_input("bad")
    assert out["error"] == "x is bad"


def test_tool_errors_passes_through_on_success():
    @_common.tool_errors
    def identity(x: str) -> dict:
        return {"ok": x}

    assert identity("fine") == {"ok": "fine"}


def test_tool_errors_does_not_swallow_other_exceptions():
    @_common.tool_errors
    def raises_type_error() -> None:
        raise TypeError("not a value error")

    with pytest.raises(TypeError):
        raises_type_error()


async def test_tool_errors_async_passes_through_on_success():
    @_common.tool_errors
    async def identity(x: str) -> dict:
        return {"ok": x}

    import inspect
    assert inspect.iscoroutinefunction(identity)
    assert await identity("fine") == {"ok": "fine"}


async def test_tool_errors_async_converts_value_error_to_err_dict():
    @_common.tool_errors
    async def raises_on_bad_input(x: str) -> dict:
        if x == "bad":
            raise ValueError("x is bad")
        return {"ok": x}

    out = await raises_on_bad_input("bad")
    assert out["error"] == "x is bad"


async def test_tool_errors_async_does_not_swallow_other_exceptions():
    @_common.tool_errors
    async def raises_type_error() -> None:
        raise TypeError("not a value error")

    with pytest.raises(TypeError):
        await raises_type_error()


def test_tool_errors_preserves_name_and_signature_via_functools_wraps():
    import inspect

    def sample(env: str = "staging", profile_id: str = "") -> dict:
        """Sample docstring."""
        return {"env": env, "profile_id": profile_id}

    wrapped = _common.tool_errors(sample)

    assert wrapped.__name__ == "sample"
    assert wrapped.__doc__ == "Sample docstring."
    assert wrapped.__wrapped__ is sample

    sig = inspect.signature(wrapped)
    assert list(sig.parameters) == ["env", "profile_id"]
    assert sig.parameters["env"].default == "staging"


def _seed():
    return FakeClient({"widgets": [
        {"id": "1", "name": "alpha", "status": "active"},
        {"id": "2", "name": "beta", "status": "archived"},
        {"id": "3", "name": "gamma", "status": "active"},
    ]})


def test_fake_table_eq_filter_narrows_reads():
    client = _seed()
    result = client.table("widgets").select("*").eq("status", "active").execute()
    assert {row["id"] for row in result.data} == {"1", "3"}


def test_fake_table_in_filter_narrows_reads():
    client = _seed()
    result = client.table("widgets").select("*").in_("id", ["1", "2"]).execute()
    assert {row["id"] for row in result.data} == {"1", "2"}


def test_fake_table_insert_appears_in_subsequent_reads():
    client = _seed()
    table = client.table("widgets")
    inserted = table.insert({"name": "delta"}).execute().data
    assert table.inserted == [{"name": "delta"}]
    assert inserted[0]["name"] == "delta"
    assert "id" in inserted[0]

    result = table.select("*").execute()
    assert len(result.data) == 4
    assert any(row["name"] == "delta" for row in result.data)


def test_fake_table_update_merges_onto_matching_row():
    client = _seed()
    table = client.table("widgets")
    result = table.update({"status": "archived"}).eq("id", "1").execute()
    assert result.data == [{"id": "1", "name": "alpha", "status": "archived"}]
    assert table.updated == [{"status": "archived"}]

    # Only the matching row changed; the others are untouched.
    remaining = table.select("*").eq("id", "2").execute().data
    assert remaining == [{"id": "2", "name": "beta", "status": "archived"}]
    remaining = table.select("*").eq("id", "3").execute().data
    assert remaining == [{"id": "3", "name": "gamma", "status": "active"}]


def test_fake_table_delete_removes_the_row():
    client = _seed()
    table = client.table("widgets")
    deleted = table.delete().eq("id", "2").execute().data
    assert deleted == [{"id": "2", "name": "beta", "status": "archived"}]
    assert table.deleted == deleted

    remaining = table.select("*").execute().data
    assert {row["id"] for row in remaining} == {"1", "3"}
