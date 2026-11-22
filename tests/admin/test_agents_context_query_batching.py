"""load_agents_context batches its independent reads across a thread pool and
stops issuing redundant round-trips.

The /admin/agents page was ~11s in prod because it fired ~20 small, *serial*,
synchronous PostgREST queries and each round-trip has a ~0.3-1s floor (network
RTT + PostgREST overhead) independent of payload size. These tests pin the
round-trip reductions that constitute the fix:

* the per-prompt ``agent_prompt_versions`` loop is now a single ``.in_()``
  query (was an N+1);
* ``agent_tools`` / ``agent_mcp_servers`` are fetched once, not twice (the
  ``_attachable_mcps`` re-fetch is gone — the lists are handed in);
* the independent reads actually overlap (a per-query sleep proves the batch
  finishes in far less than the serial sum).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Chainable postgrest-style query with REAL filtering (mirrors the fake in
    test_agents_context_mcp_attachments.py) plus per-execute bookkeeping."""

    def __init__(self, table, rows, calls, sleep):
        self._table = table
        self._rows = list(rows)
        self._calls = calls
        self._sleep = sleep

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        allowed = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        if self._sleep:
            time.sleep(self._sleep)
        # list.append is atomic under the GIL, so the batch's threads can all
        # record into this list without a lock.
        self._calls.append(self._table)
        return _Result(self._rows)


class CountingSupabase:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]], sleep: float = 0.0):
        self._tables = tables
        self._sleep = sleep
        self.calls: List[str] = []

    def table(self, name):
        return _Query(name, self._tables.get(name, []), self.calls, self._sleep)


def _tables() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "agent_profiles": [
            {"profile_id": "p1", "agent_kind": "business", "environment": "prod",
             "status": "active", "is_active": True, "model_main_id": "m1"},
        ],
        # 3 prompts — the old code fired one agent_prompt_versions query PER
        # prompt; the batch fires exactly one for all of them.
        "agent_prompts": [
            {"prompt_id": "pr1", "agent_kind": "business"},
            {"prompt_id": "pr2", "agent_kind": "business"},
            {"prompt_id": "pr3", "agent_kind": "business"},
        ],
        "agent_prompt_versions": [
            {"prompt_id": "pr1", "version_number": 2, "content": "pr1-v2"},
            {"prompt_id": "pr1", "version_number": 1, "content": "pr1-v1"},
            {"prompt_id": "pr2", "version_number": 1, "content": "pr2-v1"},
            {"prompt_id": "pr3", "version_number": 3, "content": "pr3-v3"},
            {"prompt_id": "pr3", "version_number": 2, "content": "pr3-v2"},
        ],
        "agent_tools": [
            {"tool_id": "t1", "python_name": "search_docs", "is_deprecated": False},
            {"tool_id": "t2", "python_name": "old_tool", "is_deprecated": True},
        ],
        "agent_data_sources": [{"source_id": "d1", "name": "kb"}],
        "agent_mcp_servers": [
            {"mcp_server_id": "u-bundle", "name": "Sales Docs", "source": "bundle",
             "is_builtin": False, "is_active": True},
        ],
        "agent_mcp_tools": [
            {"mcp_server_id": "u-bundle", "tool_id": "t1", "position": 0},
            {"mcp_server_id": "u-bundle", "tool_id": "t2", "position": 1},
        ],
        "agent_models": [
            {"model_id": "m1", "name": "Gemini", "provider": "google",
             "model_string": "gemini", "is_active": True, "environment": "prod"},
        ],
        "agent_e2e_runs": [],
        "agent_profile_mcp_servers": [],
        "agent_profile_skills": [],
        "agent_skills": [],
    }


def _load(monkeypatch, fake):
    import admin.screens.agents as ag

    monkeypatch.setattr("admin.router.client_for", lambda env: fake)
    return ag.load_agents_context(env="prod", selected_kind=None)


def test_prompt_versions_single_query_no_n_plus_one(monkeypatch):
    fake = CountingSupabase(_tables())
    ctx = _load(monkeypatch, fake)

    # 3 prompts, but agent_prompt_versions is queried exactly ONCE (the N+1
    # guard survives the profile-version-lifecycle change — Task 2.3 dropped
    # the per-prompt version-selector UI, so agents.html no longer needs every
    # version grouped by prompt; the loader now attaches only each prompt's
    # LATEST content, still in a single batched .in_() query).
    assert fake.calls.count("agent_prompt_versions") == 1

    # Each prompt carries its latest (highest version_number) content inline.
    content_by_prompt = {p["prompt_id"]: p["content"] for p in ctx["prompts"]}
    assert content_by_prompt["pr1"] == "pr1-v2"
    assert content_by_prompt["pr2"] == "pr2-v1"
    assert content_by_prompt["pr3"] == "pr3-v3"


def test_tools_and_mcp_servers_fetched_once_dedup(monkeypatch):
    fake = CountingSupabase(_tables())
    _load(monkeypatch, fake)

    # Previously each of these was fetched twice per load — once for the
    # mapping editor and again inside _attachable_mcps.
    assert fake.calls.count("agent_tools") == 1
    assert fake.calls.count("agent_mcp_servers") == 1


def test_attachable_mcps_resolves_names_from_passed_tools(monkeypatch):
    fake = CountingSupabase(_tables())
    ctx = _load(monkeypatch, fake)

    by_id = {r["mcp_server_id"]: r for r in ctx["attachable_mcps"]}
    # Bundle tool-name resolution still works off the handed-in (deduped)
    # non-deprecated tools: t1 (search_docs) surfaces, deprecated t2 is dropped.
    assert by_id["u-bundle"]["tool_names"] == ["search_docs"]


def test_batch_runs_concurrently(monkeypatch):
    # Each query sleeps 50ms. The reads are independent, so the batch's wall
    # time must be well under the serial sum — proving they overlap.
    fake = CountingSupabase(_tables(), sleep=0.05)

    start = time.monotonic()
    _load(monkeypatch, fake)
    elapsed = time.monotonic() - start

    n = len(fake.calls)
    serial = n * 0.05
    assert n >= 8, f"expected >=8 queries exercised, got {n}"
    assert elapsed < serial * 0.6, (
        f"elapsed {elapsed:.3f}s not < 0.6 * serial {serial:.3f}s "
        f"({n} queries) — reads did not overlap"
    )
