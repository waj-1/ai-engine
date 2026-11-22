"""Unit tests for core.env_resolver.

Resolution order:
  1. os.environ[name] wins outright.
  2. Otherwise the resolver queries agent_env_vars (environment, name)
     and only returns the inline value when source='db'.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


@pytest.fixture
def _clean_env():
    """Strip names this suite owns so resolution starts from a known state."""
    names = ("WAJ_RESOLVER_TEST_VAR", "ENVIRONMENT")
    saved = {n: os.environ.pop(n, None) for n in names}
    yield
    for n, v in saved.items():
        if v is None:
            os.environ.pop(n, None)
        else:
            os.environ[n] = v


def _stub_client(rows):
    """Build a chainable mock that returns ``rows`` from .execute()."""
    chain = MagicMock()
    chain.table.return_value = chain
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.limit.return_value = chain
    chain.execute.return_value = SimpleNamespace(data=rows)
    return chain


def test_os_environ_wins(_clean_env):
    os.environ["WAJ_RESOLVER_TEST_VAR"] = "from-os-env"
    from core.env_resolver import resolve_env

    with patch("core.auth.get_service_client") as mock_client:
        result = resolve_env("WAJ_RESOLVER_TEST_VAR")

    assert result == "from-os-env"
    mock_client.assert_not_called()


def test_db_fallback_returns_inline_value(_clean_env):
    os.environ["ENVIRONMENT"] = "dev"
    from core.env_resolver import resolve_env

    stub = _stub_client([{"value": "from-db", "source": "db"}])
    with patch("core.auth.get_service_client", return_value=stub):
        result = resolve_env("WAJ_RESOLVER_TEST_VAR")

    assert result == "from-db"


def test_db_lookup_uses_explicit_environment(_clean_env):
    os.environ["ENVIRONMENT"] = "dev"
    from core.env_resolver import resolve_env

    stub = _stub_client([{"value": "staging-only", "source": "db"}])
    with patch("core.auth.get_service_client", return_value=stub):
        result = resolve_env("WAJ_RESOLVER_TEST_VAR", environment="staging")

    assert result == "staging-only"
    # We chain .eq twice (environment then name). Both should have been called.
    eq_calls = stub.eq.call_args_list
    assert ("environment", "staging") in [c.args for c in eq_calls]
    assert ("name", "WAJ_RESOLVER_TEST_VAR") in [c.args for c in eq_calls]


def test_secret_manager_source_returns_none_for_now(_clean_env):
    """Secret Manager / Vault integration is deferred — the resolver
    should recognise the source prefix but not attempt to fetch yet."""
    os.environ["ENVIRONMENT"] = "prod"
    from core.env_resolver import resolve_env

    stub = _stub_client(
        [{"value": "projects/foo/secrets/bar", "source": "secret_manager:foo/bar"}]
    )
    with patch("core.auth.get_service_client", return_value=stub):
        result = resolve_env("WAJ_RESOLVER_TEST_VAR")

    assert result is None


def test_missing_row_returns_none(_clean_env):
    os.environ["ENVIRONMENT"] = "dev"
    from core.env_resolver import resolve_env

    stub = _stub_client([])
    with patch("core.auth.get_service_client", return_value=stub):
        result = resolve_env("WAJ_RESOLVER_TEST_VAR")

    assert result is None


def test_empty_os_env_falls_through_to_db(_clean_env):
    """Empty string should be treated as unset and fall through to the DB."""
    os.environ["WAJ_RESOLVER_TEST_VAR"] = ""
    os.environ["ENVIRONMENT"] = "dev"
    from core.env_resolver import resolve_env

    stub = _stub_client([{"value": "from-db", "source": "db"}])
    with patch("core.auth.get_service_client", return_value=stub):
        result = resolve_env("WAJ_RESOLVER_TEST_VAR")

    assert result == "from-db"
