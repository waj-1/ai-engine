"""Smoke tests for the New-from-template portal gallery
(admin/templates_handlers.py).

Covers:
  GET /admin/templates
    - 200, renders every catalog entry (monkeypatched list_templates)
  POST /admin/agents/from-template
    - happy path: 303-redirects to /admin/agents?profile_id=<new_root>
    - actor_user_id is threaded from the superuser session, not the form
    - TemplateNotFoundError / ProfileNotFoundError: re-renders the gallery
      with the error message, HTTP 400

Uses the shared `client` fixture (tests/admin/conftest.py) — superuser bypass
+ null DB stub. `list_templates` / `use_template` are monkeypatched directly
on `admin.templates_handlers` (the names the handler module actually calls),
so neither route makes a real DB call.
"""
from __future__ import annotations

import sys
from pathlib import Path

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import templates_handlers
from admin.services.profiles import ProfileNotFoundError
from admin.services.templates import TemplateNotFoundError

# Matches _fake_smoke_superuser() in conftest.py.
_SMOKE_USER_ID = "00000000-0000-0000-0000-000000000001"

_CATALOG = [
    {
        "template_id": "tmpl-single-1",
        "slug": "business_default",
        "display_name": "Business Concierge Mold",
        "agent_kind": "business",
        "description": "Single-agent business mold.",
        "is_composite": False,
    },
    {
        "template_id": "tmpl-composite-1",
        "slug": "sales_stack",
        "display_name": "Sales Stack Mold",
        "agent_kind": "sales",
        "description": "Composite mold with a sub-agent.",
        "is_composite": True,
    },
]


# ---------------------------------------------------------------------------
# GET /admin/templates
# ---------------------------------------------------------------------------


def test_gallery_lists_seeded_molds(client, monkeypatch):
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: _CATALOG)

    resp = client.get("/admin/templates")

    assert resp.status_code == 200
    assert "Business Concierge Mold" in resp.text
    assert "Sales Stack Mold" in resp.text


def test_gallery_empty_catalog_renders_without_error(client, monkeypatch):
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: [])

    resp = client.get("/admin/templates")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/agents/from-template — happy path
# ---------------------------------------------------------------------------


def test_use_template_redirects_to_new_profile(client, monkeypatch):
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: _CATALOG)
    captured = {}

    def _fake_use_template(**kwargs):
        captured.update(kwargs)
        return {
            "root": "new-profile-id",
            "created": ["new-profile-id"],
            "as_subagent_of": None,
        }

    monkeypatch.setattr(templates_handlers, "use_template", _fake_use_template)

    resp = client.post(
        "/admin/agents/from-template",
        data={"template_id": "tmpl-single-1", "display_name": "My New Agent"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/agents?profile_id=new-profile-id"
    # actor_user_id comes from the superuser session, never trusted from the form.
    assert captured["template_id"] == "tmpl-single-1"
    assert captured["display_name"] == "My New Agent"
    assert captured["actor_user_id"] == _SMOKE_USER_ID


def test_use_template_passes_as_subagent_of(client, monkeypatch):
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: _CATALOG)
    captured = {}

    def _fake_use_template(**kwargs):
        captured.update(kwargs)
        return {"root": "new-sub-id", "created": ["new-sub-id"], "as_subagent_of": "parent-1"}

    monkeypatch.setattr(templates_handlers, "use_template", _fake_use_template)

    resp = client.post(
        "/admin/agents/from-template",
        data={
            "template_id": "tmpl-single-1",
            "display_name": "My Sub Agent",
            "as_subagent_of": "parent-1",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert captured["as_subagent_of"] == "parent-1"


def test_use_template_passes_explicit_slug(client, monkeypatch):
    """The gallery form's optional slug reaches use_template verbatim, so an
    operator can create a delegation specialist with a stable ask_<slug>
    tool name entirely through the UI (no MCP required)."""
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: _CATALOG)
    captured = {}

    def _fake_use_template(**kwargs):
        captured.update(kwargs)
        return {"root": "new-id", "created": ["new-id"], "as_subagent_of": None}

    monkeypatch.setattr(templates_handlers, "use_template", _fake_use_template)

    resp = client.post(
        "/admin/agents/from-template",
        data={
            "template_id": "tmpl-single-1",
            "display_name": "Reporting specialist",
            "slug": "business_reporting",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert captured["slug"] == "business_reporting"


def test_use_template_blank_slug_passes_none(client, monkeypatch):
    """A blank slug field becomes None (fork keeps its uniqueness suffix)."""
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: _CATALOG)
    captured = {}

    def _fake_use_template(**kwargs):
        captured.update(kwargs)
        return {"root": "new-id", "created": ["new-id"], "as_subagent_of": None}

    monkeypatch.setattr(templates_handlers, "use_template", _fake_use_template)

    client.post(
        "/admin/agents/from-template",
        data={"template_id": "tmpl-single-1", "display_name": "Plain Draft", "slug": ""},
        follow_redirects=False,
    )

    assert captured["slug"] is None


# ---------------------------------------------------------------------------
# POST /admin/agents/from-template — error paths
# ---------------------------------------------------------------------------


def test_use_template_not_found_rerenders_gallery_with_error(client, monkeypatch):
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: _CATALOG)

    def _raise(**kwargs):
        raise TemplateNotFoundError("not a template: nope")

    monkeypatch.setattr(templates_handlers, "use_template", _raise)

    resp = client.post(
        "/admin/agents/from-template",
        data={"template_id": "nope", "display_name": "X"},
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "not a template" in resp.text.lower()
    # Gallery still renders behind the error banner.
    assert "Business Concierge Mold" in resp.text


def test_use_template_missing_parent_rerenders_gallery_with_error(client, monkeypatch):
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: _CATALOG)

    def _raise(**kwargs):
        raise ProfileNotFoundError("as_subagent_of parent not found: ghost")

    monkeypatch.setattr(templates_handlers, "use_template", _raise)

    resp = client.post(
        "/admin/agents/from-template",
        data={
            "template_id": "tmpl-single-1",
            "display_name": "X",
            "as_subagent_of": "ghost",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "parent not found" in resp.text.lower()


def test_new_agent_window_names_the_model_the_agent_will_get(client, monkeypatch):
    label = "Automatic (first usable model): now Local model (Gemini Flash can't run here: GOOGLE_API_KEY is not set)"
    catalog = [{**_CATALOG[0], "default_model": {"auto": True, "label": label}}]
    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: catalog)
    monkeypatch.setattr(templates_handlers, "_model_options",
                        lambda: [{"model_id": "m", "name": "Local model", "model_string": "qwen"}])
    resp = client.get("/admin/templates")
    assert resp.status_code == 200
    from html import unescape
    assert f'data-template-model="{label}"' in unescape(resp.text)
    assert "data-default-model-option" in resp.text
