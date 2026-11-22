"""
Server-owned chat session helpers.

Two pieces:

- `ChatSession` — a thin per-request wrapper around the Supabase tables
  `ai_chat_sessions` and `ai_chat_messages`. Knows nothing about LangChain,
  prompts, or models. Owns: resolve-or-create the session row, load history
  rows in agent-friendly `{role, content}` shape, persist user/assistant
  turns, update the session title.

- `run_turn` — the engine-owned turn lifecycle and the SOLE orchestrator for
  every chat agent (and voice). Resolves/creates the session via
  `ChatSession.for_identity`, loads windowed history, starts the user-turn
  write, runs the agent forwarding its events (capturing `title`/
  `suggestions`/`usage`/`turn_meta` along the way), persists the assistant
  turn + telemetry, then emits the terminal `done`. Agents no longer persist
  chat, emit `done`, or write their own `agent_traces` row themselves —
  run_turn (via `record_turn_telemetry`) owns all of it. Which of those
  writes overlap which work, and why, is spelled out on `run_turn` itself.

The chat tables (`ai_chat_sessions` / `ai_chat_messages`) live on the
standalone AI Supabase project, which is reachable only via the service role.
The Supabase client is therefore the AI service client; access is scoped
explicitly by `user_id` / `session_id` / `agent_kind` filters rather than by
JWT-driven RLS.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional
from uuid import UUID, uuid4

from core.auth import get_service_client
from core.chat_persistence import coerce_session_id
from core.config import AGENT_CONFIG
from core.memory.config import resolve_window_config
from core.tool_provenance import record_caller_text
from core.trace_recorder import current_lmnr_trace_id
from common.turn_telemetry import record_turn_telemetry

logger = logging.getLogger(__name__)

# Strong references to in-flight fire-and-join writes. `asyncio` keeps only a
# weak reference to a running task, so without this a mid-turn GC can cancel
# the user-row insert `run_turn` starts alongside the agent stream. Entries
# remove themselves via a done-callback.
_BACKGROUND_WRITES: set["asyncio.Task[None]"] = set()

DEFAULT_TITLE = "New Chat"

# Bounds the history we hand the model. A sanity cap so a runaway session
# doesn't keep growing the context — agents may apply tighter caps internally.
HISTORY_ROW_LIMIT = 40


def _writer_environment() -> str:
    """``prod`` / ``staging`` for ``metadata.environment`` on new sessions
    (read by core/conversation_insights.py — ai_chat_sessions has no
    environment column)."""
    from core.agent_profile import _default_environment

    return _default_environment()


@dataclass
class TurnIdentity:
    """Who/what a turn belongs to. Two shapes share one persistence path:
    authenticated (user_id set) and anonymous (client-supplied session id)."""

    agent_kind: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    business_id: Optional[str] = None
    session_type: str = "chat"
    source: str = "waj"
    # Extra keys merged into a NEW session row's metadata (e.g. the record
    # a conversation is about) — audit linkage.
    session_metadata: Optional[dict] = None

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None


class ChatSession:
    """Per-request handle to a single conversation's persistence layer.

    Construct one per HTTP request. Methods are synchronous because supabase-py
    is synchronous — but they block for a full network round-trip, so an async
    caller must hop them onto a worker thread (`asyncio.to_thread`, as
    `run_turn` does). Called directly on the event loop they stall every other
    coroutine in the process — in the duplex-voice WS process that means the
    audio pumps and the holding-line timer, not just the current turn.
    """

    def __init__(
        self,
        *,
        supabase: Any,
        user_id: str,
        agent_kind: str,
        business_id: Optional[Any],
        session_id: Optional[UUID],
        default_title: str = DEFAULT_TITLE,
    ) -> None:
        self._supabase = supabase
        self._user_id = user_id
        self._agent_kind = agent_kind
        self._business_id = business_id
        self._default_title = default_title
        # Resolved by `resolve_or_create`. Held as state so subsequent
        # append/update calls don't need it threaded through every signature.
        self._session_id: Optional[UUID] = session_id
        self._is_new: Optional[bool] = None
        # Set only via `for_identity` — the anonymous/authenticated dispatch
        # in `resolve_or_create` and the window cap in `load_history` are
        # both no-ops (fall back to their pre-`for_identity` behavior) when
        # these are None. Now that `run_turn`/`for_identity` is the only
        # construction path in production, no live caller leaves them unset —
        # kept as a fallback for direct `ChatSession(...)` construction (e.g.
        # a test building one without an identity).
        self._identity: Optional["TurnIdentity"] = None
        self._window: Optional[dict] = None

    @classmethod
    def for_identity(cls, supabase: Any, identity: "TurnIdentity", *, window: dict) -> "ChatSession":
        cs = cls(
            supabase=supabase,
            user_id=identity.user_id or "",
            agent_kind=identity.agent_kind,
            business_id=identity.business_id,
            session_id=(coerce_session_id(identity.session_id) if identity.session_id else None),
        )
        cs._identity = identity
        cs._window = window
        return cs

    @property
    def session_id(self) -> UUID:
        if self._session_id is None:
            raise RuntimeError("session_id accessed before resolve_or_create")
        return self._session_id

    @property
    def is_new(self) -> bool:
        if self._is_new is None:
            raise RuntimeError("is_new accessed before resolve_or_create")
        return self._is_new

    def resolve_or_create(self) -> tuple[UUID, bool, str]:
        """Return `(session_id, is_new, title)`.

        - When the constructor was given no `session_id`, inserts a new row
          and returns `is_new=True` plus the default title.
        - Otherwise, verifies the row exists, belongs to this `agent_kind`,
          and is not soft-deleted. Raises `LookupError` if not found — RLS
          hides rows owned by other users, which surfaces here as the same
          missing-row error.

        Identity-based callers (`for_identity`, used by `run_turn`) take a
        unified select-then-insert path on the (coerced) client session id —
        for BOTH anonymous and authenticated identities. Every chat client
        sends a client-generated
        `session_id` on the first turn, so the row won't exist yet: a
        verify-only path would fail turn 1 with "Session not found". Instead we
        create-if-missing (populating `user_id`/`business_id` when present) and
        recognize a returning id as `is_new=False` with its existing title.
        """
        if self._identity is not None:
            sid = self._session_id or str(uuid4())
            self._session_id = sid
            existing = (
                self._supabase.table("ai_chat_sessions")
                .select("id, title, user_id, agent_kind")
                .eq("id", sid)
                .is_("deleted_at", None)
                .maybe_single()
                .execute()
            )
            if existing and existing.data:
                row = existing.data
                # Ownership + kind enforcement. The AI project has no per-user
                # RLS, so this application-level check IS the access control for
                # resuming a session (it mirrors the explicit user_id filter in
                # core/chat_persistence.list_session_messages). A session id is
                # an unguessable UUID, but that alone must NOT be the only gate:
                # an authenticated caller may resume a session only when it is
                # theirs AND belongs to this agent_kind. Without this, a caller
                # who learns another user's session id (or reuses a business id
                # against another agent's endpoint) would have that transcript
                # loaded into the model via `load_history`. Anonymous callers
                # (identity.user_id is None — a public chat) keep the
                # session-id-as-secret model, and a session with no stored owner
                # (owner is None) stays resumable so an anon→authenticated
                # upgrade on the same browser session id isn't locked out.
                owner = row.get("user_id")
                if self._identity.user_id and owner and owner != self._identity.user_id:
                    raise LookupError(f"session {sid} not owned by caller")
                existing_kind = row.get("agent_kind")
                if existing_kind and existing_kind != self._agent_kind:
                    raise LookupError(
                        f"session {sid} belongs to agent_kind {existing_kind!r}, "
                        f"not {self._agent_kind!r}"
                    )
                self._is_new = False
                return (sid, False, row.get("title") or self._default_title)
            # session_metadata provides EXTRA keys (e.g. the record a
            # conversation is about) but must never clobber the identity's own source/
            # session_type, so seed from it first and let the core two win.
            metadata = dict(self._identity.session_metadata or {})
            metadata["source"] = self._identity.source
            metadata["session_type"] = self._identity.session_type
            metadata.setdefault("environment", _writer_environment())
            row: dict[str, Any] = {
                "id": sid,
                "business_id": (str(self._business_id) if self._business_id else None),
                "title": self._default_title,
                "agent_kind": self._agent_kind,
                "metadata": metadata,
            }
            if self._identity.user_id:
                row["user_id"] = self._identity.user_id
            self._supabase.table("ai_chat_sessions").insert(row).execute()
            self._is_new = True
            return (sid, True, self._default_title)

        if self._session_id is None:
            inserted = (
                self._supabase.table("ai_chat_sessions")
                .insert(
                    {
                        "user_id": self._user_id,
                        "business_id": str(self._business_id),
                        "title": self._default_title,
                        "agent_kind": self._agent_kind,
                        "metadata": {"environment": _writer_environment()},
                    }
                )
                .execute()
            )
            rows = inserted.data or []
            if not rows:
                raise RuntimeError("session insert returned no rows")
            session_row = rows[0]
            self._session_id = UUID(session_row["id"])
            self._is_new = True
            return (
                self._session_id,
                True,
                session_row.get("title") or self._default_title,
            )

        existing = (
            self._supabase.table("ai_chat_sessions")
            .select("id, title")
            .eq("id", str(self._session_id))
            .eq("agent_kind", self._agent_kind)
            .is_("deleted_at", None)
            .maybe_single()
            .execute()
        )
        # supabase-py returns None data when maybe_single matches nothing.
        if not existing or not existing.data:
            raise LookupError(f"session {self._session_id} not found")

        self._is_new = False
        return (
            self._session_id,
            False,
            existing.data.get("title") or self._default_title,
        )

    def load_history(self) -> list[dict[str, Any]]:
        """Return prior turns as `[{role, content}]` rows ordered oldest-first.

        Caps at `self._window["max_messages"]` when constructed via
        `for_identity`, else falls back to `HISTORY_ROW_LIMIT`. The cap is
        pushed into the query (ORDER BY created_at DESC LIMIT cap — the
        newest rows) so a long-running session doesn't ship its whole
        transcript over the wire on every turn; rows come back newest-first
        and are reversed to the oldest-first shape the agents accept under
        `context["previous_messages"]`.
        """
        cap = int((self._window or {}).get("max_messages", HISTORY_ROW_LIMIT))
        resp = (
            self._supabase.table("ai_chat_messages")
            .select("role, content, created_at, photo_transcript:metadata->photo_transcript")
            .eq("session_id", str(self.session_id))
            .order("created_at", desc=True)
            .limit(cap)
            .execute()
        )
        rows = (resp.data or [])[:cap]
        rows.reverse()
        out: list[dict[str, Any]] = []
        for r in rows:
            row = {"role": r.get("role"), "content": r.get("content") or ""}
            # Text a vision model read from photos on this user turn (the
            # bytes are never stored), for an agent that reads it.
            if isinstance(r.get("photo_transcript"), dict):
                row["photo_transcript"] = r["photo_transcript"]
            out.append(row)
        return out

    def append_user(self, content: str) -> Optional[str]:
        """Insert the user row; returns its id (None when the insert returned
        no row) so run_turn can stamp metadata the agent produced later."""
        resp = self._supabase.table("ai_chat_messages").insert(
            {
                "session_id": str(self.session_id),
                "role": "user",
                "content": content,
                "metadata": {},
            }
        ).execute()
        data = getattr(resp, "data", None)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("id")
        return None

    def update_message_metadata(self, message_id: str, metadata: dict) -> None:
        """Merge ``metadata`` into one message row's ``metadata``."""
        existing = (
            self._supabase.table("ai_chat_messages").select("metadata")
            .eq("id", message_id).eq("session_id", str(self.session_id)).limit(1).execute().data
            or []
        )
        merged = {**((existing[0].get("metadata") if existing else None) or {}), **metadata}
        self._supabase.table("ai_chat_messages").update({"metadata": merged}).eq(
            "id", message_id).eq("session_id", str(self.session_id)).execute()

    def append_assistant(self, content: str, metadata: Optional[dict] = None) -> None:
        self._supabase.table("ai_chat_messages").insert(
            {
                "session_id": str(self.session_id),
                "role": "assistant",
                "content": content,
                "metadata": metadata or {},
            }
        ).execute()

    def update_title(self, title: str) -> None:
        if not title:
            return
        self._supabase.table("ai_chat_sessions").update({"title": title}).eq(
            "id", str(self.session_id)
        ).execute()

    def update_session_suggestions(self, suggestions: list) -> None:
        existing = (
            self._supabase.table("ai_chat_sessions").select("metadata")
            .eq("id", str(self.session_id)).limit(1).execute().data or []
        )
        meta = (existing[0].get("metadata") if existing else {}) or {}
        meta["suggested_questions"] = suggestions
        self._supabase.table("ai_chat_sessions").update({"metadata": meta}).eq(
            "id", str(self.session_id)
        ).execute()


