"""The four agents of the generic AI-agents demo portal.

  agents_support     inbound customer support ("Lina")
  agents_inquiries   inbound business inquiries ("Adam")
  agents_collection  outbound collection call ("Salma")
  agents_sales       outbound sales call ("Rami")

One streaming loop (`DemoRuntimeAgent.run_stream`, same event contract as the
other agents: answer / thought / usage / turn_meta / error) and four thin
subclasses that pick the tool pool, the fallback prompt and the per-call state.

State. One conversation's state — who is verified, links sent, bookings —
lives in `self.state` (plain JSON). A voice call builds one agent per call, so
it simply lives on the instance. Text chat builds one agent per HTTP turn, so
the portal round-trips the state as a signed token (endpoints/agents_portal.py)
and hands it back to the next turn's agent.

Outbound agents also serve a voice call in PRE-FETCHED-CONTEXT mode: the case
file is rendered into the persona's `{case_file}` (`persona_variables`) and the
realtime model calls `direct_voice_tools()` itself, with the send yes-gate fed
by `note_assistant_turn`.

Inbound agents (support, inquiries) are PROMPT-ONLY when their profile sets
`voice_config.prefetched_context` (what the seeder and
scripts/agents_demo_inbound_prompt_only.py configure): the selected use case is
pre-rendered into a case file and the public catalog into a facts sheet
(runtime/tools/agents_demo/inbound.py), both go into the prompt, and NO tool is
bound or declared — in voice (`direct_voice_tools()` is empty, so the provider
declares nothing) or in text chat (an empty tool pool). A profile without the
flag keeps the old delegated, tool-backed path, so a deploy that lands before
the profiles are switched (or a rollback of the switch) still works.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date
from typing import Any, AsyncGenerator, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from common.announce import contains_announce, reply_ends_in_question
from core.base_agent import BaseStreamingAgent, TokenBudgetExceeded
from core.config import AGENT_CONFIG
from core.tool_provenance import record_tool_result
from runtime.agent import RuntimeAgent
from runtime.prompt_cache import cached_system_message
from runtime.prompts.agents_demo import LEGACY_INBOUND_SYSTEM_PROMPTS, SYSTEM_PROMPTS, VOICE_TURN_RULES
from runtime.prompts._ui import get_ui_message
from runtime.tools.agents_demo import collection, common, data, inbound, inquiries, kb, sales, support

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 6


def _is_arabic(text: str) -> bool:
    return bool(re.search(r"[؀-ۿ]", text or ""))


# The demo's "Language & accent" picker sends a full BCP-47 tag (`ar-EG`,
# `en-GB`); `_normalize_locale` folds it to the bare language for content, so
# the region is read here and bound into the prompts as words. An unknown or
# missing region keeps the old defaults (Gulf Arabic; English unaccented).
_ARABIC_DIALECTS = {
    "AE": "Emirati", "SA": "Saudi", "KW": "Kuwaiti", "QA": "Qatari", "BH": "Bahraini",
    "OM": "Omani", "EG": "Egyptian", "JO": "Levantine", "LB": "Lebanese", "SY": "Syrian",
    "PS": "Palestinian", "IQ": "Iraqi", "MA": "Moroccan",
}
_ENGLISH_ACCENTS = {
    "US": "American", "GB": "British", "AU": "Australian", "IN": "Indian", "IE": "Irish",
    "CA": "Canadian",
}
DEFAULT_ARABIC_DIALECT = "Gulf"
DEFAULT_ENGLISH_ACCENT = "neutral"


def accent_variables(locale: Optional[str]) -> Dict[str, str]:
    """`{arabic_dialect}` / `{english_accent}` for the prompts, from the
    caller's locale tag. Only the chosen language's half moves off its
    default — the other half still applies when the caller switches."""
    parts = str(locale or "").strip().replace("_", "-").split("-")
    lang = parts[0].lower()
    region = parts[1].upper() if len(parts) >= 2 else ""
    return {
        "arabic_dialect": (_ARABIC_DIALECTS.get(region) if lang == "ar" else None) or DEFAULT_ARABIC_DIALECT,
        "english_accent": (_ENGLISH_ACCENTS.get(region) if lang == "en" else None) or DEFAULT_ENGLISH_ACCENT,
    }


def profile_is_prompt_only(profile: Any) -> bool:
    """`voice_config.prefetched_context` on the agent's profile — for an
    inbound kind that flag means prompt-only (case file + facts sheet, no
    tools) in BOTH voice and text. Fails closed to the delegated path."""
    try:
        from voice.live.config import resolve_live_config

        voice = getattr(profile, "voice_config", None)
        return bool(resolve_live_config(profile_voice=voice if isinstance(voice, dict) else None)
                    .prefetched_context)
    except Exception:  # noqa: BLE001
        logger.debug("agents demo: prompt-only flag unreadable", exc_info=True)
        return False


class DemoRuntimeAgent(RuntimeAgent, BaseStreamingAgent):
    AGENT_KEY = ""

    def __init__(
        self,
        *,
        locale: str = "en",
        call_case: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
        kb_source_id: Optional[str] = None,
        environment: Optional[str] = None,
        profile_slug: Optional[str] = None,
    ):
        self.demo = data.AGENTS[self.AGENT_KEY]
        # Resolved BEFORE the profile load: an unknown case is a LookupError,
        # which the voice endpoint turns into CLOSE_FORBIDDEN and the portal
        # into a 404. Inbound calls play a case too (blank → the first one).
        self.case = data.resolve_case(self.AGENT_KEY, call_case)
        super().__init__(self.demo.kind, environment=environment, profile_slug=profile_slug)
        self.locale = self._normalize_locale(locale)
        self.accent = accent_variables(locale)
        self.state: Dict[str, Any] = state if isinstance(state, dict) else self.new_state()
        sources = list(getattr(self.profile, "data_source_ids", None) or [])
        self.kb_source_id = kb_source_id or (sources[0] if sources else None)
        # Every persona carries the accent placeholders, so they are always
        # bound (an unbound one would be read aloud as "{arabic_dialect}").
        self.persona_variables: Dict[str, Any] = dict(self.accent)

    # --- per-agent hooks ---------------------------------------------------
    def new_state(self) -> Dict[str, Any]:
        raise NotImplementedError

    def _domain_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        raise NotImplementedError

    def _call_block(self) -> str:
        """Turn-context lines that describe THIS conversation (outbound: who we called)."""
        return ""

    def _before_turn(self, message: str, previous: List[Dict[str, Any]]) -> None:
        """Text-mode bookkeeping before the model runs (the send yes-gate)."""

    def _state_lines(self) -> List[str]:
        """What already happened in this conversation, from the call state.

        Chat history keeps text only, never earlier tool results, so without
        this the brain re-verifies every turn and forgets what it already
        sent. Facts here come from the state the tools themselves wrote."""
        return []

    # --- shared -------------------------------------------------------------
    def _fallback_system_prompt(self) -> str:
        return SYSTEM_PROMPTS[self.AGENT_KEY]

    def _build_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        return [*self._domain_tools(ctx), kb.make_kb_tool(self.kb_source_id)]

    def _code_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        """Tools bound whatever the profile attaches (none by default)."""
        return []

    def _prompt_variables(self) -> Dict[str, Any]:
        """Request-bound values for the served system prompt (none by default)."""
        return {}

    async def prefetch_voice_context(self, message: str, *, match_count: int) -> Optional[str]:
        """KB chunks for a delegated voice turn, retrieved before the brain's
        first model call (one call instead of search-then-answer). Silent on
        any failure — the tool stays bound for a miss."""
        if not self.kb_source_id:
            return None
        try:
            return await asyncio.to_thread(kb.retrieve, message, source_id=self.kb_source_id,
                                           match_count=match_count)
        except Exception as exc:  # noqa: BLE001
            logger.debug("agents demo voice prefetch skipped: %s", exc)
            return None

    def _turn_context(self, message: str, voice_turn: bool, prefetched: Optional[str]) -> str:
        call_is_ar = str(self.locale or "").startswith("ar")
        if _is_arabic(message):
            reply_ar = True
        else:
            reply_ar = call_is_ar and len(re.findall(r"[A-Za-z]", message or "")) < 10
        parts = [f"Today's date is {date.today().isoformat()} ({data.say_date(date.today().isoformat())})."
                 + ("" if getattr(self, "prompt_only", False) else " Use YYYY-MM-DD for any date you pass to a tool."),
                 "## Reply language (hard rule — overrides everything above)\n" + (
                     "The customer's message is in ARABIC — reply ONLY in Arabic (clear, friendly, "
                     f"{self.accent['arabic_dialect']} dialect), every number as numerals 0-9." if reply_ar else
                     "The customer's message is in ENGLISH — reply ONLY in English.")]
        block = self._call_block()
        if block:
            parts.append(block)
        done = self._state_lines()
        if done:
            parts.append("## Already established in this conversation (from your tools — trust it, do "
                         "not repeat these steps)\n" + "\n".join(f"- {line}" for line in done))
        if voice_turn:
            parts.append(VOICE_TURN_RULES)
        if (prefetched or "").strip():
            parts.append("## Knowledge base articles already retrieved for THIS question\n"
                         "Use them for policies and how-to answers; search again only if they do not "
                         "cover it. Plans, prices, coverage and anything about an account still come "
                         "from your tools.\n\n" + prefetched.strip())
        return "\n\n".join(parts)

    async def run_stream(self, message: str, context: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        voice_turn = bool(context.get("voice_turn"))
        previous = context.get("previous_messages", []) or []
        stage = context.get("stage_timings") if isinstance(context.get("stage_timings"), dict) else None

        def _stage_add(key: str, started: float) -> None:
            if stage is not None:
                stage[key] = round(float(stage.get(key) or 0.0) + (time.monotonic() - started) * 1000.0, 1)

        try:
            if not voice_turn:
                self._before_turn(message, previous)
            tool_ctx = {**context, "call_state": self.state, "locale": self.locale,
                        "call_case_id": (self.case or {}).get("id")}
            fallback = self._fallback_system_prompt()
            ready = self.prepare(ctx={"fallback_system_prompt": fallback,
                                      "prompt_variables": self._prompt_variables()},
                                 all_tools=self._build_tools(tool_ctx))
            system_prompt = ready["system_prompt"] or fallback
            tools, tools_by_name = ready["tools"], ready["tools_by_name"]
            # Tools the CODE requires whatever the profile attaches (bundle
            # filtering above would drop them from a tool-less profile).
            for tool in self._code_tools(tool_ctx):
                if tool.name not in tools_by_name:
                    tools = [*tools, tool]
                    tools_by_name = {**tools_by_name, tool.name: tool}
            reply_lang = "Arabic" if (_is_arabic(message) or (str(self.locale).startswith("ar")
                                                               and not re.search(r"[A-Za-z]{10,}", message or ""))) \
                else "English"

            messages = self.build_messages(previous, message,
                                           history_limit=AGENT_CONFIG.history_message_limit_sales,
                                           per_message_limit=2000)
            full_messages: List[Any] = [
                cached_system_message(system_prompt, self.profile.model_main),
                SystemMessage(content=self._turn_context(message, voice_turn, context.get("prefetched_docs"))),
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
                calls = [{"name": c.get("name", ""), "args": c.get("args") or {}, "id": c.get("id") or ""}
                         for c in end_calls if c.get("name")]
                if not calls:
                    final = content
                    # Not on a prompt-only agent: its "let me pull that up… okay"
                    # is followed by the answer itself (its only tool is the
                    # identity check), so there is no stranded lookup to force.
                    if (not announce_retry and tool_count == 0 and tools_by_name
                            and not getattr(self, "prompt_only", False)
                            and contains_announce(final) and not reply_ends_in_question(final)):
                        # A reply that announced a lookup with no tool call
                        # behind it strands the customer — one forced round.
                        announce_retry = True
                        full_messages.append(AIMessage(content=content))
                        full_messages.append(HumanMessage(content=(
                            "You said you would check but called no tool, and you get no second turn. "
                            f"Call the right tool NOW, then answer in {reply_lang}. Do not repeat the "
                            "announcement.")))
                        continue
                    break
                full_messages.append(AIMessage(content=content, tool_calls=calls))
                for c in calls:
                    yield {"type": "thought", "content": get_ui_message(
                        self.locale, "using_tool", tool_name=self._format_tool_name(c["name"]))}

                async def _invoke(c: Dict[str, Any]) -> str:
                    tool = tools_by_name.get(c["name"])
                    if tool is None:
                        return json.dumps({"ok": False, "error": f"unknown tool {c['name']}"})
                    try:
                        return await tool.ainvoke(c["args"])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("agents demo tool %s failed: %s", c["name"], exc)
                        return json.dumps({"ok": False, "error": "tool_failed",
                                           "detail": "The action did not go through; say so."})

                tool_count += len(calls)
                logger.info("agents_demo %s: tools=%s", self.demo.kind, [c["name"] for c in calls])
                started = time.monotonic()
                # Sequential, not gathered: these tools share one call-state
                # dict (verify, then act) and run in microseconds.
                results = [await _invoke(c) for c in calls]
                _stage_add("tools_ms", started)
                for c, res in zip(calls, results):
                    res_str = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)
                    record_tool_result(res_str, tool_name=c["name"])
                    full_messages.append(ToolMessage(content=self.truncate_tool_result(res_str),
                                                     tool_call_id=c["id"]))
                yield {"type": "tool_activity", "tools": [c["name"] for c in calls]}

            if not (final or "").strip():
                synth = ""
                nudge = HumanMessage(content=("Using what the tools returned above, write the final reply to "
                                              f"the customer now in {reply_lang}. Do not call any tools."))
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
            logger.warning("agents demo stopped on token budget: %s", exc)
            yield {"type": "error", "message": "Please try again with a shorter message."}
        except Exception as exc:  # noqa: BLE001
            logger.error("agents demo agent error: %s", exc, exc_info=True)
            yield {"type": "error", "message": "An error occurred. Please try again."}


def _last_assistant(previous: List[Dict[str, Any]]) -> str:
    for row in reversed(previous or []):
        if row.get("role") == "assistant":
            return str(row.get("content") or "")
    return ""


class _InboundAgent(DemoRuntimeAgent):
    """Support / inquiries: prompt-only when the profile says so (see the
    module docstring), else the delegated tool-backed brain."""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.prompt_only = profile_is_prompt_only(self.profile)
        if self.prompt_only:
            self.persona_variables = {**self.accent, **self._prompt_variables()}

    def _prompt_variables(self) -> Dict[str, Any]:
        if not getattr(self, "prompt_only", False):
            return {}
        lang = "ar" if str(self.locale).startswith("ar") else "en"
        return {"facts_sheet": inbound.facts_sheet(lang, inbound.case_country(self.AGENT_KEY, self.case)),
                "case_file": inbound.case_file(self.AGENT_KEY, self.case, lang)}

    def _fallback_system_prompt(self) -> str:
        if self.prompt_only:
            return SYSTEM_PROMPTS[self.AGENT_KEY]
        return LEGACY_INBOUND_SYSTEM_PROMPTS[self.AGENT_KEY]

    def _build_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        # Prompt-only: an EMPTY pool, so nothing binds whatever the profile
        # still has attached (a bundle re-attached by an older sibling's boot
        # seeder, or a snapshot taken before the switch).
        return [] if self.prompt_only else super()._build_tools(ctx)

    def _state_lines(self) -> List[str]:
        return [] if self.prompt_only else self._tool_state_lines()

    def _tool_state_lines(self) -> List[str]:
        return []

    def direct_voice_tools(self) -> List[Any]:
        """A prompt-only call declares NO tools (the endpoint reads an empty
        list as "no tools at all", not as "fall back to ask_agent")."""
        return []

    async def prefetch_voice_context(self, message: str, *, match_count: int) -> Optional[str]:
        if self.prompt_only:
            return None
        return await super().prefetch_voice_context(message, match_count=match_count)


class SupportAgent(_InboundAgent):
    AGENT_KEY = "support"

    def new_state(self) -> Dict[str, Any]:
        return support.new_state()

    def _tool_state_lines(self) -> List[str]:
        st, out = self.state, []
        if st.get("verified"):
            cust = data.customer(st["verified"])
            out.append(f"Caller VERIFIED as {cust['name_en']} (account {cust['account_number']}) — never ask "
                       "for verification again; call the account tools directly.")
        out += [f"Refund raised: {r['ref']}" for r in st.get("refunds", [])]
        out += [f"Technician visit booked: {b['ref']} ({b['slot_id']})" for b in st.get("bookings", [])]
        out += [f"Service change done: {c['action']} ({c['ref']})" for c in st.get("changes", [])]
        out += [f"Ticket opened: {t['ref']} ({t['category']})" for t in st.get("tickets", [])]
        out += [f"Handed off to a human: {h['ref']}" for h in st.get("handoffs", [])]
        return out

    def _domain_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        return support.create_support_tools(ctx)

    # Prompt-only still checks identity in CODE: the case file no longer
    # carries the answers (a realtime model told them judged wrong ones
    # "correct" — 2026-10-08), so the one tool a prompt-only support call
    # has is the verification itself, in text and in voice alike.
    def _code_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        if not self.prompt_only:
            return []
        return [t for t in support.create_support_tools(ctx) if t.name == "support_verify_customer"]

    def direct_voice_tools(self) -> List[Any]:
        if not self.prompt_only:
            return super().direct_voice_tools()

        def _verify(a: Dict[str, Any]) -> Dict[str, Any]:
            return support.verify_customer(
                self.state, account_number=a.get("account_number") or None, mobile=a.get("mobile") or None,
                date_of_birth=a.get("date_of_birth") or None, id_last4=a.get("id_last4") or None,
                caller_name=a.get("caller_name") or None, locale=self.locale)

        decl = {"name": "support_verify_customer",
                "description": ("Check the caller's details against our records. Call it as soon as they have "
                                "given an account number or registered mobile PLUS a date of birth or ID last 4, "
                                "BEFORE saying anything about those details; only verified true means verified."),
                "parameters": {"type": "object", "required": [], "properties": {
                    "account_number": {"type": "string", "description": "8-digit account number, as said."},
                    "mobile": {"type": "string", "description": "Registered mobile number, as said."},
                    "date_of_birth": {"type": "string", "description": "Date of birth as YYYY-MM-DD."},
                    "id_last4": {"type": "string", "description": "Last 4 digits of the national ID."},
                    "caller_name": {"type": "string", "description": "The name the caller gave, if any."}}}}
        return [(decl, _verify)]


class InquiriesAgent(_InboundAgent):
    AGENT_KEY = "inquiries"

    def new_state(self) -> Dict[str, Any]:
        return inquiries.new_state()

    def _tool_state_lines(self) -> List[str]:
        st = self.state
        return ([f"Lead saved: {lead['ref']} for {lead['name']}, mobile ending {lead['mobile'][-4:]}"
                 for lead in st.get("leads", [])]
                + [f"Callback booked: {c['ref']} on {c['date']}" + (f" at {c['time']}" if c.get("time") else "")
                   for c in st.get("callbacks", [])])

    def _domain_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        return inquiries.create_inquiries_tools(ctx)


class _OutboundAgent(DemoRuntimeAgent):
    _module: Any = None
    _reason_en = ""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        cust = data.customer_for_case(self.case)
        self.persona_variables = {
            **self.accent,
            "callee_name": cust["name_ar"] if self.locale.startswith("ar") else cust["name_en"],
            "case_file": self._module.case_file(self.case),
        }

    def new_state(self) -> Dict[str, Any]:
        return self._module.new_state()

    def _last4(self) -> str:
        return data.customer_for_case(self.case)["mobile"][-4:]

    def note_assistant_turn(self, text: str) -> None:
        """Voice edge hook: one completed assistant turn (the send yes-gate)."""
        common.note_assistant_turn(self.state, text, self._last4())

    def direct_voice_tools(self) -> List[Any]:
        """(declaration, handler) pairs for a pre-fetched-context voice call."""
        return self._module.direct_tools(self.case, self.state)

    def _before_turn(self, message: str, previous: List[Dict[str, Any]]) -> None:
        common.note_assistant_turn(self.state, _last_assistant(previous), self._last4())
        common.note_customer_turn(self.state, message)

    def _call_block(self) -> str:
        cust = data.customer_for_case(self.case)
        return ("## This call\n"
                f"You placed this call to {cust['name_en']} («{cust['name_ar']}») about {self._reason_en}. "
                "Everything else about the account comes only from your tools, after verification."
                + self._call_extra())

    def _call_extra(self) -> str:
        return ""

    def _state_lines(self) -> List[str]:
        st, out = self.state, []
        if st.get("verified"):
            out.append("The customer is VERIFIED — never ask for verification again.")
        if st.get("links"):
            out.append("The link was SENT on this call — never send it again.")
        return out


class CollectionAgent(_OutboundAgent):
    AGENT_KEY = "collection"
    _module = collection
    _reason_en = "their account (an overdue balance — never say so before verification)"

    def _domain_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        return collection.create_collection_tools(ctx)

    def _state_lines(self) -> List[str]:
        st, out = self.state, super()._state_lines()
        out += [f"Payment plan set up: {p['instalments']} instalments" for p in st.get("plans", [])]
        out += [f"Promise to pay recorded for {p['date']}" for p in st.get("promises", [])]
        out += [f"Escalated ({e['reason']}) under {e['ref']}" for e in st.get("escalations", [])]
        out += [f"Note logged: {n['category']}" for n in st.get("notes", [])]
        return out


class SalesAgent(_OutboundAgent):
    AGENT_KEY = "sales"
    _module = sales
    _reason_en = "an offer that could help them"

    def _domain_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        return sales.create_sales_tools(ctx)

    def _call_extra(self) -> str:
        offer = data.products()[self.case["offer"]]
        return (f" The ONE offer for this call is {offer['name_en']} («{offer['name_ar']}», id "
                f"{self.case['offer']}): {offer['summary_en']} Pitch only this; get its price from your tools.")

    def _state_lines(self) -> List[str]:
        st, out = self.state, super()._state_lines()
        out += [f"Outcome logged: {o['outcome']}" for o in st.get("outcomes", [])]
        out += [f"Call-back booked for {f['date']}" for f in st.get("followups", [])]
        if st.get("do_not_call"):
            out.append("They asked not to be called again — no pitch, end the call.")
        return out


AGENT_CLASSES = {
    "support": SupportAgent,
    "inquiries": InquiriesAgent,
    "collection": CollectionAgent,
    "sales": SalesAgent,
}


def create_demo_agent(agent_key: str, **kwargs: Any) -> DemoRuntimeAgent:
    """Factory used by the portal's text chat and the voice registry."""
    cls = AGENT_CLASSES.get(agent_key)
    if cls is None:
        raise LookupError(f"unknown demo agent {agent_key!r}")
    return cls(**kwargs)
