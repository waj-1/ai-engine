# ai_agent/core/memory/service.py
"""Orchestration for the memory layer.

build_context_block — READ path. Embeds the incoming message, retrieves top-K
+ pinned memories and the session's rolling summary, and returns a prompt block
plus the retrieved memory ids (for provenance stamping). Bumps last_used_at.

process_turn — WRITE path (fire-and-forget). Extracts durable facts, dedups via
the nearest existing memory, inserts/merges, and refreshes the rolling summary.

Both degrade to a no-op on any failure — memory must never break a chat turn.
"""
from __future__ import annotations

import asyncio
import logging
import string
from typing import Any, Dict, List, Optional, Tuple

from core.agent_profile import Profile
from core.memory import store
from core.memory.config import resolve_memory_config
from core.memory.embeddings import embed_text
from core.memory.extract import extract_facts
from core.memory.prompts import (
    EXTRACTION_FIELDS,
    EXTRACTION_PROMPT,
    EXTRACTION_SLOT,
    SUMMARY_FIELDS,
    SUMMARY_PROMPT,
    SUMMARY_SLOT,
)
from core.memory.summarize import maybe_update_summary, needs_refresh
from core.prompt_resolver import PromptResolver
from runtime.llm_factory import build_chat_model

logger = logging.getLogger(__name__)


def _agent_memory_prompt(profile: Profile, slot: str, default: str, fields) -> str:
    """The agent's own memory prompt for `slot`, else `default`.

    Same resolution as the agent's system prompt (runtime/agent.py prepare):
    the promote snapshot's copy in prod, the pinned version in dev/staging.
    An agent with no prompt pinned for the slot gets `default`, so agents
    that never set one behave as before. A prompt that lost a placeholder
    (`fields`), or that can't be formatted (`{user_message!z}`,
    `{user_message:bogus}`), would make every extraction fail silently, so it
    is ignored with a warning. Blocking (DB read): call it off the event loop.
    """
    snapshot = (profile.resolved_prompts or {}).get(slot)
    version_id = None if snapshot is not None else (profile.prompt_version_ids or {}).get(slot)
    if snapshot is None and not version_id:
        return default
    try:
        text = PromptResolver.resolve(
            version_id,
            fallback=snapshot if snapshot is not None else default,
            agent_kind=profile.agent_kind,
            prompt_key=slot,
        )
        found = {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}
        if found != set(fields):
            raise ValueError(f"placeholders {sorted(found)}, expected {sorted(fields)}")
        # The names can be right while the template still can't be formatted
        # (a bad conversion or format spec). Try it here, where a failure
        # falls back to the default, not later in extract_facts.
        text.format(**{name: "" for name in fields})
        return text
    except Exception as exc:
        logger.warning(
            "memory.service: %s prompt for %s is unusable (%s); using the default",
            slot, profile.agent_kind, exc,
        )
        return default


def _slot_model(profile: Profile, slot: str) -> Optional[Dict[str, Any]]:
    # The per-profile light model is retired: the loader always leaves it
    # unset, so the "light" slot falls back to the main model. This keeps a
    # buildable model for extraction/summarization — build_chat_model(None)
    # returns None, which the async model call would then crash on.
    if slot == "light":
        return profile.model_light or profile.model_main
    return profile.model_main


