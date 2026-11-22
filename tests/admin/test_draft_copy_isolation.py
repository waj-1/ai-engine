"""A draft copy ("Make a draft copy" / fork) must stay isolated from the agent
it was copied from, and must carry everything that makes it the same agent.

Two defects, both found while recording the portal docs:

* The Instructions editor seeded its textarea with the NEWEST version of the
  kind-shared `agent_prompts` row, not the version the viewed profile is
  pinned to. A save on a draft copy creates that newest version, so the live
  original's editor showed the copy's text — and a Save there would pin it
  onto the original. The same display bug made a Versions-tab Restore (which
  repoints the pin to an older version) look like it had not happened.
* fork copied only the agent_profiles row. Toolsets (MCP server/bundle
  attachments) and skills live in junction tables keyed by profile_id, so the
  copy showed 0 actions.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

from admin.services import profiles


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._rows = list(db.tables.get(table, []))
        self._op = "select"
        self._payload = None

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        allowed = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def order(self, col, desc=False):
        self._rows.sort(key=lambda r: r.get(col) or 0, reverse=desc)
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._op, self._payload = "upsert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def execute(self):
        if self._op in ("insert", "upsert"):
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            self._db.tables.setdefault(self._table, []).extend(dict(r) for r in rows)
            self._db.writes.append((self._op, self._table, rows))
            return _Result(rows)
        if self._op == "update":
            for r in self._rows:
                r.update(self._payload)
            return _Result(self._rows)
        return _Result(self._rows)


class FakeSupabase:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self.tables = tables
        self.writes: List[Any] = []

    def table(self, name):
        return _Query(self, name)


ORIG = "p-orig"
COPY = "p-copy"


def _tables() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "agent_profiles": [
            {"profile_id": ORIG, "agent_kind": "business", "slug": "business_default",
             "environment": "staging", "status": "active", "is_active": True,
             "display_name": "Demo original", "model_main_id": "m1",
             "prompt_version_ids": {"system": "v1"}},
            {"profile_id": COPY, "agent_kind": "business", "slug": "business_default-fork-1",
             "environment": "staging", "status": "draft", "is_active": True,
             "display_name": "Demo original (fork)", "model_main_id": "m1",
             "prompt_version_ids": {"system": "v2"}},
        ],
        # ONE prompt row shared by every profile of the kind.
        "agent_prompts": [
            {"prompt_id": "pr-sys", "agent_kind": "business", "slug": "system"},
            {"prompt_id": "pr-title", "agent_kind": "business", "slug": "title"},
        ],
        "agent_prompt_versions": [
            {"prompt_version_id": "v1", "prompt_id": "pr-sys", "version_number": 1,
             "content": "ORIGINAL instructions"},
            {"prompt_version_id": "v2", "prompt_id": "pr-sys", "version_number": 2,
             "content": "COPY instructions (saved on the draft)"},
            {"prompt_version_id": "t1", "prompt_id": "pr-title", "version_number": 1,
             "content": "title v1"},
            {"prompt_version_id": "t2", "prompt_id": "pr-title", "version_number": 2,
             "content": "title v2"},
        ],
        "agent_tools": [],
        "agent_data_sources": [],
        "agent_mcp_servers": [],
        "agent_mcp_tools": [],
        "agent_models": [
            {"model_id": "m1", "name": "Gemini", "provider": "google",
             "model_string": "gemini", "is_active": True, "environment": "staging"},
        ],
        "agent_e2e_runs": [],
        "agent_profile_mcp_servers": [],
        "agent_profile_skills": [],
        "agent_skills": [],
    }


def _ctx(monkeypatch, fake, profile_id):
    import admin.screens.agents as ag

    monkeypatch.setattr("admin.router.client_for", lambda env: fake)
    return ag.load_agents_context(env="staging", selected_kind=None, profile_id=profile_id)


def _system(ctx):
    return next(p for p in ctx["prompts"] if p["slug"] == "system")


def test_original_editor_shows_its_own_pin_not_the_copys_newer_save(monkeypatch):
    ctx = _ctx(monkeypatch, FakeSupabase(_tables()), ORIG)
    sp = _system(ctx)
    assert sp["content"] == "ORIGINAL instructions"
    assert sp["version_number"] == 1
    assert sp["latest_version_number"] == 2


def test_copy_editor_shows_the_copys_pin(monkeypatch):
    ctx = _ctx(monkeypatch, FakeSupabase(_tables()), COPY)
    assert _system(ctx)["content"] == "COPY instructions (saved on the draft)"


def test_restored_pin_is_what_the_editor_shows(monkeypatch):
    # Versions-tab Restore writes the snapshot's prompt_version_ids back onto
    # the row. A profile pinned to v1 while v2 exists is exactly the
    # post-restore state; the editor must show v1.
    tables = _tables()
    tables["agent_profiles"][1]["prompt_version_ids"] = {"system": "v1"}
    ctx = _ctx(monkeypatch, FakeSupabase(tables), COPY)
    assert _system(ctx)["content"] == "ORIGINAL instructions"


def test_unpinned_prompt_falls_back_to_newest(monkeypatch):
    ctx = _ctx(monkeypatch, FakeSupabase(_tables()), ORIG)
    title = next(p for p in ctx["prompts"] if p["slug"] == "title")
    assert title["content"] == "title v2"


def test_pin_into_another_prompts_history_is_ignored(monkeypatch):
    tables = _tables()
    tables["agent_profiles"][0]["prompt_version_ids"] = {"system": "t1"}
    ctx = _ctx(monkeypatch, FakeSupabase(tables), ORIG)
    assert _system(ctx)["content"] == "COPY instructions (saved on the draft)"


def _fork_tables():
    t = _tables()
    t["agent_profiles"] = [dict(t["agent_profiles"][0])]
    t["agent_profile_mcp_servers"] = [
        {"profile_id": ORIG, "mcp_server_id": "bundle-1",
         "tool_subset": ["get_bookings", "get_sales"], "position": 0},
        {"profile_id": ORIG, "mcp_server_id": "ext-2", "tool_subset": None, "position": 1},
        {"profile_id": "someone-else", "mcp_server_id": "bundle-9",
         "tool_subset": [], "position": 0},
    ]
    t["agent_profile_skills"] = [
        {"profile_id": ORIG, "skill_id": "skill-1", "position": 0},
    ]
    return t


def test_fork_carries_toolsets_and_skills(monkeypatch):
    fake = FakeSupabase(_fork_tables())
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    monkeypatch.setattr(profiles, "invalidate_profile_loader", lambda *a: None)
    with patch("core.auth.admin_service_client", return_value=fake):
        new = profiles.fork_profile_row(profile_id=ORIG, actor_user_id="u")

    new_id = new["profile_id"]
    mcps = [r for r in fake.tables["agent_profile_mcp_servers"] if r["profile_id"] == new_id]
    assert {r["mcp_server_id"]: r["tool_subset"] for r in mcps} == {
        "bundle-1": ["get_bookings", "get_sales"], "ext-2": None,
    }
    assert {r["mcp_server_id"]: r["position"] for r in mcps} == {"bundle-1": 0, "ext-2": 1}
    skills = [r for r in fake.tables["agent_profile_skills"] if r["profile_id"] == new_id]
    assert [r["skill_id"] for r in skills] == ["skill-1"]
    # The original's rows are untouched and nobody else's leak in.
    assert len([r for r in fake.tables["agent_profile_mcp_servers"]
                if r["profile_id"] == ORIG]) == 2
    assert not any(r["mcp_server_id"] == "bundle-9" and r["profile_id"] == new_id
                   for r in fake.tables["agent_profile_mcp_servers"])


def test_fork_keeps_the_originals_pins_and_row_level_attachments(monkeypatch):
    tables = _fork_tables()
    tables["agent_profiles"][0].update(
        data_source_ids=["kb-1"], downstream_profile_ids=["sub-1"])
    fake = FakeSupabase(tables)
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    monkeypatch.setattr(profiles, "invalidate_profile_loader", lambda *a: None)
    with patch("core.auth.admin_service_client", return_value=fake):
        new = profiles.fork_profile_row(profile_id=ORIG, actor_user_id="u")

    assert new["data_source_ids"] == ["kb-1"]
    assert new["downstream_profile_ids"] == ["sub-1"]
    assert new["prompt_version_ids"] == {"system": "v1"}
    # A later save on the copy repoints only the copy's dict, never the
    # original's (a shared dict object would be the same bug in-process).
    new["prompt_version_ids"]["system"] = "v2"
    assert fake.tables["agent_profiles"][0]["prompt_version_ids"] == {"system": "v1"}
