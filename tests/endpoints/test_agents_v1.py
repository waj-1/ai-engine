"""endpoints/agents_v1.py — `POST /v1/agents/{slug}/chat`: the off-by-default
flag, the superuser boundary, slug resolution and the deployment-scope gate."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.agent_profile import MissingProfileError, MissingSnapshotError
from endpoints import agents_v1
from tests.scoped_build import full_build, use_agents_demo

SESSION = "0b5c1c2e-8f0a-4a8e-9d55-3d2a6f4f9a10"
AUTH = {"Authorization": "Bearer good-token"}


@pytest.fixture(autouse=True)
def _isolate_global_ip_limiter():
    import main

    main.rate_limit_store.clear()
    yield
    main.rate_limit_store.clear()


class _StubAgent:
    profile = SimpleNamespace(profile_id="stub", model_main={}, memory_config={})


@pytest.fixture
def wired(monkeypatch):
    """Flag on, a superuser token that resolves, a stub agent and run_turn."""
    import main

    seen = {"built": [], "tokens": []}
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    full_build(monkeypatch)

    def _resolve(token):
        seen["tokens"].append(token)
        if token != "good-token":
            raise HTTPException(status_code=403, detail="Not a superuser")
        return {"user_token": token, "user_id": "u-1", "email": "ops@example.com"}

    def _create(slug, **kw):
        seen["built"].append((slug, kw))
        return _StubAgent()

    async def _run_turn(*, agent, message, identity, profile, extra_context=None):
        seen.update(message=message, identity=identity, extra_context=extra_context)
        yield {"type": "answer", "content": "Hi!"}
        yield {"type": "done"}

    monkeypatch.setattr("admin.auth.resolve_superuser", _resolve)
    monkeypatch.setattr("runtime.agents.custom.create_custom_agent", _create)
    monkeypatch.setattr("common.chat_session.run_turn", _run_turn)
    return TestClient(main.app), seen


def _events(text):
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


def test_streams_a_turn_for_the_agent_named_by_slug(wired):
    client, seen = wired
    r = client.post("/v1/agents/helpdesk/chat", headers=AUTH,
                    json={"message": "hello", "session_id": SESSION, "locale": "ar-SA"})
    assert r.status_code == 200
    assert [e["type"] for e in _events(r.text)] == ["answer", "done"]
    assert seen["built"] == [("helpdesk", {"locale": "ar-SA"})]
    identity = seen["identity"]
    assert identity.agent_kind == "custom" and identity.user_id == "u-1"
    assert identity.session_id == SESSION and identity.source == "api"
    assert identity.session_metadata == {"agent_slug": "helpdesk"}
    assert seen["message"] == "hello"
    # The caller reaches the agent, so it can read ITS memories about them.
    assert seen["extra_context"] == {"user_id": "u-1"}


def test_session_id_and_locale_are_optional(wired):
    client, seen = wired
    r = client.post("/v1/agents/helpdesk/chat", headers=AUTH, json={"message": "hello"})
    assert r.status_code == 200
    assert seen["identity"].session_id is None
    assert seen["built"] == [("helpdesk", {"locale": None})]


def test_off_by_default(wired, monkeypatch):
    """Merging this route must change nothing on a running stack: with the
    flag unset it is a plain 404, before auth or any profile lookup."""
    client, seen = wired
    monkeypatch.delenv("ENABLE_GENERIC_AGENTS")
    r = client.post("/v1/agents/helpdesk/chat", headers=AUTH, json={"message": "hello"})
    assert r.status_code == 404
    assert seen["tokens"] == [] and seen["built"] == []


@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({"Authorization": "Basic abc"}, 401),
    ({"Authorization": "Bearer someone-elses-login"}, 403),
])
def test_requires_a_superuser_token(wired, headers, status):
    client, seen = wired
    r = client.post("/v1/agents/helpdesk/chat", headers=headers, json={"message": "hello"})
    assert r.status_code == status
    assert seen["built"] == []  # no agent is built for a caller that is not let in


@pytest.mark.parametrize("exc", [MissingProfileError("custom/nope"), MissingSnapshotError("unpublished")])
def test_unknown_or_unpublished_agent_is_404(wired, monkeypatch, exc):
    client, _ = wired

    def _create(slug, **kw):
        raise exc

    monkeypatch.setattr("runtime.agents.custom.create_custom_agent", _create)
    r = client.post("/v1/agents/nope/chat", headers=AUTH, json={"message": "hello"})
    assert r.status_code == 404 and "Agent not found" in r.text


def test_build_failure_is_503_without_leaking_the_error(wired, monkeypatch):
    client, _ = wired

    def _create(slug, **kw):
        raise RuntimeError("postgres://secret@host")

    monkeypatch.setattr("runtime.agents.custom.create_custom_agent", _create)
    r = client.post("/v1/agents/helpdesk/chat", headers=AUTH, json={"message": "hello"})
    assert r.status_code == 503 and "secret" not in r.text


@pytest.mark.parametrize("slug", ["Helpdesk", "help desk", "-lead", "a" * 65, "x.y"])
def test_malformed_slug_is_404_before_any_lookup(wired, slug):
    client, seen = wired
    r = client.post(f"/v1/agents/{slug}/chat", headers=AUTH, json={"message": "hello"})
    assert r.status_code == 404 and seen["built"] == []


@pytest.mark.parametrize("body", [
    {"message": ""},
    {"message": "x" * 8001},
    {"message": "x", "session_id": "not-a-uuid"},
    {"message": "x", "locale": "not a locale"},
    {},
])
def test_rejects_bad_input(wired, body):
    client, _ = wired
    assert client.post("/v1/agents/helpdesk/chat", headers=AUTH, json=body).status_code == 422


def test_channel_allow_list_applies_to_the_custom_kind(wired, monkeypatch):
    client, seen = wired

    def _resolve_channel(request):
        request.state.channel = {"slug": "partner-site", "allowed_agent_kinds": ["sales"]}

    monkeypatch.setattr(agents_v1, "resolve_channel", _resolve_channel)
    r = client.post("/v1/agents/helpdesk/chat", headers=AUTH, json={"message": "hello"})
    assert r.status_code == 403 and seen["built"] == []


def test_scoped_builds_404_the_generic_api(wired, monkeypatch):
    """A single-tenant box serves its client's agents only, flag or no flag."""
    client, seen = wired
    use_agents_demo(monkeypatch)
    r = client.post("/v1/agents/helpdesk/chat", headers=AUTH, json={"message": "hello"})
    assert r.status_code == 404 and r.json() == {"detail": "Not found"}  # the gate's own body
    assert seen["tokens"] == []
