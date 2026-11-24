"""Each custom agent keeps its own memories.

Every generic agent has `agent_kind='custom'`, and memories were stored and
read by kind only, so "Support desk" would recall what a user told "Sales
assistant". A custom agent's memories now carry its slug (`agent_slug`): the
runtime, the Memory screen and the admin MCP `memory` tool read and write only
that agent's rows. Product kinds are read by kind exactly as before. Custom-kind
rows saved before this (no slug) belong to no agent until one is assigned.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from admin import agent_kinds as ak
from core.memory import service, store
from tests.admin.test_agent_pickers_per_agent import FakeDB, PROFILES
from tests.scoped_build import full_build

ROOT = Path(__file__).resolve().parents[2]
USER = "00000000-0000-0000-0000-0000000000aa"


def _mem(mid, content, *, slug=None, kind="custom", **extra):
    return {"id": mid, "user_id": USER, "agent_kind": kind, "agent_slug": slug,
            "content": content, "importance": 0.5, "pinned": False, "source": "manual",
            "status": "active", "last_used_at": None, "created_at": "2026-11-23T00:00:00Z",
            **extra}


MEMORIES = [
    _mem("m-sales", "Wants the annual plan", slug="sales-assistant"),
    _mem("m-support", "Printer model X200", slug="support-desk"),
    _mem("m-old", "Prefers email", slug=None),
    _mem("m-product", "Product-kind fact", kind="sample_agent"),
]


@pytest.fixture
def db(monkeypatch):
    full_build(monkeypatch)
    fake = FakeDB({"agent_profiles": PROFILES, "ai_user_memory": MEMORIES})
    monkeypatch.setattr(ak, "_fetch_profiles", lambda: fake.tables["agent_profiles"])
    from admin import memory_handlers, router

    monkeypatch.setattr(router, "client_for", lambda env: fake)
    monkeypatch.setattr(memory_handlers, "client_for", lambda env: fake)
    monkeypatch.setattr(memory_handlers, "record_change", lambda **_k: None)
    monkeypatch.setattr(memory_handlers, "embed_text", lambda _t: None)
    return fake


# --- store -------------------------------------------------------------------


class _Rec:
    """Records the filters a query builder is given."""

    def __init__(self):
        self.calls = []

    def eq(self, col, val):
        self.calls.append((col, val))
        return self


def test_custom_reads_are_scoped_by_slug_and_product_kinds_by_kind_only():
    q = store.scope_query(_Rec(), agent_kind="custom", agent_slug="support-desk")
    assert q.calls == [("agent_kind", "custom"), ("agent_slug", "support-desk")]
    q = store.scope_query(_Rec(), agent_kind="business", agent_slug=None)
    assert q.calls == [("agent_kind", "business")]
    with pytest.raises(store.MissingAgentScope):
        store.scope_query(_Rec(), agent_kind="custom", agent_slug=None)


class _RpcClient:
    def __init__(self):
        self.rpcs, self.rows = [], []

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))

    def table(self, _name):
        client = self

        class _T:
            def insert(self, row):
                client.rows.append(row)
                return SimpleNamespace(execute=lambda: SimpleNamespace(data=[row]))
        return _T()


async def test_recall_rpc_names_the_agent_only_for_custom_kinds():
    c = _RpcClient()
    await store.match_memories(c, user_id=USER, agent_kind="custom", agent_slug="support-desk",
                               query_embedding=[0.0], match_count=3, min_similarity=0.5)
    await store.match_memories(c, user_id=USER, agent_kind="business",
                               query_embedding=[0.0], match_count=3, min_similarity=0.5)
    assert c.rpcs[0][1]["p_agent_slug"] == "support-desk"
    assert "p_agent_slug" not in c.rpcs[1][1]   # product call unchanged


async def test_custom_memory_is_saved_under_the_agent_without_a_business():
    c = _RpcClient()
    await store.insert_memory(c, user_id=USER, business_id=None, agent_kind="custom",
                              agent_slug="sales-assistant", content="x", embedding=None)
    assert c.rows[0]["agent_slug"] == "sales-assistant"
    with pytest.raises(store.MissingAgentScope):
        await store.insert_memory(c, user_id=USER, business_id=None, agent_kind="custom",
                                  content="x", embedding=None)


async def test_write_path_saves_a_custom_agents_fact_under_its_slug(monkeypatch):
    seen = {}

    async def fake_extract(*_a, **_k):
        return [{"content": "Wants the annual plan", "importance": 0.9}]

    async def fake_nearest(_c, **kw):
        seen["nearest"] = kw
        return None

    async def fake_insert(_c, **kw):
        seen["insert"] = kw

    monkeypatch.setattr(service, "build_chat_model", lambda _m: object())
    monkeypatch.setattr(service, "extract_facts", fake_extract)
    monkeypatch.setattr(service, "embed_text", lambda _t: [0.1])
    monkeypatch.setattr(service, "_agent_memory_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(store, "nearest_one", fake_nearest)
    monkeypatch.setattr(store, "insert_memory", fake_insert)
    profile = SimpleNamespace(memory_config={"enabled": True}, model_main={}, model_light=None,
                              agent_kind="custom", resolved_prompts={}, prompt_version_ids={})
    await service.process_turn(
        profile=profile, user_id=USER, business_id=None, agent_kind="custom",
        agent_slug="sales-assistant", session_id=None, user_message="hi",
        assistant_response="ok", previous_messages=[], client=object(),
    )
    assert seen["nearest"]["agent_slug"] == "sales-assistant"
    assert seen["insert"]["agent_slug"] == "sales-assistant"
    assert seen["insert"]["business_id"] is None


async def test_custom_agent_reads_its_own_memories(monkeypatch):
    from runtime.agents import custom

    seen = {}

    async def fake_block(**kw):
        seen.update(kw)
        return "", [], False

    monkeypatch.setattr(custom.memory_service, "build_context_block", fake_block)
    monkeypatch.setattr(custom, "_memory_client", lambda: object())
    agent = object.__new__(custom.CustomRuntimeAgent)
    agent.slug = "support-desk"
    agent.profile = SimpleNamespace(memory_config={"enabled": True})
    assert agent._memory_on(USER, voice_turn=False)
    assert not agent._memory_on(None, voice_turn=False)
    assert not agent._memory_on(USER, voice_turn=True)
    await agent._memory_block(USER, "s1", "hello")
    assert seen["agent_kind"] == "custom" and seen["agent_slug"] == "support-desk"


# --- Memory screen -----------------------------------------------------------


def test_memory_screen_lists_only_the_picked_agents_memories(client, db):
    page = client.get(f"/admin/memory?agent_kind=custom:support-desk&user={USER}&env=staging")
    assert page.status_code == 200
    assert "Printer model X200" in page.text
    assert "Wants the annual plan" not in page.text          # Sales assistant's
    assert "Product-kind fact" not in page.text
    assert "every custom agent shares" not in page.text
    # The pre-existing row with no agent is offered, not recalled.
    assert "Not assigned to an agent yet" in page.text
    assert "Prefers email" in page.text


def test_memory_added_for_one_agent_is_not_shown_for_another(client, db):
    r = client.post("/admin/memory/add?env=staging", data={
        "user_id": USER, "content": "Budget is 5k", "agent_kind": "custom:sales-assistant",
    }, follow_redirects=False)
    assert r.status_code == 302
    row = db.inserted["ai_user_memory"][0]
    assert row["agent_kind"] == "custom" and row["agent_slug"] == "sales-assistant"
    assert "business_id" not in row
    page = client.get(f"/admin/memory?agent_kind=custom:support-desk&user={USER}&env=staging")
    assert "Budget is 5k" not in page.text


def test_unassigned_memory_can_be_moved_to_the_agent(client, db):
    r = client.post("/admin/memory/m-old/assign?env=staging", data={
        "agent_kind": "custom:support-desk", "user": USER}, follow_redirects=False)
    assert r.status_code == 302
    old = next(m for m in db.tables["ai_user_memory"] if m["id"] == "m-old")
    assert old["agent_slug"] == "support-desk"
    # An agent's own memory is never re-assigned by this route.
    client.post("/admin/memory/m-sales/assign?env=staging",
                data={"agent_kind": "custom:support-desk"}, follow_redirects=False)
    sales = next(m for m in db.tables["ai_user_memory"] if m["id"] == "m-sales")
    assert sales["agent_slug"] == "sales-assistant"


# --- admin MCP ---------------------------------------------------------------


def test_mcp_memory_tool_is_per_agent(db, monkeypatch):
    import admin_mcp.tools_govern as tg

    monkeypatch.setattr(tg, "client_for_env", lambda env: db)
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: None)
    monkeypatch.setattr("core.memory.embeddings.embed_text", lambda _t: None)
    out = tg.memory(action="list", agent_kind="custom:sales-assistant", user_id=USER)
    assert [m["id"] for m in out["memories"]] == ["m-sales"]
    assert [m["id"] for m in out["unassigned_memories"]] == ["m-old"]

    added = tg.memory(action="add", agent_kind="custom:support-desk", user_id=USER,
                      content="Uses the mobile app")
    assert added["added"] is True
    assert db.inserted["ai_user_memory"][0]["agent_slug"] == "support-desk"
    assert tg.memory(action="add", agent_kind="custom", user_id=USER, content="x")["error"]

    moved = tg.memory(action="assign", memory_id="m-old", agent_kind="custom:sales-assistant")
    assert moved["assigned"] is True
    again = tg.memory(action="assign", memory_id="m-old", agent_kind="custom:support-desk")
    assert "error" in again


# --- schema ------------------------------------------------------------------


def test_migration_adds_the_scope():
    name = "supabase/migrations/20261123150000_agent_scoped_memory.sql"
    sql = (ROOT / name).read_text()
    assert "ADD COLUMN IF NOT EXISTS agent_slug" in sql
    assert "business_id DROP NOT NULL" in sql
    assert "DROP FUNCTION IF EXISTS public.match_user_memory(uuid, public.ai_agent_kind, text, integer, real)" in sql
    rpc = (ROOT / "supabase/db-functions/rpc/match_user_memory.ts").read_text()
    assert "p_agent_slug text DEFAULT NULL" in rpc and "m.agent_slug = p_agent_slug" in rpc


def test_memory_screen_opens_on_an_agent_not_the_bare_kind(client, db):
    page = client.get(f"/admin/memory?agent_kind=custom&user={USER}&env=staging")
    assert page.status_code == 200
    assert 'value="custom:' in page.text and 'value="custom" selected' not in page.text
