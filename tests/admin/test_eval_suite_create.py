"""Tests for the create-suite handler in `admin.eval_handlers`.

Covers POST /admin/evals/suites:
  * happy path — row inserted with the right fields, redirect to the new
    suite's editor.
  * validation failures — empty name, bad agent_kind, threshold out of
    range (and non-numeric).

`get_service_client` is stubbed; no network/DB.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSuiteDB:
    """Minimal supabase fake covering insert() for agent_eval_suites."""

    def __init__(self, returned_id="new-suite-1"):
        self._returned_id = returned_id
        self.insert_payload = None

    def table(self, name):
        assert name == "agent_eval_suites"
        return self

    def insert(self, payload):
        self.insert_payload = payload
        return self

    def execute(self):
        result = MagicMock()
        result.data = [{"suite_id": self._returned_id}] if self._returned_id else []
        return result


def _fake_superuser():
    return {"user_token": "x", "user_id": "u-1", "email": "a@b.c"}


def _post(form_data, fake_db, follow_redirects=False):
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
            "/admin/evals/suites", data=form_data, follow_redirects=follow_redirects
        )
    finally:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_suite_happy_path_inserts_and_redirects_to_editor():
    fake_db = _FakeSuiteDB(returned_id="new-suite-1")
    r = _post(
        {
            "name": "sample_grounding",
            "agent_kind": "sample_agent",
            "pass_threshold": "0.8",
            "evaluator_model_id": "model-abc",
            "is_active": "on",
        },
        fake_db,
    )
    assert r.status_code == 302, r.text[:400]
    assert r.headers["location"] == "/admin/evals/suites/new-suite-1"

    payload = fake_db.insert_payload
    assert payload is not None
    assert payload["name"] == "sample_grounding"
    assert payload["agent_kind"] == "sample_agent"
    assert payload["pass_threshold"] == 0.8
    assert payload["scenarios"] == []
    assert payload["is_active"] is True
    assert payload["evaluator_model_id"] == "model-abc"


def test_create_suite_defaults_threshold_and_inactive_when_unchecked():
    fake_db = _FakeSuiteDB()
    r = _post(
        {"name": "sales_basics", "agent_kind": "sample_other"},
        fake_db,
    )
    assert r.status_code == 302
    payload = fake_db.insert_payload
    # default threshold 0.7, is_active False (checkbox absent), no evaluator key
    assert payload["pass_threshold"] == 0.7
    assert payload["is_active"] is False
    assert "evaluator_model_id" not in payload


def test_create_suite_blank_evaluator_omits_key():
    fake_db = _FakeSuiteDB()
    r = _post(
        {
            "name": "x",
            "agent_kind": "sample_agent",
            "evaluator_model_id": "   ",
            "is_active": "on",
        },
        fake_db,
    )
    assert r.status_code == 302
    assert "evaluator_model_id" not in fake_db.insert_payload


def test_create_suite_falls_back_to_list_when_insert_returns_no_row():
    fake_db = _FakeSuiteDB(returned_id=None)
    r = _post({"name": "x", "agent_kind": "sample_other"}, fake_db)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/evals/suites"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def test_create_suite_rejects_empty_name():
    fake_db = _FakeSuiteDB()
    r = _post({"name": "  ", "agent_kind": "sample_agent"}, fake_db)
    assert r.status_code == 400
    assert fake_db.insert_payload is None


def test_create_suite_rejects_bad_agent_kind():
    fake_db = _FakeSuiteDB()
    r = _post({"name": "ok", "agent_kind": "nonsense"}, fake_db)
    assert r.status_code == 400
    assert fake_db.insert_payload is None


def test_create_suite_rejects_threshold_above_one():
    fake_db = _FakeSuiteDB()
    r = _post(
        {"name": "ok", "agent_kind": "sample_agent", "pass_threshold": "1.5"}, fake_db
    )
    assert r.status_code == 400
    assert fake_db.insert_payload is None


def test_create_suite_rejects_threshold_below_zero():
    fake_db = _FakeSuiteDB()
    r = _post(
        {"name": "ok", "agent_kind": "sample_agent", "pass_threshold": "-0.1"}, fake_db
    )
    assert r.status_code == 400
    assert fake_db.insert_payload is None


def test_create_suite_rejects_non_numeric_threshold():
    fake_db = _FakeSuiteDB()
    r = _post(
        {"name": "ok", "agent_kind": "sample_agent", "pass_threshold": "high"}, fake_db
    )
    assert r.status_code == 400
    assert fake_db.insert_payload is None


# ---------------------------------------------------------------------------
# Import sanity — module still imports after dataset-code removal
# ---------------------------------------------------------------------------


def test_eval_handlers_imports_after_dataset_removal():
    from admin import eval_handlers  # noqa: F401

    # Dataset-import symbols are gone.
    for removed in (
        "datapoints_to_scenarios",
        "_datapoint_to_scenario",
        "_first_str",
        "_fetch_laminar_datasets",
        "eval_datasets_list",
        "eval_dataset_detail",
        "eval_dataset_import",
    ):
        assert not hasattr(eval_handlers, removed), f"{removed} should be removed"

    # New create handler is present.
    assert hasattr(eval_handlers, "eval_suite_create")


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
