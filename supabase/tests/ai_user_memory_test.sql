-- supabase/tests/ai_user_memory_test.sql
-- Run: psql "postgresql://postgres:postgres@127.0.0.1:54422/postgres" -f supabase/tests/ai_user_memory_test.sql
BEGIN;

-- function exists and is callable with a zero vector → returns 0 rows for an unknown user
DO $$
DECLARE
  n int;
  zero text := '[' || array_to_string(array_fill(0.0::real, ARRAY[768]), ',') || ']';
BEGIN
  SELECT count(*) INTO n FROM public.match_user_memory(
    gen_random_uuid(), 'business'::public.ai_agent_kind, zero, 6, 0.7
  );
  ASSERT n = 0, 'expected 0 matches for unknown user';
END $$;

ROLLBACK;
