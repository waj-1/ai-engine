"""endpoints/agents_portal.py + the deployment-scope gating around it, on a
full build and on the demo's own standalone (scoped) build."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from endpoints import agents_portal
from tests.scoped_build import AGENTS_DEMO, ORCHARD, full_build, make_scope, use_agents_demo, use_scope

# Another tenant's scoped build, which must never serve the demo.
TENANT = ORCHARD
_OTHER = TENANT.key

SESSION = "0b5c1c2e-8f0a-4a8e-9d55-3d2a6f4f9a10"


@pytest.fixture(autouse=True)
def _isolate_global_ip_limiter():
    """main's ~100/min per-IP limiter is process-wide and every TestClient is
    the same IP — clear it around each test so these requests never 429 a
    later test file."""
    import main

    main.rate_limit_store.clear()
    yield
    main.rate_limit_store.clear()


@pytest.fixture
def client(monkeypatch):
    import main

    monkeypatch.setattr(agents_portal, "_voice_enabled", lambda kind, channel: kind != "agents_inquiries")
    agents_portal._minute.clear()
    agents_portal._day.clear()
    return TestClient(main.app)


def _events(text):
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


# ------------------------------------------------------------------- pages
def test_picker_and_workspace_pages(client):
    r = client.get("/agents")
    assert r.status_code == 200 and 'id="ap-picker"' in r.text
    assert "/static/shared/voice-live.js" in r.text and 'dir="ltr"' in r.text
    ar = client.get("/agents/collection?lang=ar")
    assert ar.status_code == 200 and 'dir="rtl"' in ar.text and 'data-agent="collection"' in ar.text
    assert client.get("/agents/billing").status_code == 404
    assert client.get("/agents/health").json() == {"ok": True}


def test_catalog(client):
    data = client.get("/agents/api/catalog").json()
    assert [a["key"] for a in data["agents"]] == ["support", "inquiries", "collection", "sales"]
    assert [a["voice"] for a in data["agents"]] == [True, False, True, True]
    assert data["voice_path"] == "/voice/live"  # full engine: the bare duplex mount
    assert data["brand"]["name_en"] and data["data_note"]["en"]


def test_cases_are_story_only(client):
    data = client.get("/agents/api/collection/cases").json()
    assert data["direction"] == "outbound" and len(data["cases"]) == 9
    case = data["cases"][0]
    assert {"headline_en", "summary_en", "summary_ar", "say_ar", "facts", "twist_en", "great_ar"} <= set(case)
    assert "emoji" not in case
    assert not {"brief", "customer_id", "outcome", "expected_tools"} & set(case)
    assert client.get("/agents/api/billing/cases").status_code == 404


# ------------------------------------------------------------------- chat
class _StubAgent:
    profile = SimpleNamespace(profile_id="stub", model_main={}, memory_config={})

    def __init__(self, state=None):
        self.state = state or {"verified": None}


def _stub_run_turn(captured):
    async def _run_turn(*, agent, message, identity, profile):
        captured.update(message=message, identity=identity, state_in=dict(agent.state))
        agent.state["verified"] = "41203317"
        yield {"type": "answer", "content": "Hi!"}
        yield {"type": "done"}
    return _run_turn


def test_chat_streams_and_returns_a_signed_state(client, monkeypatch):
    captured, built = {}, {}

    def _create(key, **kw):
        built.update(key=key, **kw)
        return _StubAgent(kw.get("state"))

    monkeypatch.setattr("runtime.agents.agents_demo.create_demo_agent", _create)
    monkeypatch.setattr("common.chat_session.run_turn", _stub_run_turn(captured))
    r = client.post("/agents/chat/stream", json={"agent": "support", "message": "hello", "session_id": SESSION})
    evts = _events(r.text)
    assert [e["type"] for e in evts] == ["answer", "state", "done"]
    # Inbound plays a case too: blank → the agent's first case.
    assert built["key"] == "support" and built["call_case"] == "S01" and built["state"] is None
    assert captured["identity"].agent_kind == "agents_support"
    assert captured["identity"].session_metadata["client"] == "agents_portal"
    token = evts[1]["token"]
    assert agents_portal.read_state(token, "support", "S01", SESSION) == {"verified": "41203317"}
    # The next turn hands the state back to the agent.
    r2 = client.post("/agents/chat/stream", json={"agent": "support", "message": "and?", "session_id": SESSION,
                                                  "state": token})
    assert r2.status_code == 200 and captured["state_in"] == {"verified": "41203317"}


def test_state_token_is_bound_to_its_conversation():
    token = agents_portal.sign_state("collection", "C01", SESSION, {"verified": True})
    assert agents_portal.read_state(token, "collection", "C01", SESSION) == {"verified": True}
    other = "11111111-2222-4333-8444-555555555555"
    assert agents_portal.read_state(token, "collection", "C01", other) is None
    assert agents_portal.read_state(token, "collection", "C02", SESSION) is None
    assert agents_portal.read_state(token, "sales", "C01", SESSION) is None
    assert agents_portal.read_state(token + "x", "collection", "C01", SESSION) is None
    assert agents_portal.read_state(None, "collection", "C01", SESSION) is None


def test_outbound_chat_defaults_and_validates_the_case(client, monkeypatch):
    built = {}

    def _create(key, **kw):
        built.update(key=key, **kw)
        if kw.get("call_case") == "C99":
            raise LookupError("unknown")
        return _StubAgent()

    monkeypatch.setattr("runtime.agents.agents_demo.create_demo_agent", _create)
    monkeypatch.setattr("common.chat_session.run_turn", _stub_run_turn({}))
    client.post("/agents/chat/stream", json={"agent": "collection", "message": "Hello?", "session_id": SESSION})
    assert built["call_case"] == "C01"
    r = client.post("/agents/chat/stream", json={"agent": "collection", "message": "Hello?",
                                                 "session_id": SESSION, "case_id": "C99"})
    assert r.status_code == 404


@pytest.mark.parametrize("body", [
    {"agent": "billing", "message": "x", "session_id": SESSION},
    {"agent": "support", "message": "", "session_id": SESSION},
    {"agent": "support", "message": "x" * 1001, "session_id": SESSION},
    {"agent": "support", "message": "x", "session_id": "not-a-uuid"},
    {"agent": "support", "message": "x", "session_id": SESSION, "locale": "fr"},
])
def test_chat_rejects_bad_input(client, body):
    assert client.post("/agents/chat/stream", json=body).status_code == 422


def test_chat_is_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(agents_portal, "_PER_MINUTE", 2)
    monkeypatch.setattr("runtime.agents.agents_demo.create_demo_agent", lambda key, **kw: _StubAgent())
    monkeypatch.setattr("common.chat_session.run_turn", _stub_run_turn({}))
    body = {"agent": "support", "message": "x", "session_id": SESSION}
    assert client.post("/agents/chat/stream", json=body).status_code == 200
    assert client.post("/agents/chat/stream", json=body).status_code == 200
    r = client.post("/agents/chat/stream", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


# ------------------------------------------------------ deployment-scope gating
def test_agents_build_serves_the_portal_and_blocks_other_clients(client, monkeypatch):
    use_agents_demo(monkeypatch)
    assert client.get("/agents").status_code == 200
    assert client.get("/agents/api/catalog").json()["voice_path"] == "/agents/voice/live"
    assert client.get("/" + _OTHER).status_code == 404
    assert client.post("/v1/agents/x/chat", json={"message": "x"}).status_code == 404


@pytest.mark.parametrize("other", [TENANT, make_scope("other")])
def test_other_client_builds_404_the_portal(client, monkeypatch, other):
    use_scope(monkeypatch, other, others=[AGENTS_DEMO])
    assert client.get("/agents").status_code == 404
    assert client.get("/agents/api/catalog").status_code == 404
    assert client.post("/agents/chat/stream", json={"agent": "support", "message": "x",
                                                    "session_id": SESSION}).status_code == 404


def test_route_guard_holds_without_the_gate_middleware(monkeypatch):
    """Defense in depth: each route re-checks, not only the middleware."""
    from fastapi import HTTPException

    use_scope(monkeypatch, TENANT, others=[AGENTS_DEMO])
    with pytest.raises(HTTPException):
        agents_portal._require_portal()
    use_agents_demo(monkeypatch)
    agents_portal._require_portal()


def test_voice_kind_gate_in_both_modes(monkeypatch):
    from endpoints.voice_live import _agent_kind_allowed

    full_build(monkeypatch)
    assert all(_agent_kind_allowed(k) for k in ("agents_support", "agents_collection", "sales"))
    use_agents_demo(monkeypatch)
    assert _agent_kind_allowed("agents_sales") and _agent_kind_allowed("agents_inquiries")
    assert not _agent_kind_allowed(_OTHER) and not _agent_kind_allowed("sales")
    use_scope(monkeypatch, TENANT, others=[AGENTS_DEMO])
    assert not _agent_kind_allowed("agents_support")


def test_demo_kinds_have_no_portal_session(monkeypatch):
    """Anonymous by design: even with another portal's cookie present, the
    demo kinds resolve no session, so the budget is IP-derived."""
    from endpoints.voice_live import _resolve_portal_session

    conn = SimpleNamespace(cookies={_OTHER + "_session": "x", "other_session": "y"})
    for kind in ("agents_support", "agents_inquiries", "agents_collection", "agents_sales"):
        assert _resolve_portal_session(kind, conn) is None


def test_voice_builders_registered():
    from runtime.agents.voice_registry import voice_agent_kinds, voice_agent_requires_auth

    for kind in ("agents_support", "agents_inquiries", "agents_collection", "agents_sales"):
        assert kind in voice_agent_kinds() and not voice_agent_requires_auth(kind)



# --------------------------------------------------------- duplex Origin gate
def _voice_harness(monkeypatch):
    import endpoints.voice_live as live_ep
    from fastapi import FastAPI
    from voice.live.base import AudioFormat
    from voice.live.config import LiveVoiceConfig
    from voice.live.testing import FakeRealtimeSession

    live_ep._connect_limiter._store.clear()
    live_ep._active.clear()
    channels = {"partner": {"channel_id": "c1", "slug": "partner",
                            "allowed_origins": ["https://partner.example"], "allowed_agent_kinds": []}}
    monkeypatch.setattr("core.channels.get_active_channels", lambda: dict(channels))
    monkeypatch.setattr(live_ep, "_live_config_for",
                        lambda conn, kind: LiveVoiceConfig(enabled=True, model_id="m", provider="fake"))
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: (None, None))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(live_ep, "build_agent_for_kind", lambda kind, params: (object(), object()))
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "persona")
    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app)


def _first_frame(client, origin):
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect("/voice/live?agentKind=agents_support",
                                      headers={"origin": origin}) as ws:
            return ws.receive_json()
    except WebSocketDisconnect as exc:
        return {"closed": exc.code}


def test_origin_gate_lets_this_engines_own_pages_through(monkeypatch):
    """With the opt-in gate ACTIVE (a channel lists allowed_origins), the
    portal's own page — same host as the socket — still connects."""
    client = _voice_harness(monkeypatch)
    assert _first_frame(client, "http://testserver")["type"] == "ready"


