"""The demo's guided tour: the server half (``DEMO_TOUR``, ``reset_id``).

What these pin:

1. ``DEMO_TOUR`` is on by default in demo mode, ``false`` switches it off,
   and outside demo mode there is no tour whatever the variable says;
   `demo_chrome()` stays ``{"enabled": False}`` there.
2. ``reset_id`` is the UTC date of the last nightly reset: today from
   ``DEMO_RESET_HOUR_UTC`` on, yesterday before it, midnight when the reset
   is off.
3. The palette's "Resume tour" / "Restart tour" actions exist only when the
   tour is on.
4. The cap answers keep the words builder-assistant.js and test-chat.js
   match to tell the tour a step hit a limit.
5. Every anchor the tour points at is still in the templates.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from admin import nav
from demo_mode import limits, settings

UTC = timezone.utc


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.delenv("DEMO_TOUR", raising=False)
    monkeypatch.delenv("DEMO_RESET_HOUR_UTC", raising=False)


# --------------------------------------------------------------------------
# 1. DEMO_TOUR
# --------------------------------------------------------------------------


def test_tour_is_on_by_default_in_demo_mode(demo):
    assert settings.tour_enabled() is True
    chrome = nav.demo_chrome()
    assert chrome["enabled"] is True and chrome["tour"] is True
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", chrome["reset_id"])


@pytest.mark.parametrize("value", ["false", "0", "off", "no", " FALSE "])
def test_tour_can_be_switched_off(demo, monkeypatch, value):
    monkeypatch.setenv("DEMO_TOUR", value)
    assert settings.tour_enabled() is False
    assert nav.demo_chrome()["tour"] is False


@pytest.mark.parametrize("value", ["true", "1", "on", "", "anything"])
def test_tour_stays_on_for_other_values(demo, monkeypatch, value):
    monkeypatch.setenv("DEMO_TOUR", value)
    assert settings.tour_enabled() is True


def test_no_tour_outside_demo_mode(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("DEMO_TOUR", "true")
    assert settings.tour_enabled() is False
    assert nav.demo_chrome() == {"enabled": False}


# --------------------------------------------------------------------------
# 2. reset_id
# --------------------------------------------------------------------------


def test_reset_id_flips_at_the_default_hour(demo):
    # Default DEMO_RESET_HOUR_UTC is 3.
    assert settings.reset_id(datetime(2026, 11, 24, 2, 59, 59, tzinfo=UTC)) == "2026-11-23"
    assert settings.reset_id(datetime(2026, 11, 24, 3, 0, 0, tzinfo=UTC)) == "2026-11-24"
    assert settings.reset_id(datetime(2026, 11, 24, 23, 59, tzinfo=UTC)) == "2026-11-24"


def test_reset_id_follows_the_reset_hour(demo, monkeypatch):
    monkeypatch.setenv("DEMO_RESET_HOUR_UTC", "17")
    assert settings.reset_id(datetime(2026, 11, 24, 16, 59, tzinfo=UTC)) == "2026-11-23"
    assert settings.reset_id(datetime(2026, 11, 24, 17, 0, tzinfo=UTC)) == "2026-11-24"
    monkeypatch.setenv("DEMO_RESET_HOUR_UTC", "0")
    assert settings.reset_id(datetime(2026, 11, 24, 0, 0, tzinfo=UTC)) == "2026-11-24"


def test_reset_id_crosses_month_and_year(demo):
    assert settings.reset_id(datetime(2027, 1, 1, 1, 0, tzinfo=UTC)) == "2026-12-31"
    assert settings.reset_id(datetime(2026, 3, 1, 2, 0, tzinfo=UTC)) == "2026-02-28"


def test_reset_id_with_the_reset_off_is_the_utc_date(demo, monkeypatch):
    monkeypatch.setenv("DEMO_RESET_HOUR_UTC", "off")
    assert settings.reset_id(datetime(2026, 11, 24, 0, 30, tzinfo=UTC)) == "2026-11-24"


def test_reset_id_reads_other_time_zones_as_utc(demo):
    # 02:30 in Dubai (UTC+4) is 22:30 UTC the day before: after that day's 03:00 reset.
    dubai = timezone(timedelta(hours=4))
    assert settings.reset_id(datetime(2026, 11, 24, 2, 30, tzinfo=dubai)) == "2026-11-23"
    # A naive time is taken as UTC.
    assert settings.reset_id(datetime(2026, 11, 24, 4, 0)) == "2026-11-24"


def test_reset_id_matches_the_reset_loop(demo, monkeypatch):
    """The reset loop sleeps until the hour, then resets: one second after
    `seconds_until` runs out the id must be that day's date."""
    from demo_mode import reset

    monkeypatch.setenv("DEMO_RESET_HOUR_UTC", "5")
    now = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
    fired = now + timedelta(seconds=reset.seconds_until(5, now) + 1)
    assert settings.reset_id(fired) == fired.date().isoformat() == "2026-11-25"


# --------------------------------------------------------------------------
# 3. Palette quick actions
# --------------------------------------------------------------------------


def _tour_hrefs():
    return [a["href"] for a in nav.search_quick_actions("staging") if a["href"].startswith("#tour:")]


def test_palette_offers_tour_actions_only_with_the_tour(demo, monkeypatch):
    assert _tour_hrefs() == ["#tour:resume", "#tour:restart"]
    monkeypatch.setenv("DEMO_TOUR", "false")
    assert _tour_hrefs() == []
    monkeypatch.delenv("DEMO_MODE")
    monkeypatch.delenv("DEMO_TOUR")
    assert _tour_hrefs() == []


