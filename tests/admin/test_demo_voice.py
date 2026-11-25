"""Voice calls in the public demo (demo_mode/voice.py, ``DEMO_VOICE``).

What these pin:

1. Off by default: with ``DEMO_MODE`` on and ``DEMO_VOICE`` unset the voice
   socket is refused as before, and ``DEMO_VOICE`` does nothing outside demo
   mode. The plain `/voice/live` socket stays refused either way.
2. With it on, every call runs under `voice.guard`: a call is cut at the
   length limit, charged while it runs and for its last part, ended when the
   day's seconds run out, and refused when the day is used up, every line is
   busy or the visitor has placed too many calls.
3. The day's cap holds with no database (it counts in memory).
4. A visitor's voice is never recorded.
5. The seed turns voice on for the Support desk only when the realtime model
   is set up, and leaves a visitor's change alone.
6. The Voice tab and the banner say what a visitor can do.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from starlette.websockets import WebSocketDisconnect

from demo_mode import limits, policy, seed, settings, voice
from tests import fake_pg


def _raise(*_a, **_k):
    raise RuntimeError("no database in tests")


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.delenv("DEMO_VOICE", raising=False)
    limits.reset_for_tests()
    voice.reset_for_tests()
    # The daily counter is the database's; here it falls back to memory.
    monkeypatch.setattr(limits, "_db_take", _raise)
    monkeypatch.setattr(limits, "_db_used", _raise)
    yield
    limits.reset_for_tests()
    voice.reset_for_tests()


@pytest.fixture
def voice_on(demo, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE", "true")
    monkeypatch.setenv("DEMO_VOICE_ENDPOINT", "https://demo-voice.services.ai.azure.com/")
    monkeypatch.setenv(settings.VOICE_KEY_ENV, "test-voice-key")
    # What the demo's process lock-down sets at start.
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")


class _Clock:
    """Virtual time for `voice._sleep`: a sleeper wakes when the clock reaches
    its time, and the clock only jumps to the earliest waiting sleeper."""

    def __init__(self):
        self.now = 0.0
        self.waiting: List[float] = []

    async def sleep(self, seconds):
        wake = self.now + seconds
        self.waiting.append(wake)
        try:
            while self.now < wake:
                await asyncio.sleep(0)  # let every other sleeper register first
                if self.now < wake and min(self.waiting) == wake:
                    self.now = wake
        finally:
            self.waiting.remove(wake)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(voice, "_sleep", c.sleep)
    monkeypatch.setattr(voice, "_now", lambda: c.now)
    return c


class _Socket:
    def __init__(self, ip="203.0.113.7"):
        self.scope = {"type": "websocket", "headers": [], "client": (ip, 1234)}
        self.accepted = False
        self.closed: Optional[tuple] = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        if self.closed is None:
            self.closed = (code, reason)


async def _forever():
    await asyncio.Event().wait()


# --------------------------------------------------------------------------
# 1. The switch
# --------------------------------------------------------------------------


def test_voice_socket_is_refused_unless_switched_on(demo, monkeypatch):
    assert policy.refusal("GET", "/v1/agents/support-desk/voice") is not None
    monkeypatch.setenv("DEMO_VOICE", "true")
    assert policy.refusal("GET", "/v1/agents/support-desk/voice") is None
    # The plain socket and the budgets screen stay locked.
    assert policy.refusal("GET", "/voice/live") is not None
    assert policy.refusal("GET", "/admin/voice-budgets") is not None


def test_demo_voice_means_nothing_outside_demo_mode(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("DEMO_VOICE", "true")
    assert settings.voice_enabled() is False
    assert voice.problem() is None and voice.note() is None


def test_gate_closes_the_handshake_when_voice_is_off(client, demo):
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/v1/agents/support-desk/voice"):
            pass
    assert closed.value.code == 1008


def test_route_runs_every_demo_call_under_the_guard(client, voice_on, monkeypatch):
    seen: List[str] = []

    async def fake_guard(websocket, run):
        seen.append(websocket.scope["path"])
        await websocket.accept()
        await websocket.close(code=4429, reason="guarded")

    monkeypatch.setattr(voice, "guard", fake_guard)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/v1/agents/support-desk/voice") as ws:
            ws.receive_text()
    assert closed.value.code == 4429 and seen == ["/v1/agents/support-desk/voice"]


def test_route_skips_the_guard_outside_demo(client, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    import endpoints.voice_live as live_ep

    async def fake_guard(websocket, run):  # pragma: no cover - must not run
        raise AssertionError("the demo guard ran outside demo mode")

    async def fake_serve(websocket, route, **_kw):
        await websocket.accept()
        await websocket.close(code=4403, reason="served")

    monkeypatch.setattr(voice, "guard", fake_guard)
    monkeypatch.setattr(live_ep, "serve_voice_route", fake_serve)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/v1/agents/support-desk/voice") as ws:
            ws.receive_text()
    assert closed.value.code == 4403


# --------------------------------------------------------------------------
# 2-3. The caps
# --------------------------------------------------------------------------


async def test_guard_refuses_when_voice_is_off(demo):
    ws = _Socket()
    ran = []

    async def run():
        ran.append(1)

    await voice.guard(ws, run)
    assert ws.closed == (1008, "") and not ran


async def test_call_is_cut_at_the_length_limit_and_charged(voice_on, clock, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE_MAX_CALL_SECONDS", "40")
    ws = _Socket()
    cancelled = []

    async def run():
        try:
            await _forever()
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    await voice.guard(ws, run)
    assert cancelled == [1]
    assert ws.closed == (voice.CLOSE_CODE, "Calls in the public demo end after 40 seconds. Thanks for trying it.")
    assert limits.used(voice.VOICE_BUCKET) == 40
    assert voice.open_calls() == 0


async def test_the_agents_own_settings_cannot_lengthen_a_call(voice_on, clock):
    """The limit is the guard's, so a visitor who saves a long
    `max_session_sec` on an agent gains nothing."""
    ws = _Socket()
    await voice.guard(ws, _forever)
    assert limits.used(voice.VOICE_BUCKET) == settings.voice_max_call_seconds() == 180
    assert "3 minutes" in ws.closed[1]


async def test_short_call_is_charged_for_its_seconds(voice_on, clock, monkeypatch):
    ws = _Socket()
    monkeypatch.setattr(voice, "_sleep", lambda _s: asyncio.Event().wait())  # the first step never comes

    async def run():
        clock.now += 7

    await voice.guard(ws, run)
    assert limits.used(voice.VOICE_BUCKET) == 7
    assert ws.closed is None  # the call ended by itself: nothing to say


async def test_call_ends_when_the_days_seconds_run_out(voice_on, clock, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE_DAILY_SECONDS", "40")
    ws = _Socket()
    await voice.guard(ws, _forever)
    assert ws.closed == (voice.CLOSE_CODE, voice.CAP_MESSAGE)
    # Two whole steps fit in 40 seconds; the third did not, and the call's
    # last part took what was left.
    assert limits.used(voice.VOICE_BUCKET) == 40 and voice.seconds_left() == 0
    # Nothing is left, so the next call is refused before it starts.
    ran = []

    async def run():
        ran.append(1)

    again = _Socket()
    await voice.guard(again, run)
    assert again.accepted and again.closed == (voice.CLOSE_CODE, voice.CAP_MESSAGE) and not ran


async def test_many_short_calls_cannot_go_past_the_day(voice_on, clock, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE_DAILY_SECONDS", "60")
    monkeypatch.setenv("DEMO_VOICE_CALLS_PER_HOUR", "1000")

    monkeypatch.setattr(voice, "_sleep", lambda _s: asyncio.Event().wait())

    async def run():
        clock.now += 9

    for i in range(50):
        await voice.guard(_Socket(ip=f"198.51.100.{i}"), run)
    assert limits.used(voice.VOICE_BUCKET) <= 60
    assert voice.seconds_left() < voice.STEP_SEC


async def test_lines_at_once_are_capped_whatever_the_address(voice_on, clock, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE_MAX_CALLS", "2")
    release = asyncio.Event()

    async def run():
        await release.wait()

    monkeypatch.setattr(voice, "_sleep", lambda _s: asyncio.Event().wait())  # no charging here
    first = asyncio.ensure_future(voice.guard(_Socket(ip="198.51.100.1"), run))
    second = asyncio.ensure_future(voice.guard(_Socket(ip="198.51.100.2"), run))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if voice.open_calls() == 2:
            break
    assert voice.open_calls() == 2
    third = _Socket(ip="198.51.100.3")  # a made-up address gains nothing
    await voice.guard(third, run)
    assert third.closed == (voice.CLOSE_CODE, voice.BUSY_MESSAGE)
    release.set()
    await asyncio.gather(first, second)
    assert voice.open_calls() == 0


async def test_calls_per_visitor_are_limited(voice_on, clock, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE_CALLS_PER_HOUR", "2")
    monkeypatch.setenv("DEMO_IP_MULTIPLIER", "1")

    async def run():
        return None

    for _ in range(2):
        ws = _Socket()
        await voice.guard(ws, run)
        assert ws.closed is None
    third = _Socket()
    await voice.guard(third, run)
    assert third.closed == (voice.CLOSE_CODE, voice.RATE_MESSAGE)


async def test_a_failed_call_still_frees_its_line(voice_on, clock):
    async def run():
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await voice.guard(_Socket(), run)
    assert voice.open_calls() == 0


def test_reasons_fit_a_close_frame(voice_on):
    for text in (voice.CAP_MESSAGE, voice.BUSY_MESSAGE, voice.RATE_MESSAGE, voice.length_message()):
        assert len(text.encode("utf-8")) <= 120, text
        assert chr(0x2014) not in text


# --------------------------------------------------------------------------
# 4. Recording
# --------------------------------------------------------------------------


def test_process_lockdown_switches_off_call_recording():
    env = {"DEMO_MODE": "1", "VOICE_RECORDING_ENABLED": "1"}
    settings.apply_process_lockdown(env)
    assert env["VOICE_RECORDING_ENABLED"] == "0"
    unset = {"DEMO_MODE": "1"}
    settings.apply_process_lockdown(unset)
    assert unset["VOICE_RECORDING_ENABLED"] == "0"
    off = {"VOICE_RECORDING_ENABLED": "1"}
    settings.apply_process_lockdown(off)
    assert off == {"VOICE_RECORDING_ENABLED": "1"}


# --------------------------------------------------------------------------
# 5. The seed
# --------------------------------------------------------------------------


class _Query(fake_pg.Query):
    def execute(self):
        if self.op == "insert" and self.table == "agent_models":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            self.payload = [dict(i, model_id=f"m{len(self.db.get('agent_models', [])) + n}")
                            for n, i in enumerate(items)]
        return super().execute()


class _Client(fake_pg.Client):
    def table(self, name):
        return _Query(self.db, name)


def _store() -> Dict[str, list]:
    return {"agent_models": [],
            "agent_profiles": [{"profile_id": "p-support", "slug": "support-desk",
                                "voice_config": None}]}


def test_seed_gives_the_support_desk_a_voice(voice_on):
    db = _store()
    model_id = seed.ensure_voice(_Client(db), "staging", {"support-desk": "p-support"})
    row = db["agent_models"][0]
    assert row["model_id"] == model_id and row["provider"] == "azure_voice_live"
    assert row["model_string"] == "gpt-4.1-mini" and row["environment"] == "staging"
    extra = row["extra_config"]
    assert extra["realtime"] is True
    assert extra["endpoint"] == "https://demo-voice.services.ai.azure.com"
    assert extra["auth_env_var"] == "DEMO_VOICE_API_KEY"
    # The row names the variable, never the key.
    assert "test-voice-key" not in repr(db)
    config = db["agent_profiles"][0]["voice_config"]
    assert config["enabled"] is True and config["model_id"] == model_id
    assert config["language"] == "en-US" and config["voice_id"] == "en-US-AvaMultilingualNeural"
    # The seeded row can take a call on this server.
    from voice.live.readiness import realtime_problem

    assert realtime_problem(row["provider"], extra) is None


def test_seed_is_idempotent_and_keeps_a_visitors_change(voice_on):
    db = _store()
    client = _Client(db)
    seed.ensure_voice(client, "staging", {"support-desk": "p-support"})
    db["agent_profiles"][0]["voice_config"] = {"enabled": False}
    seed.ensure_voice(client, "staging", {"support-desk": "p-support"})
    assert len(db["agent_models"]) == 1
    assert db["agent_profiles"][0]["voice_config"] == {"enabled": False}


@pytest.mark.parametrize("missing", ["DEMO_VOICE", "DEMO_VOICE_ENDPOINT", "DEMO_VOICE_API_KEY"])
def test_seed_adds_nothing_until_voice_is_set_up(voice_on, monkeypatch, missing):
    monkeypatch.delenv(missing)
    db = _store()
    assert seed.ensure_voice(_Client(db), "staging", {"support-desk": "p-support"}) is None
    assert db["agent_models"] == [] and db["agent_profiles"][0]["voice_config"] is None


def test_seed_can_use_gemini_live(voice_on, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE_PROVIDER", "google_genai")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")
    db = _store()
    seed.ensure_voice(_Client(db), "staging", {"support-desk": "p-support"})
    row = db["agent_models"][0]
    assert row["provider"] == "google_genai"
    assert row["model_string"] == "gemini-3.1-flash-live-preview"
    assert "endpoint" not in row["extra_config"] and "api_key" not in row["extra_config"]
    assert db["agent_profiles"][0]["voice_config"]["voice_id"] == "Kore"


# --------------------------------------------------------------------------
# 6. What a visitor sees
# --------------------------------------------------------------------------


def _panel(**block: Any):
    from admin.screens import agent_voice

    owner = {"agent_kind": "custom", "slug": "support-desk", "profile_id": "p-support",
             "is_active": True, "status": "active"}
    model = {"provider": "azure_voice_live",
             "extra_config": {"endpoint": "https://demo-voice.services.ai.azure.com",
                              "auth_env_var": "DEMO_VOICE_API_KEY"}}
    return agent_voice.generic_voice_panel(owner, {"enabled": True, **block}, model)


def test_voice_tab_says_calls_are_off_in_the_demo(demo, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE_API_KEY", "test-voice-key")
    panel = _panel()
    assert panel["can_call"] is False and "switched off in the public demo" in panel["problem"]
    assert panel["demo_note"] is None


def test_voice_tab_offers_the_call_and_names_the_limits(voice_on):
    panel = _panel()
    assert panel["can_call"] is True and panel["problem"] is None
    assert "3 minutes" in panel["demo_note"] and "30 minutes" in panel["demo_note"]


def test_voice_tab_says_when_the_day_is_used_up(voice_on, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE_DAILY_SECONDS", "10")
    assert _panel()["problem"] == voice.CAP_MESSAGE


def test_voice_tab_is_unchanged_outside_demo(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    monkeypatch.setenv("DEMO_VOICE_API_KEY", "test-voice-key")
    panel = _panel()
    assert panel["problem"] is None and panel["demo_note"] is None


def test_banner_offers_the_call_only_when_voice_is_on(client, demo, monkeypatch):
    assert "data-demo-call" not in client.get("/admin/settings").text
    assert client.get("/admin/demo/call", follow_redirects=False).status_code == 404
    monkeypatch.setenv("DEMO_VOICE", "true")
    assert "data-demo-call" in client.get("/admin/settings").text
    assert "call one from your browser" in client.get("/admin/login").text


def test_call_link_opens_the_support_desks_voice_tab(client, voice_on, monkeypatch):
    from admin import demo_handlers

    monkeypatch.setattr(demo_handlers, "_call_page_sync",
                        lambda: "/admin/agents/p-support/voice?env=staging#voice-call")
    resp = client.get("/admin/demo/call", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/agents/p-support/voice?env=staging#voice-call"


# --------------------------------------------------------------------------
# 7. A real call, on the simulated provider, cut by the guard
# --------------------------------------------------------------------------

from tests.voice.test_custom_agent_voice import _open, stack  # noqa: E402,F401  (stack is a fixture)


def test_a_running_call_is_ended_when_the_days_seconds_run_out(stack, voice_on, monkeypatch):  # noqa: F811
    """The whole path: socket, gate, guard, the shared voice handler and its
    teardown. The call is let in, greets, and is then ended with the cap
    sentence the Call button shows; the line is free again afterwards."""
    import json

    client, state = stack
    monkeypatch.setattr(voice, "STEP_SEC", 1)
    monkeypatch.setenv("DEMO_VOICE_DAILY_SECONDS", "2")
    closed = None
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        ready = _open(ws)
        assert ready["type"] == "ready"
        for _ in range(400):
            message = ws.receive()
            if message.get("type") == "websocket.close":
                closed = message
                break
            if message.get("text"):
                json.loads(message["text"])
    assert closed is not None, "the call was never ended"
    assert closed["code"] == voice.CLOSE_CODE and closed["reason"] == voice.CAP_MESSAGE
    assert limits.used(voice.VOICE_BUCKET) == 2 and voice.open_calls() == 0
    # The day is used up: the next call is refused with the same sentence.
    with client.websocket_connect("/v1/agents/helpdesk/voice") as ws:
        again = ws.receive()
    assert again["type"] == "websocket.close" and again["reason"] == voice.CAP_MESSAGE
    assert len(state["built"]) == 1


# --------------------------------------------------------------------------
# Call recording in the public demo (DEMO_VOICE_RECORDING)
# --------------------------------------------------------------------------


def test_recording_is_off_unless_asked_and_only_for_portal_agents(monkeypatch):
    from demo_mode import assistant, settings, showcase, voice

    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_VOICE", "true")
    monkeypatch.delenv("DEMO_VOICE_RECORDING", raising=False)
    assert settings.voice_recording() is False
    assert voice.may_record("support-desk") is False
    assert "not recorded" in voice.note()

    monkeypatch.setenv("DEMO_VOICE_RECORDING", "true")
    assert voice.may_record("support-desk") is True and voice.may_record("sales-assistant") is True
    # The microphone in Build with AI and the product page's calls tell the
    # caller they are not recorded, so they never are.
    assert voice.may_record(assistant.BUILDER_SLUG) is False
    assert all(voice.may_record(slug) is False for slug in showcase.SLUGS) and showcase.SLUGS
    assert voice.may_record(None) is False and voice.may_record("") is False
    note = voice.note()
    assert "Calls are recorded" in note and "anyone visiting the demo" in note
    assert "not recorded" not in note


def test_lockdown_turns_recording_on_only_with_the_demo_setting():
    from demo_mode import settings

    env = {"DEMO_MODE": "1", "VOICE_RECORDING_ENABLED": "1"}
    settings.apply_process_lockdown(env)
    assert env["VOICE_RECORDING_ENABLED"] == "0"
    env = {"DEMO_MODE": "1", "DEMO_VOICE_RECORDING": "true"}
    settings.apply_process_lockdown(env)
    assert env["VOICE_RECORDING_ENABLED"] == "1"


def test_endpoint_gate_asks_the_demo_and_changes_nothing_outside_it(monkeypatch):
    from types import SimpleNamespace

    import endpoints.voice_live as live_ep

    def conn(slug):
        route = live_ep.VoiceRoute(agent_slug=slug, authenticate=lambda _a: {}) if slug else None
        return SimpleNamespace(state=SimpleNamespace(voice_route=route))

    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert live_ep._demo_may_record(conn(None)) is True          # not the demo: unchanged
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_VOICE", "true")
    monkeypatch.delenv("DEMO_VOICE_RECORDING", raising=False)
    assert live_ep._demo_may_record(conn("support-desk")) is False
    monkeypatch.setenv("DEMO_VOICE_RECORDING", "true")
    assert live_ep._demo_may_record(conn("support-desk")) is True
    assert live_ep._demo_may_record(conn("builder-assistant")) is False
    assert live_ep._demo_may_record(conn(None)) is False           # the plain socket


def test_reset_deletes_recordings():
    from demo_mode import reset

    tables = [t for t, _pk in reset._WIPE_ALL]
    assert "ai_voice_recordings" in tables and "ai_voice_recording_chunks" in tables
    assert tables.index("ai_voice_recording_chunks") < tables.index("ai_voice_recordings")


# --------------------------------------------------------------------------
# 7. Every agent can take a call: the seeded ones and the ones a visitor makes
# --------------------------------------------------------------------------


def _four_agents() -> Dict[str, list]:
    slugs = ["support-desk", "billing-helper", "sales-assistant", "it-helpdesk", "builder-assistant"]
    return {"agent_models": [],
            "agent_profiles": [{"profile_id": f"p-{s}", "slug": s, "voice_config": None} for s in slugs]}


def test_seed_gives_every_agent_a_caller_can_reach_a_voice(voice_on):
    db = _four_agents()
    model_id = seed.ensure_voice(_Client(db), "staging", {r["slug"]: r["profile_id"] for r in db["agent_profiles"]})
    by_slug = {r["slug"]: r["voice_config"] for r in db["agent_profiles"]}
    for slug in ("support-desk", "sales-assistant", "it-helpdesk"):
        assert by_slug[slug] == voice.voice_config(model_id), slug
    # A specialist (only the Support desk reaches it; the website channel
    # does not list it) and the Builder assistant (its own microphone): no Call.
    assert by_slug["billing-helper"] is None and by_slug["builder-assistant"] is None


def _template_db(voice_config=None) -> Dict[str, list]:
    return {
        "agent_models": [{"model_id": "m-voice", "name": voice.MODEL_NAME, "environment": "staging",
                          "is_active": True}],
        "agent_profiles": [{"profile_id": "tmpl-1", "agent_kind": "custom", "slug": "outage-mold",
                            "environment": "staging", "status": "template", "display_name": "Outage mold",
                            "voice_config": voice_config}],
        "agent_profile_mcp_servers": [], "agent_profile_skills": [],
        "agent_channels": [{"channel_id": "ch-web", "slug": "northwind-website",
                            "allowed_agent_kinds": ["custom:support-desk"]}],
    }


def _fork(db, monkeypatch):
    from unittest.mock import patch

    from admin.services import profiles

    monkeypatch.setattr(profiles, "record_change", lambda **kw: None, raising=False)
    monkeypatch.setattr(profiles, "invalidate_profile_loader", lambda *a: None, raising=False)
    with patch("core.auth.admin_service_client", return_value=_Client(db)):
        return profiles.fork_profile_row(profile_id="tmpl-1", actor_user_id="u", display_name="Outage helper",
                                         slug="outage-helper", exact_slug=True)


def test_an_agent_a_visitor_makes_starts_with_the_demo_voice(voice_on, monkeypatch):
    """A template used, a draft copy, the Builder assistant's work: all go
    through fork_profile_row, and the copy can take a call at once with the
    Support desk's voice settings."""
    new = _fork(_template_db(), monkeypatch)
    assert new["voice_config"] == voice.voice_config("m-voice")
    assert new["voice_config"]["enabled"] is True
    assert new["voice_config"]["max_session_sec"] == settings.voice_max_call_seconds()


