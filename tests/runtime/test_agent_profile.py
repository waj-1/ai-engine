"""Unit tests for ``core.agent_profile.AgentProfileLoader``.

Every symbol referenced here was verified against the source at
``ai_agent/core/agent_profile.py``:

  - class ``AgentProfileLoader`` with classmethods ``load_active``,
    ``_load_from_db``, ``_profile_from_snapshot`` and staticmethod
    ``_fetch_model``; class attribute ``_cache`` (dict) and ``_CACHE_TTL_SEC``.
  - exceptions ``MissingProfileError``, ``MissingSnapshotError``,
    ``ProfileLoadError`` (all subclass ``RuntimeError``).
  - dataclass ``Profile`` with fields including ``voice_config`` and
    ``tool_python_names``.
  - module-level seam ``core.agent_profile.get_service_client`` (imported
    via ``from core.auth import get_service_client``).
  - lazily-imported snapshot seam ``runtime.compiler.load_active_snapshot``
    (``from runtime.compiler import load_active_snapshot`` inside
    ``_load_from_db``), so it must be patched at ``runtime.compiler``.

Network/DB is never touched: ``get_service_client`` returns a fake whose
``.table(...).select(...)....execute().data`` chain is fully controlled, and
``load_active_snapshot`` is patched.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.agent_profile import (
    AgentProfileLoader,
    MissingProfileError,
    MissingSnapshotError,
    Profile,
    ProfileLoadError,
    clear_prod_bundle,
    set_prod_bundle,
)


# ---------------------------------------------------------------------------
# Fake Supabase client
#
# Mirrors the exact chain agent_profile.py uses:
#   client.table(name).select(...).eq(...).<more filters>.limit(1).execute().data
# We capture each .table(name) call and hand back a query whose terminal
# .execute().data is resolved by a per-table callable, so a single fake can
# serve agent_profiles / agent_models / agent_tools differently.
# ---------------------------------------------------------------------------
class _FakeQuery:
    def __init__(self, table_name, resolver, calls):
        self._table = table_name
        self._resolver = resolver
        self._calls = calls
        self._filters: dict = {}
        self._in: dict = {}

    # chainable, all return self
    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def in_(self, col, vals):
        self._in[col] = list(vals)
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        self._calls.append((self._table, dict(self._filters), dict(self._in)))
        data = self._resolver(self._table, self._filters, self._in)
        return _FakeResult(data)


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    """Records every .table() access through ``calls``; resolves terminal
    data via ``resolver(table_name, filters, in_filters)``.
    """

    def __init__(self, resolver, calls):
        self._resolver = resolver
        self.calls = calls

    def table(self, name):
        return _FakeQuery(name, self._resolver, self.calls)


def _make_client(resolver):
    calls: list = []
    return _FakeClient(resolver, calls), calls


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the module-level process cache AND the precompiled prod-config
    registry around every test so state from one test never leaks into the
    next (the registry defaults empty, matching every pre-cutover test)."""
    AgentProfileLoader._cache.clear()
    clear_prod_bundle()
    yield
    AgentProfileLoader._cache.clear()
    clear_prod_bundle()


# ---------------------------------------------------------------------------
# 1. No active row -> MissingProfileError
# ---------------------------------------------------------------------------
def test_no_active_row_raises_missing_profile():
    def resolver(table, filters, in_filters):
        if table == "agent_profiles":
            return []  # no active row
        return []

    client, _calls = _make_client(resolver)
    with patch("core.agent_profile.get_service_client", return_value=client):
        with pytest.raises(MissingProfileError):
            AgentProfileLoader.load_active("sales", environment="dev")


