"""The product-page showcase (demo_mode/showcase.py, ``DEMO_SHOWCASE``).

What these pin:

1. Off by default: with ``DEMO_MODE`` on and ``DEMO_SHOWCASE`` unset every
   showcase route is a 404 (the socket closes 1008), and nothing is seeded.
2. Only the four use-case agents and their curated cases, and only from an
   allowed Origin; a missing or other Origin is refused before any model.
3. Its own caps: a chat turn needs room under the visitor's hour and the
   showcase's day; a call needs voice seconds left, a free line and room in
   the visitor's hour, and is cut at its length. The day's totals and the
   lines at once hold whatever address a caller claims.
4. A call goes through the shared voice handler to the agent named by the
   case's key, with the case file handed to the brain and the case's own
   greeting (simulated provider).
5. The seed builds the agents the platform's way (instructions, model,
   knowledge, voice per accent), is idempotent and puts back a changed agent;
   the portal cannot change them or their channel.
6. The portal links to the product page when one is set.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest
from starlette.websockets import WebSocketDisconnect

from demo_mode import limits, protect, seed, settings, showcase

ORIGIN = "https://product.example"


def _raise(*_a, **_k):
    raise RuntimeError("no database in tests")


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    for name in ("DEMO_SHOWCASE", "DEMO_VOICE", "DEMO_SHOWCASE_PAGE_URL", "DEMO_CLIENT_IP_HEADER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEMO_PUBLIC_URL", "https://demo.example")
    limits.reset_for_tests()
    showcase.reset_for_tests()
    protect.reset_for_tests()
    monkeypatch.setattr(limits, "_db_take", _raise)
    monkeypatch.setattr(limits, "_db_used", _raise)
    yield
    limits.reset_for_tests()
    showcase.reset_for_tests()
    protect.reset_for_tests()


@pytest.fixture
def on(demo, monkeypatch):
    monkeypatch.setenv("DEMO_SHOWCASE", "true")
    monkeypatch.setenv("DEMO_SHOWCASE_ORIGINS", f"{ORIGIN}, https://www.product.example/")
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    monkeypatch.setattr(showcase, "agent_page_ids", lambda: {"support": "p-support"})


@pytest.fixture
def voice_on(on, monkeypatch):
    monkeypatch.setenv("DEMO_VOICE", "true")
    monkeypatch.setenv("DEMO_VOICE_ENDPOINT", "https://demo-voice.services.ai.azure.com/")
    monkeypatch.setenv(settings.VOICE_KEY_ENV, "test-voice-key")


# --------------------------------------------------------------------------
# 1. The switch
# --------------------------------------------------------------------------


def test_showcase_is_off_by_default(client, demo):
    assert settings.showcase_enabled() is False
    assert client.get("/showcase/api/catalog").status_code == 404
    assert client.get("/showcase/api/support/cases").status_code == 404
    resp = client.post("/showcase/api/chat", headers={"Origin": ORIGIN},
                       json={"agent": "support", "case_id": "S04", "message": "hi",
                             "session_id": "0" * 32})
    assert resp.status_code == 404
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/showcase/api/support/voice?case=S04"):
            pass
    assert closed.value.code == 1008


def test_showcase_means_nothing_outside_demo_mode(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("DEMO_SHOWCASE", "true")
    monkeypatch.setenv("DEMO_VOICE", "true")
    assert settings.showcase_enabled() is False
    assert settings.showcase_voice_enabled() is False


def test_voice_follows_demo_voice_and_can_be_switched_off_alone(on, monkeypatch):
    assert settings.showcase_voice_enabled() is False  # no DEMO_VOICE
    monkeypatch.setenv("DEMO_VOICE", "true")
    assert settings.showcase_voice_enabled() is True
    monkeypatch.setenv("DEMO_SHOWCASE_VOICE", "off")
    assert settings.showcase_voice_enabled() is False


# --------------------------------------------------------------------------
# 2. What is public
# --------------------------------------------------------------------------


def test_catalog_lists_the_four_agents_and_what_voice_can_do(client, voice_on):
    body = client.get("/showcase/api/catalog").json()
    assert [a["key"] for a in body["agents"]] == ["collection", "sales", "support", "inquiries"]
    assert {a["slug"] for a in body["agents"]} == set(showcase.SLUGS)
    support = next(a for a in body["agents"] if a["key"] == "support")
    assert support["platform_url"] == "https://demo.example/admin/agents?env=staging&profile_id=p-support"
    assert body["voice"] == {"state": "open", "max_call_seconds": 150}
    assert body["chat"] == {"state": "open"}
    assert body["platform"]["repo_url"] == settings.repo_url()


def test_cases_are_the_curated_ones_and_carry_no_em_dash(client, on):
    for key, ids in showcase.CASES.items():
        cases = client.get(f"/showcase/api/{key}/cases").json()["cases"]
        assert [c["id"] for c in cases] == ids
        assert "—" not in json.dumps(cases, ensure_ascii=False)
        for card in cases:
            assert "brief" not in card and "customer_id" not in card  # internal fields stay in
    assert client.get("/showcase/api/billing/cases").status_code == 404


def test_the_case_file_carries_the_answers_and_no_tool_names(on):
    for key, ids in showcase.CASES.items():
        for case_id in ids:
            for locale in ("en-US", "ar-AE"):
                text = showcase.brief(key, case_id, locale)
                assert "verify_identity" not in text and "verify_customer" not in text
                assert "This conversation" not in text  # the agent adds the heading
    support = showcase.brief("support", "S04", "en-GB")
    assert "41209908" in support and "British" in support
    assert "Emirati dialect" in showcase.brief("collection", "C01", "ar-AE")
    with pytest.raises(LookupError):
        showcase.brief("support", "S01", "en-US")  # a fixture case, not a curated one


def test_greeting_is_the_cases_own_and_names_the_customer_on_an_outbound_call(on):
    assert "Yousef" in showcase.greeting("collection", "C01", "en-US")
    assert "يوسف" in showcase.greeting("collection", "C01", "ar-EG")
    assert "Egyptian" in showcase.greeting("collection", "C01", "ar-EG")
    assert "how you can help" in showcase.greeting("support", "S04", "en-US")


# --------------------------------------------------------------------------
# 3a. Chat: origin, case, caps
# --------------------------------------------------------------------------


class _Agent:
    profile = None


@pytest.fixture
def fake_turn(on, monkeypatch):
    """The agent and its turn, faked at the edge: what the route handed over."""
    seen: List[Dict[str, Any]] = []
    from runtime.agents import custom

    def _create(slug, **kw):
        seen.append({"slug": slug, **kw})
        return _Agent()

    async def _run_turn(*, agent, message, identity, profile, **kw):
        seen[-1]["identity"] = identity
        yield {"type": "answer", "content": f"echo: {message}"}
        yield {"type": "done"}

    import common.chat_session as chat_session

    monkeypatch.setattr(custom, "create_custom_agent", _create)
    monkeypatch.setattr(chat_session, "run_turn", _run_turn)
    return seen


def _chat(client, *, origin: Optional[str] = ORIGIN, agent="support", case="S04", ip=None, **body):
    headers = {"Origin": origin} if origin else {}
    if ip:
        headers["x-visitor"] = ip
    return client.post("/showcase/api/chat", headers=headers, json={
        "agent": agent, "case_id": case, "message": body.get("message", "Hello"),
        "session_id": body.get("session_id", "1" * 32), "locale": body.get("locale", "en-US")})


def test_chat_runs_the_cases_agent_with_its_case_file(client, fake_turn):
    resp = _chat(client, agent="collection", case="C01", locale="ar-AE")
    assert resp.status_code == 200 and "echo: Hello" in resp.text
    turn = fake_turn[-1]
    assert turn["slug"] == "falak-collections" and turn["locale"] == "ar-AE"
    assert "COL-" in turn["brief"] and "Emirati" in turn["brief"]
    assert turn["identity"].user_id is None
    assert turn["identity"].session_metadata == {"agent_slug": "falak-collections",
                                                 "client": "showcase", "case": "C01"}


@pytest.mark.parametrize("origin", [None, "https://evil.example", "https://product.example.evil.example"])
def test_chat_from_another_origin_is_refused_before_any_model(client, fake_turn, origin):
    resp = _chat(client, origin=origin)
    assert resp.status_code == 403 and fake_turn == []
    assert limits.used(showcase.TURNS_BUCKET) == 0


def test_the_allowed_origins_include_the_demo_itself_and_ignore_a_trailing_slash(client, fake_turn):
    assert _chat(client, origin="https://www.product.example").status_code == 200
    assert _chat(client, origin="https://demo.example").status_code == 200


def test_chat_with_another_agent_or_case_is_refused(client, fake_turn):
    assert _chat(client, agent="builder").status_code == 422
    assert _chat(client, case="S01").status_code == 404  # real fixture, not curated
    assert _chat(client, agent="support", case="C01").status_code == 404
    assert fake_turn == []


def test_a_visitor_is_limited_per_hour(client, fake_turn, monkeypatch):
    monkeypatch.setenv("DEMO_SHOWCASE_CHAT_PER_HOUR", "2")
    monkeypatch.setenv("DEMO_IP_MULTIPLIER", "1")
    assert _chat(client).status_code == 200
    assert _chat(client).status_code == 200
    resp = _chat(client)
    assert resp.status_code == 429 and resp.json()["code"] == "rate"


def test_the_day_holds_whatever_address_a_caller_claims(client, fake_turn, monkeypatch):
    """With the client-address header trusted, a caller who makes up a new
    address for every message gets a fresh hour each time, but never a
    fresh day."""
    monkeypatch.setenv("DEMO_CLIENT_IP_HEADER", "x-visitor")
    monkeypatch.setenv("DEMO_SHOWCASE_DAILY_TURNS", "3")
    monkeypatch.setenv("DEMO_SHOWCASE_CHAT_PER_HOUR", "1")
    for n in range(3):
        assert _chat(client, ip=f"198.51.100.{n}").status_code == 200
    resp = _chat(client, ip="198.51.100.99")
    assert resp.status_code == 429 and resp.json()["code"] == "budget"
    assert len(fake_turn) == 3
    assert client.get("/showcase/api/catalog").json()["chat"]["state"] == "budget"


def test_the_showcase_day_is_its_own(client, fake_turn, monkeypatch):
    """The portal's daily turns and the showcase's are separate counters."""
    monkeypatch.setenv("DEMO_DAILY_TURNS", "1")
    limits.take_turn()
    assert limits.turns_left() == 0
    assert _chat(client).status_code == 200


# --------------------------------------------------------------------------
# 3b. Voice: origin, case, caps
# --------------------------------------------------------------------------


def _closes(client, url, origin: Optional[str] = ORIGIN, headers=None):
    hdrs = dict(headers or {})
    if origin:
        hdrs["origin"] = origin
    with client.websocket_connect(url, headers=hdrs) as ws:
        message = ws.receive()
    assert message["type"] == "websocket.close", message
    return message


@pytest.fixture
def serve_spy(voice_on, monkeypatch):
    """The shared handler, faked: which agent, brief and greeting it was handed."""
    import endpoints.voice_live as live_ep

    seen: List[Any] = []

    async def _serve(websocket, route, **kw):
        seen.append((route, kw))
        await websocket.accept()
        await websocket.close(code=4000, reason="served")

    monkeypatch.setattr(live_ep, "serve_voice_route", _serve)
    return seen


def test_a_call_reaches_the_cases_agent_with_its_brief_and_greeting(client, serve_spy):
    closed = _closes(client, "/showcase/api/collection/voice?case=C06&locale=ar-SA")
    assert closed["reason"] == "served"
    route, kw = serve_spy[0]
    assert route.agent_slug == "falak-collections"
    assert "Saudi" in route.brief and route.greeting and "AI assistant" in route.greeting
    assert kw["locale"] == "ar-SA" and kw["agent_kind"] == "custom"
    assert showcase.open_calls() == 0


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_a_call_from_another_origin_is_refused(client, serve_spy, origin):
    closed = _closes(client, "/showcase/api/support/voice?case=S04", origin=origin)
    assert closed["code"] == showcase.CLOSE_CODE and closed["reason"] == showcase.MSG_ORIGIN
    assert serve_spy == []


def test_a_call_to_another_agent_or_case_is_refused(client, serve_spy):
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/showcase/api/builder/voice?case=S04", headers={"origin": ORIGIN}):
            pass
    assert closed.value.code == 1008
    assert _closes(client, "/showcase/api/support/voice?case=C01")["reason"] == "Unknown use case."
    assert serve_spy == []


def test_a_call_is_refused_when_voice_is_off(client, on, serve_spy, monkeypatch):
    monkeypatch.setenv("DEMO_SHOWCASE_VOICE", "off")
    assert _closes(client, "/showcase/api/support/voice?case=S04")["reason"] == showcase.MSG_OFF
    assert client.get("/showcase/api/catalog").json()["voice"]["state"] == "off"


def test_a_call_is_refused_once_the_days_seconds_are_used(client, serve_spy, monkeypatch):
    monkeypatch.setenv("DEMO_SHOWCASE_VOICE_DAILY_SECONDS", "20")
    limits.take(showcase.VOICE_BUCKET, 20, 10)  # 10 left: under one step
    assert _closes(client, "/showcase/api/support/voice?case=S04")["reason"] == showcase.MSG_BUDGET
    assert client.get("/showcase/api/catalog").json()["voice"]["state"] == "budget"
    assert serve_spy == []


def test_the_showcase_voice_day_is_apart_from_the_portals(voice_on, monkeypatch):
    from demo_mode import voice

    monkeypatch.setenv("DEMO_VOICE_DAILY_SECONDS", "15")
    limits.take(voice.VOICE_BUCKET, 15, 15)
    assert voice.seconds_left() == 0 and showcase.voice_seconds_left() == 2700


def test_a_visitor_is_limited_per_hour_but_new_addresses_get_no_new_day(client, serve_spy, monkeypatch):
    monkeypatch.setenv("DEMO_CLIENT_IP_HEADER", "x-visitor")
    monkeypatch.setenv("DEMO_IP_MULTIPLIER", "1")
    monkeypatch.setenv("DEMO_SHOWCASE_VOICE_CALLS_PER_HOUR", "1")
    url = "/showcase/api/support/voice?case=S04"
    assert _closes(client, url, headers={"x-visitor": "198.51.100.1"})["reason"] == "served"
    assert _closes(client, url, headers={"x-visitor": "198.51.100.1"})["reason"] == showcase.MSG_RATE
    # A made-up address gets a fresh hour...
    assert _closes(client, url, headers={"x-visitor": "198.51.100.2"})["reason"] == "served"
    # ...but not a fresh day: the seconds are the showcase's, whoever calls.
    monkeypatch.setenv("DEMO_SHOWCASE_VOICE_DAILY_SECONDS", str(showcase.STEP_SEC))
    limits.take(showcase.VOICE_BUCKET, 10 ** 6, 1)
    assert _closes(client, url, headers={"x-visitor": "198.51.100.3"})["reason"] == showcase.MSG_BUDGET


class _Socket:
    def __init__(self):
        self.scope = {"type": "websocket", "headers": [], "client": ("203.0.113.7", 1234)}
        self.accepted = False
        self.closed: Optional[tuple] = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        if self.closed is None:
            self.closed = (code, reason)


class _Clock:
    """Virtual time (as in test_demo_voice): a sleeper wakes when the clock
    reaches its time, and the clock only jumps to the earliest sleeper."""

    def __init__(self):
        self.now = 0.0
        self.waiting: List[float] = []

    async def sleep(self, seconds):
        wake = self.now + seconds
        self.waiting.append(wake)
        try:
            while self.now < wake:
                await asyncio.sleep(0)
                if self.now < wake and min(self.waiting) == wake:
                    self.now = wake
        finally:
            self.waiting.remove(wake)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(showcase, "_sleep", c.sleep)
    monkeypatch.setattr(showcase, "_now", lambda: c.now)
    return c


async def test_every_line_busy_whatever_the_address(voice_on, monkeypatch):
    monkeypatch.setenv("DEMO_SHOWCASE_VOICE_MAX_CALLS", "1")
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    first = asyncio.ensure_future(showcase.guard(_Socket(), "198.51.100.1", hold))
    for _ in range(200):
        if showcase.open_calls() == 1:
            break
        await asyncio.sleep(0.01)
    assert showcase.open_calls() == 1
    second = _Socket()
    await showcase.guard(second, "198.51.100.2", hold)  # another address, same lines
    assert second.closed == (showcase.CLOSE_CODE, showcase.MSG_BUSY)
    gate.set()
    await first
    assert showcase.open_calls() == 0


async def test_a_call_is_cut_at_its_length_and_charged(voice_on, clock, monkeypatch):
    monkeypatch.setenv("DEMO_SHOWCASE_VOICE_MAX_CALL_SECONDS", "45")

    async def forever():
        await asyncio.Event().wait()

    ws = _Socket()
    await showcase.guard(ws, "198.51.100.1", forever)
    assert ws.closed == (showcase.CLOSE_CODE, showcase.length_message())
    assert ws.closed[1].startswith("[length]")
    assert limits.used(showcase.VOICE_BUCKET) == 45 and showcase.open_calls() == 0


async def test_a_running_call_ends_when_the_day_runs_out(voice_on, clock, monkeypatch):
    monkeypatch.setenv("DEMO_SHOWCASE_VOICE_DAILY_SECONDS", "30")

    async def forever():
        await asyncio.Event().wait()

    ws = _Socket()
    await showcase.guard(ws, "198.51.100.1", forever)
    assert ws.closed == (showcase.CLOSE_CODE, showcase.MSG_BUDGET)
    assert limits.used(showcase.VOICE_BUCKET) == 30


async def test_a_short_call_is_still_charged(voice_on, monkeypatch):
    """A call shorter than one step pays for what it lasted."""
    now = {"t": 0.0}

    async def never(_seconds):
        await asyncio.Event().wait()

    monkeypatch.setattr(showcase, "_sleep", never)
    monkeypatch.setattr(showcase, "_now", lambda: now["t"])

    async def short():
        now["t"] = 7.0

    await showcase.guard(_Socket(), "198.51.100.1", short)
    assert limits.used(showcase.VOICE_BUCKET) == 7


# --------------------------------------------------------------------------
# 4. A real call on the simulated provider
# --------------------------------------------------------------------------

from tests.voice.test_custom_agent_voice import _open, _profile, _until, stack  # noqa: E402,F401


def test_a_showcase_call_greets_with_the_case_and_hands_its_file_to_the_brain(stack, voice_on, monkeypatch):  # noqa: F811
    client, state = stack
    state["profiles"]["falak-support"] = _profile("falak-support", profile_id="p-falak")
    from runtime.agents import custom

    built: List[Dict[str, Any]] = []
    real = custom.create_custom_agent

    def _create(slug, **kw):
        built.append({"slug": slug, **kw})
        return real(slug, **kw)

    monkeypatch.setattr(custom, "create_custom_agent", _create)
    with client.websocket_connect("/showcase/api/support/voice?case=S04&locale=en-GB",
                                  headers={"origin": ORIGIN}) as ws:
        ready = _open(ws, token=showcase.CALL_TOKEN)
        assert ready["type"] == "ready" and ready["greeting"] is True
        _until(ws, lambda f: f["type"] == "turn_end")
        ws.send_json({"type": "text", "text": "I was charged twice"})
        _until(ws, lambda f: f["type"] == "transcript" and f["role"] == "assistant"
               and not f.get("out_of_band"))
    assert built and built[-1]["slug"] == "falak-support"
    assert "41209908" in built[-1]["brief"] and built[-1]["locale"] == "en-GB"
    assert showcase.open_calls() == 0 and limits.used(showcase.VOICE_BUCKET) >= 0


def test_a_showcase_call_with_another_token_is_refused(stack, voice_on):  # noqa: F811
    client, state = stack
    state["profiles"]["falak-support"] = _profile("falak-support", profile_id="p-falak")
    with client.websocket_connect("/showcase/api/support/voice?case=S04",
                                  headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "auth", "token": "good-token"})
        message = ws.receive()
    assert message["type"] == "websocket.close" and message["reason"] == "Authentication required."


def test_the_brief_reaches_the_agents_turn_context(on, monkeypatch):
    from runtime.agents.custom import CustomRuntimeAgent

    agent = CustomRuntimeAgent.__new__(CustomRuntimeAgent)
    agent.locale, agent.brief = "en", "Case: Khalid, account 41209908."
    assert "## This conversation\nCase: Khalid" in agent._turn_context(False, None)
    agent.brief = None
    assert "This conversation" not in agent._turn_context(False, None)


def test_a_regional_tag_picks_its_own_voice_first():
    from voice.live.config import LiveVoiceConfig, resolve_session_voice

    cfg = LiveVoiceConfig()
    cfg.voice_id = "fallback"
    cfg.voice_by_language = {"ar": "ar-AE-FatimaNeural", "ar-EG": "ar-EG-SalmaNeural"}
    cfg.language = "ar-EG"
    assert resolve_session_voice(cfg) == "ar-EG-SalmaNeural"
    cfg.language = "ar-SA"
    assert resolve_session_voice(cfg) == "ar-AE-FatimaNeural"
    cfg.language = "en-US"
    assert resolve_session_voice(cfg) == "fallback"


# --------------------------------------------------------------------------
# 5. The seed and the portal
# --------------------------------------------------------------------------

from tests.admin.test_demo_mode import _Client  # noqa: E402


@pytest.fixture
def db(demo, monkeypatch):
    from admin import kb_handlers
    from core.agent_profile import AgentProfileLoader

    monkeypatch.setattr(seed, "_env", lambda: "staging")
    monkeypatch.setattr(kb_handlers, "embedding_status", lambda: {"ready": False, "problem": "none"})
    monkeypatch.setattr(AgentProfileLoader, "invalidate", classmethod(lambda cls, *a, **k: None))
    return {}


def test_nothing_is_seeded_while_the_showcase_is_off(db):
    seed.seed(_Client(db))
    assert not [p for p in db["agent_profiles"] if p["slug"] in showcase.SLUGS]
    assert not [c for c in db.get("agent_channels", []) if c["slug"] == showcase.CHANNEL["slug"]]


def test_the_seed_builds_the_four_agents_the_platforms_way(db, voice_on):
    client = _Client(db)
    out = seed.seed(client)
    agents = {p["slug"]: p for p in db["agent_profiles"] if p["slug"] in showcase.SLUGS}
    assert set(agents) == set(showcase.SLUGS)
    kb = next(d for d in db["agent_data_sources"] if d["name"] == showcase.KB_NAME)
    assert len([p for p in db["ai_kb_pages"] if p["source_id"] == kb["source_id"]]) == 24
    category = next(c for c in db["agent_types"] if c["slug"] == showcase.CATEGORY["slug"])
    for slug, row in agents.items():
        agent = showcase.AGENT_BY_SLUG[slug]
        assert row["status"] == "active" and row["is_active"] and row["tool_ids"] == []
        assert row["model_main_id"] == out["models"]["agent"]
        assert row["agent_type_id"] == category["agent_type_id"]
        assert row["data_source_ids"] == ([kb["source_id"]] if agent.direction == "inbound" else [])
        voice = row["voice_config"]
        assert voice["enabled"] and voice["model_id"] == out["voice_model_id"]
        assert voice["max_session_sec"] == 150
        assert set(showcase.SPEECH_TAGS) <= set(voice["voice_by_language"])
    support = agents["falak-support"]
    version = next(v for v in db["agent_prompt_versions"]
                   if v["prompt_version_id"] == support["prompt_version_ids"]["system"])
    assert "Falak Connect" in version["content"] and "{brand}" not in version["content"]
    channel = next(c for c in db["agent_channels"] if c["slug"] == showcase.CHANNEL["slug"])
    assert channel["allowed_origins"] == [ORIGIN, "https://www.product.example"]
    assert sorted(channel["allowed_agent_kinds"]) == sorted(f"custom:{s}" for s in showcase.SLUGS)
    own = next(c for c in db["agent_channels"] if c["slug"] == "northwind-website")
    assert {f"custom:{s}" for s in showcase.SLUGS} <= set(own["allowed_agent_kinds"])
    assert out["showcase"]["channel_id"] == channel["channel_id"]


def test_the_seed_is_idempotent_and_puts_a_changed_agent_back(db, voice_on):
    client = _Client(db)
    seed.seed(client)
    counts = {t: len(r) for t, r in db.items() if not t.startswith("_")}
    support = next(p for p in db["agent_profiles"] if p["slug"] == "falak-support")
    support["voice_config"] = {"enabled": False}
    support["data_source_ids"] = []
    seed.seed(client)
    assert {t: len(r) for t, r in db.items() if not t.startswith("_")} == counts
    support = next(p for p in db["agent_profiles"] if p["slug"] == "falak-support")
    assert support["voice_config"]["enabled"] is True and support["data_source_ids"]


def test_the_portal_cannot_change_the_showcase(client, on, monkeypatch):
    agent_id = "11111111-2222-3333-4444-555555555555"
    channel_id = "66666666-2222-3333-4444-555555555555"
    monkeypatch.setattr(protect, "_lookup", lambda: {agent_id, channel_id})
    monkeypatch.setattr(protect, "showcase_ids", lambda: frozenset({agent_id, channel_id}))
    for path in (f"/admin/agents/{agent_id}/instructions", f"/admin/profiles/{agent_id}",
                 f"/admin/channels/{channel_id}", f"/admin/channels/{channel_id}/delete"):
        resp = client.post(path, follow_redirects=False)
        assert resp.status_code == 403 and "product page" in resp.text, path
    assert protect.refusal_for_path("GET", f"/admin/channels/{channel_id}") is None


# --------------------------------------------------------------------------
# 6. Links
# --------------------------------------------------------------------------


def test_banner_links_to_the_product_page_only_when_one_is_set(client, on, monkeypatch):
    assert "data-demo-showcase" not in client.get("/admin/settings").text
    monkeypatch.setenv("DEMO_SHOWCASE_PAGE_URL", "https://product.example/engine#voice")
    page = client.get("/admin/settings").text
    assert "data-demo-showcase" in page and "https://product.example/engine#voice" in page
