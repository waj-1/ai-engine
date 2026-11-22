"""core/admin_account.py: the one function that makes an admin account, used
by the headless init step and by the first-run page."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from core.admin_account import AdminAccountError, RestUserRows, ensure_admin_account
from tests.first_run_fake import FakeStack

ROOT = Path(__file__).resolve().parents[2]
EMAIL, PASSWORD = "owner@corp.test", "a long enough passphrase"


def test_creates_a_confirmed_auth_user_and_its_superuser_row():
    stack = FakeStack()
    out = ensure_admin_account(stack, RestUserRows(stack), EMAIL, PASSWORD)
    assert stack.create_calls == [{"email": EMAIL, "password": PASSWORD, "email_confirm": True}]
    (row,) = stack.tables["user"]
    assert row["auth_id"] == out["auth_id"] == stack.auth_users[0].id
    assert row["permission"] == {"is_superuser": True} and row["role"] == "admin"
    assert out["user_id"] == row["id"] and out["created"] is True


def test_an_existing_auth_user_is_reused_and_its_password_reset():
    stack = FakeStack()
    first = ensure_admin_account(stack, RestUserRows(stack), EMAIL, "the first passphrase")
    stack.tables["user"][0]["permission"] = {}  # demoted by hand
    again = ensure_admin_account(stack, RestUserRows(stack), EMAIL.upper(), PASSWORD)
    assert again["auth_id"] == first["auth_id"] and again["created"] is False
    assert stack.password_resets == [first["auth_id"]] and stack.auth_users[0].password == PASSWORD
    assert len(stack.tables["user"]) == 1
    assert stack.tables["user"][0]["permission"] == {"is_superuser": True}


def test_a_refusal_by_the_auth_service_is_reported_without_the_password():
    stack = FakeStack()
    stack.create_error = "Unable to validate email address: invalid format"
    with pytest.raises(AdminAccountError) as exc:
        ensure_admin_account(stack, RestUserRows(stack), "nope", PASSWORD)
    assert "invalid format" in str(exc.value) and PASSWORD not in str(exc.value)
    assert stack.tables["user"] == []


def test_the_init_step_uses_the_same_function(monkeypatch):
    """deploy/_init_db_common.ensure_admin_user is a caller of the shared
    function, not a copy: it hands over its own way of writing the row."""
    spec = importlib.util.spec_from_file_location("_init_db_common_admin", ROOT / "deploy/_init_db_common.py")
    common = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = common
    try:
        spec.loader.exec_module(common)
    finally:
        sys.modules.pop(spec.name, None)
    stack = FakeStack()
    monkeypatch.setattr("core.auth.get_main_client", lambda: stack)
    seen = {}

    def fake_shared(auth_client, rows, email, password, *, log=None):
        seen.update(client=auth_client, rows=rows, email=email, password=password)
        return {"auth_id": "a", "user_id": "u", "created": True}

    monkeypatch.setattr("core.admin_account.ensure_admin_account", fake_shared)
    monkeypatch.setenv("ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("AI_DATABASE_URL", "postgresql://postgres:x@127.0.0.1:9/postgres")
    init = common.DBInitializer(common.InitConfig(
        client_name="t", seed_script="", ingest_script="", admin_email_default="",
        admin_password_default="", postgres_password_default=""))
    logged = []
    monkeypatch.setattr(init, "log", logged.append, raising=False)
    conn = object()
    assert init.ensure_admin_user(conn) is True
    assert seen["client"] is stack and (seen["email"], seen["password"]) == (EMAIL, PASSWORD)
    assert isinstance(seen["rows"], common.SqlUserRows) and seen["rows"]._conn is conn
    assert PASSWORD not in " ".join(logged)

    def refuse(*_a, **_k):
        raise AdminAccountError("the auth service is down")

    monkeypatch.setattr("core.admin_account.ensure_admin_account", refuse)
    assert init.ensure_admin_user(conn) is False
    assert any("skipping superuser row" in line for line in logged)


def test_the_sql_row_writer_writes_what_the_shared_function_asks_for():
    spec = importlib.util.spec_from_file_location("_init_db_common_rows", ROOT / "deploy/_init_db_common.py")
    common = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = common
    try:
        spec.loader.exec_module(common)
    finally:
        sys.modules.pop(spec.name, None)
    statements = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, sql, params=()):
            statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return None if statements[-1][0].startswith("SELECT") else ("user-id-1",)

    class _Conn:
        def cursor(self):
            return _Cursor()

    stack = FakeStack()
    out = ensure_admin_account(stack, common.SqlUserRows(_Conn()), EMAIL, PASSWORD)
    assert out["user_id"] == "user-id-1"
    insert = next(s for s in statements if s[0].startswith("INSERT"))
    assert insert[1] == (out["auth_id"], EMAIL, '{"is_superuser": true}', "admin")
