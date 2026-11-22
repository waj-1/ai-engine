"""Integration tests for the new HTTP-tool admin routes.

Covers:
  GET  /admin/tools/new
  POST /admin/tools/new
  POST /admin/tools/{id}/edit
  POST /admin/tools/{id}/delete

The supabase service client is faked per-test so we can control what
queries return, and observe what was written. `record_change` and
`AgentProfileLoader.invalidate` are stubbed to no-ops.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# A scriptable supabase fake — enough for the handlers' chain calls.
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], inserted_id: str = "new-tool-id"):
        self._rows = rows
        self._inserted_id = inserted_id
        self.last_select: Optional[str] = None
        self.last_filter: Dict[str, Any] = {}
        self.last_update: Optional[Dict[str, Any]] = None
        self.last_insert: Optional[Dict[str, Any]] = None

    # chain --------------------------------------------------------------
    def select(self, cols):
        self.last_select = cols
        return self

    def eq(self, col, val):
        self.last_filter[col] = val
        return self

    def in_(self, col, vals):
        self.last_filter[f"in_{col}"] = list(vals)
        return self

    def limit(self, n):
        return self

    def order(self, *a, **kw):
        return self

    def insert(self, payload):
        self.last_insert = payload
        return _InsertResult(payload, self._inserted_id)

    def update(self, payload):
        self.last_update = payload
        return self

    def delete(self):
        return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _InsertResult:
    def __init__(self, payload, inserted_id):
        self._payload = payload
        self._inserted_id = inserted_id

    def execute(self):
        result = MagicMock()
        # Mirror PostgREST: the inserted row, with a server-assigned tool_id
        result.data = [{**self._payload, "tool_id": self._inserted_id}]
        return result


class _FakeClient:
    def __init__(self, rows_by_table=None, inserted_id="new-tool-id"):
        self._rows_by_table = rows_by_table or {}
        self._inserted_id = inserted_id
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name):
        if name not in self.queries:
            self.queries[name] = _FakeQuery(
                self._rows_by_table.get(name, []), self._inserted_id
            )
        return self.queries[name]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def http_tool_row():
    return {
        "tool_id": "tool-1",
        "python_name": "remote_search",
        "display_name": "Remote search",
        "description": "external search tool",
        "category": "http",
        "scope": "read",
        "agent_kind": "business",
        "additional_agent_kinds": [],
        "kind": "http",
        "endpoint_url": "http://api.example.com/search",
        "http_method": "POST",
        "auth_env_var": None,
        "timeout_seconds": 30,
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        "source_file": "__portal__",
        "is_deprecated": False,
    }


@pytest.fixture
def python_tool_row():
    return {
        "tool_id": "tool-py",
        "python_name": "list_bookings",
        "display_name": "List bookings",
        "description": "list",
        "category": "business",
        "scope": "read",
        "agent_kind": "business",
        "additional_agent_kinds": [],
        "kind": "python",
        "source_file": "runtime/tools/business.py",
        "argument_schema": {},
        "is_deprecated": False,
    }


def _make_app_with_fake_db(fake_client):
    """Boot the FastAPI app with require_superuser bypassed and the
    supabase service client patched in `admin.tool_handlers`. Returns a
    TestClient that the test owns (so each test has its own fake DB)."""
    from fastapi.testclient import TestClient
    from admin import auth, tool_handlers
    from main import app

    def _fake_superuser():
        return {
            "user_token": "fake-jwt",
            "user_id": "u-1",
            "email": "test@waj.local",
        }

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(tool_handlers, "get_service_client", return_value=fake_client),
        patch.object(tool_handlers, "record_change"),
        patch("core.agent_profile.AgentProfileLoader.invalidate"),
    ]
    for p in patches:
        p.start()

    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


# ---------------------------------------------------------------------------
# GET /admin/tools/new — retired, redirects to the MCP & Tools screen
# ---------------------------------------------------------------------------


def test_get_tools_new_redirects_to_screen():
    fake = _FakeClient()
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.get("/admin/tools/new", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/admin/tools"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# POST /admin/tools/new — retired, redirects without inserting
# ---------------------------------------------------------------------------


def _valid_new_form():
    """A minimum-valid form body for POST /admin/tools/new."""
    return {
        "python_name": "good_tool",
        "display_name": "Good Tool",
        "description": "tool that does a thing",
        "endpoint_url": "https://api.example.com/x",
        "http_method": "POST",
        "scope": "read",
        "agent_kind": "business",
        "category": "http",
        "timeout_seconds": "30",
        "argument_schema": '{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}',
    }


def test_post_tools_new_redirects_without_insert():
    fake = _FakeClient(inserted_id="abc-123")
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post("/admin/tools/new", data=_valid_new_form(), follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/tools"
        # Creation is retired — nothing was written.
        q = fake.queries.get("agent_tools")
        assert q is None or q.last_insert is None
    finally:
        teardown()


# ---------------------------------------------------------------------------
# POST /admin/tools/{id}/edit — python rows are rejected
# ---------------------------------------------------------------------------


def test_post_tools_edit_rejects_python_tool(python_tool_row):
    fake = _FakeClient(rows_by_table={"agent_tools": [python_tool_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            f"/admin/tools/{python_tool_row['tool_id']}/edit",
            data={
                "endpoint_url": "https://x/y",
                "http_method": "POST",
                "timeout_seconds": "30",
                "argument_schema": '{"type":"object","properties":{}}',
            },
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert "HTTP tools" in r.text or "http tools" in r.text.lower()
        assert fake.queries["agent_tools"].last_update is None
    finally:
        teardown()


def test_post_tools_edit_updates_http_tool(http_tool_row):
    fake = _FakeClient(rows_by_table={"agent_tools": [http_tool_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            f"/admin/tools/{http_tool_row['tool_id']}/edit",
            data={
                "endpoint_url": "https://updated.example.com/v2",
                "http_method": "POST",
                "timeout_seconds": "45",
                "argument_schema": '{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}',
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:400]
        update = fake.queries["agent_tools"].last_update
        assert update is not None
        assert update["endpoint_url"] == "https://updated.example.com/v2"
        assert update["timeout_seconds"] == 45
    finally:
        teardown()


# ---------------------------------------------------------------------------
# POST /admin/tools/{id}/delete — python rows are rejected; http flips flag
# ---------------------------------------------------------------------------


def test_post_tools_delete_rejects_python_tool(python_tool_row):
    fake = _FakeClient(rows_by_table={"agent_tools": [python_tool_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            f"/admin/tools/{python_tool_row['tool_id']}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert fake.queries["agent_tools"].last_update is None
    finally:
        teardown()


def test_post_tools_delete_marks_http_deprecated(http_tool_row):
    fake = _FakeClient(rows_by_table={"agent_tools": [http_tool_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            f"/admin/tools/{http_tool_row['tool_id']}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:400]
        update = fake.queries["agent_tools"].last_update
        assert update == {"is_deprecated": True}
    finally:
        teardown()


# ---------------------------------------------------------------------------
# parse_http_tool_form — shared validation used by /tools/new and the in-MCP
# add-tool flow
# ---------------------------------------------------------------------------


def test_parse_http_tool_form_valid_payload():
    from admin.tool_handlers import parse_http_tool_form

    payload = parse_http_tool_form(_valid_new_form(), agent_kinds=("business",))
    assert payload["kind"] == "http"
    assert payload["source_file"] == "__portal__"
    assert payload["is_deprecated"] is False
    assert payload["python_name"] == "good_tool"
    assert payload["argument_schema"]["properties"]["q"]["type"] == "string"


def test_parse_http_tool_form_bad_url_raises():
    from admin.tool_handlers import parse_http_tool_form

    form = _valid_new_form()
    form["endpoint_url"] = "not-a-url"
    with pytest.raises(ValueError, match="endpoint_url"):
        parse_http_tool_form(form, agent_kinds=("business",))


def test_parse_http_tool_form_unknown_agent_kind_raises():
    from admin.tool_handlers import parse_http_tool_form

    form = _valid_new_form()
    form["agent_kind"] = "nope"
    with pytest.raises(ValueError, match="agent_kind"):
        parse_http_tool_form(form, agent_kinds=("business",))
