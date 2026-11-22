"""`db_snapshot restore` must converge after a `supabase db reset`.

CLAUDE.md documents export -> reset -> restore as THE way to keep portal edits
across a reset of the AI project. It did not work. The reset drops the database;
the boot seeders then re-create `agent_models`, `agent_prompts` /
`agent_prompt_versions`, `agent_tools`, `agent_data_sources` and
`agent_mcp_servers` with FRESH uuids, while seed.json's `agent_profiles` still
reference the OLD ones. Restore upserted them verbatim, so:

  * the real FK columns (`model_main_id`, `parent_profile_id`,
    `agent_prompt_versions.prompt_id`, `agent_profile_versions.profile_id`)
    raised, `_write_row` swallowed it, and the profile was simply GONE — three
    were lost this way on a local DB on 2026-08-17;
  * the loose uuid[] / jsonb columns (`tool_ids`, `data_source_ids`,
    `mcp_server_ids`, `prompt_version_ids`) have no FK to raise, so those rows
    landed looking healthy while pointing at rows that no longer existed.

The fake below is a small Postgres: it enforces the same foreign keys and unique
constraints the baseline migration declares, so a restore that gets a reference
wrong fails here the way it failed there. `test_without_the_remap_the_profiles_
are_lost` re-runs the whole thing with the remap disabled and pins the original
symptom — without it, every assertion in this file could be passing on a fake
that simply never checks anything.
"""
from types import SimpleNamespace

import pytest

from admin import db_snapshot

# --- a small Postgres ----------------------------------------------------

# Mirrors supabase/migrations/20260603120000_baseline_agent_schema.sql (plus
# 20260723130000_eval_screen_bakeoffs.sql and 20260724120000_profile_served_
# version.sql). Hardcoded rather than derived from db_snapshot._REFERENCES: the
# constraints are the DATABASE's, and a fake that read them from the module
# under test could not disagree with it.
_FKS = {
    "agent_profiles": [
        ("model_main_id", "agent_models", "model_id"),
        ("model_light_id", "agent_models", "model_id"),
        ("parent_profile_id", "agent_profiles", "profile_id"),
        ("served_version_id", "agent_profile_versions", "version_id"),
    ],
    "agent_prompt_versions": [("prompt_id", "agent_prompts", "prompt_id")],
    "agent_profile_versions": [("profile_id", "agent_profiles", "profile_id")],
    "agent_eval_suites": [
        ("evaluator_model_id", "agent_models", "model_id"),
        ("profile_id", "agent_profiles", "profile_id"),
    ],
}
_UNIQUE = {
    "agent_models": ("name", "environment"),
    "agent_prompts": ("agent_kind", "slug"),
    "agent_prompt_versions": ("prompt_id", "version_number"),
    "agent_tools": ("python_name",),
    "agent_data_sources": ("name",),
    "agent_mcp_servers": ("name",),
    "agent_profiles": ("agent_kind", "slug", "environment"),
    "agent_profile_versions": ("profile_id", "environment", "version_number"),
}
_PK = {
    "agent_models": "model_id",
    "agent_prompts": "prompt_id",
    "agent_prompt_versions": "prompt_version_id",
    "agent_tools": "tool_id",
    "agent_data_sources": "source_id",
    "agent_mcp_servers": "mcp_server_id",
    "agent_profiles": "profile_id",
    "agent_eval_suites": "suite_id",
    "agent_profile_versions": "version_id",
}


class IntegrityError(RuntimeError):
    """What psycopg raises and `_write_row` swallows into a warning."""


class _FakeDB:
    def __init__(self, tables):
        self.tables = {name: [dict(r) for r in rows] for name, rows in tables.items()}

    def rows(self, table):
        return self.tables.setdefault(table, [])

    def one(self, table, **match):
        found = [r for r in self.rows(table)
                 if all(str(r.get(k)) == str(v) for k, v in match.items())]
        assert len(found) == 1, f"{table} {match}: {len(found)} rows"
        return found[0]

    def _check(self, table, row, ignore=None):
        for column, parent, parent_pk in _FKS.get(table, []):
            value = row.get(column)
            if value in (None, "") or value is ignore:
                continue
            if not any(str(p.get(parent_pk)) == str(value) for p in self.rows(parent)):
                raise IntegrityError(
                    f'insert or update on table "{table}" violates foreign key '
                    f'constraint on {column}={value}')
        fields = _UNIQUE.get(table)
        if fields:
            key = tuple(str(row.get(f)) for f in fields)
            clashes = [r for r in self.rows(table)
                       if tuple(str(r.get(f)) for f in fields) == key
                       and r.get(_PK[table]) != row.get(_PK[table])]
            if clashes:
                raise IntegrityError(
                    f'duplicate key value violates unique constraint on '
                    f'{table}{fields}')

    def insert(self, table, row):
        self._check(table, row)
        self.rows(table).append(dict(row))

    def update(self, table, payload, filters):
        for row in self._matching(table, filters):
            merged = {**row, **payload}
            self._check(table, merged)
            row.update(payload)

    def _matching(self, table, filters):
        return [r for r in self.rows(table)
                if all(str(r.get(f)) == str(v) for f, v in filters)]


