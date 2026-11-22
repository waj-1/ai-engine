import { PrismaClient } from '@prisma/client';

// language=PostgreSQL
const DB_FUNCTION = `
CREATE OR REPLACE FUNCTION "public"."match_kb_embeddings"(
    p_source_ids uuid[],
    query_embedding vector(1536),
    match_threshold float DEFAULT 0.5,
    match_count int DEFAULT 5,
    filter_language text DEFAULT NULL,
    filter_type text DEFAULT NULL
)
RETURNS TABLE (
    id uuid, page_id uuid, source_id uuid, content text, heading text,
    page_title text, page_type text, similarity float
)
LANGUAGE plpgsql AS $function$
BEGIN
    RETURN QUERY
    SELECT e.id, e.page_id, e.source_id, e.content, e.heading,
           p.title AS page_title, p.type AS page_type,
           (1 - (e.embedding <=> query_embedding)) AS similarity
    FROM public.ai_kb_embeddings e
    JOIN public.ai_kb_pages p ON e.page_id = p.id
    WHERE e.source_id = ANY(p_source_ids)
      AND (1 - (e.embedding <=> query_embedding)) > match_threshold
      AND (filter_language IS NULL OR p.language = filter_language)
      AND (filter_type IS NULL OR p.type = filter_type)
    ORDER BY e.embedding <=> query_embedding
    LIMIT match_count;
END;
$function$;
`;

async function execute(client: PrismaClient) {
  // Signature changed (added filter_type) — drop the old 5-arg overload so
  // CREATE OR REPLACE doesn't collide with the previous definition.
  await client.$executeRawUnsafe(
    `DROP FUNCTION IF EXISTS "public"."match_kb_embeddings"(uuid[], vector, float, int, text);`
  );
  await client.$executeRawUnsafe(DB_FUNCTION);
}

export default execute;
