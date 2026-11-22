"""Unit tests for runtime.llm_factory.build_chat_model dispatch.

The factory is the single place the runtime decides which provider can
actually be built. Provider builders themselves pull in langchain-* deps
(env-dependent), so these tests exercise the *dispatch* and error paths
plus the pure `_common_kwargs` mapping — no real chat model is constructed
except through a stubbed builder.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from runtime import llm_factory
from runtime.llm_factory import (
    UnsupportedProviderError,
    build_chat_model,
    _common_kwargs,
)


def test_build_chat_model_returns_none_for_empty_row():
    assert build_chat_model({}) is None
    assert build_chat_model(None) is None  # type: ignore[arg-type]


def test_build_chat_model_returns_none_without_model_string():
    assert build_chat_model({"provider": "google_genai"}) is None
    assert build_chat_model({"provider": "google_genai", "model_string": ""}) is None


def test_unsupported_provider_raises():
    for provider in ("bedrock",):
        with pytest.raises(UnsupportedProviderError):
            build_chat_model({"provider": provider, "model_string": "x"})


def test_ollama_is_a_local_openai_compatible_server(monkeypatch):
    """The portal listed Ollama as a provider while the runtime refused it."""
    from runtime import llm_factory

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    llm_factory._LLM_CACHE.clear()
    llm = build_chat_model({"provider": "ollama", "model_string": "qwen2.5:3b"})
    assert str(llm.openai_api_base).startswith("http://127.0.0.1:11434/v1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")
    llm = build_chat_model({"provider": "ollama", "model_string": "qwen2.5:1.5b"})
    assert str(llm.openai_api_base).startswith("http://ollama:11434/v1")
    llm = build_chat_model({"provider": "ollama", "model_string": "llama3",
                            "extra_config": {"base_url": "http://10.0.0.5:11434/v1"}})
    assert str(llm.openai_api_base).startswith("http://10.0.0.5:11434/v1")


def test_unknown_provider_raises_with_supported_list():
    with pytest.raises(UnsupportedProviderError) as exc:
        build_chat_model({"provider": "cohere", "model_string": "x"})
    # The message should enumerate what *is* supported so the operator can fix it.
    assert "cohere" in str(exc.value)


def test_dispatch_is_case_insensitive_and_routes_to_builder(monkeypatch):
    captured = {}

    def fake_builder(model):
        captured["model"] = model
        return "BUILT"

    # Register a synthetic provider; provider lookup must lowercase first.
    monkeypatch.setitem(llm_factory._BUILDERS, "fakeprov", fake_builder)
    out = build_chat_model({"provider": "FakeProv", "model_string": "m-1"})
    assert out == "BUILT"
    assert captured["model"]["model_string"] == "m-1"


def test_default_provider_is_google_genai(monkeypatch):
    seen = {}
    monkeypatch.setitem(
        llm_factory._BUILDERS, "google_genai", lambda m: seen.setdefault("hit", True)
    )
    # No provider key → defaults to google_genai.
    build_chat_model({"model_string": "gemini-x"})
    assert seen.get("hit") is True


def test_common_kwargs_maps_core_knobs():
    kwargs = _common_kwargs(
        {
            "model_string": "gemini-2.5-pro",
            "temperature": "0.7",
            "max_output_tokens": "1024",
            "extra_config": {"top_p": "0.9"},
        }
    )
    assert kwargs["model"] == "gemini-2.5-pro"
    assert kwargs["temperature"] == pytest.approx(0.7)
    assert kwargs["max_output_tokens"] == 1024
    assert kwargs["top_p"] == pytest.approx(0.9)


def test_common_kwargs_defaults_temperature_and_omits_optional():
    kwargs = _common_kwargs({"model_string": "m"})
    assert kwargs["temperature"] == pytest.approx(0.2)
    assert "max_output_tokens" not in kwargs
    assert "top_p" not in kwargs


# ---------------------------------------------------------------------------
# Content-addressed client cache. Building a LangChain chat model on every
# request is wasteful — the model config is global per (agent_kind,
# environment), so identical configs must reuse one client instance.
# ---------------------------------------------------------------------------
def test_build_chat_model_caches_identical_config(monkeypatch):
    calls = {"n": 0}

    def fake_builder(model):
        calls["n"] += 1
        return object()

    monkeypatch.setitem(llm_factory._BUILDERS, "cacheprov", fake_builder)
    llm_factory.clear_llm_cache()

    cfg = {"provider": "cacheprov", "model_string": "m-1", "temperature": 0.2}
    first = build_chat_model(dict(cfg))
    second = build_chat_model(dict(cfg))

    assert first is second  # same instance reused across calls
    assert calls["n"] == 1  # builder invoked exactly once


def test_build_chat_model_distinct_config_not_shared(monkeypatch):
    monkeypatch.setitem(llm_factory._BUILDERS, "cacheprov", lambda m: object())
    llm_factory.clear_llm_cache()

    a = build_chat_model({"provider": "cacheprov", "model_string": "m-1"})
    b = build_chat_model({"provider": "cacheprov", "model_string": "m-2"})
    assert a is not b  # different config → different client


def test_build_chat_model_cache_ignores_key_order(monkeypatch):
    monkeypatch.setitem(llm_factory._BUILDERS, "cacheprov", lambda m: object())
    llm_factory.clear_llm_cache()

    a = build_chat_model({"provider": "cacheprov", "model_string": "m-1", "temperature": 0.3})
    b = build_chat_model({"temperature": 0.3, "model_string": "m-1", "provider": "cacheprov"})
    assert a is b  # cache key is order-independent


# ---------------------------------------------------------------------------
# Vertex removal (2026-08-16). All three Vertex builders needed GCP Application
# Default Credentials, which do not exist on Azure compute, so they were
# selectable in the admin UI and non-functional at runtime.
# ---------------------------------------------------------------------------
def _anthropic_rec(monkeypatch):
    """Fake `langchain_anthropic` in sys.modules rather than importing the real
    one, so the test pins the builder's kwargs without depending on the optional
    SDK being installed. `monkeypatch.setitem` (never assign-and-pop) so the
    entry is restored cleanly and can't leak into other tests."""
    rec = {}
    mod = types.ModuleType("langchain_anthropic")
    mod.ChatAnthropic = lambda **kw: rec.update(kw) or "LLM"
    monkeypatch.setitem(sys.modules, "langchain_anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    return rec


@pytest.mark.parametrize("provider", ["vertex_ai", "anthropic_vertex", "vertex_maas"])
def test_vertex_providers_are_gone(provider):
    """Not merely unregistered — unbuildable. `build_chat_model` must raise
    rather than fall through to some other builder, and the admin form validates
    against SUPPORTED_PROVIDERS, so this is also what stops a new Vertex row
    being saved."""
    assert provider not in llm_factory.SUPPORTED_PROVIDERS
    with pytest.raises(llm_factory.UnsupportedProviderError):
        build_chat_model({"provider": provider, "model_string": "claude-sonnet-4"})


@pytest.mark.parametrize("helper", [
    "_build_vertex_ai", "_build_anthropic_vertex", "_build_vertex_maas",
    "_vertex_project_location", "_vertex_access_token",
])
def test_vertex_helpers_are_deleted(helper):
    """`_vertex_access_token` did `import google.auth`; keeping any of these
    around would keep a GCP-credential code path alive behind a provider string
    nothing can select."""
    assert not hasattr(llm_factory, helper)


def test_anthropic_omits_temperature_when_flagged(monkeypatch):
    """Opus 4.8+ rows carry extra_config.omit_temperature=true; the builder must
    NOT pass `temperature` (the API 400s: 'temperature is deprecated').

    This escape hatch used to live only in the Claude-on-Vertex builder. Since
    that was deleted, `_build_anthropic` is the ONLY route to a Claude model, so
    the hatch had to move here — otherwise every Opus row would 400 on its first
    call, `_common_kwargs` always supplying a temperature (default 0.2)."""
    rec = _anthropic_rec(monkeypatch)
    llm_factory._build_anthropic(
        {"model_string": "claude-opus-4-8", "extra_config": {"omit_temperature": True}})
    assert "temperature" not in rec
    assert rec["model"] == "claude-opus-4-8"


def test_anthropic_includes_temperature_by_default(monkeypatch):
    rec = _anthropic_rec(monkeypatch)
    llm_factory._build_anthropic(
        {"model_string": "claude-sonnet-4-6", "temperature": 0.0, "extra_config": {}})
    assert rec["temperature"] == 0.0


@pytest.mark.parametrize("key", ["", None])
def test_anthropic_refuses_to_build_without_a_key(monkeypatch, key):
    """An empty key must fail HERE, not on every subsequent call.

    `''` is `ChatAnthropic.api_key`'s own default, so the builder used to
    return a fully-constructed client that 401'd on every invoke — which is how
    `admin/e2e_runner.py`'s documented "unset key → smoke-only" degrade became
    "a judge that scores every scenario 0.5 after a failed round-trip".
    ANTHROPIC_API_KEY is bound nowhere in this repo, so that was the DEFAULT
    path for the first claude-evaluator suite anyone ran."""
    rec = _anthropic_rec(monkeypatch)
    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)

    with pytest.raises(llm_factory.UnsupportedProviderError) as exc:
        llm_factory._build_anthropic(
            {"model_string": "claude-sonnet-4-6", "extra_config": {}})

    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert rec == {}, "no client may be constructed without a credential"


