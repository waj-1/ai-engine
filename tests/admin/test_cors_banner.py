"""Tests for the "channels changed, restart required" banner on /admin/channels.

The banner exists because FastAPI's CORSMiddleware captures the
allow-list by value at boot — channel edits made after the agent
process has started don't reach the middleware until the next
restart. The banner makes that latency visible to operators.

Three pieces glue this together:

* ``core/cors.py`` — module-globals (``_cors_dirty`` + lock) for the
  in-process flag; ``mark_cors_dirty`` / ``is_cors_dirty`` /
  ``clear_cors_dirty`` mutators.
* ``admin/channel_handlers.py`` — list view passes ``cors_dirty`` to
  the template; ``POST /admin/channels/cors-dirty/clear`` is the
  operator-facing escape hatch.
* ``admin/templates/channels_list.html`` — renders the banner +
  "Mark as fixed" form when ``cors_dirty`` is truthy.

The flag is *process-local* (a module global behind a threading.Lock,
not a DB row), so a fresh process auto-clears it; the explicit clear
exists for the "operator restarted the agent but their banner-bearing
process is still hot" case + as a way to dismiss the banner without
rebooting.
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
# Minimal supabase fake — mirrors test_channels.py's _FakeClient so that
# the /admin/channels GET handler can render against a deterministic row
# set. Keeping the surface narrow (no order/eq/limit chain bookkeeping
# beyond what we exercise here) is intentional: the cors-banner concern
# is template-level + handler-level, not DB-level.
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def select(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def limit(self, *a, **kw): return self
    def order(self, *a, **kw): return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _FakeClient:
    def __init__(self, rows_by_table=None):
        self._rows_by_table = rows_by_table or {}

    def table(self, name):
        return _FakeQuery(self._rows_by_table.get(name, []))


def _make_app_with_fake_db(fake_client):
    """Same shape as test_channels._make_app_with_fake_db: bypass auth,
    swap in the fake supabase client, leave the dirty-flag plumbing
    intact (don't patch _invalidate_channels_cache here — these tests
    want to observe its side effect)."""
    from fastapi.testclient import TestClient
    from admin import auth, channel_handlers, _business_picker
    from main import app

    def _fake_superuser():
        return {
            "user_token": "fake-jwt",
            "user_id": "u-1",
            "email": "test@waj.local",
        }

    prior_auth_override = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(channel_handlers, "admin_service_client", return_value=fake_client),
        patch.object(channel_handlers, "client_for", return_value=fake_client),
        patch.object(channel_handlers, "record_change"),
        patch.object(_business_picker, "fetch_businesses_for_picker",
                     return_value=[]),
    ]
    for p in patches:
        p.start()

    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        if prior_auth_override is not None:
            app.dependency_overrides[auth.require_superuser] = prior_auth_override
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


@pytest.fixture(autouse=True)
def _reset_cors_dirty_flag():
    """Every test starts with a clean dirty flag; reset afterwards so
    one test's mutation can't leak into the next.

    The flag is a module global protected by a Lock, so the only way
    to isolate tests is to bracket each one with an explicit clear."""
    from core.cors import clear_cors_dirty
    clear_cors_dirty()
    yield
    clear_cors_dirty()


# ---------------------------------------------------------------------------
# Module-level flag mutators
# ---------------------------------------------------------------------------


class TestCorsDirtyFlagBasics:
    def test_initial_state_is_clean(self):
        from core.cors import is_cors_dirty
        assert is_cors_dirty() is False

    def test_mark_then_clear_round_trip(self):
        from core.cors import clear_cors_dirty, is_cors_dirty, mark_cors_dirty

        assert is_cors_dirty() is False
        mark_cors_dirty()
        assert is_cors_dirty() is True
        clear_cors_dirty()
        assert is_cors_dirty() is False

    def test_mark_is_idempotent(self):
        """Two writes in a row shouldn't somehow toggle the flag back
        off — this is a sanity check that mark_cors_dirty is a set,
        not a flip."""
        from core.cors import is_cors_dirty, mark_cors_dirty

        mark_cors_dirty()
        mark_cors_dirty()
        mark_cors_dirty()
        assert is_cors_dirty() is True

    def test_build_allowed_origins_clears_flag_on_fresh_snapshot(self):
        """build_allowed_origins is the function main.py calls at boot
        to seed the CORSMiddleware. Each call (re-)snapshots the
        channel origins into the middleware, so the dirty flag — which
        only matters for *future* mutations — should be reset on every
        successful build."""
        from core.cors import (
            build_allowed_origins, is_cors_dirty, mark_cors_dirty,
        )

        mark_cors_dirty()
        assert is_cors_dirty() is True

        build_allowed_origins(
            env_value="https://app.company.example",
            extra_origins=set(),
            channels_provider=lambda: {},
        )
        assert is_cors_dirty() is False

    def test_build_allowed_origins_clears_flag_in_wildcard_mode(self):
        """Even in ALLOWED_ORIGINS=* mode (channels skipped entirely),
        a fresh build represents a fresh allow-list snapshot — clear
        the flag so the banner doesn't lie."""
        from core.cors import (
            build_allowed_origins, is_cors_dirty, mark_cors_dirty,
        )

        mark_cors_dirty()
        result = build_allowed_origins(
            env_value="*",
            extra_origins=set(),
            channels_provider=lambda: {},
        )
        assert result == ["*"]
        assert is_cors_dirty() is False


