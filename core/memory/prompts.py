# ai_agent/core/memory/prompts.py
"""Default prompts for the memory layer, and the slots that override them.

Each agent can pin its own `memory_extraction` / `memory_summary` prompt in
`agent_profiles.prompt_version_ids`, like its `system` prompt: versioned,
validated on save, frozen on promote (core/memory/service.py resolves them).
An agent with none uses these defaults, written for an assistant's own users
(business owners and staff). A customer-facing agent pins customer-facing
ones, because a customer's "I prefer Ahmad" is about a staff member, not the
customer's name.

An agent's prompt must keep the same placeholders as the default; one that
doesn't is ignored with a warning (see `*_FIELDS`).
"""

EXTRACTION_SLOT = "memory_extraction"
SUMMARY_SLOT = "memory_summary"
EXTRACTION_FIELDS = ("user_message", "assistant_response")
SUMMARY_FIELDS = ("prior_summary", "older_messages")

EXTRACTION_PROMPT = """You extract durable, reusable facts about a single business user from one chat turn.

Return STRICT JSON: {{"facts": [{{"content": "<short third-person fact>", "importance": <0.0-1.0>}}]}}.

Rules:
- Only facts useful in FUTURE conversations: stable preferences, recurring interests, the user's role, their business context, how they like answers formatted.
- NOT one-off question details, greetings, or anything tied to this single turn.
- Each fact is one short sentence, third person ("The user prefers ...").
- importance: 0.8-1.0 enduring preference/identity; 0.5-0.7 useful context; below 0.5 ephemeral.
- If nothing durable, return {{"facts": []}}.

USER MESSAGE:
{user_message}

ASSISTANT REPLY:
{assistant_response}
"""

SUMMARY_PROMPT = """You maintain a running summary of a business-analytics chat so older turns can be dropped from context without losing continuity.

Fold the prior summary and the older messages below into ONE concise summary (max ~150 words). Keep: the user's goals, decisions made, data already surfaced, and open threads. Drop pleasantries.

PRIOR SUMMARY (may be empty):
{prior_summary}

OLDER MESSAGES:
{older_messages}

Return only the updated summary text, no preamble.
"""
