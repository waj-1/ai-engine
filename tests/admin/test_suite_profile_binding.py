"""Tests for suite<->profile binding (Task 2.2).

Covers:
  * `admin.eval_handlers._sync_agent_kind` — the profile_id -> agent_kind
    lookup every suite create/update funnels through (brief's two tests,
    transcribed verbatim).
  * POST /admin/evals/suites (create) derives agent_kind from profile_id
    when supplied, and rejects an unknown profile_id.
  * `build_evals_page_context`'s `profile_id` filter narrows recent_runs.
  * POST /admin/evals/suites/{id} (update/save): clearing the binding
    (profile_id present-but-empty), back-compat when the field is absent,
    deriving agent_kind, and rejecting an unknown profile_id.
  * Scoped-build narrowing of the profiles list feeding both pickers
    (GET /admin/evals/suites/{id} and build_evals_page_context).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin.eval_handlers import _sync_agent_kind
from tests.scoped_build import full_build, make_scope, use_scope


# ---------------------------------------------------------------------------
# _sync_agent_kind — brief's Step 1 tests, transcribed verbatim
# ---------------------------------------------------------------------------


def test_sync_agent_kind_returns_profile_kind():
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"profile_id": "p1", "agent_kind": "sample_other"}]
    assert _sync_agent_kind(client, "p1") == "sample_other"


def test_sync_agent_kind_missing_profile_returns_empty():
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    assert _sync_agent_kind(client, "nope") == ""


def test_sync_agent_kind_empty_profile_id_returns_empty_without_querying():
    """No profile_id -> short-circuit, no DB round-trip."""
    client = MagicMock()
    assert _sync_agent_kind(client, "") == ""
    client.table.assert_not_called()


# ---------------------------------------------------------------------------
# Create-suite handler: profile_id -> derived agent_kind
# ---------------------------------------------------------------------------


class _FakeMultiTableDB:
    """Fake supabase client covering both `agent_eval_suites` (insert) and
    `agent_profiles` (select/eq/limit/execute) — enough for the create-suite
    handler's profile-binding path. Mirrors the chain shape asserted by
    `_sync_agent_kind`'s own tests above."""

    def __init__(self, profile_row=None, returned_suite_id="new-suite-1"):
        self._profile_row = profile_row
        self._returned_suite_id = returned_suite_id
        self.insert_payload = None
        self._table = None

    def table(self, name):
        self._table = name
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, payload):
        self.insert_payload = payload
        return self

    def execute(self):
        result = MagicMock()
        if self._table == "agent_profiles":
            result.data = [self._profile_row] if self._profile_row else []
        elif self._table == "agent_eval_suites":
            result.data = [{"suite_id": self._returned_suite_id}] if self._returned_suite_id else []
        else:
            result.data = []
        return result


def _fake_superuser():
    return {"user_token": "x", "user_id": "u-1", "email": "a@b.c"}


def _post_create_suite(form_data, fake_db):
    from fastapi.testclient import TestClient
    from admin import auth, eval_handlers
    from main import app

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(eval_handlers, "get_service_client", return_value=fake_db),
        patch.object(eval_handlers, "record_change"),
    ]
    for p in patches:
        p.start()
    try:
        client = TestClient(app)
        return client.post(
            "/admin/evals/suites", data=form_data, follow_redirects=False
        )
    finally:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)


def test_create_suite_with_profile_id_derives_agent_kind():
    fake_db = _FakeMultiTableDB(profile_row={"profile_id": "p1", "agent_kind": "sample_other"})
    r = _post_create_suite(
        {
            "name": "sales_smoke",
            # A stale/mismatched agent_kind in the form must be overridden by
            # the profile's real kind, not trusted — the create dialog
            # disables this select client-side, but the server doesn't rely
            # on that.
            "agent_kind": "support_bot",
            "profile_id": "p1",
        },
        fake_db,
    )
    assert r.status_code == 302, r.text[:400]
    payload = fake_db.insert_payload
    assert payload is not None
    assert payload["agent_kind"] == "sample_other"
    assert payload["profile_id"] == "p1"


def test_create_suite_with_unknown_profile_id_rejected():
    fake_db = _FakeMultiTableDB(profile_row=None)
    r = _post_create_suite(
        {"name": "x", "profile_id": "nope"},
        fake_db,
    )
    assert r.status_code == 400
    assert fake_db.insert_payload is None


