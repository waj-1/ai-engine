"""The OpenAI-compatible embedding provider: knowledge bases with no cloud key.

`POST {EMBED_BASE_URL}/embeddings` on Ollama, vLLM, LM Studio or a hosted
server. Every test talks to a fake HTTP transport; nothing leaves the process.

The part that must never regress is the width check. The columns are fixed
(1536 for knowledge, 768 for memory) and a local model's width is whatever it
is: a narrower vector is zero-padded (exact under cosine distance), a wider
one is only accepted when the server shortens it itself, and anything else
fails naming the model and both widths.
"""
from __future__ import annotations

import json
import math
from typing import Callable, List

import httpx
import pytest

from core import embedding_provider as provider

BASE = "http://127.0.0.1:11434/v1"


class FakeServer:
    """An OpenAI-style embeddings endpoint. `width` is the model's own width;
    `honours_dimensions` is whether it shortens vectors when asked."""

    def __init__(self, width: int = 768, honours_dimensions: bool = False,
                 handler: Callable[[httpx.Request], httpx.Response] | None = None):
        self.width = width
        self.honours_dimensions = honours_dimensions
        self.requests: List[httpx.Request] = []
        self.bodies: List[dict] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.bodies.append(body)
        if self._handler is not None:
            return self._handler(request)
        width = self.width
        if self.honours_dimensions and body.get("dimensions"):
            width = body["dimensions"]
        data = [
            # First component carries the text's position so order is checkable.
            {"object": "embedding", "index": i, "embedding": [float(len(t))] + [0.5] * (width - 1)}
            for i, t in enumerate(body["input"])
        ]
        # Out of order on purpose: the contract does not promise order.
        return httpx.Response(200, json={"object": "list", "data": list(reversed(data)), "model": body["model"]})


@pytest.fixture
def server(monkeypatch):
    """Point the provider at a fake server, with a clean slate per test."""
    for name in ("EMBED_PROVIDER", "EMBED_BASE_URL", "EMBED_MODEL", "EMBED_API_KEY_ENV",
                 "EMBED_API_KEY", "OLLAMA_BASE_URL", "GOOGLE_API_KEY",
                 "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(provider, "_oc_clients", {})
    monkeypatch.setattr(provider, "_oc_send_dimensions", {})
    monkeypatch.setattr(provider, "_oc_pad_logged", set())
    monkeypatch.setattr(provider.time, "sleep", lambda _s: None)

    def _use(fake: FakeServer) -> FakeServer:
        monkeypatch.setattr(provider, "_oc_transport", httpx.MockTransport(fake))
        return fake

    monkeypatch.setenv("EMBED_BASE_URL", BASE)
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    return _use


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


def test_request_is_an_openai_embeddings_call(server):
    fake = server(FakeServer(width=768))

    out = provider.embed_batch(["hello", "bonjour"], dim=768)

    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"{BASE}/embeddings"
    assert fake.bodies[0] == {
        "model": "nomic-embed-text", "input": ["hello", "bonjour"], "encoding_format": "float",
    }
    # A server on this machine takes no key, and none is invented for it.
    assert "authorization" not in req.headers
    assert [len(v) for v in out] == [768, 768]
    # Sorted back by `index`, whatever order the server answered in.
    assert [v[0] for v in out] == [5.0, 7.0]


def test_key_is_read_from_the_variable_the_setting_names(server, monkeypatch):
    fake = server(FakeServer(width=768))
    monkeypatch.setenv("EMBED_BASE_URL", "https://models.example.com/v1/")
    monkeypatch.setenv("EMBED_API_KEY_ENV", "MY_EMBED_KEY")
    monkeypatch.setenv("MY_EMBED_KEY", "sk-test-123")

    provider.embed_batch(["a"], dim=768)

    assert fake.requests[0].headers["authorization"] == "Bearer sk-test-123"
    # The trailing slash on the base URL does not double up.
    assert str(fake.requests[0].url) == "https://models.example.com/v1/embeddings"


def test_base_url_falls_back_to_the_ollama_setting(server, monkeypatch):
    fake = server(FakeServer(width=768))
    monkeypatch.delenv("EMBED_BASE_URL")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")

    provider.embed_batch(["a"], dim=768)

    assert str(fake.requests[0].url) == "http://ollama:11434/v1/embeddings"


def test_long_input_is_sent_in_batches_and_comes_back_in_order(server):
    fake = server(FakeServer(width=768))
    texts = ["x" * (i + 1) for i in range(250)]

    out = provider.embed_batch(texts, dim=768)

    assert [len(b["input"]) for b in fake.bodies] == [100, 100, 50]
    assert [v[0] for v in out] == [float(i + 1) for i in range(250)]


# --------------------------------------------------------------------------
# Errors say what happened
# --------------------------------------------------------------------------


def test_unknown_model_surfaces_the_servers_words_and_is_not_retried(server):
    fake = server(FakeServer(handler=lambda _r: httpx.Response(
        404, json={"error": {"message": 'model "nomic-embed-text" not found, try pulling it first'}})))

    with pytest.raises(provider.EmbeddingError) as exc:
        provider.embed_batch(["a"], dim=768)

    msg = str(exc.value)
    assert "HTTP 404" in msg and "try pulling it first" in msg and "nomic-embed-text" in msg
    assert len(fake.requests) == 1


def test_a_busy_server_is_retried(server):
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="loading model")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 768}]})

    server(FakeServer(handler=_handler))

    assert len(provider.embed_batch(["a"], dim=768)[0]) == 768
    assert calls["n"] == 2


