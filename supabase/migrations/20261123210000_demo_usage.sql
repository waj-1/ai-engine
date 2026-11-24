-- Demo mode (demo_mode/limits.py): the public demo's daily usage counters.
--
-- One row per (UTC day, bucket). `chat_turns` is the global daily cap on
-- model turns (DEMO_DAILY_TURNS); `reset` records that the nightly reset ran,
-- so of several engine processes exactly one runs it each day.
-- `demo_usage_take` takes `p_amount` units atomically, all or none, and
-- refuses (returns false) when that would take the bucket past `p_limit`,
-- so two processes can never both take the last turn. Used only when DEMO_MODE is on; an install without demo mode
-- has an empty table.
CREATE TABLE IF NOT EXISTS public.demo_usage_daily (
    day date NOT NULL,
    bucket text NOT NULL,
    used integer NOT NULL DEFAULT 0,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT demo_usage_daily_pkey PRIMARY KEY (day, bucket)
);

COMMENT ON TABLE public.demo_usage_daily IS
    'Demo mode: per-day usage counters (global turn cap, nightly reset marker).';

CREATE OR REPLACE FUNCTION public.demo_usage_take(p_bucket text, p_limit integer,
                                                  p_amount integer DEFAULT 1)
RETURNS boolean
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    v_day date := (now() AT TIME ZONE 'utc')::date;
    v_taken integer;
BEGIN
    INSERT INTO public.demo_usage_daily (day, bucket, used)
    VALUES (v_day, p_bucket, 0)
    ON CONFLICT (day, bucket) DO NOTHING;
    UPDATE public.demo_usage_daily
       SET used = used + GREATEST(p_amount, 1), updated_at = now()
     WHERE day = v_day AND bucket = p_bucket AND used + GREATEST(p_amount, 1) <= p_limit
    RETURNING used INTO v_taken;
    RETURN v_taken IS NOT NULL;
END;
$$;

CREATE OR REPLACE FUNCTION public.demo_usage_used(p_bucket text)
RETURNS integer
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    SELECT COALESCE((SELECT used FROM public.demo_usage_daily
                      WHERE day = (now() AT TIME ZONE 'utc')::date AND bucket = p_bucket), 0);
$$;

ALTER TABLE public.demo_usage_daily ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "demo_usage_daily_service_role_all" ON public.demo_usage_daily;
CREATE POLICY "demo_usage_daily_service_role_all" ON public.demo_usage_daily
    AS PERMISSIVE FOR ALL TO service_role
    USING (true) WITH CHECK (true);

REVOKE ALL ON TABLE public.demo_usage_daily FROM anon, authenticated;
GRANT ALL ON TABLE public.demo_usage_daily TO service_role;
REVOKE ALL ON FUNCTION public.demo_usage_take(text, integer, integer) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.demo_usage_used(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.demo_usage_take(text, integer, integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.demo_usage_used(text) TO service_role;