class _FakeQuery:
    def __init__(self, db, table):
        self._db, self._table = db, table
        self._op, self._payload, self._filters = "select", None, []

    def select(self, *_a, **_kw):
        self._op = "select"
        return self

    def insert(self, row):
        self._op, self._payload = "insert", row
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def eq(self, field, value):
        self._filters.append((field, value))
        return self

    def limit(self, *_a, **_kw):
        return self

    def execute(self):
        if self._op == "insert":
            self._db.insert(self._table, self._payload)
            return SimpleNamespace(data=[self._payload])
        if self._op == "update":
            self._db.update(self._table, self._payload, self._filters)
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=self._db._matching(self._table, self._filters))


class _FakeClient:
    def __init__(self, db):
        self._db = db

    def table(self, name):
        return _FakeQuery(self._db, name)


# --- the reset scenario --------------------------------------------------

def _target_after_reset():
    """The database `supabase db reset` + the boot seeders leave behind.

    Fresh uuids everywhere, and only what the seeders create: the parent
    profile, one model, one prompt at version 1. The portal-created specialist
    profile and prompt version 2 exist ONLY in seed.json.
    """
    return _FakeDB({
        "agent_models": [{"model_id": "new-model", "name": "orchard_main",
                          "environment": "dev", "temperature": 0.2}],
        "agent_prompts": [{"prompt_id": "new-prompt", "agent_kind": "orchard",
                           "slug": "system"}],
        "agent_prompt_versions": [{"prompt_version_id": "new-pv1",
                                   "prompt_id": "new-prompt",
                                   "version_number": 1, "content": "seeded"}],
        "agent_tools": [{"tool_id": "new-tool", "python_name": "list_merchants"}],
        "agent_data_sources": [{"source_id": "new-source", "name": "orchard_docs"}],
        "agent_mcp_servers": [{"mcp_server_id": "new-mcp", "name": "orchard_mcp"}],
        "agent_profiles": [{"profile_id": "new-parent", "agent_kind": "orchard",
                            "slug": "orchard_default", "environment": "dev",
                            "model_main_id": "new-model", "tool_ids": [],
                            "data_source_ids": [], "mcp_server_ids": [],
                            "downstream_profile_ids": [],
                            "prompt_version_ids": {}, "voice_config": {}}],
        "agent_eval_suites": [],
        "agent_profile_versions": [],
    })


def _seed_json():
    """The export taken BEFORE the reset. Every uuid here is stale.

    The child profile is listed before its parent on purpose: nothing orders an
    export, and a parent written second is one of the two ways the old restore
    lost a row.
    """
    return {"tables": {
        "agent_models": [{"model_id": "old-model", "name": "orchard_main",
                          "environment": "dev", "temperature": 0.4}],
        "agent_prompts": [{"prompt_id": "old-prompt", "agent_kind": "orchard",
                           "slug": "system"}],
        "agent_prompt_versions": [
            {"prompt_version_id": "old-pv1", "prompt_id": "old-prompt",
             "version_number": 1, "content": "seeded"},
            # The portal edit this whole workflow exists to preserve.
            {"prompt_version_id": "old-pv2", "prompt_id": "old-prompt",
             "version_number": 2, "content": "operator edit"},
        ],
        "agent_tools": [{"tool_id": "old-tool", "python_name": "list_merchants"}],
        "agent_data_sources": [{"source_id": "old-source", "name": "orchard_docs"}],
        "agent_mcp_servers": [{"mcp_server_id": "old-mcp", "name": "orchard_mcp"}],
        "agent_profiles": [
            {"profile_id": "old-child", "agent_kind": "orchard",
             "slug": "orchard_voice", "environment": "dev",
             "model_main_id": "old-model", "parent_profile_id": "old-parent",
             "tool_ids": [], "data_source_ids": [], "mcp_server_ids": [],
             "downstream_profile_ids": [], "prompt_version_ids": {},
             "voice_config": {}},
            {"profile_id": "old-parent", "agent_kind": "orchard",
             "slug": "orchard_default", "environment": "dev",
             "model_main_id": "old-model", "tool_ids": ["old-tool"],
             "data_source_ids": ["old-source"], "mcp_server_ids": ["old-mcp"],
             "downstream_profile_ids": ["old-child"],
             "prompt_version_ids": {"system": "old-pv2"},
             "served_version_id": "old-version", "voice_config": {}},
        ],
        "agent_eval_suites": [{"suite_id": "old-suite", "name": "orchard starter",
                               "agent_kind": "orchard",
                               "evaluator_model_id": "old-model",
                               "profile_id": "old-parent"}],
        "agent_profile_versions": [{"version_id": "old-version",
                                    "profile_id": "old-parent",
                                    "environment": "dev", "version_number": 3,
                                    "snapshot": {"agent_kind": "orchard"}}],
    }}


@pytest.fixture
def db(monkeypatch):
    fake = _target_after_reset()
    monkeypatch.setattr(db_snapshot, "get_service_client",
                        lambda: _FakeClient(fake))
    return fake


