"""The Agents screen no longer writes `voice_config` — the cockpit does.

The 26-key "Duplex voice (live)" fieldset (and its writer,
`admin/screens/agents.py::build_voice_config_from_form`) was deleted
2026-08-26. It was the trap the voice-cockpit change removes: it posted the
block to whichever row the operator was VIEWING — on a voice specialist, a
row the runtime never reads — and it could express only a subset of the keys
the runtime honours. The single portal writer of `voice_config` is now the
cockpit's own POST (`/admin/agents/{id}/voice`,
`admin/screens/agent_voice.py::voice_config_from_form`), whose write-path
contract is pinned in `tests/admin/test_voice_screen_field_coverage.py` and
`tests/admin/test_voice_screen_routes.py`.

This file replaces `tests/admin/test_agents_voice_fields.py`, whose ~25 tests
drove the deleted writer. Their behavioural claims did not vanish — blank
clears, absent-field-carries, explicit checkbox off, legacy-`live` promotion,
unknown-key survival all hold on the cockpit writer and are asserted in the
field-coverage suite — so the deletion is a move of coverage, not a loss.
What is pinned HERE instead:

* the template really carries no duplex inputs any more (a control that edits
  nothing, or worse edits the wrong row, is the defect the cockpit removes);
* the mapping-form POST (`profile_update`) never assembles a `voice_config`
  payload, even when a scripted caller still sends `live_*` fields;
* the replacement link card exists, keeps the `#voice-live` anchor the nav
  chip targets, and resolves the OWNER when the viewed profile is a served
  specialist.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

TEMPLATES = Path(__file__).resolve().parents[2] / "admin" / "templates"
# The agent editor: the detail page, its header and every tab partial (B1).
AGENT_TEMPLATES = [TEMPLATES / "agent_detail.html", TEMPLATES / "agents.html",
                   *sorted((TEMPLATES / "agents").glob("*.html"))]


def _src() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in AGENT_TEMPLATES)


# --------------------------------------------------------------------------- #
# The templates: no duplex inputs; voice lives on the Voice tab (the cockpit)
# --------------------------------------------------------------------------- #
def test_no_live_prefixed_input_survives_in_the_template():
    """Every duplex control carried the `live_` prefix; none may remain. A
    single leftover input would post into `profile_update`, which no longer
    reads it — a control that looks editable and saves nothing."""
    src = _src()
    assert 'name="live_' not in src
    assert 'name="vc_' not in src  # the cockpit's inputs belong to the cockpit


def test_the_fieldset_marker_is_gone_with_the_fieldset():
    """`live_fieldset=1` was the marker that licensed the writer to read an
    absent checkbox as an explicit OFF. The writer is deleted; a surviving
    marker would be a claim about controls that no longer exist."""
    assert 'name="live_fieldset"' not in _src()


def test_old_voice_live_anchor_lands_on_the_voice_tab():
    """Bookmarks to the old single-scroll page's `#voice-live` card are sent
    to the Voice tab, which IS the cockpit route."""
    from admin.screens.agents_views import LEGACY_ANCHORS, build_detail_view

    assert LEGACY_ANCHORS["voice-live"] == "voice"
    view = build_detail_view({
        "active_profile": {"profile_id": "p1", "agent_kind": "orchard",
                           "voice_config": {"model_id": "m1"}},
        "profiles": [],
    }, "staging")
    hrefs = {k: h for k, _label, h in view["detail_tabs"]}
    assert hrefs["voice"] == "/admin/agents/p1/voice?env=staging"


def test_specialist_overview_names_and_links_the_owner():
    """Opening a served specialist must point at the OWNER (where voice is
    configured), not teach that the specialist row is where voice lives."""
    src = (TEMPLATES / "agents" / "_tab_overview.html").read_text(encoding="utf-8")
    assert "Answers voice calls for" in src
    assert "profile_id={{ voice_config_owner.profile_id }}" in src


# --------------------------------------------------------------------------- #
# The route: profile_update leaves voice_config alone
# --------------------------------------------------------------------------- #
def test_profile_update_never_writes_voice_config(client):  # noqa: F811
    """A scripted POST still carrying the retired `live_*` fields (with both
    old markers, the strongest claim the fieldset era could make) must not
    produce a `voice_config` key in the update payload. This is the
    single-writer guarantee from the mapping form's side.

    Discriminates: restore the old `build_voice_config_from_form` call in
    `profile_update` and this fails with `voice_config` present.
    """
    captured = {}

    def _update(*, profile_id, payload, actor_user_id):
        captured["profile_id"] = profile_id
        captured["payload"] = payload
        return {}

    with patch("admin.services.profiles.update_profile_row", _update):
        r = client.post(
            "/admin/profiles/p-1",
            data={
                "_mapping_form": "1",
                "live_fieldset": "1",
                "display_name": "Renamed",
                "live_enabled": "on",
                "live_model_id": "m-realtime-1",
                "live_agent_profile_slug": "orchard_voice",
            },
            follow_redirects=False,
        )
    assert r.status_code == 302, r.text[:400]
    assert captured["profile_id"] == "p-1"
    assert "voice_config" not in captured["payload"], captured["payload"]
    # ...while the rest of the mapping form still saves exactly as before.
    assert captured["payload"]["display_name"] == "Renamed"


def test_profile_update_still_saves_the_mapping_arrays(client):  # noqa: F811
    """Surgical means surgical: removing the voice assembly must not disturb
    the checkbox-array semantics (`_mapping_form=1` → absent means empty)."""
    captured = {}

    def _update(*, profile_id, payload, actor_user_id):
        captured["payload"] = payload
        return {}

    with patch("admin.services.profiles.update_profile_row", _update):
        r = client.post(
            "/admin/profiles/p-1",
            data={"_mapping_form": "1", "tool_ids": ["t-1", "t-2"]},
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert captured["payload"]["tool_ids"] == ["t-1", "t-2"]
    assert captured["payload"]["data_source_ids"] == []
    assert captured["payload"]["mcp_server_ids"] == []
