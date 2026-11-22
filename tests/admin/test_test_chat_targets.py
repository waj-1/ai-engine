"""The Test drawer (and the admin MCP `chat` tool) must work for every agent
kind on every build, and must be answered by the profile being VIEWED.

* `request_shape` — the AI-agents demo endpoint requires `agent` (its key) and
  plays a `case_id`; the old generic body (business_id + agent_kind) got a 422
  `body.agent: Field required` for all four demo agents on the partner build.
* X-WAJ-Profile-Id — operator-gated, same-kind-only profile pin, so a draft
  copy or a specialist answers its own test chat instead of the kind's
  active profile.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from starlette.requests import Request

from admin.services.test_chat import build_body, request_shape
from core.agent_profile import AgentProfileLoader, Profile
from tests.scoped_build import full_build, use_orchard, use_agents_demo


def _p(kind, endpoint):
    return {"profile_id": "p", "agent_kind": kind, "endpoint_path": endpoint}


# ---- request shapes ---------------------------------------------------------

def test_demo_kinds_send_agent_key_and_first_case(monkeypatch):
    use_agents_demo(monkeypatch)
    for kind, key, first in (("agents_support", "support", "S01"),
                             ("agents_inquiries", "inquiries", "I01"),
                             ("agents_collection", "collection", "C01"),
                             ("agents_sales", "sales", "O01")):
        shape = request_shape(_p(kind, "/agents/chat/stream"))
        assert shape["supported"] and shape["endpoint"] == "/agents/chat/stream"
        assert shape["extra_body"] == {"agent": key, "case_id": first}
        assert shape["state_token"] and not shape["needs_business"]
        assert shape["cases"][0]["id"] == first
        body = build_body(shape, message="hi", session_id="s", locale="en",
                          business_id=None, agent_kind=kind, state="tok")
        assert body["agent"] == key and body["state"] == "tok"
        assert "business_id" not in body


def test_demo_kinds_on_the_full_build(monkeypatch):
    full_build(monkeypatch)
    assert request_shape(_p("agents_sales", "/agents/chat/stream"))["supported"]


def test_demo_kinds_unsupported_where_the_portal_is_not_served(monkeypatch):
    use_orchard(monkeypatch)
    shape = request_shape(_p("agents_sales", "/agents/chat/stream"))
    assert not shape["supported"] and "demo" in shape["reason"]


def test_business_and_tenantless_shapes(monkeypatch):
    # A plugin names the kinds that pick their own tenant (agent_catalog.tenantless_kinds).
    from admin.services import test_chat
    monkeypatch.setattr(test_chat, "_NO_BUSINESS_KINDS", {"orchard"})
    full_build(monkeypatch)
    biz = request_shape(_p("business", "/chat/stream"))
    assert biz["supported"] and biz["needs_business"] and biz["extra_body"] == {}
    body = build_body(biz, message="hi", session_id="s", locale="en",
                      business_id="b1", agent_kind="business")
    assert body["business_id"] == "b1" and body["agent_kind"] == "business"
    geo = request_shape(_p("orchard", "/orchard/chat/stream"))
    assert geo["supported"] and not geo["needs_business"]


def test_non_chat_and_missing_endpoints_are_unsupported(monkeypatch):
    # A plugin names its non-chat endpoints (agent_catalog.not_chat_endpoints).
    from admin.services import test_chat
    monkeypatch.setattr(test_chat, "_NOT_CHAT_ENDPOINTS", {"/sample/suggest", "/sample/replies"})
    for endpoint in ("/sample/suggest", "/sample/replies", "", None):
        assert not request_shape(_p("sample_agent", endpoint))["supported"]


# ---- the profile pin ----------------------------------------------------------

def _profile(kind="business", pid="p1"):
    return Profile(profile_id=pid, agent_kind=kind, slug="s", environment="staging",
                   model_main={"model_string": "m"}, model_light=None,
                   prompt_version_ids={}, tool_python_names=[], data_source_ids=[],
                   mcp_server_ids=[], mcp_attachments=[], escalation_policy={},
                   voice_config={}, memory_config={}, is_fallback=False)


def _client_with(rows):
    q = MagicMock()
    for m in ("select", "eq", "limit"):
        getattr(q, m).return_value = q
    q.execute.return_value = MagicMock(data=rows)
    c = MagicMock()
    c.table.return_value = q
    return c


def test_load_pinned_builds_the_row_in_its_own_environment():
    row = {"profile_id": "draft-1", "agent_kind": "business", "status": "draft",
           "environment": "staging", "is_active": True}
    with patch("core.agent_profile.get_service_client", return_value=_client_with([row])), \
         patch.object(AgentProfileLoader, "_build_profile",
                      return_value=_profile(pid="draft-1")) as build:
        prof = AgentProfileLoader.load_pinned("draft-1", "business")
    assert prof.profile_id == "draft-1"
    assert build.call_args.args[1] is row and build.call_args.args[2] == "staging"


def test_load_pinned_refuses_another_kind_or_a_template():
    for row in ({"profile_id": "x", "agent_kind": "sales", "status": "draft",
                 "environment": "staging"},
                {"profile_id": "x", "agent_kind": "business", "status": "template",
                 "environment": "staging"}):
        with patch("core.agent_profile.get_service_client", return_value=_client_with([row])), \
             patch.object(AgentProfileLoader, "_build_profile") as build:
            assert AgentProfileLoader.load_pinned("x", "business") is None
        build.assert_not_called()
    with patch("core.agent_profile.get_service_client", return_value=_client_with([])):
        assert AgentProfileLoader.load_pinned("gone", "business") is None


def test_runtime_agent_answers_as_the_pinned_profile():
    from runtime.agent import RuntimeAgent

    with patch("core.profile_version_resolver.get_profile_version", return_value=None), \
         patch("core.profile_version_resolver.get_profile_pin", return_value="draft-1"), \
         patch.object(RuntimeAgent, "_build_llm", return_value=None), \
         patch.object(AgentProfileLoader, "load_pinned",
                      return_value=_profile(pid="draft-1")) as lp, \
         patch.object(AgentProfileLoader, "load_active") as la:
        agent = RuntimeAgent(agent_kind="business", environment="staging")
    lp.assert_called_once_with("draft-1", "business")
    la.assert_not_called()
    assert agent.profile.profile_id == "draft-1"


def test_runtime_agent_falls_back_when_the_pin_is_unusable():
    from runtime.agent import RuntimeAgent

    with patch("core.profile_version_resolver.get_profile_version", return_value=None), \
         patch("core.profile_version_resolver.get_profile_pin", return_value="other-kind"), \
         patch.object(RuntimeAgent, "_build_llm", return_value=None), \
         patch.object(AgentProfileLoader, "load_pinned", return_value=None), \
         patch.object(AgentProfileLoader, "load_active",
                      return_value=_profile(pid="active-1")) as la:
        agent = RuntimeAgent(agent_kind="business", environment="staging")
    la.assert_called_once()
    assert agent.profile.profile_id == "active-1"


def test_version_pin_beats_profile_pin():
    from runtime.agent import RuntimeAgent

    with patch("core.profile_version_resolver.get_profile_version", return_value="v9"), \
         patch("core.profile_version_resolver.get_profile_pin", return_value="draft-1"), \
         patch.object(RuntimeAgent, "_build_llm", return_value=None), \
         patch.object(AgentProfileLoader, "load_version", return_value=_profile()) as lv, \
         patch.object(AgentProfileLoader, "load_pinned") as lp:
        RuntimeAgent(agent_kind="business", environment="staging")
    lv.assert_called_once()
    lp.assert_not_called()


PIN = "11111111-2222-3333-4444-555555555555"


async def _run_middleware(headers, operator):
    import main
    from core.profile_version_resolver import clear_profile_pin, get_profile_pin

    clear_profile_pin()
    seen = {}

    async def call_next(_req):
        seen["pin"] = get_profile_pin()
        return object()

    req = Request({"type": "http", "headers": headers, "method": "POST", "path": "/"})
    with patch.object(main, "_request_is_operator", return_value=operator):
        await main.profile_version_override_middleware(req, call_next)
    return seen["pin"], get_profile_pin()


async def test_middleware_pins_profile_for_an_operator_only():
    during, after = await _run_middleware([(b"x-waj-profile-id", PIN.encode())], True)
    assert during == PIN and after is None
    during, _ = await _run_middleware([(b"x-waj-profile-id", PIN.encode())], False)
    assert during is None


async def test_middleware_ignores_a_malformed_profile_id():
    during, _ = await _run_middleware([(b"x-waj-profile-id", b"not-a-uuid'; drop")], True)
    assert during is None


def test_generic_agent_is_tested_at_its_slug_address(monkeypatch):
    """A `custom` agent stores no endpoint_path: its address comes from its
    slug, and it has no business scope to pick."""
    from admin.services import test_chat

    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    shape = test_chat.request_shape({"agent_kind": "custom", "slug": "helpdesk", "endpoint_path": None})
    assert shape["supported"] is True
    assert shape["endpoint"] == "/v1/agents/helpdesk/chat"
    assert shape["needs_business"] is False and shape["extra_body"] == {}


def test_generic_agent_is_not_testable_while_the_api_is_off(monkeypatch):
    from admin.services import test_chat

    monkeypatch.delenv("ENABLE_GENERIC_AGENTS", raising=False)
    shape = test_chat.request_shape({"agent_kind": "custom", "slug": "helpdesk"})
    assert shape["supported"] is False and "ENABLE_GENERIC_AGENTS" in shape["reason"]


def test_test_runners_get_a_generic_agents_address_from_its_slug(monkeypatch):
    """A generic agent stores no endpoint_path. The eval and E2E runners ask
    own_body_shape for the address before they check the stored one; without
    it every suite run errored "profile has no endpoint_path"."""
    from admin.services.test_chat import own_body_shape

    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    shape = own_body_shape({"agent_kind": "custom", "slug": "support-desk", "endpoint_path": None})
    assert shape["supported"] and shape["endpoint"] == "/v1/agents/support-desk/chat"
    assert shape["needs_business"] is False
    # Kinds with a stored address and the generic body are left to the caller.
    assert own_body_shape({"agent_kind": "business", "endpoint_path": "/chat/stream"}) is None


def test_runners_resolve_the_address_before_refusing_a_missing_endpoint():
    """Pins WHERE the fix lives: in each runner the kind's own address is
    worked out above the "has no endpoint_path" refusal."""
    import inspect

    from admin import e2e_runner, eval_runs_handlers, evals

    for module in (evals, eval_runs_handlers, e2e_runner):
        src = inspect.getsource(module)
        assert src.index("own_body_shape(") < src.index("no endpoint_path"), module.__name__


def test_a_test_run_pins_the_generic_agent_under_test(monkeypatch):
    """A suite run is about one profile, published or not. Without the pin a
    draft's run was sent to its public address, which only a published agent
    answers, and every case came back a bare 404."""
    from admin import evals
    from admin.services.test_chat import own_body_shape

    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    shape = own_body_shape({"profile_id": "p-draft", "agent_kind": "custom", "slug": "support-desk"})
    assert shape["pin_profile"] == "p-draft"

    seen = {}

    class _Resp:
        status_code = 200
        def iter_text(self):
            return iter(['data: {"type":"answer","content":"hi"}\n\n'])
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, json=None, headers=None):
            seen.update(url=url, headers=headers)
            return _Resp()

    monkeypatch.setattr(evals.httpx, "Client", _Client)
    evals._invoke_endpoint(base_url="http://x", endpoint=shape["endpoint"],
                           scenario={"question": "q"}, user_token="t", request_shape=shape)
    assert seen["headers"]["X-WAJ-Profile-Id"] == "p-draft"
    # A run pinned to a saved version is about that version, not the live draft.
    evals._invoke_endpoint(base_url="http://x", endpoint=shape["endpoint"],
                           scenario={"question": "q"}, user_token="t", request_shape=shape,
                           profile_version_id="v1")
    assert "X-WAJ-Profile-Id" not in seen["headers"]
