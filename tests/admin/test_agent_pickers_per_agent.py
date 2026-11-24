"""Agent pickers list AGENTS, not agent kinds — and picking one works.

Every generic agent (templates, the New agent wizard, the seeded demo) has
`agent_kind='custom'` and is told apart by slug. The Knowledge, Skills,
Memory, Channels and Conversations pickers listed kinds, so they offered one
entry, labelled after whichever custom profile sorted first ("Demo agent").
They now list one entry per agent (`custom:<slug>` for a generic agent), and
filtering / attaching by one reaches that agent:

* a KB attached the way the runtime reads it (`data_source_ids`) shows as
  attached, and picking a generic agent on the KB page attaches it there;
* a skill created for "Support desk" is attached to Support desk;
* memory settings for "Support desk" are read from and saved to its row;
* a channel can allow just "Support desk", and the runtime enforces it;
* Conversations narrows to one generic agent's chats.
"""
from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from admin import agent_kinds as ak
from tests.scoped_build import full_build


# ---------------------------------------------------------------------------
# A small in-memory Supabase: real eq / neq / in_ filtering, insert, update.
# ---------------------------------------------------------------------------


class _Q:
    def __init__(self, db: "FakeDB", table: str):
        self.db, self.table, self.filters, self.op, self.payload = db, table, [], "select", None
        self._limit = None

    def select(self, *_a, **_k):
        return self

    def schema(self, *_a):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def neq(self, col, val):
        self.filters.append(lambda r, c=col, v=val: r.get(c) != v)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self.filters.append(lambda r, c=col, v=vals: r.get(c) in v)
        return self

    def is_(self, col, val):
        self.filters.append(lambda r, c=col: r.get(c) is None)
        return self

    def gte(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for item in items:
                row = dict(item)
                pk = {"agent_skills": "skill_id", "agent_data_sources": "source_id",
                      "ai_user_memory": "id"}.get(self.table)
                if pk and pk not in row:
                    row[pk] = str(uuid.uuid4())
                rows.append(row)
                out.append(dict(row))
            self.db.inserted.setdefault(self.table, []).extend(out)
            return SimpleNamespace(data=out)
        hits = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hits:
                r.update(copy.deepcopy(self.payload))
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._limit is not None:
            hits = hits[: self._limit]
        return SimpleNamespace(data=[dict(r) for r in hits])


class FakeDB:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self.tables = copy.deepcopy(tables)
        self.inserted: Dict[str, List[Dict[str, Any]]] = {}

    def table(self, name):
        return _Q(self, name)

    def schema(self, *_a):
        return self


KB_ID = "11111111-1111-1111-1111-111111111111"


def _profile(pid, slug, name, kind="custom", status="active", **extra):
    return {"profile_id": pid, "agent_kind": kind, "slug": slug, "display_name": name,
            "status": status, "environment": "staging", "is_active": status == "active",
            "parent_profile_id": None, "data_source_ids": [], "memory_config": {}, **extra}


PROFILES = [
    _profile("p-demo", "demo", "Demo agent"),
    _profile("p-support", "support-desk", "Support desk", data_source_ids=[KB_ID],
             memory_config={"enabled": True, "retention_days": 30}),
    _profile("p-sales", "sales-assistant", "Sales assistant", status="draft"),
    _profile("p-mold", "mold:support", "Support desk template", status="template"),
]


@pytest.fixture
def db(monkeypatch):
    """Full build (no scope), the custom agents above, one shared DB."""
    full_build(monkeypatch)
    fake = FakeDB({
        "agent_profiles": PROFILES,
        "agent_data_sources": [{"source_id": KB_ID, "name": "help_center", "kind": "pgvector",
                                "agent_kind": None, "additional_agent_kinds": [],
                                "config": {}, "is_active": True}],
        "agent_profile_skills": [],
        "agent_skills": [],
        "agent_skill_versions": [],
    })
    monkeypatch.setattr(ak, "_fetch_profiles", lambda: fake.tables["agent_profiles"])
    import core.auth
    from admin import kb_handlers, router, skill_handlers

    monkeypatch.setattr(router, "client_for", lambda env: fake)
    monkeypatch.setattr(kb_handlers, "admin_service_client", lambda: fake)
    monkeypatch.setattr(kb_handlers, "record_change", lambda **_k: None)
    monkeypatch.setattr(skill_handlers, "admin_service_client", lambda: fake)
    monkeypatch.setattr(skill_handlers, "record_change", lambda **_k: None)
    monkeypatch.setattr(core.auth, "admin_service_client", lambda: fake)
    monkeypatch.setattr("admin.services.profiles.record_change", lambda **_k: None)
    return fake


# ---------------------------------------------------------------------------
# The shared list
# ---------------------------------------------------------------------------


def test_choices_list_each_custom_agent_by_name(db):
    choices = ak.build_agent_choices()
    assert "custom:support-desk" in choices
    assert "custom:sales-assistant" in choices      # a draft is still an agent
    assert "custom:demo" in choices
    assert "custom" not in choices                  # the bare kind names nobody
    assert not any("mold" in c for c in choices)    # templates are not agents
    assert ak.agent_label("custom:support-desk") == "Support desk"
    # The kind itself is no longer labelled after the first custom profile.
    assert ak.agent_label("custom") != "Demo agent"


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


def test_kb_list_reads_the_runtime_attachment_and_filters_per_agent(client, db):
    page = client.get("/admin/kb")
    assert page.status_code == 200
    row = page.text.split('href="/admin/kb/%s"' % KB_ID, 1)[1].split("</tr>", 1)[0]
    assert "Support desk" in row
    assert "Not attached to any agent" not in row
    # The Agent filter offers each custom agent, and narrows to it.
    assert 'value="custom:support-desk"' in page.text
    assert 'value="custom:sales-assistant"' in page.text
    only_sales = client.get("/admin/kb?kind=custom:sales-assistant")
    assert only_sales.status_code == 200
    assert 'href="/admin/kb/%s"' % KB_ID not in only_sales.text
    only_support = client.get("/admin/kb?kind=custom:support-desk")
    assert 'href="/admin/kb/%s"' % KB_ID in only_support.text


def test_kb_detail_shows_the_attachment_and_also_used_by_attaches(client, db):
    page = client.get(f"/admin/kb/{KB_ID}")
    assert page.status_code == 200
    assert "Not attached" not in page.text.split("<dt>Agent</dt>", 1)[1].split("</dd>", 1)[0]
    assert '<option value="custom:support-desk" selected>' in page.text
    assert 'name="additional_agent_kinds" value="custom:sales-assistant"' in page.text

    r = client.post(f"/admin/kb/{KB_ID}", data={
        "name": "help_center", "agent_kind": "custom:support-desk",
        "additional_agent_kinds": ["custom:sales-assistant"],
        "_additional_kinds_form": "1", "_active_form": "1", "is_active": "on",
    }, follow_redirects=False)
    assert r.status_code == 302
    by_slug = {p["slug"]: p for p in db.tables["agent_profiles"]}
    assert KB_ID in by_slug["sales-assistant"]["data_source_ids"]   # attached on its profile
    assert KB_ID in by_slug["support-desk"]["data_source_ids"]      # kept
    kb = db.tables["agent_data_sources"][0]
    assert kb["agent_kind"] == "custom"                  # the kind they share
    assert kb["additional_agent_kinds"] == []            # no "custom:" value on the row

    # Unticking detaches it from that agent again.
    client.post(f"/admin/kb/{KB_ID}", data={
        "name": "help_center", "agent_kind": "custom:support-desk",
        "_additional_kinds_form": "1"}, follow_redirects=False)
    by_slug = {p["slug"]: p for p in db.tables["agent_profiles"]}
    assert KB_ID not in by_slug["sales-assistant"]["data_source_ids"]


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def test_new_skill_for_one_custom_agent_is_attached_to_it(client, db):
    page = client.get("/admin/skills")
    assert 'value="custom:support-desk"' in page.text
    r = client.post("/admin/skills/create", data={
        "slug": "refund-note", "display_name": "Refund note",
        "description": "How to write a refund note", "agent_kind": "custom:support-desk",
        "content": "Say sorry, then the amount."}, follow_redirects=False)
    assert r.status_code == 302, r.text
    skill = db.tables["agent_skills"][0]
    assert skill["agent_kind"] == "custom"
    atts = db.tables["agent_profile_skills"]
    assert [a["profile_id"] for a in atts] == ["p-support"]
    assert "Support desk" in client.get("/admin/skills").text.split("refund-note", 1)[1].split("</tr>", 1)[0]


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def test_memory_settings_follow_the_picked_custom_agent(client, db, monkeypatch):
    import admin.memory_handlers as mh

    monkeypatch.setattr(mh, "client_for", lambda env: db)
    monkeypatch.setattr(mh, "record_change", lambda **_k: None)
    page = client.get("/admin/agents/memory?agent_kind=custom:support-desk&env=staging")
    assert page.status_code == 200
    assert 'value="custom:sales-assistant"' in page.text
    assert '<option value="custom:support-desk" selected>' in page.text
    assert 'value="30"' in page.text            # Support desk's own retention

    r = client.post("/admin/memory/config?env=staging", data={
        "agent_kind": "custom:sales-assistant", "enabled": "on", "retention_days": "7"},
        follow_redirects=False)
    assert r.status_code == 302
    by_slug = {p["slug"]: p for p in db.tables["agent_profiles"]}
    assert by_slug["sales-assistant"]["memory_config"]["retention_days"] == 7
    assert by_slug["support-desk"]["memory_config"]["retention_days"] == 30


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_channel_can_allow_one_custom_agent_and_the_runtime_enforces_it(db):
    from admin.channel_handlers import _channel_agent_choices, _parse_agent_kinds
    from core.channel_resolver import enforce_channel_kind

    assert "custom:support-desk" in _channel_agent_choices()
    assert _parse_agent_kinds(["custom:support-desk", "custom:nobody"]) == ["custom:support-desk"]

    conn = SimpleNamespace(state=SimpleNamespace(channel={
        "slug": "site", "allowed_agent_kinds": ["custom:support-desk"]}))
    enforce_channel_kind(conn, "custom", agent_slug="support-desk")   # allowed
    with pytest.raises(HTTPException) as exc:
        enforce_channel_kind(conn, "custom", agent_slug="sales-assistant")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def test_conversations_filter_narrows_to_one_custom_agent(db):
    from admin.screens import observability as obs
    from admin.screens.conversations import agent_filter_options

    options = agent_filter_options(["custom"], {"custom": "Demo agent"}, "staging")
    assert {"kind": "custom:support-desk", "name": "Support desk"} in options
    assert {"kind": "custom:sales-assistant", "name": "Sales assistant"} in options

    traces = [
        {"id": "t1", "metadata": {"waj.agent_kind": "custom", "waj.agent_slug": "support-desk"}},
        {"id": "t2", "metadata": {"waj.agent_kind": "custom", "waj.agent_slug": "sales-assistant"}},
    ]
    import admin.seed_agent_profiles as sap

    orig = obs.load_clickhouse_traces
    try:
        obs.load_clickhouse_traces = lambda **_k: copy.deepcopy(traces)
        sap_filter = sap.filter_trace_rows_to_client
        sap.filter_trace_rows_to_client = lambda rows: rows
        out = obs.load_traces_list(agent_kind="custom:support-desk", env="staging")
    finally:
        obs.load_clickhouse_traces = orig
        sap.filter_trace_rows_to_client = sap_filter
    assert [t["id"] for t in out["traces"]] == ["t1"]


def test_saved_chats_filter_narrows_to_one_custom_agent(db, monkeypatch):
    from admin.screens import saved_chats

    db.tables["ai_chat_sessions"] = [
        {"id": "s1", "agent_kind": "custom", "metadata": {"agent_slug": "support-desk"},
         "deleted_at": None, "updated_at": "2099-01-01T00:00:00Z"},
        {"id": "s2", "agent_kind": "custom", "metadata": {"agent_slug": "sales-assistant"},
         "deleted_at": None, "updated_at": "2099-01-01T00:00:00Z"},
    ]
    monkeypatch.setattr(saved_chats, "_client", lambda env: db)
    groups = saved_chats.list_conversations(env="staging", hours=24,
                                            agent_kind="custom:support-desk")
    assert [g["session_id"] for g in groups] == ["s1"]
    assert groups[0]["agent_name"] == "Support desk"
