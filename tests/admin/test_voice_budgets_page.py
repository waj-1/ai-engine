"""Duplex-voice budgets live on Settings › Voice budgets (2026-10-11).

They used to be a card repeated on every realtime model page (spec D4). The
allowance belongs to a tenant, not a model, so it now has one page of its own
and the model pages only link there. What this pins:

1. The page renders the allowances, today's burn and the tenants burning the
   default; no model page carries the card any more (a realtime one links).
2. The copy still says the allowance is per **tenant** and keeps the
   env-default restart caveat — the substance of D4 survives the move.
3. The write routes did not move and still take a validated ``next=`` hop
   (operator input landing in a Location header) — a hostile ``next`` must
   never leave the portal. The fallback is now the page itself.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

MODEL_PAGE = "/admin/agent-models/rt-1"
FALLBACK = "/admin/voice-budgets"
PAGE = "/admin/voice-budgets"


def _flat(html: str) -> str:
    """Collapse whitespace so a line-wrapped sentence still matches."""
    return " ".join(html.split())


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return _Result(self._rows)


class FakeSupabase:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


def _tables() -> Dict[str, List[Dict[str, Any]]]:
    today = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).date().isoformat()
    return {
        "agent_models": [
            {"model_id": "rt-1", "name": "Azure Voice Live Native (gpt-realtime)",
             "provider": "azure_voice_live", "model_string": "gpt-realtime",
             "is_active": True, "environment": "staging",
             "extra_config": {"realtime": True, "voice_type": "native"}},
            {"model_id": "chat-1", "name": "Gemini 3.5 Flash",
             "provider": "google_genai", "model_string": "gemini-3.5-flash",
             "is_active": True, "environment": "staging",
             "extra_config": {"location": "global"}},
        ],
        "ai_voice_budgets": [
            {"business_id": "11111111-1111-1111-1111-111111111111",
             "daily_seconds": 1200, "note": "client demo VM"},
        ],
        "ai_voice_usage_daily": [
            {"business_id": "11111111-1111-1111-1111-111111111111",
             "usage_date": today, "seconds_used": 300},
            {"business_id": "22222222-2222-2222-2222-222222222222",
             "usage_date": today, "seconds_used": 900},
        ],
    }


@pytest.fixture
def model_db():
    """Point the model-detail handler and the budgets page at the fake DB.

    `admin.model_handlers` imports `client_for` into its own namespace, so the
    conftest's `admin.router.client_for` patch does not reach it.
    """
    fake = FakeSupabase(_tables())
    with patch("admin.model_handlers.client_for", return_value=fake), \
         patch("admin.router.client_for", return_value=fake):
        yield


# --------------------------------------------------------------------------
# 1. Where the budgets render
# --------------------------------------------------------------------------


def test_page_renders_allowances_and_burn(client, model_db):
    r = client.get(PAGE)
    assert r.status_code == 200
    # Data, not just chrome: the allowance, today's burn, and the tenant
    # burning the default with no row of its own.
    assert "11111111-1111-1111-1111-111111111111" in r.text
    assert "1200s" in r.text
    assert "Using the default today" in r.text
    assert "22222222-2222-2222-2222-222222222222" in r.text


def test_realtime_model_page_links_instead_of_repeating_the_card(client, model_db):
    r = client.get(MODEL_PAGE)
    assert r.status_code == 200
    assert 'href="/admin/voice-budgets"' in r.text
    # The card itself is gone: no budget rows, no write form on a model page.
    assert 'action="/admin/voice-budgets"' not in r.text
    assert "11111111-1111-1111-1111-111111111111" not in r.text


def test_card_absent_on_a_chat_model(client, model_db):
    """A chat row cannot burn an audio-second — no card, not even a link."""
    r = client.get("/admin/agent-models/chat-1")
    assert r.status_code == 200
    assert 'action="/admin/voice-budgets"' not in r.text
    assert 'href="/admin/voice-budgets"' not in r.text


def test_page_labels_the_allowance_as_per_tenant(client, model_db):
    """The anti-misreading copy is the point of D4 — pin it on its new home."""
    text = _flat(client.get(PAGE).text)
    assert "belongs to a <strong>business</strong>, not to a voice model" in text
    assert "per tenant, not per model" in text
    assert "does not change, reset or duplicate its budget" in text


def test_page_keeps_the_env_default_restart_caveat_visible(client, model_db):
    """VOICE_LIVE_DAILY_SECONDS is read at import in voice/live/budget.py —
    an operator who edits it needs to know a restart is required."""
    text = client.get(PAGE).text
    assert "VOICE_LIVE_DAILY_SECONDS" in text
    assert "restart" in text.lower()


def test_page_forms_post_back_to_the_page(client, model_db):
    text = client.get(PAGE).text
    assert 'action="/admin/voice-budgets"' in text
    assert f'name="next" value="{PAGE}"' in text


def test_page_is_a_settings_member(client, model_db):
    from admin import nav

    assert nav.section_for(PAGE) == "settings"
    tabs = nav.section_tabs_for(PAGE)
    assert tabs is not None


# --------------------------------------------------------------------------
# 2. The `next` hop — honoured when same-site, refused otherwise
# --------------------------------------------------------------------------

HOSTILE_NEXTS = [
    "https://evil.example/login",          # absolute URL
    "//evil.example/login",                # protocol-relative: leaves the site
    "/\\evil.example",                     # backslash normalised to "/" by some browsers
    "/etc/passwd",                         # same-site but outside the portal
    "/admin/../../evil.example",           # escapes /admin/ once the browser resolves it
    "/admin/agents\r\nSet-Cookie: x=1",    # header splitting
    "javascript:alert(1)",
]


def test_upsert_honours_a_same_site_next(client):
    r = client.post("/admin/voice-budgets", data={
        "business_id": "11111111-1111-1111-1111-111111111111",
        "daily_seconds": "1200", "note": "", "next": MODEL_PAGE,
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == MODEL_PAGE


@pytest.mark.parametrize("hostile", HOSTILE_NEXTS)
def test_upsert_refuses_a_hostile_next(client, hostile):
    r = client.post("/admin/voice-budgets", data={
        "business_id": "11111111-1111-1111-1111-111111111111",
        "daily_seconds": "1200", "note": "", "next": hostile,
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == FALLBACK


def test_upsert_without_next_keeps_todays_behaviour(client):
    r = client.post("/admin/voice-budgets", data={
        "business_id": "11111111-1111-1111-1111-111111111111",
        "daily_seconds": "1200", "note": "",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == FALLBACK


def test_upsert_validation_error_returns_to_next(client):
    """A rejected value must surface where the operator was, or the message
    lands on a screen they never opened."""
    r = client.post("/admin/voice-budgets", data={
        "business_id": "11111111-1111-1111-1111-111111111111",
        "daily_seconds": "-5", "note": "", "next": MODEL_PAGE,
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(MODEL_PAGE + "?")
    assert "error=" in r.headers["location"]


def test_delete_honours_a_same_site_next(client):
    r = client.post(
        "/admin/voice-budgets/11111111-1111-1111-1111-111111111111/delete",
        data={"next": MODEL_PAGE}, follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == MODEL_PAGE


@pytest.mark.parametrize("hostile", HOSTILE_NEXTS)
def test_delete_refuses_a_hostile_next(client, hostile):
    r = client.post(
        "/admin/voice-budgets/11111111-1111-1111-1111-111111111111/delete",
        data={"next": hostile}, follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == FALLBACK


def test_delete_without_next_keeps_todays_behaviour(client):
    r = client.post(
        "/admin/voice-budgets/11111111-1111-1111-1111-111111111111/delete",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == FALLBACK


# --------------------------------------------------------------------------
# 3. The URL
# --------------------------------------------------------------------------


def test_voice_budgets_url_is_a_page_again(client, model_db):
    """The URL docs/admin-portal/ and bookmarks use keeps resolving — now to
    the page itself rather than a redirect to the Models tab."""
    r = client.get("/admin/voice-budgets?env=prod", follow_redirects=False)
    assert r.status_code == 200
    assert "Voice budgets" in r.text


def test_nav_no_longer_offers_a_voice_budgets_screen(client):
    r = client.get("/admin/live")
    assert r.status_code == 200
    assert 'href="/admin/voice-budgets"' not in r.text