# ---------------------------------------------------------------------------
# 2. Non-dev env + snapshot loader returns None -> MissingSnapshotError
# ---------------------------------------------------------------------------
def test_non_dev_missing_snapshot_raises():
    def resolver(table, filters, in_filters):
        if table == "agent_profiles":
            return [{
                "profile_id": "p-1",
                "agent_kind": "sales",
                "slug": "sales_default",
                "environment": "prod",
            }]
        return []

    client, _calls = _make_client(resolver)
    with patch("core.agent_profile.get_service_client", return_value=client), \
         patch("runtime.compiler.load_active_snapshot", return_value=None) as snap:
        with pytest.raises(MissingSnapshotError):
            AgentProfileLoader.load_active("sales", environment="prod")
    # The snapshot seam was actually consulted with the row's profile_id.
    snap.assert_called_once_with("p-1", "prod")


# ---------------------------------------------------------------------------
# 3. DB raises -> ProfileLoadError
# ---------------------------------------------------------------------------
def test_db_error_raises_profile_load_error():
    boom = RuntimeError("connection refused")

    def get_client_raises():
        raise boom

    with patch("core.agent_profile.get_service_client", side_effect=get_client_raises):
        with pytest.raises(ProfileLoadError) as ei:
            AgentProfileLoader.load_active("sales", environment="dev")
    # Original transport error is chained, not swallowed.
    assert ei.value.__cause__ is boom


# ---------------------------------------------------------------------------
# 4a. MissingProfileError is NOT cached: the DB seam is hit on every call.
# ---------------------------------------------------------------------------
def test_missing_profile_not_cached():
    def resolver(table, filters, in_filters):
        return []  # always "no active row"

    client, calls = _make_client(resolver)
    with patch("core.agent_profile.get_service_client", return_value=client):
        with pytest.raises(MissingProfileError):
            AgentProfileLoader.load_active("sales", environment="dev")
        with pytest.raises(MissingProfileError):
            AgentProfileLoader.load_active("sales", environment="dev")

    # agent_profiles was queried twice -> nothing cached the failure.
    profile_queries = [c for c in calls if c[0] == "agent_profiles"]
    assert len(profile_queries) == 2
    # And the cache slot stays empty.
    assert AgentProfileLoader._cache == {}


# ---------------------------------------------------------------------------
# 4b. MissingSnapshotError is NOT cached either.
# ---------------------------------------------------------------------------
def test_missing_snapshot_not_cached():
    def resolver(table, filters, in_filters):
        if table == "agent_profiles":
            return [{
                "profile_id": "p-1",
                "agent_kind": "sales",
                "slug": "sales_default",
                "environment": "prod",
            }]
        return []

    client, calls = _make_client(resolver)
    with patch("core.agent_profile.get_service_client", return_value=client), \
         patch("runtime.compiler.load_active_snapshot", return_value=None) as snap:
        with pytest.raises(MissingSnapshotError):
            AgentProfileLoader.load_active("sales", environment="prod")
        with pytest.raises(MissingSnapshotError):
            AgentProfileLoader.load_active("sales", environment="prod")

    assert snap.call_count == 2
    profile_queries = [c for c in calls if c[0] == "agent_profiles"]
    assert len(profile_queries) == 2
    assert AgentProfileLoader._cache == {}


# ---------------------------------------------------------------------------
# 4c. A successful load IS cached: the second call does not re-hit the DB.
# ---------------------------------------------------------------------------
def test_successful_load_is_cached():
    def resolver(table, filters, in_filters):
        if table == "agent_profiles":
            return [{
                "profile_id": "p-1",
                "agent_kind": "sales",
                "slug": "sales_default",
                "environment": "dev",
                # no model ids / tool ids -> no further table lookups
            }]
        return []

    client, calls = _make_client(resolver)
    with patch("core.agent_profile.get_service_client", return_value=client) as gsc:
        first = AgentProfileLoader.load_active("sales", environment="dev")
        second = AgentProfileLoader.load_active("sales", environment="dev")

    assert isinstance(first, Profile)
    assert first is second  # same cached object handed back
    # DB client only fetched once; second call short-circuits on the cache.
    assert gsc.call_count == 1
    profile_queries = [c for c in calls if c[0] == "agent_profiles"]
    assert len(profile_queries) == 1
    assert first.is_fallback is False


