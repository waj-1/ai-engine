"""The embedding provider switch — and the trap it exists to signpost.

Vectors from different models are not comparable, so `EMBED_PROVIDER` is not
a runtime toggle: flipping it without re-embedding leaves retrieval searching
a space its corpus was never embedded in, which returns nothing (memory) or
confident nonsense (KB) and NEVER raises. These tests pin the routing and the
safe default; the data migration is `scripts/reembed.py`.
"""
from __future__ import annotations

import pytest

from core import embedding_provider as provider


def test_google_is_the_default_provider(monkeypatch):
    """The default must stay Gemini: every vector stored to date is Gemini's.

    Defaulting to Azure would silently invalidate the entire existing corpus
    on the next deploy, with no error anywhere.
    """
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)
    assert provider.active_provider() == "google"


@pytest.mark.parametrize(
    "value,expected",
    [("azure", "azure"), ("AZURE", "azure"), ("  azure  ", "azure"),
     ("google", "google"), ("", "google")],
)
def test_the_switch_is_case_and_whitespace_insensitive(monkeypatch, value, expected):
    """Operators set this by hand; a stray capital must not silently mean Gemini."""
    monkeypatch.setenv("EMBED_PROVIDER", value)
    assert provider.active_provider() == expected


def test_azure_is_selected_and_asked_for_the_callers_width(monkeypatch):
    seen = {}

    def _fake_azure(texts, dim):
        seen["texts"], seen["dim"] = texts, dim
        return [[0.0] * dim for _ in texts]

    monkeypatch.setenv("EMBED_PROVIDER", "azure")
    monkeypatch.setattr(provider, "_embed_azure", _fake_azure)

    out = provider.embed_batch(["a", "b"], dim=1536)

    assert seen["texts"] == ["a", "b"]
    # The width is the CALLER's column, not a provider default -- 768 for
    # memory, 1536 for the KB, from one shared implementation.
    assert seen["dim"] == 1536
    assert [len(v) for v in out] == [1536, 1536]


def test_empty_input_never_reaches_a_provider(monkeypatch):
    def _explode(texts, dim):  # pragma: no cover - must not run
        raise AssertionError("no network call for empty input")

    monkeypatch.setattr(provider, "_embed_google", _explode)
    monkeypatch.setattr(provider, "_embed_azure", _explode)
    assert provider.embed_batch([], dim=768) == []


def test_a_partial_batch_is_never_returned_as_if_complete(monkeypatch):
    """Raising beats truncating.

    A caller that stored a short batch as the whole thing would corrupt the
    corpus far more quietly than a failed ingest does -- the rows simply go
    missing and retrieval degrades with nothing in the logs.
    """
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)

    def _boom(texts, dim):
        raise ValueError("permanent failure")

    monkeypatch.setattr(provider, "_embed_google", _boom)
    with pytest.raises(ValueError):
        provider.embed_batch(["a"], dim=768, retries=1)


def test_a_transient_failure_is_retried_then_succeeds(monkeypatch):
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)
    monkeypatch.setattr(provider.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _flaky(texts, dim):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 rate limit")
        return [[0.1] * dim for _ in texts]

    monkeypatch.setattr(provider, "_embed_google", _flaky)
    out = provider.embed_batch(["a"], dim=768)

    assert calls["n"] == 2
    assert len(out[0]) == 768


def test_a_permanent_failure_is_not_retried(monkeypatch):
    """Retrying a 401 just multiplies the latency of a certain failure."""
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)
    monkeypatch.setattr(provider.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _auth_error(texts, dim):
        calls["n"] += 1
        raise ValueError("401 unauthorized")

    monkeypatch.setattr(provider, "_embed_google", _auth_error)
    with pytest.raises(ValueError):
        provider.embed_batch(["a"], dim=768, retries=3)
    assert calls["n"] == 1


def test_azure_without_credentials_raises_config_error(monkeypatch):
    monkeypatch.setenv("EMBED_PROVIDER", "azure")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(provider, "_azure_client", None)

    with pytest.raises(provider.ConfigError):
        provider.embed_batch(["a"], dim=768, retries=1)
