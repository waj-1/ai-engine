"""Guard: catch a system prompt that mandates a tool the profile never binds.

Both #1032 occurrences had the same shape — the business prompt hard-codes
"you MUST call `search_documentation`" while the resolved profile omitted that
tool, so the model called it and dispatch returned `{"error": "Tool ... not
found"}` → apology + hallucination, silently. This guard turns that class of
mismatch into a loud signal: a hard gate at promote time and an error log at
runtime.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Union

from langchain_core.tools import BaseTool

ToolOrName = Union[str, BaseTool]


def _names(items: Iterable[ToolOrName]) -> List[str]:
    out: List[str] = []
    for it in items or []:
        name = it if isinstance(it, str) else getattr(it, "name", None)
        if name:
            out.append(name)
    return out


def prompt_mandated_unbound_tools(
    prompt: str,
    known_tools: Iterable[ToolOrName],
    bound_tools: Iterable[ToolOrName],
) -> List[str]:
    """Names the prompt references that are real tools but are NOT bound.

    `known_tools` is the universe of names that count as tool references (the
    agent's tool pool / bundle members); `bound_tools` is what actually binds
    this request. A name counts as "referenced" when it appears in the prompt
    as a whole word (tool python_names are distinctive snake_case identifiers,
    so a word-boundary match is reliable and dodges substring false positives).
    Returns the sorted, de-duplicated referenced-but-unbound names.
    """
    known = set(_names(known_tools))
    bound = set(_names(bound_tools))
    text = prompt or ""
    referenced = {
        name for name in known
        if re.search(rf"\b{re.escape(name)}\b", text)
    }
    return sorted(referenced - bound)
