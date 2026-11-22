"""User-defined agent categories: the rules in admin/services/agent_types.py.

A category is data. These tests pin what that means: a fresh install has
none, the slug is a stable handle, deleting a category leaves its agents
uncategorised, the category follows the agent across environments without a
promote, and it never enters a promote snapshot.
"""
from __future__ import annotations

import uuid

import pytest

from admin.services import agent_types as svc
from admin.services import profiles
from tests.admin_mcp.conftest import FakeClient as _BareClient, FakeTable

ACTOR = "00000000-0000-0000-0000-000000000001"

# Primary keys Postgres fills in by default; the shared fake does not.
_DEFAULT_PKS = {"agent_profiles": "profile_id", "agent_profile_versions": "version_id"}


class _Table(FakeTable):
    def __init__(self, data, pk=None):
        super().__init__(data)
        self._pk = pk

    def insert(self, payload):
        if self._pk and isinstance(payload, dict) and not payload.get(self._pk):
            payload = {**payload, self._pk: str(uuid.uuid4())}
        return super().insert(payload)


class FakeClient(_BareClient):
    """The shared postgrest fake, plus database-generated primary keys."""

    def __init__(self, tables):
        self.tables = {name: _Table(data, _DEFAULT_PKS.get(name))
                       for name, data in tables.items()}

    def table(self, name):
        return self.tables.setdefault(name, _Table([], _DEFAULT_PKS.get(name)))


@pytest.fixture
def db(monkeypatch):
    """An empty engine database (no categories, as on a fresh install) with
    two agents; the audit log and the profile-loader cache are stubbed."""
    client = FakeClient({
        "agent_types": [],
        "agent_profiles": [
            {"profile_id": "p-desk", "agent_kind": "custom", "slug": "front-desk",
             "environment": "staging", "status": "active", "display_name": "Front desk"},
            {"profile_id": "p-desk-live", "agent_kind": "custom", "slug": "front-desk",
             "environment": "prod", "status": "active", "display_name": "Front desk"},
            {"profile_id": "p-notes", "agent_kind": "custom", "slug": "notes",
             "environment": "staging", "status": "draft", "display_name": "Notes"},
            {"profile_id": "p-mold", "agent_kind": "custom", "slug": "mold:blank",
             "environment": "staging", "status": "template", "display_name": "Blank"},
        ],
    })
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    monkeypatch.setattr(svc, "record_change", lambda **kw: None)
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    monkeypatch.setattr(profiles, "invalidate_profile_loader", lambda *a: None)
    return client


def _profile(db, profile_id):
    return next(r for r in db.table("agent_profiles")._data if r["profile_id"] == profile_id)


def _make(db, name, **kw):
    return svc.create_type(display_name=name, actor_user_id=ACTOR, client=db, **kw)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_a_fresh_install_has_no_categories(db):
    assert svc.list_types(db) == []
    assert svc.types_by_id(db) == {}


def test_create_makes_the_slug_from_the_name_and_appends_to_the_order(db):
    support = _make(db, "  Support  ")
    tools = _make(db, "Internal tools", color="violet", description="Staff only")

    assert (support["slug"], support["display_name"], support["color"]) == (
        "support", "Support", svc.DEFAULT_COLOR)
    assert (tools["slug"], tools["color"], tools["description"]) == (
        "internal-tools", "violet", "Staff only")
    assert [t["slug"] for t in svc.list_types(db)] == ["support", "internal-tools"]
    assert [t["sort_order"] for t in svc.list_types(db)] == [0, 1]
    assert support["created_by"] == ACTOR
    assert support["agent_type_id"] != tools["agent_type_id"]


def test_a_name_with_no_latin_letters_still_gets_a_slug(db):
    assert _make(db, "دعم")["slug"] == "category-1"
    assert _make(db, "مبيعات")["slug"] == "category-2"
    assert _make(db, "24/7")["slug"] == "c-24-7"


@pytest.mark.parametrize("kwargs, message", [
    ({"display_name": "   "}, "needs a name"),
    ({"display_name": "x" * 61}, "too long"),
    ({"display_name": "Sales", "color": "chartreuse"}, "colour must be one of"),
    ({"display_name": "Sales", "slug": "Not A Slug"}, "slug must start with a letter"),
    ({"display_name": "Sales", "description": "d" * 281}, "description is too long"),
    ({"display_name": "Sales", "sort_order": "first"}, "whole number"),
])
def test_create_refuses_bad_input(db, kwargs, message):
    with pytest.raises(svc.AgentTypeError, match=message):
        svc.create_type(actor_user_id=ACTOR, client=db, **kwargs)
    assert svc.list_types(db) == []


