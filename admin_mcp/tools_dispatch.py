"""Dispatch-domain MCP tools: talk to an agent.

`chat` is the MCP twin of the portal's floating test-chat widget
(admin/static/test-chat.js): it POSTs one turn to the profile's own
`endpoint_path` with the OPERATOR's JWT and the same body the widget sends
({message, session_id, locale, business_id, agent_kind}), collects the SSE
stream server-side, then looks the turn up the way the portal does — the
`agent_traces` row for the session (trace id) and the persisted assistant
message (tool calls). Nothing here re-implements an agent: the request goes
through the real route, middleware and agent code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from typing import Annotated, Any, Dict, List, Tuple

from pydantic import Field

from admin_mcp._common import Env, client_for_env, err, get_row, rows, tool_errors, \
    use_env, validate_env
from admin_mcp.context import current_actor
from admin_mcp.server import mcp

logger = logging.getLogger(__name__)

# SSE event types whose `content` is reply text (mirrors common/sse.py's
# _CONTENT_TYPES minus `chart`). `answer` is the streamed form every chat
# agent uses; the others are single-shot fallbacks a few endpoints emit.
_REPLY_TYPES = ("answer",)
_FALLBACK_REPLY_TYPES = ("final", "result", "final_response", "output")
_TOOL_EVENT_TYPES = ("tool_call", "tool_activity", "action")
_TOOL_OUTPUT_CAP = 2000


def _app_for_in_process():
    """The running FastAPI app. Prod starts `uvicorn main:app`, so `main` is
    in sys.modules; `python main.py` runs it as __main__. Importing `main`
    fresh is the last resort (a second copy would re-run module setup)."""
    for name in ("main", "__main__"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "app", None) is not None:
            return mod.app
    import main  # noqa: WPS433 — last resort, see docstring
    return main.app


def _process_env() -> str:
    from core.agent_profile import _default_environment
    return _default_environment()


async def _post_chat(endpoint: str, body: Dict[str, Any], headers: Dict[str, str],
                     env: str, timeout_seconds: int) -> Tuple[int, str, str, str]:
    """POST one chat turn. Returns (status, content_type, body_text, target).

    Same env as this process → in-process through the app itself (what the
    portal widget does: a same-origin POST to the control plane, which
    serves every agent route). Another env → that env's default deployment
    target (the same resolution the E2E runner uses)."""
    import httpx

    if env == _process_env():
        transport = httpx.ASGITransport(app=_app_for_in_process())
        base_url, target = "http://in-process", "in-process"
    else:
        from admin.e2e_runner import _base_url_for
        with use_env(env):
            base_url = await asyncio.to_thread(_base_url_for, env)
        transport, target = None, base_url
    async with httpx.AsyncClient(transport=transport, base_url=base_url,
                                 timeout=timeout_seconds) as client:
        resp = await client.post(endpoint, json=body, headers=headers)
        return (resp.status_code, resp.headers.get("content-type", ""),
                resp.text or "", target)


def parse_sse(text: str) -> Dict[str, Any]:
    """Collapse an SSE body into {reply, errors, tool_events, session_id,
    event_counts}. Tolerates a non-SSE JSON body (some endpoints answer
    with plain JSON) by returning its text as the reply."""
    reply: List[str] = []
    fallback: List[str] = []
    errors: List[str] = []
    tool_events: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    session_id = None
    state_token = None
    saw_data = False
    for block in (text or "").split("\n\n"):
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                evt = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(evt, dict):
                continue
            saw_data = True
            etype = str(evt.get("type") or "")
            counts[etype] = counts.get(etype, 0) + 1
            content = evt.get("content")
            if etype in _REPLY_TYPES and isinstance(content, str):
                reply.append(content)
            elif etype in _FALLBACK_REPLY_TYPES and isinstance(content, str):
                fallback.append(content)
            elif etype == "error":
                errors.append(str(evt.get("message") or evt.get("content") or evt))
            elif etype == "session":
                session_id = evt.get("id") or session_id
            elif etype == "state" and isinstance(evt.get("token"), str):
                state_token = evt["token"]
            elif etype in _TOOL_EVENT_TYPES:
                tool_events.append({k: v for k, v in evt.items()
                                    if k in ("type", "name", "tool", "args",
                                             "input", "status", "action")})
    if not saw_data:
        body = (text or "").strip()
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                body = str(parsed.get("response") or parsed.get("answer")
                           or parsed.get("detail") or body)
        except ValueError:
            pass
        return {"reply": body, "errors": [], "tool_events": [],
                "session_id": None, "event_counts": {}, "state": None}
    return {"reply": "".join(reply) or "".join(fallback), "errors": errors,
            "tool_events": tool_events, "session_id": session_id,
            "event_counts": counts, "state": state_token}


def _lookup_turn(session_id: str, env: str) -> Dict[str, Any]:
    """Trace id + persisted tool calls for the turn just sent (sync — run
    it off the loop). Same sources as the portal: agent_traces (what the
    /admin/sessions/{id} redirect reads) and the newest assistant row of
    ai_chat_messages (what the Transcript tab renders tool calls from)."""
    from admin.screens.observability import (_coerce_session_id_for_lookup,
                                             _persisted_tool_calls)
    lookup = _coerce_session_id_for_lookup(session_id) or session_id
    client = client_for_env(env)
    out: Dict[str, Any] = {"trace_id": None, "tool_calls": []}
    try:
        traces = rows(client.table("agent_traces")
                      .select("lmnr_trace_id, created_at")
                      .eq("session_id", lookup)
                      .order("created_at", desc=True).limit(1).execute())
        if traces:
            out["trace_id"] = traces[0].get("lmnr_trace_id")
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment
        logger.debug("chat: trace lookup failed for %s: %s", lookup, exc)
    try:
        msgs = rows(client.table("ai_chat_messages")
                    .select("role, metadata, created_at")
                    .eq("session_id", lookup)
                    .order("created_at", desc=True).limit(4).execute())
        last = next((m for m in msgs if m.get("role") == "assistant"), None)
        meta = (last or {}).get("metadata")
        if isinstance(meta, dict):
            calls = _persisted_tool_calls(meta)
            for c in calls:
                if len(c.get("output") or "") > _TOOL_OUTPUT_CAP:
                    c["output"] = c["output"][:_TOOL_OUTPUT_CAP] + "…"
                    c["output_truncated"] = True
            out["tool_calls"] = calls
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment
        logger.debug("chat: message lookup failed for %s: %s", lookup, exc)
    return out


@mcp.tool()
@tool_errors
async def chat(
        profile_id: Annotated[str, Field(
            description="Profile to talk to (list_profiles). Its endpoint_path "
                        "is the route the message is sent to.")],
        message: Annotated[str, Field(description="The user message for this turn.")],
        business_id: Annotated[str, Field(
            description="Business UUID to chat as (required by agents that "
                        "read one business's data; '' = none).")] = "",
        session_id: Annotated[str, Field(
            description="Continue this conversation ('' = start a new one; "
                        "the new id is returned).")] = "",
        env: Env = "staging",
        locale: Annotated[str, Field(description="en | ar | tr | ru.")] = "en",
        profile_version_id: Annotated[str, Field(
            description="Answer with this saved agent_profile_versions snapshot "
                        "(X-WAJ-Profile-Version, operator-only) instead of "
                        "profile_id's current config.")] = "",
        case_id: Annotated[str, Field(
            description="AI-agents demo kinds only: the use case to play "
                        "(e.g. 'O01'; '' = the agent's first case).")] = "",
        state: Annotated[str, Field(
            description="AI-agents demo kinds only: the `state` token the "
                        "previous turn returned — pass it back with session_id "
                        "to continue (it carries verification/sent status).")] = "",
        timeout_seconds: Annotated[int, Field(
            description="Give up after this many seconds (10-300).")] = 120,
) -> Dict[str, Any]:
    """Run ONE chat turn against an agent through the same path the
    portal's test-chat widget uses: a POST to the profile's endpoint_path
    with your operator JWT, collected server-side. Returns {reply,
    tool_calls, trace_id, session_id, latency_ms, errors, served_by,
    status}. Pass the returned session_id back to continue the
    conversation.

    Which config answers: profile_id ITSELF — a draft copy or a specialist
    answers as itself (X-WAJ-Profile-Id, operator-only, same agent_kind as
    the endpoint), exactly like the widget. profile_version_id instead pins a
    saved snapshot. Each kind's own body is built for you (the AI-agents
    demo's `agent` key + case_id); pass back the returned `state` with
    session_id to continue a demo conversation.
    env = this process's own environment runs in-process; the other env
    goes to its default deployment target (deployment_targets).

    tool_calls come from the persisted assistant message (name, input,
    output — outputs capped at 2000 chars); trace_id is the turn's trace
    (open with traces action=overview / transcript). Both can be null if
    the agent doesn't persist/trace that turn; the reply is still
    returned. This is a REAL turn: it runs the agent's tools and writes
    chat history, as the widget does."""
    validate_env(env)
    if not message.strip():
        return err("message is empty")
    client = client_for_env(env)
    profile = await asyncio.to_thread(get_row, client, "agent_profiles",
                                      "profile_id", profile_id)
    if not profile:
        return err("profile not found", hint="list_profiles shows valid ids")
    from admin.services.test_chat import build_body, request_shape

    shape = request_shape(profile)
    if not shape["supported"]:
        return err(shape["reason"],
                   hint="set an endpoint_path with update_profile(endpoint_path=...)"
                   if not (profile.get("endpoint_path") or "").strip() else None)
    endpoint = shape["endpoint"]

    warning = None
    if profile_version_id:
        served_by = f"profile version {profile_version_id}"
    else:
        served_by = (f"profile {profile_id} "
                     f"({profile.get('display_name') or profile.get('agent_kind')}, "
                     f"status={profile.get('status')})")
        if not profile.get("is_active"):
            warning = (f"profile {profile_id} is switched off (is_active=false), "
                       "so the pin is ignored and the ACTIVE profile for "
                       f"{profile.get('agent_kind')!r} answers instead.")

    sid = session_id or str(uuid.uuid4())
    body = build_body(shape, message=message, session_id=sid, locale=locale,
                      business_id=business_id or None,
                      agent_kind=str(profile.get("agent_kind") or ""),
                      case_id=case_id or None, state=state or None)
    headers = {"Authorization": f"Bearer {current_actor()['user_token']}",
               "Accept": "text/event-stream, application/json;q=0.9, */*;q=0.5"}
    if profile_version_id:
        headers["X-WAJ-Profile-Version"] = profile_version_id
    else:
        headers["X-WAJ-Profile-Id"] = profile_id

    started = time.monotonic()
    try:
        status, _ctype, text, target = await _post_chat(
            endpoint, body, headers, env, max(10, min(300, timeout_seconds)))
    except Exception as exc:  # noqa: BLE001 — surface transport failures as err()
        return err(f"chat request failed: {type(exc).__name__}: {exc}",
                   endpoint=endpoint, session_id=sid)
    latency_ms = int((time.monotonic() - started) * 1000)
    parsed = parse_sse(text)
    if status >= 400:
        return err(f"endpoint returned HTTP {status}", endpoint=endpoint,
                   detail=(text or "")[:1000], session_id=sid)
    sid = parsed.get("session_id") or sid

    found: Dict[str, Any] = {"trace_id": None, "tool_calls": []}
    for attempt in range(3):  # the trace row can land just after the stream ends
        found = await asyncio.to_thread(_lookup_turn, sid, env)
        if found.get("trace_id"):
            break
        await asyncio.sleep(0.5 * (attempt + 1))

    out: Dict[str, Any] = {
        "reply": parsed["reply"],
        "tool_calls": found["tool_calls"] or parsed["tool_events"],
        "trace_id": found["trace_id"],
        "session_id": sid,
        "latency_ms": latency_ms,
        "errors": parsed["errors"],
        "status": status,
        "endpoint": endpoint,
        "target": target,
        "served_by": served_by,
        "event_counts": parsed["event_counts"],
    }
    if parsed.get("state"):
        out["state"] = parsed["state"]
    if warning:
        out["warning"] = warning
    return out
