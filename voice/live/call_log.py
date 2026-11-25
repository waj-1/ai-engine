"""What was said on a call, saved as a conversation.

A delegated turn (`ask_agent` -> `common.chat_session.run_turn`) saves its own
two rows: the question as the realtime model worded it, and the brain's
answer. Everything else said on a call was saved nowhere: the greeting, and
every turn the realtime model answered by itself. A call with no delegated
turn therefore left no conversation at all.

`CallLog` collects the call's transcript as the endpoint forwards it to the
caller's screen and, when the call ends, writes the turns `run_turn` did not
already save into the same `ai_chat_sessions` / `ai_chat_messages` rows, with
the time each was said. The session row is created when no delegated turn
made it.

The rows carry ``metadata.voice.spoken``; `ChatSession.load_history` leaves
them out, so what the brain reads as history is unchanged.

Nothing here may break a call: `save` never raises, returns at once and does
its blocking database work in a worker thread. It does not depend on the
call's own task either: that task is often being cancelled when a call ends
(the server tearing it down, the demo's call-length cap), and a write that
had to be awaited there would be lost.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)

#: One message's text is capped: a transcript is a record, not a dump.
_MAX_CHARS = 8000
#: And one call's messages (a long call is still a few dozen turns).
_MAX_ROWS = 400
#: The threads that write. Two: a write is a few round trips at a call's end.
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-call-log")


def is_spoken(metadata: Any) -> bool:
    """Is this message row a call's spoken transcript (written here)?"""
    voice = metadata.get("voice") if isinstance(metadata, dict) else None
    return isinstance(voice, dict) and voice.get("spoken") is True


@dataclass
class _Turn:
    """One caller utterance and what the agent said until the next one. The
    first turn of a call has no caller text: it is the greeting."""

    at: float
    caller: str = ""
    #: The caller's text is still arriving (or still owed after a commit).
    caller_open: bool = False
    agent: str = ""
    agent_at: Optional[float] = None
    agent_break: bool = False
    #: None: not delegated. True: `run_turn` saved question and answer.
    #: False: the delegated turn failed (`run_turn` saved the question only).
    delegated: Optional[bool] = None


@dataclass
class CallLog:
    identity: Any
    call_id: str
    clock: Callable[[], float] = time.time
    _turns: List[_Turn] = field(default_factory=list)
    _stored: bool = False

    # ------------------------------------------------------------ collecting
    def _last(self) -> Optional[_Turn]:
        return self._turns[-1] if self._turns else None

    def _open_turn(self, *, caller_open: bool) -> _Turn:
        turn = _Turn(at=self.clock(), caller_open=caller_open)
        self._turns.append(turn)
        return turn

    def caller_committed(self) -> None:
        """The provider committed a caller utterance. Its text may arrive
        later, even after the reply has started; it belongs to this turn."""
        self._open_turn(caller_open=True)

    def caller_text(self, text: Optional[str], *, final: bool) -> None:
        turn = self._last()
        if turn is None or not turn.caller_open:
            if not text:
                return
            turn = self._open_turn(caller_open=True)
        turn.caller += text or ""
        if final:
            turn.caller_open = False

    def agent_text(self, text: Optional[str]) -> None:
        if not text:
            return
        turn = self._last() or self._open_turn(caller_open=False)
        if turn.caller_open and turn.caller:
            # A provider with no commit event and no final: the agent
            # speaking is what ends the caller's text.
            turn.caller_open = False
        if turn.agent_at is None:
            turn.agent_at = self.clock()
        if turn.agent_break and turn.agent:
            turn.agent += "\n"
        turn.agent_break = False
        turn.agent += text

    def response_ended(self) -> None:
        """One provider response ended; the agent's next words start a line."""
        turn = self._last()
        if turn is None:
            return
        if turn.agent:
            turn.agent_break = True
        if turn.caller:
            turn.caller_open = False

    def delegated(self, *, ok: bool) -> None:
        """A delegated turn ran for the current caller utterance."""
        turn = self._last()
        if turn is not None and turn.delegated is not True:
            turn.delegated = bool(ok)

    # --------------------------------------------------------------- reading
    def rows(self) -> List[Dict[str, Any]]:
        """The `ai_chat_messages` rows to write, oldest first (without the
        session id). Adjacent agent messages are one row."""
        said: List[Dict[str, Any]] = []
        for turn in self._turns:
            caller, agent = turn.caller.strip(), turn.agent.strip()
            if caller and turn.delegated is None:
                said.append({"role": "user", "content": caller, "at": turn.at})
            if agent and turn.delegated is not True:
                at = turn.agent_at if turn.agent_at is not None else turn.at
                if said and said[-1]["role"] == "assistant":
                    said[-1]["content"] += "\n" + agent
                else:
                    said.append({"role": "assistant", "content": agent, "at": at})
        rows: List[Dict[str, Any]] = []
        previous: Optional[datetime] = None
        for item in said[:_MAX_ROWS]:
            stamp = datetime.fromtimestamp(item["at"], tz=timezone.utc)
            if previous is not None and stamp <= previous:
                # Every reader orders by created_at: never two rows on one tick.
                stamp = previous + timedelta(milliseconds=1)
            previous = stamp
            rows.append({
                "role": item["role"],
                "content": item["content"][:_MAX_CHARS],
                "metadata": {"voice": {"call_id": self.call_id, "spoken": True}},
                "created_at": stamp.isoformat(),
            })
        return rows

    # --------------------------------------------------------------- storing
    def save(self) -> Optional[Future]:
        """Hand the call's rows to a worker thread, once, and return at once.
        Never raises. The returned future (None when there is nothing to
        write) is for tests; nothing needs to wait for it."""
        if self._stored:
            return None
        self._stored = True
        try:
            rows = self.rows()
            if not rows:
                return None
            return _POOL.submit(_write_logged, self.identity, rows)
        except Exception:  # noqa: BLE001 (a transcript must never fail a call)
            logger.warning("voice.live: the call's transcript was not saved", exc_info=True)
            return None


def _write_logged(identity: Any, rows: List[Dict[str, Any]]) -> None:
    try:
        _write(identity, rows)
    except Exception:  # noqa: BLE001
        logger.warning("voice.live: the call's transcript was not saved", exc_info=True)


def _write(identity: Any, rows: List[Dict[str, Any]]) -> None:
    """Blocking: make sure the session row exists, then add the rows."""
    from common.chat_session import ChatSession

    client = get_service_client()
    session = ChatSession.for_identity(client, identity, window={})
    try:
        # Creates the row exactly as a delegated turn would (same metadata),
        # and refuses a session that belongs to another user or agent kind.
        session_id, _is_new, _title = session.resolve_or_create()
    except LookupError:
        logger.info("voice.live: transcript not saved, the session is not this caller's")
        return
    sid = str(session_id)
    client.table("ai_chat_messages").insert(
        [{"session_id": sid, **row} for row in rows]
    ).execute()
    try:
        # Sort the conversation by its last words, as a chat turn does.
        client.table("ai_chat_sessions").update(
            {"updated_at": rows[-1]["created_at"]}).eq("id", sid).execute()
    except Exception:  # noqa: BLE001 (the rows are saved; only the sort key is stale)
        logger.debug("voice.live: could not bump the session's updated_at", exc_info=True)