def test_create_refuses_a_duplicate_slug_or_name(db):
    _make(db, "Support")
    with pytest.raises(svc.AgentTypeError, match="already exists"):
        _make(db, "support")                       # same name, other case
    with pytest.raises(svc.AgentTypeError, match="already exists"):
        _make(db, "Helpdesk", slug="support")      # same slug
    assert len(svc.list_types(db)) == 1


def test_rename_and_recolour_keep_the_slug_and_the_id(db):
    support = _make(db, "Support")
    after = svc.update_type("support", {"display_name": "Customer care", "color": "teal"},
                            actor_user_id=ACTOR, client=db)

    assert after["display_name"] == "Customer care" and after["color"] == "teal"
    assert after["slug"] == "support"
    assert after["agent_type_id"] == support["agent_type_id"]
    assert svc.get_type("support", db)["display_name"] == "Customer care"
    # ... and by id too.
    assert svc.get_type(support["agent_type_id"], db)["slug"] == "support"


def test_update_refuses_the_slug_an_unknown_field_and_a_taken_name(db):
    _make(db, "Support")
    _make(db, "Sales")
    with pytest.raises(svc.AgentTypeError, match="slug is fixed"):
        svc.update_type("support", {"slug": "care"}, actor_user_id=ACTOR, client=db)
    with pytest.raises(svc.AgentTypeError, match="already exists"):
        svc.update_type("support", {"display_name": "SALES"}, actor_user_id=ACTOR, client=db)
    with pytest.raises(svc.AgentTypeError, match="nothing to change"):
        svc.update_type("support", {}, actor_user_id=ACTOR, client=db)
    with pytest.raises(svc.AgentTypeNotFoundError):
        svc.update_type("nope", {"color": "red"}, actor_user_id=ACTOR, client=db)


def test_reorder_and_move(db):
    for name in ("Support", "Sales", "Internal tools"):
        _make(db, name)

    svc.move_type("internal-tools", "up", actor_user_id=ACTOR, client=db)
    assert [t["slug"] for t in svc.list_types(db)] == ["support", "internal-tools", "sales"]

    # Moving the first one up is a no-op, not an error.
    svc.move_type("support", "up", actor_user_id=ACTOR, client=db)
    assert [t["slug"] for t in svc.list_types(db)] == ["support", "internal-tools", "sales"]

    # A partial order: the ones named come first, the rest keep their order.
    svc.reorder_types(["sales"], actor_user_id=ACTOR, client=db)
    assert [t["slug"] for t in svc.list_types(db)] == ["sales", "support", "internal-tools"]
    assert [t["sort_order"] for t in svc.list_types(db)] == [0, 1, 2]

    with pytest.raises(svc.AgentTypeError, match="up.*down"):
        svc.move_type("sales", "sideways", actor_user_id=ACTOR, client=db)


# --------------------------------------------------------------------------- #
# The profile's reference
# --------------------------------------------------------------------------- #
def test_putting_an_agent_in_a_category_moves_every_environments_row(db):
    """The category is about the agent, so its promoted sibling follows at
    once: no re-promote, and the staging and live lists cannot disagree."""
    support = _make(db, "Support")

    svc.set_profile_type("p-desk", "support", actor_user_id=ACTOR)

    assert _profile(db, "p-desk")["agent_type_id"] == support["agent_type_id"]
    assert _profile(db, "p-desk-live")["agent_type_id"] == support["agent_type_id"]
    assert _profile(db, "p-notes").get("agent_type_id") is None     # another agent

    svc.set_profile_type("p-desk", "", actor_user_id=ACTOR)         # take it out again
    assert _profile(db, "p-desk")["agent_type_id"] is None
    assert _profile(db, "p-desk-live")["agent_type_id"] is None


