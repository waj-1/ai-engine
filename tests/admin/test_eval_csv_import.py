"""Tests for the CSV-import handler in `admin.eval_handlers`.

Covers:
  * helpers — `_slugify_question_first_words` and the inline CSV row
    parsing logic (parsed via a small reusable function we drive
    directly from the handler module).
  * POST /admin/evals/suites/{suite_id}/import — the multipart upload
    flow, fake supabase client, audit + redirect.
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------


def test_slugify_question_first_words_basic():
    from admin.eval_handlers import _slugify_question_first_words
    assert _slugify_question_first_words("What are your hours on Saturday?") == "what_are_your_hours_on_saturday"


def test_slugify_question_first_words_truncates_to_max_words():
    from admin.eval_handlers import _slugify_question_first_words
    out = _slugify_question_first_words(
        "one two three four five six seven eight nine"
    )
    # default max_words is 6
    assert out == "one_two_three_four_five_six"


def test_slugify_question_first_words_empty_returns_fallback():
    from admin.eval_handlers import _slugify_question_first_words
    assert _slugify_question_first_words("") == "scenario"
    assert _slugify_question_first_words("?!@") == "scenario"


# ---------------------------------------------------------------------------
# Inline CSV row parsing — exercised via a small local helper that mirrors
# what the handler does. We can't import a parser fn (the handler inlines
# the loop) so we replicate the loop here and verify the same set of
# rules. NB: if the handler ever drifts, these tests will start failing
# alongside the multipart handler test below — which is the contract we
# care about.
# ---------------------------------------------------------------------------


def _parse_csv_rows(text: str) -> List[Dict[str, Any]]:
    """Mirror of the CSV-row loop in admin.eval_handlers.eval_suite_import_csv."""
    from admin.eval_handlers import _slugify_question_first_words, csv_scenario_columns

    rows: List[Dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(text))
    for raw_row in reader:
        if raw_row is None:
            continue
        question = (raw_row.get("question") or "").strip()
        if not question:
            continue
        scenario: Dict[str, Any] = {}
        for col in csv_scenario_columns():
            value = (raw_row.get(col) or "").strip()
            if value:
                scenario[col] = value
        substrings_raw = (raw_row.get("expected_substrings") or "").strip()
        if substrings_raw:
            substrings = [
                frag.strip()
                for frag in substrings_raw.split(";")
                if frag and frag.strip()
            ]
            if substrings:
                scenario["expected_substrings"] = substrings
        if "id" not in scenario:
            scenario["id"] = _slugify_question_first_words(question)
        rows.append(scenario)
    return rows


SAMPLE_CSV = (
    "id,category,question,expected_answer,expected_substrings,locale\n"
    "hours_check,grounding,What are your hours on Saturday?,We are open 9am to 5pm Saturday.,9am;5pm;Saturday,en\n"
    ",grounding,Do you accept walk-ins?,,walk-in;welcome,en\n"
    ",,,,,\n"
    "empty_q,grounding,,placeholder,foo,en\n"
)


def test_csv_parser_handles_sample_with_blank_rows():
    rows = _parse_csv_rows(SAMPLE_CSV)
    # 4 input lines: 1 valid w/id, 1 valid auto-slug, 1 blank, 1 missing question
    assert len(rows) == 2
    ids = {r["id"] for r in rows}
    assert "hours_check" in ids
    # auto-slug from "Do you accept walk-ins?" — non-alnum chars scrubbed,
    # words joined by underscores, capped at the first 6 words.
    assert "do_you_accept_walkins" in {r["id"] for r in rows}


def test_csv_parser_splits_expected_substrings_on_semicolon():
    rows = _parse_csv_rows(SAMPLE_CSV)
    hours = next(r for r in rows if r["id"] == "hours_check")
    assert hours["expected_substrings"] == ["9am", "5pm", "Saturday"]


def test_csv_parser_drops_empty_cells_from_row_dict():
    rows = _parse_csv_rows(SAMPLE_CSV)
    walkins = next(r for r in rows if r["question"] == "Do you accept walk-ins?")
    # expected_answer is blank in the source → must NOT be in the dict
    assert "expected_answer" not in walkins
    # locale carries through
    assert walkins["locale"] == "en"


def test_csv_parser_assigns_auto_slug_when_id_missing():
    rows = _parse_csv_rows(SAMPLE_CSV)
    auto = next(r for r in rows if r["question"] == "Do you accept walk-ins?")
    # Must have a non-empty id derived from the question
    assert auto["id"]
    assert auto["id"] != "hours_check"


def test_csv_parser_keeps_business_id_and_branch_id():
    text = (
        "question,business_id,branch_id,customer_id\n"
        "do thing,biz-1,br-2,cu-5\n"
    )
    rows = _parse_csv_rows(text)
    assert rows[0]["business_id"] == "biz-1"
    assert rows[0]["branch_id"] == "br-2"
    assert rows[0]["customer_id"] == "cu-5"


def test_csv_parser_keeps_a_plugins_context_ids(monkeypatch):
    """Columns for the ids a plugin's agents read (admin/eval_context.py)."""
    from types import SimpleNamespace

    hook = SimpleNamespace(eval_context_fields=lambda: (("ticket_id", "Ticket id", ""),))
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [hook] if name == "agent_catalog" else [])
    rows = _parse_csv_rows("question,ticket_id,other\ndo thing,T-3,x\n")
    assert rows[0]["ticket_id"] == "T-3"
    assert "other" not in rows[0]
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [])
    assert "ticket_id" not in _parse_csv_rows("question,ticket_id\ndo thing,T-3\n")[0]


