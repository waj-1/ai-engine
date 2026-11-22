"""seed_eval_suites: profile/judge binding + per-suite failure isolation.

Three bugs pinned here (2026-08-26):

* Seeded suites landed with NULL `profile_id`, which silently hid every one
  of them from the bake-off wizard (`admin/eval_runs_handlers.
  _load_suites_by_profile` drops NULL-bound suites) — the seeder now binds
  each insert to the kind's ACTIVE profile in the seeder's environment, and
  backfills NULL `profile_id` / `evaluator_model_id` on already-seeded rows
  under a ONE-SHOT gate: only rows never modified since seeding
  (`updated_at == created_at`) are eligible, the backfill's own UPDATE
  writes `updated_at` (so each row is filled at most once, ever), and an
  operator edit — including deliberately CLEARING a binding to NULL, which
  the suite editor supports — bumps `updated_at` and permanently opts the
  row out. On a pristine row, non-NULL values are still never overwritten.
* `_resolve_judge_model_id` only fell back to Gemini rows, so a Claude-only
  catalog seeded every judged suite evaluator-less (and the bake-off
  pre-flight then refused creation). It now tries Claude too — the only
  other provider `admin/e2e_runner._try_build_judge` can build — and both
  fallbacks are scoped to the seeder's environment (`agent_models` is
  env-scoped in the shared AI DB, so an unscoped ilike could hand back
  another environment's row).
* The insert loop was un-guarded: one bad suite aborted every later suite.
  Each suite now seeds inside its own try/except.

All tests run against a fake in-memory Supabase client — never a live DB.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

import core.agent_profile as agent_profile_mod
from admin import seed_eval_suites as seed_mod


# ---------------------------------------------------------------------------
# Fake Supabase client: filter-aware select/insert/update over dict tables
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db: "_FakeDB", table: str):
        self._db, self._table = db, table
        self._filters: Dict[str, Any] = {}
        self._op, self._payload = "select", None

    def select(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters[col] = ("eq", val)
        return self

    def ilike(self, col, pattern):
        self._filters[col] = ("ilike", pattern)
        return self

    def insert(self, row):
        self._op, self._payload = "insert", row
        return self

    def update(self, patch):
        self._op, self._payload = "update", patch
        return self

    def _matches(self, row) -> bool:
        for col, (op, val) in self._filters.items():
            if op == "ilike":
                if val.strip("%").lower() not in str(row.get(col, "")).lower():
                    return False
            elif row.get(col) != val:
                return False
        return True

    def execute(self):
        rows = self._db.tables.setdefault(self._table, [])
        if self._op == "insert":
            self._db.inserts.append((self._table, dict(self._payload)))
            rows.append(dict(self._payload))
            return _Result([dict(self._payload)])
        if self._op == "update":
            matched = [r for r in rows if self._matches(r)]
            for r in matched:
                r.update(self._payload)  # mirrors the DB: the patch lands on the row
            self._db.updates.append(
                (self._table, dict(self._payload),
                 {c: v for c, (_o, v) in self._filters.items()})
            )
            return _Result([dict(r) for r in matched])
        return _Result([dict(r) for r in rows if self._matches(r)])


class _FakeDB:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.inserts: List = []
        self.updates: List = []

    def table(self, name):
        return _Query(self, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENV = "staging"

# A pristine seeded row: created_at == updated_at (both default to the same
# now() in the seeder's INSERT, which sets neither).
_SEED_TS = "2026-08-20T09:00:00+00:00"
# An operator-touched row: updated_at bumped past created_at (by the
# set_updated_at trigger, or the suite editor's explicit updated_at).
_EDIT_TS = "2026-08-21T14:30:00+00:00"

_ACTIVE_SALES_PROFILE = {
    "profile_id": "p-sales", "agent_kind": "sample_other", "environment": _ENV,
    "is_active": True, "status": "active",
}

_CHAT_SUITE = {
    "name": "sales_binding_probe", "agent_kind": "sample_other",
    "scenarios": [{"id": "s1", "question": "q", "expected_answer": "a"}],
    "pass_threshold": 0.8,
}

_JUDGED_SUITE = {
    "name": "sales_judged_probe", "agent_kind": "sample_other", "evaluator": True,
    "scenarios": [{"id": "s1", "question": "q", "expected_answer": "a"}],
    "pass_threshold": 0.8,
}

_GEMINI_MODEL = {
    "model_id": "m-gem", "model_string": "gemini-3-flash",
    "is_active": True, "environment": _ENV,
}


def _run(monkeypatch, tables=None, suites=(), db: Optional[_FakeDB] = None):
    """Run seed_eval_suites.run() against a fake client. Pass `db` to re-run
    against the SAME state (the one-shot / repeat-boot tests)."""
    if db is None:
        db = _FakeDB(tables or {})
    monkeypatch.setattr(seed_mod, "_SUITES", list(suites))
    monkeypatch.setattr(seed_mod, "active_scope", lambda: None)  # WAJ build
    monkeypatch.setattr(seed_mod, "get_service_client", lambda: db)
    monkeypatch.setattr(agent_profile_mod, "_default_environment", lambda: _ENV)
    result = seed_mod.run()
    return result, db


def _suite_inserts(db):
    return [row for (t, row) in db.inserts if t == "agent_eval_suites"]


def _suite_updates(db):
    return [(patch, flt) for (t, patch, flt) in db.updates if t == "agent_eval_suites"]


# ---------------------------------------------------------------------------
# Insert path: profile binding
# ---------------------------------------------------------------------------


def test_insert_binds_suite_to_kinds_active_profile(monkeypatch):
    result, db = _run(
        monkeypatch,
        {"agent_profiles": [_ACTIVE_SALES_PROFILE], "agent_eval_suites": []},
        [dict(_CHAT_SUITE)],
    )
    rows = _suite_inserts(db)
    assert len(rows) == 1
    assert rows[0]["profile_id"] == "p-sales"
    assert result == {"inserted": 1, "skipped": 0}


def test_insert_ignores_profiles_that_are_not_active(monkeypatch):
    """Resolution mirrors AgentProfileLoader._load_from_db: status='active' +
    is_active + the seeder's environment. A draft fork, an inactive row, or a
    row in another environment must NOT be picked — the suite seeds unbound
    ("unassigned" in the portal) instead of bound to the wrong profile."""
    wrong_profiles = [
        {**_ACTIVE_SALES_PROFILE, "profile_id": "p-draft", "status": "draft"},
        {**_ACTIVE_SALES_PROFILE, "profile_id": "p-inactive", "is_active": False},
        {**_ACTIVE_SALES_PROFILE, "profile_id": "p-prod", "environment": "prod"},
    ]
    _result, db = _run(
        monkeypatch,
        {"agent_profiles": wrong_profiles, "agent_eval_suites": []},
        [dict(_CHAT_SUITE)],
    )
    rows = _suite_inserts(db)
    assert len(rows) == 1
    assert "profile_id" not in rows[0]


# ---------------------------------------------------------------------------
# Existing rows: one-shot NULL backfill on PRISTINE rows only
# ---------------------------------------------------------------------------


def test_pristine_row_with_null_profile_id_gets_backfilled(monkeypatch):
    existing = {
        "suite_id": "su-1", "name": _CHAT_SUITE["name"],
        "profile_id": None, "evaluator_model_id": None,
        "created_at": _SEED_TS, "updated_at": _SEED_TS,  # never touched
    }
    result, db = _run(
        monkeypatch,
        {"agent_profiles": [_ACTIVE_SALES_PROFILE], "agent_eval_suites": [existing]},
        [dict(_CHAT_SUITE)],
    )
    assert _suite_inserts(db) == []  # still insert-if-missing on the row
    assert result == {"inserted": 0, "skipped": 1}
    updates = _suite_updates(db)
    assert len(updates) == 1
    patch, flt = updates[0]
    assert flt == {"suite_id": "su-1"}
    assert patch["profile_id"] == "p-sales"
    # One-shot marker: the backfill writes updated_at itself, so it works
    # even on a stack whose set_updated_at trigger (db-functions) never ran.
    assert patch["updated_at"] and patch["updated_at"] != _SEED_TS
    assert set(patch) == {"profile_id", "updated_at"}


def test_operator_cleared_binding_stays_cleared(monkeypatch):
    """THE regression this review round is about: the suite editor supports
    deliberately CLEARING profile_id/evaluator_model_id to NULL, and any
    operator save bumps updated_at. A cleared (touched) row must stay NULL
    across every later boot — the seeder must never re-fill it."""
    cleared = {
        "suite_id": "su-1", "name": _JUDGED_SUITE["name"],
        "profile_id": None, "evaluator_model_id": None,
        "created_at": _SEED_TS, "updated_at": _EDIT_TS,  # operator touched
    }
    tables = {
        "agent_profiles": [_ACTIVE_SALES_PROFILE],
        "agent_eval_suites": [cleared],
        "agent_models": [dict(_GEMINI_MODEL)],  # a judge IS available — still no re-fill
    }
    result, db = _run(monkeypatch, tables, [dict(_JUDGED_SUITE)])
    assert result == {"inserted": 0, "skipped": 1}
    assert _suite_updates(db) == []
    # And again on the NEXT boot (same DB state): still untouched.
    result2, db = _run(monkeypatch, suites=[dict(_JUDGED_SUITE)], db=db)
    assert result2 == {"inserted": 0, "skipped": 1}
    assert _suite_updates(db) == []
    row = db.tables["agent_eval_suites"][0]
    assert row["profile_id"] is None
    assert row["evaluator_model_id"] is None


def test_backfill_is_one_shot_across_boots(monkeypatch):
    """The backfill's own UPDATE writes updated_at, so the row it just filled
    is no longer pristine — a second boot performs NO further update."""
    existing = {
        "suite_id": "su-1", "name": _CHAT_SUITE["name"],
        "profile_id": None, "evaluator_model_id": None,
        "created_at": _SEED_TS, "updated_at": _SEED_TS,
    }
    _result, db = _run(
        monkeypatch,
        {"agent_profiles": [_ACTIVE_SALES_PROFILE], "agent_eval_suites": [existing]},
        [dict(_CHAT_SUITE)],
    )
    assert len(_suite_updates(db)) == 1  # boot 1: filled once
    _result2, db = _run(monkeypatch, suites=[dict(_CHAT_SUITE)], db=db)
    assert len(_suite_updates(db)) == 1  # boot 2: nothing more, ever


def test_row_without_timestamps_is_treated_as_touched(monkeypatch):
    """Fail safe: when the row can't be proven pristine (timestamps missing/
    unreadable), the seeder keeps its hands off."""
    existing = {
        "suite_id": "su-1", "name": _CHAT_SUITE["name"],
        "profile_id": None, "evaluator_model_id": None,
        # no created_at / updated_at
    }
    _result, db = _run(
        monkeypatch,
        {"agent_profiles": [_ACTIVE_SALES_PROFILE], "agent_eval_suites": [existing]},
        [dict(_CHAT_SUITE)],
    )
    assert _suite_updates(db) == []


def test_pristine_non_null_binding_is_not_rewritten(monkeypatch):
    """A row the seeder itself bound at insert time (pristine, both columns
    non-NULL) needs nothing on the next boot: NULL-only fill means no patch,
    no update — the row stays byte-identical."""
    existing = {
        "suite_id": "su-1", "name": _JUDGED_SUITE["name"],
        "profile_id": "p-earlier", "evaluator_model_id": "m-earlier",
        "created_at": _SEED_TS, "updated_at": _SEED_TS,
    }
    tables = {
        "agent_profiles": [_ACTIVE_SALES_PROFILE],
        "agent_eval_suites": [existing],
        "agent_models": [dict(_GEMINI_MODEL)],
    }
    result, db = _run(monkeypatch, tables, [dict(_JUDGED_SUITE)])
    assert result == {"inserted": 0, "skipped": 1}
    assert _suite_updates(db) == []
    row = db.tables["agent_eval_suites"][0]
    assert row["profile_id"] == "p-earlier"
    assert row["evaluator_model_id"] == "m-earlier"


def test_pristine_judged_suite_with_null_evaluator_gets_judge_backfilled(monkeypatch):
    existing = {
        "suite_id": "su-1", "name": _JUDGED_SUITE["name"],
        "profile_id": "p-sales", "evaluator_model_id": None,
        "created_at": _SEED_TS, "updated_at": _SEED_TS,
    }
    tables = {
        "agent_profiles": [_ACTIVE_SALES_PROFILE],
        "agent_eval_suites": [existing],
        "agent_models": [dict(_GEMINI_MODEL)],
    }
    _result, db = _run(monkeypatch, tables, [dict(_JUDGED_SUITE)])
    updates = _suite_updates(db)
    assert len(updates) == 1
    patch, flt = updates[0]
    assert flt == {"suite_id": "su-1"}
    assert patch["evaluator_model_id"] == "m-gem"
    assert set(patch) == {"evaluator_model_id", "updated_at"}


# ---------------------------------------------------------------------------
# Judge resolution: Claude fallback, env scoping, honest None
# ---------------------------------------------------------------------------


def test_judge_falls_back_to_active_claude_when_no_gemini():
    db = _FakeDB({
        "agent_profiles": [],
        "agent_models": [
            {"model_id": "m-gpt", "model_string": "gpt-5-mini",
             "is_active": True, "environment": _ENV},
            {"model_id": "m-gem-off", "model_string": "gemini-3-flash",
             "is_active": False, "environment": _ENV},
            {"model_id": "m-claude", "model_string": "claude-sonnet-4-5",
             "is_active": True, "environment": _ENV},
        ],
    })
    assert seed_mod._resolve_judge_model_id(db, _ENV) == "m-claude"


def test_judge_prefers_gemini_over_claude():
    db = _FakeDB({
        "agent_profiles": [],
        "agent_models": [
            {"model_id": "m-claude", "model_string": "claude-sonnet-4-5",
             "is_active": True, "environment": _ENV},
            {"model_id": "m-gem", "model_string": "gemini-3-flash",
             "is_active": True, "environment": _ENV},
        ],
    })
    assert seed_mod._resolve_judge_model_id(db, _ENV) == "m-gem"


def test_judge_fallback_is_env_scoped():
    """agent_models is env-scoped in the shared AI DB: another environment's
    Gemini row must not be picked as this environment's judge."""
    db = _FakeDB({
        "agent_profiles": [],
        "agent_models": [
            {"model_id": "m-gem-prod", "model_string": "gemini-3-flash",
             "is_active": True, "environment": "prod"},
            {"model_id": "m-claude-stg", "model_string": "claude-sonnet-4-5",
             "is_active": True, "environment": _ENV},
        ],
    })
    assert seed_mod._resolve_judge_model_id(db, _ENV) == "m-claude-stg"
    assert seed_mod._resolve_judge_model_id(db, "prod") == "m-gem-prod"


