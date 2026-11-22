"""Profile.downstream_delegates wiring — live, snapshot, load_by_id."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.agent_profile import (
    AgentProfileLoader,
    MissingProfileError,
    Profile,
    clear_prod_bundle,
    set_prod_bundle,
)

_DELEGATE_ROW = {"profile_id": "sub1", "slug": "business_reporting",
                 "display_name": "Reporting", "description": "attribution, retention, comparisons",
                 "agent_kind": "business", "delegatable": True, "is_active": True,
                 "status": "specialist"}
_DESC = {"profile_id": "sub1", "slug": "business_reporting", "display_name": "Reporting",
         "description": "attribution, retention, comparisons", "agent_kind": "business"}

# A `status='specialist'` delegate row: same agent_kind as its (imaginary)
# parent's `business_default` active profile, but a different profile_id.
_SPECIALIST_ROW = {
    "profile_id": "spec-1",
    "agent_kind": "business",
    "slug": "business_reporting_specialist",
    "environment": "staging",
    "status": "specialist",
    "is_active": True,
}


@pytest.fixture(autouse=True)
def _reset_loader_state():
    """Isolate the process-local profile cache and prod-bundle registry so
    load_by_id/invalidate tests never leak state across tests."""
    AgentProfileLoader._cache.clear()
    clear_prod_bundle()
    yield
    AgentProfileLoader._cache.clear()
    clear_prod_bundle()


class _FakeQuery:
    """Minimal chainable stub for
    ``client.table(name).select(...).eq(...).limit(...).execute().data``.

    A single ``resolver(table_name, filters)`` callable decides the
    terminal data for any table queried, so one fake client can serve
    ``agent_profiles`` while every other table (mcp/skill/tool/model
    lookups the live-row build path touches) resolves to ``[]``.
    """

    def __init__(self, table_name, resolver, calls):
        self._table = table_name
        self._resolver = resolver
        self._calls = calls
        self._filters: dict = {}

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def in_(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        self._calls.append(self._table)

        class _Result:
            pass

        result = _Result()
        result.data = self._resolver(self._table, dict(self._filters))
        return result


class _FakeClient:
    def __init__(self, resolver):
        self._resolver = resolver
        self.calls: list = []

    def table(self, name):
        return _FakeQuery(name, self._resolver, self.calls)


def _agent_profiles_by_id_resolver(rows_by_id: dict):
    """Resolver serving ``agent_profiles`` filtered by ``profile_id``;
    every other table (mcp attachments, skills, tools, models, downstream
    delegates) resolves to an empty result, matching a row with no
    model/tool/mcp/delegate ids attached."""

    def resolver(table, filters):
        if table == "agent_profiles":
            row = rows_by_id.get(filters.get("profile_id"))
            return [row] if row else []
        return []

    return resolver


# ---------------------------------------------------------------------------
# load_by_id — happy path + cache
# ---------------------------------------------------------------------------
def test_load_by_id_happy_path_and_cache():
    client = _FakeClient(_agent_profiles_by_id_resolver({"spec-1": _SPECIALIST_ROW}))
    with patch("core.agent_profile.get_service_client", return_value=client) as gsc:
        first = AgentProfileLoader.load_by_id("spec-1", "staging")
        second = AgentProfileLoader.load_by_id("spec-1", "staging")

    assert isinstance(first, Profile)
    assert first.slug == "business_reporting_specialist"
    assert first.agent_kind == "business"
    assert first.is_fallback is False
    # Second call hit the cache: the client was only fetched once.
    assert first is second
    assert gsc.call_count == 1


# ---------------------------------------------------------------------------
# load_by_id — missing/inactive row -> MissingProfileError
# ---------------------------------------------------------------------------
def test_load_by_id_missing_row_raises():
    client = _FakeClient(_agent_profiles_by_id_resolver({}))
    with patch("core.agent_profile.get_service_client", return_value=client):
        with pytest.raises(MissingProfileError):
            AgentProfileLoader.load_by_id("does-not-exist", "staging")


# ---------------------------------------------------------------------------
# load_delegate — resolves by (agent_kind, slug, environment), NOT by id.
# This is the cross-env promotion fix: the parent snapshot freezes the
# delegate's STAGING id, but the prod delegate has a DIFFERENT id. Resolving
# by slug finds the prod row regardless of the frozen id.
# ---------------------------------------------------------------------------
def _agent_profiles_by_slug_resolver(rows: list):
    """Resolver serving ``agent_profiles`` filtered by agent_kind+slug+env."""
    def resolver(table, filters):
        if table == "agent_profiles":
            return [
                r for r in rows
                if r.get("agent_kind") == filters.get("agent_kind")
                and r.get("slug") == filters.get("slug")
                and r.get("environment") == filters.get("environment")
                and r.get("is_active", True) == filters.get("is_active", True)
            ][:1]
        return []
    return resolver


def test_load_delegate_resolves_by_slug_ignoring_frozen_id():
    # The prod delegate has a NEW id (mint-on-promote), unrelated to the
    # staging id the parent snapshot froze. load_delegate must still find it
    # by (agent_kind, slug, environment).
    prod_specialist = {"profile_id": "prod-new-id-xyz", "agent_kind": "business",
                       "slug": "business_reporting", "environment": "prod",
                       "status": "specialist", "is_active": True}
    client = _FakeClient(_agent_profiles_by_slug_resolver([prod_specialist]))
    # the prod delegate has its own prod snapshot (minted by its own promote)
    prod_snapshot = {"agent_kind": "business", "slug": "business_reporting"}
    with patch("core.agent_profile.get_service_client", return_value=client), \
         patch("runtime.compiler.load_active_snapshot", return_value=prod_snapshot):
        # even though the frozen descriptor id would be "d4db3d82" (staging),
        # we resolve by slug in the prod env and get the prod row:
        p = AgentProfileLoader.load_delegate("business", "business_reporting", "prod")
    assert isinstance(p, Profile)
    assert p.slug == "business_reporting"
    assert p.agent_kind == "business"


def test_load_delegate_missing_raises():
    client = _FakeClient(_agent_profiles_by_slug_resolver([]))
    with patch("core.agent_profile.get_service_client", return_value=client):
        with pytest.raises(MissingProfileError):
            AgentProfileLoader.load_delegate("business", "nope", "prod")


# ---------------------------------------------------------------------------
# REGRESSION (Finding 1): a prod-bundle entry for this agent_kind that holds
# a DIFFERENT profile_id (the kind's active/parent profile) must NOT be
# served for a delegate's load_by_id — the loader must fall through to the
# normal snapshot path and return the delegate's own data.
# ---------------------------------------------------------------------------
def test_load_by_id_ignores_bundle_entry_for_a_different_profile_id():
    parent_row = {"profile_id": "parent-1", "agent_kind": "business",
                  "slug": "business_default"}
    parent_snapshot = {"agent_kind": "business", "slug": "business_default_snap"}
    set_prod_bundle({"business": {"row": parent_row, "snapshot": parent_snapshot}})

    specialist_row = {**_SPECIALIST_ROW, "environment": "prod"}
    specialist_snapshot = {"agent_kind": "business",
                            "slug": "business_reporting_specialist_snap"}

    client = _FakeClient(_agent_profiles_by_id_resolver({"spec-1": specialist_row}))
    with patch("core.agent_profile.get_service_client", return_value=client), \
         patch("runtime.compiler.load_active_snapshot",
               return_value=specialist_snapshot) as snap:
        profile = AgentProfileLoader.load_by_id("spec-1", "prod")

    # Fell through to the DB-snapshot path keyed on the delegate's OWN id,
    # not served from the parent's bundle entry.
    snap.assert_called_once_with("spec-1", "prod")
    assert profile.slug == "business_reporting_specialist_snap"
    assert profile.slug != parent_snapshot["slug"]


# ---------------------------------------------------------------------------
# REGRESSION (Finding 2): AgentProfileLoader.invalidate(agent_kind, env) must
# also clear id-keyed load_by_id cache entries sharing that (agent_kind,
# environment), not just the kind-keyed load_active slot.
# ---------------------------------------------------------------------------
def test_invalidate_kind_clears_id_keyed_delegate_cache():
    client = _FakeClient(_agent_profiles_by_id_resolver({"spec-1": _SPECIALIST_ROW}))
    with patch("core.agent_profile.get_service_client", return_value=client) as gsc:
        AgentProfileLoader.load_by_id("spec-1", "staging")
        assert gsc.call_count == 1

        AgentProfileLoader.invalidate("business", "staging")

        AgentProfileLoader.load_by_id("spec-1", "staging")
        assert gsc.call_count == 2


def test_profile_dataclass_default():
    p = Profile(profile_id=None, agent_kind="business", slug="x", environment="staging")
    assert p.downstream_delegates == []


def test_profile_from_snapshot_reads_delegates():
    row = {"profile_id": "p1", "agent_kind": "business", "slug": "business_default"}
    snap = {"agent_kind": "business", "slug": "business_default",
            "downstream_delegates": [_DESC]}
    p = AgentProfileLoader._profile_from_snapshot(row, snap, "prod")
    assert p.downstream_delegates == [_DESC]


def test_profile_from_snapshot_tolerates_legacy():
    row = {"profile_id": "p1", "agent_kind": "business", "slug": "business_default"}
    p = AgentProfileLoader._profile_from_snapshot(row, {"agent_kind": "business"}, "prod")
    assert p.downstream_delegates == []


def test_fetch_delegates_filters_non_delegatable():
    from core.agent_profile import _fetch_downstream_delegates

    class _Q:
        def __init__(self, rows): self._rows = rows
        def select(self, *_a, **_k): return self
        def in_(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def execute(self):
            class R: data = [_DELEGATE_ROW,
                             {**_DELEGATE_ROW, "profile_id": "aux", "slug": "business_title",
                              "delegatable": False}]
            return R()

    class _C:
        def table(self, _): return _Q(None)

    out = _fetch_downstream_delegates(_C(), ["sub1", "aux"])
    assert [d["profile_id"] for d in out] == ["sub1"]
    assert set(out[0]) == {"profile_id", "slug", "display_name", "description", "agent_kind"}
