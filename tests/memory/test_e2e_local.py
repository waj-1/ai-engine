"""Opt-in e2e against the local AI stack (port 54422).

Run: RUN_MEMORY_E2E=1 python -m pytest tests/memory/test_e2e_local.py -v
Requires: ai_agent/supabase stack up + migrations applied + match_user_memory deployed,
and AI_SUPABASE_* env pointing at the local AI project.
"""
import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MEMORY_E2E") != "1", reason="set RUN_MEMORY_E2E=1 to run"
)


def _vec(seed: float):
    # deterministic 768-dim vector; first component carries the signal
    v = [0.0] * 768
    v[0] = seed
    v[1] = 1.0
    return v


def test_insert_then_match_roundtrip():
    from core.auth import get_service_client
    from core.memory import store

    client = get_service_client()
    user_id = str(uuid.uuid4())
    business_id = str(uuid.uuid4())

    asyncio.run(store.insert_memory(
        client, user_id=user_id, business_id=business_id, agent_kind="business",
        content="Prefers revenue in AED", embedding=_vec(0.9), importance=0.9, source="auto",
    ))
    asyncio.run(store.insert_memory(
        client, user_id=user_id, business_id=business_id, agent_kind="business",
        content="Unrelated note", embedding=_vec(-0.9), importance=0.6, source="auto",
    ))

    matches = asyncio.run(store.match_memories(
        client, user_id=user_id, agent_kind="business",
        query_embedding=_vec(0.9), match_count=1, min_similarity=0.5,
    ))
    assert matches, "expected at least one match"
    assert matches[0]["content"] == "Prefers revenue in AED"
