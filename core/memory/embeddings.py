"""Text-embedding helper for the memory layer.

The provider is chosen by :mod:`core.embedding_provider` — Gemini's
`gemini-embedding-001` by default, the in-region Azure OpenAI
`text-embedding-3-small` when `EMBED_PROVIDER=azure`, or an OpenAI-compatible
server (`EMBED_BASE_URL` + `EMBED_MODEL`). All are asked for `EMBED_DIM` (768),
matching the `ai_user_memory.embedding vector(768)` column, so none needs a
schema change.

Flipping the provider without re-embedding what is already stored makes
recall return nothing at all — vectors from different models are not
comparable, and a similarity search reports no error when it is searching a
space the corpus was never embedded in. See the warning in
`core.embedding_provider` and `scripts/reembed.py`.

`embed_text` is sync; callers on the async hot path wrap it in
`asyncio.to_thread`. Better still, keep it off that path entirely — on the
voice edge this call sits between the caller finishing their sentence and
the model being invoked.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

EMBED_MODEL = os.getenv("MEMORY_EMBED_MODEL", "models/gemini-embedding-001")
EMBED_DIM = 768

def embed_text(text: str) -> Optional[List[float]]:
    """Return a 768-float embedding, or None on failure (caller degrades).

    Dispatches through :mod:`core.embedding_provider`, so `EMBED_PROVIDER`
    selects Gemini (default) or the in-region Azure deployment. Read the
    warning in that module before flipping it: stored vectors do NOT survive a
    provider change, and the failure is silent rather than loud.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        from core.embedding_provider import embed_batch

        vecs = embed_batch([text], dim=EMBED_DIM)
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("memory.embeddings: embed failed (%s)", exc)
        return None
    return vecs[0] if vecs else None


def to_vector_literal(embedding: List[float]) -> str:
    """Serialize a float list to a pgvector text literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"