def test_an_agent_cannot_be_put_in_a_category_that_does_not_exist(db):
    with pytest.raises(svc.AgentTypeNotFoundError):
        svc.set_profile_type("p-desk", "no-such-category", actor_user_id=ACTOR)
    # The id path (the portal's picker posts an id) is checked too.
    with pytest.raises(ValueError, match="no category with id"):
        profiles.update_profile_row(
            profile_id="p-desk",
            payload={"agent_type_id": "11111111-1111-1111-1111-111111111111"},
            actor_user_id=ACTOR)
    assert _profile(db, "p-desk").get("agent_type_id") is None


def test_a_template_cannot_carry_a_category(db):
    _make(db, "Support")
    with pytest.raises(profiles.TemplateProtectedError):
        svc.set_profile_type("p-mold", "support", actor_user_id=ACTOR)


def test_counts_are_agents_not_rows_and_skip_templates(db):
    support = _make(db, "Support")
    sales = _make(db, "Sales")
    svc.set_profile_type("p-desk", "support", actor_user_id=ACTOR)   # two rows, one agent
    svc.set_profile_type("p-notes", "support", actor_user_id=ACTOR)
    _profile(db, "p-mold")["agent_type_id"] = sales["agent_type_id"]  # must not count

    by_slug = {t["slug"]: t["agent_count"] for t in svc.list_types(db)}
    assert by_slug == {"support": 2, "sales": 0}
    # A scoped build counts only the kinds its Agents list shows.
    assert svc.agent_counts(db, kinds={"other"}) == {}
    assert svc.agent_counts(db, kinds={"custom"}) == {support["agent_type_id"]: 2}


def test_deleting_a_category_leaves_its_agents_uncategorised(db):
    """No agent is deleted, and the outcome does not lean on the foreign key
    (this fake has none): the column is cleared before the row goes."""
    _make(db, "Support")
    _make(db, "Sales")
    svc.set_profile_type("p-desk", "support", actor_user_id=ACTOR)
    svc.set_profile_type("p-notes", "sales", actor_user_id=ACTOR)
    before = len(db.table("agent_profiles")._data)

    out = svc.delete_type("support", actor_user_id=ACTOR, client=db)

    assert out["agents_uncategorised"] == 1
    assert out["deleted"]["slug"] == "support"
    assert [t["slug"] for t in svc.list_types(db)] == ["sales"]
    assert len(db.table("agent_profiles")._data) == before            # nobody deleted
    assert _profile(db, "p-desk")["agent_type_id"] is None
    assert _profile(db, "p-desk-live")["agent_type_id"] is None
    assert _profile(db, "p-notes")["agent_type_id"] is not None       # other category kept
    with pytest.raises(svc.AgentTypeNotFoundError):
        svc.delete_type("support", actor_user_id=ACTOR, client=db)


def test_a_draft_copy_stays_in_the_originals_category(db):
    support = _make(db, "Support")
    svc.set_profile_type("p-notes", "support", actor_user_id=ACTOR)

    copy = profiles.fork_profile_row(profile_id="p-notes", actor_user_id=ACTOR)

    assert copy["agent_type_id"] == support["agent_type_id"]
    assert "agent_type_id" in profiles._FORK_COPY_FIELDS   # copied on purpose, no warning


# --------------------------------------------------------------------------- #
# A category applies to any kind of agent (the plugin seam)
# --------------------------------------------------------------------------- #
def test_a_product_kind_can_be_categorised_or_left_alone(db, product_build):
    """A build with a product plugin has code-backed kinds of its own. A
    category is not tied to `custom`: such an agent can be filed or left
    uncategorised, and its kind (which code runs it) is untouched."""
    from tests.conftest import SAMPLE_AGENT, SAMPLE_OTHER

    db.table("agent_profiles")._data.extend([
        {"profile_id": "p-sample", "agent_kind": SAMPLE_AGENT, "slug": "default",
         "environment": "staging", "status": "active", "is_active": True},
        {"profile_id": "p-other", "agent_kind": SAMPLE_OTHER, "slug": "default",
         "environment": "staging", "status": "active", "is_active": True},
    ])
    support = _make(db, "Support")

    after = svc.set_profile_type("p-sample", "support", actor_user_id=ACTOR)

    assert after["agent_kind"] == SAMPLE_AGENT and after["status"] == "active"
    assert _profile(db, "p-sample")["agent_type_id"] == support["agent_type_id"]
    assert _profile(db, "p-other").get("agent_type_id") is None
    assert svc.agent_counts(db) == {support["agent_type_id"]: 1}


