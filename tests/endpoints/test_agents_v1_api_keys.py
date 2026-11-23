"""`POST /v1/agents/{slug}/chat` with a portal-issued API key in place of the
superuser token: which keys are let in, who the turn runs as, and what a
refused caller is told. The token path's own tests are test_agents_v1.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from admin.services import api_keys as service
from core import api_keys as keys
from tests.api_keys_fake import install
from tests.scoped_build import full_build

ACTOR = "00000000-0000-0000-0000-000000000001"
URL = "/v1/agents/helpdesk/chat"


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
    """Flag on, a key store, a stub agent and run_turn. The superuser check
    records every token it is handed and lets none in."""
    import main

    store = install(monkeypatch)
    seen = {"built": [], "tokens": [], "identities": []}
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    full_build(monkeypatch)

    def _resolve(token):
        seen["tokens"].append(token)
        raise HTTPException(status_code=401, detail="Invalid token")

    def _create(slug, **kw):
        seen["built"].append(slug)
        return _StubAgent()

    async def _run_turn(*, agent, message, identity, profile, extra_context=None):
        seen["identities"].append(identity)
        yield {"type": "answer", "content": "Hi!"}
        yield {"type": "done"}

    monkeypatch.setattr("admin.auth.resolve_superuser", _resolve)
    monkeypatch.setattr("runtime.agents.custom.create_custom_agent", _create)
    monkeypatch.setattr("common.chat_session.run_turn", _run_turn)
    return TestClient(main.app), store, seen


def _post(client, key, **kw):
    return client.post(URL, headers={"Authorization": f"Bearer {key}"}, json={"message": "hello"}, **kw)


def _events(text):
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


@pytest.mark.parametrize("scopes", [["chat"], ["admin"], ["chat", "admin"]])
def test_a_chat_or_admin_key_is_accepted_in_place_of_the_token(wired, scopes):
    client, store, seen = wired
    row, key = service.create_key("Website widget", scopes, created_by=ACTOR)
    r = _post(client, key)
    assert r.status_code == 200
    assert [e["type"] for e in _events(r.text)] == ["answer", "done"]
    assert seen["built"] == ["helpdesk"]
    # A key never goes to the token verifier (whose errors and logs are not
    # written with keys in mind).
    assert seen["tokens"] == []


def test_the_turn_runs_as_the_key_so_two_keys_have_separate_sessions(wired):
    """Sessions are scoped by user_id (core/chat_persistence.py). Each key is
    its own user: neither can read or continue the other's conversations, and
    neither is the person who created them."""
    client, store, seen = wired
    first, first_key = service.create_key("Widget", ["chat"], created_by=ACTOR)
    second, second_key = service.create_key("Batch job", ["chat"], created_by=ACTOR)
    for key in (first_key, second_key, first_key):
        assert _post(client, key).status_code == 200
    users = [i.user_id for i in seen["identities"]]
    assert users == [first["id"], second["id"], first["id"]]  # stable per key
    assert first["id"] != second["id"] and ACTOR not in users
    assert all(i.authenticated and i.source == "api" for i in seen["identities"])


def test_bad_keys_are_all_refused_the_same_way(wired):
    client, store, seen = wired
    revoked_row, revoked = service.create_key("Old", ["chat"], created_by=ACTOR)
    service.revoke_key(revoked_row["id"], revoked_by=ACTOR)
    expired_row, expired = service.create_key("Short", ["chat"], created_by=ACTOR)
    store.row(expired_row["id"])["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    unknown = keys.generate_key()
    known_prefix = revoked[:12] + "B" * 35  # a real prefix, the wrong key
    answers = {(r.status_code, r.text) for r in (
        _post(client, k) for k in (revoked, expired, unknown, known_prefix, "wae_nope"))}
    assert len(answers) == 1  # byte-for-byte the same response for every one
    ((status, body),) = answers
    assert status == 401 and "Invalid API key" in body
    assert seen["built"] == [] and seen["tokens"] == []


def test_a_key_stops_working_the_moment_it_is_revoked(wired):
    client, store, seen = wired
    row, key = service.create_key("Widget", ["chat"], created_by=ACTOR)
    assert _post(client, key).status_code == 200
    service.revoke_key(row["id"], revoked_by=ACTOR)
    assert _post(client, key).status_code == 401
    assert seen["built"] == ["helpdesk"]  # only the first call built an agent


def test_a_response_never_echoes_the_key_or_its_digest(wired):
    client, store, seen = wired
    row, key = service.create_key("Widget", ["chat"], created_by=ACTOR)
    ok = _post(client, key)
    service.revoke_key(row["id"], revoked_by=ACTOR)
    refused = _post(client, key)
    for r in (ok, refused):
        blob = r.text + repr(dict(r.headers))
        assert key not in blob and keys.hash_key(key) not in blob and key[:12] not in blob


def test_the_flag_still_gates_the_route_before_any_key_lookup(wired, monkeypatch):
    client, store, seen = wired
    _, key = service.create_key("Widget", ["chat"], created_by=ACTOR)
    store.queries.clear()
    monkeypatch.delenv("ENABLE_GENERIC_AGENTS")
    assert _post(client, key).status_code == 404
    assert store.queries == []


def test_key_store_outage_is_a_503_not_a_way_in(wired):
    client, store, seen = wired
    _, key = service.create_key("Widget", ["chat"], created_by=ACTOR)
    store.fail = RuntimeError("database is down")
    r = _post(client, key)
    assert r.status_code == 503 and seen["built"] == []


def test_guessing_keys_runs_into_the_routes_existing_per_ip_limit(wired):
    """No limiter of its own: failed attempts spend the same per-IP budget as
    every other request to the engine (main.security_and_rate_limit_middleware)."""
    import main

    client, store, seen = wired
    statuses = [_post(client, keys.generate_key()).status_code
                for _ in range(main.RATE_LIMIT_REQUESTS + 1)]
    assert set(statuses[:-1]) == {401} and statuses[-1] == 429


def test_only_an_admin_key_may_send_operator_overrides(wired):
    """`X-WAJ-*-Override` / `X-WAJ-Profile-Id` are operator powers
    (main._request_is_operator). A chat key can talk to an agent; it cannot
    choose the model or prompt that answers."""
    import main

    client, store, seen = wired
    _, chat_key = service.create_key("Widget", ["chat"], created_by=ACTOR)
    admin_row, admin_key = service.create_key("Ops", ["admin"], created_by=ACTOR)

    def _req(key):
        return SimpleNamespace(headers={"Authorization": f"Bearer {key}"}, cookies={})

    assert main._request_is_operator(_req(admin_key)) is True
    assert main._request_is_operator(_req(chat_key)) is False
    assert main._request_is_operator(_req(keys.generate_key())) is False
    service.revoke_key(admin_row["id"], revoked_by=ACTOR)
    assert main._request_is_operator(_req(admin_key)) is False
    assert seen["tokens"] == []


def test_a_whole_request_logs_no_key_material(wired, caplog):
    """End to end through the app's middleware and error handlers, not just the
    key module: neither an accepted nor a refused key reaches a log line."""
    import logging

    client, store, seen = wired
    row, key = service.create_key("Widget", ["chat"], created_by=ACTOR)
    unknown = keys.generate_key()
    with caplog.at_level(logging.DEBUG):
        assert _post(client, key).status_code == 200
        assert _post(client, unknown).status_code == 401
        service.revoke_key(row["id"], revoked_by=ACTOR)
        assert _post(client, key).status_code == 401
    logged = caplog.text + "".join(repr(r.args) for r in caplog.records)
    assert "wae_" not in logged
    for secret in (key[4:], unknown[4:], key[4:12], keys.hash_key(key), keys.hash_key(unknown)):
        assert secret not in logged
