"""Unit tests for ``runtime.prod_bundle`` (precompiled prod config bundle).

Every symbol referenced here was verified against the source at
``ai_agent/runtime/prod_bundle.py``:

  - ``SCHEMA_VERSION`` (int), ``BUNDLE_LATEST_PATH`` / ``BUNDLE_HISTORY_PREFIX``
    (GCS object paths).
  - ``compile_prod_bundle(client, *, generated_at=None)`` — queries
    ``agent_profiles`` (environment=prod, status=active) on the passed client
    and loads each snapshot via the lazily-imported
    ``runtime.compiler.load_active_snapshot`` seam.
  - ``push_bundle(bundle, *, version=None)`` / ``load_bundle()`` — round-trip
    through the ``shared.cloud_storage.upload_json`` / ``download_json`` seams
    (imported lazily inside the functions, so patched at
    ``shared.cloud_storage``).

No network / GCS is ever touched: the Supabase client is a fake whose
``.table(...).select(...).eq(...).execute().data`` chain is fully controlled,
``load_active_snapshot`` is patched, and the GCS helpers are replaced by an
in-memory fake store.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from runtime import prod_bundle
from runtime.prod_bundle import (
    SCHEMA_VERSION,
    BUNDLE_LATEST_PATH,
    compile_prod_bundle,
    load_bundle,
    push_bundle,
)


# ---------------------------------------------------------------------------
# Fake Supabase client — mirrors the exact chain prod_bundle uses:
#   client.table(name).select(...).eq(col, val).eq(col, val).execute().data
# ---------------------------------------------------------------------------
class _FakeQuery:
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

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        self._calls.append((self._table, dict(self._filters)))
        return _FakeResult(self._resolver(self._table, self._filters))


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    def __init__(self, resolver):
        self._resolver = resolver
        self.calls: list = []

    def table(self, name):
        return _FakeQuery(name, self._resolver, self.calls)


# ---------------------------------------------------------------------------
# compile_prod_bundle
# ---------------------------------------------------------------------------
def test_compile_prod_bundle_includes_only_kinds_with_snapshot():
    """Two active prod profiles — one with a snapshot, one without. The bundle
    contains only the kind WITH a snapshot, shaped {schema_version, profiles:
    {kind: {row, snapshot}}}."""
    sales_row = {
        "profile_id": "p-sales", "agent_kind": "sales",
        "slug": "sales_default", "environment": "prod", "status": "active",
    }
    biz_row = {
        "profile_id": "p-biz", "agent_kind": "business",
        "slug": "business_default", "environment": "prod", "status": "active",
    }

    def resolver(table, filters):
        if table == "agent_profiles":
            # The fake ignores filters; the source-of-truth for "prod + active"
            # is what compile_prod_bundle asks for — assert those filters below.
            return [sales_row, biz_row]
        return []

    client = _FakeClient(resolver)
    sales_snapshot = {"agent_kind": "sales", "tool_python_names": ["a"]}

    def fake_snapshot(profile_id, environment):
        assert environment == "prod"
        return sales_snapshot if profile_id == "p-sales" else None

    with patch("runtime.compiler.load_active_snapshot", side_effect=fake_snapshot):
        bundle = compile_prod_bundle(client)

    assert bundle["schema_version"] == SCHEMA_VERSION
    # Only the kind WITH a snapshot survives.
    assert set(bundle["profiles"].keys()) == {"sales"}
    entry = bundle["profiles"]["sales"]
    assert entry["row"] == sales_row
    assert entry["snapshot"] == sales_snapshot
    # No wall-clock stamp unless the caller passes one.
    assert "generated_at" not in bundle

    # It queried agent_profiles scoped to prod + active.
    prof_calls = [c for c in client.calls if c[0] == "agent_profiles"]
    assert prof_calls, "agent_profiles was never queried"
    filters = prof_calls[0][1]
    assert filters.get("environment") == "prod"
    assert filters.get("status") == "active"


def test_compile_prod_bundle_passes_generated_at_through():
    def resolver(table, filters):
        return []

    with patch("runtime.compiler.load_active_snapshot", return_value=None):
        bundle = compile_prod_bundle(_FakeClient(resolver), generated_at="2026-07-07T00:00:00Z")
    assert bundle == {
        "schema_version": SCHEMA_VERSION,
        "profiles": {},
        "generated_at": "2026-07-07T00:00:00Z",
    }


def test_compile_prod_bundle_skips_rows_missing_ids():
    def resolver(table, filters):
        if table == "agent_profiles":
            return [{"agent_kind": "sales", "environment": "prod"}]  # no profile_id
        return []

    with patch("runtime.compiler.load_active_snapshot", return_value={"x": 1}) as snap:
        bundle = compile_prod_bundle(_FakeClient(resolver))
    assert bundle["profiles"] == {}
    snap.assert_not_called()  # never even attempted a snapshot load


# ---------------------------------------------------------------------------
# push_bundle / load_bundle — round-trip through a fake GCS store
# ---------------------------------------------------------------------------
class _FakeGCS:
    """In-memory stand-in for the shared.cloud_storage JSON helpers."""

    def __init__(self):
        self.store: dict = {}

    def upload_json(self, path, obj):
        self.store[path] = obj
        return f"gs://csv-exports/{path}"

    def download_json(self, path):
        return self.store.get(path)


def test_push_and_load_bundle_round_trip():
    fake = _FakeGCS()
    bundle = {"schema_version": SCHEMA_VERSION, "profiles": {"sales": {"row": {}, "snapshot": {}}}}
    with patch("shared.cloud_storage.upload_json", side_effect=fake.upload_json):
        gs_path = push_bundle(bundle)
    assert gs_path == f"gs://csv-exports/{BUNDLE_LATEST_PATH}"
    assert fake.store[BUNDLE_LATEST_PATH] == bundle
    # No version passed -> no history copy written.
    assert list(fake.store.keys()) == [BUNDLE_LATEST_PATH]

    with patch("shared.cloud_storage.download_json", side_effect=fake.download_json):
        loaded = load_bundle()
    assert loaded == bundle


def test_push_bundle_writes_history_copy_when_version_given():
    fake = _FakeGCS()
    bundle = {"schema_version": SCHEMA_VERSION, "profiles": {}}
    with patch("shared.cloud_storage.upload_json", side_effect=fake.upload_json):
        push_bundle(bundle, version="v42")
    assert BUNDLE_LATEST_PATH in fake.store
    assert "ai-agent/prod-config/history/v42.json" in fake.store


def test_load_bundle_missing_object_returns_none():
    fake = _FakeGCS()  # empty store
    with patch("shared.cloud_storage.download_json", side_effect=fake.download_json):
        assert load_bundle() is None


def test_load_bundle_gcs_error_returns_none_never_raises():
    def boom(path):
        raise RuntimeError("GCS exploded")

    # download_json itself swallows in production; simulate a helper that
    # raises to prove load_bundle's own guard never propagates.
    with patch("shared.cloud_storage.download_json", side_effect=boom):
        assert load_bundle() is None