async def run_turn(
    *,
    agent: Any,
    message: str,
    identity: "TurnIdentity",
    profile: Any,
    extra_context: Optional[dict[str, Any]] = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Engine-owned turn lifecycle. The ONLY code that persists a chat turn.

    Resolve/create session -> load windowed history -> START the user-turn
    write -> run the agent forwarding its events (accumulating answer text,
    capturing title/suggestions/turn_meta) -> persist the assistant turn +
    telemetry -> emit `done`. **All** persistence still lands before `done`, so
    the SSE break-on-`done` can never skip it. Agents no longer persist or emit
    `done` themselves.

    What is NOT serialized (and why — a delegated voice turn has an ~8s budget
    before the realtime service discards its tool call, so a round-trip the
    answer doesn't depend on is a real cost):

    * the user row is written CONCURRENTLY with the agent stream and joined
      before any assistant-side write, so transcript order is unchanged;
    * the transcript writes and the `agent_traces` telemetry insert run
      concurrently with each other — different tables, no write-order
      relationship, both after the answer text is final.
    """
    window = resolve_window_config(getattr(profile, "memory_config", {}) or {})
    if (extra_context or {}).get("voice_turn"):
        # A delegated voice turn runs against the realtime service's ~8s
        # tool-drop ceiling, and every history row is prompt tokens on ~2
        # sequential model calls per answer — clamp the window to the voice
        # cap (core/config.py: voice_history_msg_count, VOICE_HISTORY_MSG_COUNT).
        # min(), not override: a profile window already smaller stays smaller.
        # Text chat (no voice_turn) keeps the profile's window untouched.
        window = {
            **window,
            "max_messages": min(
                int(window.get("max_messages", HISTORY_ROW_LIMIT)),
                AGENT_CONFIG.voice_history_msg_count,
            ),
        }
    supabase = get_service_client()
    session = ChatSession.for_identity(supabase, identity, window=window)

    # Stage timing for a delegated voice turn: voice/live/delegate.py threads
    # a `stage_timings` dict through extra_context and stamps it onto the
    # `voice.agent_turn` root span at turn end (waj.voice.live.stage.*).
    # `history_ms` covers the session resolve + history load below — the
    # pre-agent Supabase bookkeeping that used to be invisible inside
    # `delegate_ms`. Text chat passes no dict and pays one lookup here.
    _stage_timings = (extra_context or {}).get("stage_timings")
    if not isinstance(_stage_timings, dict):
        _stage_timings = None
    _history_started = time.monotonic()

    # ChatSession methods are sync supabase-py round-trips — every call goes
    # through to_thread so the event loop (and, in the voice WS process, the
    # audio pumps) keeps running while the DB answers.
    try:
        _, is_new, title = await asyncio.to_thread(session.resolve_or_create)
    except LookupError:
        yield {"type": "error", "message": "Session not found"}
        return
    except Exception:
        logger.exception("run_turn: failed to resolve session")
        yield {"type": "error", "message": "Failed to load session"}
        return

    if is_new:
        yield {"type": "session", "id": str(session.session_id), "title": title}
        previous_messages: list[dict[str, Any]] = []
    else:
        try:
            previous_messages = await asyncio.to_thread(session.load_history)
        except Exception:
            logger.exception("run_turn: failed to load history")
            previous_messages = []

    if _stage_timings is not None:
        try:
            _stage_timings["history_ms"] = round(
                (time.monotonic() - _history_started) * 1000.0, 1
            )
        except Exception:  # a timing failure must never break the turn
            logger.debug("run_turn: stage timing write failed", exc_info=True)

    # Provenance for anything a VOICE turn is about to read out loud. A no-op
    # on the text path (nobody installed a recorder), a few string scans on a
    # delegated one. USER rows only, oldest first, then the current message:
    #
    #  - oldest-first because a merchant dictating an IBAN pauses between
    #    groups and each pause arrives as its own message, so the digits he
    #    is owed on the read-back only exist as a contiguous run once his
    #    utterances are joined IN ORDER (see core/tool_provenance.py);
    #  - user rows ONLY because admitting the assistant's own past messages
    #    would launder a fabrication — say a wrong account number once and
    #    every later turn could cite the transcript as its source.
    for _row in previous_messages:
        if _row.get("role") == "user":
            record_caller_text(_row.get("content"))
    record_caller_text(message)

    context: dict[str, Any] = {
        "previous_messages": previous_messages,
        "is_first_message": is_new,
        "session_id": str(session.session_id),
    }
    if extra_context:
        context.update(extra_context)

    user_row: dict[str, Any] = {}

    async def _persist_user() -> None:
        try:
            user_row["id"] = await asyncio.to_thread(session.append_user, message)
        except Exception:
            logger.exception(
                "run_turn: failed to persist user message for session=%s",
                session.session_id,
            )

    # The user row is a write the ANSWER does not depend on. Awaiting it here
    # put a whole Supabase round-trip in front of the first model token — dead
    # time a duplex-voice turn cannot spare, because it runs against the
    # realtime service's ~8s tool-drop ceiling (measured in prod on one voice
    # brain: ~0.78s of pre-agent bookkeeping on a ~6.7s p50 delegate).
    # Started here, JOINED before the assistant row is written, so the
    # transcript's user-then-assistant order is unchanged — `load_history`
    # already ran above, so nothing in this turn can observe the row early.
    user_write = asyncio.create_task(_persist_user())
    _BACKGROUND_WRITES.add(user_write)
    user_write.add_done_callback(_BACKGROUND_WRITES.discard)

    start = time.monotonic()
    accumulated: list[str] = []
    pending_title: Optional[str] = None
    suggestions: Optional[list] = None
    usage: dict[str, Any] = {}
    turn_meta: dict[str, Any] = {}

    async def _emit_telemetry(status: str) -> None:
        """Write the per-turn agent_traces row. Called on BOTH the success
        (`ok`) and failure (`error`) exits so the dashboards can compute an
        error rate and see failed-turn latency — a turn that erred used to
        leave no telemetry row at all, making failures invisible."""
        usage["cache_read_tokens"] = turn_meta.get("cache_read_tokens", 0)
        usage["cache_creation_tokens"] = turn_meta.get("cache_creation_tokens", 0)
        await record_turn_telemetry(
            session_id=str(session.session_id),
            agent_kind=identity.agent_kind,
            profile_id=getattr(profile, "profile_id", None),
            model=getattr(profile, "model_main", None),
            usage=usage,
            latency_ms=round((time.monotonic() - start) * 1000),
            iteration_count=turn_meta.get("iteration_count"),
            lmnr_trace_id=current_lmnr_trace_id(),
            status=status,
        )

    try:
        async for event in agent.run_stream(message, context):
            etype = event.get("type")
            if etype == "done":
                break  # defensive: legacy agents may still emit it pre-migration
            if etype == "error":
                # Agent signalled failure by yielding an error (rather than
                # raising — some agents catch internally). Treat it as terminal:
                # forward it and do NOT persist a (possibly partial) assistant
                # row (the user row is still joined below — it was started
                # before the stream and must never be left running), but DO
                # record a telemetry row marked `error` so the failed turn is
                # visible to observability.
                yield event
                await user_write
                await _emit_telemetry("error")
                return
            if etype == "answer":
                if event.get("content"):
                    accumulated.append(event["content"])
            elif etype == "title" and is_new:
                pending_title = event.get("content") or None
            elif etype == "suggestions":
                suggestions = event.get("questions")
            elif etype == "usage":
                usage["input_tokens"] = event.get("input_tokens")
                usage["output_tokens"] = event.get("output_tokens")
            elif etype == "turn_meta":
                # Internal orchestrator contract: captured for the telemetry row,
                # NOT forwarded to the client stream. Normalize to a dict at the
                # capture point so every downstream `turn_meta.get(...)` (memory
                # provenance AND telemetry) is uniformly safe.
                _md = event.get("metadata")
                turn_meta = _md if isinstance(_md, dict) else {}
                continue
            yield event
    except Exception:
        logger.exception("run_turn: agent stream failed for session=%s", session.session_id)
        yield {"type": "error", "message": "Model failed mid-stream"}
        await user_write
        await _emit_telemetry("error")
        return

    # Join the user row before ANY assistant-side write: `append_assistant`
    # must land after it so the transcript reads in order.
    await user_write

    final_text = "".join(accumulated)

    async def _persist_transcript() -> None:
        """The chat-row writes, in their existing order.

        Kept sequential INTERNALLY — `update_title` and
        `update_session_suggestions` both UPDATE `ai_chat_sessions`, and
        `update_session_suggestions` is a read-modify-write of that row's
        `metadata`, so nothing in here may overlap anything else in here.
        """
        # Metadata the agent produced for the USER row during the turn (e.g. a
        # photo transcript, so later turns can read it). It
        # rides turn_meta, which is never forwarded to the stream.
        user_meta = turn_meta.get("user_message_metadata")
        if isinstance(user_meta, dict) and user_meta and isinstance(user_row.get("id"), str):
            try:
                await asyncio.to_thread(session.update_message_metadata, user_row["id"], user_meta)
            except Exception:
                logger.exception("run_turn: failed to stamp user-row metadata for session=%s",
                                 session.session_id)
        if not final_text:
            return
        # Lean provenance passthrough: agents that own their own memory
        # subsystem stamp
        # `turn_meta.metadata.memory` with {retrieved_memory_ids, summary_used}
        # so the /admin/memory Inspector can resolve what was recalled for
        # this turn. Agents that don't set it get `assistant_metadata=None`
        # -> `{}`.
        memory_meta = turn_meta.get("memory")  # turn_meta normalized to dict at capture
        assistant_metadata = {"memory": memory_meta} if memory_meta else None
        try:
            await asyncio.to_thread(
                session.append_assistant, final_text, metadata=assistant_metadata
            )
        except Exception:
            logger.exception("run_turn: failed to persist turn for session=%s", session.session_id)
        if pending_title:
            try:
                await asyncio.to_thread(session.update_title, pending_title)
            except Exception:
                logger.exception("run_turn: failed to update title")
        if suggestions is not None:
            try:
                await asyncio.to_thread(session.update_session_suggestions, suggestions)
            except Exception:
                logger.exception("run_turn: failed to mirror suggestions")

    # Transcript rows (`ai_chat_messages`/`ai_chat_sessions`) and the telemetry
    # row (`agent_traces`) are independent tables with no write-order
    # relationship, and BOTH run after the answer text is already final — so
    # serializing them just charged the caller two round-trips of dead time.
    # On a duplex-voice turn that is spend against the realtime service's ~8s
    # tool-drop ceiling: prod measured ~0.58s p50 of post-answer bookkeeping on
    # a ~6.7s p50 delegate. Both still complete BEFORE `done`, so the
    # break-on-`done` contract in the module docstring is unchanged. One
    # deliberate side effect: `latency_ms` on the telemetry row now excludes
    # the transcript write it used to sit behind — it measures the agent turn,
    # which is what the column is read as.
    await asyncio.gather(_persist_transcript(), _emit_telemetry("ok"))

    yield {"type": "done"}