# ---------------------------------------------------------------------------
# Channel-handler integration: mutations flip the flag.
# ---------------------------------------------------------------------------


class TestInvalidateMarksDirty:
    def test_invalidate_helper_sets_dirty(self):
        from admin.channel_handlers import _invalidate_channels_cache
        from core.cors import is_cors_dirty

        assert is_cors_dirty() is False
        _invalidate_channels_cache()
        assert is_cors_dirty() is True


# ---------------------------------------------------------------------------
# List view: template receives + renders the banner.
# ---------------------------------------------------------------------------


class TestListViewBanner:
    def test_banner_absent_when_flag_clean(self):
        fake = _FakeClient(rows_by_table={"agent_channels": []})
        client, teardown = _make_app_with_fake_db(fake)
        try:
            r = client.get("/admin/channels")
            assert r.status_code == 200
            assert "data-cors-dirty-banner" not in r.text
            # And no copy from the banner body.
            assert "CORS allow-list is stale" not in r.text
        finally:
            teardown()

    def test_banner_present_when_flag_set(self):
        from core.cors import mark_cors_dirty

        fake = _FakeClient(rows_by_table={"agent_channels": []})
        client, teardown = _make_app_with_fake_db(fake)
        try:
            mark_cors_dirty()
            r = client.get("/admin/channels")
            assert r.status_code == 200
            # Marker attribute (data-cors-dirty-banner on the wrapper div),
            # banner copy (inside the alert callout), and the "Mark as fixed"
            # form (inside the alert body) all need to be present together.
            assert "data-cors-dirty-banner" in r.text
            assert "CORS allow-list is stale" in r.text
            assert 'action="/admin/channels/cors-dirty/clear"' in r.text
            assert "Mark as fixed" in r.text
            # The alert renders via the sanctioned callout component.
            assert 'class="alert alert-warning"' in r.text
        finally:
            teardown()


# ---------------------------------------------------------------------------
# POST /admin/channels/cors-dirty/clear — manual reset endpoint.
# ---------------------------------------------------------------------------


class TestCorsDirtyClearEndpoint:
    def test_post_clears_flag_and_redirects(self):
        from admin import channel_handlers
        from core.cors import is_cors_dirty, mark_cors_dirty

        fake = _FakeClient()
        client, teardown = _make_app_with_fake_db(fake)
        try:
            mark_cors_dirty()
            assert is_cors_dirty() is True

            r = client.post(
                "/admin/channels/cors-dirty/clear", follow_redirects=False
            )
            assert r.status_code == 303, r.text[:400]
            assert r.headers["location"] == "/admin/channels"
            assert is_cors_dirty() is False

            # Audit row was emitted. We patched record_change in the
            # fixture; assert it was called with the right shape.
            channel_handlers.record_change.assert_called_once()
            kwargs = channel_handlers.record_change.call_args.kwargs
            assert kwargs["entity_type"] == "agent_channels"
            assert kwargs["action"] == "cors_dirty_clear"
            assert kwargs["actor_user_id"] == "u-1"
        finally:
            teardown()

    def test_post_when_already_clean_is_noop_redirect(self):
        """Clicking the button when the flag is already clean must not
        500 — it's just a redundant clear."""
        from core.cors import is_cors_dirty

        fake = _FakeClient()
        client, teardown = _make_app_with_fake_db(fake)
        try:
            assert is_cors_dirty() is False
            r = client.post(
                "/admin/channels/cors-dirty/clear", follow_redirects=False
            )
            assert r.status_code == 303
            assert is_cors_dirty() is False
        finally:
            teardown()
