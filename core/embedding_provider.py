"""Which service turns text into vectors: Gemini (default), Azure OpenAI, or
any OpenAI-compatible server (Ollama, vLLM, LM Studio, a hosted catalog).

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

THE OPENAI-COMPATIBLE PROVIDER (no cloud key needed)

`EMBED_PROVIDER=openai_compatible` calls `POST {EMBED_BASE_URL}/embeddings`
with `EMBED_MODEL`. The base URL falls back to `OLLAMA_BASE_URL`; the key, if
the server wants one, is read from the variable NAMED by `EMBED_API_KEY_ENV`
(default `EMBED_API_KEY`). With `EMBED_PROVIDER` unset, setting `EMBED_MODEL`
(and a base URL) selects this provider, so an install with no cloud key gets
working knowledge bases from two settings.

The same warning applies, with one more way to trip it: changing `EMBED_MODEL`
is a provider change. Re-embed first.

Width. The columns are fixed (`ai_kb_embeddings` 1536, `ai_user_memory` 768)
and a local model's width is whatever it is, so the first answer for each
column is checked against it:

  * equal: stored as is.
  * narrower (`nomic-embed-text` is 768 against the 1536 column): padded with
    zeros. Both columns are searched by cosine distance, and trailing zeros
    change neither a dot product nor a norm, so every similarity is exactly
    what it would be at the model's own width.
  * wider: the request is repeated with `dimensions=<column width>`, which a
    Matryoshka model honours. If the answer is still not the column's width
    the call fails, naming the model and both widths. Nothing is truncated
    here: cutting a vector is only valid for models trained for it, and the
    server is the one that knows.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "ConfigError", "EmbeddingError", "EmbeddingWidthError",
    "OPENAI_COMPATIBLE", "active_provider", "embed_batch",
    "embedding_identity", "embedding_status",
]

OPENAI_COMPATIBLE = "openai_compatible"
# What an operator might reasonably type for it.
_OPENAI_COMPATIBLE_ALIASES = frozenset(
    {OPENAI_COMPATIBLE, "openai-compatible", "openai", "ollama", "local"}
)
_DEFAULT_KEY_ENV = "EMBED_API_KEY"

# Batch cap. Gemini's endpoint takes a list; Azure's takes up to 2048 inputs
# but bills per token either way, so the cap is about keeping a retry cheap
# rather than about a hard service limit.
_BATCH_SIZE = 100

_AZURE_DEPLOYMENT = os.getenv("AZURE_EMBED_DEPLOYMENT", "text-embedding-3-small")
_GOOGLE_MODEL = os.getenv("EMBED_MODEL_GOOGLE", "gemini-embedding-001")


class ConfigError(RuntimeError):
    """Required configuration (API key, endpoint) is missing."""


class EmbeddingError(RuntimeError):
    """The embedding server answered with an error, or not at all.

    `transient` says whether trying again can help (no answer, a timeout,
    429, 5xx) or cannot (unknown model, bad key, malformed answer).
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class EmbeddingWidthError(ConfigError):
    """The model's vectors do not fit the column they are meant for."""


def active_provider() -> str:
    """`google` (the default), `azure` or `openai_compatible`. Read per call.

    Not cached on purpose: this is read on cold paths only (a client is built
    lazily and cached by the caller), and a cached value would make the env
    var untestable without a process restart.

    `EMBED_PROVIDER` wins when set. Unset, `EMBED_MODEL` together with a base
    URL selects the OpenAI-compatible provider: those settings are read by
    nothing else, so setting them is the operator saying which embedder to
    use. The choice deliberately does not look at which cloud keys are
    present: an install that embedded its documents with a local model must
    not start embedding questions with Gemini the day someone adds a Gemini
    key for a chat model.
    """
    raw = (os.getenv("EMBED_PROVIDER") or "").strip().lower()
    if raw:
        return OPENAI_COMPATIBLE if raw in _OPENAI_COMPATIBLE_ALIASES else raw
    if _oc_model() and _oc_base_url():
        return OPENAI_COMPATIBLE
    return "google"


# --------------------------------------------------------------------------
# Azure OpenAI
# --------------------------------------------------------------------------

_azure_client = None
_azure_client_key = None


def _get_azure_client():
    global _azure_client, _azure_client_key
    # A key stored on Settings > Model keys wins over the variable, and a
    # replaced key builds a new client.
    from core.provider_keys import env_or_stored

    api_key = env_or_stored("AZURE_OPENAI_API_KEY")
    if _azure_client is None or api_key != _azure_client_key:
        from openai import AzureOpenAI

        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
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
        _azure_client_key = api_key
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
    from core.provider_keys import env_or_stored

    api_key = env_or_stored("GOOGLE_API_KEY")
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
# Any OpenAI-compatible server: POST {base_url}/embeddings
# --------------------------------------------------------------------------

