"""Which service turns text into vectors — Gemini (default) or Azure OpenAI.

WHY THIS EXISTS

Both embedding call sites (`core.memory.embeddings` at 768-dim for user
memory, `runtime.embeddings` at 1536-dim for the KB) hardcoded Gemini. From
the Azure-hosted engine that is a CROSS-CLOUD round trip to
`generativelanguage.googleapis.com`, and on the voice edge it sits on the
answer's critical path: a live demo call on 2026-08-05 spent four
embed -> match -> write cycles inside one 5.6s turn while the caller sat in
silence. Azure OpenAI's `text-embedding-3-small` is deployed in the SAME
region as the engine (`uaenorth`), so the same call stays inside the region.

`text-embedding-3-small` supports the `dimensions` parameter natively, so it
can serve both existing column widths — `ai_user_memory.embedding vector(768)`
and `ai_kb_embeddings.embedding vector(1536)` — with no migration of the
SCHEMA.

    !!  THE DATA IS ANOTHER MATTER  !!

Embeddings from different models are NOT comparable. Cosine similarity
between a Gemini vector and an Azure vector is noise. Flipping the provider
therefore does not degrade retrieval gracefully — every stored vector must be
re-embedded with the new provider or it simply stops matching, silently and
with no error, because a similarity search always returns *something* (or
nothing) rather than failing.

That is why the default here stays `google`. Flipping `EMBED_PROVIDER=azure`
is a two-step operation:

  1. re-embed every stored vector with the new provider
     (`scripts/reembed.py --target memory|kb`), then
  2. set the env var, so reads and writes agree.

Doing step 2 alone leaves a corpus that returns confident nonsense — the KB
answers docs questions from whatever happens to be nearest in a space it was
never embedded in, and user memory recalls nothing at all.
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

__all__ = ["ConfigError", "embed_batch", "active_provider"]

# Batch cap. Gemini's endpoint takes a list; Azure's takes up to 2048 inputs
# but bills per token either way, so the cap is about keeping a retry cheap
# rather than about a hard service limit.
_BATCH_SIZE = 100

_AZURE_DEPLOYMENT = os.getenv("AZURE_EMBED_DEPLOYMENT", "text-embedding-3-small")
_GOOGLE_MODEL = os.getenv("EMBED_MODEL_GOOGLE", "gemini-embedding-001")


class ConfigError(RuntimeError):
    """Required configuration (API key, endpoint) is missing."""


def active_provider() -> str:
    """`azure` or `google` (the default). Read per call, not cached.

    Not cached on purpose: this is read on cold paths only (a client is built
    lazily and cached by the caller), and a cached value would make the env
    var untestable without a process restart.
    """
    return (os.getenv("EMBED_PROVIDER") or "google").strip().lower()


# --------------------------------------------------------------------------
# Azure OpenAI
# --------------------------------------------------------------------------

_azure_client = None


def _get_azure_client():
    global _azure_client
    if _azure_client is None:
        from openai import AzureOpenAI

        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        if not endpoint or not api_key:
            raise ConfigError(
                "EMBED_PROVIDER=azure needs AZURE_OPENAI_ENDPOINT and "
                "AZURE_OPENAI_API_KEY"
            )
        _azure_client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            # Embeddings have been stable across versions; pin to the same
            # default the rest of the stack uses so one env var moves them all.
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
    return _azure_client


def _embed_azure(texts: List[str], dim: int) -> List[List[float]]:
    client = _get_azure_client()
    resp = client.embeddings.create(
        model=_AZURE_DEPLOYMENT,
        input=texts,
        # Native Matryoshka support — ask for the column's width directly
        # rather than truncating afterwards.
        dimensions=dim,
    )
    # `data` is not guaranteed ordered by the API contract; sort by index.
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


# --------------------------------------------------------------------------
# Google Gemini (default, unchanged behaviour)
# --------------------------------------------------------------------------


def _embed_google(texts: List[str], dim: int) -> List[List[float]]:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ConfigError("GOOGLE_API_KEY is not set; cannot embed")
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - dependency-absent path
        raise RuntimeError("install google-generativeai to embed") from exc

    client = genai.Client(api_key=api_key)
    resp = client.models.embed_content(
        model=_GOOGLE_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(output_dimensionality=dim),
    )
    out = [list(e.values) for e in resp.embeddings]
    # Some client versions ignore output_dimensionality and return the native
    # 3072-dim vector, which a narrower column rejects outright.
    # gemini-embedding-001 is Matryoshka, so the leading `dim` components are a
    # valid lower-dim embedding, and cosine (the RPC's operator) is
    # scale-invariant — no renormalization needed.
    return [v[:dim] if len(v) > dim else v for v in out]


# --------------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    if any(s in name for s in ("rate", "deadline", "timeout", "connection")):
        return True
    msg = str(exc).lower()
    return any(
        s in msg
        for s in ("rate limit", "timeout", "429", "500", "502", "503", "504", "deadline")
    )


def embed_batch(
    texts: List[str],
    *,
    dim: int,
    retries: int = 3,
    provider: Optional[str] = None,
) -> List[List[float]]:
    """Embed `texts` at `dim` dimensions through the configured provider.

    `provider` overrides the `EMBED_PROVIDER` dispatch (`"azure"` or
    `"google"`) for callers that carry their own selector — today the docs
    query path in `runtime.tools.business._embed_query_for_search`, whose
    `DOCS_EMBEDDING_PROVIDER` may legitimately differ from the engine-wide
    setting. Omit it everywhere else so reads and writes keep agreeing.

    Raises rather than returning partial output: a caller that stored a short
    batch as if it were the whole thing would corrupt the corpus far more
    quietly than a failed ingest does.
    """
    if not texts:
        return []
    provider = provider or active_provider()
    fn = _embed_azure if provider == "azure" else _embed_google

    out: List[List[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[start:start + _BATCH_SIZE]
        last: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                out.extend(fn(batch, dim))
                last = None
                break
            except Exception as exc:  # noqa: BLE001 - retry shim
                last = exc
                if attempt == retries - 1 or not _is_transient(exc):
                    break
                sleep_for = 2 ** attempt
                logger.warning(
                    "embed_batch(%s): transient failure (%s), retrying in %ss",
                    provider, exc, sleep_for,
                )
                time.sleep(sleep_for)
        if last is not None:
            raise last
    return out