def test_origin_gate_still_rejects_a_foreign_origin(monkeypatch):
    from voice.live.protocol import CLOSE_FORBIDDEN

    client = _voice_harness(monkeypatch)
    assert _first_frame(client, "https://evil.example") == {"closed": CLOSE_FORBIDDEN}
    assert _first_frame(client, "http://testserver.evil.example") == {"closed": CLOSE_FORBIDDEN}


def test_same_origin_helper():
    from endpoints.voice_live import _same_origin

    ws = SimpleNamespace(headers={"host": "engine.example"})
    assert _same_origin(ws, "https://engine.example")
    assert not _same_origin(ws, "https://engine.example.attacker.test")
    assert not _same_origin(ws, "null") and not _same_origin(ws, None)



def test_public_cases_carry_extra_suggestions_without_identity_numbers():
    """`say_more_*` feeds the landing page's suggestion carousel: exposed by the
    public API, mirrored EN/AR, and never an identity detail (those stay in
    `say_*`, pinned)."""
    import re

    from endpoints.agents_portal import public_cases

    seen = 0
    for key in ("collection", "sales", "support", "inquiries"):
        for case in public_cases(key):
            if "say_more_en" not in case:
                continue
            seen += 1
            assert len(case["say_more_en"]) == len(case["say_more_ar"]) >= 3
            for line in case["say_more_en"] + case["say_more_ar"]:
                assert line.strip() and not re.search(r"\d{4}", line), (case["id"], line)
    assert seen >= 12
