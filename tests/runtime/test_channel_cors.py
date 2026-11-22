"""Tests for the agent_channels → runtime wiring.

Covers the four deliverables from the integration PR:

* ``core.cors.build_allowed_origins`` unions the extra origins
  with channels.allowed_origins, short-circuits on the ``"*"`` wildcard,
  and copes with an empty channels map.
* ``core.channel_resolver.resolve_channel`` matches by exact Origin,
  returns None for unknown origins, and stashes the channel on
  ``request.state.channel``.
* ``core.channel_resolver.enforce_channel_kind`` returns 403 when the
  channel's ``allowed_agent_kinds`` doesn't permit the dispatched
  agent_kind, and is a no-op for the public-API path (channel=None).
* ``core.channel_resolver.tag_active_span`` calls
  ``Laminar.set_span_attributes`` with ``waj.channel.*`` keys.

The unit tests deliberately avoid spinning up the full FastAPI app —
the relevant behaviours all live in helper functions we can call
directly. End-to-end coverage of the dependency wiring belongs in the
existing smoke suite (which already boots the app and would catch a
broken dependency).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _waj_app_channel() -> Dict[str, Any]:
    return {
        "channel_id": "ch-waj-app",
        "slug": "waj_app",
        "name": "WAJ App",
        "kind": "waj_app",
        "is_active": True,
        "allowed_origins": ["https://app.company.example"],
        "allowed_agent_kinds": ["business"],
        "default_profile_id": None,
        "business_id": None,
        "auth_required": True,
        "rate_limit_rpm": 60,
        "config": {},
    }


def _helpdesk_channel() -> Dict[str, Any]:
    return {
        "channel_id": "ch-helpdesk",
        "slug": "helpdesk_app",
        "name": "Helpdesk App",
        "kind": "helpdesk_app",
        "is_active": True,
        "allowed_origins": ["https://helpdesk.company.example", "https://helpdesk-staging.company.example"],
        "allowed_agent_kinds": ["support", "support_triage"],
        "default_profile_id": None,
        "business_id": None,
        "auth_required": True,
        "rate_limit_rpm": 60,
        "config": {},
    }


def _unrestricted_channel() -> Dict[str, Any]:
    """Channel with no agent_kind restriction — allowed_agent_kinds=[]
    means 'any kind goes' per the form contract."""
    return {
        "channel_id": "ch-open",
        "slug": "embed_open",
        "name": "Open embed",
        "kind": "embed",
        "is_active": True,
        "allowed_origins": ["https://embed.example.com"],
        "allowed_agent_kinds": [],
        "default_profile_id": None,
        "business_id": None,
        "auth_required": False,
        "rate_limit_rpm": 60,
        "config": {},
    }


# ---------------------------------------------------------------------------
# build_allowed_origins
# ---------------------------------------------------------------------------


class TestBuildAllowedOrigins:
    def test_unions_extra_and_channels(self):
        from core.cors import build_allowed_origins

        result = build_allowed_origins(
            env_value="https://explicit.example.com",
            extra_origins={"https://tenant1.com", "https://tenant2.com"},
            channels_provider=lambda: {"waj_app": _waj_app_channel(), "helpdesk": _helpdesk_channel()},
        )
        assert "https://explicit.example.com" in result
        assert "https://tenant1.com" in result
        assert "https://tenant2.com" in result
        assert "https://app.company.example" in result
        assert "https://helpdesk.company.example" in result
        assert "https://helpdesk-staging.company.example" in result

    def test_empty_channels_keeps_extra(self):
        from core.cors import build_allowed_origins

        result = build_allowed_origins(
            env_value="https://explicit.example.com",
            extra_origins={"https://tenant.example.com"},
            channels_provider=lambda: {},
        )
        assert result == sorted(
            ["https://explicit.example.com", "https://tenant.example.com"]
        )

    def test_wildcard_short_circuits(self):
        """ALLOWED_ORIGINS=* must NOT pull in channels (which would
        forbid `*` mode anyway by virtue of CORS spec disallowing
        credentials with a wildcard)."""
        from core.cors import build_allowed_origins

        # Provider should never even be called when wildcard mode is on;
        # set up one that would fail loudly if invoked.
        def boom():
            raise AssertionError("channels_provider must not be called when env='*'")

        result = build_allowed_origins(
            env_value="*",
            extra_origins={"https://tenant.example.com"},
            channels_provider=boom,
        )
        assert result == ["*"]

    def test_dedupes_origins(self):
        """An origin appearing in BOTH the env list and a channel's
        allowed_origins ends up once in the merged list."""
        from core.cors import build_allowed_origins

        result = build_allowed_origins(
            env_value="https://app.company.example",
            extra_origins={"https://app.company.example"},
            channels_provider=lambda: {"waj_app": _waj_app_channel()},
        )
        # Single occurrence, sorted order is stable.
        assert result.count("https://app.company.example") == 1

    def test_provider_failure_doesnt_break(self):
        """A flaky channels_provider must not blow up the CORS bootstrap."""
        from core.cors import build_allowed_origins

        def flaky():
            raise RuntimeError("supabase is down")

        result = build_allowed_origins(
            env_value="https://app.company.example",
            extra_origins=set(),
            channels_provider=flaky,
        )
        assert result == ["https://app.company.example"]

    def test_no_extra_no_channels(self):
        from core.cors import build_allowed_origins

        result = build_allowed_origins(
            env_value="https://only.example.com",
            extra_origins=None,
            channels_provider=lambda: {},
        )
        assert result == ["https://only.example.com"]


# ---------------------------------------------------------------------------
# CORS dirty flag
# ---------------------------------------------------------------------------


class TestCorsDirtyFlag:
    def test_flag_starts_clean(self):
        from core.cors import clear_cors_dirty, is_cors_dirty

        clear_cors_dirty()
        assert is_cors_dirty() is False

    def test_mark_sets_flag(self):
        from core.cors import clear_cors_dirty, is_cors_dirty, mark_cors_dirty

        clear_cors_dirty()
        mark_cors_dirty()
        assert is_cors_dirty() is True
        clear_cors_dirty()
        assert is_cors_dirty() is False

    def test_channel_handlers_invalidate_marks_dirty(self):
        """The channel admin write path should set the dirty flag so
        future banner UI can tell operators a restart is needed."""
        from admin.channel_handlers import _invalidate_channels_cache
        from core.cors import clear_cors_dirty, is_cors_dirty

        clear_cors_dirty()
        _invalidate_channels_cache()
        assert is_cors_dirty() is True
        clear_cors_dirty()


# ---------------------------------------------------------------------------
# resolve_channel
# ---------------------------------------------------------------------------


def _fake_request(origin: str | None = None) -> Any:
    """Build a minimal stand-in for ``fastapi.Request`` sufficient for
    the resolver. Avoids spinning up an ASGI app for unit tests."""
    state = SimpleNamespace()
    headers = {"origin": origin} if origin else {}
    return SimpleNamespace(headers=headers, state=state)


class TestResolveChannel:
    def test_matches_by_origin_string(self):
        from core import channel_resolver

        channels = {"waj_app": _waj_app_channel(), "helpdesk": _helpdesk_channel()}
        with patch.object(channel_resolver, "_channels_snapshot", return_value=channels):
            request = _fake_request(origin="https://app.company.example")
            channel = channel_resolver.resolve_channel(request)

        assert channel is not None
        assert channel["slug"] == "waj_app"
        # Side effect — stash on request.state for downstream consumers.
        assert request.state.channel is channel

    def test_matches_secondary_origin_in_same_channel(self):
        """A channel can list multiple origins (prod + staging); the
        resolver must match any of them."""
        from core import channel_resolver

        channels = {"helpdesk": _helpdesk_channel()}
        with patch.object(channel_resolver, "_channels_snapshot", return_value=channels):
            request = _fake_request(origin="https://helpdesk-staging.company.example")
            channel = channel_resolver.resolve_channel(request)
        assert channel is not None
        assert channel["slug"] == "helpdesk_app"

    def test_unknown_origin_returns_none(self):
        from core import channel_resolver

        channels = {"waj_app": _waj_app_channel()}
        with patch.object(channel_resolver, "_channels_snapshot", return_value=channels):
            request = _fake_request(origin="https://malicious.example.com")
            channel = channel_resolver.resolve_channel(request)
        assert channel is None
        assert request.state.channel is None

    def test_missing_origin_header_returns_none(self):
        """Server-to-server calls (the public-API path) have no Origin
        header. The resolver must NOT raise — it just attaches None."""
        from core import channel_resolver

        channels = {"waj_app": _waj_app_channel()}
        with patch.object(channel_resolver, "_channels_snapshot", return_value=channels):
            request = _fake_request(origin=None)
            channel = channel_resolver.resolve_channel(request)
        assert channel is None
        assert request.state.channel is None

    def test_origin_match_is_case_insensitive(self):
        """Browsers send lowercase but operators occasionally enter
        mixed-case domains in the admin form."""
        from core import channel_resolver

        channels = {"waj_app": _waj_app_channel()}
        with patch.object(channel_resolver, "_channels_snapshot", return_value=channels):
            request = _fake_request(origin="HTTPS://APP.COMPANY.EXAMPLE")
            channel = channel_resolver.resolve_channel(request)
        assert channel is not None
        assert channel["slug"] == "waj_app"


# ---------------------------------------------------------------------------
# enforce_channel_kind
# ---------------------------------------------------------------------------


class TestEnforceChannelKind:
    def test_403_on_kind_mismatch(self):
        from fastapi import HTTPException

        from core.channel_resolver import enforce_channel_kind

        request = _fake_request()
        request.state.channel = _waj_app_channel()  # allowed: ["business"]
        with pytest.raises(HTTPException) as exc_info:
            enforce_channel_kind(request, "support")
        assert exc_info.value.status_code == 403
        # Message names the slug + offending kind so operators can
        # diagnose without grepping logs.
        detail = exc_info.value.detail.lower()
        assert "waj_app" in detail
        assert "support" in detail

    def test_allows_when_kind_in_list(self):
        from core.channel_resolver import enforce_channel_kind

        request = _fake_request()
        request.state.channel = _helpdesk_channel()  # allowed: support, support_triage
        # Should not raise.
        enforce_channel_kind(request, "support")
        enforce_channel_kind(request, "support_triage")

    def test_empty_allowlist_means_unrestricted(self):
        """Operators may leave allowed_agent_kinds blank to indicate
        "any kind". Treat that as no restriction."""
        from core.channel_resolver import enforce_channel_kind

        request = _fake_request()
        request.state.channel = _unrestricted_channel()
        enforce_channel_kind(request, "business")
        enforce_channel_kind(request, "billing")

    def test_none_channel_is_noop(self):
        """The public-API path (no Origin / unmatched Origin) must not
        get blocked — the endpoint's own auth gate is what protects it."""
        from core.channel_resolver import enforce_channel_kind

        request = _fake_request()
        request.state.channel = None
        # Should not raise.
        enforce_channel_kind(request, "business")


