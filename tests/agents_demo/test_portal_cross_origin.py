"""The public /agents API called cross-origin by the landing page (the React
portal on https://company.example / https://www.staging.example): preflight + real requests
through the exact CORS middleware prod installs, the WAJ Landing channel, and
the inbound case carried on the chat request."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.cors import FirstPartyCORSMiddleware
from endpoints import agents_portal

SESSION = "0b5c1c2e-8f0a-4a8e-9d55-3d2a6f4f9a10"
LANDING = {"slug": "waj_landing", "kind": "waj_landing", "allowed_origins": [
    "https://staging.example", "https://www.staging.example", "https://company.example", "https://www.company.example"],
    "allowed_agent_kinds": [], "is_active": True}


@pytest.fixture
def app(monkeypatch):
    """The portal router behind the allow-list middleware main.py installs
    when ALLOWED_ORIGINS is an explicit list (prod): an explicit list that
    does NOT name company.example, so the first-party rule is what lets it through."""
    monkeypatch.setattr("core.channel_resolver._channels_snapshot", lambda: {"waj_landing": dict(LANDING)})
    monkeypatch.setattr(agents_portal, "_voice_enabled", lambda kind, channel: True)
    agents_portal._minute.clear()
    agents_portal._day.clear()
    application = FastAPI()
    application.include_router(agents_portal.router)
    application.add_middleware(FirstPartyCORSMiddleware, allow_origins=["https://engine.company.example"],
                               allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
                               dynamic_origins=lambda: {"https://tenant-shop.com"})
    return application


@pytest.mark.parametrize("origin", ["https://company.example", "https://www.company.example", "https://www.staging.example"])
def test_preflight_for_the_json_chat_post(app, origin):
    r = TestClient(app).options("/agents/chat/stream", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    assert "POST" in r.headers["access-control-allow-methods"]
    assert "content-type" in r.headers["access-control-allow-headers"].lower()


def test_catalog_and_cases_are_readable_cross_origin(app):
    client = TestClient(app)
    for path in ("/agents/api/catalog", "/agents/api/support/cases", "/agents/api/inquiries/cases"):
        r = client.get(path, headers={"Origin": "https://company.example"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "https://company.example", path
    cases = client.get("/agents/api/support/cases", headers={"Origin": "https://company.example"}).json()
    assert cases["direction"] == "inbound" and cases["cases"][0]["id"] == "S01"
    assert {"facts", "say_en", "say_ar", "title_en"} <= set(cases["cases"][0])
    assert "customer_id" not in cases["cases"][0] and "brief" not in cases["cases"][0]


def test_a_foreign_origin_gets_no_cors_grant(app):
    client = TestClient(app)
    pre = client.options("/agents/chat/stream", headers={
        "Origin": "https://company.example.evil.com", "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 400 and "access-control-allow-origin" not in pre.headers
    get = client.get("/agents/api/catalog", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in get.headers
    # Tenant custom domains still resolve at request time.
    ok = client.get("/agents/api/catalog", headers={"Origin": "https://tenant-shop.com"})
    assert ok.headers["access-control-allow-origin"] == "https://tenant-shop.com"


def test_cross_origin_inbound_chat_carries_the_case(app, monkeypatch):
    built = {}

    class _Agent:
        profile = None
        state = {}

    def _create(key, **kw):
        built.update(key=key, **kw)
        return _Agent()

    async def _run_turn(*, agent, message, identity, profile):
        yield {"type": "answer", "content": "hi"}
        yield {"type": "done"}

    monkeypatch.setattr("runtime.agents.agents_demo.create_demo_agent", _create)
    monkeypatch.setattr("common.chat_session.run_turn", _run_turn)
    r = TestClient(app).post("/agents/chat/stream", headers={"Origin": "https://company.example"}, json={
        "agent": "inquiries", "message": "hi", "session_id": SESSION, "locale": "ar", "case_id": "i04"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "https://company.example"
    assert built["call_case"] == "I04" and built["locale"] == "ar"
    events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    token = next(e["token"] for e in events if e["type"] == "state")
    assert agents_portal.read_state(token, "inquiries", "I04", SESSION) == {}


def test_unknown_inbound_case_is_a_404(app, monkeypatch):
    def _create(key, **kw):
        raise LookupError("unknown case")

    monkeypatch.setattr("runtime.agents.agents_demo.create_demo_agent", _create)
    r = TestClient(app).post("/agents/chat/stream", json={
        "agent": "support", "message": "hi", "session_id": SESSION, "case_id": "S99"})
    assert r.status_code == 404


def test_the_landing_channel_must_allow_the_demo_kinds(app, monkeypatch):
    """An operator who narrows WAJ Landing's allowed_agent_kinds must list the
    four agents_* kinds, or the landing page's chat is refused."""
    narrowed = dict(LANDING, allowed_agent_kinds=["sales"])
    monkeypatch.setattr("core.channel_resolver._channels_snapshot", lambda: {"waj_landing": narrowed})
    r = TestClient(app).post("/agents/chat/stream", headers={"Origin": "https://company.example"}, json={
        "agent": "support", "message": "hi", "session_id": SESSION})
    assert r.status_code == 403


def test_main_app_answers_the_landing_preflight():
    """Whichever CORS mode the process booted in (ALLOWED_ORIGINS='*' in
    tests, an explicit list in prod), the real app grants company.example."""
    import main

    main.rate_limit_store.clear()
    r = TestClient(main.app).options("/agents/chat/stream", headers={
        "Origin": "https://company.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] in ("https://company.example", "*")
