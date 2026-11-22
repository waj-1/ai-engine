"""Regression tests for scripts/create_dev_superuser.py guards.

The script must never:
  * create a user with a hardcoded/default password (a password must be
    explicitly supplied via --password or DEV_SUPERUSER_PASSWORD), or
  * run against a non-development ENVIRONMENT (production/staging).

scripts/ is not an importable package, so the script is loaded by file path.
The client seam is core.auth.get_main_client (auth + public.user live in the
MAIN project), imported inside main(); we patch it there so no real Supabase
call is made.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "create_dev_superuser.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("create_dev_superuser", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_module()


@pytest.fixture()
def clean_env(monkeypatch):
    """Strip env vars the script reads so each test controls them explicitly."""
    for var in (
        "DEV_SUPERUSER_PASSWORD",
        "DEV_SUPERUSER_EMAIL",
        "ENVIRONMENT",
        "SUPABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _fake_client():
    client = mock.MagicMock()
    created = mock.MagicMock()
    created.user.id = "auth-123"
    client.auth.admin.create_user = mock.MagicMock(return_value=created)
    # public.user lookup -> no existing row -> insert path.
    client.schema.return_value.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    return client


def _patched_service_client(fake):
    """Patch the get_main_client symbol imported inside main()."""
    return mock.patch("core.auth.get_main_client", return_value=fake)


def test_no_password_exits_nonzero_without_creating(script, clean_env):
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("SUPABASE_URL", "http://127.0.0.1:54321")

    fake = _fake_client()
    with _patched_service_client(fake), mock.patch.object(
        sys, "argv", ["create_dev_superuser.py"]
    ):
        rc = script.main()

    assert rc != 0
    fake.auth.admin.create_user.assert_not_called()


def test_production_env_exits_nonzero_without_creating(script, clean_env):
    clean_env.setenv("ENVIRONMENT", "production")
    clean_env.setenv("SUPABASE_URL", "http://127.0.0.1:54321")

    fake = _fake_client()
    with _patched_service_client(fake), mock.patch.object(
        sys, "argv", ["create_dev_superuser.py", "--password", "explicit-pw"]
    ):
        rc = script.main()

    assert rc != 0
    fake.auth.admin.create_user.assert_not_called()


def test_happy_path_calls_create_user(script, clean_env):
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("SUPABASE_URL", "http://127.0.0.1:54321")

    fake = _fake_client()
    with _patched_service_client(fake), mock.patch.object(
        sys,
        "argv",
        ["create_dev_superuser.py", "--password", "explicit-pw", "--email", "x@y.local"],
    ):
        rc = script.main()

    assert rc == 0
    fake.auth.admin.create_user.assert_called_once()
    (call_payload,), _ = fake.auth.admin.create_user.call_args
    assert call_payload["password"] == "explicit-pw"
