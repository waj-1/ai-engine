"""Demo mode's usage analytics (demo_mode/analytics.py, ``DEMO_POSTHOG_KEY``).

What these pin:

1. Off by default: in demo mode without a key, and with a key but demo mode
   off, no page carries the config or loads the script.
2. On with a key: the entry page, every portal page and the gate's "locked"
   and "too fast" pages embed the config and load the script before any
   other deferred script, with the page kind and the section.
3. The config holds only what the script needs: the public key, the host, the
   seeded agents' slugs and the cap sentences, and no secret.
4. The script itself is opt-out by construction: no autocapture, replay,
   heatmaps or surveys, and every event is tagged with the app.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from demo_mode import analytics, gate, limits, settings

KEY = "phc_testpublickey"
SCRIPT = Path(__file__).resolve().parents[2] / "admin" / "static" / "demo-analytics.js"


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_PUBLIC_URL", "https://demo.example.com")
    monkeypatch.delenv("DEMO_POSTHOG_KEY", raising=False)
    monkeypatch.delenv("DEMO_POSTHOG_HOST", raising=False)
    limits.reset_for_tests()
    yield
    limits.reset_for_tests()


@pytest.fixture
def tracked(demo, monkeypatch):
    monkeypatch.setenv("DEMO_POSTHOG_KEY", KEY)


def _config(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="demo-analytics-config">(.*?)</script>', html, re.S)
    assert m, "no analytics config on the page"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------
# 1. Off by default
# --------------------------------------------------------------------------


def test_off_without_a_key(client, demo):
    assert analytics.enabled() is False
    assert analytics.config("portal") is None
    assert analytics.head_tags("locked") == ""
    for path in ("/admin/login", "/admin/settings"):
        page = client.get(path).text
        assert "demo-analytics" not in page, path


def test_off_when_demo_mode_is_off(client, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("DEMO_POSTHOG_KEY", KEY)
    assert analytics.enabled() is False
    for path in ("/admin/login", "/admin/settings"):
        page = client.get(path).text
        assert "demo-analytics" not in page and KEY not in page, path


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("DEMO_POSTHOG_KEY", raising=False)
    monkeypatch.delenv("DEMO_POSTHOG_HOST", raising=False)
    assert settings.posthog_key() == ""
    assert settings.posthog_host() == "https://us.i.posthog.com"
    monkeypatch.setenv("DEMO_POSTHOG_HOST", "https://ph.example.com/")
    assert settings.posthog_host() == "https://ph.example.com"


# --------------------------------------------------------------------------
# 2. On with a key
# --------------------------------------------------------------------------


def test_entry_page_carries_the_config(client, tracked):
    page = client.get("/admin/login").text
    cfg = _config(page)
    assert cfg["key"] == KEY and cfg["page"] == "entry"
    assert cfg["host"] == "https://us.i.posthog.com"
    assert cfg["public_host"] == "demo.example.com"
    assert "/admin/static/demo-analytics.js" in page


def test_portal_page_carries_page_and_section(client, tracked):
    page = client.get("/admin/settings").text
    cfg = _config(page)
    assert cfg["page"] == "portal" and cfg["section"] == "settings"
    # In <head>, so it runs before the deferred builder-assistant.js.
    assert page.index("demo-analytics.js") < page.index("builder-assistant.js")
    assert page.index("demo-analytics.js") < page.index("</head>")


def test_custom_host(client, tracked, monkeypatch):
    monkeypatch.setenv("DEMO_POSTHOG_HOST", "https://ph.example.com")
    assert _config(client.get("/admin/login").text)["host"] == "https://ph.example.com"


def test_locked_page_carries_the_config(client, tracked):
    resp = client.get("/admin/model-keys", headers={"Accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 403
    assert _config(resp.text)["page"] == "locked"


def test_refusal_pages_name_their_kind(tracked):
    assert _config(gate._page("x", "rate").decode())["page"] == "rate"
    assert _config(gate._page("x", "daily").decode())["page"] == "daily"


def test_refusal_page_unchanged_without_a_key(demo):
    assert "demo-analytics" not in gate._page("x").decode()


# --------------------------------------------------------------------------
# 3. What the config holds
# --------------------------------------------------------------------------


def test_config_holds_seeded_slugs_and_cap_sentences(tracked):
    cfg = analytics.config("portal", "agents")
    assert "support-desk" in cfg["seeded"]
    assert cfg["messages"]["daily"] == limits.CAP_MESSAGE
    assert cfg["messages"]["rate"] == limits.RATE_MESSAGE
    assert set(cfg) == {"key", "host", "public_host", "repo_url", "showcase_url", "page",
                        "section", "seeded", "messages"}


def test_config_cannot_close_its_script_element(tracked, monkeypatch):
    monkeypatch.setenv("DEMO_REPO_URL", "https://example.com/</script><script>alert(1)")
    tags = analytics.head_tags("locked")
    body = tags.split('id="demo-analytics-config">', 1)[1].split("</script>", 1)[0]
    assert "</" not in body


# --------------------------------------------------------------------------
# 4. The script
# --------------------------------------------------------------------------


def test_script_switches_off_autocollection():
    js = SCRIPT.read_text(encoding="utf-8")
    for option in ("autocapture: false", "capture_pageview: false", "disable_session_recording: true",
                   "capture_heatmaps: false", "disable_surveys: true", "capture_dead_clicks: false",
                   "rageclick: false", "capture_exceptions: false", "advanced_disable_flags: true"):
        assert option in js, option
    assert 'app: "engine_demo"' in js
    # Every custom event is namespaced.
    names = set(re.findall(r'send\("([a-z$_]+)"', js))
    assert names and all(n.startswith("engine_demo_") or n == "$pageview" for n in names), names


def test_script_drops_the_page_title():
    """The library adds the page title to a $pageview as `title`; an agent
    page's title is the agent's name, which a visitor may have typed."""
    js = SCRIPT.read_text(encoding="utf-8")
    pattern = re.search(r"var DROP_KEY = /(.+)/;", js).group(1)
    for key in ("title", "$title", "$el_text", "$elements", "$elements_chain"):
        assert re.fullmatch(pattern.replace("\\$", r"\$"), key), key
    assert not re.fullmatch(pattern, "section")


def test_tour_run_finished_is_reported_from_the_tour_only():
    """Run pages' pollers announce every run they watch, some twice; only the
    tour's own announcement (source "tour") is an event."""
    js = SCRIPT.read_text(encoding="utf-8")
    handler = js.split('"tour:run-finished": function (d) {', 1)[1].split("},", 1)[0]
    assert 'if (d.source !== "tour") return;' in handler
