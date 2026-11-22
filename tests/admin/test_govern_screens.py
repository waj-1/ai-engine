"""Ship / Govern screens: Activity sentences + filters, Team & access,
Releases overview + promote dialog contract, deploy history/rollback states,
drift grouping. Pure helpers are tested directly; routes through the shared
smoke `client` with module-level patches for the DB clients."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from admin.screens import govern

NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.timezone.utc)
ADMIN_ID = "11111111-1111-1111-1111-111111111111"
ADMIN_AUTH = "22222222-2222-2222-2222-222222222222"
PROFILE_ID = "33333333-3333-3333-3333-333333333333"
PROD_PROFILE_ID = "44444444-4444-4444-4444-444444444444"
T1 = "77777777-7777-7777-7777-777777777771"
T2 = "77777777-7777-7777-7777-777777777772"


class _Q:
    """Minimal postgrest query: eq / in_ filter for real, the rest recorded."""

    def __init__(self, rows: List[Dict[str, Any]], calls: list):
        self.rows, self.calls = list(rows), calls

    def select(self, *a, **kw):
        self.calls.append(("select", a, kw))
        return self

    def eq(self, col, val):
        self.calls.append(("eq", col, val))
        self.rows = [r for r in self.rows if str(r.get(col)) == str(val)]
        return self

    def in_(self, col, vals):
        self.calls.append(("in_", col, tuple(vals)))
        vals = {str(v) for v in vals}
        self.rows = [r for r in self.rows if str(r.get(col)) in vals]
        return self

    def or_(self, expr):
        self.calls.append(("or_", expr))
        return self

    def gte(self, col, val):
        self.calls.append(("gte", col, val))
        return self

    def order(self, *a, **kw):
        return self

    def limit(self, *a):
        return self

    def range(self, *a):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows, count=len(self.rows))


class _DB:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self.tables, self.calls = tables, []

    def table(self, name):
        return _Q(self.tables.get(name, []), self.calls)


MAIN = _DB({"user": [
    {"id": ADMIN_ID, "auth_id": ADMIN_AUTH, "email": "sara@partner.com", "role": "admin",
     "permission": {"is_superuser": True}},
]})


# --------------------------------------------------------------------------
# people + sentences
# --------------------------------------------------------------------------

def test_resolve_people_by_id_auth_id_and_system():
    people = govern.resolve_people(MAIN, [ADMIN_ID, ADMIN_AUTH, "00000000-0000-0000-0000-000000000001",
                                          "99999999-9999-9999-9999-999999999999"])
    assert people[ADMIN_ID]["label"] == "sara@partner.com"
    assert people[ADMIN_AUTH]["label"] == "sara@partner.com"  # MCP writers record the auth id
    assert people["00000000-0000-0000-0000-000000000001"]["is_system"] is True
    assert people["99999999-9999-9999-9999-999999999999"]["label"].startswith("Unknown user")


def test_promote_row_reads_as_sentence_with_version_and_link():
    ai = _DB({
        "agent_profiles": [{"profile_id": PROD_PROFILE_ID, "display_name": "WAJ AI", "slug": "business_default"}],
        "agent_profile_versions": [{"profile_id": PROD_PROFILE_ID, "version_number": 12,
                                    "promoted_at": "2026-10-11T10:00:02+00:00"}],
    })
    row = {"audit_id": "a1", "action": "CONFIG_PROMOTE", "entity_type": "agent_profiles",
           "entity_id": PROD_PROFILE_ID, "actor_user_id": ADMIN_ID,
           "created_at": "2026-10-11T10:00:00+00:00",
           "before": {"environment": "staging"}, "after": {"environment": "prod"}}
    people = govern.resolve_people(MAIN, [ADMIN_ID])
    d = govern.describe_row(row, people=people, entities=govern.resolve_entities(ai, [row]),
                            versions=govern.promote_versions(ai, [row]), now=NOW)
    assert govern.sentence_text(d) == "sara@partner.com published WAJ AI v12 to live"
    assert d["href"] == f"/admin/agents?profile_id={PROD_PROFILE_ID}"
    assert d["when"] == "2h ago"
    assert d["changed"] == []  # a promote's env-only diff is not a "field change"


def test_unknown_action_and_missing_entity_fall_back_to_plain_words():
    row = {"audit_id": "a2", "action": "SOME_NEW_THING", "entity_type": "environment",
           "entity_id": "prod", "actor_user_id": None, "created_at": "2026-10-01T00:00:00+00:00"}
    d = govern.describe_row(row, people={}, entities={}, versions={}, now=NOW)
    assert d["actor"]["label"] == "System"
    assert d["verb"] == "some new thing"
    assert d["name"] == "Production"


PROMPT_ID = "55555555-5555-5555-5555-555555555555"
GONE_VERSION = "966b8e14-0000-0000-0000-000000000000"


def test_prompt_version_rows_never_read_as_a_raw_id():
    # Two real cases that rendered "prompt version 966b8e14": a version row
    # since deleted, and the "Save anyway" override, audited under the
    # PROMPT's id rather than a version's.
    ai = _DB({
        "agent_prompt_versions": [],
        "agent_prompts": [{"prompt_id": PROMPT_ID, "slug": "system"}],
        "agent_profiles": [{"profile_id": PROFILE_ID, "display_name": "WAJ AI"}],
    })
    gone = {"audit_id": "a3", "action": "create", "entity_type": "agent_prompt_versions",
            "entity_id": GONE_VERSION, "actor_user_id": None,
            "created_at": "2026-10-11T10:00:00+00:00",
            "after": {"version_number": 10, "prompt_id": PROMPT_ID,
                      "created_at": "2026-10-11T10:00:00+00:00"}}
    override = {"audit_id": "a4", "action": "validation_override",
                "entity_type": "agent_prompt_versions", "entity_id": PROMPT_ID,
                "actor_user_id": None, "created_at": "2026-10-11T10:00:00+00:00",
                "after": {"prompt_id": PROMPT_ID, "profile_id": PROFILE_ID,
                          "prompt_key": "system", "problems": []}}
    ents = govern.resolve_entities(ai, [gone, override])
    d = govern.describe_row(gone, people={}, entities=ents, versions={}, now=NOW)
    assert d["name"] == "v10 · 11 Oct 2026"
    d = govern.describe_row(override, people={}, entities=ents, versions={}, now=NOW)
    assert d["name"] == "WAJ AI instructions"
    for row in (gone, override):
        d = govern.describe_row(row, people={}, entities=ents, versions={}, now=NOW)
        assert "966b8e14" not in govern.sentence_text(d)
        assert PROMPT_ID[:8] not in govern.sentence_text(d)


def test_activity_filters_map_chips_to_queries():
    db = _DB({})
    q = db.table("agent_audit_log").select("*")
    govern.apply_activity_filters(q, who="me", what="publishing", when="7d", entity_type=None,
                                  me=ADMIN_ID, now=NOW)
    assert ("eq", "actor_user_id", ADMIN_ID) in db.calls
    ors = [c[1] for c in db.calls if c[0] == "or_"]
    assert ors and "action.in.(CONFIG_PROMOTE" in ors[0] and "entity_type.in.(environment" in ors[0]
    gte = [c for c in db.calls if c[0] == "gte"]
    assert gte and gte[0][2].startswith("2026-10-04")


def test_field_changes_inline_short_values_and_diff_long_ones():
    changes = govern.field_changes(
        {"name": "Old", "content": "line a\nline b", "updated_at": "x"},
        {"name": "New", "content": "line a\nline c", "updated_at": "y"})
    by_key = {c["key"]: c for c in changes}
    assert "updated_at" not in by_key  # bookkeeping fields hidden
    assert (by_key["name"]["before"], by_key["name"]["after"]) == ("Old", "New")
    kinds = [cls for cls, _ in by_key["content"]["diff"]]
    assert "add" in kinds and "del" in kinds


def test_field_changes_on_create_shows_plain_text_not_all_added_diff():
    changes = govern.field_changes(None, {"content": "a\nb\nc", "notes": "seeded"})
    by_key = {c["key"]: c for c in changes}
    assert by_key["content"]["text"] == "a\nb\nc" and by_key["content"]["diff"] is None
    assert by_key["notes"]["after"] == "seeded"


# --------------------------------------------------------------------------
# Activity routes
# --------------------------------------------------------------------------

def _audit_rows():
    return [{"audit_id": "55555555-5555-5555-5555-555555555555", "action": "CONFIG_PROMOTE",
             "entity_type": "agent_profiles", "entity_id": PROD_PROFILE_ID, "actor_user_id": ADMIN_ID,
             "created_at": "2026-10-11T10:00:00+00:00", "before": {"environment": "staging", "name": "A"},
             "after": {"environment": "prod", "name": "B"}}]


def test_activity_page_renders_sentences_chips_and_overflow_menu(client):
    ai = _DB({"agent_audit_log": _audit_rows(),
              "agent_profiles": [{"profile_id": PROD_PROFILE_ID, "display_name": "WAJ AI"}]})
    with patch("admin.router.client_for", return_value=ai), \
         patch("admin.router.get_main_client", return_value=MAIN):
        r = client.get("/admin/audit")
    assert r.status_code == 200
    html = r.text
    assert "sara@partner.com" in html and "published" in html and ">WAJ AI</a>" in html
    assert "<strong>sara@partner.com</strong>" in html  # actor resolved, not a uuid
    assert f"<strong>User {ADMIN_ID[:8]}" not in html
    for label in ("Who", "What", "When", "Anyone", "Publishing &amp; releases", "Last 7 days"):
        assert label in html
    assert 'class="menu"' in html and "Export (JSON)" in html and "Verify log replica" in html
    assert 'name="entity_type"' not in html  # free-text filter replaced by chips


def test_activity_legacy_entity_type_filter_and_json_export(client):
    ai = _DB({"agent_audit_log": _audit_rows()})
    with patch("admin.router.client_for", return_value=ai), \
         patch("admin.router.get_main_client", return_value=MAIN):
        r = client.get("/admin/audit?entity_type=agent_profiles&format=json")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["actor_name"] == "sara@partner.com"
    assert body[0]["summary"].startswith("sara@partner.com published")
    assert ("eq", "entity_type", "agent_profiles") in ai.calls


def test_activity_detail_is_flat_with_kv_readable_diff_and_raw_json(client):
    ai = _DB({"agent_audit_log": _audit_rows(),
              "agent_profiles": [{"profile_id": PROD_PROFILE_ID, "display_name": "WAJ AI"}]})
    with patch("admin.router.client_for", return_value=ai), \
         patch("admin.router.get_main_client", return_value=MAIN):
        r = client.get("/admin/audit/55555555-5555-5555-5555-555555555555")
        bad = client.get("/admin/audit/not-a-uuid", headers={"accept": "text/html"})
    assert r.status_code == 200
    html = r.text
    assert '<dl class="kv' in html and "sara@partner.com" in html
    assert 'href="/admin/agents?profile_id=' in html
    assert "Technical details and raw data" in html
    assert 'class="card"' not in html.split('class="app-main')[-1]  # no stacked cards
    assert bad.status_code == 404


# --------------------------------------------------------------------------
# Releases
# --------------------------------------------------------------------------

def _profiles():
    src = {"profile_id": PROFILE_ID, "agent_kind": "business", "slug": "business_default",
           "display_name": "WAJ AI", "environment": "staging", "tool_ids": [T1, T2],
           "prompt_version_ids": {"system": "pv2"}, "voice_config": {"a": 1}, "updated_at": "2026-10-10"}
    tgt = {**src, "profile_id": PROD_PROFILE_ID, "environment": "prod", "tool_ids": [T1],
           "prompt_version_ids": {"system": "pv1"}, "voice_config": {"a": 2}, "served_version_id": None}
    return src, tgt


def test_release_rows_show_live_version_changes_and_quality():
    src, tgt = _profiles()
    db = _DB({
        "agent_profile_versions": [{"version_id": "v", "profile_id": PROD_PROFILE_ID, "version_number": 7,
                                    "promoted_at": "2026-10-07T09:00:00+00:00", "promoted_by": ADMIN_ID}],
        "agent_tools": [{"tool_id": T2, "display_name": "Book appointment"}],
    })
    rows = govern.release_rows(src_client=db, tgt_client=db, src_rows=[src], tgt_rows=[tgt],
                               eval_status={PROFILE_ID: {"status": "below_threshold", "score": 0.79,
                                                         "threshold": 0.9, "suite_name": "s"}},
                               main_client=MAIN, now=NOW)
    r = rows[0]
    assert r["live"]["number"] == 7 and r["live"]["by"] == "sara@partner.com"
    labels = {c["label"]: c for c in r["changes"]}
    assert labels["Tools"]["added"] == ["Book appointment"]
    assert "Instructions" in labels and "Voice settings" in labels
    assert r["quality"]["blocks"] is True and "79%" in r["quality"]["text"]


def test_release_rows_up_to_date_and_new_agent():
    src, tgt = _profiles()
    same = {**src, "profile_id": PROD_PROFILE_ID, "environment": "prod"}
    other = {**src, "profile_id": "66666666-6666-6666-6666-666666666666", "slug": "fresh"}
    rows = govern.release_rows(src_client=_DB({}), tgt_client=_DB({}), src_rows=[src, other],
                               tgt_rows=[same], eval_status={}, now=NOW)
    by_slug = {r["technical"].split(" · ")[1]: r for r in rows}
    assert by_slug["business_default"]["has_changes"] is False
    assert by_slug["fresh"]["is_new"] is True


def test_env_labels_single_env_is_draft_to_live():
    assert govern.env_labels("staging")["source"] == "Draft"
    assert govern.env_labels("staging")["target"] == "Live"
    assert govern.env_labels(None)["target"] == "Production"


def test_single_env_build_renders_draft_live_naming(client, monkeypatch):
    from tests.scoped_build import make_scope, use_scope

    use_scope(monkeypatch, make_scope("orchard"))
    r = client.get("/admin/env")
    assert r.status_code == 200
    assert "Publish draft agent changes to live." in r.text
    assert "your draft setup next to what is live" in r.text
    assert "Production" not in r.text.split("<main")[-1]


def test_promote_preview_returns_changes_and_gates(client):
    src, tgt = _profiles()
    db = _DB({"agent_profiles": [src, tgt]})
    with patch("admin.router.client_for", return_value=db), \
         patch("admin.router._latest_eval_for_profile",
               return_value={"status": "below_threshold", "score": 0.5, "threshold": 0.7}), \
         patch("admin.services.promotion._prompt_mandated_unbound_tools", return_value=["book"]):
        r = client.get(f"/admin/env/promote/preview?profile_id={PROFILE_ID}")
    assert r.status_code == 200
    d = r.json()
    assert d["needs_reason"] is True and d["missing_tools"] == ["book"]
    assert any(c["label"] == "Tools" for c in d["changes"])


def test_promote_from_dialog_requires_reason_when_forcing(client):
    calls = []
    with patch("admin.services.promotion.promote", side_effect=lambda **kw: calls.append(kw) or {}):
        r = client.post("/admin/env/promote", data={"profile_id": PROFILE_ID, "target_env": "staging",
                                                    "force": "true", "ui": "1"}, follow_redirects=False)
        assert r.status_code == 303 and "error=" in r.headers["location"]
        assert calls == []
        r = client.post("/admin/env/promote", data={"profile_id": PROFILE_ID, "target_env": "staging",
                                                    "force": "true", "ui": "1", "reason": "judge flaky"},
                        follow_redirects=False)
    assert r.status_code == 303 and "promoted=" in r.headers["location"]
    assert calls[0]["reason"] == "judge flaky" and calls[0]["force"] is True


def test_promote_gate_failure_from_dialog_is_a_readable_message(client):
    from admin.services.promotion import EvalGateFailed

    def boom(**kw):
        raise EvalGateFailed(422, {"error": "eval_gate_failed", "score": 0.1})

    with patch("admin.services.promotion.promote", side_effect=boom):
        ui = client.post("/admin/env/promote", data={"profile_id": PROFILE_ID, "target_env": "staging",
                                                     "ui": "1"}, follow_redirects=False)
        api = client.post("/admin/env/promote", data={"profile_id": PROFILE_ID, "target_env": "staging"},
                          follow_redirects=False)
    assert ui.status_code == 303 and "below+the+bar" in ui.headers["location"]
    assert api.status_code == 422  # contract unchanged for non-dialog callers


# --------------------------------------------------------------------------
# Deploy history / rollback list states
# --------------------------------------------------------------------------

def test_deploy_runs_json_reports_error_without_token(client, monkeypatch):
    monkeypatch.delenv("GITHUB_DEPLOY_TOKEN", raising=False)
    r = client.get("/admin/deploy-runs.json")
    assert r.status_code == 200
    assert r.json()["runs"] == [] and "GitHub deploy token" in r.json()["error"]


def test_deploy_revisions_json_explains_when_unavailable(client, monkeypatch):
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    r = client.get("/admin/deploy-revisions.json")
    assert r.status_code == 200
    assert r.json()["revisions"] == [] and "isn't available" in r.json()["error"]


def test_deploy_revisions_mark_live_and_below_floor(monkeypatch):
    import asyncio

    from admin import deploy_handlers as dh

    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("ACA_APP_NAME", "ai-engine")
    monkeypatch.setenv("ACA_RESOURCE_GROUP", "rg-engine")
    monkeypatch.setattr(dh, "_arm_token", lambda: "tok")

    class _Resp:
        status_code = 200

        def json(self):
            return {"value": [
                {"name": "ai-engine--0000294", "properties": {"createdTime": "2026-10-11T13:55:01+00:00",
                                                                 "active": True, "trafficWeight": 100}},
                {"name": "ai-engine--0000100", "properties": {"createdTime": "2026-08-01T00:00:00+00:00",
                                                                 "active": False}},
            ]}

    class _Http:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(dh.httpx, "AsyncClient", _Http)
    out = asyncio.run(dh._fetch_revisions("prod"))
    names = [r["name"] for r in out["revisions"]]
    assert names == ["ai-engine--0000294", "ai-engine--0000100"]
    assert out["revisions"][0]["traffic"] == 100
    assert out["revisions"][1]["below_floor"] is True


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

def test_group_drift_groups_by_agent_worst_first():
    from scripts.check_profile_drift import Finding

    groups = govern.group_drift([
        Finding("ok", "prompt", "orchard_voice", "fine"),
        Finding("info", "config", "orchard_default.voice_config.x", "unset"),
        Finding("critical", "config", "orchard_default.voice_config.barge_in_enabled", "off"),
        Finding("ok", "model", "orchard_voice -> model.extra_config.y", "= 1"),
    ])
    assert [g["agent"] for g in groups] == ["orchard_default", "orchard_voice"]
    assert groups[0]["counts"] == {"critical": 1, "warning": 0, "info": 1, "ok": 0}
    assert groups[0]["items"][0].severity == "critical"
    assert groups[1]["counts"]["ok"] == 2


def test_drift_screen_collapses_info_and_shows_badges(monkeypatch):
    import scripts.check_profile_drift as drift_mod
    from scripts.check_profile_drift import Finding

    monkeypatch.setattr(drift_mod, "collect_drift", lambda **kw: [
        Finding("critical", "config", "orchard_default.voice_config.barge_in_enabled", "off"),
        Finding("info", "config", "orchard_default.voice_config.x", "unset"),
        Finding("info", "config", "support_voice.voice_config.y", "unset"),
    ])
    from tests.admin.test_drift_screen import _app

    html = _app().get("/admin/drift").text
    assert "1 critical" in html and "1 info" in html
    # The group with a critical finding opens; info-only groups stay closed.
    assert '<details class="drift-group" open>' in html
    assert html.count('<details class="drift-group" >') + html.count('<details class="drift-group">') == 1
    assert '<details class="drift-quiet">' in html
