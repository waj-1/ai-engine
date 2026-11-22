"""LangChain chat-model factory for `agent_models` rows.

`agent_models.provider` is a free-text column that the admin form pins to
one of `_MODEL_PROVIDERS` in `admin/router.py`. Some of those providers
are merely "claimed" — the user can pick them in the UI but there's no
LangChain integration wired up yet. This module is the single place we
decide what actually works and what raises a clear error.

Adding a new provider:
  1. Add the langchain-* dep to requirements.txt, then run `make lock` and
     commit requirements.lock with it — the lock is what CI and the image
     install, so a dep declared only in requirements.txt never ships
  2. Add a builder below
  3. Register it in `_BUILDERS`
  4. Remove the provider's entry from `UNSUPPORTED_PROVIDERS` if present
  5. Add it to `_MODEL_PROVIDERS` in `admin/router.py` so the form offers it

REMOVED 2026-08-16 — `vertex_ai`, `anthropic_vertex`, `vertex_maas`. All three
needed GCP Application Default Credentials, which do not exist on Azure compute
(this repo has been Azure-only since 2026-08-02), so all three were selectable in
the admin UI and non-functional at runtime. The Claude-on-Vertex model rows had
already been deleted for exactly this reason (see admin/seed_agent_models.py).
Reach Claude through the `anthropic` builder (direct API, ANTHROPIC_API_KEY)
instead; Gemini is unaffected and stays on `google_genai`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# Process-local, content-addressed cache of built chat-model clients.
# The model config is global per (agent_kind, environment) — it does NOT
# vary by business, org, or user — so identical configs across every
# tenant share one LangChain client instead of rebuilding the model (and
# its underlying HTTP client) on every request. The cache key IS the
# config, so a changed `agent_models` row produces a new client with zero
# staleness; superseded entries are harmless and bounded by the number of
# distinct model configs in play.
_LLM_CACHE: Dict[str, Any] = {}


def _cache_key(model: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(model, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def clear_llm_cache() -> None:
    """Drop all cached chat-model clients. Test hook / manual reset."""
    _LLM_CACHE.clear()


class UnsupportedProviderError(RuntimeError):
    """Raised when an agent_models row claims a provider this runtime
    cannot build. The admin form should reject this case at save time
    (see admin/router.py:_validate_model_provider), so a runtime raise
    means the row was inserted before validation was wired or via direct
    SQL."""


# Providers that the admin UI may surface but no LangChain integration
# exists for. Saving a model row with one of these is rejected; resolving
# one at runtime raises so the boot fails fast.
UNSUPPORTED_PROVIDERS = frozenset({"bedrock"})


def build_chat_model(model: Dict[str, Any]) -> Optional[Any]:
    """Resolve an `agent_models` row to a LangChain chat model instance.

    Returns None when the row has no `model_string` (the caller treats
    this as "no model configured"). Raises `UnsupportedProviderError` if
    the provider can't be built.
    """
    if not model:
        return None
    if not model.get("model_string"):
        return None

    key = _cache_key(model)
    cached = _LLM_CACHE.get(key)
    if cached is not None:
        return cached

    provider = (model.get("provider") or "google_genai").lower()
    if provider in UNSUPPORTED_PROVIDERS:
        raise UnsupportedProviderError(
            f"provider '{provider}' is listed in the admin UI but has no "
            f"runtime integration yet. Change the model's provider, or "
            f"add a builder to runtime/llm_factory.py and remove '{provider}' "
            f"from UNSUPPORTED_PROVIDERS."
        )

    builder = _BUILDERS.get(provider)
    if builder is None:
        raise UnsupportedProviderError(
            f"unknown provider '{provider}'. Supported: "
            f"{sorted(set(_BUILDERS) | UNSUPPORTED_PROVIDERS)}"
        )
    client = builder(model)
    _LLM_CACHE[key] = client
    return client


def _common_kwargs(model: Dict[str, Any]) -> Dict[str, Any]:
    """Knobs that map cleanly across providers."""
    extra = model.get("extra_config") or {}
    kwargs: Dict[str, Any] = {
        "model": model.get("model_string"),
        "temperature": float(model.get("temperature", 0.2)),
    }
    if model.get("max_output_tokens"):
        kwargs["max_output_tokens"] = int(model["max_output_tokens"])
    if "top_p" in extra:
        kwargs["top_p"] = float(extra["top_p"])
    return kwargs


_VALID_THINKING_LEVELS = ("minimal", "low", "medium", "high")


def _google_thinking_kwargs(extra: Dict[str, Any]) -> Dict[str, Any]:
    """Map a model row's `extra_config` thinking controls to builder kwargs.

    Gemini 3+ models default to ``thinking_level="high"`` (full reasoning on
    every call — high latency); they take ``thinking_level`` of
    ``'minimal'|'low'|'medium'|'high'``. Gemini 2.5 models instead take an
    integer ``thinking_budget`` (``0`` disables thinking). Operators set
    whichever the model supports on the model row, e.g. ``thinking_level:
    "low"`` for a tool-driven concierge that doesn't need deep reasoning.

    ``thinking_level`` wins when both are present — the SDK rejects passing
    both, and level is the Gemini 3+ path. Unknown levels are dropped rather
    than forwarded so a typo can't fail every request at build time.
    """
    level = extra.get("thinking_level")
    if isinstance(level, str) and level.lower() in _VALID_THINKING_LEVELS:
        return {"thinking_level": level.lower()}
    if extra.get("thinking_budget") is not None:
        try:
            return {"thinking_budget": int(extra["thinking_budget"])}
        except (TypeError, ValueError):
            return {}
    return {}


def _first_chunk_timeout(extra: Dict[str, Any]) -> Optional[float]:
    """`extra_config.first_chunk_timeout_s` as a positive float, else None."""
    raw = extra.get("first_chunk_timeout_s")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class FirstChunkTimeoutError(TimeoutError):
    """A model stream sent nothing within its first-chunk deadline, twice."""


_GUARDED_GOOGLE_CLASS: Optional[type] = None


def _guarded_google_class() -> type:
    """`ChatGoogleGenerativeAI` with a first-chunk deadline on streaming.

    On prod (2026-10-14) customer-chat replies were either fast (p50 2.6s, none
    between 20s and 150s over 7 days) or hung: one `generate_content_stream`
    call sat 3-4 min and produced almost nothing, so the eval client gave up
    at 120s. The SDK can't bound this: its async timeout covers the WHOLE
    stream (a limit low enough to catch the hang also cuts long healthy
    answers), and it retries HTTP status codes, never a timeout. A stall
    shows up as no first chunk, so that is what we time: no chunk within the
    deadline → cancel and retry once. Once a chunk has been yielded the
    output is already the caller's, so a later stall is not retried."""
    global _GUARDED_GOOGLE_CLASS
    if _GUARDED_GOOGLE_CLASS is not None:
        return _GUARDED_GOOGLE_CLASS

    import asyncio

    from langchain_google_genai import ChatGoogleGenerativeAI

    class FirstChunkGuardedChatGoogle(ChatGoogleGenerativeAI):
        first_chunk_timeout_s: Optional[float] = None

        async def _astream(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            deadline = self.first_chunk_timeout_s
            if not deadline:
                async for chunk in super()._astream(*args, **kwargs):
                    yield chunk
                return
            for attempt in (1, 2):
                stream = super()._astream(*args, **kwargs)
                try:
                    first = await asyncio.wait_for(stream.__anext__(), deadline)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    await stream.aclose()
                    if attempt == 1:
                        logger.warning(
                            "%s: no stream chunk within %.0fs, retrying once",
                            self.model, deadline,
                        )
                        continue
                    raise FirstChunkTimeoutError(
                        f"{self.model}: no stream chunk within {deadline:.0f}s "
                        f"on 2 attempts"
                    ) from None
                yield first
                async for chunk in stream:
                    yield chunk
                return

    _GUARDED_GOOGLE_CLASS = FirstChunkGuardedChatGoogle
    return _GUARDED_GOOGLE_CLASS


def _build_google_genai(model: Dict[str, Any]) -> Any:
    from langchain_google_genai import ChatGoogleGenerativeAI

    kwargs = _common_kwargs(model)
    extra = model.get("extra_config") or {}
    if "top_k" in extra:
        kwargs["top_k"] = int(extra["top_k"])
    kwargs.update(_google_thinking_kwargs(extra))
    kwargs["google_api_key"] = os.getenv("GOOGLE_API_KEY", "")
    # Opt-in per model row: a row without it builds the stock class, unchanged.
    timeout = _first_chunk_timeout(extra)
    if timeout is not None:
        return _guarded_google_class()(first_chunk_timeout_s=timeout, **kwargs)
    return ChatGoogleGenerativeAI(**kwargs)


# "none" switches reasoning off. Newer deployments (gpt-6-luna on aoai-example,
# 2026-10-22) reason by default and reject function tools on
# /chat/completions unless it is sent: "Function tools with reasoning_effort
# are not supported for this model … set reasoning_effort to 'none'".
_VALID_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")


def _openai_reasoning_kwargs(extra: Dict[str, Any]) -> Dict[str, Any]:
    """Map `extra_config.reasoning_effort` onto the OpenAI/Azure reasoning knob.

    The direct analogue of `_google_thinking_kwargs`'s `thinking_level`, and it
    shares the same four-way vocabulary — so a Gemini row and an OpenAI/Azure row
    describe "how hard should this model think" the same way, which is what makes
    a cross-provider bake-off comparable.

    Only emitted when set: reasoning models (gpt-5*, o-series) accept it, while a
    non-reasoning deployment rejects it, so a blanket default would break every
    plain chat model.
    """
    effort = extra.get("reasoning_effort")
    if effort in (None, ""):
        return {}
    effort = str(effort).strip().lower()
    if effort not in _VALID_REASONING_EFFORTS:
        raise UnsupportedProviderError(
            f"invalid extra_config.reasoning_effort {effort!r}; "
            f"expected one of {_VALID_REASONING_EFFORTS}"
        )
    return {"reasoning_effort": effort}


def _apply_openai_family_knobs(kwargs: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """Shared post-processing for the OpenAI-shaped builders (openai, azure_openai).

    Two things reasoning models need that `_common_kwargs` can't express:

    1. `omit_temperature` — the gpt-5 / o-series reasoning models reject any
       `temperature` other than the API default and 400 the request. This is the
       same escape hatch the deleted Claude-on-Vertex builder carried for Claude
       Opus 4.8, which deprecated the parameter for the same reason.
    2. `reasoning_effort` — see `_openai_reasoning_kwargs`.
    """
    if extra.get("omit_temperature"):
        kwargs.pop("temperature", None)
    kwargs.update(_openai_reasoning_kwargs(extra))
    # The SDK's own default is a 600s request timeout: one GPT-6 Luna chat
    # reply waited 204s on Azure with three lookups (2026-10-23 exam), and the
    # customer sees nothing meanwhile. Opt-in per model row, so other rows
    # keep the SDK default; the SDK retries a timed-out request max_retries
    # times.
    timeout = extra.get("request_timeout_s")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        kwargs["timeout"] = float(timeout)
    retries = extra.get("max_retries")
    if isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0:
        kwargs["max_retries"] = retries


def _build_openai(model: Dict[str, Any]) -> Any:
    from langchain_openai import ChatOpenAI

    kwargs = _common_kwargs(model)
    extra = model.get("extra_config") or {}
    # OpenAI uses `max_tokens` not `max_output_tokens`.
    if "max_output_tokens" in kwargs:
        kwargs["max_tokens"] = kwargs.pop("max_output_tokens")
    if "frequency_penalty" in extra:
        kwargs["frequency_penalty"] = float(extra["frequency_penalty"])
    if "presence_penalty" in extra:
        kwargs["presence_penalty"] = float(extra["presence_penalty"])
    _apply_openai_family_knobs(kwargs, extra)

    # Any OpenAI-COMPATIBLE endpoint, not just api.openai.com. Set
    # `extra_config.base_url` to point a row at Azure AI Foundry, a self-hosted
    # vLLM, OpenRouter, etc. — the wire protocol is identical, only the host and
    # credential differ.
    #
    # This is how the non-OpenAI Azure catalog is reachable. Foundry serves
    # DeepSeek / Grok / Mistral / gpt-oss over the Azure AI Model Inference API
    # at `https://<res>.services.ai.azure.com/models`, which takes the model NAME
    # in the request body — NOT the `/openai/deployments/<name>` path that
    # `_build_azure_openai` constructs. So those models go through THIS builder
    # with a base_url, while first-party Azure OpenAI deployments keep using
    # `azure_openai`. (Verified live: no `api-version` query param is required,
    # and the endpoint accepts the SDK's `Authorization: Bearer` header.)
    base_url = (extra.get("base_url") or "").strip()
    if base_url:
        kwargs["base_url"] = base_url

    # Which env var holds the key. Defaults to OPENAI_API_KEY so existing rows
    # are untouched; a Foundry row sets e.g. "AZURE_AI_FOUNDRY_API_KEY". Named
    # rather than inlined because a key is a secret and must not live in
    # extra_config, which the admin portal renders in plain text.
    key_env = (extra.get("api_key_env") or "OPENAI_API_KEY").strip()
    kwargs["api_key"] = os.getenv(key_env, "")
    if base_url and not kwargs["api_key"]:
        if _is_local_endpoint(base_url):
            # A model server on this machine or network (Ollama, vLLM, LM
            # Studio) takes no key, but the SDK refuses an empty one.
            kwargs["api_key"] = "not-needed"
        else:
            raise UnsupportedProviderError(
                f"extra_config.base_url is set but {key_env} is empty — an "
                f"OpenAI-compatible endpoint still needs a credential"
            )
    if base_url and extra.get("stream_usage", True):
        # The SDK only asks for token counts on a streamed reply when it is
        # talking to api.openai.com. Behind any other base_url every turn
        # reported 0 tokens, so usage and cost were empty for local models and
        # for the Foundry catalog alike. `extra_config.stream_usage: false`
        # turns it off for a server that rejects `stream_options`.
        kwargs["stream_usage"] = True
    return ChatOpenAI(**kwargs)


def _is_local_endpoint(base_url: str) -> bool:
    """True for a loopback, private-network or single-label host."""
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal") or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _build_azure_openai(model: Dict[str, Any]) -> Any:
    from langchain_openai import AzureChatOpenAI

    kwargs = _common_kwargs(model)
    if "max_output_tokens" in kwargs:
        kwargs["max_tokens"] = kwargs.pop("max_output_tokens")
    extra = model.get("extra_config") or {}
    # `model_string` for Azure is the deployment name, not the OpenAI model id.
    kwargs["azure_deployment"] = kwargs.pop("model")
    kwargs["api_version"] = extra.get("api_version") or os.getenv(
        "AZURE_OPENAI_API_VERSION", "2024-06-01"
    )
    kwargs["azure_endpoint"] = extra.get("endpoint") or os.getenv("AZURE_OPENAI_ENDPOINT", "")
    kwargs["api_key"] = os.getenv("AZURE_OPENAI_API_KEY", "")
    _apply_openai_family_knobs(kwargs, extra)
    if not kwargs["azure_endpoint"]:
        raise UnsupportedProviderError(
            "azure_openai requires AZURE_OPENAI_ENDPOINT in env or "
            "`endpoint` in extra_config"
        )
    return AzureChatOpenAI(**kwargs)


def _build_anthropic(model: Dict[str, Any]) -> Any:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise UnsupportedProviderError(
            "anthropic requires `langchain-anthropic` in requirements.txt"
        ) from exc

    kwargs = _common_kwargs(model)
    extra = model.get("extra_config") or {}
    if "max_output_tokens" in kwargs:
        kwargs["max_tokens"] = kwargs.pop("max_output_tokens")
    if "top_k" in extra:
        kwargs["top_k"] = int(extra["top_k"])
    # Claude Opus 4.8+ DEPRECATED `temperature` and 400s if it is sent. The
    # deleted Claude-on-Vertex builder carried this escape hatch; it has to live
    # here now, because after the Vertex removal this builder is the ONLY way to
    # reach a Claude model (including the eval/E2E `claude-*` judge). Without it
    # every Opus row would fail on its first call — `_common_kwargs` always sets
    # a temperature, defaulting to 0.2.
    if extra.get("omit_temperature"):
        kwargs.pop("temperature", None)
    kwargs["api_key"] = os.getenv("ANTHROPIC_API_KEY", "")
    # `''` is `ChatAnthropic.api_key`'s OWN default, so an empty key CONSTRUCTS
    # FINE and 401s on every call instead. That turned "unset key → smoke-only
    # degrade" in admin/e2e_runner.py into "a judge that fails every scenario":
    # each 401 is caught as {'score': 0.5, 'pass': False, 'rationale':
    # 'judge_error: …'}, so a claude-evaluator suite scored a red run 0.5 after
    # a ~30s failed round-trip per scenario. ANTHROPIC_API_KEY is bound nowhere
    # today — no workflow, not in DEPLOY.md, commented out in .env.example — so
    # that was the DEFAULT path, not an edge case. Fail closed, like
    # `_build_openai` does for a base_url row with no credential.
    if not kwargs["api_key"]:
        raise UnsupportedProviderError(
            "anthropic requires ANTHROPIC_API_KEY — it is unset/empty, and "
            "ChatAnthropic accepts an empty key and 401s on every call rather "
            "than failing at build time"
        )
    return ChatAnthropic(**kwargs)


def _build_ollama(model: Dict[str, Any]) -> Any:
    """A model served by Ollama, through its OpenAI-compatible API.

    The portal offered "Ollama" as a provider for months while the runtime
    refused it. It is the `openai` builder pointed at the local server: the
    row's `extra_config.base_url`, else `OLLAMA_BASE_URL`, else Ollama's
    default port on this machine. No key is needed (`_is_local_endpoint`).
    """
    extra = dict(model.get("extra_config") or {})
    extra["base_url"] = ((extra.get("base_url") or "").strip()
                         or os.getenv("OLLAMA_BASE_URL", "").strip()
                         or "http://127.0.0.1:11434/v1")
    return _build_openai({**model, "extra_config": extra})


_BUILDERS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "ollama": _build_ollama,
    "google_genai": _build_google_genai,
    "google": _build_google_genai,  # legacy alias
    "openai": _build_openai,
    "azure_openai": _build_azure_openai,
    "anthropic": _build_anthropic,
}


SUPPORTED_PROVIDERS = frozenset(_BUILDERS)