# --------------------------------------------------------------------------- #
# Promote: on the row, never in the snapshot
# --------------------------------------------------------------------------- #
def test_the_category_is_not_frozen_into_a_promote_snapshot(db):
    """Organisational metadata: a rename or a move must not wait for a
    re-promote, and restoring an old version must not move the agent."""
    from admin.services import profile_versions
    from runtime import compiler

    support = _make(db, "Support")
    svc.set_profile_type("p-desk", "support", actor_user_id=ACTOR)

    version_id = compiler.compile_profile_snapshot(
        profile_id="p-desk", environment="prod", client=db)
    stored = next(r for r in db.table("agent_profile_versions")._data
                  if r["version_id"] == version_id)

    assert "agent_type_id" not in stored["snapshot"]
    assert support["agent_type_id"] not in repr(stored["snapshot"])
    assert stored["snapshot"]["slug"] == "front-desk"      # it is a real snapshot
    assert "agent_type_id" not in compiler._SHALLOW_FIELDS
    # Restore copies `_CONFIG_FIELDS` only, so it cannot move the agent back.
    assert "agent_type_id" not in profile_versions._CONFIG_FIELDS


def test_promote_in_one_database_keeps_the_id(db):
    support = _make(db, "Support")
    assert svc.carry_to(db, db, support["agent_type_id"]) == support["agent_type_id"]
    assert svc.carry_to(db, db, None) is None


def test_promote_across_two_databases_matches_the_category_by_slug(db):
    """One database per environment: the source's id means nothing in the
    target. Written verbatim it fails the target's foreign key and the whole
    promote with it."""
    support = _make(db, "Support", color="teal")
    target = FakeClient({"agent_types": [
        {"agent_type_id": "target-support", "slug": "support", "display_name": "Support"}]})

    assert svc.carry_to(db, target, support["agent_type_id"]) == "target-support"
    assert len(target.table("agent_types")._data) == 1               # nothing duplicated


def test_promote_across_two_databases_creates_a_missing_category(db):
    sales = _make(db, "Sales", color="amber", description="Outbound")
    target = FakeClient({"agent_types": []})

    new_id = svc.carry_to(db, target, sales["agent_type_id"], actor_user_id=ACTOR)

    made = target.table("agent_types")._data
    assert [(r["slug"], r["display_name"], r["color"], r["description"]) for r in made] == [
        ("sales", "Sales", "amber", "Outbound")]
    assert new_id == made[0]["agent_type_id"] != sales["agent_type_id"]


def test_a_category_never_fails_a_promote(db):
    class Broken:
        def table(self, name):
            raise RuntimeError("target database unreachable")

    support = _make(db, "Support")
    assert svc.carry_to(db, Broken(), support["agent_type_id"]) is None
    assert svc.carry_to(db, FakeClient({}), "id-that-is-gone") is None


def test_promote_hands_the_target_database_its_own_category_id(db, monkeypatch):
    """End to end through promote(): the row written to the target carries
    the TARGET's category id, and the snapshot carries none."""
    from admin.services import promotion

    support = _make(db, "Support")
    svc.set_profile_type("p-notes", "support", actor_user_id=ACTOR)
    target = FakeClient({
        "agent_types": [{"agent_type_id": "target-support", "slug": "support",
                         "display_name": "Support"}],
        "agent_profiles": [],
    })
    monkeypatch.setattr("core.auth.get_service_client_for_env", lambda env: target)
    monkeypatch.setattr(promotion, "record_change", lambda **kw: None)
    monkeypatch.setattr(promotion, "_latest_eval_for_profile", lambda *a, **k: None)
    monkeypatch.setattr(promotion, "_prompt_mandated_unbound_tools", lambda *a, **k: [])
    monkeypatch.setattr("core.agent_profile.AgentProfileLoader.invalidate",
                        classmethod(lambda cls, *a, **k: None))

    promotion.promote(profile_id="p-notes", target_env="prod", force=False,
                      actor_user_id=ACTOR, actor_email="admin@example.test")

    promoted = target.table("agent_profiles")._data
    assert len(promoted) == 1
    assert promoted[0]["agent_type_id"] == "target-support"
    assert promoted[0]["agent_type_id"] != support["agent_type_id"]
    snapshot = target.table("agent_profile_versions")._data[0]["snapshot"]
    assert "agent_type_id" not in snapshot
