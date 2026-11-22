# ai_agent/tests/admin/test_memory_agent_kind.py
"""Tests that the /admin/memory screen is parameterized by agent_kind.

Strategy: call the screen functions directly with a mock client so we can
inspect the `.eq("agent_kind", ...)` calls without booting the full app.
Also exercise the route via the shared smoke `client` fixture to confirm
the query param is forwarded end-to-end.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Mock DB client helpers
# ---------------------------------------------------------------------------

def _make_mock_client():
    """Fluent supabase mock that records .eq() calls and returns empty data."""
    mock_result = MagicMock()
    mock_result.data = []

    builder = MagicMock()
    builder.execute.return_value = mock_result
    for method in ("select", "eq", "order", "limit", "insert", "update", "delete", "in_"):
        getattr(builder, method).return_value = builder

    client = MagicMock()
    client.table.return_value = builder
    return client, builder


# ---------------------------------------------------------------------------
# Unit tests: screen functions called directly
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _sample_kinds(product_build):
    """Generic screens, fictional agent kinds (tests/conftest.py)."""


class TestLoadMemoriesContext:
    def test_uses_supplied_agent_kind(self):
        from admin.screens.memory import load_memories_context

        client, builder = _make_mock_client()
        # Provide a UUID-like string so _resolve_user_id returns it directly.
        load_memories_context(client, "00000000-0000-0000-0000-000000000001", agent_kind="sample_other")

        # Collect all .eq() calls made on the builder
        eq_calls = [c for c in builder.eq.call_args_list]
        agent_kind_calls = [c for c in eq_calls if c.args and c.args[0] == "agent_kind"]
        assert agent_kind_calls, "No .eq('agent_kind', ...) call found"
        # The value passed must be 'sample_other'
        assert agent_kind_calls[0].args[1] == "sample_other"

    def test_defaults_to_the_default_kind_when_no_arg(self):
        from admin.screens.memory import load_memories_context

        client, builder = _make_mock_client()
        load_memories_context(client, "00000000-0000-0000-0000-000000000001")

        from admin.screens.memory import _DEFAULT_AGENT_KIND
        eq_calls = builder.eq.call_args_list
        agent_kind_calls = [c for c in eq_calls if c.args and c.args[0] == "agent_kind"]
        assert agent_kind_calls, "No .eq('agent_kind', ...) call found"
        assert agent_kind_calls[0].args[1] == _DEFAULT_AGENT_KIND

    def test_no_query_when_no_user(self):
        from admin.screens.memory import load_memories_context

        client, builder = _make_mock_client()
        ctx = load_memories_context(client, None, agent_kind="sample_other")
        # No DB query should be made when user_id is missing
        assert ctx["memories"] == []
        assert ctx["resolved_user_id"] is None


class TestLoadConfigContext:
    def test_uses_supplied_agent_kind(self):
        from admin.screens.memory import load_config_context

        client, builder = _make_mock_client()
        load_config_context(client, agent_kind="sample_other")

        eq_calls = builder.eq.call_args_list
        agent_kind_calls = [c for c in eq_calls if c.args and c.args[0] == "agent_kind"]
        assert agent_kind_calls, "No .eq('agent_kind', ...) call found"
        assert agent_kind_calls[0].args[1] == "sample_other"

    def test_defaults_to_the_default_kind(self):
        from admin.screens.memory import load_config_context

        client, builder = _make_mock_client()
        load_config_context(client)

        from admin.screens.memory import _DEFAULT_AGENT_KIND
        eq_calls = builder.eq.call_args_list
        agent_kind_calls = [c for c in eq_calls if c.args and c.args[0] == "agent_kind"]
        assert agent_kind_calls
        assert agent_kind_calls[0].args[1] == _DEFAULT_AGENT_KIND


# ---------------------------------------------------------------------------
# Handler-level validation: _resolve_agent_kind
# ---------------------------------------------------------------------------

class TestResolveAgentKind:
    def test_valid_kind_passes_through(self):
        from admin.memory_handlers import _resolve_agent_kind

        assert _resolve_agent_kind("sample_agent") == "sample_agent"
        assert _resolve_agent_kind("sample_other") == "sample_other"

    def test_invalid_kind_falls_back_to_the_default(self):
        from admin.memory_handlers import _resolve_agent_kind

        from admin.memory_handlers import _default_agent_kind

        assert _resolve_agent_kind("nonexistent_agent") == _default_agent_kind()
        assert _resolve_agent_kind("") == _default_agent_kind()

    def test_all_seed_kinds_are_valid(self):
        from admin.memory_handlers import _resolve_agent_kind
        from admin.seed_agent_profiles import _AGENT_KINDS

        for kind in _AGENT_KINDS:
            assert _resolve_agent_kind(kind) == kind, f"{kind} should be valid"


# ---------------------------------------------------------------------------
# Route smoke tests via TestClient
# ---------------------------------------------------------------------------

class TestMemoryRoute:
    def test_default_agent_kind_is_the_default(self, client):
        """GET /admin/memory with no agent_kind param renders the default agent."""
        r = client.get("/admin/memory")
        assert r.status_code == 200
        from admin.memory_handlers import _default_agent_kind
        assert _default_agent_kind() in r.text

    def test_other_agent_kind_renders(self, client):
        """GET /admin/memory?agent_kind=sample_other should return 200 and show sample_other."""
        r = client.get("/admin/memory?agent_kind=sample_other")
        assert r.status_code == 200
        assert "sample_other" in r.text

    def test_invalid_agent_kind_falls_back(self, client):
        """GET /admin/memory?agent_kind=doesnotexist should still 200 (falls back)."""
        r = client.get("/admin/memory?agent_kind=doesnotexist")
        assert r.status_code == 200
        # Falls back to the default kind
        from admin.memory_handlers import _default_agent_kind
        assert _default_agent_kind() in r.text

    def test_agent_kind_preserved_in_tab_links(self, client):
        """Switching tabs should carry agent_kind in the link hrefs."""
        r = client.get("/admin/memory?agent_kind=sample_other&tab=memories")
        assert r.status_code == 200
        # Tab links must carry agent_kind=sample_other
        assert "agent_kind=sample_other" in r.text

    def test_config_tab_other_kind(self, client):
        """Config (now the agent memory settings page) for sample_other renders."""
        r = client.get("/admin/memory?agent_kind=sample_other&tab=config")
        assert r.status_code == 200
        assert "sample_other" in r.text

    def test_picker_lists_product_agents_not_system_ones(self, client):
        """The agent picker lists every product agent kind; system agents
        (title / follow-up suggestion generators, translator) stay out of
        it — partners don't know those are agents. They still resolve when
        deep-linked (TestResolveAgentKind)."""
        from admin.agent_kinds import is_system_kind
        from admin.seed_agent_profiles import _AGENT_KINDS

        r = client.get("/admin/memory?agent_kind=sample_agent")
        assert r.status_code == 200
        for kind in _AGENT_KINDS:
            if is_system_kind(kind):
                assert f'value="{kind}"' not in r.text, f"system kind {kind!r} in picker"
            else:
                assert f'value="{kind}"' in r.text, f"Kind '{kind}' missing from picker"

    def test_config_tab_redirects_to_agent_memory_settings(self, client):
        """Per-agent memory settings moved to /admin/agents/memory; the old
        tab URL keeps working as a redirect."""
        r = client.get("/admin/memory?agent_kind=sample_other&tab=config", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"].startswith("/admin/agents/memory?agent_kind=sample_other")

    def test_memory_page_links_to_agent_memory_settings(self, client):
        r = client.get("/admin/memory?agent_kind=sample_agent")
        assert "/admin/agents/memory?agent_kind=sample_agent" in r.text

    def test_agent_memory_settings_master_switch_gates_dependents(self, client, monkeypatch):
        """Settings page: the master switch sits first and the dependent
        groups are wrapped so they can be greyed out (inert, not disabled —
        disabled inputs would not submit and saving would reset them)."""
        from admin.screens import memory as memory_screen

        monkeypatch.setattr(memory_screen, "load_config_context", lambda *_a, **_k: {
            "profile": {"profile_id": "p1", "slug": "sample_agent_default", "memory_config": {}},
            "config_form": memory_screen.shape_config_for_form({}),
        })
        r = client.get("/admin/agents/memory?agent_kind=sample_agent")
        assert r.status_code == 200
        text = r.text
        assert 'id="memory-config"' in text
        assert text.index('id="memory-enabled"') < text.index('id="memory-dependent"')
        assert 'name="extraction_importance_threshold"' in text.split('id="memory-dependent"', 1)[1]
        assert "setAttribute('inert'" in text
        assert " disabled" not in text.split('id="memory-dependent"', 1)[1].split("</form>")[0]
