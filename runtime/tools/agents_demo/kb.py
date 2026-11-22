"""Knowledge-base retrieval for the four demo agents.

The corpus is the bilingual markdown under scripts/kb_content/agents_demo/,
ingested into ONE pgvector source (`DATA_SOURCE_NAME`) by
scripts/ingest_agents_demo_kb.py. Retrieval is `match_kb_embeddings` over that
source with the engine's embedder (EMBED_PROVIDER), so a query is embedded in
the same space the corpus was.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DATA_SOURCE_NAME = "agents_demo_kb"
KB_TOOL_NAME = "agents_kb_search"

_SOURCE_CACHE: Dict[str, Any] = {"id": None, "at": 0.0}
_SOURCE_TTL_SEC = 300.0


def kb_source_id() -> Optional[str]:
    """The KB source's id (cached 5 min). None when it is not seeded yet."""
    now = time.monotonic()
    if _SOURCE_CACHE["id"] and now - _SOURCE_CACHE["at"] < _SOURCE_TTL_SEC:
        return _SOURCE_CACHE["id"]
    try:
        from core.auth import get_service_client

        rows = (get_service_client().table("agent_data_sources").select("source_id")
                .eq("name", DATA_SOURCE_NAME).limit(1).execute().data or [])
    except Exception as exc:  # noqa: BLE001 — retrieval degrades, never crashes a turn
        logger.warning("agents demo kb: source lookup failed: %s", exc)
        return None
    sid = rows[0]["source_id"] if rows else None
    _SOURCE_CACHE.update(id=sid, at=now)
    return sid


def query_language(query: str) -> str:
    """'ar' when the query has Arabic script, else 'en' — the corpus has both
    languages and an English question should get English pages."""
    return "ar" if re.search(r"[\u0600-\u06FF]", query or "") else "en"


def retrieve(query: str, *, source_id: Optional[str], match_count: int = 4) -> Optional[str]:
    """Titled chunks for ``query`` in its own language, or None when nothing
    clears the threshold.

    Raises on an embed / RPC failure — "we don't know" is not "there is no
    documentation", so the tool wrapper reports it as unavailable instead.
    """
    from core.auth import get_service_client
    from core.config import AGENT_CONFIG
    from runtime.embeddings import embed_texts

    if not source_id:
        return None
    emb = embed_texts([query])[0]
    res = get_service_client().rpc("match_kb_embeddings", {
        "p_source_ids": [source_id], "query_embedding": emb,
        "match_threshold": AGENT_CONFIG.kb_match_threshold,
        "match_count": match_count, "filter_language": query_language(query),
    }).execute()
    rows = res.data or []
    if not rows:
        return None
    parts: List[str] = []
    for r in rows:
        title, sub = r.get("page_title") or "", r.get("heading") or ""
        heading = title if (not sub or sub == title) else f"{title} — {sub}"
        parts.append((f"### {heading}\n" if heading else "") + (r.get("content") or "").strip())
    return "\n\n".join(parts)


class KbInput(BaseModel):
    query: str = Field(..., description="The customer's question in their own words (any language).")


def make_kb_tool(source_id: Optional[str]) -> StructuredTool:
    def _search(query: str) -> str:
        try:
            found = retrieve(query, source_id=source_id or kb_source_id())
        except Exception as exc:  # noqa: BLE001
            logger.warning("agents demo kb search failed: %s", exc)
            return json.dumps({"ok": False, "error": "kb_unavailable",
                               "detail": "The knowledge base is unavailable right now — say you cannot check "
                                         "that at the moment; never answer it from memory."})
        if not found:
            return json.dumps({"ok": True, "found": False,
                               "detail": "Nothing in the knowledge base covers this. Say so plainly; never "
                                         "invent a policy, price or timeline."})
        return json.dumps({"ok": True, "found": True, "articles": found}, ensure_ascii=False)

    return StructuredTool.from_function(
        func=_search, name=KB_TOOL_NAME, args_schema=KbInput,
        description=("Search the company knowledge base (plans, coverage, installation, billing, late payment, "
                     "refunds, moving, router troubleshooting, SIM/eSIM, business bundles, contracts, privacy). "
                     "Use it for any policy or how-to question; answer only from what it returns."),
    )
