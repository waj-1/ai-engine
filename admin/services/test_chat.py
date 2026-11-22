"""How to talk to an agent profile from the portal's Test drawer and the admin
MCP `chat` tool — one definition so the two can't drift.

Every chat endpoint takes `message` / `session_id` / `locale`, but they do NOT
share one body. The generic shape the drawer used to send (`business_id` +
`agent_kind`) is what `/chat/stream` wants; the AI-agents demo endpoint
(`POST /agents/chat/stream`, `endpoints/agents_portal.AgentsChatRequest`)
requires `agent` — the demo's agent KEY (`support`, `sales`, …), not the
`agent_kind` — and plays a `case_id`, so every demo agent answered 422
`body.agent: Field required`. Kinds whose endpoint is not a chat stream at all
are reported unsupported instead of offering a Test button that can only
fail.

`request_shape(profile)` returns:
  supported      bool — False → no Test drawer / MCP chat refuses with `reason`
  reason         why unsupported (plain language)
  endpoint       the route to POST to
  extra_body     fields merged into the generic body (e.g. {"agent": "sales",
                 "case_id": "O01"})
  needs_business True when the endpoint scopes the turn to a WAJ business
  cases          [{"id", "label"}] — demo agents play one; the drawer offers a
                 picker, `extra_body["case_id"]` is the first
  state_token    True when the endpoint streams a `{"type": "state",
                 "token": …}` event the NEXT turn must echo back as `state`
                 (the demo's stateless text turns carry verification there)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from admin.agent_kinds import catalog_tuple as _catalog_tuple

# Endpoints that are not a conversational stream: a test chat can only 422
# (a plugin names its own, `agent_catalog.not_chat_endpoints()`).
_NOT_CHAT_ENDPOINTS = set(_catalog_tuple("not_chat_endpoints"))

# Kinds that pick their own tenant server-side (no business picker).
_NO_BUSINESS_KINDS = set(_catalog_tuple("tenantless_kinds"))


def _demo_agent_key(kind: str) -> Optional[str]:
    try:
        from runtime.tools.agents_demo import data
    except Exception:  # noqa: BLE001 — a build without the demo package
        return None
    for key, meta in data.AGENTS.items():
        if meta.kind == kind:
            return key
    return None


def _demo_cases(key: str) -> List[Dict[str, str]]:
    from runtime.tools.agents_demo import data

    out = []
    for c in data.cases(key):
        label = c.get("title_en") or c.get("title") or c.get("name_en") or ""
        out.append({"id": c["id"], "label": f"{c['id']} — {label}" if label else c["id"]})
    return out


def request_shape(profile: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(profile.get("agent_kind") or "")
    endpoint = (profile.get("endpoint_path") or "").strip()
    if endpoint and not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    shape: Dict[str, Any] = {
        "supported": True, "reason": "", "endpoint": endpoint,
        "extra_body": {}, "needs_business": kind not in _NO_BUSINESS_KINDS,
        "cases": [], "state_token": False,
    }

    demo_key = _demo_agent_key(kind)
    if demo_key:
        from core.deployment import serves_agents_portal

        if not serves_agents_portal():
            return {**shape, "supported": False,
                    "reason": "This build doesn't serve the AI-agents demo, so "
                              "there's nothing to chat with here."}
        cases = _demo_cases(demo_key)
        extra = {"agent": demo_key}
        if cases:
            extra["case_id"] = cases[0]["id"]
        return {**shape, "endpoint": "/agents/chat/stream", "extra_body": extra,
                "needs_business": False, "cases": cases, "state_token": True}

    if kind == "custom":
        # A generic agent's address is derived from its slug, never stored on
        # the row (endpoints/agents_v1.py), and it has no business scope.
        from endpoints.agents_v1 import generic_agents_enabled

        slug = str(profile.get("slug") or "")
        if not generic_agents_enabled() or not slug:
            return {**shape, "supported": False,
                    "reason": "The generic agents API is switched off on this "
                              "server (ENABLE_GENERIC_AGENTS), so this agent "
                              "has no address to send a test message to."}
        # `pin_profile`: a test run is about THIS profile, published or not.
        # The runners send it as X-WAJ-Profile-Id (as the Test panel does), so
        # a draft can be tested before it is published; without it every case
        # of a draft's suite came back a bare 404 (2026-11-22).
        return {**shape, "endpoint": f"/v1/agents/{slug}/chat", "needs_business": False,
                "pin_profile": profile.get("profile_id") or None}

    if not endpoint:
        return {**shape, "supported": False,
                "reason": "This agent has no chat address, so there's nothing "
                          "to send a test message to."}
    if endpoint in _NOT_CHAT_ENDPOINTS:
        return {**shape, "supported": False,
                "reason": "This agent isn't a chat agent — it answers app "
                          "requests, not messages."}
    return shape


def build_body(shape: Dict[str, Any], *, message: str, session_id: str,
               locale: str, business_id: Optional[str], agent_kind: str,
               case_id: Optional[str] = None,
               state: Optional[str] = None) -> Dict[str, Any]:
    """The JSON body for one turn — the generic fields plus the kind's own."""
    body: Dict[str, Any] = {"message": message, "session_id": session_id,
                            "locale": locale or "en"}
    if shape.get("needs_business"):
        body["business_id"] = business_id or None
        body["agent_kind"] = agent_kind
    body.update(shape.get("extra_body") or {})
    if case_id and shape.get("cases"):
        body["case_id"] = case_id
    if state and shape.get("state_token"):
        body["state"] = state
    return body


def own_body_shape(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``request_shape`` for kinds whose endpoint needs its OWN body (the
    AI-agents demo: ``agent`` + ``case_id`` + the state token) or whose
    address is not stored on the row (a generic ``custom`` agent), else None —
    the caller keeps its generic body. The eval / E2E runners use this so a
    test-suite run speaks to a demo agent exactly like the Test drawer does
    (every case used to 422 ``body.agent: Field required``). A returned shape
    may be ``supported: False`` (this build doesn't serve the demo)."""
    kind = str((profile or {}).get("agent_kind") or "")
    # A generic agent has no stored endpoint_path: its address comes from its
    # slug. Without this a suite run against one errored "profile has no
    # endpoint_path" before sending anything (2026-11-22).
    if not _demo_agent_key(kind) and kind != "custom":
        return None
    return request_shape(profile)


def state_token_from_sse(body: str) -> Optional[str]:
    """The last ``{"type": "state", "token": …}`` event of an SSE body — the
    demo's next turn must echo it back as ``state``."""
    import json
    import re

    token = None
    for m in re.finditer(r"^data:\s*(\{.*\})\s*$", body or "", re.M):
        try:
            evt = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(evt, dict) and evt.get("type") == "state" and evt.get("token"):
            token = str(evt["token"])
    return token


def voice_target(profile: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Where a voice eval reaches a demo agent on THIS build: the duplex WS
    path (``/agents/voice/live`` on the agents build, ``/voice/live`` on the
    full engine) and the case the call plays. None for other kinds."""
    shape = own_body_shape(profile)
    if not shape or not shape.get("supported"):
        return None
    from core.deployment import active_scope

    client = active_scope()
    out = {"ws_path": (client.portal_prefix if client else "") + "/voice/live"}
    if (shape.get("extra_body") or {}).get("case_id"):
        out["call_case"] = shape["extra_body"]["case_id"]
    return out
