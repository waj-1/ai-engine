"""
Shared base for the streaming agents.

What lived here before: each streaming agent's module defined its own _normalize_content, _build_messages, _format_tool_name, and
_generate_suggestions with ~identical implementations. That copy-pasted code
drifted over time and made every small change (e.g. adjusting truncation
length or token counting) a three-file edit.

This module consolidates the shared pieces. Per-agent behavior (prompts,
toolset, tool-call display, streaming shape) stays in the concrete subclasses.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from langchain_core.messages import AIMessage, HumanMessage

from core.config import AGENT_CONFIG, SUPPORTED_LOCALES, truncate_for_llm
from core.llm_usage import extract_cache_tokens, extract_usage

logger = logging.getLogger(__name__)


class TokenBudgetExceeded(RuntimeError):
    """Raised when a single agent turn exceeds the configured token budget."""


class BaseStreamingAgent:
    """Common helpers for streaming LLM agents.

    Concrete agents own their system prompt, tool set, and run_stream body.
    They call into the helpers here for the bits that were previously
    duplicated across files.
    """

    # Subclasses override when their locale set differs.
    supported_locales = SUPPORTED_LOCALES

    def _normalize_locale(self, locale: str) -> str:
        """Portal locale -> a supported locale key, folding the region subtag.

        The membership test alone silently anglicised every REGIONED tag: the
        duplex voice edge passes the caller's BCP-47 speech tag straight
        through (`ar-SA`, `en-US` — see endpoints/voice_live.py's
        `_LOCALE_SPEECH`), and `"ar-SA" not in {"en","ar",...}` resolved to
        `"en"`. So an Arabic voice call built its brain with the ENGLISH
        prompt, tool locale and status strings, and the only thing keeping
        its answers in Arabic was the model inferring the language from the
        question — which is exactly what failed live on 2026-09-02 (an
        `en-US` call answered in Arabic, and an `ar-SA` call had its English
        question translated into an Arabic tool query).

        Fold `xx-YY` to `xx` before the check. Text chat already passes bare
        `"en"`/`"ar"`, so nothing on that path changes; an unknown language
        still falls back to English.
        """
        primary = (locale or "").strip().replace("_", "-").split("-")[0].lower()
        return primary if primary in self.supported_locales else "en"

    # -- Content normalization --------------------------------------------

    @staticmethod
    def normalize_content(content: Any) -> str:
        """LangChain/Gemini can return content as a list of parts for multimodal
        or multi-part responses; always return a string.
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text", "") or item.get("content", "")
                    if text:
                        parts.append(text)
                else:
                    parts.append(str(item))
            return "".join(parts)
        return str(content)

    # Back-compat alias — the concrete agents used to call self._normalize_content.
    def _normalize_content(self, content: Any) -> str:
        return self.normalize_content(content)

    # -- Tool name display -------------------------------------------------

    @staticmethod
    def format_tool_name(tool_name: str) -> str:
        """Strip common prefixes and underscores for human-readable display."""
        name = tool_name
        for prefix in ("get_", "list_", "search_"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        return name.replace("_", " ")

    def _format_tool_name(self, tool_name: str) -> str:
        return self.format_tool_name(tool_name)

    # -- Message history building -----------------------------------------

    def build_messages(
        self,
        previous_messages: List[Dict[str, Any]],
        current_message: str,
        *,
        history_limit: Optional[int] = None,
        per_message_limit: Optional[int] = None,
        message_prefix: Optional[str] = None,
    ) -> List[Any]:
        """Build a LangChain message list from a dict-shaped history plus the
        current user turn.

        - history_limit trims to the last N previous messages
        - per_message_limit truncates each individual message body
        - message_prefix prepends a short context note to the *current* user
          message (e.g. to emphasise the screen the user is looking at)
        """
        hist_cap = history_limit if history_limit is not None else AGENT_CONFIG.history_message_limit_main
        msg_cap = per_message_limit if per_message_limit is not None else AGENT_CONFIG.history_message_char_limit

        recent = previous_messages[-hist_cap:] if len(previous_messages) > hist_cap else previous_messages
        messages: List[Any] = []
        for msg in recent:
            role = msg.get("role", "user")
            content = msg.get("content", "") or ""
            if len(content) > msg_cap:
                content = content[:msg_cap] + "...[truncated]"
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role in ("assistant", "model"):
                messages.append(AIMessage(content=content))

        final_content = current_message
        if message_prefix:
            final_content = f"{message_prefix}\n\n{current_message}"
        messages.append(HumanMessage(content=final_content))
        return messages

    def _build_messages(
        self,
        previous_messages: List[Dict[str, Any]],
        current_message: str,
        **kwargs: Any,
    ) -> List[Any]:
        return self.build_messages(previous_messages, current_message, **kwargs)

    # -- Tool result shaping ----------------------------------------------

    @staticmethod
    def truncate_tool_result(text: str, limit: Optional[int] = None) -> str:
        """Truncate a tool payload for LLM context with a marker that signals
        to the model that data was cut (so it narrows the query instead of
        confabulating the missing tail).
        """
        return truncate_for_llm(text, limit)

    # -- Token budget -----------------------------------------------------

    @staticmethod
    def update_usage(usage_totals: Dict[str, int], output: Any) -> None:
        """Accumulate usage from a LangChain on_chat_model_end event into a
        mutable dict with keys 'input_tokens'/'output_tokens' plus cache
        counters 'cache_read_tokens'/'cache_creation_tokens'. Uses .get so a
        dict pre-seeded with only input/output keys never KeyErrors.
        """
        i, o, _t = extract_usage(output)
        cr, cc = extract_cache_tokens(output)
        usage_totals["input_tokens"] += i
        usage_totals["output_tokens"] += o
        usage_totals["cache_read_tokens"] = usage_totals.get("cache_read_tokens", 0) + cr
        usage_totals["cache_creation_tokens"] = usage_totals.get("cache_creation_tokens", 0) + cc

    @staticmethod
    def check_token_budget(
        usage_totals: Dict[str, int],
        *,
        budget: Optional[int] = None,
        warn_ratio: Optional[float] = None,
        label: str = "agent",
    ) -> Optional[str]:
        """Enforce the per-request token budget.

        Returns a short user-visible warning string once when usage crosses
        the warn threshold; raises TokenBudgetExceeded once the hard cap is
        crossed. Safe to call every loop iteration — internally idempotent.
        """
        cap = budget if budget is not None else AGENT_CONFIG.per_request_token_budget
        ratio = warn_ratio if warn_ratio is not None else AGENT_CONFIG.per_request_token_warn_ratio
        total = usage_totals.get("input_tokens", 0) + usage_totals.get("output_tokens", 0)
        if total >= cap:
            logger.error(f"{label}: token budget exceeded ({total}/{cap})")
            raise TokenBudgetExceeded(
                f"Token budget of {cap} exceeded ({total} used) — stopping agent loop."
            )
        if not usage_totals.get("_warned") and total >= int(cap * ratio):
            usage_totals["_warned"] = 1
            logger.warning(f"{label}: token usage at {total}/{cap} ({int(100 * total / cap)}%)")
            return (
                f"Approaching the per-request token budget ({total}/{cap}). "
                "Narrowing to a final answer."
            )
        return None

    # -- Suggestion parsing -----------------------------------------------

    def parse_suggestions(self, content: Any, limit: Optional[int] = None) -> List[str]:
        """Parse an LLM response into a list of short follow-up questions."""
        cap = limit if limit is not None else AGENT_CONFIG.suggestion_max
        text = self.normalize_content(content)
        if not text:
            return []
        questions: List[str] = []
        for line in text.strip().split("\n"):
            cleaned = re.sub(r"^[\d\.\-\*\•]+\s*", "", line.strip())
            if cleaned and len(cleaned) > 5:
                questions.append(cleaned)
        return questions[:cap]

    # -- Title cleanup ----------------------------------------------------

    def clean_title(self, raw: Any, fallback: str = "New Conversation") -> str:
        """Strip quotes/markdown/whitespace from a model-generated title."""
        text = self.normalize_content(raw).strip()
        text = re.sub(r'^["\'"*#`]+|["\'"*#`]+$', "", text)
        text = re.sub(r"[\n\r\t]", " ", text)
        text = " ".join(text.split())
        cap = AGENT_CONFIG.title_max_chars
        if len(text) > cap:
            text = text[: cap - 3] + "..."
        return text or fallback


def build_history_summary(
    history: Iterable[Dict[str, Any]],
    user_message: str,
    ai_response: str = "",
    *,
    max_history: int = 3,
    msg_char_cap: int = 100,
    response_char_cap: int = 500,
) -> str:
    """Build the conversation context string fed into suggestion generation."""
    parts: List[str] = []
    recent = list(history)[-max_history:] if history else []
    for msg in recent:
        role = msg.get("role", "user")
        content = (msg.get("content") or "")[:msg_char_cap]
        parts.append(f"{role}: {content}")
    parts.append(f"User question: {user_message[:200]}")
    if ai_response:
        parts.append(f"AI response: {ai_response[:response_char_cap]}")
    return "\n".join(parts)
