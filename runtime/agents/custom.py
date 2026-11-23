"""The generic agent: one class that runs any `agent_kind='custom'` profile.

Every other agent in `runtime/agents/` is a Python class written for one
product surface, with its own tool factory, fallback prompt and route. This
one has no behaviour of its own. What a custom agent does is entirely its
`agent_profiles` row:

  instructions   the profile's pinned `system` prompt version
  model          `model_main_id`
  tools          the MCP servers and tool bundles attached to the profile
                 (resolved by `runtime.tools._mcp_resolver`, same as every agent)
  knowledge      `data_source_ids` -> the `search_knowledge` tool
  sub-agents     delegatable downstream profiles -> `ask_<slug>` tools

So an agent is created in the portal or over MCP, never by a code change.
MANY profiles share the kind and are told apart by slug, which is why the
profile is always resolved by `(custom, slug, environment)` and never through
`load_active` (there is no single "active custom profile").

Served at `POST /v1/agents/{slug}/chat` (endpoints/agents_v1.py). The event
contract is the one every agent shares: answer / thought / tool_activity /
usage / turn_meta / error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date
from typing import Any, AsyncGenerator, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from common.announce import contains_announce, reply_ends_in_question
from core.agent_profile import AgentProfileLoader, Profile, _default_environment
from core.base_agent import BaseStreamingAgent, TokenBudgetExceeded
from core.config import AGENT_CONFIG
from core.tool_provenance import record_tool_result
from runtime.agent import RuntimeAgent
from runtime.prompt_cache import cached_system_message
from runtime.prompts._ui import get_ui_message
from runtime.tools import knowledge

logger = logging.getLogger(__name__)

CUSTOM_AGENT_KIND = "custom"

_MAX_ITERATIONS = 8

# Served only when a profile has no instructions yet (a just-created draft).
# Deliberately says nothing about who the agent is: that is the operator's job.
DEFAULT_INSTRUCTIONS = (
    "You are a helpful assistant. Answer clearly and briefly. If you do not know "
    "something, say so instead of guessing."
)

VOICE_TURN_RULES = (
    "## This turn is spoken aloud\n"
    "Your reply is read out by a voice, not shown on a screen. Answer in one to three "
    "short sentences. No markdown, lists, tables, links or emoji. Say numbers the way a "
    "person would say them, and read long reference numbers in groups of four digits."
)


class CustomRuntimeAgent(RuntimeAgent, BaseStreamingAgent):
    """A profile-driven agent with no code of its own."""

    # A custom agent replies in whatever language its user writes, so no locale
    # is ever folded to "en" for being outside a product's supported set.
    supported_locales = None  # type: ignore[assignment]

    def __init__(
        self,
        slug: str,
        *,
        locale: Optional[str] = None,
        environment: Optional[str] = None,
    ):
        self.slug = slug
        super().__init__(CUSTOM_AGENT_KIND, environment=environment, profile_slug=slug)
        self.locale = self._normalize_locale(locale or "")

    def _normalize_locale(self, locale: str) -> str:
        """Primary language subtag of ``locale`` ("ar-SA" -> "ar"), "" when unset."""
        return (locale or "").strip().replace("_", "-").split("-")[0].lower()

    @staticmethod
    def _load_profile_by_slug(agent_kind: str, slug: str, environment: Optional[str]) -> Profile:
        """Resolve `(custom, slug, environment)` and FAIL CLOSED.

        The base class falls back to the kind's active profile when a slug does
        not resolve, which is right for one-profile kinds (a typo in a voice
        specialist's slug must not take a surface down). Here it would be
        wrong: there are many custom agents, so "the active one" is some OTHER
        agent, and a mistyped slug would be answered by a stranger's agent.
        `MissingProfileError` propagates and the route turns it into a 404.
        """
        return AgentProfileLoader.load_delegate(agent_kind, slug, environment or _default_environment())

    # --- tools --------------------------------------------------------------
    def _knowledge_tools(self) -> List[Any]:
        return knowledge.knowledge_tools_for(self.profile)

    async def prefetch_voice_context(self, message: str, *, match_count: int) -> Optional[str]:
        """Knowledge chunks for a voice turn, fetched before the first model
        call so a knowledge question costs one call instead of two. Silent on
        any failure: the tool stays bound for a miss."""
        sources = list(getattr(self.profile, "data_source_ids", None) or [])
        if not sources:
            return None
        try:
            return await asyncio.to_thread(
                knowledge.retrieve, message, source_ids=sources, match_count=match_count)
        except Exception as exc:  # noqa: BLE001
            logger.debug("custom agent %s: voice prefetch skipped: %s", self.slug, exc)
            return None

    # --- per-turn context ---------------------------------------------------
    def _turn_context(self, voice_turn: bool, prefetched: Optional[str]) -> str:
        parts = [
            f"Today's date is {date.today().isoformat()}.",
            "## Reply language\n"
            "Reply in the language of the user's latest message, whatever language earlier "
            "messages or these instructions are written in."
            + (f" If that message has no clear language, use `{self.locale}`." if self.locale else ""),
        ]
        if voice_turn:
            parts.append(VOICE_TURN_RULES)
        if (prefetched or "").strip():
            parts.append(
                "## Knowledge base articles already retrieved for THIS question\n"
                "Answer from them when they cover the question; search again only if they do "
                "not.\n\n" + prefetched.strip())
        return "\n\n".join(parts)

    # --- the loop -----------------------------------------------------------
    async def run_stream(self, message: str, context: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        voice_turn = bool(context.get("voice_turn"))
        previous = context.get("previous_messages", []) or []
        stage = context.get("stage_timings") if isinstance(context.get("stage_timings"), dict) else None

        def _stage_add(key: str, started: float) -> None:
            if stage is not None:
                stage[key] = round(float(stage.get(key) or 0.0) + (time.monotonic() - started) * 1000.0, 1)

        try:
            if self.llm is None:
                if getattr(self, "model_unresolved", False):
                    # Pinned to the Automatic model and the install has no
                    # usable model yet (a fresh install with no key and no
                    # local model): say what to do, in words a new user can act on.
                    from core.model_resolver import NO_MODEL_MESSAGE

                    logger.info("custom agent %s: no usable model on this install yet", self.slug)
                    yield {"type": "error", "message": NO_MODEL_MESSAGE}
                    return
                logger.error("custom agent %s: profile has no usable model", self.slug)
                yield {"type": "error", "message": "This agent has no model configured."}
                return
            # No code tool pool: what binds is whatever the profile attaches.
            ready = self.prepare(ctx={**context, "locale": self.locale,
                                      "fallback_system_prompt": DEFAULT_INSTRUCTIONS},
                                 all_tools=[])
            system_prompt = ready["system_prompt"] or DEFAULT_INSTRUCTIONS
            tools, tools_by_name = list(ready["tools"]), dict(ready["tools_by_name"])
            # Knowledge search is engine infrastructure, not an attachable tool
            # row, so it is added after the profile's own tools resolve. An
            # operator tool that already claimed the name wins.
            for tool in self._knowledge_tools():
                if tool.name not in tools_by_name:
                    tools.append(tool)
                    tools_by_name[tool.name] = tool

            messages = self.build_messages(previous, message)
            full_messages: List[Any] = [
                cached_system_message(system_prompt, self.profile.model_main),
                SystemMessage(content=self._turn_context(voice_turn, context.get("prefetched_docs"))),
            ] + messages
            llm = self.llm.bind_tools(tools) if tools else self.llm

            usage = {"input_tokens": 0, "output_tokens": 0}
            final = ""
            tool_count = 0
            announce_retry = False
            iteration = 0
            while iteration < _MAX_ITERATIONS:
                iteration += 1
                content = ""
                end_calls: List[Dict[str, Any]] = []
                started = time.monotonic()
                async for event in llm.astream_events(full_messages, version="v2"):
                    kind = event.get("event", "")
                    if kind == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk is not None and getattr(chunk, "content", None):
                            text = self._normalize_content(chunk.content)
                            content += text
                            if not getattr(chunk, "tool_call_chunks", None):
                                yield {"type": "answer", "content": text}
                    elif kind == "on_chat_model_end":
                        out = event.get("data", {}).get("output")
                        self.update_usage(usage, out)
                        end_calls = list(getattr(out, "tool_calls", None) or [])
                _stage_add("llm_ms", started)
                self.check_token_budget(usage, label=f"custom:{self.slug}")
                calls = [{"name": c.get("name", ""), "args": c.get("args") or {}, "id": c.get("id") or ""}
                         for c in end_calls if c.get("name")]
                if not calls:
                    final = content
                    if (not announce_retry and tool_count == 0 and tools_by_name
                            and contains_announce(final) and not reply_ends_in_question(final)):
                        # "Let me check" with no tool call behind it strands the
                        # user: the turn is over and nothing is checking. One
                        # forced round.
                        announce_retry = True
                        full_messages.append(AIMessage(content=content))
                        full_messages.append(HumanMessage(content=(
                            "You said you would check but called no tool, and you get no second "
                            "turn. Call the right tool NOW, then answer in the user's language. "
                            "Do not repeat the announcement.")))
                        continue
                    break
                full_messages.append(AIMessage(content=content, tool_calls=calls))
                for c in calls:
                    yield {"type": "thought", "content": get_ui_message(
                        self.locale, "using_tool", tool_name=self._format_tool_name(c["name"]))}

                async def _invoke(c: Dict[str, Any]) -> Any:
                    tool = tools_by_name.get(c["name"])
                    if tool is None:
                        return json.dumps({"ok": False, "error": f"unknown tool {c['name']}"})
                    try:
                        return await tool.ainvoke(c["args"])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("custom agent %s: tool %s failed: %s", self.slug, c["name"], exc)
                        return json.dumps({"ok": False, "error": "tool_failed",
                                           "detail": "The action did not go through; say so."})

                tool_count += len(calls)
                logger.info("custom agent %s: tools=%s", self.slug, [c["name"] for c in calls])
                started = time.monotonic()
                # Independent external calls: run them together. Order of the
                # results is the order of the calls.
                results = await asyncio.gather(*(_invoke(c) for c in calls))
                _stage_add("tools_ms", started)
                # Provenance is recorded for EVERY result before anything can
                # leave this loop: a voice turn's identifier guard withholds any
                # number no recorded tool result vouches for.
                for c, res in zip(calls, results):
                    res_str = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)
                    record_tool_result(res_str, tool_name=c["name"])
                    full_messages.append(ToolMessage(content=self.truncate_tool_result(res_str),
                                                     tool_call_id=c["id"]))
                yield {"type": "tool_activity", "tools": [c["name"] for c in calls]}

            if not (final or "").strip():
                # Ran out of rounds mid-tool-use (or the model returned nothing):
                # one tool-less call to turn what was gathered into an answer.
                synth = ""
                nudge = HumanMessage(content=(
                    "Using what the tools returned above, write the final reply to the user now, "
                    "in the user's language. Do not call any tools."))
                async for event in self.llm.astream_events(full_messages + [nudge], version="v2"):
                    if event.get("event") == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk is not None and getattr(chunk, "content", None):
                            text = self._normalize_content(chunk.content)
                            synth += text
                            yield {"type": "answer", "content": text}
                    elif event.get("event") == "on_chat_model_end":
                        self.update_usage(usage, event.get("data", {}).get("output"))
                final = synth

            yield {"type": "usage",
                   "model": (self.profile.model_main or {}).get("model_string") or AGENT_CONFIG.main_model,
                   "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                   "iterations": iteration, "tool_count": tool_count}
            yield {"type": "turn_meta", "metadata": {
                "iteration_count": iteration,
                "cache_read_tokens": usage.get("cache_read_tokens", 0),
                "cache_creation_tokens": usage.get("cache_creation_tokens", 0),
            }}
        except TokenBudgetExceeded as exc:
            logger.warning("custom agent %s stopped on token budget: %s", self.slug, exc)
            yield {"type": "error", "message": "Please try again with a shorter message."}
        except Exception as exc:  # noqa: BLE001
            logger.error("custom agent %s error: %s", self.slug, exc, exc_info=True)
            yield {"type": "error", "message": "An error occurred. Please try again."}


def create_custom_agent(slug: str, **kwargs: Any) -> CustomRuntimeAgent:
    """Build the agent for `slug`. Raises `MissingProfileError` when no such
    agent exists in this environment. Blocking (profile load): call through
    asyncio.to_thread from async code."""
    return CustomRuntimeAgent(slug, **kwargs)