def test_a_stale_seed_json_restores_onto_a_freshly_reset_database(db):
    """The headline: every reference lands on a row that exists."""
    counts = db_snapshot.restore(_seed_json())

    assert counts["agent_profiles"] == 2, "both profiles written, none swallowed"

    parent = db.one("agent_profiles", slug="orchard_default")
    assert parent["profile_id"] == "new-parent", (
        "the TARGET's id survives — it is what every remapped reference now "
        "points at, so an upsert must never write the export's id over it")
    assert parent["model_main_id"] == "new-model"
    assert parent["tool_ids"] == ["new-tool"]
    assert parent["data_source_ids"] == ["new-source"]
    assert parent["mcp_server_ids"] == ["new-mcp"]

    child = db.one("agent_profiles", slug="orchard_voice")
    assert child["parent_profile_id"] == "new-parent"
    assert parent["downstream_profile_ids"] == [child["profile_id"]], (
        "the child was inserted under its exported id, so the parent's loose "
        "reference to it is still correct")


def test_a_portal_created_prompt_version_lands_under_the_reseeded_prompt(db):
    """`prompt_id` is a real FK: unmapped, this insert raised and the operator's
    prompt edit — the reason the workflow exists — was silently dropped."""
    db_snapshot.restore(_seed_json())

    versions = db.rows("agent_prompt_versions")
    assert len(versions) == 2, "v1 updated in place, v2 inserted — no duplicate v1"
    v2 = db.one("agent_prompt_versions", version_number=2)
    assert v2["prompt_id"] == "new-prompt"
    assert v2["content"] == "operator edit"
    assert db.one("agent_prompt_versions", version_number=1)["prompt_version_id"] \
        == "new-pv1"


def test_a_pin_at_a_reseeded_version_follows_the_new_id(db):
    """`prompt_version_ids` carries no FK, so a stale pin cannot raise — it just
    resolves to nothing at request time. Version 1 exists in both databases
    under different ids, which is exactly when the remap has to fire."""
    payload = _seed_json()
    payload["tables"]["agent_profiles"][1]["prompt_version_ids"] = {"system": "old-pv1"}

    db_snapshot.restore(payload)

    assert db.one("agent_profiles", slug="orchard_default")["prompt_version_ids"] \
        == {"system": "new-pv1"}


def test_a_pin_at_a_version_only_the_export_has_keeps_its_exported_id(db):
    """The other half of the same rule: v2 exists nowhere in the target, so the
    pin must be left alone — the row it names is inserted by this same restore."""
    db_snapshot.restore(_seed_json())

    assert db.one("agent_profiles", slug="orchard_default")["prompt_version_ids"] \
        == {"system": "old-pv2"}


def test_the_served_version_pin_is_applied_after_the_versions_exist(db):
    """`served_version_id` points INTO agent_profile_versions, which restore
    writes after agent_profiles. Written inline it fails the FK and takes the
    whole profile row with it, so it is deferred to the end."""
    db_snapshot.restore(_seed_json())

    assert db.one("agent_profile_versions", version_number=3)["profile_id"] \
        == "new-parent"
    assert db.one("agent_profiles", slug="orchard_default")["served_version_id"] \
        == "old-version"


def test_eval_suite_references_are_remapped_too(db):
    db_snapshot.restore(_seed_json())

    suite = db.one("agent_eval_suites", name="orchard starter")
    assert suite["evaluator_model_id"] == "new-model"
    assert suite["profile_id"] == "new-parent"


def test_restoring_twice_is_still_idempotent(db):
    """The claim CLAUDE.md always made, now that it holds across a reset: the
    second pass finds every natural key present and updates in place."""
    db_snapshot.restore(_seed_json())
    before = {t: [dict(r) for r in rows] for t, rows in db.tables.items()}

    db_snapshot.restore(_seed_json())

    assert db.tables == before


def test_without_the_remap_the_profiles_are_lost(db, monkeypatch):
    """The pre-fix behaviour, pinned.

    Disabling the remap must reproduce the original loss — otherwise every
    assertion above is passing against a fake that enforces nothing, and the
    remap is untested decoration. `_write_row` swallows the violation, which is
    why this was so surprising in the first place: restore printed a row count
    and exited 0.
    """
    monkeypatch.setattr(db_snapshot, "_build_id_map", lambda *_a, **_kw: {})

    db_snapshot.restore(_seed_json())

    slugs = [r["slug"] for r in db.rows("agent_profiles")]
    assert "orchard_voice" not in slugs, (
        "expected the child profile to be LOST to the parent_profile_id FK")
    parent = db.one("agent_profiles", slug="orchard_default")
    assert parent["model_main_id"] == "new-model", (
        "expected the parent's update to have been rejected wholesale, leaving "
        "the seeded row untouched — the portal edits never landed")
    assert parent["tool_ids"] == [], "and its tool/data-source edits with it"
    assert len(db.rows("agent_prompt_versions")) == 1, (
        "expected both exported versions to fail the prompt_id FK")
