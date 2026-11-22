"""Route-level tests for the create/update/delete endpoints in
``admin.env_vars_handlers``.

The existing suite (tests/admin/test_env_vars_masking.py) only covers the
``_mask`` helper and the grouped ``fetch_env_vars_by_env`` read path. These
tests drive the three mutating HTTP routes end-to-end through the shared
``client`` TestClient fixture:

  POST /admin/env-vars              -> create
  POST /admin/env-vars/{var_id}     -> update
  POST /admin/env-vars/{var_id}/delete -> delete

The security-critical invariant under test: a *secret* row's raw value must
never reach the audit log. ``record_change`` is patched to capture its
kwargs, and we assert the raw secret string is absent from every audit
argument while ``SECRET_PLACEHOLDER`` is present in its place.

``admin.env_vars_handlers.client_for`` (create targets the form's own env
branch) / ``admin_service_client`` (update/delete) are patched to a fake client
that records insert/update/delete payloads. ``record_change`` is patched at
the same module-level seam (it is imported into env_vars_handlers, so we
patch it there, not in admin.audit).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import env_vars_handlers as evh

RAW_SECRET = "super-secret-value-do-not-leak"


# ---------------------------------------------------------------------------
# A scriptable supabase fake — enough for the handlers' chain calls.
# ---------------------------------------------------------------------------


class _FakeQuery:
    """One per (client, table) — records the last insert/update/delete and
    the filters applied, and returns a scripted row set from ``execute``.

    ``insert`` mirrors PostgREST by echoing the payload back (with a
    server-assigned ``var_id``) on ``.data`` so the create handler's
    ``inserted.data[0]`` path works.
    """

    def __init__(self, rows: List[Dict[str, Any]], inserted_var_id: str):
        self._rows = rows
        self._inserted_var_id = inserted_var_id
        self.last_select: Optional[str] = None
        self.last_filter: Dict[str, Any] = {}
        self.last_insert: Optional[Dict[str, Any]] = None
        self.last_update: Optional[Dict[str, Any]] = None
        self.delete_called: bool = False
        self._pending_insert: Optional[Dict[str, Any]] = None

    # chain ---------------------------------------------------------------
    def select(self, cols: str):
        self.last_select = cols
        return self

    def eq(self, col, val):
        self.last_filter[col] = val
        return self

    def order(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    def insert(self, payload):
        self.last_insert = payload
        self._pending_insert = payload
        return self

    def update(self, payload):
        self.last_update = payload
        return self

    def delete(self):
        self.delete_called = True
        return self

    def execute(self):
        result = MagicMock()
        if self._pending_insert is not None:
            # PostgREST returns the inserted row with its new id.
            result.data = [{**self._pending_insert, "var_id": self._inserted_var_id}]
            self._pending_insert = None
        else:
            result.data = list(self._rows)
        return result


class _FakeClient:
    def __init__(self, rows_by_table=None, inserted_var_id="new-var-id"):
        self._rows_by_table = rows_by_table or {}
        self._inserted_var_id = inserted_var_id
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name):
        if name not in self.queries:
            self.queries[name] = _FakeQuery(
                self._rows_by_table.get(name, []), self._inserted_var_id
            )
        return self.queries[name]


def _secret_row(var_id: str = "var-1") -> Dict[str, Any]:
    return {
        "var_id": var_id,
        "environment": "prod",
        "name": "API_KEY",
        "value": RAW_SECRET,
        "source": "db",
        "description": "an api key",
        "is_secret": True,
    }


def _assert_secret_never_in_kwargs(captured_kwargs: Dict[str, Any]) -> None:
    """The raw secret must not appear anywhere in the record_change kwargs."""
    for key, val in captured_kwargs.items():
        assert RAW_SECRET not in repr(val), (
            f"raw secret leaked into record_change kwarg {key!r}: {val!r}"
        )


# ---------------------------------------------------------------------------
# POST /admin/env-vars — create
# ---------------------------------------------------------------------------


def test_create_secret_inserts_row_and_masks_audit_after(client):
    fake = _FakeClient(inserted_var_id="created-123")
    captured: Dict[str, Any] = {}

    def _capture_record_change(**kwargs):
        captured.update(kwargs)

    with patch.object(evh, "client_for", return_value=fake), patch.object(
        evh, "record_change", side_effect=_capture_record_change
    ):
        r = client.post(
            "/admin/env-vars",
            data={
                "environment": "prod",
                "name": "API_KEY",
                "value": RAW_SECRET,
                "source": "db",
                "description": "an api key",
                "is_secret": "on",
            },
            follow_redirects=False,
        )

    # Redirect back to the Variables tab of the consolidated env screen.
    assert r.status_code == 302, r.text[:400]
    assert r.headers["location"] == "/admin/env?tab=variables"

    # The raw secret WAS written to the DB (insert payload), as expected.
    q = fake.queries.get("agent_env_vars")
    assert q is not None
    assert q.last_insert is not None
    assert q.last_insert["value"] == RAW_SECRET
    assert q.last_insert["is_secret"] is True
    assert q.last_insert["name"] == "API_KEY"

    # But the audit 'after' payload must be MASKED.
    assert captured, "record_change was never called"
    assert captured["action"] == "create"
    assert captured["entity_type"] == "agent_env_vars"
    assert captured["entity_id"] == "created-123"
    after = captured["after"]
    assert after["value"] == evh.SECRET_PLACEHOLDER
    assert after["value"] != RAW_SECRET
    assert after.get("_masked") is True

    _assert_secret_never_in_kwargs(captured)


def test_create_blank_name_returns_400(client):
    fake = _FakeClient()
    captured: Dict[str, Any] = {}

    def _capture_record_change(**kwargs):
        captured.update(kwargs)

    with patch.object(evh, "client_for", return_value=fake), patch.object(
        evh, "record_change", side_effect=_capture_record_change
    ):
        r = client.post(
            "/admin/env-vars",
            data={
                "environment": "prod",
                "name": "   ",  # whitespace-only -> stripped -> empty -> 400
                "value": "whatever",
                "is_secret": "on",
            },
            follow_redirects=False,
        )

    assert r.status_code == 400, r.text[:400]
    # No insert, no audit row written on the validation failure path.
    assert "agent_env_vars" not in fake.queries
    assert captured == {}


# ---------------------------------------------------------------------------
# POST /admin/env-vars/{var_id} — update
# ---------------------------------------------------------------------------


def test_update_secret_masks_audit_before_and_after(client):
    fake = _FakeClient(rows_by_table={"agent_env_vars": [_secret_row("var-1")]})
    captured: Dict[str, Any] = {}

    def _capture_record_change(**kwargs):
        captured.update(kwargs)

    with patch.object(evh, "admin_service_client", return_value=fake), patch.object(
        evh, "record_change", side_effect=_capture_record_change
    ):
        r = client.post(
            "/admin/env-vars/var-1",
            data={
                "environment": "prod",
                "name": "API_KEY",
                "value": RAW_SECRET,  # a new secret value being saved
                "source": "db",
                "description": "rotated key",
                "is_secret": "on",
            },
            follow_redirects=False,
        )

    assert r.status_code == 302, r.text[:400]
    assert r.headers["location"] == "/admin/env?tab=variables"

    # The update was applied with the raw value in the DB payload.
    q = fake.queries["agent_env_vars"]
    assert q.last_update is not None
    assert q.last_update["value"] == RAW_SECRET
    assert q.last_update["is_secret"] is True
    assert q.last_filter.get("var_id") == "var-1"

    # Audit before AND after must be masked for a secret row.
    assert captured["action"] == "update"
    assert captured["entity_id"] == "var-1"
    before = captured["before"]
    after = captured["after"]
    assert before["value"] == evh.SECRET_PLACEHOLDER
    assert after["value"] == evh.SECRET_PLACEHOLDER
    assert before.get("_masked") is True
    assert after.get("_masked") is True

    _assert_secret_never_in_kwargs(captured)


def test_update_unmark_secret_masks_audit_before_and_after(client):
    """Regression: updating a secret row to is_secret=False, with no new
    value submitted, must not let the carried-over plaintext value reach
    the audit log via {**before, **payload} + a merged-flag-only mask."""
    fake = _FakeClient(rows_by_table={"agent_env_vars": [_secret_row("var-1")]})
    captured: Dict[str, Any] = {}

    def _capture_record_change(**kwargs):
        captured.update(kwargs)

    with patch.object(evh, "admin_service_client", return_value=fake), patch.object(
        evh, "record_change", side_effect=_capture_record_change
    ):
        r = client.post(
            "/admin/env-vars/var-1",
            data={
                "environment": "prod",
                "name": "API_KEY",
                # No "value" field submitted — the form only flips is_secret.
                "source": "db",
                "description": "no longer a secret",
                # is_secret omitted -> "on" absent -> False.
            },
            follow_redirects=False,
        )

    assert r.status_code == 302, r.text[:400]

    q = fake.queries["agent_env_vars"]
    assert q.last_update is not None
    assert q.last_update["is_secret"] is False
    assert "value" not in q.last_update  # no new value was submitted

    assert captured["action"] == "update"
    before = captured["before"]
    after = captured["after"]
    # The row was secret going in, so both audit payloads must still mask
    # the (still-plaintext, still-stored) value, even though `after`'s own
    # is_secret flag is now False.
    assert before["value"] == evh.SECRET_PLACEHOLDER
    assert after["value"] == evh.SECRET_PLACEHOLDER
    assert after["is_secret"] is False

    _assert_secret_never_in_kwargs(captured)


# ---------------------------------------------------------------------------
# POST /admin/env-vars/{var_id}/delete — delete
# ---------------------------------------------------------------------------


def test_delete_removes_by_var_id_and_masks_audit_before(client):
    fake = _FakeClient(rows_by_table={"agent_env_vars": [_secret_row("var-9")]})
    captured: Dict[str, Any] = {}

    def _capture_record_change(**kwargs):
        captured.update(kwargs)

    with patch.object(evh, "admin_service_client", return_value=fake), patch.object(
        evh, "record_change", side_effect=_capture_record_change
    ):
        r = client.post(
            "/admin/env-vars/var-9/delete",
            follow_redirects=False,
        )

    assert r.status_code == 302, r.text[:400]
    assert r.headers["location"] == "/admin/env?tab=variables"

    # Deleted, filtered by the var_id from the path.
    q = fake.queries["agent_env_vars"]
    assert q.delete_called is True
    assert q.last_filter.get("var_id") == "var-9"

    # Audit 'before' is masked; the raw secret never reaches the log.
    assert captured["action"] == "delete"
    assert captured["entity_id"] == "var-9"
    before = captured["before"]
    assert before["value"] == evh.SECRET_PLACEHOLDER
    assert before.get("_masked") is True

    _assert_secret_never_in_kwargs(captured)
