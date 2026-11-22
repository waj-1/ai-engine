"""Unit tests for core.prompt_resolver — the request-scoped prompt
override path plus the SafeDict format tolerance.

Confirmed against core/prompt_resolver.py:
  - set_prompt_override / clear_prompt_override / get_prompt_override
    drive the ``_PROMPT_OVERRIDE`` ContextVar.
  - PromptResolver.resolve(prompt_version_id, fallback=None,
    variables=None, *, agent_kind=None, prompt_key=None).
  - The override maps (agent_kind, prompt_key) -> agent_prompts.prompt_id
    (filtered by agent_kind + slug) -> agent_prompt_versions row
    (filtered by prompt_id + version_number) -> prompt_version_id, then
    _fetch_or_fallback reads agent_prompt_versions.content for that id.
  - The seam is the module-level ``get_service_client`` imported from
    core.auth, so we patch ``core.prompt_resolver.get_service_client``.
  - PromptResolver caches by prompt_version_id at class scope, so each
    test invalidates the cache to stay isolated.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from core import prompt_resolver as pr
from core.prompt_resolver import (
    PromptResolver,
    clear_prompt_override,
    get_prompt_override,
    set_prompt_override,
)


@pytest.fixture(autouse=True)
def _reset_resolver_state():
    """Each test starts with a clear override + empty version cache, and
    leaves both clean for the next test (the ContextVar and the class-level
    cache are both shared process state)."""
    clear_prompt_override()
    PromptResolver.invalidate()
    yield
    clear_prompt_override()
    PromptResolver.invalidate()


def _table_router(handlers):
    """Build a chainable Supabase-style mock whose ``.execute().data`` is
    decided by a per-call handler.

    ``handlers`` maps a table name to a callable taking the dict of
    accumulated ``.eq(col, val)`` filters and returning the rows list. Each
    ``.table(name)`` call starts a fresh filter dict, so the same client can
    serve the agent_prompts lookup and the two agent_prompt_versions
    lookups in one resolve() call.
    """
    client = MagicMock()
    state = {"table": None, "filters": {}}

    def _table(name):
        state["table"] = name
        state["filters"] = {}
        return client

    def _eq(col, val):
        state["filters"][col] = val
        return client

    def _execute():
        handler = handlers[state["table"]]
        rows = handler(dict(state["filters"]))
        return SimpleNamespace(data=rows)

    client.table.side_effect = _table
    client.select.return_value = client
    client.eq.side_effect = _eq
    client.limit.return_value = client
    client.execute.side_effect = _execute
    return client


def test_set_override_resolves_to_pinned_version_content():
    """set_prompt_override -> resolve(agent_kind, prompt_key) returns the
    content of the pinned version, NOT the active prompt_version_id."""

    def _prompts(filters):
        assert filters == {"agent_kind": "support_bot", "slug": "system"}
        return [{"prompt_id": "prompt-clin-sys"}]

    def _versions(filters):
        # First call resolves version_number -> prompt_version_id.
        if "version_number" in filters:
            assert filters == {
                "prompt_id": "prompt-clin-sys",
                "version_number": 7,
            }
            return [{"prompt_version_id": "ver-7"}]
        # Second call fetches the content for that pinned id.
        assert filters == {"prompt_version_id": "ver-7"}
        return [{"content": "PINNED v7 content"}]

    client = _table_router(
        {"agent_prompts": _prompts, "agent_prompt_versions": _versions}
    )

    set_prompt_override({"support_bot/system": 7})
    with patch.object(pr, "get_service_client", return_value=client):
        out = PromptResolver.resolve(
            "active-version-id",
            fallback="FS fallback",
            agent_kind="support_bot",
            prompt_key="system",
        )

    assert out == "PINNED v7 content"


def test_non_integer_override_value_is_ignored_falls_back_to_active():
    """A non-int override value is ignored; the resolver uses the supplied
    active prompt_version_id instead (its content is fetched and returned)."""

    def _versions(filters):
        # Only the active-id content fetch should ever run — the override
        # was rejected before touching agent_prompts.
        assert filters == {"prompt_version_id": "active-version-id"}
        return [{"content": "ACTIVE content"}]

    def _prompts(filters):  # pragma: no cover - must not be reached
        raise AssertionError("agent_prompts must not be queried for a bad override")

    client = _table_router(
        {"agent_prompts": _prompts, "agent_prompt_versions": _versions}
    )

    set_prompt_override({"support_bot/system": "not-an-int"})
    with patch.object(pr, "get_service_client", return_value=client):
        out = PromptResolver.resolve(
            "active-version-id",
            fallback="FS fallback",
            agent_kind="support_bot",
            prompt_key="system",
        )

    assert out == "ACTIVE content"


def test_missing_prompt_or_version_row_falls_back_to_active():
    """When the override target prompt/version row is missing, the resolver
    falls back to the active prompt_version_id content."""

    def _prompts(filters):
        # No agent_prompts row for this (agent_kind, slug).
        return []

    def _versions(filters):
        assert filters == {"prompt_version_id": "active-version-id"}
        return [{"content": "ACTIVE content"}]

    client = _table_router(
        {"agent_prompts": _prompts, "agent_prompt_versions": _versions}
    )

    set_prompt_override({"support_bot/system": 7})
    with patch.object(pr, "get_service_client", return_value=client):
        out = PromptResolver.resolve(
            "active-version-id",
            fallback="FS fallback",
            agent_kind="support_bot",
            prompt_key="system",
        )

    assert out == "ACTIVE content"


def test_db_error_during_override_lookup_falls_back_to_active_no_raise():
    """A DB exception while resolving the override id is swallowed; the
    resolver falls back to the active prompt_version_id content and does not
    propagate the error."""

    # First client raises during the override lookup (agent_prompts query);
    # the second handles the active-id content fetch. We swap clients on
    # successive get_service_client() calls so the override lookup fails but
    # the fallback content fetch succeeds.
    boom = MagicMock()
    boom.table.return_value = boom
    boom.select.return_value = boom
    boom.eq.return_value = boom
    boom.limit.return_value = boom
    boom.execute.side_effect = RuntimeError("connection reset")

    def _versions(filters):
        assert filters == {"prompt_version_id": "active-version-id"}
        return [{"content": "ACTIVE content"}]

    good = _table_router(
        {"agent_prompts": lambda f: [], "agent_prompt_versions": _versions}
    )

    set_prompt_override({"support_bot/system": 7})
    with patch.object(pr, "get_service_client", side_effect=[boom, good]):
        out = PromptResolver.resolve(
            "active-version-id",
            fallback="FS fallback",
            agent_kind="support_bot",
            prompt_key="system",
        )

    assert out == "ACTIVE content"


def test_missing_placeholder_left_intact_via_safedict():
    """A variable referenced in the content but absent from ``variables`` is
    left as a literal ``{placeholder}`` rather than raising (SafeDict)."""

    def _versions(filters):
        assert filters == {"prompt_version_id": "ver-fmt"}
        return [{"content": "Hello {name}, your locale is {locale}"}]

    client = _table_router({"agent_prompt_versions": _versions})

    with patch.object(pr, "get_service_client", return_value=client):
        out = PromptResolver.resolve(
            "ver-fmt",
            fallback="FS fallback",
            variables={"name": "Sam"},
        )

    # {name} substituted, {locale} left intact.
    assert out == "Hello Sam, your locale is {locale}"


def test_override_contextvar_round_trip_helpers():
    """Sanity check on the override helpers themselves: set then read then
    clear returns to None."""
    assert get_prompt_override() is None
    set_prompt_override({"support_bot/system": 3})
    assert get_prompt_override() == {"support_bot/system": 3}
    clear_prompt_override()
    assert get_prompt_override() is None
