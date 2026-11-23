"""The Automatic model row (core.model_resolver): which model rows this
install can use, and which one an agent pinned to Automatic runs on."""
from __future__ import annotations

import pytest

from core import model_resolver as mr

_KEYS = ("GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY",
         "AZURE_OPENAI_ENDPOINT", "MY_HOSTED_KEY")


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for name in _KEYS:
        monkeypatch.delenv(name, raising=False)


def _m(provider, model_string="m", created="2026-01-01", **extra):
    row = {"model_id": f"id-{provider}-{model_string}", "name": f"{provider} {model_string}",
           "provider": provider, "model_string": model_string, "is_active": True,
           "environment": "staging", "created_at": created, "extra_config": extra}
    return row


def test_a_cloud_model_is_usable_only_with_its_key(monkeypatch):
    for provider, key in (("google_genai", "GOOGLE_API_KEY"), ("google", "GOOGLE_API_KEY"),
                          ("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
        assert mr.model_unusable_reason(_m(provider)) == f"{key} is not set"
        monkeypatch.setenv(key, "k")
        assert mr.is_usable_model(_m(provider))
        monkeypatch.delenv(key)


def test_azure_needs_a_key_and_an_endpoint(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    assert mr.model_unusable_reason(_m("azure_openai")) == "AZURE_OPENAI_ENDPOINT is not set"
    assert mr.is_usable_model(_m("azure_openai", endpoint="https://example.invalid"))


def test_local_models_need_no_key():
    assert mr.is_usable_model(_m("ollama"))
    assert mr.is_usable_model(_m("openai", base_url="http://localhost:8080/v1"))
    assert mr.is_usable_model(_m("openai", base_url="http://model-server:8000/v1"))


def test_a_hosted_openai_compatible_endpoint_needs_its_own_key(monkeypatch):
    row = _m("openai", base_url="https://models.example.com/v1", api_key_env="MY_HOSTED_KEY")
    assert mr.model_unusable_reason(row) == "MY_HOSTED_KEY is not set"
    monkeypatch.setenv("OPENAI_API_KEY", "k")  # the wrong key does not count
    assert not mr.is_usable_model(row)
    monkeypatch.setenv("MY_HOSTED_KEY", "k")
    assert mr.is_usable_model(row)


def test_rows_that_are_never_a_chat_brain():
    assert not mr.is_usable_model(_m("ollama", realtime=True))
    assert not mr.is_usable_model({**_m("ollama"), "is_active": False})
    assert not mr.is_usable_model({**_m("ollama"), "model_string": ""})
    assert not mr.is_usable_model(_m("azure_voice_live"))
    assert not mr.is_usable_model(_m(mr.AUTO_PROVIDER, mr.AUTO_MODEL_STRING))


def test_first_usable_prefers_the_default_model_then_the_oldest(monkeypatch):
    from core.config import AGENT_CONFIG

    rows = [_m("google_genai", "other", created="2026-01-01"),
            _m("ollama", "local", created="2026-03-01"),
            _m("ollama", "older-local", created="2026-02-01")]
    # No Google key: the Gemini row is skipped, the OLDEST usable row wins.
    assert mr.first_usable_model(rows)["model_string"] == "older-local"
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    assert mr.first_usable_model(rows)["model_string"] == "other"
    rows.append(_m("google_genai", AGENT_CONFIG.main_model, created="2026-09-01"))
    assert mr.first_usable_model(rows)["model_string"] == AGENT_CONFIG.main_model
    assert mr.first_usable_model([]) is None


def test_first_usable_keeps_to_the_environment_when_it_has_one():
    rows = [{**_m("ollama", "prod-model", created="2026-01-01"), "environment": "prod"},
            _m("ollama", "staging-model", created="2026-02-01")]
    assert mr.first_usable_model(rows, "staging")["model_string"] == "staging-model"
    assert mr.first_usable_model(rows, "prod")["model_string"] == "prod-model"


def test_resolve_leaves_a_real_model_alone_and_resolves_automatic(monkeypatch):
    real = _m("ollama")
    assert mr.resolve_auto_model(real, "staging") is real
    assert mr.resolve_auto_model(None, "staging") is None
    auto = _m(mr.AUTO_PROVIDER, mr.AUTO_MODEL_STRING)
    monkeypatch.setattr(mr, "load_usable_model", lambda env=None, client=None: real)
    assert mr.resolve_auto_model(auto, "staging") is real
    monkeypatch.setattr(mr, "load_usable_model", lambda env=None, client=None: None)
    assert mr.resolve_auto_model(auto, "staging") is None


def test_load_usable_model_reads_rows_and_survives_a_dead_database(monkeypatch):
    from tests.fake_pg import Client

    db = {"agent_models": [_m("google_genai"), _m("ollama", "local")]}
    assert mr.load_usable_model("staging", Client(db))["model_string"] == "local"

    class Dead:
        def table(self, _n):
            raise RuntimeError("down")

    assert mr.load_usable_model("staging", Dead()) is None


def test_build_chat_model_builds_the_resolved_model_for_the_automatic_row(monkeypatch):
    from runtime import llm_factory

    auto = _m(mr.AUTO_PROVIDER, mr.AUTO_MODEL_STRING)
    monkeypatch.setattr(mr, "load_usable_model", lambda env=None, client=None: None)
    assert llm_factory.build_chat_model(auto) is None  # nothing usable: no client, no raise
    built = []
    monkeypatch.setitem(llm_factory._BUILDERS, "ollama", lambda m: built.append(m) or "client")
    monkeypatch.setattr(mr, "load_usable_model", lambda env=None, client=None: _m("ollama", "local-x"))
    llm_factory.clear_llm_cache()
    assert llm_factory.build_chat_model(auto) == "client"
    assert built[0]["model_string"] == "local-x"
    llm_factory.clear_llm_cache()