# ---------------------------------------------------------------------------
# 5. Snapshot path carries voice_config / tool_python_names through.
# ---------------------------------------------------------------------------
def test_snapshot_carries_voice_config_and_tools():
    snapshot = {
        "agent_kind": "sales",
        "slug": "sales_snap",
        "model_main": {"name": "gemini-pro"},
        # A legacy snapshot may still carry model_light; the loader must ignore it.
        "model_light": {"name": "gemini-flash"},
        "prompt_version_ids": {"system": "pv-1"},
        "prompt_contents": {"system": "You are a sales agent."},
        "tool_python_names": ["search_services", "create_booking"],
        "data_source_ids": [1, 2],
        "mcp_server_ids": ["m-1"],
        "escalation_policy": {"threshold": 3},
        "voice_config": {"provider": "elevenlabs", "voice_id": "abc123"},
    }

    def resolver(table, filters, in_filters):
        if table == "agent_profiles":
            return [{
                "profile_id": "p-9",
                "agent_kind": "sales",
                "slug": "sales_default",
                "environment": "prod",
            }]
        return []

    client, _calls = _make_client(resolver)
    with patch("core.agent_profile.get_service_client", return_value=client), \
         patch("runtime.compiler.load_active_snapshot", return_value=snapshot):
        profile = AgentProfileLoader.load_active("sales", environment="prod")

    assert isinstance(profile, Profile)
    assert profile.is_fallback is False
    assert profile.environment == "prod"
    # snapshot fields flow straight through
    assert profile.voice_config == {"provider": "elevenlabs", "voice_id": "abc123"}
    assert profile.tool_python_names == ["search_services", "create_booking"]
    assert profile.resolved_prompts == {"system": "You are a sales agent."}
    assert profile.model_main == {"name": "gemini-pro"}
    # Light model retired: the loader ignores any model_light the snapshot carries.
    assert profile.model_light is None
    # snapshot's slug wins over the live row's slug
    assert profile.slug == "sales_snap"
    assert profile.escalation_policy == {"threshold": 3}


# ---------------------------------------------------------------------------
# 6. _fetch_model prefers an env-scoped sibling over the pinned row.
# ---------------------------------------------------------------------------
def test_fetch_model_prefers_env_scoped_sibling():
    pinned = {
        "model_id": "m-base",
        "name": "gemini-pro",
        "environment": "dev",  # NOT staging -> triggers sibling lookup
        "temperature": 0.0,
    }
    sibling = {
        "model_id": "m-staging",
        "name": "gemini-pro",
        "environment": "staging",
        "is_active": True,
        "temperature": 0.7,
    }

    def resolver(table, filters, in_filters):
        # base lookup: by model_id
        if "model_id" in filters:
            return [pinned]
        # sibling lookup: by name + environment + is_active
        if filters.get("name") == "gemini-pro" and filters.get("environment") == "staging":
            return [sibling]
        return []

    client, _calls = _make_client(resolver)
    result = AgentProfileLoader._fetch_model(client, "m-base", "staging")
    assert result is sibling
    assert result["model_id"] == "m-staging"


def test_fetch_model_falls_back_to_pinned_when_no_sibling():
    pinned = {
        "model_id": "m-base",
        "name": "gemini-pro",
        "environment": "dev",
    }

    def resolver(table, filters, in_filters):
        if "model_id" in filters:
            return [pinned]
        # no env-scoped sibling exists
        return []

    client, _calls = _make_client(resolver)
    result = AgentProfileLoader._fetch_model(client, "m-base", "staging")
    assert result is pinned


# ---------------------------------------------------------------------------
# 7. Precompiled prod-config registry — a populated bundle short-circuits the
#    DB snapshot path; an empty registry falls back to it (existing behaviour).
# ---------------------------------------------------------------------------
def _prod_profiles_resolver(row):
    def resolver(table, filters, in_filters):
        if table == "agent_profiles":
            return [row]
        return []
    return resolver


