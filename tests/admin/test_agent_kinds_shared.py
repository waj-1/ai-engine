"""One live list of agents for every Build-area picker (admin/agent_kinds.py).

The Tools, Channels, Knowledge and Skills screens each carried their own
hard-coded tuple that predated the orchard / agents_* agents,
so a KB, channel or tool for those agents could not be created or filtered.
"""
from __future__ import annotations

import pytest

from admin import agent_kinds as ak
from tests.scoped_build import full_build

_PROFILES = [
    {"agent_kind": "business", "slug": "business_default", "display_name": "WAJ AI",
     "status": "active", "parent_profile_id": None},
    {"agent_kind": "business", "slug": "mold:rag", "display_name": "RAG mold",
     "status": "template", "parent_profile_id": None},
    {"agent_kind": "orchard", "slug": "orchard_voice", "display_name": "Noor voice",
     "status": "specialist", "parent_profile_id": "x"},
    {"agent_kind": "orchard", "slug": "orchard_default", "display_name": "Orchard Assistant",
     "status": "active", "parent_profile_id": None},
    {"agent_kind": "agents_support", "slug": "agents_support_default",
     "display_name": "Lina — Customer support (AI agents demo)", "status": "active",
     "parent_profile_id": None},
    {"agent_kind": "orchard_title", "slug": "orchard_title_default",
     "display_name": "Orchard Conversation Title", "status": "active", "parent_profile_id": None},
    {"agent_kind": "templ_only", "slug": "mold:x", "display_name": "Mold",
     "status": "template", "parent_profile_id": None},
]


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(ak, "_fetch_profiles", lambda: list(_PROFILES))
    full_build(monkeypatch)


def test_live_list_is_base_then_live_kinds_without_system_agents(live):
    kinds = ak.live_agent_kinds()
    assert kinds[:len(ak.BASE_AGENT_KINDS)] == ak.BASE_AGENT_KINDS
    assert "orchard" in kinds and "agents_support" in kinds
    assert "orchard_title" not in kinds          # system agent hidden
    assert "templ_only" not in kinds            # a template is not an agent
    assert "orchard_title" in ak.live_agent_kinds(include_system=True)


def test_labels_use_the_default_profile_name(live):
    assert ak.agent_label("orchard") == "Orchard Assistant"            # default beats specialist
    assert ak.agent_label("agents_support") == "Lina — Customer support"  # demo suffix dropped
    assert ak.agent_label("triage_desk") == "Triage desk"                 # fallback when no profile
    assert ak.agent_label("some_new_kind") == "Some new kind"


def test_db_failure_still_lists_seeded_kinds(monkeypatch):
    """A failed profile read must not hide agents defined in code."""
    from admin.seed_agent_profiles import _AGENT_KINDS
    monkeypatch.setattr(ak, "_fetch_profiles", lambda: [])
    kinds = ak.live_agent_kinds()
    assert kinds[:len(ak.BASE_AGENT_KINDS)] == ak.BASE_AGENT_KINDS
    for kind in _AGENT_KINDS:
        assert (kind in kinds) != ak.is_system_kind(kind)


@pytest.mark.parametrize("helper", ["tools", "channels", "kb"])
def test_every_build_picker_offers_live_agents(live, helper):
    from admin.channel_handlers import _channel_agent_kinds
    from admin.kb_handlers import _kb_agent_kinds
    from admin.tool_handlers import _tool_agent_kinds

    kinds = {"tools": _tool_agent_kinds, "channels": _channel_agent_kinds, "kb": _kb_agent_kinds}[helper]()
    assert "orchard" in kinds
    assert "agents_support" in kinds


def test_channel_validation_keeps_system_kinds_already_saved(live):
    """Saving a channel that already lists a system agent must not drop it."""
    from admin.channel_handlers import _parse_agent_kinds

    assert _parse_agent_kinds(["orchard", "orchard_title", "bogus"]) == ["orchard", "orchard_title"]


def test_kb_list_filter_and_create_form_offer_orchard(client, live):  # noqa: F811
    r = client.get("/admin/kb")
    assert r.status_code == 200
    assert 'value="orchard"' in r.text
    assert 'value="agents_support"' in r.text
    assert "Orchard Assistant" in r.text


from tests.admin.conftest import client  # noqa: E402,F401