def test_create_suite_without_profile_id_keeps_legacy_agent_kind_path():
    """No profile_id supplied (legacy/API caller) -> back-compat: agent_kind
    comes straight from the form, validated against _suite_agent_kinds()."""
    fake_db = _FakeMultiTableDB()
    r = _post_create_suite(
        {"name": "legacy", "agent_kind": "sample_agent"},
        fake_db,
    )
    assert r.status_code == 302, r.text[:400]
    payload = fake_db.insert_payload
    assert payload["agent_kind"] == "sample_agent"
    assert "profile_id" not in payload


# ---------------------------------------------------------------------------
# build_evals_page_context: profile_id filter narrows recent_runs
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def select(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def gte(self, *a, **kw): return self
    def lte(self, *a, **kw): return self
    def order(self, *a, **kw): return self
    def limit(self, *a, **kw): return self
    def is_(self, *a, **kw): return self
    def in_(self, *a, **kw): return self

    def execute(self):
        r = MagicMock()
        r.data = list(self._rows)
        return r


class _FakeClient:
    def __init__(self, rows_by_table=None):
        self._rows = rows_by_table or {}

    def table(self, name):
        return _FakeQuery(self._rows.get(name, []))

    def schema(self, _name):
        return self


_SUITES = [
    {"suite_id": "s1", "name": "Booking Suite", "agent_kind": "sample_agent",
     "pass_threshold": 0.7, "is_active": True, "scenarios": [], "evaluator_model_id": None,
     "profile_id": "p1"},
]

_RUNS = [
    {"run_id": "r1", "suite_id": "s1", "profile_id": "p1", "status": "passed",
     "score": 0.9, "created_at": "2026-06-01T10:00:00Z", "results": [{"passed": True}],
     "context": {}},
    {"run_id": "r2", "suite_id": "s1", "profile_id": "p2", "status": "failed",
     "score": 0.5, "created_at": "2026-06-01T09:00:00Z", "results": [{"passed": False}],
     "context": {}},
]

_PROFILES = [
    {"profile_id": "p1", "display_name": "Profile One", "agent_kind": "sample_agent", "endpoint_path": "/chat"},
    {"profile_id": "p2", "display_name": "Profile Two", "agent_kind": "sample_agent", "endpoint_path": "/chat"},
]
_MODELS: List[Dict[str, Any]] = []
_SCHEDULES: List[Dict[str, Any]] = []
_BUSINESSES: List[Dict[str, Any]] = []


def _make_client():
    return _FakeClient({
        "agent_eval_suites": _SUITES,
        "agent_eval_runs": _RUNS,
        "agent_eval_schedules": _SCHEDULES,
        "agent_profiles": _PROFILES,
        "agent_models": _MODELS,
    })


def _build_ctx(**kwargs):
    client = _make_client()
    with patch("admin.eval_runs_handlers.get_service_client", return_value=client), \
         patch("admin._business_picker.fetch_businesses_for_picker", side_effect=lambda c: _BUSINESSES):
        from admin.eval_runs_handlers import build_evals_page_context
        return build_evals_page_context(**kwargs)


def test_profile_id_filter_narrows_recent_runs():
    ctx_all = _build_ctx()
    assert {r["run_id"] for r in ctx_all["recent_runs"]} == {"r1", "r2"}

    ctx_p1 = _build_ctx(profile_id="p1")
    assert [r["run_id"] for r in ctx_p1["recent_runs"]] == ["r1"]
    assert ctx_p1["filters"]["profile_id"] == "p1"

    ctx_p2 = _build_ctx(profile_id="p2")
    assert [r["run_id"] for r in ctx_p2["recent_runs"]] == ["r2"]


def test_recent_runs_and_suites_decorated_with_profile_display_name():
    ctx = _build_ctx()
    runs_by_id = {r["run_id"]: r for r in ctx["recent_runs"]}
    assert runs_by_id["r1"]["_profile"] == "Profile One"
    assert runs_by_id["r2"]["_profile"] == "Profile Two"

    suite = next(s for s in ctx["suites"] if s["suite_id"] == "s1")
    assert suite["_profile"] == "Profile One"


# ---------------------------------------------------------------------------
# Review fixes (Task 2.2 findings):
#   (1) eval_suite_save must be able to CLEAR a profile binding — the edit
#       form's Profile <select> always submits `profile_id`, and "— none —"
#       submits "" (present, not absent).
#   (2) the profiles list feeding the pickers/filters must be narrowed
#       on a scoped build, same as their sibling agent_kind lists.
# ---------------------------------------------------------------------------


class _FakeSaveSuiteDB:
    """Fake supabase client for eval_suite_save: `agent_eval_suites` (select
    the existing row for `before`, then update) and `agent_profiles`
    (select/eq/limit/execute for `_sync_agent_kind`). Mirrors
    `_FakeMultiTableDB`'s chain shape above (create-suite handler)."""

    def __init__(self, suite_row, profile_row=None):
        self._suite_row = suite_row
        self._profile_row = profile_row
        self.update_payload = None
        self._table = None

    def table(self, name):
        self._table = name
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def update(self, payload):
        self.update_payload = payload
        return self

    def execute(self):
        result = MagicMock()
        if self._table == "agent_profiles":
            result.data = [self._profile_row] if self._profile_row else []
        elif self._table == "agent_eval_suites":
            result.data = [self._suite_row]
        else:
            result.data = []
        return result


def _post_save_suite(suite_id, form_data, fake_db):
    from fastapi.testclient import TestClient
    from admin import auth, eval_handlers
    from main import app

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(eval_handlers, "get_service_client", return_value=fake_db),
        patch.object(eval_handlers, "record_change"),
    ]
    for p in patches:
        p.start()
    try:
        client = TestClient(app)
        return client.post(
            f"/admin/evals/suites/{suite_id}", data=form_data, follow_redirects=False
        )
    finally:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)