# ---------------------------------------------------------------------------
# Thinking controls. Gemini 3+ models default to thinking_level="high" (high
# latency); they take `thinking_level` ('minimal'|'low'|'medium'|'high'), while
# Gemini 2.5 models take an integer `thinking_budget` (0 disables). Operators
# set whichever fits the model on the model row's `extra_config`, and the
# google_genai builder must forward it so a tool-driven concierge can run at
# 'low' instead of paying full reasoning latency on every tool-loop step.
# ---------------------------------------------------------------------------
def test_thinking_kwargs_maps_thinking_level():
    assert llm_factory._google_thinking_kwargs({"thinking_level": "low"}) == {
        "thinking_level": "low"
    }


def test_thinking_kwargs_lowercases_and_validates_level():
    assert llm_factory._google_thinking_kwargs({"thinking_level": "HIGH"}) == {
        "thinking_level": "high"
    }
    # Unknown level is ignored rather than passed through to fail at build time.
    assert llm_factory._google_thinking_kwargs({"thinking_level": "ultra"}) == {}


def test_thinking_kwargs_maps_thinking_budget_to_int():
    assert llm_factory._google_thinking_kwargs({"thinking_budget": "0"}) == {
        "thinking_budget": 0
    }
    assert llm_factory._google_thinking_kwargs({"thinking_budget": 512}) == {
        "thinking_budget": 512
    }


