"""Agents › Categories in the portal: the screen, the pickers and the list.

Covers /admin/agent-types (create, rename, recolour, reorder, delete with its
"how many agents" warning), the category picker in the New agent wizard and
on an agent's Settings tab, and the chip and filter on the Agents list. The
rules themselves are pinned in test_agent_types_service.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from admin import nav
from admin.screens import agents_present as P
from admin.screens import agents_views as V
from admin.services import agent_types as svc
from admin.services import profiles
from admin.services import templates as tpl
from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient
from tests.admin_mcp.conftest import FakeClient

SUPPORT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SALES = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EMPTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _tables():
    return {
        "agent_types": [
            {"agent_type_id": SUPPORT, "slug": "support", "display_name": "Support",
             "description": "Answers customers", "color": "teal", "sort_order": 0},
            {"agent_type_id": SALES, "slug": "sales", "display_name": "Sales",
             "description": None, "color": "amber", "sort_order": 1},
            {"agent_type_id": EMPTY, "slug": "internal-tools", "display_name": "Internal tools",
             "description": None, "color": "slate", "sort_order": 2},
        ],
        "agent_profiles": [
            {"profile_id": "p-desk", "agent_kind": "custom", "slug": "front-desk",
             "environment": "staging", "status": "active", "display_name": "Front desk",
             "agent_type_id": SUPPORT},
            {"profile_id": "p-night", "agent_kind": "custom", "slug": "night-desk",
             "environment": "staging", "status": "active", "display_name": "Night desk",
             "agent_type_id": SUPPORT},
            {"profile_id": "p-deals", "agent_kind": "custom", "slug": "deals",
             "environment": "staging", "status": "draft", "display_name": "Deals",
             "agent_type_id": SALES},
            {"profile_id": "p-notes", "agent_kind": "custom", "slug": "notes",
             "environment": "staging", "status": "draft", "display_name": "Notes",
             "agent_type_id": None},
            {"profile_id": "t-blank", "agent_kind": "custom", "slug": "mold:blank",
             "environment": "staging", "status": "template", "display_name": "Blank",
             "downstream_profile_ids": []},
        ],
    }


@pytest.fixture
def db(monkeypatch):
    fake = FakeClient(_tables())
    monkeypatch.setattr("core.auth.admin_service_client", lambda: fake)
    monkeypatch.setattr(svc, "record_change", lambda **kw: None)
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    monkeypatch.setattr(profiles, "invalidate_profile_loader", lambda *a: None)
    monkeypatch.setattr(tpl, "record_change", lambda **kw: None)
    return fake


def _flat(html: str) -> str:
    return " ".join(html.split())


def _query(resp):
    assert resp.status_code == 303, resp.text[:300]
    parts = urlsplit(resp.headers["location"])
    assert parts.path == "/admin/agent-types"
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def _types(db):
    return {r["slug"]: r for r in db.table("agent_types")._data}


def _profile(db, profile_id):
    return next(r for r in db.table("agent_profiles")._data if r["profile_id"] == profile_id)


# --------------------------------------------------------------------------- #
# Where it lives
# --------------------------------------------------------------------------- #
def test_the_screen_sits_under_agents():
    assert nav.section_for("/admin/agent-types") == "agents"
    assert nav.section_for("/admin/agent-types/x/update") == "agents"


@pytest.mark.parametrize("path", ["/admin/agents", "/admin/agents?tab=models",
                                  "/admin/templates", "/admin/agent-types"])
def test_every_agents_tab_strip_links_to_categories(client, db, path):  # noqa: F811
    with patch("admin.screens.agents_views.load_agents_list",
               return_value={"agent_rows": [], "agent_counts": {"all": 0, "templates": 0, "draft": 0},
                             "compare_options": [], "agent_categories": []}), \
         patch("admin.templates_handlers.list_templates", return_value=[]):
        r = client.get(path)
    assert r.status_code == 200
    assert 'href="/admin/agent-types?env=staging">Categories</a>' in _flat(r.text)


# --------------------------------------------------------------------------- #
# The Categories screen
# --------------------------------------------------------------------------- #
def test_a_fresh_install_shows_an_empty_screen(client, db):  # noqa: F811
    db.table("agent_types")._data.clear()
    r = client.get("/admin/agent-types")
    assert r.status_code == 200
    assert "No categories yet" in r.text
    assert "data-categories-table" not in r.text
    assert 'id="dlg-new-category"' in r.text


def test_the_screen_lists_categories_in_order_with_agent_counts(client, db):  # noqa: F811
    r = client.get("/admin/agent-types")
    assert r.status_code == 200
    text = _flat(r.text)
    assert text.index('data-category-row="support"') < text.index('data-category-row="sales"') \
        < text.index('data-category-row="internal-tools"')
    assert "chip-cat-teal" in text and "chip-cat-amber" in text
    # The count links to the Agents list filtered by that category.
    assert '<a href="/admin/agents?env=staging&category=support">2</a>' in text
    # Every palette colour is offered, in the new and the edit dialogs.
    for colour in svc.PALETTE:
        assert f'value="{colour}"' in text
    # It says plainly that a category does not change how an agent runs.
    assert "does not change what an agent says or does" in text


def test_the_delete_dialog_says_how_many_agents_are_affected(client, db):  # noqa: F811
    text = _flat(client.get("/admin/agent-types").text)

    def dialog(type_id):
        start = text.index(f'<dialog id="dlg-delete-{type_id}"')
        return text[start:text.index("</dialog>", start)]

    assert "<strong>2 agents</strong> are in this category" in dialog(SUPPORT)
    assert "They will not be deleted or changed" in dialog(SUPPORT)
    assert "<strong>1 agent</strong> is in this category" in dialog(SALES)
    assert "No agent is in this category" in dialog(EMPTY)


def test_create(client, db):  # noqa: F811
    r = client.post("/admin/agent-types", data={
        "display_name": "Back office", "description": "Staff helpers", "color": "violet"},
        follow_redirects=False)
    assert _query(r)["notice"] == "Added “Back office”."
    made = _types(db)["back-office"]
    assert (made["display_name"], made["color"], made["description"]) == (
        "Back office", "violet", "Staff helpers")
    assert made["sort_order"] == 3                         # after the three that exist
    assert made["created_by"] == "00000000-0000-0000-0000-000000000001"


def test_create_reports_a_refusal_and_writes_nothing(client, db):  # noqa: F811
    r = client.post("/admin/agent-types", data={"display_name": "Support"},
                    follow_redirects=False)
    assert "already exists" in _query(r)["error"]
    assert len(db.table("agent_types")._data) == 3
    page = client.get(r.headers["location"])
    assert "Could not save" in page.text and "already exists" in page.text


def test_rename_and_recolour(client, db):  # noqa: F811
    r = client.post(f"/admin/agent-types/{SUPPORT}/update", data={
        "display_name": "Customer care", "description": "", "color": "blue"},
        follow_redirects=False)
    assert _query(r)["notice"] == "Saved “Customer care”."
    row = _types(db)["support"]                            # the slug did not move
    assert (row["display_name"], row["color"], row["description"]) == (
        "Customer care", "blue", None)
    assert _profile(db, "p-desk")["agent_type_id"] == SUPPORT   # agents untouched


def test_reorder(client, db):  # noqa: F811
    r = client.post(f"/admin/agent-types/{EMPTY}/move", data={"direction": "up"},
                    follow_redirects=False)
    assert "error" not in _query(r)
    assert [t["slug"] for t in svc.list_types(db)] == ["support", "internal-tools", "sales"]


def test_delete_leaves_the_agents_uncategorised_and_says_so(client, db):  # noqa: F811
    r = client.post(f"/admin/agent-types/{SUPPORT}/delete", follow_redirects=False)
    assert _query(r)["notice"] == "Deleted “Support”. 2 agents are now uncategorised."
    assert "support" not in _types(db)
    assert _profile(db, "p-desk")["agent_type_id"] is None
    assert _profile(db, "p-night")["agent_type_id"] is None
    assert _profile(db, "p-deals")["agent_type_id"] == SALES
    assert len(db.table("agent_profiles")._data) == 5      # no agent deleted

    r = client.post(f"/admin/agent-types/{EMPTY}/delete", follow_redirects=False)
    assert _query(r)["notice"] == "Deleted “Internal tools”. No agent was in it."

    r = client.post(f"/admin/agent-types/{SUPPORT}/delete", follow_redirects=False)
    assert "no category" in _query(r)["error"]             # already gone


# --------------------------------------------------------------------------- #
# The Agents list: chip + filter
# --------------------------------------------------------------------------- #
def test_the_list_loader_labels_each_agent_with_its_category(db):
    with patch("admin.router.client_for", return_value=db):
        data = V.load_agents_list("staging")

    rows = {r["profile_id"]: r for r in data["agent_rows"]}
    assert rows["p-desk"]["category"]["slug"] == "support"
    assert rows["p-desk"]["category"]["color"] == "teal"
    assert rows["p-notes"]["category"] is None
    assert rows["t-blank"]["category"] is None
    assert [c["slug"] for c in data["agent_categories"]] == ["support", "sales", "internal-tools"]
    assert "support" in rows["p-desk"]["search"]            # searchable by category name


def test_the_list_survives_a_database_without_the_table(db):
    """An install that has not run the migration yet: no chips, no filter,
    and the list still renders."""
    class NoTable(FakeClient):
        def table(self, name):
            if name == "agent_types":
                raise RuntimeError('relation "agent_types" does not exist')
            return super().table(name)

    with patch("admin.router.client_for", return_value=NoTable(_tables())):
        data = V.load_agents_list("staging")
    assert data["agent_categories"] == []
    assert all(r["category"] is None for r in data["agent_rows"])


def _row(profile_id, name, category):
    return {"profile_id": profile_id, "name": name, "description": "",
            "status": P.status_info("active"), "type_key": "main", "type_label": "Main agent",
            "parent_name": None, "channels": [], "channel_keys": "", "model_name": "m",
            "can_use": "", "updated_rel": "", "updated_at": "", "updated_by_you": False,
            "children": [], "search": name.lower(), "family_search": name.lower(),
            "category": category}


def _list_payload(categories=True):
    support = {"slug": "support", "display_name": "Support", "color": "teal",
               "description": "Answers customers"}
    return {"agent_rows": [_row("p-desk", "Front desk", support if categories else None),
                           _row("p-notes", "Notes", None)],
            "agent_counts": {"all": 2, "templates": 0, "draft": 0}, "compare_options": [],
            "agent_categories": [support] if categories else []}


def test_the_list_shows_a_chip_and_a_category_filter(client):  # noqa: F811
    with patch("admin.screens.agents_views.load_agents_list", return_value=_list_payload()):
        r = client.get("/admin/agents")
    text = _flat(r.text)
    assert r.status_code == 200
    assert 'data-category="support"' in text and 'data-category=""' in text
    assert 'class="chip chip-cat chip-cat-teal"' in text and "data-category-chip" in text
    assert '<select data-filter-select="category" aria-label="Category">' in text
    assert '<option value="support" >Support</option>' in text
    assert '<option value="__none__" >Uncategorised</option>' in text
    # The filter itself runs in the browser, over the row attribute.
    js = (Path(__file__).resolve().parents[2] / "admin" / "static" / "agents.js").read_text(
        encoding="utf-8")
    assert 'r.getAttribute("data-category")' in js and '"__none__"' in js


def test_a_category_link_opens_the_list_already_filtered(client):  # noqa: F811
    with patch("admin.screens.agents_views.load_agents_list", return_value=_list_payload()):
        text = _flat(client.get("/admin/agents?category=support").text)
        assert '<option value="support" selected>Support</option>' in text
        text = _flat(client.get("/admin/agents?category=__none__").text)
        assert '<option value="__none__" selected>Uncategorised</option>' in text
        # A slug that is not a category is ignored, never echoed back.
        text = _flat(client.get('/admin/agents?category="><script>').text)
        assert " selected>" not in text.split('data-filter-select="category"')[1].split("</select>")[0]


def test_with_no_categories_the_list_has_no_filter_and_no_chip(client):  # noqa: F811
    with patch("admin.screens.agents_views.load_agents_list",
               return_value=_list_payload(categories=False)):
        text = client.get("/admin/agents").text
    assert 'data-filter-select="category"' not in text
    assert "data-category-chip" not in text


# --------------------------------------------------------------------------- #
# An agent's Settings tab
# --------------------------------------------------------------------------- #
def _detail(client, db, profile, tab="settings"):  # noqa: F811
    with patch("admin.screens.agents.load_agents_context",
               return_value={"active_profile": profile, "profiles": [profile],
                             "top_level_profiles": [profile]}), \
         patch("admin.screens.agents_views.promote_counterpart", return_value=None):
        return client.get(f"/admin/agents?profile_id={profile['profile_id']}&tab={tab}&env=staging")


def test_settings_offers_the_category_picker_with_the_current_one_selected(client, db):  # noqa: F811
    ap = {**_profile(db, "p-deals"), "endpoint_path": "/chat/stream"}
    r = _detail(client, db, ap)
    assert r.status_code == 200
    text = _flat(r.text)
    form = text[text.index('id="form-settings"'):text.index("</form>", text.index('id="form-settings"'))]
    assert '<select name="agent_type_id" >' in form
    assert '<option value="">— Uncategorised —</option>' in form
    assert f'<option value="{SALES}" selected>Sales</option>' in form
    assert f'<option value="{SUPPORT}" >Support</option>' in form
    assert "It does not change what the agent does" in form
    # ... and the header carries the chip, linking to the filtered list.
    assert 'href="/admin/agents?env=staging&category=sales" title="Category" data-category-chip' in text


def test_settings_with_no_categories_points_at_the_screen(client, db):  # noqa: F811
    db.table("agent_types")._data.clear()
    ap = {**_profile(db, "p-notes"), "endpoint_path": "/chat/stream"}
    text = _flat(_detail(client, db, ap).text)
    assert '<select name="agent_type_id" disabled>' in text    # a disabled field posts nothing
    assert "No categories yet." in text and ">Add one</a>" in text


def test_a_template_has_no_category_picker(client, db):  # noqa: F811
    ap = {**_profile(db, "t-blank"), "endpoint_path": "/chat/stream"}
    text = _detail(client, db, ap).text
    assert 'name="agent_type_id"' not in text


def test_saving_settings_sets_the_category(client, db):  # noqa: F811
    r = client.post("/admin/profiles/p-notes?env=staging",
                    data={"display_name": "Notes", "agent_type_id": SUPPORT},
                    follow_redirects=False)
    assert r.status_code == 302
    assert _profile(db, "p-notes")["agent_type_id"] == SUPPORT


def test_saving_settings_with_the_blank_choice_clears_the_category(client, db):  # noqa: F811
    """Blank is a choice here ("Uncategorised"), unlike the form's other
    fields, where blank means "leave it"."""
    r = client.post("/admin/profiles/p-deals?env=staging",
                    data={"display_name": "Deals", "agent_type_id": ""},
                    follow_redirects=False)
    assert r.status_code == 302
    assert _profile(db, "p-deals")["agent_type_id"] is None


def test_a_form_without_the_picker_leaves_the_category_alone(client, db):  # noqa: F811
    """The other tabs post to the same route without this field."""
    r = client.post("/admin/profiles/p-deals?env=staging",
                    data={"display_name": "Deals (renamed)"}, follow_redirects=False)
    assert r.status_code == 302
    assert _profile(db, "p-deals")["display_name"] == "Deals (renamed)"
    assert _profile(db, "p-deals")["agent_type_id"] == SALES


def test_saving_settings_with_a_deleted_category_is_refused(client, db):  # noqa: F811
    r = client.post("/admin/profiles/p-notes?env=staging",
                    data={"agent_type_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert _profile(db, "p-notes")["agent_type_id"] is None


# --------------------------------------------------------------------------- #
# The New agent wizard
# --------------------------------------------------------------------------- #
_CATALOG = [{"template_id": "t-blank", "slug": "mold:blank", "display_name": "Blank",
             "agent_kind": "", "description": "", "is_composite": False}]


def test_the_wizard_offers_the_categories(client, db):  # noqa: F811
    with patch("admin.templates_handlers.list_templates", return_value=_CATALOG), \
         patch("admin.templates_handlers.admin_service_client", return_value=db):
        text = _flat(client.get("/admin/templates?new=1").text)
    assert '<select name="agent_type_id">' in text
    assert f'<option value="{SUPPORT}">Support</option>' in text
    assert text.index(f'value="{SUPPORT}"') < text.index(f'value="{SALES}"')


def test_the_wizard_hides_the_picker_when_there_are_no_categories(client, db):  # noqa: F811
    db.table("agent_types")._data.clear()
    with patch("admin.templates_handlers.list_templates", return_value=_CATALOG), \
         patch("admin.templates_handlers.admin_service_client", return_value=db):
        text = client.get("/admin/templates?new=1").text
    assert 'name="agent_type_id"' not in text


def test_the_wizard_passes_the_category_to_the_service(client, db):  # noqa: F811
    seen = {}

    def fake_use_template(**kwargs):
        seen.update(kwargs)
        return {"root": "new-id", "created": ["new-id"], "as_subagent_of": None}

    with patch("admin.templates_handlers.use_template", fake_use_template):
        r = client.post("/admin/agents/from-template", data={
            "template_id": "t-blank", "display_name": "Help desk", "agent_type_id": SUPPORT},
            follow_redirects=False)
        assert r.status_code == 303 and seen["agent_type"] == SUPPORT
        client.post("/admin/agents/from-template", data={
            "template_id": "t-blank", "display_name": "Help desk"}, follow_redirects=False)
        assert seen["agent_type"] is None                  # left out = uncategorised


def test_a_new_agent_is_created_in_the_chosen_category(db):
    out = tpl.use_template(template_id="t-blank", display_name="Help desk",
                           agent_type="support", actor_user_id="u")
    made = _profile(db, out["root"])
    assert made["agent_type_id"] == SUPPORT and made["status"] == "draft"
    assert out["agent_type_id"] == SUPPORT
    # By id works too (what the portal's picker posts).
    out = tpl.use_template(template_id="t-blank", display_name="Deal desk",
                           agent_type=SALES, actor_user_id="u")
    assert _profile(db, out["root"])["agent_type_id"] == SALES
    # No category asked for: none set, and the template itself never gets one.
    out = tpl.use_template(template_id="t-blank", display_name="Plain", actor_user_id="u")
    assert not _profile(db, out["root"]).get("agent_type_id")
    assert not _profile(db, "t-blank").get("agent_type_id")


def test_an_unknown_category_refuses_the_new_agent_before_anything_is_written(db):
    before = len(db.table("agent_profiles")._data)
    with pytest.raises(svc.AgentTypeNotFoundError):
        tpl.use_template(template_id="t-blank", display_name="Help desk",
                         agent_type="no-such-category", actor_user_id="u")
    assert len(db.table("agent_profiles")._data) == before


def test_the_wizard_shows_the_refusal(client, db):  # noqa: F811
    with patch("admin.templates_handlers.list_templates", return_value=_CATALOG), \
         patch("admin.templates_handlers.admin_service_client", return_value=db):
        r = client.post("/admin/agents/from-template", data={
            "template_id": "t-blank", "display_name": "Help desk",
            "agent_type_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"}, follow_redirects=False)
    assert r.status_code == 400
    assert "Could not create the agent" in r.text and "no category" in r.text