def test_eval_suite_save_clears_binding_when_profile_id_present_but_empty():
    """Finding 1: choosing "— none —" on the edit form submits profile_id=""
    (present, not absent) — that must null out the binding, not no-op and
    leave the stale profile_id forever. agent_kind is left untouched."""
    suite_row = {
        "suite_id": "s1", "name": "Bound Suite", "agent_kind": "sample_other",
        "profile_id": "p1", "scenarios": [],
    }
    fake_db = _FakeSaveSuiteDB(suite_row)
    r = _post_save_suite("s1", {"name": "Bound Suite", "profile_id": ""}, fake_db)
    assert r.status_code == 302, r.text[:400]
    payload = fake_db.update_payload
    assert payload is not None
    assert payload["profile_id"] is None
    assert "agent_kind" not in payload


def test_eval_suite_save_keeps_binding_when_profile_id_field_absent():
    """Back-compat: a bare programmatic PATCH that omits profile_id entirely
    must not touch profile_id/agent_kind at all (unchanged by the fix)."""
    suite_row = {
        "suite_id": "s1", "name": "Bound Suite", "agent_kind": "sample_other",
        "profile_id": "p1", "scenarios": [],
    }
    fake_db = _FakeSaveSuiteDB(suite_row)
    r = _post_save_suite("s1", {"name": "Bound Suite"}, fake_db)
    assert r.status_code == 302, r.text[:400]
    payload = fake_db.update_payload
    assert payload is not None
    assert "profile_id" not in payload
    assert "agent_kind" not in payload


def test_eval_suite_save_derives_agent_kind_when_profile_id_supplied():
    """Selecting a real profile still derives + binds agent_kind, same as
    the create path."""
    suite_row = {
        "suite_id": "s1", "name": "Suite", "agent_kind": "sample_agent",
        "profile_id": None, "scenarios": [],
    }
    fake_db = _FakeSaveSuiteDB(suite_row, profile_row={"profile_id": "p2", "agent_kind": "support_bot"})
    r = _post_save_suite("s1", {"name": "Suite", "profile_id": "p2"}, fake_db)
    assert r.status_code == 302, r.text[:400]
    payload = fake_db.update_payload
    assert payload["profile_id"] == "p2"
    assert payload["agent_kind"] == "support_bot"


def test_eval_suite_save_rejects_unknown_profile_id():
    """Finding 3(b): an unresolvable profile_id on the update path is a 400,
    same as the create path — never silently persisted."""
    suite_row = {
        "suite_id": "s1", "name": "Suite", "agent_kind": "sample_agent",
        "profile_id": None, "scenarios": [],
    }
    fake_db = _FakeSaveSuiteDB(suite_row, profile_row=None)
    r = _post_save_suite("s1", {"name": "Suite", "profile_id": "nope"}, fake_db)
    assert r.status_code == 400
    assert fake_db.update_payload is None


# ---------------------------------------------------------------------------
# Finding 2/3(c): scoped-build narrowing of the profiles list
# ---------------------------------------------------------------------------