def test_a_server_that_is_down_says_where_it_was_looking(server):
    def _handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    server(FakeServer(handler=_handler))

    with pytest.raises(provider.EmbeddingError) as exc:
        provider.embed_batch(["a"], dim=768, retries=1)

    assert BASE in str(exc.value) and "EMBED_BASE_URL" in str(exc.value)


def test_a_short_answer_is_refused_not_stored(server):
    server(FakeServer(handler=lambda _r: httpx.Response(
        200, json={"data": [{"index": 0, "embedding": [0.1] * 768}]})))

    with pytest.raises(provider.EmbeddingError, match="1 vectors for 2 texts"):
        provider.embed_batch(["a", "b"], dim=768)


def test_missing_settings_are_named(server, monkeypatch):
    monkeypatch.setenv("EMBED_PROVIDER", "openai_compatible")
    monkeypatch.delenv("EMBED_MODEL")

    with pytest.raises(provider.ConfigError, match="EMBED_MODEL"):
        provider.embed_batch(["a"], dim=768)


def test_a_hosted_server_without_a_key_is_refused_before_any_request(server, monkeypatch):
    fake = server(FakeServer())
    monkeypatch.setenv("EMBED_BASE_URL", "https://models.example.com/v1")

    with pytest.raises(provider.ConfigError, match="EMBED_API_KEY"):
        provider.embed_batch(["a"], dim=768)
    assert fake.requests == []


# --------------------------------------------------------------------------
# Width
# --------------------------------------------------------------------------


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_a_matching_width_is_stored_as_is_with_no_dimensions_parameter(server):
    fake = server(FakeServer(width=768))

    out = provider.embed_batch(["abc"], dim=768)

    assert out[0] == [3.0] + [0.5] * 767
    assert "dimensions" not in fake.bodies[0]


def test_a_narrower_model_is_zero_padded_and_similarity_is_unchanged(server):
    """768-wide vectors against the 1536 knowledge column: trailing zeros
    change neither a dot product nor a norm."""
    def _handler(request):
        body = json.loads(request.content)
        vecs = {"a": [1.0, 2.0, 3.0] + [0.25] * 765, "b": [3.0, -1.0, 0.5] + [0.75] * 765}
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": vecs[t]} for i, t in enumerate(body["input"])]})

    fake = server(FakeServer(handler=_handler))

    a, b = provider.embed_batch(["a", "b"], dim=1536)

    assert len(a) == len(b) == 1536
    assert a[768:] == [0.0] * 768
    assert _cosine(a, b) == pytest.approx(_cosine(a[:768], b[:768]), abs=1e-12)
    # Padding is not something to ask the server for.
    assert all("dimensions" not in body for body in fake.bodies)


def test_a_wider_model_is_asked_for_the_columns_width(server):
    fake = server(FakeServer(width=1024, honours_dimensions=True))

    first = provider.embed_batch(["a"], dim=768)
    second = provider.embed_batch(["b"], dim=768)

    assert len(first[0]) == len(second[0]) == 768
    # First use: one request at the model's own width, one with `dimensions`.
    # After that the width is known and every request asks for it directly.
    assert [b.get("dimensions") for b in fake.bodies] == [None, 768, 768]


def test_a_wider_model_the_server_cannot_shorten_fails_naming_model_and_widths(server, monkeypatch):
    """The regression this file exists for: 1024 numbers must never reach a
    768 column, be truncated behind the operator's back, or be searched."""
    fake = server(FakeServer(width=1024, honours_dimensions=False))
    provider_model = "mxbai-embed-large"
    monkeypatch.setenv("EMBED_MODEL", provider_model)

    with pytest.raises(provider.EmbeddingWidthError) as exc:
        provider.embed_batch(["a"], dim=768, retries=3)

    msg = str(exc.value)
    assert provider_model in msg and "1024" in msg and "768" in msg
    # Not retried: a wrong width does not fix itself.
    assert len(fake.requests) == 2


