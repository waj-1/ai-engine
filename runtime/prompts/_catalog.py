"""Render an agent's resolved tool set into an English markdown catalog.

Replaces the hand-written "## Tools available" blocks in the prompt files.
Injected into prompts via the `{available_tools}` placeholder by
RuntimeAgent.prepare(). Generated from each tool's `description` (which
already mirrors the old hand-written bullets), so drift from the previous
catalog is minimal. Behavioral rules ("call X first") live in the prompt
body, not here.
"""
from __future__ import annotations

import re
from typing import Iterable

from langchain_core.tools import BaseTool

_WS = re.compile(r"\s+")


def render_tool_catalog(tools: Iterable[BaseTool]) -> str:
    lines = []
    for t in tools:
        name = getattr(t, "name", None)
        if not name:
            continue
        desc = _WS.sub(" ", (getattr(t, "description", "") or "").strip()) or name
        lines.append(f"- `{name}` — {desc}")
        guidance = (getattr(t, "metadata", None) or {}).get("usage_guidance")
        if guidance:
            lines.append(f"  ↳ {_WS.sub(' ', str(guidance).strip())}")
    return "\n".join(lines)


def render_skill_catalog(entries) -> str:
    """One catalog line per SKILL_ENTRY (see core/skills.py): `slug` — description.
    Same shape as render_tool_catalog so the two sections read uniformly.
    Whitespace-collapsed; injected via literal replace/append — NEVER format_map
    (bodies and descriptions are operator text)."""
    lines = []
    for e in entries or []:
        skill = e.get("skill") or {}
        slug = skill.get("slug")
        if not slug:
            continue
        desc = _WS.sub(" ", (skill.get("description") or "").strip()) or slug
        lines.append(f"- `{slug}` — {desc}")
    return "\n".join(lines)
