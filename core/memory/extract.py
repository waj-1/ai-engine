# ai_agent/core/memory/extract.py
"""Light-model fact extraction for long-term memory."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage

from core.memory.prompts import EXTRACTION_PROMPT

logger = logging.getLogger(__name__)


def content_to_text(content: Any) -> str:
    """Flatten a LangChain message ``content`` to plain text.

    Gemini 3 (and other thinking models) return ``content`` as a list of
    blocks like ``[{"type": "text", "text": "..."}]`` rather than a string.
    The old ``str(content)`` stringified the Python repr, which then failed
    JSON parsing — silently dropping every extracted memory / summary. Join the
    text of any text blocks instead.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


def _parse_facts(raw: str) -> List[Dict[str, Any]]:
    """Parse the model's JSON, tolerating code fences / surrounding prose."""
    text = (raw or "").strip()
    if not text:
        return []
    # Strip ```json fences if present.
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: grab the first {...} block.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    facts = data.get("facts") if isinstance(data, dict) else None
    return facts if isinstance(facts, list) else []


async def extract_facts(
    light_llm,
    user_message: str,
    assistant_response: str,
    extraction_cfg: Dict[str, Any],
    prompt: str = EXTRACTION_PROMPT,
) -> List[Dict[str, Any]]:
    """Return [{"content": str, "importance": float}] above threshold, capped.

    `prompt` is the agent's extraction prompt (default: EXTRACTION_PROMPT).
    Never raises — returns [] on any failure (caller degrades to no-write).
    """
    threshold = float(extraction_cfg.get("importance_threshold", 0.5))
    cap = int(extraction_cfg.get("max_per_turn", 5))
    prompt = prompt.format(
        user_message=(user_message or "")[:2000],
        assistant_response=(assistant_response or "")[:2000],
    )
    try:
        resp = await light_llm.ainvoke([HumanMessage(content=prompt)])
    except Exception as exc:
        logger.warning("memory.extract: llm failed (%s)", exc)
        return []

    content = content_to_text(resp.content)
    out: List[Dict[str, Any]] = []
    for f in _parse_facts(content):
        text = (f.get("content") or "").strip() if isinstance(f, dict) else ""
        if not text:
            continue
        try:
            importance = float(f.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        if importance >= threshold:
            out.append({"content": text, "importance": importance})
    return out[:cap]
