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
