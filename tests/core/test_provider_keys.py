"""Model provider keys stored encrypted (core/provider_keys.py) and how the
chat-model builders pick a key (runtime/llm_factory.py).

What these pin:

1. Encryption: a round trip, rotation through a comma-separated SECRET_KEY,
   and refusal (never plain text) when SECRET_KEY is missing or too short.
2. Resolution: a stored key wins over the environment variable; with none
   stored the variable is used as before; with no SECRET_KEY the table is
   never read.
3. The snapshot: reads are served from memory for the TTL, a write through
   the service is visible at once, and a read on the event loop never blocks.
4. Each chat-model builder takes the stored key.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from core import provider_keys as pk
from tests.provider_keys_fake import FakeDB

SECRET = "unit-test-secret-key-0123456789"
OTHER = "another-unit-test-secret-abcdef"


@pytest.fixture
def db(monkeypatch):
    """A fake database, SECRET_KEY set, and a clean snapshot before and after."""
    fake = FakeDB()
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setattr("core.auth.get_service_client", lambda: fake)
    monkeypatch.setattr("core.auth.get_service_client_for_env", lambda env: fake)
    monkeypatch.setattr("admin.audit.record_change", lambda **kw: fake.tables.setdefault("audit", []).append(kw))
    from runtime.llm_factory import clear_llm_cache

    pk.invalidate()
    clear_llm_cache()
    yield fake
    pk.invalidate()
    clear_llm_cache()


def _store(fake, provider, key, name=""):
    from admin.services import provider_keys as svc

    return svc.set_key(provider, key, actor=None, name=name or None)


# --------------------------------------------------------------------------
# 1. Encryption
# --------------------------------------------------------------------------


def test_round_trip_and_ciphertext_does_not_contain_the_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)
    token = pk.encrypt("sk-live-abcdefgh12345678")
    assert "sk-live" not in token and "12345678" not in token
    assert pk.decrypt(token) == "sk-live-abcdefgh12345678"
    # Authenticated: a changed ciphertext is rejected, not decrypted to noise.
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(pk.DecryptError):
        pk.decrypt(tampered)


def test_rotation_first_secret_encrypts_all_decrypt(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", OTHER)
    old_token = pk.encrypt("key-under-the-old-secret")
    # Rotate: the new secret first, the old one kept for reading.
    monkeypatch.setenv("SECRET_KEY", f"{SECRET}, {OTHER}")
    assert pk.decrypt(old_token) == "key-under-the-old-secret"
    new_token = pk.encrypt("key-under-the-new-secret")
    # Drop the old secret: what was re-encrypted still opens, the rest does not.
    monkeypatch.setenv("SECRET_KEY", SECRET)
    assert pk.decrypt(new_token) == "key-under-the-new-secret"
    with pytest.raises(pk.DecryptError) as exc:
        pk.decrypt(old_token)
    assert old_token not in str(exc.value)


@pytest.mark.parametrize("value", ["", "   ", "short", "long-enough-secret-0000,short"])
def test_no_usable_secret_key_refuses_to_encrypt(monkeypatch, value):
    monkeypatch.setenv("SECRET_KEY", value)
    assert not pk.encryption_available()
    with pytest.raises(pk.SecretKeyMissing) as exc:
        pk.encrypt("sk-live-abcdefgh12345678")
    assert "SECRET_KEY" in str(exc.value) and "sk-live" not in str(exc.value)


def test_unset_secret_key_refuses_to_encrypt(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(pk.SecretKeyMissing):
        pk.encrypt("sk-live-abcdefgh12345678")


def test_hint_is_the_last_four_and_absent_for_a_short_value():
    assert pk.hint_for("sk-live-abcdefgh1234") == "1234"
    assert pk.hint_for("12345678") == ""


# --------------------------------------------------------------------------
# 2. Resolution order
# --------------------------------------------------------------------------


def test_stored_key_wins_over_the_environment_variable(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    assert pk.resolve_key("openai", "OPENAI_API_KEY") == "env-openai-key"
    _store(db, "openai", "portal-openai-key-0001")
    assert pk.resolve_key("openai", "OPENAI_API_KEY") == "portal-openai-key-0001"
    assert pk.env_or_stored("OPENAI_API_KEY") == "portal-openai-key-0001"
    # Another provider is untouched and still reads its variable.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic-key")
    assert pk.resolve_key("anthropic", "ANTHROPIC_API_KEY") == "env-anthropic-key"


def test_no_stored_key_and_no_variable_is_empty(db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert pk.resolve_key("anthropic", "ANTHROPIC_API_KEY") == ""
    assert not pk.has_key("anthropic")
    _store(db, "anthropic", "portal-anthropic-key-01")
    assert pk.has_key("anthropic")


def test_legacy_google_alias_shares_the_gemini_key(db):
    _store(db, "google", "portal-gemini-key-0001")
    assert pk.stored_key("google_genai") == "portal-gemini-key-0001"
    assert pk.stored_key("google") == "portal-gemini-key-0001"


def test_named_credential_is_separate_from_the_default_key(db):
    _store(db, "openai", "portal-default-key-0001")
    _store(db, "openai", "portal-catalog-key-0002", name="catalog")
    assert pk.stored_key("openai") == "portal-default-key-0001"
    assert pk.stored_key("openai", "catalog") == "portal-catalog-key-0002"
    assert pk.stored_key("openai", "missing") is None


def test_without_secret_key_the_table_is_never_read(db, monkeypatch):
    db.tables["provider_keys"] = [{"environment": "staging", "provider": "openai", "name": "",
                                   "ciphertext": "not-a-token", "updated_at": "t"}]
    monkeypatch.delenv("SECRET_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    assert pk.resolve_key("openai", "OPENAI_API_KEY") == "env-openai-key"
    assert pk.cache_token() == ""
    assert db.calls == []


def test_a_row_this_server_cannot_decrypt_falls_back_to_the_variable(db, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", OTHER)
    foreign = pk.encrypt("key-from-another-server")
    monkeypatch.setenv("SECRET_KEY", SECRET)
    db.tables["provider_keys"] = [{"environment": "staging", "provider": "openai", "name": "",
                                   "ciphertext": foreign, "updated_at": "t"}]
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    assert pk.resolve_key("openai", "OPENAI_API_KEY") == "env-openai-key"


def test_a_database_error_falls_back_to_the_variable(db, monkeypatch):
    db.fail = True
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    assert pk.resolve_key("openai", "OPENAI_API_KEY") == "env-openai-key"


def test_keys_are_scoped_to_this_environment(db, monkeypatch):
    db.tables["provider_keys"] = [{"environment": "prod", "provider": "openai", "name": "",
                                   "ciphertext": pk.encrypt("prod-only-key-000001"), "updated_at": "t"}]
    monkeypatch.setenv("ENVIRONMENT", "development")  # reads as staging
    assert pk.stored_key("openai") is None
    monkeypatch.setenv("ENVIRONMENT", "production")
    pk.invalidate()
    assert pk.stored_key("openai") == "prod-only-key-000001"


# --------------------------------------------------------------------------
# 3. The in-memory snapshot
# --------------------------------------------------------------------------


def test_reads_are_served_from_memory_within_the_ttl(db):
    _store(db, "openai", "portal-openai-key-0001")
    before = db.reads()
    for _ in range(25):
        assert pk.stored_key("openai") == "portal-openai-key-0001"
    assert db.reads() == before


def test_a_write_through_the_service_is_visible_at_once(db, monkeypatch):
    from admin.services import provider_keys as svc

    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    _store(db, "openai", "portal-openai-key-0001")
    assert pk.stored_key("openai") == "portal-openai-key-0001"
    first_token = pk.cache_token()
    db.tables["provider_keys"][0]["updated_at"] = "earlier"  # two writes in one clock tick
    _store(db, "openai", "portal-openai-key-0002")
    assert pk.stored_key("openai") == "portal-openai-key-0002"
    assert pk.cache_token() and pk.cache_token() != first_token
    assert svc.remove_key("openai", actor=None) is True
    assert pk.resolve_key("openai", "OPENAI_API_KEY") == "env-openai-key"
    assert pk.cache_token() == ""


def test_a_change_made_elsewhere_is_picked_up_after_the_ttl(db, monkeypatch):
    assert pk.stored_key("openai") is None
    # Another replica stores a key: this process still has its snapshot.
    db.tables["provider_keys"] = [{"environment": "staging", "provider": "openai", "name": "",
                                   "ciphertext": pk.encrypt("stored-by-a-replica-01"), "updated_at": "t"}]
    assert pk.stored_key("openai") is None
    monkeypatch.setattr(pk, "CACHE_TTL_SEC", 0.0)
    assert pk.stored_key("openai") == "stored-by-a-replica-01"


async def test_a_read_on_the_event_loop_never_touches_the_database_inline(db, monkeypatch):
    """Supabase-py is synchronous: on the loop the read returns what is in
    memory and the reload runs in another thread."""
    db.tables["provider_keys"] = [{"environment": "staging", "provider": "openai", "name": "",
                                   "ciphertext": pk.encrypt("loaded-in-background-1"), "updated_at": "t"}]
    loop_thread = threading.get_ident()
    seen = []
    real = pk._fetch_rows

    def spying():
        seen.append(threading.get_ident())
        return real()

    monkeypatch.setattr(pk, "_fetch_rows", spying)
    pk.stored_key("openai")  # nothing loaded yet: starts a reload, does not read inline
    for _ in range(100):
        if seen and pk._loaded_at is not None:
            break
        await asyncio.sleep(0.01)
    assert seen and loop_thread not in seen
    assert pk.stored_key("openai") == "loaded-in-background-1"
    # The boot path: an awaited load, also off the loop.
    seen.clear()
    await pk.arefresh()
    assert seen and loop_thread not in seen


# --------------------------------------------------------------------------
# 4. The chat-model builders
# --------------------------------------------------------------------------


def _secret(value):
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def _model(provider, **extra):
    return {"provider": provider, "model_string": "sample-model", "temperature": 0.2,
            "extra_config": extra}


def test_google_builder_uses_the_stored_key(db, monkeypatch):
    from runtime.llm_factory import build_chat_model

    monkeypatch.setenv("GOOGLE_API_KEY", "env-gemini-key")
    assert _secret(build_chat_model(_model("google_genai")).google_api_key) == "env-gemini-key"
    _store(db, "google_genai", "portal-gemini-key-0001")
    assert _secret(build_chat_model(_model("google_genai")).google_api_key) == "portal-gemini-key-0001"
    assert _secret(build_chat_model(_model("google")).google_api_key) == "portal-gemini-key-0001"


def test_openai_builder_uses_the_stored_key_and_rebuilds_when_it_changes(db, monkeypatch):
    from runtime.llm_factory import build_chat_model

    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    row = _model("openai")
    first = build_chat_model(row)
    assert _secret(first.openai_api_key) == "env-openai-key"
    assert build_chat_model(row) is first  # cached while nothing changes
    _store(db, "openai", "portal-openai-key-0001")
    second = build_chat_model(row)
    assert second is not first and _secret(second.openai_api_key) == "portal-openai-key-0001"


def test_a_key_replaced_on_another_replica_rebuilds_the_cached_client(db, monkeypatch):
    """The client cache is keyed by the model row. Without the stored-keys
    token in that key, a replica that did not make the change would keep its
    old client, and the old key, until restart."""
    from runtime.llm_factory import build_chat_model

    _store(db, "openai", "portal-openai-key-0001")
    row = _model("openai")
    first = build_chat_model(row)
    # Another replica replaces the key: only the database changes here.
    db.tables["provider_keys"][0].update(
        ciphertext=pk.encrypt("portal-openai-key-0002"), updated_at="later")
    assert build_chat_model(row) is first  # within the TTL
    monkeypatch.setattr(pk, "CACHE_TTL_SEC", 0.0)
    second = build_chat_model(row)
    assert second is not first and _secret(second.openai_api_key) == "portal-openai-key-0002"


def test_azure_builder_uses_the_stored_key(db, monkeypatch):
    from runtime.llm_factory import build_chat_model

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "env-azure-key")
    _store(db, "azure_openai", "portal-azure-key-000001")
    llm = build_chat_model(_model("azure_openai", endpoint="https://sample.openai.azure.com"))
    assert _secret(llm.openai_api_key) == "portal-azure-key-000001"


def test_anthropic_builder_uses_the_stored_key_with_no_variable(db, monkeypatch):
    from runtime.llm_factory import UnsupportedProviderError, build_chat_model

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(UnsupportedProviderError):
        build_chat_model(_model("anthropic"))
    _store(db, "anthropic", "portal-anthropic-key-01")
    assert _secret(build_chat_model(_model("anthropic")).anthropic_api_key) == "portal-anthropic-key-01"


def test_named_credential_and_custom_variable_do_not_take_the_default_key(db, monkeypatch):
    """The default stored OpenAI key is for api.openai.com. A row that points
    elsewhere takes a stored key only by name."""
    from runtime.llm_factory import UnsupportedProviderError, build_chat_model

    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    monkeypatch.setenv("SAMPLE_CATALOG_KEY", "env-catalog-key")
    _store(db, "openai", "portal-default-key-0001")
    _store(db, "openai", "portal-catalog-key-0002", name="catalog")
    hosted = "https://models.example.test/v1"
    by_env = build_chat_model(_model("openai", base_url=hosted, api_key_env="SAMPLE_CATALOG_KEY"))
    assert _secret(by_env.openai_api_key) == "env-catalog-key"
    by_name = build_chat_model(_model("openai", base_url=hosted, credential="catalog"))
    assert _secret(by_name.openai_api_key) == "portal-catalog-key-0002"
    with pytest.raises(UnsupportedProviderError) as exc:
        build_chat_model(_model("openai", base_url=hosted, credential="nobody-stored-this"))
    assert "portal-default" not in str(exc.value)
    # A local model server keeps working with no key, and is not sent the default one.
    local = build_chat_model(_model("ollama"))
    assert _secret(local.openai_api_key) == "env-openai-key"


def test_env_only_deployment_builds_exactly_as_before(monkeypatch):
    """No SECRET_KEY (how a deployment that keeps keys in the environment
    runs): same keys, and the cache key is the plain hash of the model row."""
    import hashlib
    import json

    from runtime import llm_factory

    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    llm_factory.clear_llm_cache()
    row = _model("openai")
    assert _secret(llm_factory.build_chat_model(row).openai_api_key) == "env-openai-key"
    expected = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    assert llm_factory._cache_key(row) == expected
    llm_factory.clear_llm_cache()
