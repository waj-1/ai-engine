"""core/api_keys.py + admin/services/api_keys.py: what a key is, what is
stored, how one is checked, and what never leaks.

Each security property the module docstring claims has a test here:
storage (digest only), lookup (by digest, constant-time compare), one answer
for every bad key, no key material in logs or audit rows, and a
`last_used_at` write that stays off the request path.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from admin.services import api_keys as service
from core import api_keys as keys
from tests.api_keys_fake import install

ACTOR = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def store(monkeypatch):
    return install(monkeypatch)


def _status(exc_info) -> tuple:
    return exc_info.value.status_code, exc_info.value.detail


# --------------------------------------------------------------------------
# The key and what is stored
# --------------------------------------------------------------------------


def test_key_format_is_recognisable_and_high_entropy():
    made = {keys.generate_key() for _ in range(50)}
    assert len(made) == 50
    for key in made:
        assert key.startswith("wae_") and keys._KEY_RE.match(key)
        assert len(key) == 4 + 43  # 32 random bytes, url-safe, unpadded


def test_only_the_digest_and_a_prefix_are_stored(store):
    row, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    stored = store.rows[0]
    assert stored["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
    assert stored["key_prefix"] == key[:12] and row["key_prefix"] == key[:12]
    # The key itself is in no column of the stored row...
    assert all(key not in str(v) for v in stored.values())
    # ...and the prefix is not enough of it to matter (8 of 43 random characters).
    assert len(stored["key_prefix"]) - len("wae_") == 8


def test_what_create_and_list_return_never_includes_the_digest(store):
    row, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    digest = keys.hash_key(key)
    listed = service.list_keys()
    assert "key_hash" not in row and all("key_hash" not in r for r in listed)
    assert digest not in repr(row) and digest not in repr(listed) and key not in repr(listed)
    assert listed[0]["status"] == "active" and listed[0]["name"] == "Billing sync"
    # The management queries do not even ask the database for the column.
    selects = [q["columns"] for q in store.queries if q["op"] == "select"]
    assert selects and all(c != "*" and "key_hash" not in c for c in selects)


@pytest.mark.parametrize("name,scopes,message", [
    ("", ["chat"], "name"),
    ("   ", ["chat"], "name"),
    ("x" * 121, ["chat"], "120"),
    ("ok", [], "at least one scope"),
    ("ok", ["chat", "root"], "unknown scope"),
])
def test_create_refuses_bad_input(store, name, scopes, message):
    with pytest.raises(ValueError, match=message):
        service.create_key(name, scopes, created_by=ACTOR)
    assert store.rows == []


def test_create_refuses_an_expiry_in_the_past(store):
    with pytest.raises(ValueError, match="future"):
        service.create_key("ok", ["chat"], expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))


# --------------------------------------------------------------------------
# Checking a key
# --------------------------------------------------------------------------


def test_a_live_key_verifies_by_its_digest(store):
    row, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    store.queries.clear()
    found = keys.verify_key(key)
    assert found["id"] == row["id"]
    # The lookup filter is the digest: the key itself never goes to the database.
    (query,) = store.queries
    assert query["filters"] == [("eq", "key_hash", keys.hash_key(key))]
    assert key not in repr(query)


def test_every_bad_key_gets_the_same_answer(store):
    """Unknown, malformed, revoked and expired are indistinguishable: a caller
    cannot learn that a prefix exists or that a key used to work."""
    _, revoked = service.create_key("old", ["chat"], created_by=ACTOR)
    service.revoke_key(store.rows[0]["id"], revoked_by=ACTOR)
    _, expired = service.create_key("short", ["chat"], created_by=ACTOR,
                                    expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    store.rows[1]["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    same_prefix_wrong_tail = revoked[:12] + "A" * 35
    answers = set()
    for bad in (revoked, expired, keys.generate_key(), same_prefix_wrong_tail,
                "wae_short", "wae_" + "!" * 43, "wae_"):
        with pytest.raises(HTTPException) as info:
            keys.verify_key(bad)
        answers.add(_status(info))
    assert answers == {(401, "Invalid API key")}


def test_an_unreadable_expiry_is_treated_as_expired(store):
    _, key = service.create_key("odd", ["chat"], created_by=ACTOR)
    store.rows[0]["expires_at"] = "not-a-date"
    with pytest.raises(HTTPException) as info:
        keys.verify_key(key)
    assert info.value.status_code == 401


def test_a_malformed_key_never_reaches_the_database(store):
    for bad in ("wae_short", "wae_" + "a" * 500, "wae_" + "a" * 42 + " "):
        with pytest.raises(HTTPException):
            keys.verify_key(bad)
    assert store.queries == []


def test_the_digest_is_compared_in_constant_time(store, monkeypatch):
    """The row the database returns is compared again with hmac.compare_digest,
    and that comparison decides: a row whose digest does not match the presented
    key is refused even though the database handed it back."""
    _, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    calls = []
    real = keys.hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(keys.hmac, "compare_digest", _spy)
    keys.verify_key(key)
    assert calls == [(keys.hash_key(key).encode(), keys.hash_key(key).encode())]

    # A filter that returns the wrong row (a proxy bug, a widened query): the
    # comparison, not the filter, is what lets a caller in.
    other = keys.generate_key()
    monkeypatch.setattr(type(store.table("api_keys")), "_match", lambda self, row: True)
    with pytest.raises(HTTPException) as info:
        keys.verify_key(other)
    assert _status(info) == (401, "Invalid API key")


def test_scopes_admin_includes_chat_and_chat_does_not_include_admin():
    assert keys.scope_allows(["chat"], "chat")
    assert keys.scope_allows(["admin"], "chat")
    assert keys.scope_allows(["chat", "admin"], "admin")
    assert not keys.scope_allows(["chat"], "admin")
    assert not keys.scope_allows([], "chat")


async def test_authenticate_enforces_the_scope_and_returns_the_key_as_the_identity(store):
    row, key = service.create_key("Chat widget", ["chat"], created_by=ACTOR)
    ctx = await keys.authenticate(key, keys.SCOPE_CHAT)
    assert ctx["user_id"] == row["id"] and ctx["api_key_id"] == row["id"]
    assert ctx["auth"] == "api_key" and ctx["scopes"] == ["chat"] and ctx["user_token"] == key
    with pytest.raises(HTTPException) as info:
        await keys.authenticate(key, keys.SCOPE_ADMIN)
    assert info.value.status_code == 403


def test_a_store_outage_is_a_503_that_names_no_key_material(store, caplog):
    _, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    digest = keys.hash_key(key)
    # A client error that carries the request URL, digest included.
    store.fail = RuntimeError(f"GET /rest/v1/api_keys?key_hash=eq.{digest} timed out")
    with caplog.at_level(logging.DEBUG), pytest.raises(HTTPException) as info:
        keys.verify_key(key)
    assert info.value.status_code == 503
    assert digest not in str(info.value.detail) and digest not in caplog.text
    assert info.value.__cause__ is None  # the URL-bearing error is not chained out either


# --------------------------------------------------------------------------
# Logs and the audit trail
# --------------------------------------------------------------------------


async def test_nothing_logs_a_key_a_digest_or_a_prefix(store, caplog):
    with caplog.at_level(logging.DEBUG):
        row, key = service.create_key("Billing sync", ["chat", "admin"], created_by=ACTOR)
        await keys.authenticate(key, keys.SCOPE_ADMIN)
        task = keys.record_use(row["id"])
        if task:
            await task
        presented = [keys.generate_key(), key[:-1] + ("A" if key[-1] != "A" else "B"),
                     "wae_nope", "wae_" + "z" * 60]
        for bad in presented:
            with pytest.raises(HTTPException):
                await keys.authenticate(bad, keys.SCOPE_CHAT)
        service.list_keys()
        service.revoke_key(row["id"], revoked_by=ACTOR)
        with pytest.raises(HTTPException):
            await keys.authenticate(key, keys.SCOPE_CHAT)
        store.fail = RuntimeError("down")
        keys.touch_last_used(row["id"])
    logged = caplog.text + "".join(repr(r.args) for r in caplog.records)
    assert caplog.records, "the paths above do log; an empty capture would prove nothing"
    for secret in (key, keys.hash_key(key), key[:12], key[4:]):
        assert secret not in logged
    # Nor any part of a key that was merely presented and refused.
    assert "wae_" not in logged
    for bad in presented:
        assert keys.hash_key(bad) not in logged and bad[4:12] not in logged


def test_create_and_revoke_are_audited_without_key_material(store):
    row, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    other = "00000000-0000-0000-0000-000000000002"
    service.revoke_key(row["id"], revoked_by=other)
    created, revoked = store.audit
    assert (created["entity_type"], created["action"], created["actor_user_id"]) == ("api_keys", "create", ACTOR)
    assert created["entity_id"] == row["id"]
    assert created["after"]["name"] == "Billing sync" and created["after"]["scopes"] == ["chat"]
    assert (revoked["action"], revoked["actor_user_id"]) == ("revoke", other)
    assert revoked["before"]["revoked_at"] is None and revoked["after"]["revoked_at"]
    trail = repr(store.audit)
    assert key not in trail and keys.hash_key(key) not in trail and "key_hash" not in trail
    assert store.row(row["id"])["revoked_by"] == other


def test_revoke_is_idempotent_and_unknown_is_none(store):
    row, _ = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    first = service.revoke_key(row["id"], revoked_by=ACTOR)
    again = service.revoke_key(row["id"], revoked_by=ACTOR)
    assert first["status"] == again["status"] == "revoked"
    assert again["revoked_at"] == first["revoked_at"]
    assert [a["action"] for a in store.audit] == ["create", "revoke"]  # not audited twice
    assert service.revoke_key("11111111-1111-1111-1111-111111111111", revoked_by=ACTOR) is None


# --------------------------------------------------------------------------
# last_used_at
# --------------------------------------------------------------------------


async def test_last_used_is_written_off_the_event_loop_and_does_not_hold_the_request(store, monkeypatch):
    row, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    loop_thread = threading.get_ident()
    release = threading.Event()
    seen = {}

    def _slow_touch(key_id):
        seen["thread"] = threading.get_ident()
        release.wait(5)
        seen["key_id"] = key_id

    monkeypatch.setattr(keys, "touch_last_used", _slow_touch)
    # The write is stuck for as long as we like: authenticate still returns.
    ctx = await asyncio.wait_for(keys.authenticate(key, keys.SCOPE_CHAT), timeout=2)
    assert ctx["user_id"] == row["id"] and "key_id" not in seen
    (task,) = list(keys._background)
    release.set()
    await task
    assert seen["key_id"] == row["id"]
    assert seen["thread"] != loop_thread  # supabase-py is synchronous: never on the loop


async def test_last_used_is_written_at_most_once_a_minute_per_key(store, monkeypatch):
    row, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    other, other_key = service.create_key("Second", ["chat"], created_by=ACTOR)
    for _ in range(5):
        await keys.authenticate(key, keys.SCOPE_CHAT)
    await keys.authenticate(other_key, keys.SCOPE_CHAT)
    await asyncio.gather(*list(keys._background))
    updates = [q for q in store.queries if q["op"] == "update"]
    assert sorted(q["filters"][0][2] for q in updates) == sorted([row["id"], other["id"]])
    assert store.row(row["id"])["last_used_at"] and store.row(other["id"])["last_used_at"]

    # Once the interval has passed the next use is written again.
    monkeypatch.setattr(keys, "LAST_USED_WRITE_INTERVAL", 0.0)
    await keys.authenticate(key, keys.SCOPE_CHAT)
    await asyncio.gather(*list(keys._background))
    assert len([q for q in store.queries if q["op"] == "update"]) == 3


async def test_a_refused_key_is_not_marked_used(store):
    row, key = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    with pytest.raises(HTTPException):
        await keys.authenticate(key, keys.SCOPE_ADMIN)  # live key, wrong scope
    service.revoke_key(row["id"], revoked_by=ACTOR)
    with pytest.raises(HTTPException):
        await keys.authenticate(key, keys.SCOPE_CHAT)
    assert not keys._background
    assert store.row(row["id"])["last_used_at"] is None


def test_a_failed_last_used_write_is_swallowed(store):
    row, _ = service.create_key("Billing sync", ["chat"], created_by=ACTOR)
    store.fail = RuntimeError("database is down")
    keys.touch_last_used(row["id"])  # must not raise: it runs in a background task
