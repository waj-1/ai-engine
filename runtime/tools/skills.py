"""Synthetic `read_skill` tool — the progressive-disclosure fetch path.

Not a pool tool: RuntimeAgent.prepare() binds it directly (after MCP
resolution) whenever the resolved skill set is non-empty. It closes over
the already-resolved SKILL_ENTRY list (core/skills.py), so a call does no
DB work — platform entries come from the profile/snapshot, tenant entries
from the 60s cache. Tool RESULTS bypass every prompt format pass, so
arbitrary `{...}` in skill bodies is safe here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

READ_SKILL_TOOL_NAME = "read_skill"


class _ReadSkillArgs(BaseModel):
    slug: str = Field(description="The skill's slug, exactly as listed in the Skills catalog.")
    file: Optional[str] = Field(
        default=None,
        description="Optional bundled reference filename to read instead of the skill body.",
    )


def build_read_skill_tool(entries: List[Dict[str, Any]]) -> StructuredTool:
    by_slug: Dict[str, Dict[str, Any]] = {}
    for e in entries or []:
        slug = (e.get("skill") or {}).get("slug")
        if slug:
            by_slug.setdefault(slug, e)

    def _read(slug: str, file: Optional[str] = None) -> str:
        entry = by_slug.get(slug)
        if entry is None:
            known = ", ".join(sorted(by_slug)) or "(none)"
            return f"Unknown skill '{slug}'. Available skills: {known}"
        resources = entry.get("resources") or []
        if file:
            for res in resources:
                if res.get("filename") == file:
                    return res.get("content") or ""
            names = ", ".join(r.get("filename") or "?" for r in resources) or "(none)"
            return f"Skill '{slug}' has no file '{file}'. Bundled files: {names}"
        body = (entry.get("version") or {}).get("content") or ""
        parts = [body or f"Skill '{slug}' has no published content yet."]
        if resources:
            names = "\n".join(f"- {r.get('filename')}" for r in resources)
            parts.append(
                "Bundled reference files (read one with read_skill(slug, file)):\n" + names
            )
        for ds in entry.get("linked_data_sources") or []:
            parts.append(
                f"Linked knowledge base: {ds.get('name')} — consult it with your "
                "KB search tool when this skill needs source material."
            )
        return "\n\n".join(parts)

    return StructuredTool.from_function(
        func=_read,
        name=READ_SKILL_TOOL_NAME,
        description=(
            "Read the full instructions of a skill listed in the Skills catalog. "
            "Call this BEFORE acting on any request a skill covers, then follow "
            "the returned instructions. Pass `file` to read a bundled reference file."
        ),
        args_schema=_ReadSkillArgs,
    )
