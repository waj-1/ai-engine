"""Demo mode (demo_mode/, ``DEMO_MODE``): the public one-click demo.

What these pin:

1. Off by default: with ``DEMO_MODE`` unset every demo route 404s and every
   locked screen is reachable as before.
2. One-click entry: the sign-in page offers "Try the demo" and no form;
   `/` and `/admin` send a signed-out visitor there; the button signs in as
   the demo admin; the first-run page never appears; every portal page has
   the banner and the builder assistant's panel.
3. Lock-down: every locked route is refused, GET and POST, and allowed
   outside demo mode; `/mcp` is refused and the process lock-down removes
   ``ENABLE_ADMIN_MCP``; a tool server at an internal address is refused and
   no process secret is ever sent to a public one.
4. The builder assistant's allow-list: a forbidden tool, action or server
   address is refused even when the model asks for it, and an argument
   outside the list never reaches the tool.
5. Caps: per-visitor limits, the daily cap on agent chats, assistant turns
   and test-judge calls.
6. The nightly reset wipes what visitors made and the seed is idempotent.
7. The in-process Northwind tool server.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Dict, List

import pytest

from demo_mode import assistant, gate, limits, northwind, policy, reset, seed, settings
from tests import fake_pg


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_PUBLIC_URL", "https://demo.example.com")
    limits.reset_for_tests()
    # The daily counter is the database's; here it falls back to memory.
    monkeypatch.setattr(limits, "_db_take", _raise)
    monkeypatch.setattr(limits, "_db_used", _raise)
    yield
    limits.reset_for_tests()


def _raise(*_a, **_k):
    raise RuntimeError("no database in tests")


def _refused(resp) -> bool:
    return resp.status_code == 403 and "public demo" in resp.text


# --------------------------------------------------------------------------
# 1-2. Entry, banner, first run
# --------------------------------------------------------------------------


def test_demo_routes_are_404_when_off(client, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert client.post("/admin/demo/enter", follow_redirects=False).status_code == 404
    assert client.post("/admin/demo/assistant/chat", json={"message": "hi"}).status_code == 404
    assert client.post(northwind.PATH, json={"jsonrpc": "2.0", "id": 1,
                                             "method": "tools/list"}).status_code == 404
    login = client.get("/admin/login")
    assert "Try the demo" not in login.text


def test_login_page_offers_one_click_entry(client, demo):
    page = client.get("/admin/login")
    assert page.status_code == 200
    assert "Try the demo" in page.text
    assert 'action="/admin/demo/enter"' in page.text
    assert 'type="password"' not in page.text


def test_signed_out_visitor_goes_to_the_demo_button(client, demo):
    client.cookies.clear()
    for path in ("/admin", "/admin/"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert resp.headers["location"] == "/admin/login"
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/admin")


def test_try_the_demo_signs_in_as_the_demo_admin(client, demo, monkeypatch):
    from demo_mode import account

    monkeypatch.setattr(account, "sign_in", lambda: "demo-session-token")
    resp = client.post("/admin/demo/enter", follow_redirects=False)
    assert resp.status_code == 303
    # Lands on the agents, in staging.
    assert resp.headers["location"] == "/admin/agents?env=staging"
    cookies = resp.headers.get_list("set-cookie")
    assert any("demo-session-token" in c for c in cookies)
    assert any(c.startswith("waj_admin_env=staging") for c in cookies)
    client.cookies.clear()


def test_demo_entry_refuses_another_site(client, demo, monkeypatch):
    from demo_mode import account

    monkeypatch.setattr(account, "sign_in", lambda: "demo-session-token")
    resp = client.post("/admin/demo/enter", headers={"Origin": "https://elsewhere.example",
                                                     "Sec-Fetch-Site": "cross-site"},
                       follow_redirects=False)
    assert resp.status_code == 403


def test_demo_password_is_derived_not_stored(monkeypatch):
    from demo_mode import account

    monkeypatch.setenv("SECRET_KEY", "one-secret")
    first = account.demo_password()
    assert first == account.demo_password() and len(first) > 30
    monkeypatch.setenv("SECRET_KEY", "another-secret")
    assert account.demo_password() != first


def test_first_run_is_off_in_demo_mode(demo, monkeypatch):
    from admin.services import first_run

    monkeypatch.setattr(first_run.plugins, "has_product", lambda: False)
    monkeypatch.delenv("FIRST_RUN_SETUP", raising=False)
    assert first_run.enabled() is False


def test_portal_pages_show_banner_and_assistant(client, demo):
    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert "data-demo-banner" in page.text
    assert "https://github.com/waj-1/ai-engine" in page.text
    assert 'id="ba-panel"' in page.text and "builder-assistant.js" in page.text
    # A phone's first visit shows this hint instead of opening the sheet
    # (builder-assistant.js); it starts hidden and can be dismissed.
    hint = re.search(r'<div class="ba-hint" id="ba-hint"[^>]*>', page.text)
    assert hint and " hidden" in hint.group(0)
    assert "data-hint-close" in page.text


def test_no_banner_or_assistant_outside_demo(client, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    page = client.get("/admin/settings")
    assert "data-demo-banner" not in page.text
    assert 'id="ba-panel"' not in page.text and 'id="ba-hint"' not in page.text


def test_settings_hides_locked_screens(client, demo):
    page = client.get("/admin/settings").text
    for href in ("/admin/model-keys", "/admin/api-keys", "/admin/users", "/admin/docs"):
        assert f'href="{href}' not in page, href


# --------------------------------------------------------------------------
# 3. Lock-down
# --------------------------------------------------------------------------

LOCKED = [
    "/admin/model-keys", "/admin/model-keys/status.json", "/admin/api-keys",
    "/admin/env-vars", "/admin/users", "/admin/users/x/superuser", "/admin/setup",
    "/admin/setup/model-key", "/admin/drift", "/admin/diagnostics/runtime",
    "/admin/lmnr/diagnose", "/admin/tests", "/admin/docs", "/admin/docs/api",
    "/admin/deployments", "/admin/deploy-runs.json", "/admin/env/prod/deploy",
    "/admin/env/prod/rollback", "/admin/jobs", "/admin/voice-budgets", "/mcp",
    "/agents", "/voice/live", "/admin/prompts/p1/optimize",
]


@pytest.mark.parametrize("path", LOCKED)
def test_locked_route_refused_get_and_post(client, demo, path):
    assert _refused(client.get(path, follow_redirects=False)), path
    assert _refused(client.post(path, follow_redirects=False)), path


@pytest.mark.parametrize("path", LOCKED)
def test_locked_route_not_refused_outside_demo(client, monkeypatch, path):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    for resp in (client.get(path, follow_redirects=False), client.post(path, follow_redirects=False)):
        assert not _refused(resp), path


def test_environment_variables_tab_refused(client, demo):
    assert _refused(client.get("/admin/env?tab=variables"))
    assert not _refused(client.get("/admin/env?tab=releases", follow_redirects=False))


@pytest.mark.parametrize("path", ["/admin/agent-models", "/admin/evals/bakeoffs", "/admin/audit"])
def test_read_only_screens(client, demo, path):
    assert not _refused(client.get(path, follow_redirects=False))
    assert client.post(path, follow_redirects=False).status_code == 403


def test_open_screens_stay_usable(client, demo):
    for path in ("/admin/agents", "/admin/kb", "/admin/evals", "/admin/observability",
                 "/admin/tools", "/admin/channels"):
        assert not _refused(client.get(path, follow_redirects=False)), path


def test_policy_matches_on_segment_boundary():
    assert policy.refusal("GET", "/admin/users") is not None
    assert policy.refusal("GET", "/admin/users-guide") is None


def test_process_lockdown_switches_off_admin_mcp():
    env = {"DEMO_MODE": "1", "ENABLE_ADMIN_MCP": "1", "MCP_ALLOW_LOCAL_ENDPOINTS": "1",
           "HTTP_TOOL_ALLOW_HOSTS": "localhost:9", "CONVERSATION_INSIGHTS": "1"}
    changed = settings.apply_process_lockdown(env)
    assert "ENABLE_ADMIN_MCP" not in env and "MCP_ALLOW_LOCAL_ENDPOINTS" not in env
    assert "HTTP_TOOL_ALLOW_HOSTS" not in env
    # The insights sweep is on under the demo's own budget (demo_mode/insights.py)...
    assert env["ENABLE_GENERIC_AGENTS"] == "1" and env["CONVERSATION_INSIGHTS"] == "1"
    assert env["TOOL_ERROR_EVENTS"] == "1"
    assert "ENABLE_ADMIN_MCP" in changed
    # ...and off, whatever the operator set, when that budget is zero.
    capped = {"DEMO_MODE": "1", "CONVERSATION_INSIGHTS": "1", "DEMO_INSIGHTS_DAILY_CALLS": "0"}
    settings.apply_process_lockdown(capped)
    assert capped["CONVERSATION_INSIGHTS"] == "0"
    off = {"ENABLE_ADMIN_MCP": "1"}
    assert settings.apply_process_lockdown(off) == [] and off == {"ENABLE_ADMIN_MCP": "1"}


def test_internal_addresses_blocked_and_no_secret_ever_sent(demo, monkeypatch):
    from runtime.tools import _http_adapter

    monkeypatch.setattr(_http_adapter, "_resolved_ips",
                        lambda host: [__import__("ipaddress").ip_address("93.184.216.34")])
    classify = _http_adapter._classify_endpoint
    assert classify("http://127.0.0.1:8000/admin") == "blocked"
    assert classify("http://169.254.169.254/latest") == "blocked"
    assert classify("http://10.0.0.5/mcp") == "blocked"
    assert classify("http://100.64.0.1/mcp") == "blocked"
    # A public host: reachable, but classified so no auth_env_var is sent.
    assert classify("https://tools.example.org/mcp") == "private"
    # The demo's own server: in-process.
    assert classify(northwind.northwind_endpoint()) == "private"
    monkeypatch.setenv("HTTP_TOOL_ALLOW_HOSTS", "127.0.0.1:8000")
    assert classify("http://127.0.0.1:8000/x") == "blocked"


def test_public_endpoint_keeps_secrets_outside_demo(monkeypatch):
    from runtime.tools import _http_adapter

    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setattr(_http_adapter, "_resolved_ips",
                        lambda host: [__import__("ipaddress").ip_address("93.184.216.34")])
    assert _http_adapter._classify_endpoint("https://tools.example.org/mcp") == "public"


def test_tool_endpoint_problem(demo):
    assert policy.tool_endpoint_problem("http://127.0.0.1:5432/") is not None
    assert policy.tool_endpoint_problem(northwind.northwind_endpoint()) is None
    assert policy.tool_endpoint_problem(northwind.northwind_endpoint(), "AZURE_OPENAI_API_KEY")


def test_connect_server_refuses_internal_address(client, demo):
    resp = client.post("/admin/mcp/connect", data={
        "name": "internal", "endpoint": "http://127.0.0.1:54421/rest/v1", "transport": "http"})
    assert "local or internal addresses" in resp.text


def test_kb_config_stays_on_builtin_tables(demo):
    payload = {"kind": "pgvector", "config": {"table": "provider_keys", "pages_table": "agent_api_keys"}}
    assert policy.clamp_kb_payload(payload) is None
    assert payload["config"] == policy.SHARED_KB_CONFIG
    assert policy.clamp_kb_payload({"kind": "http"}) is not None


def test_model_output_is_capped(demo, monkeypatch):
    from runtime import llm_factory

    monkeypatch.setenv("DEMO_MAX_OUTPUT_TOKENS", "300")
    kwargs = llm_factory._common_kwargs({"model_string": "m", "max_output_tokens": 9000})
    assert kwargs["max_output_tokens"] == 300


# --------------------------------------------------------------------------
# 4. The builder assistant's allow-list
# --------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name):
        from mcp.server.fastmcp.tools.base import Tool

        def fn(action: str = "list", endpoint: str = "", auth_env_var: str = "",
               env: str = "staging", name: str = ""):
            self.calls.append({"action": action, "endpoint": endpoint,
                               "auth_env_var": auth_env_var, "env": env, "name": name})
            return {"ok": True, "mcp_server_id": "s1", "name": name or "x"}

        self.calls: List[Dict[str, Any]] = []
        real = Tool.from_function(fn, name=name)
        self.fn, self.fn_metadata, self.is_async = real.fn, real.fn_metadata, False
        self.description, self.parameters = "fake", real.parameters


class _FakeServer:
    def __init__(self):
        self.tools: Dict[str, _FakeTool] = {}

        class _Manager:
            def get_tool(inner, name):
                return self.tools.setdefault(name, _FakeTool(name))
        self._tool_manager = _Manager()


@pytest.fixture
def fake_admin(monkeypatch, demo):
    server = _FakeServer()
    monkeypatch.setattr(assistant, "_admin_mcp", lambda: server)
    token = assistant.TURN.set(assistant.Turn(actor={"user_id": "u1", "email": "demo@example.com",
                                                     "user_token": "t"}, ip="203.0.113.9",
                                              session="s-1"))
    yield server
    assistant.TURN.reset(token)


def _run(name, args):
    return asyncio.run(_in_turn(name, args))


async def _in_turn(name, args):
    return await assistant.run_tool(name, args)


@pytest.mark.parametrize("name", ["provider_keys", "api_keys", "users", "env_vars", "deploy",
                                  "rollback", "deployment_targets", "runtime_flags", "jobs",
                                  "audit", "memory", "bakeoffs", "pytest_runs", "skills"])
def test_forbidden_tool_refused_even_if_asked(fake_admin, name):
    out = _run(name, {"action": "list"})
    assert out["ok"] is False and "not available" in out["error"]
    assert name not in fake_admin.tools  # never looked up, never run


def test_forbidden_tools_are_never_bound(demo):
    names = {t.name for t in assistant.build_tools()}
    assert {"provider_keys", "api_keys", "users", "env_vars", "memory", "jobs"}.isdisjoint(names)
    assert {"templates", "update_profile", "eval_suites", "trigger_eval_run", "chat"} <= names


def test_forbidden_action_refused(fake_admin):
    out = _run("prompts", {"action": "publish_from_repo"})
    assert out["ok"] is False and "not available" in out["error"]
    out = _run("models", {"action": "create", "name": "m"})
    assert out["ok"] is False
    assert "prompts" not in fake_admin.tools and "models" not in fake_admin.tools


def test_only_the_northwind_server_can_be_added(fake_admin):
    for endpoint in ("http://169.254.169.254/latest/meta-data", "http://localhost:54421/rest/v1",
                     "https://tools.example.org/mcp", "http://127.0.0.1:8000/admin/users"):
        out = _run("mcp_servers", {"action": "create", "name": "x", "endpoint": endpoint})
        assert out["ok"] is False, endpoint
    assert "mcp_servers" not in fake_admin.tools
    out = _run("mcp_servers", {"action": "create", "name": "Northwind 2",
                               "endpoint": northwind.northwind_endpoint(),
                               "auth_env_var": "AZURE_OPENAI_API_KEY", "env": "prod"})
    assert out.get("ok") is True
    call = fake_admin.tools["mcp_servers"].calls[-1]
    # Arguments outside the allow-list never reach the tool.
    assert call["auth_env_var"] == "" and call["env"] == "staging"


def test_tool_call_produces_a_step_with_a_link(fake_admin):
    _run("mcp_servers", {"action": "create", "name": "Northwind 2",
                         "endpoint": northwind.northwind_endpoint()})
    turn = assistant.TURN.get()
    step = turn.drain()[-1]
    assert step["ok"] is True and step["links"][0]["href"] == "/admin/agent-mcp-servers/s1"
    assert turn.wrote is True


def test_tool_calls_per_message_are_capped(fake_admin, monkeypatch):
    monkeypatch.setenv("DEMO_ASSISTANT_TOOLS_PER_TURN", "2")
    assert _run("mcp_servers", {"action": "list"}).get("ok") is True
    assert _run("mcp_servers", {"action": "list"}).get("ok") is True
    out = _run("mcp_servers", {"action": "list"})
    assert out["ok"] is False and "one message" in out["error"]


def test_tool_calls_count_against_the_hourly_allowance(fake_admin, monkeypatch):
    monkeypatch.setenv("DEMO_ASSISTANT_TOOLS_PER_HOUR", "1")
    assert _run("mcp_servers", {"action": "list"}).get("ok") is True
    assistant.TURN.get().calls = 0
    out = _run("mcp_servers", {"action": "list"})
    assert out["ok"] is False and out["error"] == limits.RATE_MESSAGE


def test_test_runs_and_uploads_are_bounded():
    assert assistant.check_call("trigger_eval_run", {"source": "live_chats"})
    assert assistant.check_call("trigger_eval_run", {"suite_id": "s", "repeats": 5})
    assert assistant.check_call("eval_suites", {"action": "create", "scenarios": [{}] * 11})
    assert assistant.check_call("kb_sources", {"action": "upload_document", "content": "x" * 10**7})
    assert assistant.check_call("kb_sources", {"action": "create", "kind": "http"})
    assert assistant.check_call("trigger_eval_run", {"suite_id": "s"}) is None


def test_schema_shows_only_allowed_actions_and_args(demo):
    tools = {t.name: t for t in assistant.build_tools()}
    props = tools["mcp_servers"].args_schema["properties"]
    assert set(props["action"]["enum"]) == {"list", "get", "members", "discover", "create", "update"}
    assert "auth_env_var" not in props and "env" not in props
    assert "force" not in tools["promote_profile"].args_schema["properties"]


class _ScriptedLLM:
    """A chat model that asks for one tool call, then answers."""

    def __init__(self, calls):
        self.calls, self.round = calls, 0

    def bind_tools(self, tools):
        self.bound = [t.name for t in tools]
        return self

    async def astream_events(self, messages, version="v2"):
        from langchain_core.messages import AIMessage

        self.round += 1
        if self.round == 1:
            out = AIMessage(content="", tool_calls=self.calls)
        else:
            out = AIMessage(content="Done.")
            yield {"event": "on_chat_model_stream", "data": {"chunk": out}}
        yield {"event": "on_chat_model_end", "data": {"output": out}}


def test_assistant_agent_refuses_a_forbidden_tool_the_model_asks_for(fake_admin, monkeypatch):
    """End to end through the runtime: the model names a tool that is not on
    the list (and one that is, with a forbidden action); neither runs."""
    from runtime.agents import custom

    agent = custom.CustomRuntimeAgent.__new__(custom.CustomRuntimeAgent)
    llm = _ScriptedLLM([
        {"name": "provider_keys", "args": {"action": "list"}, "id": "c1"},
        {"name": "prompts", "args": {"action": "publish_from_repo"}, "id": "c2"},
    ])
    agent.slug, agent.locale, agent.llm = assistant.BUILDER_SLUG, "", llm
    agent.profile = type("P", (), {"model_main": {}, "data_source_ids": [], "memory_config": {}})()
    agent.prepare = lambda ctx, all_tools: {"system_prompt": "x", "tools": [], "tools_by_name": {}}
    agent._knowledge_tools = lambda: assistant.build_tools()
    monkeypatch.setattr(custom, "cached_system_message",
                        lambda text, model: __import__("langchain_core.messages",
                                                       fromlist=["SystemMessage"]).SystemMessage(content=text))

    async def go():
        return [e async for e in agent.run_stream("hi", {})]

    events = asyncio.run(go())
    assert "provider_keys" not in llm.bound
    assert any(e.get("type") == "answer" for e in events)
    assert not any(t.calls for t in fake_admin.tools.values())  # nothing ran
    steps = assistant.TURN.get().drain()
    assert [s["ok"] for s in steps] == [False]  # the prompts call, refused


# --------------------------------------------------------------------------
# 5. Caps
# --------------------------------------------------------------------------


def test_per_visitor_limit_counts_session_and_address(demo, monkeypatch):
    monkeypatch.setenv("DEMO_CHAT_PER_HOUR", "2")
    monkeypatch.setenv("DEMO_IP_MULTIPLIER", "2")
    assert limits.hit("chat", ip="198.51.100.1", session="a")
    assert limits.hit("chat", ip="198.51.100.1", session="a")
    assert not limits.hit("chat", ip="198.51.100.1", session="a")
    # A new session on the same address: the address still has room for 2 more.
    assert limits.hit("chat", ip="198.51.100.1", session="b")
    assert limits.hit("chat", ip="198.51.100.1", session="c")
    assert not limits.hit("chat", ip="198.51.100.1", session="d")
    # The engine's own in-process calls are counted by session only.
    assert limits.hit("chat", ip="127.0.0.1", session="e")
    assert limits.hit("chat", ip="127.0.0.1", session="e")
    assert not limits.hit("chat", ip="127.0.0.1", session="e")


def test_daily_cap_takes_amounts_all_or_none(demo, monkeypatch):
    monkeypatch.setenv("DEMO_DAILY_TURNS", "5")
    assert limits.take_turn(3)
    assert not limits.take_turn(3)
    assert limits.take_turn(2)
    assert not limits.take_turn()
    assert limits.turns_left() == 0


def _token(session_id: str) -> str:
    import base64

    body = base64.urlsafe_b64encode(json.dumps({"sub": "u", "session_id": session_id}).encode())
    return "e30." + body.decode().rstrip("=") + ".sig"


def test_assistant_rate_limit_answers_in_the_panel(client, demo, monkeypatch):
    monkeypatch.setenv("DEMO_ASSISTANT_PER_HOUR", "1")
    monkeypatch.setattr(limits, "take_turn", lambda amount=1: False)
    headers = {"Authorization": f"Bearer {_token('rl-1')}"}
    first = client.post("/admin/demo/assistant/chat", json={"message": "hi"}, headers=headers)
    assert limits.CAP_MESSAGE in first.text  # the daily cap, answered as a message
    second = client.post("/admin/demo/assistant/chat", json={"message": "hi"}, headers=headers)
    assert second.status_code == 200 and limits.RATE_MESSAGE in second.text


def test_agent_chat_answers_the_cap_message_without_a_model(client, demo, monkeypatch):
    import endpoints.agents_v1 as v1

    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    monkeypatch.setenv("DEMO_DAILY_TURNS", "0")

    async def caller(_auth):
        return {"user_id": "u1"}

    monkeypatch.setattr(v1, "_require_caller", caller)
    monkeypatch.setattr("runtime.agents.custom.create_custom_agent",
                        lambda *a, **k: pytest.fail("a model was called past the cap"))
    resp = client.post("/v1/agents/support-desk/chat", json={"message": "hi"},
                       headers={"Authorization": f"Bearer {_token('cap-1')}"})
    assert resp.status_code == 200 and limits.CAP_MESSAGE in resp.text


def test_test_run_takes_its_judge_calls_from_the_cap(demo, monkeypatch):
    from admin import evals

    monkeypatch.setenv("DEMO_DAILY_TURNS", "6")
    assert evals._demo_run_refusal(5, judged=True) is None
    assert evals._demo_run_refusal(5, judged=True) == limits.CAP_MESSAGE
    monkeypatch.setenv("DEMO_MAX_TEST_CASES", "3")
    assert "at most 3" in evals._demo_run_refusal(4, judged=False)
    monkeypatch.delenv("DEMO_MODE")
    assert evals._demo_run_refusal(500, judged=True) is None


def test_upload_size_is_capped(client, demo, monkeypatch):
    monkeypatch.setenv("DEMO_MAX_UPLOAD_BYTES", "1000")
    resp = client.post("/admin/kb/abc/upload", files={"files": ("big.md", b"x" * 200_000)})
    assert resp.status_code == 413


def test_automatic_model_means_the_demo_agent_model(demo):
    from core import model_resolver

    rows = [
        {"model_id": "b", "provider": "openai", "model_string": "big", "is_active": True,
         "created_at": "1", "extra_config": {"demo_role": "builder", "base_url": "http://x"}},
        {"model_id": "a", "provider": "openai", "model_string": "small", "is_active": True,
         "created_at": "2", "extra_config": {"demo_role": "agent", "base_url": "http://x"}},
    ]
    picked = model_resolver.first_usable_model(rows)
    assert picked is None or picked["model_id"] == "a"


# --------------------------------------------------------------------------
# 6. Seed and reset
# --------------------------------------------------------------------------

_PKS = {"agent_types": "agent_type_id", "agent_data_sources": "source_id", "ai_kb_pages": "id",
        "agent_mcp_servers": "mcp_server_id", "agent_eval_suites": "suite_id",
        "agent_channels": "channel_id", "ai_chat_sessions": "id", "ai_chat_messages": "id",
        "agent_profile_mcp_servers": "id", "agent_tools": "tool_id",
        "agent_skills": "skill_id", "agent_skill_versions": "skill_version_id",
        "agent_eval_schedules": "schedule_id", "agent_bakeoffs": "bakeoff_id"}


_CASCADE = (
    ("agent_prompts", "agent_prompt_versions", "prompt_id", "prompt_id"),
    ("agent_profiles", "agent_profile_mcp_servers", "profile_id", "profile_id"),
    ("agent_mcp_servers", "agent_profile_mcp_servers", "mcp_server_id", "mcp_server_id"),
    ("ai_chat_sessions", "ai_chat_messages", "session_id", "id"),
    ("agent_data_sources", "ai_kb_pages", "source_id", "source_id"),
    ("agent_skills", "agent_skill_versions", "skill_id", "skill_id"),
    ("agent_skills", "agent_profile_skills", "skill_id", "skill_id"),
)


class _Query(fake_pg.Query):
    def neq(self, col, val):
        self.filters.append(lambda r: r.get(col) != val)
        return self

    def execute(self):
        if self.op == "insert":
            pk = _PKS.get(self.table)
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            self.payload = [dict(i, **({pk: str(uuid.uuid4())} if pk and pk not in i else {}))
                            for i in items]
        out = super().execute()
        if self.op == "delete":  # the schema's ON DELETE CASCADE
            for parent, child, col, pk in _CASCADE:
                if self.table == parent:
                    gone = {r[pk] for r in out.data}
                    self.db[child] = [r for r in self.db.get(child, []) if r.get(col) not in gone]
        return out


class _Client(fake_pg.Client):
    def table(self, name):
        return _Query(self.db, name)


@pytest.fixture
def db(demo, monkeypatch):
    from admin import kb_handlers
    from core.agent_profile import AgentProfileLoader

    monkeypatch.setattr(seed, "_env", lambda: "staging")
    # No tracing store, as on a default install: the stored example chats are seeded.
    monkeypatch.setenv("LMNR_DISABLED", "true")
    monkeypatch.delenv("LMNR_CLICKHOUSE_URL", raising=False)
    monkeypatch.setattr(kb_handlers, "embedding_status", lambda: {"ready": False, "problem": "none"})
    monkeypatch.setattr(AgentProfileLoader, "invalidate", classmethod(lambda cls, *a, **k: None))
    store: Dict[str, list] = {}
    # What the engine itself seeds and a reset keeps: a template.
    store["agent_profiles"] = [{"profile_id": "tpl", "agent_kind": "custom", "slug": "mold:rag",
                                "status": "template", "environment": "staging",
                                "prompt_version_ids": {"system": "tv1"}}]
    store["agent_prompts"] = [{"prompt_id": "tp", "agent_kind": "custom", "slug": "mold:rag:system"}]
    store["agent_prompt_versions"] = [{"prompt_version_id": "tv1", "prompt_id": "tp",
                                       "version_number": 1, "content": "template text"}]
    return store


def _counts(store):
    return {t: len(rows) for t, rows in store.items() if not t.startswith("_")}


def test_seed_builds_the_northwind_demo_and_is_idempotent(db):
    client = _Client(db)
    out = seed.seed(client)
    slugs = {p["slug"] for p in db["agent_profiles"]}
    assert {"support-desk", "billing-helper", "sales-assistant", "it-helpdesk",
            "builder-assistant"} <= slugs
    support = next(p for p in db["agent_profiles"] if p["slug"] == "support-desk")
    billing = next(p for p in db["agent_profiles"] if p["slug"] == "billing-helper")
    builder = next(p for p in db["agent_profiles"] if p["slug"] == "builder-assistant")
    assert support["downstream_profile_ids"] == [billing["profile_id"]]
    assert support["mcp_server_ids"] and support["data_source_ids"]
    assert builder["model_main_id"] == out["models"]["builder"] != support["model_main_id"]
    assert {c["slug"] for c in db["agent_types"]} == {"support", "sales", "internal-tools"}
    # Four knowledge bases, each with its own pages, attached where they belong.
    bases = {d["name"]: d["source_id"] for d in db["agent_data_sources"]}
    assert set(bases) == {"Northwind help center", "Northwind plans and pricing",
                          "Northwind billing and refunds", "Northwind IT handbook"}
    assert len(db["ai_kb_pages"]) == 9
    it = next(p for p in db["agent_profiles"] if p["slug"] == "it-helpdesk")
    sales = next(p for p in db["agent_profiles"] if p["slug"] == "sales-assistant")
    assert it["data_source_ids"] == [bases["Northwind IT handbook"]]
    assert sales["data_source_ids"] == [bases["Northwind plans and pricing"],
                                        bases["Northwind help center"]]
    assert billing["data_source_ids"][0] == bases["Northwind billing and refunds"]
    assert builder["data_source_ids"] == []
    # One five-case suite per agent, judged by the judge model.
    suites = {x["name"]: x for x in db["agent_eval_suites"]}
    assert len(suites) == 4 and set(out["suites"]) == {"support-desk", "sales-assistant",
                                                       "billing-helper", "it-helpdesk"}
    assert all(len(x["scenarios"]) == 5 and x["evaluator_model_id"] == out["models"]["judge"]
               for x in suites.values())
    assert suites["IT helpdesk: staff questions"]["profile_id"] == it["profile_id"]
    # Playbooks attached to their agents, and a schedule per suite.
    assert {k["slug"] for k in db["agent_skills"]} == {
        "calm-an-upset-customer", "compare-two-plans", "report-a-security-problem"}
    assert len(db["agent_skill_versions"]) == 3 and len(db["agent_profile_skills"]) == 3
    assert {(x["cadence"], x["is_enabled"]) for x in db["agent_eval_schedules"]} == {
        ("daily", True), ("weekly", True)}
    assert all(x["suite_id"] and x["next_run_at"] for x in db["agent_eval_schedules"])
    server = db["agent_mcp_servers"][0]
    assert server["endpoint"] == "https://demo.example.com/demo/northwind/mcp"
    assert len(db["ai_chat_sessions"]) == 5
    models = {m["extra_config"]["demo_role"]: m for m in db["agent_models"]}
    assert models["agent"]["model_string"] == "gpt-5.4-mini"
    assert models["builder"]["model_string"] == "gpt-5.5"
    assert models["agent"]["provider"] == "azure_openai"
    before = _counts(db)
    seed.seed(client)
    assert _counts(db) == before


def test_reset_wipes_visitor_data_and_seeds_again(db):
    client = _Client(db)
    seed.seed(client)
    seeded = _counts(db)
    # A visitor's work.
    db["agent_profiles"].append({"profile_id": "v1", "agent_kind": "custom", "slug": "my-agent",
                                 "status": "draft", "environment": "staging"})
    db.setdefault("agent_prompts", []).append({"prompt_id": "vp", "agent_kind": "custom",
                                               "slug": "my-agent:system"})
    db["agent_mcp_servers"].append({"mcp_server_id": "vs", "source": "external",
                                    "is_builtin": False, "name": "Mine"})
    db["ai_chat_sessions"].append({"id": "vc", "title": "secret stuff", "agent_kind": "custom"})
    db.setdefault("ai_user_memory", []).append({"id": "m1", "content": "a memory"})
    db["agent_types"].append({"agent_type_id": "vt", "slug": "mine"})
    db.setdefault("agent_eval_runs", []).append({"run_id": "r1"})
    support = next(p for p in db["agent_profiles"] if p["slug"] == "support-desk")
    support["display_name"] = "Changed by a visitor"
    db.setdefault("demo_usage_daily", []).append({"bucket": "chat_turns", "used": 9})
    # ...and a new version served on a template's instructions.
    db["agent_prompt_versions"].append({"prompt_version_id": "tv2", "prompt_id": "tp",
                                        "version_number": 2, "content": "visitor text"})
    db["agent_profiles"][0]["prompt_version_ids"] = {"system": "tv2"}

    out = reset.reset(client, engine_seeders=False)

    slugs = {p["slug"] for p in db["agent_profiles"]}
    assert "my-agent" not in slugs and "mold:rag" in slugs
    assert not any(r.get("title") == "secret stuff" for r in db["ai_chat_sessions"])
    assert db["ai_user_memory"] == [] and db["agent_eval_runs"] == []
    assert "Mine" not in {s["name"] for s in db["agent_mcp_servers"]}
    assert {c["slug"] for c in db["agent_types"]} == {"support", "sales", "internal-tools"}
    support = next(p for p in db["agent_profiles"] if p["slug"] == "support-desk")
    assert support["display_name"] == "Support desk"
    assert out["removed"]["agents"] == 6
    assert db["agent_profiles"][0]["prompt_version_ids"] == {"system": "tv1"}
    assert "tv2" not in {v["prompt_version_id"] for v in db["agent_prompt_versions"]}
    # The day's usage is never refilled by a reset.
    assert db["demo_usage_daily"] == [{"bucket": "chat_turns", "used": 9}]
    assert {k: v for k, v in _counts(db).items() if k in seeded} == seeded


def test_reset_runs_once_a_day(demo, monkeypatch):
    calls = []
    monkeypatch.setattr(reset, "reset", lambda: calls.append(1))
    assert reset.run_once_today() is True
    assert reset.run_once_today() is False
    assert calls == [1]


def test_reset_hour_setting(monkeypatch):
    monkeypatch.setenv("DEMO_RESET_HOUR_UTC", "off")
    assert settings.reset_hour_utc() is None
    monkeypatch.setenv("DEMO_RESET_HOUR_UTC", "5")
    assert settings.reset_hour_utc() == 5
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    assert reset.seconds_until(5, now) == 23 * 3600


def test_reset_script_needs_demo_mode(monkeypatch):
    from scripts import demo_reset

    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert demo_reset.main([]) == 2


# --------------------------------------------------------------------------
# 7. The Northwind tool server
# --------------------------------------------------------------------------


def _rpc(method, params=None, msg_id=1):
    return northwind.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def test_northwind_tools():
    names = [t["name"] for t in _rpc("tools/list")["result"]["tools"]]
    assert names == ["check_outage", "get_order_status", "create_ticket", "escalate_to_human",
                     "list_plans"]
    text = lambda r: r["result"]["content"][0]["text"]  # noqa: E731
    assert "planned maintenance in Riverside" in text(_rpc("tools/call", {"name": "check_outage", "arguments": {"area": "Riverside"}}))
    assert "No known outage" in text(_rpc("tools/call", {"name": "check_outage", "arguments": {"area": "Hilltop"}}))
    assert "shipped yesterday" in text(_rpc("tools/call", {"name": "get_order_status", "arguments": {"order_id": "nw-1042"}}))
    assert "T-2318" in text(_rpc("tools/call", {"name": "create_ticket", "arguments": {"summary": "red light"}}))
    assert "$45/month" in text(_rpc("tools/call", {"name": "list_plans"}))
    assert _rpc("tools/call", {"name": "rm_rf"})["result"]["isError"] is True
    assert _rpc("tools/call", {"name": "check_outage", "arguments": {}})["result"]["isError"] is True
    assert northwind.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_northwind_over_http(client, demo):
    resp = client.post(northwind.PATH, json={"jsonrpc": "2.0", "id": 7, "method": "initialize",
                                             "params": {"protocolVersion": "2025-03-26"}})
    assert resp.status_code == 200 and resp.json()["result"]["serverInfo"]["name"] == "northwind"
    assert client.get(northwind.PATH).status_code == 405


def test_engine_calls_northwind_in_process(demo, monkeypatch):
    """The demo's server row is reached without the network: the pooled MCP
    client for exactly that address gets the in-process transport."""
    import httpx

    from runtime.tools import _mcp_adapter

    _mcp_adapter.clear_mcp_pool()
    monkeypatch.setattr(httpx.AsyncClient, "send",
                        _guard_network(httpx.AsyncClient.send), raising=True)
    client = _mcp_adapter.get_pooled_client(northwind.northwind_endpoint(), "secret-token")
    names = [t["name"] for t in client.list_tools()]
    assert "check_outage" in names
    result = client.call_tool("check_outage", {"area": "Old Town"})
    assert "planned maintenance" in json.dumps(result)
    other = _mcp_adapter.get_pooled_client("https://demo.example.com/admin/users")
    assert getattr(other, "_transport", None) is None
    _mcp_adapter.clear_mcp_pool()


def _guard_network(real_send):
    async def send(self, request, *a, **k):
        assert request.url.host == "demo.example.com"
        assert "authorization" not in {h.lower() for h in request.headers}
        return await real_send(self, request, *a, **k)
    return send


def test_northwind_is_not_in_process_outside_demo(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert northwind.handles("http://localhost:8000/demo/northwind/mcp") is False


def test_visitor_key_reads_session_claim():
    scope = {"type": "http", "client": ("203.0.113.5", 1),
             "headers": [(b"authorization", f"Bearer {_token('abc')}".encode())]}
    assert gate.visitor(scope) == ("203.0.113.5", "abc")


def test_visitor_address_from_a_cdn_header(monkeypatch):
    scope = {"type": "http", "client": ("172.70.1.1", 1),
             "headers": [(b"cf-connecting-ip", b"198.51.100.7")]}
    assert gate.visitor(scope)[0] == "172.70.1.1"
    monkeypatch.setenv("DEMO_CLIENT_IP_HEADER", "CF-Connecting-IP")
    assert gate.visitor(scope)[0] == "198.51.100.7"


# --------------------------------------------------------------------------
# One host, no compose (deploy/compose/setup.py)
# --------------------------------------------------------------------------


def test_setup_renders_single_host_addresses(tmp_path, monkeypatch):
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    monkeypatch.setenv("STACK_GATEWAY_URL", "http://127.0.0.1:54321")
    monkeypatch.setenv("STACK_DB_HOST", "127.0.0.1:5432")
    monkeypatch.setenv("STACK_REST_UPSTREAM", "http://127.0.0.1:3000")
    monkeypatch.setenv("STACK_AUTH_UPSTREAM", "http://127.0.0.1:9999")
    monkeypatch.setenv("ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    spec = importlib.util.spec_from_file_location("compose_setup", root / "deploy/compose/setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main() == 0
    engine = (tmp_path / "engine.env").read_text()
    assert "SUPABASE_URL=http://127.0.0.1:54321" in engine
    assert "@127.0.0.1:5432/" in engine
    assert "127.0.0.1:5432" in (tmp_path / "postgrest.conf").read_text()
    kong = (tmp_path / "kong.yml").read_text()
    assert "http://127.0.0.1:3000" in kong and "http://127.0.0.1:9999" in kong
    assert "__ANON_KEY__" not in kong and "rest:3000" not in kong
    assert "CREATE ROLE authenticator" in (tmp_path / "db-init.sql").read_text()
    # A second run keeps the secrets and re-renders the portable files.
    before = (tmp_path / "engine.env").read_text()
    monkeypatch.setenv("STACK_REST_UPSTREAM", "http://127.0.0.1:3001")
    assert mod.main() == 0
    assert (tmp_path / "engine.env").read_text() == before
    assert "http://127.0.0.1:3001" in (tmp_path / "kong.yml").read_text()


def test_assistant_refuses_another_site(client, demo):
    resp = client.post("/admin/demo/assistant/chat", json={"message": "hi"},
                       headers={"Origin": "https://elsewhere.example", "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_assistant_streams_steps_and_changed(client, demo, monkeypatch):
    """The endpoint runs the agent with the visitor as the actor and turns
    each tool call into a `step` event, then `changed`."""
    from admin import demo_handlers
    import common.chat_session as chat_session

    server = _FakeServer()
    monkeypatch.setattr(assistant, "_admin_mcp", lambda: server)
    seen = {}

    class _Agent:
        profile = None

        async def run_stream(self, message, context):
            seen["actor"] = assistant.TURN.get().actor
            seen["task"] = asyncio.current_task()
            seen["spans_open"] = list(spans)
            await assistant.run_tool("mcp_servers", {"action": "create", "name": "Northwind 2",
                                                     "endpoint": northwind.northwind_endpoint()})
            await assistant.run_tool("users", {"action": "list"})
            yield {"type": "tool_activity", "tools": ["mcp_servers", "users"]}
            yield {"type": "answer", "content": "Done."}

    async def fake_run_turn(*, agent, message, identity, profile, extra_context=None):
        seen["slug"] = identity.session_metadata["agent_slug"]
        async for event in agent.run_stream(message, {}):
            yield event
        yield {"type": "done"}

    # The turn's root span: where it is opened, and by which task.
    import contextlib

    from core import observability

    spans: List[Any] = []
    closed: List[str] = []

    @contextlib.contextmanager
    def fake_root_span(name, attributes=None, **_k):
        if name:
            spans.append((name, asyncio.current_task()))
        yield None
        if name:
            closed.append(name)

    monkeypatch.setattr(observability, "root_span", fake_root_span)
    monkeypatch.setattr(demo_handlers, "_build_assistant", lambda page: _Agent())
    monkeypatch.setattr(chat_session, "run_turn", fake_run_turn)
    resp = client.post("/admin/demo/assistant/chat", json={"message": "add the tools"},
                       headers={"Authorization": f"Bearer {_token('st-1')}"})
    events = [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]
    steps = [e for e in events if e.get("type") == "step"]
    assert [s["ok"] for s in steps] == [True, False]
    assert steps[0]["links"] == [{"text": "Open Northwind 2",
                                  "href": "/admin/agent-mcp-servers/s1"}]
    assert any(e.get("type") == "changed" for e in events)
    assert seen["slug"] == assistant.BUILDER_SLUG
    assert seen["actor"]["user_id"] == "00000000-0000-0000-0000-000000000001"
    # One root span for the turn, opened INSIDE the task that runs the model
    # calls and before the first of them (around the response stream it was in
    # another task's context, and every model call was a trace of its own).
    assert [name for name, _task in spans] == ["demo.builder_assistant"]
    assert spans[0][1] is seen["task"] and seen["spans_open"] == spans
    assert closed == ["demo.builder_assistant"]


# --------------------------------------------------------------------------
# Polish: friendly steps, the read-only Builder assistant, waiting for a run,
# the conversation across pages
# --------------------------------------------------------------------------

BUILDER_ID = "11111111-2222-3333-4444-555555555555"
BUILDER_PROMPT = "66666666-7777-8888-9999-000000000000"


@pytest.fixture
def builder_ids(monkeypatch, demo):
    from demo_mode import protect

    protect.reset_for_tests()
    monkeypatch.setattr(protect, "_lookup", lambda: {BUILDER_ID, BUILDER_PROMPT})
    yield
    protect.reset_for_tests()


@pytest.mark.parametrize("name,args,result,label", [
    ("models", {"action": "list"}, {}, "Looked at the models"),
    ("templates", {"action": "use", "display_name": "Outage Support Agent"}, {},
     "Created Outage Support Agent from a template"),
    ("profile_attachments", {"action": "add", "kind": "mcp"}, {"attached": ["Northwind tools"]},
     "Attached the Northwind tools"),
    ("profile_attachments", {"action": "add", "kind": "kb"}, {}, "Attached the knowledge base"),
    ("prompts", {"action": "create_version"}, {}, "Wrote its instructions"),
    ("chat", {"display_name": "Support desk"}, {}, "Chatted with Support desk"),
    ("trigger_eval_run", {}, {}, "Started a test run"),
    ("search", {"q": "x"}, {}, "Searched the portal"),
    ("eval_runs", {"action": "get"}, {}, "Read the test results"),
    ("wait_for_test_run", {}, {"finished": True, "passed": 4, "scenarios": 5},
     "Test run finished: 4 of 5 passed"),
])
def test_steps_have_friendly_labels(name, args, result, label):
    assert assistant.step_label(name, args, result) == label


def test_no_step_label_is_a_raw_tool_name():
    for name in list(assistant.ALLOWED_TOOLS) + [assistant.WAIT_TOOL]:
        for action in ("", "list", "get", "create", "update", "add", "use"):
            text = assistant.step_label(name, {"action": action}, {})
            assert "(" not in text and "_" not in text, (name, action, text)


def test_agent_links_open_the_agent_in_staging():
    links = assistant.links_for("templates", {"action": "use"},
                                {"profile": {"profile_id": "p1", "display_name": "Outage helper"}})
    assert links == [{"text": "Open Outage helper",
                      "href": "/admin/agents?profile_id=p1&env=staging"}]


def test_builder_is_read_only_in_the_portal(client, builder_ids):
    for path in (f"/admin/agents/{BUILDER_ID}/instructions", f"/admin/profiles/{BUILDER_ID}",
                 f"/admin/agents/{BUILDER_ID}/fork", f"/admin/profiles/{BUILDER_ID}/archive",
                 f"/admin/prompts/{BUILDER_PROMPT}/versions"):
        resp = client.post(path, follow_redirects=False)
        assert resp.status_code == 403 and "read-only" in resp.text, path
    # Reading it is fine, and other agents are untouched.
    assert client.get(f"/admin/agents?profile_id={BUILDER_ID}").status_code == 200
    from demo_mode import protect

    assert protect.refusal_for_path("POST", "/admin/profiles/99999999-2222-3333-4444-555555555555") is None
    assert protect.refusal_for_path("GET", f"/admin/profiles/{BUILDER_ID}") is None
    promote = client.post("/admin/env/promote", data={"profile_id": BUILDER_ID,
                                                      "target_env": "prod"},
                          follow_redirects=False)
    assert promote.status_code == 403


def test_builder_is_editable_outside_demo(client, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    resp = client.post(f"/admin/agents/{BUILDER_ID}/instructions", follow_redirects=False)
    assert "read-only" not in resp.text


def test_assistant_cannot_change_itself(fake_admin, builder_ids):
    for name, args in (("update_profile", {"profile_id": BUILDER_ID, "display_name": "x"}),
                       ("fork_profile", {"profile_id": BUILDER_ID}),
                       ("promote_profile", {"profile_id": BUILDER_ID, "target_env": "prod"}),
                       ("prompts", {"action": "create_version", "prompt_id": BUILDER_PROMPT}),
                       ("profile_attachments", {"profile_id": "other", "kind": "downstream",
                                                "action": "add", "ids": [BUILDER_ID]}),
                       ("chat", {"profile_id": BUILDER_ID, "message": "hi"})):
        out = _run(name, args)
        assert out["ok"] is False and "read-only" in out["error"], name
    assert not any(t.calls for t in fake_admin.tools.values())
    # Reading it is allowed.
    assert "read-only" not in json.dumps(_run("get_profile", {"profile_id": BUILDER_ID}))


def test_builder_is_hidden_from_lists_pickers_and_search(builder_ids, monkeypatch):
    from admin import agent_kinds
    from demo_mode import protect

    rows = [{"agent_kind": "custom", "slug": "builder-assistant", "display_name": "Builder assistant",
             "status": "specialist", "environment": "staging"},
            {"agent_kind": "custom", "slug": "support-desk", "display_name": "Support desk",
             "status": "active", "environment": "staging"}]
    assert [r["slug"] for r in protect.visible(rows)] == ["support-desk"]
    monkeypatch.setattr(agent_kinds, "_fetch_profiles", lambda: rows)
    assert [r["slug"] for r in agent_kinds.custom_agent_rows()] == ["support-desk"]
    monkeypatch.delenv("DEMO_MODE")
    assert len(protect.visible(rows)) == 2
    assert len(agent_kinds.custom_agent_rows()) == 2


def test_agents_list_hides_the_builder(builder_ids, monkeypatch):
    from admin.screens import agents_views

    profiles = [{"profile_id": BUILDER_ID, "agent_kind": "custom", "slug": "builder-assistant",
                 "display_name": "Builder assistant", "status": "specialist",
                 "environment": "staging"},
                {"profile_id": "p2", "agent_kind": "custom", "slug": "support-desk",
                 "display_name": "Support desk", "status": "active", "environment": "staging"}]
    db = {"agent_profiles": profiles}
    monkeypatch.setattr("admin.router.client_for", lambda env: _Client(db))
    data = agents_views.load_agents_list("staging")
    text = json.dumps(data, default=str)
    assert "Support desk" in text and "Builder assistant" not in text


def _fake_runs(states):
    """An eval_runs tool whose `get` returns each state in turn."""
    calls = []

    def fn(action="get", run_id="", include_results=False, **_):
        calls.append(run_id)
        return states[min(len(calls), len(states)) - 1]
    return fn, calls


def test_wait_for_test_run_reports_the_result(fake_admin, monkeypatch):
    monkeypatch.setattr(assistant, "_POLL_SEC", 0.01)
    done = {"run_id": "r1", "status": "completed", "score": 0.8,
            "results_summary": {"scenarios": 5, "passed": 4, "failed": 1,
                                "failing": [{"scenario_id": "refund-policy"}]},
            "results": [{"scenario_id": "refund-policy", "passed": False,
                         "question": "Do I get money back?", "response": "No.",
                         "reasoning": "It should say one free day per day down."}]}
    fn, calls = _fake_runs([{"run_id": "r1", "status": "running"}, done])
    fake_admin.tools["eval_runs"] = type("T", (), {"fn": staticmethod(fn)})()
    out = asyncio.run(_wait("r1"))
    assert out["finished"] is True and out["passed"] == 4 and out["scenarios"] == 5
    assert out["failing_cases"][0]["question"] == "Do I get money back?"
    assert len(calls) == 2
    step = assistant.TURN.get().drain()[-1]
    assert step["label"] == "Test run finished: 4 of 5 passed"
    assert step["links"] == [{"text": "Open the test run", "href": "/admin/evals/runs/r1"}]


def test_wait_for_test_run_reads_the_wrapped_run(fake_admin, monkeypatch):
    """eval_runs(action="get") returns {"run": {...}}: a finished run must be seen as finished.
    Found on a live stack: the waiter read the wrapper's empty status and waited the full
    two minutes for a run that took 11 seconds."""
    monkeypatch.setattr(assistant, "_POLL_SEC", 0.01)
    done = {"run": {"run_id": "r4", "status": "passed", "score": 0.95,
                    "results_summary": {"scenarios": 5, "passed": 5, "failed": 0, "failing": []},
                    "results": []}}
    fn, calls = _fake_runs([{"run": {"run_id": "r4", "status": "running"}}, done])
    fake_admin.tools["eval_runs"] = type("T", (), {"fn": staticmethod(fn)})()
    out = asyncio.run(_wait("r4"))
    assert out["finished"] is True and out["score"] == 0.95 and out["passed"] == 5
    assert len(calls) == 2


def test_step_cap_note_is_plain_for_the_visitor(fake_admin, monkeypatch):
    monkeypatch.setenv("DEMO_ASSISTANT_TOOLS_PER_TURN", "1")
    assistant.TURN.get().calls = 1
    out = asyncio.run(_wait("r5"))
    assert "one message" in out["error"]  # the model still gets the instruction
    step = assistant.TURN.get().drain()[-1]
    assert step["note"] == assistant.STEP_CAP_FOR_VISITOR
    assert "Tell the visitor" not in step["note"]


def test_wait_for_test_run_gives_up_in_time(fake_admin, monkeypatch):
    monkeypatch.setattr(assistant, "_POLL_SEC", 0.01)
    monkeypatch.setattr(assistant, "time", type("T", (), {"monotonic": _clock()}))
    fn, _ = _fake_runs([{"run_id": "r2", "status": "running"}])
    fake_admin.tools["eval_runs"] = type("T", (), {"fn": staticmethod(fn)})()
    out = asyncio.run(_wait("r2", 10))
    assert out["finished"] is False and "/admin/evals/runs/r2" in out["note"]
    assert assistant.TURN.get().drain()[-1]["label"] == "The test run is still going"


def _clock():
    now = [0.0]

    def monotonic():
        now[0] += 3.0
        return now[0]
    return monotonic


async def _wait(run_id, seconds=110):
    return await assistant.wait_for_test_run(run_id, seconds)


def test_wait_counts_as_a_tool_call(fake_admin, monkeypatch):
    monkeypatch.setenv("DEMO_ASSISTANT_TOOLS_PER_TURN", "1")
    assistant.TURN.get().calls = 1
    out = asyncio.run(_wait("r3"))
    assert out["ok"] is False and "one message" in out["error"]


def test_builder_instructions_wait_for_runs():
    from demo_mode import content

    assert "wait_for_test_run" in content.BUILDER_INSTRUCTIONS
    assert "ask me again" not in content.BUILDER_INSTRUCTIONS.lower()
    assert "/admin/agents?profile_id=" in content.BUILDER_INSTRUCTIONS


def test_seed_keeps_the_builder_on_its_code(db):
    client = _Client(db)
    seed.seed(client)
    builder = next(p for p in db["agent_profiles"] if p["slug"] == "builder-assistant")
    pinned = builder["prompt_version_ids"]["system"]
    version = next(v for v in db["agent_prompt_versions"] if v["prompt_version_id"] == pinned)
    version["content"] = "Edited by someone"
    builder["status"] = "archived"
    seed.seed(client)
    builder = next(p for p in db["agent_profiles"] if p["slug"] == "builder-assistant")
    served = next(v for v in db["agent_prompt_versions"]
                  if v["prompt_version_id"] == builder["prompt_version_ids"]["system"])
    from demo_mode import content

    assert served["content"] == content.BUILDER_INSTRUCTIONS and served["version_number"] == 2
    assert builder["status"] == "specialist"


def test_turn_survives_leaving_the_page(client, demo, monkeypatch):
    """The panel reads a turn back after a page load: its steps, answer and
    state are kept by conversation id and served by /history."""
    from admin import demo_handlers
    import common.chat_session as chat_session

    server = _FakeServer()
    monkeypatch.setattr(assistant, "_admin_mcp", lambda: server)
    sid = "abcdefab-cdef-abcd-efab-cdefabcdefab"

    class _Agent:
        profile = None

        async def run_stream(self, message, context):
            await assistant.run_tool("models", {"action": "list"})
            yield {"type": "tool_activity", "tools": ["models"]}
            yield {"type": "answer", "content": "Here are the models."}

    async def fake_run_turn(*, agent, message, identity, profile, extra_context=None):
        yield {"type": "session", "id": sid}
        async for event in agent.run_stream(message, {}):
            yield event
        yield {"type": "done"}

    monkeypatch.setattr(demo_handlers, "_build_assistant", lambda page: _Agent())
    monkeypatch.setattr(chat_session, "run_turn", fake_run_turn)
    monkeypatch.setattr("core.auth.get_service_client", lambda: _Client({}))
    client.post("/admin/demo/assistant/chat", json={"message": "models?"},
                headers={"Authorization": f"Bearer {_token('lv-1')}"})
    resp = client.get(f"/admin/demo/assistant/history?session_id={sid}")
    live = resp.json()["live"]
    assert live["done"] is True and live["answer"] == "Here are the models."
    assert [s["label"] for s in live["steps"]] == ["Looked at the models"]


def test_panel_keeps_suggestions_and_about_note(client, demo):
    page = client.get("/admin/settings").text
    assert "About this demo" in page and "Builder assistant" in page
    js = client.get("/admin/static/builder-assistant.js").text
    assert "ba-chips-compact" in js and "/admin/demo/assistant/history" in js
    assert "chips.hidden" not in js


# --------------------------------------------------------------------------
# 9. The assistant across turns, and the demo's own test round
# --------------------------------------------------------------------------


def test_assistant_remembers_what_a_turn_made_for_the_next_turn():
    assistant._ACTIONS.clear()
    steps = [
        {"tool": "templates", "action": "list", "ok": True, "label": "Looked at the templates"},
        {"tool": "templates", "action": "use", "ok": True, "label": "Created Outage helper from a template",
         "links": [{"href": "/admin/agents?profile_id=p-1&env=staging", "label": "Open the agent"}]},
        {"tool": "chat", "action": "", "ok": False, "label": "Chatted with Outage helper"},
        {"tool": "get_profile", "action": "", "ok": True, "label": "Looked at Outage helper"},
    ]
    assistant.remember("s-1", steps)
    note = assistant.earlier_note("s-1")
    # Only the change is kept, with the id a follow-up needs.
    assert "Created Outage helper from a template" in note and "profile_id=p-1" in note
    assert "Looked at" not in note and "Chatted" not in note
    assert "do not make new ones" in note
    assert assistant.earlier_note("another-conversation") == ""
    assert assistant.earlier_note(None) == ""
    assistant.remember(None, steps)  # no conversation yet: nothing to key it by
    assert set(assistant._ACTIONS) == {"s-1"}
    assistant._ACTIONS.clear()


def test_builder_instructions_say_a_follow_up_edits_and_a_draft_goes_live_first():
    from demo_mode import content

    text = content.BUILDER_INSTRUCTIONS
    assert "Never create another agent for a follow-up" in text
    # A request to build is never answered with "one already exists".
    assert 'always means a NEW agent' in text and 'Never answer a \
build request' in text
    assert 'status "active"' in text and "a draft cannot be chatted with" in text
    for name in (content.KB_NAME, content.PLANS_KB_NAME, content.BILLING_KB_NAME, content.IT_KB_NAME):
        assert name in text


def test_assistant_will_not_chat_with_a_draft(fake_admin, monkeypatch):
    monkeypatch.setattr(assistant, "_is_live", lambda profile_id: False)
    out = _run("chat", {"profile_id": "p-draft", "message": "hi"})
    assert out == {"ok": False, "error": assistant.DRAFT_MESSAGE}
    turn = assistant.TURN.get()
    assert turn.steps[-1]["ok"] is False and "draft" in turn.steps[-1]["note"]
    assert turn.calls == 0 and "chat" not in fake_admin.tools  # refused before it ran


def test_a_second_message_waits_while_the_first_turn_runs(client, demo, monkeypatch):
    """A message sent while the conversation's last turn is still running is
    answered with a wait note and builds no agent, and the next turn is told
    what the first one made."""
    import time

    from admin import demo_handlers
    import common.chat_session as chat_session

    server = _FakeServer()
    monkeypatch.setattr(assistant, "_admin_mcp", lambda: server)
    assistant._ACTIONS.clear()
    sid = "abcdefab-cdef-abcd-efab-cdefabcdef01"
    built: List[str] = []
    seen: Dict[str, Any] = {}

    class _Agent:
        profile = None

        async def run_stream(self, message, context):
            seen["note"] = assistant.earlier_note(assistant.TURN.get().conversation)
            await assistant.run_tool("update_profile", {"profile_id": "p-9", "display_name": "Ali"})
            yield {"type": "tool_activity", "tools": ["update_profile"]}
            yield {"type": "answer", "content": "Renamed."}

    async def fake_run_turn(*, agent, message, identity, profile, extra_context=None):
        yield {"type": "session", "id": sid}
        async for event in agent.run_stream(message, {}):
            yield event
        yield {"type": "done"}

    def _build(page):
        built.append(page or "")
        return _Agent()

    monkeypatch.setattr(demo_handlers, "_build_assistant", _build)
    monkeypatch.setattr(chat_session, "run_turn", fake_run_turn)
    monkeypatch.setattr(assistant, "_profile_name", lambda profile_id: "Outage helper")
    monkeypatch.setattr("core.auth.get_service_client", lambda: _Client({}))
    headers = {"Authorization": f"Bearer {_token('lv-2')}"}

    # The first turn is still running: the second message waits.
    user_id = client.get(f"/admin/demo/assistant/history?session_id={sid}", headers=headers)
    assert user_id.status_code == 200
    demo_handlers._LIVE[sid] = {"answer": "", "steps": [], "changed": False, "done": False,
                                "at": time.monotonic(), "user_id": None}
    first = client.post("/admin/demo/assistant/chat", json={"message": "x", "session_id": sid},
                        headers=headers)
    # Another visitor's (or nobody's) live turn does not block this one.
    assert demo_handlers.STILL_WORKING not in first.text and len(built) == 1
    live_user = demo_handlers._LIVE[sid]["user_id"]
    demo_handlers._LIVE[sid] = {"answer": "", "steps": [], "changed": False, "done": False,
                                "at": time.monotonic(), "user_id": live_user}
    waiting = client.post("/admin/demo/assistant/chat", json={"message": "name it ali",
                                                              "session_id": sid}, headers=headers)
    assert demo_handlers.STILL_WORKING in waiting.text and len(built) == 1
    # Once it is done, the follow-up runs and is told what the first turn changed.
    demo_handlers._LIVE[sid]["done"] = True
    client.post("/admin/demo/assistant/chat", json={"message": "name it ali", "session_id": sid},
                headers=headers)
    assert len(built) == 2
    assert "profile_id=p-9" in seen["note"] or "Outage helper" in seen["note"] or "Ali" in seen["note"]
    demo_handlers._LIVE.pop(sid, None)
    assistant._ACTIONS.clear()


def test_warmup_runs_each_suite_then_compares_models(db, monkeypatch):
    from admin.services import bakeoffs, evals
    from demo_mode import account, content, warmup

    client = _Client(db)
    seed.seed(client)
    started: List[Dict[str, Any]] = []
    compared: List[Dict[str, Any]] = []

    def _start(**kw):
        run_id = f"run-{len(db.setdefault('agent_eval_runs', []))}"
        db["agent_eval_runs"].append({"run_id": run_id, "status": "passed"})
        started.append(kw)
        return run_id

    def _compare(**kw):
        compared.append(kw)
        ids = [_start(suite_id=kw["suite_ids"][0], profile_id=kw["profile_id"], bakeoff=True)
               for _ in kw["variants"]]
        return {"bakeoff_id": f"b-{len(compared)}", "run_ids": ids}

    monkeypatch.setattr(evals, "start_suite_run", _start)
    monkeypatch.setattr(bakeoffs, "create_bakeoff", _compare)
    settled: List[str] = []
    monkeypatch.setattr(bakeoffs, "get_bakeoff", lambda bakeoff_id: settled.append(bakeoff_id))
    monkeypatch.setattr(account, "sign_in", lambda: "token")
    monkeypatch.setattr(account, "ensure_demo_user", lambda: {"user_id": "demo-user"})
    monkeypatch.setattr(warmup, "run_chats", lambda token, env: 7)

    out = warmup.run_round(client, sleep=lambda _s: None)
    assert len(out["runs"]) == 4 and len(out["comparisons"]) == 4
    # The sample-traffic generator holds the conversations; the round holds none.
    assert out["chat_turns"] == 0
    scheduled = [k for k in started if not k.get("bakeoff")]
    assert all(k["user_token"] == "token" and k["actor_user_id"] == "demo-user" for k in scheduled)
    assert {k["schedule_id"] for k in scheduled} == {x["schedule_id"] for x in db["agent_eval_schedules"]}
    assert all(x.get("last_run_at") for x in db["agent_eval_schedules"])
    models = {m["name"]: m["model_id"] for m in db["agent_models"]}
    for kw in compared:
        assert [v["model_id"] for v in kw["variants"]] == [models["Demo agents model"],
                                                         models["Demo builder model"]]
        assert kw["repeats"] == 1 and len(kw["suite_ids"]) == 1
    assert len(compared) == len(content.SUITES)
    assert settled == out["comparisons"]  # each comparison's status is brought up to date
    # A second round (a restart) does nothing: the runs are there.
    assert warmup.run_round(client, sleep=lambda _s: None) == {"skipped": "runs exist"}
    assert len(started) == 12


def test_warmup_is_off_outside_demo_mode_and_by_setting(monkeypatch):
    from demo_mode import warmup

    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert warmup.enabled() is False and warmup.start_in_background() is None
    monkeypatch.setenv("DEMO_MODE", "true")
    assert warmup.enabled() is True
    monkeypatch.setenv("DEMO_WARMUP", "off")
    assert warmup.enabled() is False


def test_warmup_holds_real_conversations_one_session_each(demo, monkeypatch):
    from admin_mcp import tools_dispatch
    from demo_mode import warmup

    posted: List[Dict[str, Any]] = []

    async def _post(endpoint, body, headers, env, timeout):
        posted.append({"endpoint": endpoint, **body, "auth": headers["Authorization"], "env": env,
                       "origin": headers.get("Origin")})
        if "sales" in endpoint and len([p for p in posted if "sales" in p["endpoint"]]) == 1:
            return 503, "application/json", "{}", "in-process"   # one agent is unavailable
        return 200, "text/event-stream", 'data: {"type": "answer", "content": "ok"}\n\n', "in-process"

    monkeypatch.setattr(tools_dispatch, "_post_chat", _post)
    answered = warmup.run_chats("tok", "staging")
    assert all(p["auth"] == "Bearer tok" and p["env"] == "staging" for p in posted)
    # Each names the sample channel's origin, so it is not listed under "Unknown".
    assert {p["origin"] for p in posted} == {"https://sample-traffic.northwind.example"}
    assert all(p["endpoint"].startswith("/v1/agents/") and p["endpoint"].endswith("/chat")
               for p in posted)
    # A conversation's turns share one session; different conversations do not.
    by_session: Dict[str, List[str]] = {}
    for p in posted:
        by_session.setdefault(p["session_id"], []).append(p["endpoint"])
    assert len(by_session) == len(warmup.CHATS)
    assert all(len(set(v)) == 1 for v in by_session.values())
    # The agent that answered 503 is skipped after its first message; the rest go on.
    total = sum(len(m) for _s, m in warmup.CHATS)
    assert answered == total - 2 and len(posted) == total - 1