def test_a_server_that_rejects_dimensions_still_reports_the_width_problem(server):
    def _handler(request):
        body = json.loads(request.content)
        if "dimensions" in body:
            return httpx.Response(400, json={"error": {"message": "model does not support matryoshka"}})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 2048}]})

    server(FakeServer(handler=_handler))

    with pytest.raises(provider.EmbeddingWidthError) as exc:
        provider.embed_batch(["a"], dim=1536)

    assert "2048" in str(exc.value) and "1536" in str(exc.value) and "matryoshka" in str(exc.value)


def test_a_width_error_is_a_config_error_for_the_upload_path(server):
    """`runtime.embeddings.embed_texts` turns the provider's ConfigError into
    the one the knowledge handlers show; the width error rides along."""
    from runtime import embeddings

    server(FakeServer(width=4096, honours_dimensions=False))

    with pytest.raises(embeddings.ConfigError, match="4096"):
        embeddings.embed_texts(["a"])


def test_knowledge_and_memory_each_get_their_own_columns_width(server):
    """One 768-wide model serves both columns: as is for memory, padded for
    knowledge."""
    from core.memory import embeddings as memory_embeddings
    from runtime import embeddings

    server(FakeServer(width=768))

    assert len(embeddings.embed_texts(["a question"])[0]) == 1536
    assert len(memory_embeddings.embed_text("a fact")) == 768


# --------------------------------------------------------------------------
# Which provider is in force
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["openai_compatible", "OpenAI-Compatible", " ollama ", "openai", "local"])
def test_the_provider_can_be_named(server, monkeypatch, value):
    monkeypatch.setenv("EMBED_PROVIDER", value)
    monkeypatch.delenv("EMBED_MODEL")
    assert provider.active_provider() == "openai_compatible"


def test_a_keyless_install_with_an_endpoint_uses_it_without_being_told(server):
    """No EMBED_PROVIDER, no cloud key, a model and a base URL: it just works."""
    fake = server(FakeServer(width=768))

    assert provider.active_provider() == "openai_compatible"
    assert len(provider.embed_batch(["a"], dim=1536)[0]) == 1536
    assert len(fake.requests) == 1


def test_adding_a_cloud_key_later_does_not_move_the_corpus_to_another_model(server, monkeypatch):
    """The documents were embedded locally. A Gemini key added for a chat
    model must not start embedding questions with Gemini."""
    monkeypatch.setenv("GOOGLE_API_KEY", "added-later")
    assert provider.active_provider() == "openai_compatible"


def test_an_explicit_provider_wins_over_the_model_setting(server, monkeypatch):
    monkeypatch.setenv("EMBED_PROVIDER", "google")
    assert provider.active_provider() == "google"


def test_a_model_with_no_base_url_does_not_select_the_provider(server, monkeypatch):
    monkeypatch.delenv("EMBED_BASE_URL")
    assert provider.active_provider() == "google"


def test_the_kb_threshold_has_no_model_specific_value_for_it(server):
    from core.config import _KB_MATCH_THRESHOLD_FALLBACK, _kb_match_threshold

    assert _kb_match_threshold() == _KB_MATCH_THRESHOLD_FALLBACK


# --------------------------------------------------------------------------
# What the portal is told
# --------------------------------------------------------------------------


def test_status_describes_the_local_model(server):
    status = provider.embedding_status()

    assert status["ready"] is True and status["problem"] is None
    assert status["provider"] == "openai_compatible"
    assert status["model"] == "nomic-embed-text"
    assert status["base_url"] == BASE
    assert status["identity"] == "openai_compatible:nomic-embed-text"


def test_status_on_an_install_with_nothing_set_says_what_to_set(server, monkeypatch):
    monkeypatch.delenv("EMBED_BASE_URL")
    monkeypatch.delenv("EMBED_MODEL")

    status = provider.embedding_status()

    assert status["ready"] is False
    for name in ("GOOGLE_API_KEY", "EMBED_BASE_URL", "EMBED_MODEL", "nomic-embed-text"):
        assert name in status["problem"]


def test_status_never_shows_credentials_in_the_address(server, monkeypatch):
    monkeypatch.setenv("EMBED_BASE_URL", "http://user:hunter2@10.0.0.5:8000/v1")

    status = provider.embedding_status()

    assert status["base_url"] == "http://10.0.0.5:8000/v1"
    assert "hunter2" not in json.dumps(status)