# ---------------------------------------------------------------------------
# tag_active_span — spy pattern
# ---------------------------------------------------------------------------


class TestTagActiveSpan:
    def test_attributes_set_with_resolved_channel(self):
        """Laminar.set_span_attributes is called with the channel's
        slug + kind plus the agent_kind passed in."""
        from core import channel_resolver

        request = _fake_request()
        request.state.channel = _helpdesk_channel()

        captured: Dict[str, Any] = {}

        class _SpyLaminar:
            @staticmethod
            def set_span_attributes(attrs: Dict[str, Any]) -> None:
                captured.update(attrs)

        # Monkeypatch the import inside tag_active_span. The function
        # imports lazily so we patch the module attribute via sys.modules.
        fake_module = SimpleNamespace(Laminar=_SpyLaminar)
        with patch.dict(sys.modules, {"lmnr": fake_module}):
            channel_resolver.tag_active_span(request, agent_kind="support")

        assert captured.get("waj.channel.slug") == "helpdesk_app"
        assert captured.get("waj.channel.kind") == "helpdesk_app"
        assert captured.get("waj.agent_kind") == "support"
        # Env is always tagged so ClickHouse rows are env-attributable.
        assert captured.get("waj.env") in ("dev", "staging", "prod")

    def test_env_attr_normalised_from_environment(self, monkeypatch):
        """waj.env mirrors the dev/staging/prod enum, normalising the
        ENVIRONMENT var (e.g. 'production' -> 'prod')."""
        from core import channel_resolver

        request = _fake_request()
        request.state.channel = None

        monkeypatch.setenv("ENVIRONMENT", "production")
        attrs = channel_resolver.root_span_attrs(request, agent_kind="business")
        assert attrs["waj.env"] == "prod"

        monkeypatch.setenv("ENVIRONMENT", "staging")
        attrs = channel_resolver.root_span_attrs(request, agent_kind="business")
        assert attrs["waj.env"] == "staging"

        monkeypatch.delenv("ENVIRONMENT", raising=False)
        attrs = channel_resolver.root_span_attrs(request, agent_kind="business")
        assert attrs["waj.env"] == "staging"  # dev retired -> defaults to staging

    def test_attributes_default_to_unknown_when_no_channel(self):
        """If no channel was resolved, we still tag the span with
        explicit 'unknown' markers so operators can filter for
        unmatched requests in Laminar."""
        from core import channel_resolver

        request = _fake_request()
        request.state.channel = None

        captured: Dict[str, Any] = {}

        class _SpyLaminar:
            @staticmethod
            def set_span_attributes(attrs: Dict[str, Any]) -> None:
                captured.update(attrs)

        fake_module = SimpleNamespace(Laminar=_SpyLaminar)
        with patch.dict(sys.modules, {"lmnr": fake_module}):
            channel_resolver.tag_active_span(request, agent_kind="business")

        assert captured.get("waj.channel.slug") == "unknown"
        assert captured.get("waj.channel.kind") == "unknown"
        assert captured.get("waj.agent_kind") == "business"

    def test_lmnr_import_failure_is_swallowed(self):
        """A missing/broken Laminar SDK must not break the request."""
        from core import channel_resolver

        request = _fake_request()
        request.state.channel = _waj_app_channel()

        # Make `from lmnr import Laminar` raise. We accomplish this by
        # inserting an object whose attribute access raises.
        class _ExplodingModule:
            def __getattr__(self, name):
                raise RuntimeError("lmnr broken")

        with patch.dict(sys.modules, {"lmnr": _ExplodingModule()}):
            # Must not raise.
            channel_resolver.tag_active_span(request, agent_kind="business")
