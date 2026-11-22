"""Shared target-resolution for the KB batch importers.

Both importers now write the AI-engine shared KB (ai_kb_pages /
ai_kb_embeddings), scoped by source_id. The source row is resolved by mapping
the importer's `source` tag to the seeded agent_data_sources.name, then
looking up source_id by name — so ids can differ per environment and we reuse
the proven .eq("name", ...) lookup instead of a JSONB filter.
"""
from __future__ import annotations

import os
import sys

from supabase import Client, create_client

def source_tag_to_name() -> dict[str, str]:
    """Importer `source` tag -> seeded agent_data_sources.name.

    Every knowledge source a plugin seeds (its `data_source_seeds` hook) that
    carries `config.source_tag` maps that tag to its own name. A plugin may
    add aliases (more tags landing in one source) with `kb_source_tags()`.
    A build with no plugin seeds none, so an operator's own sources are
    added in the portal or over MCP.
    """
    from core.plugins import call_hook, load_hook

    modules = load_hook("data_source_seeds")
    out: dict[str, str] = {}
    for specs in call_hook(modules, "data_source_specs"):
        for spec in specs:
            tag = (spec.get("config") or {}).get("source_tag")
            if tag:
                out[tag] = spec["name"]
    for extra in call_hook(modules, "kb_source_tags"):
        out.update(extra)
    return out


def ai_client() -> Client:
    url = os.getenv("AI_SUPABASE_URL")
    key = os.getenv("AI_SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.stderr.write("AI_SUPABASE_URL and AI_SUPABASE_SERVICE_KEY are required\n")
        raise SystemExit(2)
    return create_client(url, key)


def resolve_source_id(client: Client, source_tag: str) -> str:
    """Map a source tag to its seeded row name and return that row's source_id."""
    tags = source_tag_to_name()
    name = tags.get(source_tag)
    if not name:
        sys.stderr.write(
            f"Unknown source tag {source_tag!r}; expected one of "
            f"{sorted(tags)}\n"
        )
        raise SystemExit(3)
    rows = (
        client.table("agent_data_sources")
        .select("source_id")
        .eq("name", name)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        sys.stderr.write(
            f"No agent_data_sources row named {name!r} (tag {source_tag!r}). "
            f"Ensure seed_data_sources.py has run against this AI DB.\n"
        )
        raise SystemExit(3)
    return rows[0]["source_id"]
