"""An OpenAI-compatible face for the generic agent API.

    GET  /v1/models              the agents this install serves, one per model
    GET  /v1/models/{id}         one of them
    POST /v1/chat/completions    one turn of the agent named by `model`

So a program written for the OpenAI API (its SDKs, a chat UI that takes an
"OpenAI-compatible" base URL) can use an agent as if it were a model: the base
URL is this engine's `/v1`, the API key is a portal-issued one, and the model
name is the agent's slug.

This module is a translation layer and nothing else. A turn is opened by
`endpoints.agents_v1.open_turn`, the same function behind
`POST /v1/agents/{slug}/chat`, so the flag (`ENABLE_GENERIC_AGENTS`), the
caller check, key scoping, the public demo's cap, the channel check, the agent
build and `run_turn` are that route's, not a copy of them. The per-IP rate
limit and the scoped-build gate on `/v1/` are `main.py` middleware and cover
these paths like any other.

Conversations
-------------
An OpenAI client keeps the conversation and resends all of it on every
request; the engine's own chat route takes one message and keeps the history
itself. Here the CLIENT'S `messages` win:

- the last message must be a `user` message: it is the turn;
- the `user` and `assistant` messages before it are the history the agent
  sees for this turn (`run_turn(history=...)`), in place of whatever the
  engine has stored. So an edited or regenerated message behaves as the client
  expects, and nothing is doubled or lost;
- the turn is still RECORDED in an engine session, so it shows up in the
  portal's Conversations and counts toward usage like any other. One client
  conversation maps to one session: named by the `X-Session-Id` request header
  when the client sends one, else by the conversation's first user message
  (plus the optional `user` field). The session id is derived from the caller
  as well, so a name one key picks can never address another key's session.
  It comes back in the `X-Engine-Session-Id` response header.

`system` and `developer` messages are dropped: an agent's instructions are its
operator's, and a chat key must not be able to replace them. Tool messages and
non-text content parts are dropped too. Sampling parameters, `tools`, `n` and
the rest are accepted and ignored. docs/openai-compatible.md is the user-facing
statement of all this.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from common.sse import SSE_HEADERS
from endpoints import agents_v1

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compatible"])

#: The longest user message a turn takes: the chat route's own limit
#: (`AgentChatRequest.message`).
MAX_MESSAGE_CHARS = 8000
#: Response header carrying the engine session a turn was recorded in.
SESSION_HEADER = "X-Engine-Session-Id"
#: `owned_by` on every model entry.
OWNED_BY = "engine"

_HISTORY_ROLES = ("user", "assistant")
_NO_MODEL_CODE = "model_unavailable"


class ChatCompletionRequest(BaseModel):
    """The fields this layer reads. Everything else an OpenAI client may send
    (temperature, tools, n, max_tokens, ...) is accepted and ignored."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(..., min_length=1, max_length=200)
    messages: List[Dict[str, Any]] = Field(..., min_length=1)
    stream: StrictBool = False
    stream_options: Optional[Dict[str, Any]] = None
    user: Optional[str] = Field(default=None, max_length=512)


class _Refused(Exception):
    """A request this layer answers itself, as an OpenAI-shaped error."""

    def __init__(self, status: int, message: str, *, code: Optional[str] = None,
                 param: Optional[str] = None):
        super().__init__(message)
        self.status, self.message, self.code, self.param = status, message, code, param


def _error_body(status: int, message: str, *, code: Optional[str] = None,
                param: Optional[str] = None) -> Dict[str, Any]:
    if status == 401:
        kind, code = "invalid_request_error", code or "invalid_api_key"
    elif status == 403:
        kind, code = "invalid_request_error", code or "insufficient_permissions"
    elif status == 429:
        kind, code = "rate_limit_error", code or "rate_limit_exceeded"
    elif status < 500:
        kind = "invalid_request_error"
    else:
        kind = "server_error"
    return {"error": {"message": message, "type": kind, "param": param, "code": code}}


def _error_response(status: int, message: str, *, code: Optional[str] = None,
                    param: Optional[str] = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content=_error_body(status, message, code=code, param=param))


def _from_http_exception(exc: HTTPException, *, model: Optional[str] = None) -> JSONResponse:
    """An `HTTPException` from the shared turn path, in OpenAI's error shape."""
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    if exc.status_code == 404 and model is not None:
        return _error_response(404, f"The model '{model}' does not exist or is not published.",
                               code="model_not_found", param="model")
    return _error_response(exc.status_code, detail)