def test_judge_is_none_when_no_judge_capable_model_exists():
    """An Azure-only catalog has NO buildable judge (`_try_build_judge`
    supports Gemini + Claude only) — the honest answer is None, never an
    unusable row that would 404 at judge-call time."""
    db = _FakeDB({
        "agent_profiles": [],
        "agent_models": [
            {"model_id": "m-gpt", "model_string": "gpt-5-mini",
             "is_active": True, "environment": _ENV},
        ],
    })
    assert seed_mod._resolve_judge_model_id(db, _ENV) is None


# ---------------------------------------------------------------------------
# Per-suite failure isolation
# ---------------------------------------------------------------------------


def test_one_bad_suite_does_not_abort_the_rest(monkeypatch, caplog):
    """A suite whose seed raises (here: a malformed entry with no
    pass_threshold) must be logged by name and skipped — the suites after it
    still seed. Before the per-suite guard, the exception aborted the whole
    loop and every later suite silently never existed."""
    import logging

    bad = {
        "name": "broken_probe", "agent_kind": "sample_other",
        "scenarios": [{"id": "s1", "question": "q"}],
        # no pass_threshold -> KeyError at row build
    }
    with caplog.at_level(logging.WARNING):
        result, db = _run(
            monkeypatch,
            {"agent_profiles": [_ACTIVE_SALES_PROFILE], "agent_eval_suites": []},
            [bad, dict(_CHAT_SUITE)],
        )
    rows = _suite_inserts(db)
    assert [r["name"] for r in rows] == [_CHAT_SUITE["name"]]
    assert result == {"inserted": 1, "skipped": 0}
    assert any("broken_probe" in r.getMessage() for r in caplog.records)


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
