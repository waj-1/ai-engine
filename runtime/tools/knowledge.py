"""Knowledge-base search for the generic (`custom`) agent.

One tool, `search_knowledge`, over whatever knowledge sources the agent's
profile attaches (`agent_profiles.data_source_ids`). Nothing here names a
tenant, a product or a language: the sources decide what can be found.

No language filter is passed to `match_kb_embeddings`. The embedding models
the engine supports are multilingual, so an Arabic question finds an English
article and the other way round; filtering by the question's language would
hide the only article that answers it whenever the corpus is in one language.
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Sequence

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

KNOWLEDGE_TOOL_NAME = "search_knowledge"
DEFAULT_MATCH_COUNT = 4


def retrieve(query: str, *, source_ids: Sequence[str], match_count: int = DEFAULT_MATCH_COUNT) -> Optional[str]:
    """Titled chunks answering ``query``, or None when nothing clears the threshold.

    Raises on an embedding or RPC failure: "the search failed" is not "there is
    nothing on this", and the tool wrapper reports the two differently.
    Synchronous (embedding + supabase-py) — call it through asyncio.to_thread
    from async code.
    """
    from core.auth import get_service_client
    from core.config import AGENT_CONFIG
    from runtime.embeddings import embed_texts

    ids = [str(s) for s in (source_ids or []) if s]
    if not ids or not (query or "").strip():
        return None
    embedding = embed_texts([query])[0]
    rows = get_service_client().rpc("match_kb_embeddings", {
        "p_source_ids": ids,
        "query_embedding": embedding,
        "match_threshold": AGENT_CONFIG.kb_match_threshold,
        "match_count": match_count,
        "filter_language": None,
    }).execute().data or []
    if not rows:
        return None
    parts: List[str] = []
    for row in rows:
        title, sub = row.get("page_title") or "", row.get("heading") or ""
        heading = title if (not sub or sub == title) else f"{title} — {sub}"
        parts.append((f"### {heading}\n" if heading else "") + (row.get("content") or "").strip())
    return "\n\n".join(parts)


class KnowledgeInput(BaseModel):
    query: str = Field(..., description="The question to look up, in the user's own words (any language).")


def make_knowledge_tool(source_ids: Sequence[str]) -> StructuredTool:
    """The `search_knowledge` tool bound to ``source_ids``."""
    ids = [str(s) for s in (source_ids or []) if s]

    def _search(query: str) -> str:
        try:
            found = retrieve(query, source_ids=ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge search failed: %s", exc)
            return json.dumps({
                "ok": False, "error": "knowledge_unavailable",
                "detail": "The knowledge base is unavailable right now. Say you cannot check that "
                          "at the moment; never answer it from memory.",
            })
        if not found:
            return json.dumps({
                "ok": True, "found": False,
                "detail": "Nothing in the knowledge base covers this. Say so plainly; never invent "
                          "a policy, price or timeline.",
            })
        return json.dumps({"ok": True, "found": True, "articles": found}, ensure_ascii=False)

    return StructuredTool.from_function(
        func=_search, name=KNOWLEDGE_TOOL_NAME, args_schema=KnowledgeInput,
        description=("Search this agent's knowledge base. Use it for any question about policies, "
                     "products, prices or how something works, and answer only from what it returns."),
    )


def knowledge_tools_for(profile: Any) -> List[StructuredTool]:
    """[`search_knowledge`] when the profile attaches a knowledge source, else []."""
    ids = list(getattr(profile, "data_source_ids", None) or [])
    return [make_knowledge_tool(ids)] if ids else []