def _text_of(content: Any) -> str:
    """A message's text: the string itself, or its `text` parts joined. Images,
    audio and files are not passed to the agent."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                parts.append(str(part.get("text") or ""))
        return "\n".join(p.strip() for p in parts if p.strip())
    return ""


def split_messages(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, str]]]:
    """(this turn's user message, the history before it) from a client's
    `messages`. Raises `_Refused` when the last message is not a user message
    with text in it."""
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        raise _Refused(400, "The last message must have the role 'user'.", param="messages")
    message = _text_of(last.get("content"))
    if not message:
        raise _Refused(400, "The last message has no text.", param="messages")
    if len(message) > MAX_MESSAGE_CHARS:
        raise _Refused(400, f"The last message is longer than {MAX_MESSAGE_CHARS} characters.",
                       param="messages", code="string_above_max_length")
    history: List[Dict[str, str]] = []
    for item in messages[:-1]:
        if not isinstance(item, dict) or item.get("role") not in _HISTORY_ROLES:
            continue  # system / developer / tool / function: see the module docstring
        text = _text_of(item.get("content"))
        if text:
            history.append({"role": item["role"], "content": text})
    return message, history


def derive_session_id(*, caller_id: str, slug: str, conversation: Optional[str],
                      end_user: Optional[str], first_user_message: str) -> str:
    """The engine session one client conversation is recorded in.

    A digest of who is calling, which agent, and what names the conversation:
    the client's `X-Session-Id` when given, else the first user message and the
    `user` field. Deterministic, so every request of one conversation lands in
    one session with nothing stored to look it up; scoped by the caller, so a
    conversation name is only ever meaningful to the key that chose it
    (`ChatSession.resolve_or_create` checks the owner again when the session is
    opened).
    """
    named = ["named", conversation] if conversation else ["opening", end_user or "", first_user_message]
    digest = hashlib.sha256(
        json.dumps(["openai-compat", caller_id, slug, *named], ensure_ascii=False).encode("utf-8")
    ).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _usage(usage: Dict[str, Any]) -> Dict[str, int]:
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _turn_error(message: str) -> Tuple[int, Optional[str]]:
    """(status, code) for an `error` event a turn ended on."""
    from core.model_resolver import NO_MODEL_MESSAGE

    if message in (NO_MODEL_MESSAGE, "This agent has no model configured."):
        return 503, _NO_MODEL_CODE
    return 500, None


def _traced(request: Request, slug: str, session_id: str):
    """The same root span `sse_response` opens for the chat route."""
    from core.observability import root_span

    return root_span("custom.agent", agents_v1.turn_trace_attributes(request, slug, session_id))


async def _stream(source: Callable[[], AsyncGenerator[Dict[str, Any], None]], *, span: Callable[[], Any],
                  completion_id: str, model: str, include_usage: bool) -> AsyncGenerator[str, None]:
    """A turn's events as `chat.completion.chunk` frames, then `[DONE]`."""
    created = int(time.time())

    def _frame(payload: Dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _chunk(delta: Optional[Dict[str, Any]], finish: Optional[str] = None,
               usage: Optional[Dict[str, int]] = None) -> str:
        payload: Dict[str, Any] = {
            "id": completion_id, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [] if delta is None else [
                {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
        }
        if include_usage:
            payload["usage"] = usage
        return _frame(payload)

    usage: Dict[str, Any] = {}
    failed: Optional[str] = None
    with span():
        yield _chunk({"role": "assistant", "content": ""})
        try:
            async for event in source():
                kind = event.get("type")
                if kind == "answer" and event.get("content"):
                    yield _chunk({"content": event["content"]})
                elif kind == "usage":
                    usage = event
                elif kind == "error":
                    failed = str(event.get("message") or "The turn failed.")
                    break
                elif kind == "done":
                    break
        except Exception as exc:  # noqa: BLE001
            logger.error("openai-compat: stream failed for %s: %s", model, exc, exc_info=True)
            failed = "An error occurred. Please try again."
        if failed is not None:
            status, code = _turn_error(failed)
            yield _frame(_error_body(status, failed, code=code))
        else:
            yield _chunk({}, finish="stop")
            if include_usage:
                yield _chunk(None, usage=_usage(usage))
        yield "data: [DONE]\n\n"


async def _collect(source: Callable[[], AsyncGenerator[Dict[str, Any], None]], *, span: Callable[[], Any],
                   completion_id: str, model: str, headers: Dict[str, str]) -> JSONResponse:
    """A whole turn as one `chat.completion` object (or an error)."""
    parts: List[str] = []
    usage: Dict[str, Any] = {}
    with span():
        try:
            async for event in source():
                kind = event.get("type")
                if kind == "answer" and event.get("content"):
                    parts.append(event["content"])
                elif kind == "usage":
                    usage = event
                elif kind == "error":
                    message = str(event.get("message") or "The turn failed.")
                    status, code = _turn_error(message)
                    return _error_response(status, message, code=code)
                elif kind == "done":
                    break
        except Exception as exc:  # noqa: BLE001
            logger.error("openai-compat: turn failed for %s: %s", model, exc, exc_info=True)
            return _error_response(500, "An error occurred. Please try again.")
    return JSONResponse(headers=headers, content={
        "id": completion_id, "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "logprobs": None,
                     "message": {"role": "assistant", "content": "".join(parts), "refusal": None}}],
        "usage": _usage(usage),
    })


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_id: Optional[str] = Header(None),
):
    """One turn of the agent named by `model`, in OpenAI's chat-completions
    request and response shapes."""
    if not agents_v1.generic_agents_enabled():
        return _error_response(404, "Not found")
    model: Optional[str] = None
    try:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            raise _Refused(400, "The request body is not valid JSON.") from None
        try:
            body = ChatCompletionRequest.model_validate(payload)
        except ValidationError as exc:
            first = (exc.errors() or [{}])[0]
            field = ".".join(str(p) for p in first.get("loc") or ()) or None
            raise _Refused(400, f"Invalid request: {field or 'body'}: {first.get('msg', 'invalid')}",
                           param=field) from None
        model = body.model.strip()
        message, history = split_messages(body.messages)
        first_user = next((h["content"] for h in history if h["role"] == "user"), message)
        conversation = (x_session_id or "").strip()[:512] or None
        end_user = (body.user or "").strip() or None
        picked: Dict[str, str] = {}

        def _session_for(caller: Dict[str, Any]) -> str:
            picked["id"] = derive_session_id(
                caller_id=str(caller.get("user_id") or ""), slug=model,
                conversation=conversation, end_user=end_user, first_user_message=first_user)
            return picked["id"]

        source = await agents_v1.open_turn(model, request, authorization, message=message,
                                           history=history, session_for=_session_for)
        session_id = picked["id"]
    except _Refused as refused:
        return _error_response(refused.status, refused.message, code=refused.code,
                               param=refused.param)
    except HTTPException as exc:
        return _from_http_exception(exc, model=model)

    completion_id = "chatcmpl-" + uuid.uuid4().hex
    headers = {SESSION_HEADER: session_id}

    def span():
        return _traced(request, model, session_id)

    if body.stream:
        include_usage = bool((body.stream_options or {}).get("include_usage"))
        return StreamingResponse(
            _stream(source, span=span, completion_id=completion_id, model=model,
                    include_usage=include_usage),
            media_type="text/event-stream", headers={**SSE_HEADERS, **headers})
    return await _collect(source, span=span, completion_id=completion_id, model=model,
                          headers=headers)


def _created(value: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def _model_entry(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": row["slug"], "object": "model", "created": _created(row.get("created_at")),
            "owned_by": OWNED_BY, "name": row.get("display_name") or row["slug"]}


async def _served_models(authorization: Optional[str]) -> List[Dict[str, Any]]:
    """The model list for an authenticated caller. Every chat key may talk to
    every published agent (keys are not scoped per agent), so the list is the
    install's published agents."""
    if not agents_v1.generic_agents_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    await agents_v1._require_caller(authorization)
    from runtime.agents import custom

    try:
        rows = await asyncio.to_thread(custom.list_served_agents)
    except Exception as exc:  # noqa: BLE001
        logger.error("openai-compat: agent list failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not list the agents. Try again.") from None
    return [_model_entry(row) for row in rows]


@router.get("/models")
async def list_models(authorization: Optional[str] = Header(None)):
    """The published agents, as OpenAI's model list: `id` is the agent's slug."""
    try:
        return {"object": "list", "data": await _served_models(authorization)}
    except HTTPException as exc:
        return _from_http_exception(exc)


@router.get("/models/{model_id}")
async def get_model(model_id: str, authorization: Optional[str] = Header(None)):
    try:
        for entry in await _served_models(authorization):
            if entry["id"] == model_id:
                return entry
    except HTTPException as exc:
        return _from_http_exception(exc)
    return _from_http_exception(HTTPException(status_code=404), model=model_id)
