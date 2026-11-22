"""Regression tests for the memory embedding path's 3072→768 truncation.

Some Gemini client versions ignore ``output_dimensionality`` and return
gemini-embedding-001's native 3072-dim vector, which the
``ai_user_memory.embedding vector(768)`` column rejects outright. The defence
is to truncate to the first EMBED_DIM components — valid because the model is
Matryoshka and the RPC's cosine op is scale-invariant.

That defence now lives in `core.embedding_provider._embed_google` rather than
in `embed_text` itself, because both embedding call sites (memory at 768,
the KB at 1536) need it and both now dispatch through the provider module.
These tests follow it there, and keep pinning the same behaviour through the
public `embed_text` surface so the contract callers depend on is unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[1].parent
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from core import embedding_provider as provider
from core.memory import embeddings as emb


def _patch_google(monkeypatch, vector, *, calls=None):
    """Stand in for the Gemini leg, preserving its truncation contract."""
    def _fake(texts, dim):
        if calls is not None:
            calls.extend(texts)
        # Mirror the real leg: it truncates an over-wide native vector.
        return [list(vector)[:dim] if len(vector) > dim else list(vector)
                for _ in texts]
    monkeypatch.setattr(provider, "_embed_google", _fake)


def test_native_3072_vector_truncated_to_embed_dim(monkeypatch):
    _patch_google(monkeypatch, [0.01 * i for i in range(3072)])

    vec = emb.embed_text("remember this")

    assert vec is not None
    assert len(vec) == emb.EMBED_DIM == 768
    # Truncation keeps the FIRST EMBED_DIM components (Matryoshka prefix).
    assert vec == [0.01 * i for i in range(768)]


def test_correct_size_vector_passes_through(monkeypatch):
    native = [0.5] * emb.EMBED_DIM
    _patch_google(monkeypatch, native)

    vec = emb.embed_text("already 768")

    assert vec == native
    assert len(vec) == emb.EMBED_DIM


def test_shorter_vector_left_untouched(monkeypatch):
    # Defensive: never pad. A short vector is returned as-is (caller degrades).
    short = [0.1, 0.2, 0.3]
    _patch_google(monkeypatch, short)

    assert emb.embed_text("short") == short


def test_empty_text_returns_none_without_calling_embedder(monkeypatch):
    calls: list = []
    _patch_google(monkeypatch, [0.0] * 3072, calls=calls)

    assert emb.embed_text("") is None
    assert emb.embed_text("   ") is None
    assert calls == []  # never hit the network for blank input


def test_embedder_failure_returns_none(monkeypatch):
    def _boom(texts, dim):
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_embed_google", _boom)

    assert emb.embed_text("anything") is None


def test_the_truncation_lives_in_the_google_leg_not_the_caller(monkeypatch):
    """Pin WHERE the defence is, not just that it happens.

    `embed_text` no longer truncates — if the provider module stopped doing it,
    every test above would still pass while real inserts failed on a 3072-dim
    vector. This asserts the real Gemini leg trims, with no fake in the way.

    Uses `monkeypatch.setitem` rather than assigning `sys.modules` directly:
    `google` is a NAMESPACE package shared with `google.protobuf`, so popping
    it in a teardown removes the real one for the rest of the session and
    unrelated tests start failing on `module 'google' has no attribute
    'protobuf'`. monkeypatch restores the previous entry (or removes one it
    added) instead.
    """
    import types as _t

    class _Resp:
        embeddings = [type("E", (), {"values": [0.02 * i for i in range(3072)]})()]

    class _Models:
        def embed_content(self, **kw):
            return _Resp()

    class _Client:
        models = _Models()

    fake_types = _t.SimpleNamespace(EmbedContentConfig=lambda **kw: None)
    fake_genai = _t.SimpleNamespace(Client=lambda **kw: _Client(), types=fake_types)
    google_mod = _t.ModuleType("google")
    google_mod.genai = fake_genai  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    out = provider._embed_google(["x"], 768)

    assert len(out[0]) == 768, "the Gemini leg must trim an over-wide vector"
