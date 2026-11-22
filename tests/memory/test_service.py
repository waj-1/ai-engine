# ai_agent/tests/memory/test_service.py
import asyncio

from core.agent_profile import Profile


def _profile(memory_config):
    return Profile(
        profile_id="p1", agent_kind="business", slug="business_default",
        environment="staging",
        model_main={"provider": "google_genai", "model_string": "gemini-x"},
        memory_config=memory_config,
    )


def test_slot_model_light_falls_back_to_main():
    from core.memory.service import _slot_model

    profile = _profile({"enabled": True})
    # Light model retired: model_light is always None, so the "light" slot must
    # fall back to model_main. build_chat_model(None) returns None, which would
    # otherwise crash the async memory-model call.
    assert profile.model_light is None
    assert _slot_model(profile, "light") == profile.model_main
    assert _slot_model(profile, "main") == profile.model_main


def test_build_context_block_disabled_returns_empty():
    from core.memory.service import build_context_block

    block, ids, summary_used = asyncio.run(build_context_block(
        profile=_profile({"enabled": False}),
        user_id="u1", business_id="b1", agent_kind="business",
        session_id="s1", current_message="hi", client=object(),
    ))
    assert block == ""
    assert ids == []
    assert summary_used is False


def test_build_context_block_no_user_returns_empty():
    from core.memory.service import build_context_block

    block, ids, summary_used = asyncio.run(build_context_block(
        profile=_profile({"enabled": True}),
        user_id=None, business_id="b1", agent_kind="business",
        session_id="s1", current_message="hi", client=object(),
    ))
    assert block == ""
    assert ids == []
    assert summary_used is False


def test_build_context_block_composes_summary_and_memories(monkeypatch):
    from core.memory import service

    monkeypatch.setattr(service, "embed_text", lambda _t: [0.1] * 768)

    async def fake_match(*_a, **_k):
        return [{"id": "m1", "content": "Prefers AED", "similarity": 0.9, "pinned": False}]

    async def fake_pinned(*_a, **_k):
        return [{"id": "m2", "content": "Owner of clinic X", "pinned": True}]

    async def fake_summary(*_a, **_k):
        return {"running_summary": "Discussed July revenue.", "summary_message_count": 4}

    monkeypatch.setattr(service.store, "match_memories", fake_match)
    monkeypatch.setattr(service.store, "list_pinned", fake_pinned)
    monkeypatch.setattr(service.store, "get_session_summary", fake_summary)
    monkeypatch.setattr(service.store, "bump_usage", lambda *a, **k: _aresult(None))

    block, ids, summary_used = asyncio.run(service.build_context_block(
        profile=_profile({"enabled": True}),
        user_id="u1", business_id="b1", agent_kind="business",
        session_id="s1", current_message="how was revenue", client=object(),
    ))
    assert "Prefers AED" in block
    assert "Owner of clinic X" in block
    assert "Discussed July revenue." in block
    assert set(ids) == {"m1", "m2"}
    assert summary_used is True


async def _aresult(v):
    return v