def test_thinking_kwargs_prefers_level_over_budget():
    # The SDK rejects specifying both; thinking_level (Gemini 3+) wins.
    assert llm_factory._google_thinking_kwargs(
        {"thinking_level": "low", "thinking_budget": 0}
    ) == {"thinking_level": "low"}


def test_thinking_kwargs_empty_when_unset():
    assert llm_factory._google_thinking_kwargs({}) == {}


def test_build_google_genai_forwards_thinking_level(monkeypatch):
    captured = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "langchain_google_genai.ChatGoogleGenerativeAI", FakeChat, raising=False
    )
    llm_factory._build_google_genai(
        {
            "model_string": "gemini-3.1-flash-lite-preview",
            "extra_config": {"thinking_level": "low"},
        }
    )
    assert captured.get("thinking_level") == "low"
    assert captured.get("model") == "gemini-3.1-flash-lite-preview"


# ---------------------------------------------------------------------------
# OpenAI-family reasoning controls (Azure migration, 2026-07-26).
#
# `extra_config.reasoning_effort` is the OpenAI/Azure analogue of Gemini's
# `thinking_level` and shares its four-way vocabulary, so a Gemini row and an
# Azure row express "how hard should this think" the same way — which is what
# makes a cross-provider bake-off comparable.
#
# `omit_temperature` matters because the gpt-5 / o-series reasoning models reject
# any non-default temperature and 400 the request. The seeder's default is 0.2,
# so without this escape hatch every Azure reasoning row would fail on its first
# call. Same problem Claude Opus 4.8 already had on Vertex.
# ---------------------------------------------------------------------------


