-- Memories belong to one agent, not to a kind of agent.
--
-- ai_user_memory was namespaced by agent_kind only. Every generic agent has
-- kind `custom`, so every one of them would read and write the same memories:
-- a support agent could recall what a user told a sales agent. A custom
-- agent's memories now carry its slug (`agent_slug`), and the runtime, the
-- portal's Memory screen and the admin MCP `memory` tool read and write only
-- the rows of that slug.
--
-- Product kinds are unchanged: their rows keep `agent_slug` NULL and are read
-- by kind exactly as before.
--
-- Existing rows of kind `custom` with no slug:
--   1. a row saved from a chat is given the slug of the agent whose session
--      saved it (ai_chat_sessions.metadata.agent_slug);
--   2. any other row (added by hand, before memories were per agent) stays
--      unassigned: no agent recalls it, and the Memory screen lists it under
--      every custom agent as "not assigned to an agent yet", with a button
--      that moves it to the agent being viewed.
--
-- `business_id` becomes optional: a generic agent has no business, and its
-- memories are saved without one. Rows that have one keep it.
--
-- Additive and idempotent.

ALTER TABLE public.ai_user_memory
    ADD COLUMN IF NOT EXISTS agent_slug text;

ALTER TABLE public.ai_user_memory
    ALTER COLUMN business_id DROP NOT NULL;

COMMENT ON COLUMN public.ai_user_memory.agent_slug IS 'The custom agent (agent_profiles.slug, kind custom) this memory belongs to. NULL for product kinds, which are scoped by agent_kind alone. A custom row with NULL here predates per-agent memory and is recalled by no agent until it is assigned.';

CREATE INDEX IF NOT EXISTS idx_ai_user_memory_agent_slug
    ON public.ai_user_memory (user_id, agent_kind, agent_slug, status);

UPDATE public.ai_user_memory m
SET agent_slug = s.metadata->>'agent_slug'
FROM public.ai_chat_sessions s
WHERE m.agent_kind = 'custom'
  AND m.agent_slug IS NULL
  AND m.origin_session_id = s.id
  AND COALESCE(s.metadata->>'agent_slug', '') <> '';

-- The recall function gains the slug. Drop the old five-argument version
-- first: with both present, a call naming five arguments is ambiguous.
DROP FUNCTION IF EXISTS public.match_user_memory(uuid, public.ai_agent_kind, text, integer, real);

CREATE OR REPLACE FUNCTION public.match_user_memory(
  p_user_id uuid,
  p_agent_kind public.ai_agent_kind,
  p_query_embedding text,
  p_match_count integer DEFAULT 6,
  p_min_similarity real DEFAULT 0.7,
  p_agent_slug text DEFAULT NULL
)
RETURNS TABLE (
  id uuid,
  content text,
  importance real,
  pinned boolean,
  source text,
  similarity real,
  last_used_at timestamptz
)
LANGUAGE sql STABLE AS $function$
  SELECT
    m.id,
    m.content,
    m.importance,
    m.pinned,
    m.source,
    (1 - (m.embedding <=> p_query_embedding::vector))::real AS similarity,
    m.last_used_at
  FROM public.ai_user_memory m
  WHERE m.user_id = p_user_id
    AND m.agent_kind = p_agent_kind
    AND (p_agent_kind <> 'custom'::public.ai_agent_kind OR m.agent_slug = p_agent_slug)
    AND m.status = 'active'
    AND m.embedding IS NOT NULL
    AND (1 - (m.embedding <=> p_query_embedding::vector)) >= p_min_similarity
  ORDER BY m.embedding <=> p_query_embedding::vector
  LIMIT p_match_count;
$function$;
