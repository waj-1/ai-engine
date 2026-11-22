"""Per-agent prepare hooks.

Bindings let an agent inject runtime context the generic LLM loop can't
know about: a record fetched from another system, a stateful editor built
from the request, a permissions filter applied on top of the profile
whitelist.

A binding is a `(profile, ctx) -> BindingResult` callable. `BindingResult`
carries:
- `system_addendum: str` — appended to the resolved system prompt
- `tool_filter: callable[list, list] | None` — extra filter applied
  after the profile whitelist (None = no extra filter)
- `tool_factory_kwargs: dict` — passed to whatever tool factory the
  agent uses (e.g. the caller's backend token for its tool factory)
- `metadata: dict` — extra data passed to trace recorder

Bindings register at module import; the runtime looks them up by
`agent_kind`. If no binding is registered, `default_binding()` returns
an empty `BindingResult`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.agent_profile import Profile


@dataclass
class BindingResult:
    system_addendum: str = ""
    tool_filter: Optional[Callable[[List[Any]], List[Any]]] = None
    tool_factory_kwargs: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


# Type alias for a binding function.
BindingFn = Callable[[Profile, Dict[str, Any]], BindingResult]


_REGISTRY: Dict[str, BindingFn] = {}


def register_binding(agent_kind: str, fn: BindingFn) -> None:
    """Attach a prepare hook to an agent_kind.

    Re-registering the same kind overrides — useful for tests.
    """
    _REGISTRY[agent_kind] = fn


def get_binding(agent_kind: str) -> BindingFn:
    """Lookup; falls back to `default_binding` if no binding registered."""
    return _REGISTRY.get(agent_kind, default_binding)


def default_binding(profile: Profile, ctx: Dict[str, Any]) -> BindingResult:
    """No-op binding used for agents that don't need a prepare hook."""
    return BindingResult()