def test_registry_hit_serves_from_bundle_without_db_snapshot():
    """With `_PROD_BUNDLE` populated for the kind, `_load_from_db(kind, 'prod')`
    builds the Profile from the registry via `_profile_from_snapshot` and does
    NOT call `load_active_snapshot`.

    The bundle entry's row must share the live-queried row's profile_id —
    `compile_prod_bundle` always compiles the entry from that SAME active
    profile, and `_build_profile` now requires the ids to match before
    serving the bundle (see `test_load_by_id_ignores_bundle_entry_for_a_
    different_profile_id` in tests/core/test_profile_delegates.py for the
    mismatch/delegate case this guards against)."""
    live_row = {
        "profile_id": "p-1", "agent_kind": "sales",
        "slug": "sales_live", "environment": "prod",
    }
    # The registry carries its OWN frozen row (same profile_id, possibly
    # stale on other fields) + snapshot — true zero-DB path.
    registry_row = {
        "profile_id": "p-1", "agent_kind": "sales",
        "slug": "sales_frozen", "environment": "prod",
    }
    snapshot = {
        "agent_kind": "sales",
        "slug": "sales_snap",
        "model_main": {"name": "gemini-pro"},
        "prompt_contents": {"system": "frozen system prompt"},
        "tool_python_names": ["search_services"],
        "voice_config": {"provider": "elevenlabs"},
    }
    set_prod_bundle({"sales": {"row": registry_row, "snapshot": snapshot}})

    client, _calls = _make_client(_prod_profiles_resolver(live_row))

    def _boom(*a, **k):  # pragma: no cover - must never be reached on a hit
        raise AssertionError("load_active_snapshot must not be called on a registry hit")

    with patch("core.agent_profile.get_service_client", return_value=client), \
         patch("runtime.compiler.load_active_snapshot", side_effect=_boom):
        profile = AgentProfileLoader.load_active("sales", environment="prod")

    assert isinstance(profile, Profile)
    assert profile.is_fallback is False
    assert profile.environment == "prod"
    # snapshot fields flow through _profile_from_snapshot
    assert profile.slug == "sales_snap"
    assert profile.resolved_prompts == {"system": "frozen system prompt"}
    assert profile.tool_python_names == ["search_services"]
    assert profile.voice_config == {"provider": "elevenlabs"}
    # registry hit served (matching profile_id), zero DB-snapshot calls
    assert profile.profile_id == "p-1"


def test_registry_miss_falls_back_to_db_snapshot_path():
    """A kind absent from the registry falls through to the unchanged
    `load_active_snapshot` DB path (byte-identical to pre-cutover)."""
    row = {
        "profile_id": "p-1", "agent_kind": "sales",
        "slug": "sales_default", "environment": "prod",
    }
    snapshot = {"agent_kind": "sales", "slug": "sales_snap",
                "tool_python_names": ["create_booking"]}

    # Registry populated for a DIFFERENT kind -> "sales" is a miss.
    set_prod_bundle({"business": {"row": {}, "snapshot": {"agent_kind": "business"}}})

    client, _calls = _make_client(_prod_profiles_resolver(row))
    with patch("core.agent_profile.get_service_client", return_value=client), \
         patch("runtime.compiler.load_active_snapshot", return_value=snapshot) as snap:
        profile = AgentProfileLoader.load_active("sales", environment="prod")

    # Fell back to the DB snapshot path.
    snap.assert_called_once_with("p-1", "prod")
    assert profile.slug == "sales_snap"
    assert profile.tool_python_names == ["create_booking"]


def test_empty_registry_is_byte_identical_missing_snapshot():
    """With the registry empty, a prod load with no DB snapshot still raises
    MissingSnapshotError — no behaviour change until a bundle is pushed."""
    row = {
        "profile_id": "p-1", "agent_kind": "sales",
        "slug": "sales_default", "environment": "prod",
    }
    client, _calls = _make_client(_prod_profiles_resolver(row))
    with patch("core.agent_profile.get_service_client", return_value=client), \
         patch("runtime.compiler.load_active_snapshot", return_value=None):
        with pytest.raises(MissingSnapshotError):
            AgentProfileLoader.load_active("sales", environment="prod")
