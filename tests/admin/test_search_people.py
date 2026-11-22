"""Global search: records, conversations and people — PII masking, partner
scoping, environment scoping, fail-soft sources and log hygiene.

A small in-memory PostgREST fake stands in for the AI-project and main
Supabase clients (``admin.search_sources._ai`` / ``_main``), applying the
filters the sources send so the tests see what a real query would return.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

import pytest

import admin.search as search
import admin.search_sources as src
from admin import search_pii as pii
from tests.product import requires_product
from tests.scoped_build import full_build as _full_build, make_scope, use_scope


# ---------------------------------------------------------------------------
# Fake PostgREST
# ---------------------------------------------------------------------------


def _get(row, col):
    if "->>" in col:
        base, key = col.split("->>", 1)
        return (row.get(base) or {}).get(key)
    return row.get(col)


def _like(value, pattern):
    if value is None:
        return False
    rx = "^" + re.escape(pattern.strip('"')).replace("%", ".*") + "$"
    return re.match(rx, str(value), re.I | re.S) is not None


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table_name, self.preds = db, table, []
        self._limit = None
        db.calls.append(self)

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def eq(self, col, val):
        self.preds.append(("eq", col, val))
        self.preds_fn(lambda r: str(_get(r, col)).lower() == str(val).lower())
        return self

    def in_(self, col, vals):
        self.preds.append(("in", col, list(vals)))
        vs = {str(v) for v in vals}
        self.preds_fn(lambda r: str(_get(r, col)) in vs)
        return self

    def ilike(self, col, pat):
        self.preds.append(("ilike", col, pat))
        self.preds_fn(lambda r: _like(_get(r, col), pat))
        return self

    def is_(self, col, val):
        self.preds.append(("is", col, val))
        self.preds_fn(lambda r: _get(r, col) is None)
        return self

    def or_(self, expr):
        self.preds.append(("or", expr))
        parts = []
        for p in re.findall(r'([\w>-]+)\.ilike\.("[^"]*"|[^,]*)', expr):
            parts.append(p)
        self.preds_fn(lambda r: any(_like(_get(r, c), pat) for c, pat in parts))
        return self

    def preds_fn(self, fn):
        self.__dict__.setdefault("_fns", []).append(fn)

    def execute(self):
        if self.db.fail_tables and self.table_name in self.db.fail_tables:
            raise RuntimeError("boom 971500009901")  # error text carries the term
        rows = [r for r in self.db.tables.get(self.table_name, [])
                if all(f(r) for f in self.__dict__.get("_fns", []))]
        if self._limit:
            rows = rows[: self._limit]
        return type("Res", (), {"data": [dict(r) for r in rows]})()


class FakeDB:
    def __init__(self, tables, fail_tables=()):
        self.tables, self.calls, self.fail_tables = tables, [], set(fail_tables)

    def table(self, name):
        return FakeQuery(self, name)

    def queried(self):
        return {c.table_name for c in self.calls}


INBOX_SID = "ab1fbc7e-dc80-4a26-8b44-cb29b737370c"
RECORDS_SID = "7e570000-0000-4000-8000-0000000b7001"
ORCHARD_SID = "9e000000-0000-4000-8000-0000000000aa"
BIZ = "ec9c986d-e46d-4d12-9357-9d8b0edcc541"


def _ai_tables():
    return {
        "ai_chat_sessions": [
            {"id": INBOX_SID, "title": "New Chat", "agent_kind": "support_bot", "business_id": BIZ,
             "user_id": None, "message_count": 4, "metadata": {"session_type": "inbox"},
             "created_at": "2026-10-10T10:00:00+00:00", "deleted_at": None},
            {"id": RECORDS_SID, "title": "Follow-up case note", "agent_kind": "records", "business_id": None,
             "user_id": None, "message_count": 2,
             "metadata": {"record_id": "rec-778899", "case_id": "case-112233"},
             "created_at": "2026-10-09T10:00:00+00:00", "deleted_at": None},
            {"id": ORCHARD_SID, "title": "Sara asks about her IBAN", "agent_kind": "orchard", "business_id": None,
             "user_id": None, "message_count": 6, "metadata": {},
             "created_at": "2026-10-08T10:00:00+00:00", "deleted_at": None},
            {"id": "b0000000-0000-4000-8000-0000000000bb", "title": "Sara Time Off Request",
             "agent_kind": "business", "business_id": BIZ, "user_id": "u-auth-1", "message_count": 3,
             "metadata": {}, "created_at": "2026-10-07T10:00:00+00:00", "deleted_at": None},
        ],
        "agent_profiles": [
            {"profile_id": "p-orchard", "display_name": "Orchard Merchant Assistant", "slug": "orchard_default",
             "description": "", "agent_kind": "orchard", "status": "active", "environment": "staging"},
            {"profile_id": "p-orchard-prod", "display_name": "Orchard Merchant Assistant", "slug": "orchard_default",
             "description": "", "agent_kind": "orchard", "status": "active", "environment": "prod"},
            {"profile_id": "p-waj", "display_name": "WAJ Merchant Assistant", "slug": "business_default",
             "description": "", "agent_kind": "business", "status": "active", "environment": "staging"},
        ],
        "agent_tools": [
            {"tool_id": "t1", "display_name": "Merchant lookup", "python_name": "orchard_lookup", "description": "",
             "agent_kind": "orchard", "additional_agent_kinds": [], "is_deprecated": False},
            {"tool_id": "t2", "display_name": "Merchant bookings", "python_name": "get_bookings",
             "description": "", "agent_kind": "business", "additional_agent_kinds": [], "is_deprecated": False},
        ],
        "grove_leads": [],
    }


def _main_tables():
    return {
        "customers": [
            {"id": "c-cristel", "customer_name": "Cristel Testerson", "phone_number": "500009901",
             "country_code": "971", "customer_email": "cristel@example.com", "business_id": BIZ,
             "external_record_id": None, "active": True},
            {"id": "c-nour", "customer_name": "Nour Haddad", "phone_number": "500004455",
             "country_code": "971", "customer_email": None, "business_id": BIZ,
             "external_record_id": "rec-778899", "active": True},
        ],
        "conversations": [
            {"id": INBOX_SID, "customer_id": "c-cristel", "customer_name": "Cristel Testerson",
             "customer_phone": "+971500009901", "customer_handle": None, "business_id": BIZ,
             "channel": "whatsapp", "last_message_at": "2026-10-10T10:00:00+00:00", "is_deleted": False},
        ],
        "user": [],
        "business": [{"id": BIZ, "business_name": "Harbor Cafe"}],
    }


@pytest.fixture
def dbs(monkeypatch):
    ai, main = FakeDB(_ai_tables()), FakeDB(_main_tables())
    envs = []

    def fake_ai(env):
        envs.append(env)
        return ai

    monkeypatch.setattr(src, "_ai", fake_ai)
    monkeypatch.setattr(src, "_main", lambda: main)
    monkeypatch.setattr(src, "_ch", lambda *a, **k: [])
    # Which session-metadata keys hold record ids, and which customers column
    # links to them, is a plugin's declaration; these are test data.
    monkeypatch.setattr(src, "session_record_keys", lambda: ["record_id", "case_id"])
    monkeypatch.setattr(src, "customer_record_links",
                        lambda: [("external_record_id", "record_id", "Member")])
    src._biz_cache.clear()
    ai.envs = envs
    return ai, main


@pytest.fixture
def full_build(monkeypatch):
    _full_build(monkeypatch)


@pytest.fixture
def orchard_build(monkeypatch):
    use_scope(monkeypatch, make_scope("orchard"))


def _people(rows):
    return [r for r in rows if r["group"] == "Conversations & people"]


# ---------------------------------------------------------------------------
# Finding people and their conversations
# ---------------------------------------------------------------------------


@requires_product
def test_customer_by_name_links_to_their_conversation_masked(dbs, full_build):
    p = search.search_payload("cristel", "staging")
    people = [r for r in _people(p["results"]) if r["type"] == "Customer"]
    assert len(people) == 1
    row = people[0]
    assert row["label"] == "Cristel T."
    assert row["href"] == f"/admin/sessions/{INBOX_SID}?env=staging"
    assert "Harbor Cafe" in row["context"] and "1 conversation" in row["context"]
    blob = json.dumps(p)
    assert "Testerson" not in blob and "500009901" not in blob and "cristel@example.com" not in blob


@pytest.mark.parametrize("q", ["+971 50 000 9901", "0500009901", "971500009901", "500009901"])
@requires_product
def test_customer_by_phone_in_any_format(dbs, full_build, q):
    rows = _people(search.search_payload(q, "staging")["results"])
    hit = next(r for r in rows if r["type"] == "Customer")
    assert hit["href"].startswith(f"/admin/sessions/{INBOX_SID}")
    assert "9901" in hit["context"] and "50000" not in json.dumps(rows)


@requires_product
def test_member_found_by_name_through_their_record_id(dbs, full_build):
    rows = _people(search.search_payload("nour", "staging")["results"])
    hit = next(r for r in rows if r["type"] == "Member")
    assert hit["label"] == "Nour H."
    assert hit["href"].startswith(f"/admin/sessions/{RECORDS_SID}")


def test_conversation_by_linked_record_id(dbs, full_build):
    rows = _people(search.search_payload("rec-778899", "staging")["results"])
    assert any(r["href"].startswith(f"/admin/sessions/{RECORDS_SID}") for r in rows)


def test_conversation_by_exact_session_id(dbs, full_build):
    rows = _people(search.search_payload(ORCHARD_SID, "staging")["results"])
    assert rows[0]["href"].startswith(f"/admin/sessions/{ORCHARD_SID}")
    assert rows[0]["type"] == "Conversation"


def test_people_without_a_conversation_here_are_not_listed(dbs, full_build):
    ai, _ = dbs
    ai.tables["ai_chat_sessions"] = [s for s in ai.tables["ai_chat_sessions"] if s["id"] != INBOX_SID]
    rows = _people(search.search_payload("cristel", "staging")["results"])
    assert not [r for r in rows if r["type"] == "Customer"]


def test_env_scoping_uses_the_selected_env_client_and_rows(dbs, full_build):
    ai, _ = dbs
    rows = search.search_payload("orchard merchant", "prod")["results"]
    assert set(ai.envs) == {"prod"}
    agents = [r for r in rows if r["type"] == "Agent"]
    assert [r["href"] for r in agents] == ["/admin/agents?env=prod&profile_id=p-orchard-prod"]
    assert all("env=prod" in r["href"] for r in rows if r["href"].startswith(("/admin/sessions", "/admin/agents")))


def test_agent_name_plus_tab_pairs_into_a_deep_link(dbs, full_build):
    rows = search.search_payload("orchard prompt", "staging")["results"]
    hit = next(r for r in rows if r["type"] == "Agent tab" and r["label"].startswith("Orchard"))
    assert hit["label"] == "Orchard Merchant Assistant › Instructions"
    assert hit["href"] == "/admin/agents?env=staging&profile_id=p-orchard&tab=instructions"


# ---------------------------------------------------------------------------
# Partner builds never return other clients' data
# ---------------------------------------------------------------------------


def test_partner_build_never_queries_the_waj_main_database(dbs, orchard_build):
    _, main = dbs
    for q in ("cristel", "+971 50 000 9901", "harbor cafe", "@nour"):
        search.search_payload(q, "staging")
    # Only the Team source may read the box's own `user` table (the same rows
    # Settings › Team & access lists there); WAJ business data is never read.
    assert main.queried() <= {"user"}, f"main DB queried on a partner build: {main.queried()}"


def test_partner_build_sessions_are_limited_to_client_kinds(dbs, orchard_build):
    rows = _people(search.search_payload("sara", "staging")["results"])
    hrefs = [r["href"] for r in rows]
    assert any(ORCHARD_SID in h for h in hrefs)
    assert not [h for h in hrefs if "b0000000-0000-4000-8000-0000000000bb" in h]  # a WAJ business chat
    # By id too: a WAJ session id resolves to nothing on a orchard build.
    rows = _people(search.search_payload(INBOX_SID, "staging")["results"])
    assert rows == []


def test_partner_build_records_are_limited_to_client_kinds(dbs, orchard_build):
    rows = search.search_payload("merchant", "staging")["results"]
    labels = {r["label"] for r in rows}
    assert "Orchard Merchant Assistant" in labels and "Merchant lookup" in labels
    assert "WAJ Merchant Assistant" not in labels and "Merchant bookings" not in labels


def test_partner_telemetry_query_is_kind_scoped(monkeypatch, orchard_build):
    seen = {}

    def fake_ch(sql, params, timeout):
        seen.update(sql=sql, params=params, timeout=timeout)
        return [{"id": "4cc56aa1-0000-4000-8000-000000000001", "session_id": "", "start_time": "",
                 "agent_kind": "orchard", "top_span_name": "x"}]

    monkeypatch.setattr(src, "_ch", fake_ch)
    ctx = search._ctx("4cc56aa1", "prod")
    rows = src.traces(ctx)
    assert rows and rows[0]["href"] == "/admin/observability?env=prod#trace=4cc56aa1-0000-4000-8000-000000000001"
    assert "4cc56aa1" not in seen["sql"], "the query text must travel as a parameter"
    assert seen["params"]["q"] == "4cc56aa1" and seen["params"]["env"] == "prod"
    assert "orchard" in seen["params"]["kinds"] and "business" not in seen["params"]["kinds"]
    assert seen["timeout"] <= 3


# ---------------------------------------------------------------------------
# Fail-soft
# ---------------------------------------------------------------------------


@requires_product
def test_a_failing_source_is_skipped_with_a_quiet_note(dbs, full_build):
    _, main = dbs
    main.fail_tables = {"customers"}
    p = search.search_payload("sara", "staging")
    assert [n["source"] for n in p["notes"]] == ["people"]
    assert "boom" not in json.dumps(p["notes"])
    # The other sources still answered: the conversation found by its title.
    assert any("b0000000-0000-4000-8000-0000000000bb" in r["href"] for r in p["results"])


def test_a_slow_source_is_skipped_at_the_deadline(monkeypatch, full_build):
    def slow(ctx):
        time.sleep(1.0)
        return [{"label": "late", "type": "X", "group": "Screens", "href": "/x", "icon": "screen", "_rank": 0}]

    def fast(ctx):
        return [{"label": "quick", "type": "X", "group": "Screens", "href": "/y", "icon": "screen", "_rank": 0}]

    ctx = search._ctx("anything", "staging")
    t0 = time.monotonic()
    rows, notes = search.run_sources(ctx, (("slow", "Slow things", slow), ("fast", "Fast", fast)), deadline=0.2)
    assert time.monotonic() - t0 < 0.9
    assert [r["label"] for r in rows] == ["quick"]
    assert notes == [{"source": "slow", "message": "Slow things took too long and were skipped."}]


def test_endpoint_survives_every_source_failing(client, monkeypatch):
    def boom(ctx):
        raise RuntimeError("down")

    monkeypatch.setattr(search, "RECORD_SOURCES", (("a", "A", boom),))
    monkeypatch.setattr(search, "PEOPLE_SOURCES", (("b", "B", boom),))
    r = client.post("/admin/search.json", json={"q": "overview", "env": "staging"})
    assert r.status_code == 200
    body = r.json()
    assert any(row["label"] == "Overview" for row in body["results"])
    assert {n["source"] for n in body["notes"]} == {"a", "b"}


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,masked", [
    ("Cristel Testerson", "Cristel T."),
    ("Nour Al Haddad", "Nour A. H."),
    ("Madonna", "Mad•••"),
    ("Al", "Al"),
    ("", ""),
    (None, ""),
])
def test_mask_name(name, masked):
    assert pii.mask_name(name) == masked


@pytest.mark.parametrize("phone,cc,masked", [
    ("500009901", "971", "+971 ••• 9901"),
    ("+971500009901", None, "+971 ••• 9901"),
    ("0501234567", None, "••• 4567"),
    ("", None, ""),
])
def test_mask_phone(phone, cc, masked):
    assert pii.mask_phone(phone, cc) == masked


def test_mask_email():
    assert pii.mask_email("cristel@example.com") == "cr•••@example.com"
    assert pii.mask_email("nope") == ""


def test_phone_candidates_cover_stored_formats():
    c = pii.phone_candidates("+971 50 000 9901")
    assert {"971500009901", "500009901", "0500009901"} <= set(c)
    assert "500009901" in pii.phone_candidates("0500009901")


def test_query_classification():
    assert pii.is_phone("+971 50 000 9901") and not pii.is_phone("orchard")
    assert pii.is_id_fragment("4cc56aa1") and not pii.is_id_fragment("decade")
    assert not pii.is_id_fragment("0500009901")  # a phone number is not an id
    assert pii.is_uuid(INBOX_SID)


def test_people_rows_never_go_into_recents():
    js = (__import__("pathlib").Path(__file__).resolve().parents[2]
          / "admin/static/global-search.js").read_text()
    assert "item.person" in js and "People never go into browser storage" in js


# ---------------------------------------------------------------------------
# Logs never carry a phone number
# ---------------------------------------------------------------------------


def test_search_logs_never_contain_the_query(dbs, full_build, caplog):
    _, main = dbs
    main.fail_tables = {"customers", "conversations", "user", "business"}
    caplog.set_level(logging.DEBUG)
    search.search_payload("+971 50 000 9901", "staging")
    search.search_payload("@cristel", "staging")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "9901" not in text and "cristel" not in text.lower()


def test_access_log_filter_redacts_search_query():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "GET", "/admin/search.json?q=%2B971500009901&env=prod", "1.1", 200), None)
    pii.SearchQueryLogFilter().filter(rec)
    assert "971500009901" not in rec.getMessage() and "env=prod" in rec.getMessage()


def test_httpx_request_logs_from_search_threads_are_dropped():
    f = pii.SearchThreadHttpLogFilter()
    rec = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: GET ...phone_number=in.(971500009901)", (), None)
    out = {}

    def run():
        out["v"] = f.filter(rec)

    t = threading.Thread(target=run, name=pii.SEARCH_THREAD_PREFIX + "_0")
    t.start()
    t.join()
    assert out["v"] is False
    assert f.filter(rec) is True  # other threads keep their request logs


def test_redact_query():
    assert pii.redact_query("call +971 50 000 9901 now") == "call +[number] now"