def _azure_rec(monkeypatch):
    rec = {}
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "AzureChatOpenAI", lambda **kw: rec.update(kw) or "LLM")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    return rec


def test_azure_openai_forwards_reasoning_effort(monkeypatch):
    rec = _azure_rec(monkeypatch)
    llm_factory._build_azure_openai(
        {"model_string": "gpt-5-mini-dep", "extra_config": {"reasoning_effort": "low"}})
    assert rec["reasoning_effort"] == "low"
    # model_string is the Azure DEPLOYMENT name, not the OpenAI model id.
    assert rec["azure_deployment"] == "gpt-5-mini-dep"


def test_azure_openai_omits_reasoning_effort_when_unset(monkeypatch):
    """A non-reasoning deployment rejects the param, so it must not be sent."""
    rec = _azure_rec(monkeypatch)
    llm_factory._build_azure_openai({"model_string": "gpt-4o-dep", "extra_config": {}})
    assert "reasoning_effort" not in rec


def test_azure_openai_omits_temperature_when_flagged(monkeypatch):
    rec = _azure_rec(monkeypatch)
    llm_factory._build_azure_openai(
        {"model_string": "gpt-5-mini-dep", "temperature": 0.2,
         "extra_config": {"omit_temperature": True}})
    assert "temperature" not in rec


def test_azure_openai_includes_temperature_by_default(monkeypatch):
    rec = _azure_rec(monkeypatch)
    llm_factory._build_azure_openai(
        {"model_string": "gpt-4o-dep", "temperature": 0.7, "extra_config": {}})
    assert rec["temperature"] == 0.7


def test_azure_openai_forwards_reasoning_effort_none(monkeypatch):
    # gpt-6-luna reasons by default and 400s a request with tools unless
    # reasoning_effort is "none" (prod probe 2026-10-22).
    rec = _azure_rec(monkeypatch)
    llm_factory._build_azure_openai(
        {"model_string": "gpt-6-luna", "extra_config": {"reasoning_effort": "none"}})
    assert rec["reasoning_effort"] == "none"


def test_azure_openai_rejects_invalid_reasoning_effort(monkeypatch):
    _azure_rec(monkeypatch)
    with pytest.raises(llm_factory.UnsupportedProviderError):
        llm_factory._build_azure_openai(
            {"model_string": "gpt-5-mini-dep", "extra_config": {"reasoning_effort": "turbo"}})


def test_openai_builder_shares_the_same_reasoning_knobs(monkeypatch):
    """gpt-5 via first-party OpenAI has the identical constraint."""
    rec = {}
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kw: rec.update(kw) or "LLM")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    llm_factory._build_openai(
        {"model_string": "gpt-5-mini", "temperature": 0.2,
         "extra_config": {"reasoning_effort": "medium", "omit_temperature": True}})
    assert rec["reasoning_effort"] == "medium"
    assert "temperature" not in rec