# ---------------------------------------------------------------------------
# Handler test — multipart upload over TestClient
# ---------------------------------------------------------------------------


class _FakeSuiteClient:
    """Minimal supabase fake for the import handler."""

    def __init__(self, suite_row):
        self._suite_row = suite_row
        self.update_payload = None

    def table(self, name):
        assert name == "agent_eval_suites"
        return self

    def select(self, cols):
        return self

    def eq(self, col, val):
        return self

    def update(self, payload):
        self.update_payload = payload
        return self

    def execute(self):
        result = MagicMock()
        result.data = [self._suite_row]
        return result


def test_csv_import_handler_appends_and_redirects():
    """End-to-end POST: existing suite + uploaded CSV → scenarios appended,
    redirect to /admin/evals/suites/{id}?imported=N."""
    from fastapi.testclient import TestClient
    from admin import auth, eval_handlers
    from main import app

    suite_row = {
        "suite_id": "suite-1",
        "name": "Existing",
        "scenarios": [{"id": "old_scenario", "question": "pre-existing"}],
    }
    fake = _FakeSuiteClient(suite_row)

    def _fake_superuser():
        return {"user_token": "x", "user_id": "u-1", "email": "a@b.c"}

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(eval_handlers, "get_service_client", return_value=fake),
        patch.object(eval_handlers, "record_change"),
    ]
    for p in patches:
        p.start()
    try:
        client = TestClient(app)
        files = {"csv_file": ("scenarios.csv", SAMPLE_CSV, "text/csv")}
        r = client.post(
            "/admin/evals/suites/suite-1/import",
            files=files,
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:400]
        assert "/admin/evals/suites/suite-1?imported=2" in r.headers["location"]

        # The supabase update was given old + 2 new scenarios.
        assert fake.update_payload is not None
        scenarios = fake.update_payload["scenarios"]
        assert len(scenarios) == 3  # 1 existing + 2 new
        assert scenarios[0]["id"] == "old_scenario"
        assert scenarios[1]["id"] == "hours_check"
    finally:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)


def test_csv_import_404_for_missing_suite():
    from fastapi.testclient import TestClient
    from admin import auth, eval_handlers
    from main import app

    class _NoSuite:
        def table(self, n):
            return self
        def select(self, c):
            return self
        def eq(self, c, v):
            return self
        def execute(self):
            r = MagicMock(); r.data = []
            return r

    def _fake_superuser():
        return {"user_token": "x", "user_id": "u-1", "email": "a@b.c"}

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(eval_handlers, "get_service_client", return_value=_NoSuite()),
        patch.object(eval_handlers, "record_change"),
    ]
    for p in patches:
        p.start()
    try:
        client = TestClient(app)
        files = {"csv_file": ("x.csv", SAMPLE_CSV, "text/csv")}
        r = client.post(
            "/admin/evals/suites/missing/import",
            files=files,
            follow_redirects=False,
        )
        assert r.status_code == 404
    finally:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)
