// ai_agent/supabase/db-functions/rpc/match_user_memory.ts
import { PrismaClient } from '@prisma/client';

// language=PostgreSQL
const DB_FUNCTION = `
CREATE OR REPLACE FUNCTION "public"."match_user_memory"(
  p_user_id uuid,
  p_agent_kind public.ai_agent_kind,
  p_query_embedding text,
  p_match_count integer DEFAULT 6,
  p_min_similarity real DEFAULT 0.7
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
    AND m.status = 'active'
    AND m.embedding IS NOT NULL
    AND (1 - (m.embedding <=> p_query_embedding::vector)) >= p_min_similarity
  ORDER BY m.embedding <=> p_query_embedding::vector
  LIMIT p_match_count;
$function$;
`;

async function execute(client: PrismaClient) {
  await client.$executeRawUnsafe(DB_FUNCTION);
}

export default execute;
