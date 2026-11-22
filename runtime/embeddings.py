"""
Text chunking + Gemini embedding helpers used by the admin KB ingestion path.

Two responsibilities, kept here so the admin handlers don't grow a tail of
text-processing logic:

  * :func:`chunk_text` — slice plain text into overlapping windows that fit
    inside an embedding model's context budget.
  * :func:`embed_texts` — batch-embed a list of strings via Gemini's
    ``gemini-embedding-001`` model at the same 1536-dim output the retriever
    queries against (see :mod:`runtime.tools.business._embed_query_for_search`).

Both functions tolerate missing optional dependencies: ``tiktoken`` and
``google-generativeai`` are imported lazily so this module imports cleanly in
environments (CI, lint) that don't ship them. The embed call raises a clear
``RuntimeError`` instead of an ``ImportError`` so callers can surface a
useful message to operators.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


__all__ = ["ConfigError", "chunk_text", "embed_texts"]


class ConfigError(RuntimeError):
    """Raised when required configuration (API keys, etc.) is missing."""


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


def _try_tiktoken_encoder():
    """Return a tiktoken encoder for gpt-3.5-turbo, or None if unavailable.

    tiktoken is an optional dependency — when it's not installed we silently
    fall back to a word-count heuristic. Network calls are never made.
    """
    try:
        import tiktoken  # type: ignore
    except Exception:  # pragma: no cover - exercised only when tiktoken is absent
        return None
    try:
        return tiktoken.encoding_for_model("gpt-3.5-turbo")
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


_ENCODER = _try_tiktoken_encoder()


def _count_tokens(text: str) -> int:
    """Token count via tiktoken when present, else a 0.75-words-per-token shim."""
    if _ENCODER is not None:
        try:
            return len(_ENCODER.encode(text))
        except Exception:
            pass
    # Rough heuristic: GPT-style tokens average ~0.75 words. Word count
    # over-estimates slightly which is the safe direction (smaller chunks).
    words = re.findall(r"\S+", text)
    return max(1, int(len(words) / 0.75)) if words else 0


def _encode(text: str) -> List[int]:
    """Tokenize to ids when tiktoken is available, else split on whitespace.

    We never expose the raw ids — they're an opaque sequence the chunker
    slices on. Falling back to whitespace tokens keeps the slicing math
    identical for the non-tiktoken path.
    """
    if _ENCODER is not None:
        try:
            return list(_ENCODER.encode(text))
        except Exception:
            pass
    # Word tokens — return indices into a parallel word list via a sentinel
    # type. The caller only uses these for slicing.
    return list(range(len(re.findall(r"\S+", text))))


def _decode(text: str, start: int, end: int) -> str:
    """Inverse of :func:`_encode`, returning the substring for tokens[start:end]."""
    if _ENCODER is not None:
        try:
            ids = _ENCODER.encode(text)
            return _ENCODER.decode(ids[start:end])
        except Exception:
            pass
    words = re.findall(r"\S+", text)
    return " ".join(words[start:end])


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------


def _slugify(source: str) -> str:
    """First 5 words of ``source`` lowercased + underscored, ascii-only."""
    if not source:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9\s]+", " ", source).strip().lower()
    words = cleaned.split()[:5]
    return "_".join(words)


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    *,
    max_tokens: int = 512,
    overlap_tokens: int = 64,
    heading: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Chunk ``text`` into overlapping windows for embedding.

    Each chunk is a dict ``{content, heading, slug, token_count}``.

    ``max_tokens`` sets the window size; ``overlap_tokens`` is how many
    tokens at the tail of one chunk are repeated at the head of the next.
    Both default to the values used by the existing docs importer so chunks
    line up with what the retriever was trained against.

    The ``heading`` argument is recorded verbatim on every chunk and used to
    derive the slug. When omitted, the first non-empty line of ``text``
    fills in.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must satisfy 0 <= overlap < max_tokens")

    stripped = (text or "").strip()
    if not stripped:
        return []

    effective_heading = heading
    if not effective_heading:
        first_line = next((ln.strip() for ln in stripped.splitlines() if ln.strip()), "")
        effective_heading = first_line[:120]
    slug = _slugify(effective_heading) or _slugify(stripped[:120])

    token_ids = _encode(stripped)
    total = len(token_ids)
    chunks: List[Dict[str, Any]] = []

    if total <= max_tokens:
        chunks.append(
            {
                "content": stripped,
                "heading": effective_heading,
                "slug": slug,
                "token_count": _count_tokens(stripped),
            }
        )
        return chunks

    step = max_tokens - overlap_tokens
    start = 0
    while start < total:
        end = min(start + max_tokens, total)
        piece = _decode(stripped, start, end).strip()
        if piece:
            chunks.append(
                {
                    "content": piece,
                    "heading": effective_heading,
                    "slug": slug,
                    "token_count": _count_tokens(piece),
                }
            )
        if end >= total:
            break
        start += step
    return chunks


# ---------------------------------------------------------------------------
# embed_texts (Gemini)
# ---------------------------------------------------------------------------


# Gemini's embed_content endpoint accepts a list; we cap our batch at 100 to
# stay well inside published quota and keep retries cheap.
_BATCH_SIZE = 100
_OUTPUT_DIM = 1536
_DEFAULT_MODEL = "gemini-embedding-001"


def _is_transient_error(exc: BaseException) -> bool:
    """Best-effort classification — retry on rate limits and 5xx-ish errors."""
    name = type(exc).__name__.lower()
    if "rate" in name or "deadline" in name or "timeout" in name:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("rate limit", "timeout", "500", "502", "503", "504", "deadline"))


def embed_texts(
    texts: List[str],
    *,
    model: str = _DEFAULT_MODEL,
) -> List[List[float]]:
    """Batch-embed ``texts``, returning one 1536-dim vector each.

    Dispatches through :mod:`core.embedding_provider`: Gemini's
    ``gemini-embedding-001`` by default, or the in-region Azure OpenAI
    ``text-embedding-3-small`` when ``EMBED_PROVIDER=azure``. Either way the
    width is ``_OUTPUT_DIM`` (1536), matching both the
    ``ai_kb_embeddings.embedding vector(1536)`` column and what
    :func:`runtime.tools.business._embed_query_for_search` asks for at query
    time.

    This function serves BOTH ingest and query. That is what makes the
    provider switch safe to reason about — the two sides cannot drift apart
    within one deployment — and also what makes flipping it mid-corpus unsafe:
    queries would be embedded by the new provider against pages embedded by
    the old one, which returns confident nonsense rather than an error. See
    the warning in :mod:`core.embedding_provider`.

    The ``model`` argument is retained for call-site compatibility and applies
    to the Gemini path only; the Azure deployment is named by
    ``AZURE_EMBED_DEPLOYMENT``.

    Raises :class:`ConfigError` when the selected provider's credentials are
    missing. Transient API failures are retried with exponential backoff.
    """
    if not texts:
        return []

    from core.embedding_provider import ConfigError as _ProviderConfigError
    from core.embedding_provider import embed_batch

    try:
        return embed_batch(texts, dim=_OUTPUT_DIM)
    except _ProviderConfigError as exc:
        # Re-raise as THIS module's ConfigError: callers (the admin KB
        # handlers) already catch that name to show operators a useful
        # "configure your key" message rather than a stack trace.
        raise ConfigError(str(exc)) from exc
