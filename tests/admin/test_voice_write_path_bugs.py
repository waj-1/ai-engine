"""The three D11 write-path bugs — each one a silent write.

None of these produced an error message; all three changed stored config
behind the operator's back, which is why they survived long enough to be
written down in a spec:

1. `pretty_json` turned an ABSENT value into `{}`. In a `<pre>` that is noise;
   in a textarea it is a write — the first save of any profile persisted
   `voice_config.voice_by_language = {}`, a key nobody had touched, into every
   subsequent diff and promote snapshot.
2. `voice_config` was missing from `_FORK_COPY_FIELDS`, so every fork of a
   voice-configured profile copied it through the "a column nobody listed"
   fallback — and logged a WARNING while doing the right thing, training
   operators to ignore the one signal that means "decide about this column".
3. The portal's voice-scenario upsert was a FULL overwrite while the admin
   MCP's was partial. The screen has one shared create form and no per-row
   editor, so re-submitting an existing slug to fix its steps also NULLed its
   title, locale and notes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient


# --------------------------------------------------------------------------- #
# 1. pretty_json: absent is not {}
# --------------------------------------------------------------------------- #
def _pretty(value):
    from admin.router import templates

    return templates.env.filters["pretty_json"](value)


def test_pretty_json_renders_an_absent_value_as_empty_string():
    """`{}` in a textarea is indistinguishable from an operator typing it, and
    the save path cannot tell them apart — so an unset key round-tripped into a
    stored `{}` on the first save of any profile."""
    assert _pretty(None) == ""


def test_pretty_json_still_renders_a_stored_empty_object():
    """The other half: `{}` that really IS stored must keep rendering, because
    round-tripping a value the operator set is correct."""
    assert _pretty({}) == "{}"
    assert _pretty({"ar": "x"}) == '{\n  "ar": "x"\n}'


def test_an_unset_voice_by_language_does_not_round_trip_into_a_stored_empty_map():
    """End-to-end over the real writer: render an absent value the way the
    template does, feed it back the way the browser would, and assert nothing
    was invented. This is the actual defect — the renderer and the writer are
    each defensible alone. (The writer under test moved 2026-08-26: the
    mapping-form fieldset is deleted, so the cockpit's renderer/writer pair —
    `_render_value` / `voice_config_from_form` — is the round trip a browser
    actually makes now.)"""
    from admin.screens.agent_voice import (VOICE_FIELDS, _render_value,
                                           voice_config_from_form)

    field = next(f for f in VOICE_FIELDS if f.key == "voice_by_language")
    rendered = _render_value(field, {})   # what the textarea now shows
    assert rendered == ""                 # absent renders empty, not "{}"
    out = voice_config_from_form(
        {"_voice_form": "1", "vc_voice_by_language": rendered},
        existing={"enabled": True},
    )
    assert "voice_by_language" not in out


# --------------------------------------------------------------------------- #
# 2. voice_config is a first-class fork field
# --------------------------------------------------------------------------- #
def test_voice_config_is_an_explicit_fork_copy_field():
    from admin.services.profiles import _FORK_COPY_FIELDS

    assert "voice_config" in _FORK_COPY_FIELDS


def test_forking_a_voice_profile_copies_the_block_without_warning(caplog):
    """The `untouched` fallback exists to shout about a column nobody decided
    on. Firing it for a column we DO want copied is worse than useless: it is a
    warning on the happy path, which is how a real one gets ignored."""
    from admin.services import profiles as profile_services

    src = {
        "profile_id": "p-1", "agent_kind": "orchard", "slug": "orchard_default",
        "environment": "prod", "status": "active", "is_active": True,
        "display_name": "Orchard", "description": "", "model_main_id": "m-1",
        "model_light_id": None, "tool_ids": [], "data_source_ids": [],
        "mcp_server_ids": [], "downstream_profile_ids": [],
        "parent_profile_id": None, "endpoint_path": "/orchard/chat/stream",
        "prompt_version_ids": {}, "escalation_policy": {},
        "additional_agent_kinds": [],
        "voice_config": {"enabled": True, "model_id": "rt-1",
                         "barge_in_enabled": False},
    }
    inserted: Dict[str, Any] = {}

    class _Q:
        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def order(self, *_a, **_k):
            return self

        def execute(self):
            class _R:
                data = [src]
            return _R()

        def insert(self, payload):
            inserted.update(payload)

            class _Ins:
                def execute(self_inner):
                    class _R:
                        data = [payload]
                    return _R()
            return _Ins()

    class _C:
        def table(self, _name):
            return _Q()

    with caplog.at_level(logging.WARNING, logger=profile_services.__name__), \
            patch("core.auth.get_service_client", return_value=_C()), \
            patch.object(profile_services, "record_change"), \
            patch.object(profile_services, "invalidate_profile_loader"):
        profile_services.fork_profile_row(profile_id="p-1", actor_user_id="u-1")

    assert inserted["voice_config"] == src["voice_config"]
    assert not [r for r in caplog.records if "not in copy list" in r.message], \
        [r.message for r in caplog.records]


# --------------------------------------------------------------------------- #
# 3. The portal's voice-scenario upsert is partial, like MCP's
# --------------------------------------------------------------------------- #
class _UpsertRecorder:
    def __init__(self):
        self.payloads: List[Dict[str, Any]] = []

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def delete(self):
        return self

    def upsert(self, payload, **_k):
        self.payloads.append(payload)
        return self

    def execute(self):
        class _R:
            data = []
        return _R()


@pytest.fixture
def scenario_writes():
    rec = _UpsertRecorder()
    with patch("admin.router.client_for", return_value=rec), \
            patch("admin.router.record_change"):
        yield rec


def test_portal_scenario_upsert_omits_blank_columns(client, scenario_writes):  # noqa: F811
    """Matching `admin_mcp/tools_test.py`'s `set`, which drops `None`s. A full
    overwrite from a form that cannot express every column is how title, locale
    and notes got silently cleared by an edit that only meant to change steps."""
    r = client.post(
        "/admin/voice-scenarios?env=staging",
        data={"slug": "orchard_iban", "agent_kind": "orchard", "title": "",
              "ws_path": "", "locale": "", "notes": "",
              "steps": '[{"type": "say", "text": "hi"}]', "enabled": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text[:400]
    payload = scenario_writes.payloads[-1]
    assert payload["slug"] == "orchard_iban"
    assert payload["steps"] == [{"type": "say", "text": "hi"}]
    for cleared in ("title", "locale", "notes"):
        assert cleared not in payload, cleared
    # `ws_path` is the one blank that still lands, and it is not a clear:
    # FastAPI substitutes a `Form()` default for an empty string, so an empty
    # box arrives here as the route's declared `/voice/live` — the same value
    # the column defaults to. Writing it is idempotent, not destructive.
    assert payload["ws_path"] == "/voice/live"


def test_portal_scenario_upsert_still_writes_values_it_was_given(
    client, scenario_writes,  # noqa: F811
):
    r = client.post(
        "/admin/voice-scenarios?env=staging",
        data={"slug": "s", "agent_kind": "orchard", "title": "IBAN change",
              "ws_path": "/orchard/voice/live", "locale": "ar", "notes": "n",
              "steps": "[]", "enabled": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    payload = scenario_writes.payloads[-1]
    assert payload["title"] == "IBAN change"
    assert payload["ws_path"] == "/orchard/voice/live"
    assert payload["locale"] == "ar"
    assert payload["notes"] == "n"


def test_unchecking_enabled_still_disables_the_scenario(client, scenario_writes):  # noqa: F811
    """The one deliberate difference from MCP, and the reason it is deliberate:
    MCP's `enabled` is a tri-state Optional[bool] because a partial call may
    not mention it, while this form ALWAYS renders the checkbox — so absent
    means the operator unchecked it. Dropping it as "blank" would make
    disabling a scenario from the portal impossible."""
    r = client.post(
        "/admin/voice-scenarios?env=staging",
        data={"slug": "s", "agent_kind": "orchard", "steps": "[]"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert scenario_writes.payloads[-1]["enabled"] is False
