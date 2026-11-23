"""Request-scoped LLM model override (mirrors core/prompt_resolver).

The eval-run trigger resolves a chosen agent_models row into a partial model
dict and sends it as the `X-WAJ-Model-Override` header. A middleware parks it
in this ContextVar; `runtime.agent.RuntimeAgent._build_llm` consults it and
builds the LLM from the override instead of the profile's pinned model — for
that one request only. The profile object is never mutated.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, Optional

_MODEL_OVERRIDE: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "_MODEL_OVERRIDE", default=None,
)


def set_model_override(d: Optional[Dict[str, Any]]) -> Any:
    return _MODEL_OVERRIDE.set(d)


def clear_model_override() -> None:
    _MODEL_OVERRIDE.set(None)


def get_model_override() -> Optional[Dict[str, Any]]:
    return _MODEL_OVERRIDE.get()


# ---------------------------------------------------------------------------
# The "Automatic" model: whichever model this install can actually use
# ---------------------------------------------------------------------------
#
# An `agent_models` row with provider `auto` names no model. An agent pinned to
# it runs on the first USABLE model of its environment, chosen per request, so
# it starts answering as soon as a model is added (or a key is set) with no
# edit to the agent. The seeded demo agent is pinned to it
# (admin/seed_demo_agent.py); any agent may be.
#
# `agent_profiles.model_main_id` is NOT NULL, which is why "use whatever is
# configured" is a model row and not an empty pin.

AUTO_PROVIDER = "auto"
AUTO_MODEL_NAME = "Automatic (first usable model)"
AUTO_MODEL_STRING = "auto"

# Providers that reach a model server on this machine or network: no key.
_LOCAL_PROVIDERS = frozenset({"ollama"})

# provider -> the environment variable that holds its key.
_PROVIDER_KEY_ENV = {
    "google_genai": "GOOGLE_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
}


def is_auto_model(model: Any) -> bool:
    return isinstance(model, dict) and (model.get("provider") or "").strip().lower() == AUTO_PROVIDER


def provider_key_available(env_name: str) -> bool:
    """True when the key named `env_name` is set for this process.

    The ONE place "does this provider have a key" is decided: a key stored on
    Settings > Model keys counts the same as the environment variable
    (core/provider_keys.py, which never blocks the event loop on this read).
    """
    from core.provider_keys import env_or_stored

    return bool((env_or_stored(env_name) or "").strip())


def model_unusable_reason(model: Dict[str, Any]) -> Optional[str]:
    """Why `model` cannot serve a chat turn on this install, or None if it can.

    "Usable" means the engine can build a client for it and the provider would
    not refuse the call for a missing credential: a local provider, or a cloud
    provider whose key is set. It does not prove the model answers (a local
    server may be stopped); that is only knowable by calling it.
    """
    import os

    if not isinstance(model, dict) or not model.get("model_string"):
        return "no model string"
    if model.get("is_active") is False:
        return "inactive"
    extra = model.get("extra_config") if isinstance(model.get("extra_config"), dict) else {}
    if extra.get("realtime"):
        return "a realtime voice model, not a chat model"
    provider = (model.get("provider") or "google_genai").strip().lower()
    if provider == AUTO_PROVIDER:
        return "the Automatic row names no model"
    if provider in _LOCAL_PROVIDERS:
        return None
    key_env = _PROVIDER_KEY_ENV.get(provider)
    if key_env is None:
        return f"provider {provider} is not a chat provider"
    if provider == "openai":
        key_env = (extra.get("api_key_env") or key_env).strip()
        base_url = (extra.get("base_url") or "").strip()
        if base_url and not provider_key_available(key_env):
            from runtime.llm_factory import _is_local_endpoint

            return None if _is_local_endpoint(base_url) else f"{key_env} is not set"
    if not provider_key_available(key_env):
        return f"{key_env} is not set"
    if provider == "azure_openai" and not (
            (extra.get("endpoint") or "").strip() or (os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip()):
        return "AZURE_OPENAI_ENDPOINT is not set"
    return None


def is_usable_model(model: Dict[str, Any]) -> bool:
    return model_unusable_reason(model) is None


def first_usable_model(rows: Any, environment: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The model an Automatic agent runs on, from `rows` (agent_models).

    Rows of `environment` win over the rest. Among the usable ones, the
    engine's default chat model (`AGENT_MAIN_MODEL`) is preferred, then the
    OLDEST row: `created_at` is stable, so adding a model never changes which
    one an agent already runs on (the reason admin/seed_templates.py orders
    the same way).
    """
    from core.config import AGENT_CONFIG

    usable = [r for r in (rows or []) if is_usable_model(r)]
    if environment:
        scoped = [r for r in usable if (r.get("environment") or environment) == environment]
        usable = scoped or usable
    if not usable:
        return None
    usable.sort(key=lambda r: str(r.get("created_at") or ""))
    preferred = [r for r in usable if r.get("model_string") == AGENT_CONFIG.main_model]
    return (preferred or usable)[0]


def load_usable_model(environment: Optional[str] = None, client: Any = None) -> Optional[Dict[str, Any]]:
    """Read the active model rows and pick `first_usable_model`. Blocking
    (supabase-py): call off the event loop. None when nothing is usable or
    the read fails; the caller says "no model configured"."""
    import logging

    try:
        if client is None:
            from core.auth import get_service_client

            client = get_service_client()
        rows = (client.table("agent_models").select("*").eq("is_active", True)
                .order("created_at", desc=False).execute().data or [])
    except Exception as exc:  # noqa: BLE001 - never a 500 for a missing model
        logging.getLogger(__name__).warning("model_resolver: model rows unavailable (%s)", exc)
        return None
    return first_usable_model(rows, environment)


def resolve_auto_model(model: Any, environment: Optional[str] = None) -> Any:
    """`model` unless it is the Automatic row; then the first usable model of
    `environment`, or None when the install has none yet."""
    if not is_auto_model(model):
        return model
    return load_usable_model(environment)


NO_MODEL_MESSAGE = (
    "No model is configured yet, so this agent cannot answer. Add one in the portal under "
    "Agents > Models (a model served on your own machine needs no key), or save a provider "
    "key under Settings > Model keys. This agent will then use it automatically."
)
