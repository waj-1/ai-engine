"""Generic sub-agent delegation — ask_<slug> tools.

A parent profile's `downstream_delegates` (delegatable=true downstream rows,
spec 2026-07-23-agent-context-optimization) each become one synthetic tool.
Calling it loads the delegate profile by id, reuses RuntimeAgent.prepare()
(so the delegate's own curation, usage_guidance, and skills all apply), and
runs a bounded sync tool loop. Sync on purpose: agents execute tools via
`await tool.ainvoke(...)` (e.g. runtime/agents/business.py:349), which runs
sync funcs in a thread executor — the event loop is never blocked.

Depth is exactly 1: prepare() skips delegate binding when
ctx["_delegation_depth"] >= 1, and every delegated run sets it to 1.
"""
from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from core.config import AGENT_CONFIG
from runtime.llm_factory import build_chat_model

logger = logging.getLogger(__name__)

_SLUG_SAFE = re.compile(r"[^a-z0-9_]+")


class _AskArgs(BaseModel):
    request: str = Field(description="The full request for the specialist, self-contained "
                                     "(include dates, filters, and what to compute).")


def _tool_name(slug: str) -> str:
    return "ask_" + _SLUG_SAFE.sub("_", (slug or "specialist").lower()).strip("_")


def _run_delegate(descriptor: Dict[str, Any], parent_profile, ctx: Dict[str, Any],
                  all_tools: List[Any], request: str) -> str:
    from core.agent_profile import AgentProfileLoader
    from runtime.agent import RuntimeAgent
    from runtime.bindings import get_binding

    # Resolve by (agent_kind, slug, env), NOT by the frozen descriptor
    # profile_id: compile-on-promote freezes the delegate's SOURCE-env id into
    # the parent snapshot, but promoting the delegate mints a new target-env id
    # — a by-id lookup in prod finds no prod snapshot and fails. Slug resolution
    # survives the id remap and any promote order (the id is retained only as a
    # diagnostic fallback for a delegate that predates a slug).
    delegate_kind = descriptor.get("agent_kind") or parent_profile.agent_kind
    delegate_slug = descriptor.get("slug")
    try:
        if delegate_slug:
            sub_profile = AgentProfileLoader.load_delegate(
                delegate_kind, delegate_slug, parent_profile.environment)
        else:
            sub_profile = AgentProfileLoader.load_by_id(
                descriptor["profile_id"], parent_profile.environment)
    except Exception as exc:
        logger.error("delegation: delegate %r failed to load (%s)", delegate_slug, exc)
        return f"Specialist '{descriptor.get('slug')}' is unavailable right now."

    # build_chat_model already treats a falsy model dict as "no model
    # configured" (returns None) — call it unconditionally rather than
    # short-circuiting here, so the single build_chat_model call site is
    # the one source of truth for that check.
    llm = build_chat_model(sub_profile.model_main)
    if llm is None:
        logger.error("delegation: delegate %r has no usable model", descriptor.get("slug"))
        return f"Specialist '{descriptor.get('slug')}' is unavailable (no model configured)."

    # Reuse prepare() via a shim so the delegate's MCP curation, guidance
    # catalog, and skills all resolve exactly like a first-class agent —
    # including its agent_kind's registered binding. get_binding() falls
    # back to the no-op default for kinds with nothing registered.
    shim = SimpleNamespace(agent_kind=sub_profile.agent_kind, profile=sub_profile,
                           binding_fn=get_binding(sub_profile.agent_kind), llm=llm)
    sub_ctx = {**ctx, "_delegation_depth": 1,
               "fallback_system_prompt": ctx.get("fallback_system_prompt", "")}
    ready = RuntimeAgent.prepare(shim, sub_ctx, list(all_tools))
    tools = ready["tools"]
    tools_by_name = ready["tools_by_name"]

    bound = llm.bind_tools(tools) if tools else llm
    messages: List[Any] = [SystemMessage(content=ready["system_prompt"]),
                           HumanMessage(content=request)]
    deadline = time.monotonic() + AGENT_CONFIG.delegate_timeout_seconds
    for _ in range(AGENT_CONFIG.delegate_max_iterations):
        if time.monotonic() > deadline:
            break
        try:
            resp: AIMessage = bound.invoke(messages)
        except Exception as exc:  # delegate LLM error (network/rate-limit/provider) —
            # this ask_ tool must always return a string, never raise.
            logger.error("delegation: %r model invoke failed (%s)", descriptor.get("slug"), exc)
            return f"Specialist '{descriptor.get('slug')}' hit an error and couldn't finish."
        calls = getattr(resp, "tool_calls", None) or []
        if not calls:
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
            return text or "(the specialist returned no text)"
        messages.append(resp)
        for call in calls:
            tool = tools_by_name.get(call.get("name"))
            try:
                result = tool.invoke(call.get("args") or {}) if tool is not None \
                    else f"Tool {call.get('name')!r} not found."
            except Exception as exc:  # tool errors are data, not crashes
                result = f"Tool {call.get('name')!r} failed: {exc}"
            messages.append(ToolMessage(content=str(result), tool_call_id=call.get("id") or ""))
    logger.warning("delegation: %r hit iteration/time budget", descriptor.get("slug"))
    return "The specialist ran out of time before finishing; try a narrower request."


def build_delegate_tools(profile, ctx: Dict[str, Any], all_tools: List[Any]) -> List[StructuredTool]:
    """One ask_<slug> StructuredTool per delegate descriptor. Empty when the
    request is already a delegated run (depth guard) or nothing is delegatable."""
    if (ctx or {}).get("_delegation_depth", 0) >= 1:
        return []
    out: List[StructuredTool] = []
    for descriptor in getattr(profile, "downstream_delegates", None) or []:
        name = _tool_name(descriptor.get("slug") or "")
        description = (descriptor.get("description")
                       or f"Delegate to the {descriptor.get('display_name') or name} specialist.")
        description += (" Returns the specialist's finished report — present it to the "
                        "user directly rather than re-deriving it.")

        def _make(desc=descriptor):
            def _ask(request: str) -> str:
                return _run_delegate(desc, profile, ctx, all_tools, request)
            return _ask

        out.append(StructuredTool.from_function(
            func=_make(), name=name, description=description, args_schema=_AskArgs))
    return out