def _build_block(summary: str, memories: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    if summary:
        parts.append("## Conversation summary so far\n" + summary)
    if memories:
        lines = "\n".join(f"- {m['content']}" for m in memories if m.get("content"))
        if lines:
            parts.append(
                "## What you remember about this user\n"
                "(durable facts from past conversations; use them when relevant, "
                "don't recite them verbatim)\n" + lines
            )
    return "\n\n".join(parts)


async def build_context_block(
    *,
    profile: Profile,
    user_id: Optional[str],
    business_id: Optional[str],
    agent_kind: str,
    session_id: Optional[str],
    current_message: str,
    client,
    agent_slug: Optional[str] = None,
) -> Tuple[str, List[str], bool]:
    """READ path. Returns (prompt_block, retrieved_memory_ids, summary_used). Never raises.

    `agent_slug` names the custom agent whose memories are read (every custom
    agent shares one kind; store.scope_query). Product kinds pass none."""
    cfg = resolve_memory_config(profile.memory_config)
    if not cfg.get("enabled") or not user_id:
        return "", [], False
    try:
        retr = cfg["retrieval"]
        # Dedup pinned + semantic by id; pinned always included.
        pinned = await store.list_pinned(
            client, user_id=user_id, agent_kind=agent_kind, agent_slug=agent_slug)
        semantic: List[Dict[str, Any]] = []
        embedding = await asyncio.to_thread(embed_text, current_message)
        if embedding is not None:
            semantic = await store.match_memories(
                client, user_id=user_id, agent_kind=agent_kind, agent_slug=agent_slug,
                query_embedding=embedding,
                match_count=int(retr["top_k"]),
                min_similarity=float(retr["min_similarity"]),
            )
        by_id: Dict[str, Dict[str, Any]] = {}
        if retr.get("always_include_pinned", True):
            for m in pinned:
                by_id[m["id"]] = m
        for m in semantic:
            by_id.setdefault(m["id"], m)
        memories = list(by_id.values())

        summary_row = {}
        if session_id:
            summary_row = await store.get_session_summary(client, session_id=session_id)
        summary = (summary_row or {}).get("running_summary") or ""
        summary_used = bool(summary)

        ids = [m["id"] for m in memories]
        if ids:
            await store.bump_usage(client, memory_ids=ids)
        return _build_block(summary, memories), ids, summary_used
    except Exception as exc:
        logger.warning("memory.service: build_context_block failed (%s)", exc)
        return "", [], False


def _unsaved_part_of_turn(
    tail: List[Dict[str, Any]], this_turn: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Which of this turn's two rows are not persisted yet, given the newest
    persisted rows (`tail`, oldest first). Matched on the user row: the
    assistant row a caller passes may differ slightly from what run_turn saved.
    """
    user = this_turn[0]
    if len(tail) >= 2 and tail[-2] == user and tail[-1].get("role") == "assistant":
        return []
    if tail and tail[-1] == user:
        return this_turn[1:]
    return list(this_turn)


async def process_turn(
    *,
    profile: Profile,
    user_id: Optional[str],
    business_id: Optional[str],
    agent_kind: str,
    session_id: Optional[str],
    user_message: str,
    assistant_response: str,
    previous_messages: List[Dict[str, Any]],
    client=None,
    agent_slug: Optional[str] = None,
) -> None:
    """WRITE path (fire-and-forget). Extract+dedup+store facts; refresh summary.

    A custom agent's facts are saved under its `agent_slug` and need no
    business (a generic agent has none); a product kind's need its business,
    as before."""
    cfg = resolve_memory_config(profile.memory_config)
    if not cfg.get("enabled") or not user_id:
        return
    if agent_kind == store.SLUG_SCOPED_KIND:
        if not agent_slug:
            return
    elif not business_id:
        return
    client = client or store.default_client()
    light = build_chat_model(_slot_model(profile, cfg["extraction"].get("model_slot", "light")))
    if light is None:
        logger.debug("memory.service: no light model; skipping write path")
        return

    # --- Extraction + dedup + store ---
    try:
        extraction_prompt = await asyncio.to_thread(
            _agent_memory_prompt, profile, EXTRACTION_SLOT, EXTRACTION_PROMPT, EXTRACTION_FIELDS,
        )
        facts = await extract_facts(
            light, user_message, assistant_response, cfg["extraction"], prompt=extraction_prompt,
        )
        dedup_threshold = float(cfg["dedup"]["similarity_threshold"])
        for fact in facts:
            embedding = await asyncio.to_thread(embed_text, fact["content"])
            if embedding is None:
                continue
            nearest = await store.nearest_one(
                client, user_id=user_id, agent_kind=agent_kind, agent_slug=agent_slug,
                query_embedding=embedding,
            )
            if nearest and float(nearest.get("similarity", 0.0)) >= dedup_threshold:
                # Merge: refresh the existing memory's content + embedding.
                await store.update_memory_content(
                    client, memory_id=nearest["id"], content=fact["content"], embedding=embedding,
                )
            else:
                await store.insert_memory(
                    client, user_id=user_id, business_id=business_id, agent_kind=agent_kind,
                    content=fact["content"], embedding=embedding,
                    importance=float(fact["importance"]), source="auto",
                    origin_session_id=session_id, agent_slug=agent_slug,
                )
    except Exception as exc:
        logger.warning("memory.service: extraction path failed (%s)", exc)

    # --- Rolling summary ---
    try:
        if session_id:
            summary_row = await store.get_session_summary(client, session_id=session_id)
            prior_summary = (summary_row or {}).get("running_summary") or ""
            prior_count = int((summary_row or {}).get("summary_message_count") or 0)
            # Count the session's WHOLE transcript, not `previous_messages`:
            # agents pass only their capped history window, and counting that
            # froze the summary after its first refresh.
            total = await store.count_messages(client, session_id=session_id)
            this_turn = [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_response},
            ]
            unsaved: List[Dict[str, Any]] = []
            if not total:
                # Nothing persisted for this session: fall back to what the
                # agent passed.
                unsaved = list(previous_messages) + this_turn
            elif needs_refresh(total + len(this_turn), prior_count, cfg["summary"]):
                # Agents start this save while still streaming the reply, and
                # run_turn persists the turn only afterwards, so its rows may
                # not be in the count yet. Count them in from here.
                tail = await store.list_session_history(
                    client, session_id=session_id, start=max(total - 2, 0), end=total,
                )
                unsaved = _unsaved_part_of_turn(tail, this_turn)
            if needs_refresh(total + len(unsaved), prior_count, cfg["summary"]):
                # Only the part the summary doesn't cover yet (from prior_count).
                start = min(prior_count, total)
                history = await store.list_session_history(
                    client, session_id=session_id, start=start, end=total,
                )
                summary_prompt = await asyncio.to_thread(
                    _agent_memory_prompt, profile, SUMMARY_SLOT, SUMMARY_PROMPT, SUMMARY_FIELDS,
                )
                result = await maybe_update_summary(
                    light, prior_summary=prior_summary, prior_count=prior_count,
                    messages=history + unsaved, cfg=cfg["summary"], offset=start,
                    prompt=summary_prompt,
                )
                if result is not None:
                    new_summary, covered = result
                    written = await store.set_session_summary(
                        client, session_id=session_id, summary=new_summary,
                        message_count=covered, expected_count=prior_count,
                    )
                    if not written:
                        logger.info(
                            "memory.service: another save refreshed the summary for "
                            "session %s first; kept its version", session_id,
                        )
    except Exception as exc:
        logger.warning("memory.service: summary path failed (%s)", exc)
