// ai_agent/supabase/db-functions/rpc/touch_user_memory.ts
import { PrismaClient } from '@prisma/client';

// language=PostgreSQL
const DB_FUNCTION = `
CREATE OR REPLACE FUNCTION "public"."touch_user_memory"(p_ids uuid[])
RETURNS void LANGUAGE sql AS $function$
  UPDATE public.ai_user_memory
  SET use_count = use_count + 1, last_used_at = now()
  WHERE id = ANY(p_ids);
$function$;
`;

async function execute(client: PrismaClient) {
  await client.$executeRawUnsafe(DB_FUNCTION);
}

export default execute;