def test_eval_suite_edit_profiles_on_a_scoped_build(monkeypatch):
    """GET /admin/evals/suites/{id}: the Profile <select> must only offer the
    active deployment scope's own profiles — mirrors the harbor_mode/waj_mode
    stand-in scope used across tests/scoped_build.py."""
    use_scope(monkeypatch, make_scope("harbor"))

    suites = [{
        "suite_id": "5e5e5e5e-0000-4000-8000-000000000001", "name": "HARBOR Suite", "agent_kind": "harbor",
        "pass_threshold": 0.7, "is_active": True, "scenarios": [],
        "evaluator_model_id": None, "profile_id": None,
    }]
    profiles = [
        {"profile_id": "p-harbor", "display_name": "HARBOR Main Profile", "agent_kind": "harbor"},
        {"profile_id": "p-biz", "display_name": "Foreign Business Profile", "agent_kind": "sample_agent"},
    ]
    fake_client = _FakeClient({
        "agent_eval_suites": suites, "agent_models": [], "agent_profiles": profiles,
    })

    from fastapi.testclient import TestClient
    from admin import auth, eval_handlers
    from main import app

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    try:
        with patch.object(eval_handlers, "get_service_client", return_value=fake_client):
            test_client = TestClient(app)
            r = test_client.get("/admin/evals/suites/5e5e5e5e-0000-4000-8000-000000000001")
    finally:
        app.dependency_overrides.pop(auth.require_superuser, None)

    assert r.status_code == 200, r.text[:500]
    assert "HARBOR Main Profile" in r.text
    assert "Foreign Business Profile" not in r.text


def test_eval_suite_edit_profiles_full_catalog_on_a_full_build(monkeypatch):
    """No-op companion: with no scope active, the picker still
    narrows to suite-eligible kinds (`_AGENT_KINDS_FOR_SUITES`, unchanged
    from before this fix) — a WAJ-catalog `business` profile still shows,
    only a scope-only kind (orchard) is excluded."""
    full_build(monkeypatch)

    suites = [{
        "suite_id": "5e5e5e5e-0000-4000-8000-000000000001", "name": "Biz Suite", "agent_kind": "sample_agent",
        "pass_threshold": 0.7, "is_active": True, "scenarios": [],
        "evaluator_model_id": None, "profile_id": None,
    }]
    profiles = [
        {"profile_id": "p-biz", "display_name": "Business Profile", "agent_kind": "sample_agent"},
        {"profile_id": "p-orchard", "display_name": "Orchard Profile", "agent_kind": "orchard"},
    ]
    fake_client = _FakeClient({
        "agent_eval_suites": suites, "agent_models": [], "agent_profiles": profiles,
    })

    from fastapi.testclient import TestClient
    from admin import auth, eval_handlers
    from main import app

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    try:
        with patch.object(eval_handlers, "get_service_client", return_value=fake_client):
            test_client = TestClient(app)
            r = test_client.get("/admin/evals/suites/5e5e5e5e-0000-4000-8000-000000000001")
    finally:
        app.dependency_overrides.pop(auth.require_superuser, None)

    assert r.status_code == 200, r.text[:500]
    assert "Business Profile" in r.text
    assert "Orchard Profile" not in r.text


def test_build_evals_page_context_profiles_on_a_scoped_build(monkeypatch):
    """Finding 2's second site: `build_eval_runs_context`'s (via
    `build_evals_page_context`) profiles list — feeds both the suites-editor
    picker context and the pre-existing Run-now picker. Mirrors `_build_ctx`
    above, with its own local fixtures (the module-level _PROFILES/_SUITES
    constants are shared by other tests and must stay untouched)."""
    use_scope(monkeypatch, make_scope("harbor"))

    suites = [{
        "suite_id": "s1", "name": "HARBOR Suite", "agent_kind": "harbor",
        "pass_threshold": 0.7, "is_active": True, "scenarios": [],
        "evaluator_model_id": None, "profile_id": "p-harbor",
    }]
    profiles = [
        {"profile_id": "p-harbor", "display_name": "HARBOR Main", "agent_kind": "harbor",
         "endpoint_path": "/harbor/chat"},
        {"profile_id": "p-biz", "display_name": "Foreign Business Profile",
         "agent_kind": "sample_agent", "endpoint_path": "/chat"},
    ]
    fake_client = _FakeClient({
        "agent_eval_suites": suites,
        "agent_eval_runs": [],
        "agent_eval_schedules": [],
        "agent_profiles": profiles,
        "agent_models": [],
    })

    with patch("admin.eval_runs_handlers.get_service_client", return_value=fake_client), \
         patch("admin._business_picker.fetch_businesses_for_picker", side_effect=lambda c: []):
        from admin.eval_runs_handlers import build_evals_page_context
        ctx = build_evals_page_context()

    assert ctx["agent_kinds"] == ("harbor",)
    ids = {p["profile_id"] for p in ctx["profiles"]}
    assert ids == {"p-harbor"}


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
