"""The cockpit's brain panel — every write lands on the row the runtime reads.

The duplex call's settings split across TWO rows: the PARENT (active chat
profile) stores `voice_config`, the persona pin and the frozen realtime
model; the SPECIALIST named by `voice_config.agent_profile_slug` is the brain
— its `model_main_id`, its system-prompt pin, its bound tools, its
`memory_config`. Editing a brain setting on the parent looks applied and does
nothing, which is the trap this cockpit exists to remove. So what these tests
pin is TARGETING, not rendering polish:

* the brain form posts to the SPECIALIST's id while the voice form on the
  same page posts to the OWNER's;
* the memory toggle merges onto the row's existing `memory_config` (sibling
  keys survive) and an unmarked POST is refused rather than guessed at;
* the persona editor pins on the OWNER even when the cockpit was opened from
  the specialist (a specialist-side pin is inert and warns —
  endpoints/voice_live.py::resolve_persona_prompt);
* the bound-tool list is resolved the way the RUNTIME resolves it — MCP
  attachments narrowed by `tool_subset`
  (runtime/tools/_mcp_resolver.py::bound_tool_names_from_attachments) — and
  NOT from `agent_profiles.tool_ids`, which tool resolution reads nowhere; a
  `tool_ids` entry that does not bind is reported as exactly that.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

_PARENT = {
    "profile_id": "parent-1", "agent_kind": "orchard", "slug": "orchard_default",
    "display_name": "Orchard Merchant Data Assistant", "status": "active",
    "environment": "staging", "parent_profile_id": None,
    "prompt_version_ids": {}, "served_version_id": None,
    "model_main_id": "chat-parent",
    "voice_config": {
        "enabled": True,
        "model_id": "model-rt-1",
        "agent_profile_slug": "orchard_voice",
        "persona_prompt_slug": "orchard_voice_persona",
    },
}
_SPECIALIST = {
    "profile_id": "voice-1", "agent_kind": "orchard", "slug": "orchard_voice",
    "display_name": "Layla — Orchard Voice Call Agent", "status": "specialist",
    "environment": "staging", "parent_profile_id": "parent-1",
    "prompt_version_ids": {"system": "spv-7"}, "served_version_id": None,
    "model_main_id": "chat-1",
    "memory_config": {"enabled": False, "retrieval": {"min_similarity": 0.4}},
    "tool_ids": ["t-iban", "t-tickets", "t-orphan"],
    "voice_config": {},
}
_RT_MODEL = {
    "model_id": "model-rt-1", "name": "Azure Voice Live (native)",
    "provider": "azure_voice_live", "model_string": "gpt-realtime",
    "is_active": True, "environment": "staging",
    "extra_config": {"realtime": True, "voice_type": "native"},
}
_CHAT_MODEL = {
    "model_id": "chat-1", "name": "GPT-5 Mini", "provider": "azure_openai",
    "model_string": "gpt-5-mini", "is_active": True, "environment": "staging",
    "extra_config": {},
}

_TABLES = {
    "agent_profiles": [_PARENT, _SPECIALIST],
    "agent_models": [_RT_MODEL, _CHAT_MODEL],
    "agent_profile_versions": [],
    "agent_prompts": [
        {"prompt_id": "pp-1", "slug": "orchard_voice_persona",
         "agent_kind": "orchard"},
    ],
    "agent_prompt_versions": [
        {"prompt_version_id": "ppv-3", "prompt_id": "pp-1",
         "version_number": 3, "content": "أنت ليلى.",
         "created_at": "2026-08-20T09:00:00"},
        {"prompt_version_id": "spv-7", "prompt_id": "sys-1",
         "version_number": 7, "content": "You are the orchard brain.",
         "created_at": "2026-08-21T09:00:00"},
    ],
    "agent_profile_mcp_servers": [
        {"profile_id": "voice-1", "mcp_server_id": "srv-1",
         "tool_subset": ["orchard_get_iban"], "position": 0},
    ],
    "agent_mcp_servers": [
        {"mcp_server_id": "srv-1", "name": "orchard-bundle",
         "source": "bundle", "is_active": True},
    ],
    "agent_mcp_tools": [
        {"mcp_server_id": "srv-1", "tool_id": "t-iban", "position": 0},
        {"mcp_server_id": "srv-1", "tool_id": "t-tickets", "position": 1},
    ],
    "agent_tools": [
        {"tool_id": "t-iban", "python_name": "orchard_get_iban",
         "is_deprecated": False},
        {"tool_id": "t-tickets", "python_name": "orchard_create_ticket",
         "is_deprecated": False},
        {"tool_id": "t-orphan", "python_name": "search_orchard_docs",
         "is_deprecated": False},
    ],
}


class _Query:
    """A minimal PostgREST-shaped query over in-memory rows — the surface
    `load_voice_context` + `collect_brain_panel` use (`select/eq/in_/order/
    limit/execute`). Not a MagicMock: the point is that the screen asks the
    right questions of the right tables, and a mock answering everything with
    `[]` would pass while resolving nothing."""

    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def order(self, column, desc=False, **_k):
        return _Query(sorted(
            self._rows, key=lambda r: (r.get(column) is None, r.get(column)),
            reverse=bool(desc),
        ))

    def eq(self, column, value):
        return _Query([r for r in self._rows if r.get(column) == value])

    def in_(self, column, values):
        allowed = set(values or [])
        return _Query([r for r in self._rows if r.get(column) in allowed])

    def limit(self, n):
        return _Query(self._rows[:n])

    def insert(self, payload):
        row = dict(payload)
        row.setdefault("prompt_version_id", "ppv-new")
        return _Query([row])

    def update(self, payload):
        return _Query([dict(payload)])

    def execute(self):
        class _R:
            data = self._rows
        return _R()


class _Client:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


@pytest.fixture
def voice_db():
    with patch("admin.router.client_for", return_value=_Client(_TABLES)), \
            patch("admin.router.admin_service_client",
                  return_value=_Client(_TABLES)):
        yield


# --------------------------------------------------------------------------- #
# Write targeting — the whole point
# --------------------------------------------------------------------------- #
def test_brain_post_writes_model_and_memory_to_the_row_in_its_path(client, voice_db):  # noqa: F811
    """model_main_id and memory_config land on the SPECIALIST, and the memory
    toggle merges — the row's sibling keys (`retrieval`) survive.

    Discriminates: routing the write to the owner instead fails on
    profile_id; overwriting memory_config wholesale fails on `retrieval`.
    """
    captured = {}

    def _update(*, profile_id, payload, actor_user_id):
        captured["profile_id"] = profile_id
        captured["payload"] = payload
        return {}

    with patch("admin.services.profiles.update_profile_row", _update):
        r = client.post(
            "/admin/agents/voice-1/voice/brain?env=staging",
            data={"_brain_form": "1", "return_profile_id": "parent-1",
                  "brain_model_main_id": "chat-1",
                  "brain_memory_enabled": "on"},
            follow_redirects=False,
        )
    assert r.status_code == 303, r.text[:400]
    assert r.headers["location"].startswith("/admin/agents/parent-1/voice")
    assert captured["profile_id"] == "voice-1"
    assert captured["payload"]["model_main_id"] == "chat-1"
    assert captured["payload"]["memory_config"] == {
        "enabled": True, "retrieval": {"min_similarity": 0.4},
    }
    assert "voice_config" not in captured["payload"]


def test_brain_post_unchecked_memory_persists_an_explicit_false(client, voice_db):  # noqa: F811
    """The panel renders the checkbox on every load, so a marked submission
    with it absent means the operator unchecked it — and `memory_config.enabled`
    is one of the drift check's silent-when-wrong levers, so the OFF must be a
    stored value, not a missing key."""
    captured = {}

    def _update(*, profile_id, payload, actor_user_id):
        captured["payload"] = payload
        return {}

    with patch("admin.services.profiles.update_profile_row", _update):
        r = client.post(
            "/admin/agents/voice-1/voice/brain?env=staging",
            data={"_brain_form": "1", "brain_model_main_id": "chat-1"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert captured["payload"]["memory_config"]["enabled"] is False
    assert captured["payload"]["memory_config"]["retrieval"] == {
        "min_similarity": 0.4,
    }


def test_null_memory_config_row_gets_a_fresh_dict_not_a_crash():
    """A row that has never had `memory_config` set stores SQL NULL, which
    reaches the writer as None — the merge must start from an empty dict."""
    from admin.screens.agent_voice import brain_update_from_form

    payload = brain_update_from_form(
        {"_brain_form": "1", "brain_memory_enabled": "on"},
        existing_memory_config=None,
    )
    assert payload["memory_config"] == {"enabled": True}


def test_brain_post_without_the_marker_is_refused(client, voice_db):  # noqa: F811
    """Same refuse-don't-guess gate as the voice form: a POST that cannot
    prove the panel was rendered owns nothing — writing its absent memory
    checkbox would turn a stray request into a lever flip."""
    with patch("admin.services.profiles.update_profile_row") as update:
        r = client.post(
            "/admin/agents/voice-1/voice/brain?env=staging",
            data={"brain_memory_enabled": "on"},
            follow_redirects=False,
        )
    assert r.status_code == 400
    update.assert_not_called()


def test_blank_model_select_does_not_clear_the_brain_model(client, voice_db):  # noqa: F811
    """A brain with no model_main_id cannot answer at all, and the cockpit
    offers no legitimate way to want that — blank is skipped, never written."""
    captured = {}

    def _update(*, profile_id, payload, actor_user_id):
        captured["payload"] = payload
        return {}

    with patch("admin.services.profiles.update_profile_row", _update):
        r = client.post(
            "/admin/agents/voice-1/voice/brain?env=staging",
            data={"_brain_form": "1", "brain_model_main_id": ""},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "model_main_id" not in captured["payload"]


def test_the_two_forms_on_one_page_target_their_own_rows(client, voice_db):  # noqa: F811
    """One screen, two writers, each pinned to its row: the voice form posts
    to the OWNER (voice_config), the brain form to the SPECIALIST
    (model_main_id / memory_config) — from BOTH entry points, because the
    cockpit resolves the pair from the DB regardless of which id opened it."""
    for opened_from in ("parent-1", "voice-1"):
        r = client.get(f"/admin/agents/{opened_from}/voice?env=staging")
        assert r.status_code == 200, r.text[:800]
        assert 'action="/admin/agents/parent-1/voice?env=staging"' in r.text
        assert 'action="/admin/agents/voice-1/voice/brain?env=staging"' in r.text


# --------------------------------------------------------------------------- #
# Persona editor — always pins on the owner
# --------------------------------------------------------------------------- #
def test_persona_editor_pins_on_the_owner_even_from_the_specialist(client, voice_db):  # noqa: F811
    """The runtime reads the persona pin off the PARENT; a specialist-side pin
    is inert and warns. So the editor's hidden profile_id — the row the
    version-create route repoints — must be the owner's on BOTH entry points.

    Discriminates: templating `profile.profile_id` instead of
    `owner.profile_id` fails this on the specialist entry.
    """
    for opened_from in ("parent-1", "voice-1"):
        r = client.get(f"/admin/agents/{opened_from}/voice?env=staging")
        assert r.status_code == 200, r.text[:800]
        assert 'action="/admin/prompts/pp-1/versions"' in r.text
        # Scope to the persona form: the page header (Voice tab of the
        # agent) carries its own promote form with the VIEWED profile's id.
        start = r.text.index('action="/admin/prompts/pp-1/versions"')
        persona_form = r.text[start:r.text.index("</form>", start)]
        assert 'name="profile_id" value="parent-1"' in persona_form
        assert 'name="profile_id" value="voice-1"' not in persona_form
        # ...and the save returns the operator to the cockpit they were on.
        assert f'name="voice_return_profile_id" value="{opened_from}"' in persona_form
        # The persona text itself is the editable content.
        assert "أنت ليلى." in r.text


def test_persona_save_route_redirects_back_to_the_cockpit(client, voice_db):  # noqa: F811
    """`voice_return_profile_id` sends the operator back to the cockpit; an
    arbitrary value cannot become a redirect target."""
    with patch("admin.router.PromptResolver"), \
            patch("admin.router.AgentProfileLoader"), \
            patch("admin.router.record_change"):
        r = client.post(
            "/admin/prompts/pp-1/versions",
            data={"content": "أنت ليلى. الإصدار الجديد.",
                  "profile_id": "parent-1", "prompt_key": "orchard_voice_persona",
                  "voice_return_profile_id": "voice-1"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:400]
        # env comes from the repointed profile row's own environment (#149's
        # redirect convention, kept on the cockpit return path too).
        assert r.headers["location"] == "/admin/agents/voice-1/voice?env=staging"

        evil = client.post(
            "/admin/prompts/pp-1/versions",
            data={"content": "أنت ليلى. الإصدار الجديد.",
                  "profile_id": "parent-1", "prompt_key": "orchard_voice_persona",
                  "voice_return_profile_id": "../../evil.example"},
            follow_redirects=False,
        )
        assert evil.status_code == 302
        # Falls through to the ordinary quoted profile redirect — the evil
        # value never reaches a Location header.
        assert "evil" not in evil.headers["location"]
        assert evil.headers["location"].startswith("/admin/agents?profile_id=parent-1")


# --------------------------------------------------------------------------- #
# Bound tools — tool_subset, not tool_ids
# --------------------------------------------------------------------------- #
def test_bound_tools_come_from_tool_subset_not_tool_ids():
    """The runtime binds from MCP attachments narrowed by `tool_subset`;
    `tool_ids` is read by nothing on that path. The fixture makes the two
    answers different on purpose: `tool_ids` lists three tools, the subset
    binds ONE — a panel reading tool_ids would show all three as live.

    Discriminates: resolving from `tool_ids` fails both assertions.
    """
    from admin.screens.agent_voice import collect_brain_panel

    panel = collect_brain_panel(
        _Client(_TABLES), "staging", _SPECIALIST, is_specialist=True,
    )
    assert panel["bound_tools"] == ["orchard_get_iban"]
    assert panel["listed_not_bound"] == [
        "orchard_create_ticket", "search_orchard_docs",
    ]


def test_an_empty_tool_subset_binds_every_member_tool():
    """`tool_subset` None/empty means "all of the bundle" — mirroring
    `_bundle_tools`' gate exactly, so the panel cannot under-report a profile
    that never narrowed its attachment."""
    from admin.screens.agent_voice import collect_brain_panel

    tables = dict(_TABLES)
    tables["agent_profile_mcp_servers"] = [
        {"profile_id": "voice-1", "mcp_server_id": "srv-1",
         "tool_subset": None, "position": 0},
    ]
    panel = collect_brain_panel(
        _Client(tables), "staging", _SPECIALIST, is_specialist=True,
    )
    assert panel["bound_tools"] == ["orchard_create_ticket", "orchard_get_iban"]
    assert panel["listed_not_bound"] == ["search_orchard_docs"]


def test_brain_model_picker_offers_chat_models_only():
    """The exact inverse of the realtime picker's gate: a realtime row picked
    as `model_main_id` breaks every delegated turn, so the brain's <select>
    must never offer one."""
    from admin.screens.agent_voice import collect_brain_panel

    panel = collect_brain_panel(
        _Client(_TABLES), "staging", _SPECIALIST, is_specialist=True,
    )
    ids = {m["model_id"] for m in panel["chat_models"]}
    assert "chat-1" in ids
    assert "model-rt-1" not in ids


def test_brain_panel_reports_the_pinned_system_prompt_version():
    from admin.screens.agent_voice import collect_brain_panel

    panel = collect_brain_panel(
        _Client(_TABLES), "staging", _SPECIALIST, is_specialist=True,
    )
    assert panel["system_prompt"]["version_number"] == 7
    assert panel["system_prompt"]["pinned"] is True


# --------------------------------------------------------------------------- #
# Rendering — the panel says what it resolved
# --------------------------------------------------------------------------- #
def test_cockpit_renders_the_brain_panel_with_bound_and_unbound_tools(client, voice_db):  # noqa: F811
    r = client.get("/admin/agents/parent-1/voice?env=staging")
    assert r.status_code == 200, r.text[:800]
    assert "The brain (specialist row)" in r.text
    assert "orchard_get_iban" in r.text
    # The tool_ids illusion is named, subdued, with its members listed.
    assert "not bound" in r.text
    assert "orchard_create_ticket" in r.text
    assert "search_orchard_docs" in r.text
    # The pinned brain prompt version is visible read-only.
    assert "v7" in r.text
    # Memory is off on the fixture → the checkbox is not pre-checked and the
    # latency warning does not fire.
    assert "cross-cloud" not in r.text


def test_cockpit_with_no_specialist_shows_the_owner_as_its_own_brain(client, voice_db):  # noqa: F811
    solo = {**_PARENT, "profile_id": "solo-1", "slug": "solo",
            "voice_config": {"enabled": True, "model_id": "model-rt-1"}}
    tables = {**_TABLES, "agent_profiles": [solo]}
    with patch("admin.router.client_for", return_value=_Client(tables)):
        r = client.get("/admin/agents/solo-1/voice?env=staging")
    assert r.status_code == 200, r.text[:800]
    assert "no separate specialist" in r.text
    assert 'action="/admin/agents/solo-1/voice/brain?env=staging"' in r.text