# ---------------------------------------------------------------------------
# OpenAI-COMPATIBLE endpoints (Azure migration, 2026-07-27).
#
# Azure AI Foundry serves the non-OpenAI catalog (DeepSeek / Grok / Mistral /
# gpt-oss) over the Azure AI Model Inference API at
# https://<res>.services.ai.azure.com/models — model name in the BODY, not the
# /openai/deployments/<name> path that _build_azure_openai constructs. Those
# rows therefore go through _build_openai with extra_config.base_url.
# ---------------------------------------------------------------------------


def test_openai_builder_honours_base_url(monkeypatch):
    rec = {}
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kw: rec.update(kw) or "LLM")
    monkeypatch.setenv("AZURE_AI_FOUNDRY_API_KEY", "fk")
    llm_factory._build_openai({
        "model_string": "DeepSeek-V3.2",
        "extra_config": {
            "base_url": "https://aifoundry-waj.services.ai.azure.com/models",
            "api_key_env": "AZURE_AI_FOUNDRY_API_KEY",
        },
    })
    assert rec["base_url"] == "https://aifoundry-waj.services.ai.azure.com/models"
    assert rec["api_key"] == "fk"
    assert rec["model"] == "DeepSeek-V3.2"


def test_openai_builder_defaults_to_openai_key_and_no_base_url(monkeypatch):
    """Existing first-party OpenAI rows must be completely unaffected."""
    rec = {}
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kw: rec.update(kw) or "LLM")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    llm_factory._build_openai({"model_string": "gpt-4o", "extra_config": {}})
    assert "base_url" not in rec
    assert rec["api_key"] == "sk-test"


def test_openai_builder_rejects_base_url_without_credential(monkeypatch):
    """Failing loudly beats silently sending an empty key and 401-ing per call."""
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kw: "LLM")
    monkeypatch.delenv("AZURE_AI_FOUNDRY_API_KEY", raising=False)
    with pytest.raises(llm_factory.UnsupportedProviderError):
        llm_factory._build_openai({
            "model_string": "DeepSeek-V3.2",
            "extra_config": {
                "base_url": "https://aifoundry-waj.services.ai.azure.com/models",
                "api_key_env": "AZURE_AI_FOUNDRY_API_KEY",
            },
        })


# ---------------------------------------------------------------------------
# First-chunk deadline (`extra_config.first_chunk_timeout_s`). Prod inbox
# replies hung 3-4 min on one Gemini stream call that produced almost nothing
# (2026-10-14); the SDK neither bounds that nor retries a timeout.
# ---------------------------------------------------------------------------
import asyncio

from langchain_google_genai import ChatGoogleGenerativeAI


def _gemini_row(**extra):
    return {"provider": "google_genai", "model_string": "gemini-3-flash-preview",
            "extra_config": extra}