# Tests swap in an `httpx.MockTransport`; None means a real connection.
_oc_transport = None
_oc_clients: Dict[str, object] = {}
# (base_url, model, column width) -> whether to send `dimensions`. Filled at
# first use, when the model's own width is seen for the first time.
_oc_send_dimensions: Dict[Tuple[str, str, int], bool] = {}
# Says once per (model, width) that vectors are being padded.
_oc_pad_logged: set = set()


def _oc_base_url() -> str:
    return ((os.getenv("EMBED_BASE_URL") or "").strip()
            or (os.getenv("OLLAMA_BASE_URL") or "").strip()).rstrip("/")


def _oc_model() -> str:
    return (os.getenv("EMBED_MODEL") or "").strip()


def _oc_key_env() -> str:
    return (os.getenv("EMBED_API_KEY_ENV") or "").strip() or _DEFAULT_KEY_ENV


def _oc_api_key() -> str:
    """The server's key, from the variable `EMBED_API_KEY_ENV` names.

    A key saved in the portal would be looked up HERE, before the
    environment: this is the one place the provider learns its credential.
    """
    return (os.getenv(_oc_key_env()) or "").strip()


def _is_local_endpoint(base_url: str) -> bool:
    """True for a loopback, private-network or single-label host.

    The same rule `runtime.llm_factory` applies to a chat model's base URL;
    repeated because this module stays importable with the stdlib alone.
    """
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal") or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _display_url(url: str) -> str:
    """scheme://host[:port]/path with any `user:password@` removed."""
    from urllib.parse import urlparse

    try:
        parts = urlparse(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}{parts.path}" if host else url
    except ValueError:
        return url


def _oc_problem() -> Optional[str]:
    """Why the OpenAI-compatible provider cannot be used, in plain words."""
    base_url, model = _oc_base_url(), _oc_model()
    if not base_url and not model:
        return ("Set EMBED_BASE_URL to the server's address (for Ollama: "
                "http://127.0.0.1:11434/v1) and EMBED_MODEL to the embedding "
                "model's name (for example nomic-embed-text).")
    if not base_url:
        return ("Set EMBED_BASE_URL to the embedding server's address (for "
                "Ollama: http://127.0.0.1:11434/v1).")
    if not model:
        return ("Set EMBED_MODEL to the embedding model's name (for example "
                "nomic-embed-text).")
    if not _oc_api_key() and not _is_local_endpoint(base_url):
        return (f"{_display_url(base_url)} is not on this machine or network, "
                f"so it needs a key: set {_oc_key_env()}.")
    return None


def _oc_client(base_url: str):
    import httpx

    client = _oc_clients.get(base_url)
    if client is None:
        client = httpx.Client(
            transport=_oc_transport,
            timeout=httpx.Timeout(float(os.getenv("EMBED_TIMEOUT_SECONDS") or 60), connect=5.0),
        )
        _oc_clients[base_url] = client
    return client