def test_a_copy_keeps_its_own_voice(voice_on, monkeypatch):
    own = {"enabled": True, "model_id": "m-other", "voice_id": "Kore"}
    assert _fork(_template_db(own), monkeypatch)["voice_config"] == own


@pytest.mark.parametrize("missing", ["DEMO_VOICE", "DEMO_MODE"])
def test_no_voice_is_added_with_the_demo_voice_off(voice_on, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert _fork(_template_db(), monkeypatch).get("voice_config") is None


def test_the_test_panel_links_to_the_call_only_for_a_voice_agent_in_the_demo():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "admin" / "templates" / "agent_detail.html").read_text()
    assert "call_href:" in src and "#voice-call" in src
    assert "_dc.enabled and _dc.voice and (active_profile.voice_config or {}).get('enabled')" in src
    js = (Path(__file__).resolve().parents[2] / "admin" / "static" / "test-chat.js").read_text()
    assert 'cfg.call_href.indexOf("/admin/") === 0' in js


def test_the_portals_test_and_call_reach_an_agent_a_visitor_made(voice_on, monkeypatch):
    """The portal is served from the website channel's own address, which
    lists the agents it may reach: a visitor's own agent is added to it, or
    its Test chat and Call answer 403 (found on the live demo, 2026-11-25)."""
    db = _template_db()
    _fork(db, monkeypatch)
    assert db["agent_channels"][0]["allowed_agent_kinds"] == ["custom:support-desk", "custom:outage-helper"]
    # Once only, and never outside demo mode.
    seed.allow_on_website(_Client(db), "outage-helper")
    assert db["agent_channels"][0]["allowed_agent_kinds"].count("custom:outage-helper") == 1
    monkeypatch.delenv("DEMO_MODE")
    db = _template_db()
    _fork(db, monkeypatch)
    assert db["agent_channels"][0]["allowed_agent_kinds"] == ["custom:support-desk"]