def test_first_chunk_timeout_is_opt_in(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    llm = llm_factory._build_google_genai(_gemini_row())
    assert type(llm) is ChatGoogleGenerativeAI
    for bad in ("", "abc", 0, -5):
        assert type(llm_factory._build_google_genai(
            _gemini_row(first_chunk_timeout_s=bad))) is ChatGoogleGenerativeAI


def test_first_chunk_timeout_builds_the_guarded_model(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    llm = llm_factory._build_google_genai(
        _gemini_row(first_chunk_timeout_s="30", thinking_level="low"))
    assert isinstance(llm, ChatGoogleGenerativeAI)
    assert llm.first_chunk_timeout_s == 30.0
    assert llm.thinking_level == "low"


def _guarded_with_attempts(monkeypatch, attempts):
    """A guarded model whose parent stream plays `attempts` in order: each is
    a list of (delay_s, chunk) pairs."""
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    llm = llm_factory._build_google_genai(_gemini_row(first_chunk_timeout_s=0.05))
    calls = []

    async def fake_parent(self, *a, **k):
        script = attempts[len(calls)]
        calls.append(script)
        for delay, chunk in script:
            await asyncio.sleep(delay)
            yield chunk

    monkeypatch.setattr(ChatGoogleGenerativeAI, "_astream", fake_parent)
    return llm, calls


async def _drain(llm):
    return [c async for c in llm._astream([])]


async def test_a_stalled_first_chunk_is_retried_once(monkeypatch):
    llm, calls = _guarded_with_attempts(
        monkeypatch, [[(5, "never")], [(0, "a"), (0, "b")]])
    assert await _drain(llm) == ["a", "b"]
    assert len(calls) == 2


async def test_two_stalls_raise(monkeypatch):
    llm, calls = _guarded_with_attempts(
        monkeypatch, [[(5, "never")], [(5, "never")]])
    with pytest.raises(llm_factory.FirstChunkTimeoutError):
        await _drain(llm)
    assert len(calls) == 2


async def test_a_healthy_stream_is_untouched(monkeypatch):
    # Slow AFTER the first chunk is fine: only the first chunk is timed.
    llm, calls = _guarded_with_attempts(
        monkeypatch, [[(0, "a"), (0.2, "b"), (0.2, "c")]])
    assert await _drain(llm) == ["a", "b", "c"]
    assert len(calls) == 1


def test_azure_openai_forwards_a_request_timeout_and_retries(monkeypatch):
    # GPT-6 Luna once waited 204s on Azure for one chat reply (2026-10-23):
    # the SDK default is 600s. Opt-in per model row.
    rec = _azure_rec(monkeypatch)
    llm_factory._build_azure_openai({"model_string": "gpt-6-luna", "extra_config": {
        "endpoint": "https://x.openai.azure.com/", "request_timeout_s": 45, "max_retries": 1}})
    assert rec["timeout"] == 45.0 and rec["max_retries"] == 1


def test_azure_openai_keeps_the_sdk_defaults_when_unset(monkeypatch):
    rec = _azure_rec(monkeypatch)
    llm_factory._build_azure_openai({"model_string": "gpt-4o-dep", "extra_config": {
        "endpoint": "https://x.openai.azure.com/", "request_timeout_s": 0, "max_retries": True}})
    assert "timeout" not in rec and "max_retries" not in rec


# ── OpenAI-compatible endpoints (local model servers, hosted catalogs) ───────

def _openai_row(**extra):
    return {"provider": "openai", "model_string": "qwen2.5:3b", "temperature": 0.1,
            "extra_config": extra}


def test_local_endpoint_needs_no_key(monkeypatch):
    """Ollama / vLLM / LM Studio take no key; the builder used to refuse the
    row with "still needs a credential" unless a dummy one was exported."""
    from runtime import llm_factory

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm_factory._LLM_CACHE.clear()
    for url in ("http://127.0.0.1:11434/v1", "http://localhost:11434/v1",
                "http://ollama:11434/v1", "http://192.168.1.20:8000/v1",
                "http://host.docker.internal:11434/v1"):
        assert llm_factory.build_chat_model(_openai_row(base_url=url)) is not None, url


def test_hosted_endpoint_still_needs_its_key(monkeypatch):
    import pytest

    from runtime import llm_factory

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm_factory._LLM_CACHE.clear()
    with pytest.raises(llm_factory.UnsupportedProviderError, match="needs a credential"):
        llm_factory.build_chat_model(_openai_row(base_url="https://models.example.com/v1"))


def test_token_counts_are_requested_on_a_streamed_reply_behind_a_base_url(monkeypatch):
    """The SDK asks for usage on a stream only against api.openai.com, so
    every turn behind another base_url reported 0 tokens."""
    from runtime import llm_factory

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    llm_factory._LLM_CACHE.clear()
    on = llm_factory.build_chat_model(_openai_row(base_url="http://127.0.0.1:11434/v1"))
    assert on.stream_usage is True
    off = llm_factory.build_chat_model(
        _openai_row(base_url="http://127.0.0.1:11435/v1", stream_usage=False))
    assert not off.stream_usage