# --------------------------------------------------------------------------
# 4. The cap answers the browser matches
# --------------------------------------------------------------------------


def test_cap_answers_keep_the_words_the_tour_matches():
    assert "reached today's limit" in limits.CAP_MESSAGE
    assert "going a little fast for the public demo" in limits.RATE_MESSAGE
    root = Path(__file__).resolve().parents[2] / "admin" / "static"
    for name in ("builder-assistant.js", "test-chat.js"):
        js = (root / name).read_text(encoding="utf-8")
        assert "reached today's limit" in js and "going a little fast for the public demo" in js, name


# --------------------------------------------------------------------------
# 5. Anchors
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
CATALOG = (ROOT / "admin" / "static" / "demo-tour-steps.js").read_text(encoding="utf-8")


def _catalog_anchors() -> set:
    """Every anchor the catalog points a card or a rule at, read from the file."""
    return set(re.findall(r'anchor:\s*"([a-z0-9-]+)"', CATALOG))


def test_every_catalog_anchor_is_in_the_templates():
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "admin" / "templates").rglob("*.html"))
    text += (ROOT / "admin" / "static" / "test-chat.js").read_text(encoding="utf-8")
    anchors = _catalog_anchors()
    assert {"conversation-list", "conversation-row", "needs-attention", "case-row"} <= anchors
    # The spec's anchors stay in the templates even where a walkthrough uses a
    # finer one (agent-tools, agent-subagents).
    spec = {"agent-instructions", "agent-test", "test-input", "agents-new", "agent-tools",
            "agent-subagents", "suite-row", "run-tests", "run-cases", "runs-compare",
            "agent-promote", "overview-kpis", "needs-attention", "audit-list", "search-trigger"}
    missing = sorted(a for a in anchors | spec if f'data-tour="{a}"' not in text)
    assert missing == []


def test_every_ai_tool_and_action_is_the_assistants():
    from demo_mode import assistant

    allowed = dict(assistant.ALLOWED_TOOLS)
    allowed[assistant.WAIT_TOOL] = None
    tools = set()
    for block in re.findall(r"tools:\s*\[([^\]]*)\]", CATALOG):
        tools |= set(re.findall(r'"([a-z_]+)"', block))
    actions = set()
    for block in re.findall(r"actions:\s*\[(.*?)\]\s*[,}\n]", CATALOG, flags=re.S):
        actions |= set(re.findall(r'"([a-z_]+(?::[a-z_]+)*)"', block))
    assert tools and actions
    assert sorted(t for t in tools if t not in allowed) == []
    for action in actions:
        name, _, rest = action.partition(":")
        assert name in allowed, action
        verb = rest.split(":")[0] if rest else ""
        if verb and allowed[name] is not None:
            assert verb in allowed[name], action
    # The catalog's own list of the assistant's tools matches the server's.
    listed = set(re.findall(r'"([a-z_]+)"', CATALOG.split("assistantTools:")[1].split("]")[0]))
    assert listed == set(allowed)


# --------------------------------------------------------------------------
# 6. Nothing of the tour on the page unless demo mode and DEMO_TOUR are on
# --------------------------------------------------------------------------

_TOUR_MARKS = ("demo-tour.js", "demo-tour-steps.js", "demo-tour.css", 'id="demo-tour-config"',
               'id="dt-entry"', 'id="dt-pill"', 'data-tour="search-trigger"')


def test_the_tour_loads_in_demo_mode(client, demo, monkeypatch):
    monkeypatch.setenv("DEMO_PUBLIC_URL", "https://demo.example.com")
    page = client.get("/admin/settings").text
    assert all(m in page for m in _TOUR_MARKS), [m for m in _TOUR_MARKS if m not in page]


def test_no_tour_with_demo_tour_false(client, demo, monkeypatch):
    monkeypatch.setenv("DEMO_TOUR", "false")
    page = client.get("/admin/settings").text
    assert "builder-assistant.js" in page  # still the demo
    assert [m for m in _TOUR_MARKS if m in page] == []


def test_no_tour_outside_demo_mode(client, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("DEMO_TOUR", "true")
    page = client.get("/admin/settings").text
    assert [m for m in _TOUR_MARKS if m in page] == []
    # portal.css carries no tour rules: they live in demo-tour.css only.
    css = (ROOT / "admin" / "static" / "portal.css").read_text(encoding="utf-8")
    assert ".dt-" not in css and "ba-tour" not in css


# --------------------------------------------------------------------------
# 7. reset_id follows the data, not only the clock
# --------------------------------------------------------------------------

def test_reset_id_follows_the_demo_data(demo, monkeypatch):
    from demo_mode import protect

    ids = {"value": {"builder-1", "prompt-1"}}
    monkeypatch.setattr(protect, "_lookup", lambda: set(ids["value"]))
    protect.forget()
    first = nav.demo_chrome()["reset_id"]
    assert first and first == nav.demo_chrome()["reset_id"]
    # A wipe and seed gives the Builder assistant new ids: a new reset id.
    ids["value"] = {"builder-2", "prompt-2"}
    protect.forget()
    second = nav.demo_chrome()["reset_id"]
    assert second != first
    protect.forget()


def test_reset_id_falls_back_to_the_clock(demo, monkeypatch):
    from demo_mode import protect

    def boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(protect, "_lookup", boom)
    protect.forget()
    assert nav.demo_chrome()["reset_id"] == settings.reset_id()
    protect.forget()


def test_no_data_generation_outside_demo_mode(monkeypatch):
    from demo_mode import protect

    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert protect.data_generation() is None