def _oc_post(texts: List[str], *, dimensions: Optional[int]) -> List[List[float]]:
    """One `/embeddings` request. Returns one vector per text, in order."""
    import httpx

    base_url, model = _oc_base_url(), _oc_model()
    where = _display_url(base_url)
    body: Dict[str, object] = {"model": model, "input": texts, "encoding_format": "float"}
    if dimensions:
        body["dimensions"] = dimensions
    headers = {"Content-Type": "application/json"}
    key = _oc_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        resp = _oc_client(base_url).post(f"{base_url}/embeddings", json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise EmbeddingError(
            f"The embedding server at {where} did not answer in time (timeout) "
            f"for model {model!r}.", transient=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise EmbeddingError(
            f"Could not reach the embedding server at {where} "
            f"(connection failed: {type(exc).__name__}). Is it running, and is "
            f"EMBED_BASE_URL right?", transient=True,
        ) from exc
    if resp.status_code >= 400:
        raise EmbeddingError(
            f"The embedding server at {where} refused model {model!r} "
            f"(HTTP {resp.status_code}): {_oc_error_text(resp)}",
            transient=resp.status_code == 429 or resp.status_code >= 500,
        )
    try:
        data = resp.json()["data"]
        # `data` is not guaranteed ordered by the API contract; sort by index.
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vectors = [[float(x) for x in d["embedding"]] for d in ordered]
    except (ValueError, KeyError, TypeError) as exc:
        raise EmbeddingError(
            f"The embedding server at {where} sent an answer that is not an "
            f"OpenAI-style embeddings response for model {model!r}."
        ) from exc
    if len(vectors) != len(texts):
        raise EmbeddingError(
            f"The embedding server at {where} returned {len(vectors)} vectors "
            f"for {len(texts)} texts (model {model!r})."
        )
    return vectors


def _oc_error_text(resp) -> str:
    """The server's own words for a failure, kept short."""
    try:
        payload = resp.json()
        err = payload.get("error", payload) if isinstance(payload, dict) else payload
        text = err.get("message") if isinstance(err, dict) else err
        text = str(text or "")
    except ValueError:
        text = resp.text or ""
    return text.strip()[:300] or "no details given"


def _embed_openai_compatible(texts: List[str], dim: int) -> List[List[float]]:
    problem = _oc_problem()
    if problem:
        raise ConfigError(f"The embedding model is not set up. {problem}")
    base_url, model = _oc_base_url(), _oc_model()
    cache_key = (base_url, model, dim)

    send_dimensions = _oc_send_dimensions.get(cache_key)
    vectors = _oc_post(texts, dimensions=dim if send_dimensions else None)
    width = len(vectors[0])
    if width > dim and send_dimensions is None:
        # First sight of a model wider than the column: ask the server to
        # produce the column's width itself.
        native = width
        try:
            vectors = _oc_post(texts, dimensions=dim)
        except EmbeddingError as exc:
            raise EmbeddingWidthError(_width_message(model, native, dim) + f" ({exc})") from exc
        if any(len(v) != dim for v in vectors):
            raise EmbeddingWidthError(_width_message(model, native, dim))
        send_dimensions = True
    _oc_send_dimensions[cache_key] = bool(send_dimensions)

    out: List[List[float]] = []
    for vec in vectors:
        if len(vec) > dim or len(vec) != len(vectors[0]) or not vec:
            raise EmbeddingWidthError(_width_message(model, len(vec), dim))
        if len(vec) < dim:
            if cache_key not in _oc_pad_logged:
                _oc_pad_logged.add(cache_key)
                logger.info(
                    "embedding model %s returns %d numbers per text; storing them "
                    "zero-padded to the column's %d (cosine similarity is unchanged)",
                    model, len(vec), dim,
                )
            vec = vec + [0.0] * (dim - len(vec))
        out.append(vec)
    return out


def _width_message(model: str, got: int, want: int) -> str:
    return (
        f"Embedding model {model!r} returns vectors {got} numbers wide, but the "
        f"column they are stored in is {want} wide, and the server did not "
        f"shorten them when asked (dimensions={want}). Nothing was stored or "
        f"searched. Choose a model that is at most {want} wide, or one that "
        f"supports the `dimensions` setting."
    )


# --------------------------------------------------------------------------
# What is in force, for the portal and for stamping a corpus
# --------------------------------------------------------------------------


def embedding_identity() -> str:
    """`provider:model` for the embedder in force, e.g. `google:gemini-embedding-001`.

    Written next to a corpus when it is embedded, and compared with the value
    in force later: a difference means the stored vectors and the questions
    are no longer in the same space.
    """
    provider = active_provider()
    if provider == OPENAI_COMPATIBLE:
        return f"{provider}:{_oc_model() or 'unset'}"
    if provider == "azure":
        return f"azure:{_AZURE_DEPLOYMENT}"
    return f"google:{_GOOGLE_MODEL}"


def embedding_status() -> Dict[str, object]:
    """What the portal shows about embeddings. No network call.

    `ready` is whether the settings the provider needs are present, not
    whether the server answers: that is only known by calling it.
    """
    provider = active_provider()
    out: Dict[str, object] = {
        "provider": provider, "identity": embedding_identity(),
        "ready": True, "problem": None, "base_url": None, "key_env": None,
    }
    if provider == OPENAI_COMPATIBLE:
        out.update(label="OpenAI-compatible server", model=_oc_model() or None,
                   base_url=_display_url(_oc_base_url()) if _oc_base_url() else None,
                   key_env=_oc_key_env(), problem=_oc_problem())
    elif provider == "azure":
        out.update(label="Azure OpenAI", model=_AZURE_DEPLOYMENT)
        if not (os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY")):
            out["problem"] = "Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY."
    else:
        out.update(label="Google Gemini", model=_GOOGLE_MODEL)
        if not os.getenv("GOOGLE_API_KEY"):
            out["problem"] = (
                "Set GOOGLE_API_KEY to use Gemini. To use a model on this "
                "machine instead, with no key: set EMBED_BASE_URL (for Ollama: "
                "http://127.0.0.1:11434/v1) and EMBED_MODEL (for example "
                "nomic-embed-text), then restart the engine."
            )
    out["ready"] = out["problem"] is None
    return out


# --------------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ConfigError):
        # Missing settings and a wrong width do not fix themselves, and a
        # width such as 1500 must not read as an HTTP 500.
        return False
    if isinstance(exc, EmbeddingError):
        return exc.transient
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

    `provider` overrides the `EMBED_PROVIDER` dispatch (`"azure"`,
    `"google"` or `"openai_compatible"`) for callers that carry their own selector — today the docs
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
    if provider == OPENAI_COMPATIBLE:
        fn = _embed_openai_compatible
    else:
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
