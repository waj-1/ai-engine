"""`db_snapshot` carries the user-defined agent categories.

`agent_profiles.agent_type_id` is a real foreign key into `agent_types`. The
export/restore workflow (CLAUDE.md "Database snapshot") therefore has to know
the table, or two things go wrong on the far side of a `supabase db reset`:

  * the categories are not in seed.json at all, so they are gone, and every
    profile that was in one fails the foreign key on restore: `_write_row`
    swallows that, and the profile is silently MISSING;
  * a category that already exists in the target under a different id (made
    again by hand before the restore) is duplicated or, with the unique slug,
    refused, and again takes its profiles down with it.

Both are pinned below on the small Postgres from
test_db_snapshot_reference_remap.py (it enforces foreign keys and unique
constraints), extended with this table's own.
"""
from __future__ import annotations

import pytest

from admin import db_snapshot
from tests.admin import test_db_snapshot_reference_remap as pg


@pytest.fixture(autouse=True)
def _agent_types_constraints(monkeypatch):
    """What 20261123040000_agent_types.sql declares. Hardcoded, like the
    constraints in the shared fake: they are the DATABASE's."""
    monkeypatch.setitem(pg._FKS, "agent_profiles", pg._FKS["agent_profiles"] + [
        ("agent_type_id", "agent_types", "agent_type_id")])
    monkeypatch.setitem(pg._UNIQUE, "agent_types", ("slug",))
    monkeypatch.setitem(pg._PK, "agent_types", "agent_type_id")


def _profile(profile_id, slug, agent_type_id):
    return {"profile_id": profile_id, "agent_kind": "custom", "slug": slug,
            "environment": "dev", "tool_ids": [], "data_source_ids": [],
            "mcp_server_ids": [], "downstream_profile_ids": [],
            "prompt_version_ids": {}, "voice_config": {},
            "agent_type_id": agent_type_id}


def _source():
    """The database before the reset: two categories, three agents."""
    return pg._FakeDB({
        "agent_types": [
            {"agent_type_id": "old-support", "slug": "support", "display_name": "Support",
             "description": "Answers customers", "color": "teal", "sort_order": 0},
            {"agent_type_id": "old-sales", "slug": "sales", "display_name": "Sales",
             "description": None, "color": "amber", "sort_order": 1},
        ],
        "agent_profiles": [
            _profile("old-desk", "front-desk", "old-support"),
            _profile("old-deals", "deals", "old-sales"),
            _profile("old-notes", "notes", None),
        ],
    })


def _use(monkeypatch, fake):
    monkeypatch.setattr(db_snapshot, "get_service_client", lambda: pg._FakeClient(fake))
    return fake


def test_export_includes_the_categories_before_the_profiles(monkeypatch):
    _use(monkeypatch, _source())
    payload = db_snapshot.export()

    assert [t["slug"] for t in payload["tables"]["agent_types"]] == ["support", "sales"]
    # Parents first: the profile's foreign key must resolve on insert.
    order = db_snapshot._TABLES
    assert order.index("agent_types") < order.index("agent_profiles")


def test_round_trip_onto_an_empty_database(monkeypatch):
    """export -> reset -> restore: categories, colours, order and every
    agent's category come back."""
    _use(monkeypatch, _source())
    payload = db_snapshot.export()

    target = _use(monkeypatch, pg._FakeDB({}))
    counts = db_snapshot.restore(payload)

    assert counts["agent_types"] == 2
    assert counts["agent_profiles"] == 3, "no profile swallowed by a foreign key"
    support = target.one("agent_types", slug="support")
    assert (support["display_name"], support["color"], support["sort_order"]) == (
        "Support", "teal", 0)
    assert target.one("agent_profiles", slug="front-desk")["agent_type_id"] == \
        support["agent_type_id"]
    assert target.one("agent_profiles", slug="deals")["agent_type_id"] == \
        target.one("agent_types", slug="sales")["agent_type_id"]
    assert target.one("agent_profiles", slug="notes")["agent_type_id"] is None


def test_a_category_that_already_exists_in_the_target_is_matched_by_slug(monkeypatch):
    """Someone re-created "Support" after the reset, so it has a NEW id and a
    new name. The restore must point the profile at that row (not insert a
    second one, which the unique slug refuses), and restore the exported
    name and colour onto it."""
    _use(monkeypatch, _source())
    payload = db_snapshot.export()

    target = _use(monkeypatch, pg._FakeDB({
        "agent_types": [{"agent_type_id": "new-support", "slug": "support",
                         "display_name": "Helpdesk", "color": "slate", "sort_order": 9}],
    }))
    counts = db_snapshot.restore(payload)

    assert counts["agent_profiles"] == 3
    assert len(target.rows("agent_types")) == 2                       # support matched, sales added
    support = target.one("agent_types", slug="support")
    assert support["agent_type_id"] == "new-support"                  # the target's id is kept
    assert (support["display_name"], support["color"]) == ("Support", "teal")
    assert target.one("agent_profiles", slug="front-desk")["agent_type_id"] == "new-support"


def test_restoring_twice_changes_nothing(monkeypatch):
    _use(monkeypatch, _source())
    payload = db_snapshot.export()
    target = _use(monkeypatch, pg._FakeDB({}))

    first = db_snapshot.restore(payload)
    snapshot = {t: [dict(r) for r in target.rows(t)] for t in ("agent_types", "agent_profiles")}
    second = db_snapshot.restore(payload)

    assert first == second
    assert {t: target.rows(t) for t in snapshot} == snapshot


def test_an_export_from_before_categories_still_restores(monkeypatch):
    """A seed.json made before this table existed has no `agent_types` key
    and no `agent_type_id` on its profiles."""
    target = _use(monkeypatch, pg._FakeDB({}))
    old = {"tables": {"agent_profiles": [
        {k: v for k, v in _profile("old-desk", "front-desk", None).items()
         if k != "agent_type_id"}]}}

    counts = db_snapshot.restore(old)

    assert counts["agent_profiles"] == 1 and counts["agent_types"] == 0
    assert target.one("agent_profiles", slug="front-desk")["profile_id"] == "old-desk"
