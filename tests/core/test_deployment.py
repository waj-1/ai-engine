"""core/deployment.py: deployment scopes, with a stand-in provider (no plugin)."""
import pytest
from fastapi.testclient import TestClient

import core.deployment as deployment
from tests.scoped_build import make_scope, use_scope

ORCHARD = make_scope("orchard", extra_kinds=("orchard_outbound",))
OTHER = make_scope("other")


def test_full_build_when_no_provider_has_a_scope(monkeypatch):
    monkeypatch.setattr(deployment, "_providers", lambda: [])
    assert deployment.active_scope() is None
    assert deployment.scoped_build() is False
    assert deployment.all_scopes() == {}
    assert deployment.all_scope_kinds() == frozenset()
    assert deployment.mount_prefix() == ""
    assert deployment.single_env_deployment() is None
    assert deployment.serves_kind("anything") is True
    assert deployment.serves_agents_portal() is True
    assert deployment.scope_env_vars() == ()
    assert deployment.demo_tenant_build() is False


def test_scope_kinds_cover_main_sub_agents_and_extras():
    assert deployment.scope_kinds(ORCHARD) == frozenset(
        {"orchard", "orchard_title", "orchard_suggestions", "orchard_outbound"})


def test_active_scope_answers_for_the_scoped_build(monkeypatch):
    use_scope(monkeypatch, ORCHARD, others=[OTHER])
    assert deployment.active_scope() is ORCHARD
    assert deployment.scoped_build() is True
    assert deployment.mount_prefix() == "/orchard"
    assert deployment.serves_kind("orchard_outbound") is True
    assert deployment.serves_kind("other") is False
    assert deployment.serves_agents_portal() is False
    assert deployment.all_scope_kinds() == (
        deployment.scope_kinds(ORCHARD) | deployment.scope_kinds(OTHER))


def test_demo_tenant_build_follows_the_active_scope(monkeypatch):
    use_scope(monkeypatch, ORCHARD)
    assert deployment.demo_tenant_build() is False
    use_scope(monkeypatch, make_scope("orchard", demo_tenant=True))
    assert deployment.demo_tenant_build() is True
    use_scope(monkeypatch, None, others=[make_scope("orchard", demo_tenant=True)])
    assert deployment.demo_tenant_build() is False


def test_a_full_build_still_knows_every_scope(monkeypatch):
    use_scope(monkeypatch, None, others=[ORCHARD, OTHER])
    assert deployment.active_scope() is None
    assert set(deployment.all_scopes()) == {"orchard", "other"}


def test_an_unknown_scope_configuration_propagates(monkeypatch):
    class Broken:
        @staticmethod
        def active_scope():
            raise ValueError("unknown scope 'orchardd'")

    monkeypatch.setattr(deployment, "_providers", lambda: [Broken])
    with pytest.raises(ValueError, match="orchardd"):
        deployment.active_scope()


@pytest.mark.parametrize("env,expected", [
    ("production", "prod"), ("prod", "prod"), ("staging", "staging"),
    ("development", "staging"), ("", "staging"),
])
def test_single_env_deployment_on_a_scoped_build(monkeypatch, env, expected):
    use_scope(monkeypatch, ORCHARD)
    monkeypatch.setenv("ENVIRONMENT", env)
    assert deployment.single_env_deployment() == expected


def test_blocked_prefixes(monkeypatch):
    use_scope(monkeypatch, ORCHARD, others=[OTHER], blocked=("/product/",))
    blocked = deployment.blocked_path_prefixes(ORCHARD)
    assert "/v1/" in blocked            # the generic agent API is in no scope
    assert "/product/" in blocked       # the plugin's own routes
    assert "/other" in blocked and "/other/chat/stream" in blocked
    assert not any(p.startswith("/orchard") for p in blocked)


def test_deployment_gate_404s_what_the_scope_does_not_serve(monkeypatch):
    import main

    use_scope(monkeypatch, ORCHARD, others=[OTHER], blocked=("/product/",))
    client = TestClient(main.app)
    for path in ("/v1/agents/x/chat", "/other/chat/stream", "/product/thing"):
        r = client.post(path, json={})
        assert r.status_code == 404 and r.json() == {"detail": "Not found"}, path
    assert client.get("/health").status_code != 404


def test_deployment_gate_is_a_no_op_on_a_full_build(monkeypatch):
    import main

    use_scope(monkeypatch, None, others=[ORCHARD, OTHER], blocked=("/health",))
    assert TestClient(main.app).get("/health").status_code != 404


def test_boot_refuses_an_unknown_scope(monkeypatch):
    import main

    class Broken:
        @staticmethod
        def active_scope():
            raise ValueError("unknown scope 'orchardd'")

    monkeypatch.setattr(deployment, "_providers", lambda: [Broken])
    with pytest.raises(RuntimeError, match="orchardd"):
        main._validate_env_or_die({"ENVIRONMENT": "staging", "SUPABASE_URL": "x",
                                   "SUPABASE_ANON_KEY": "y"})
