"""Generic runtime for admin-portal-managed agents.

An agent built on `runtime.agent.RuntimeAgent` has its prompts, model,
tool whitelist, endpoint and downstream sub-agents expressed entirely in
`agent_profiles`; `runtime/agents/custom.py` is one with no code of its own.

Public API:
- `RuntimeAgent(agent_kind)` — instantiate a chat agent from its profile
- `bindings.register(agent_kind, fn)` — attach an agent-specific prepare
  hook (context the generic loop can't know about: a record fetched from
  another system, a stateful editor, a permissions filter)
- `compile_profile_snapshot(profile_id, env)` — capture an immutable
  version of a profile at promote time
"""
from runtime.agent import RuntimeAgent
from runtime.bindings import register_binding, get_binding
from runtime.compiler import compile_profile_snapshot, load_active_snapshot

__all__ = [
    "RuntimeAgent",
    "register_binding",
    "get_binding",
    "compile_profile_snapshot",
    "load_active_snapshot",
]
