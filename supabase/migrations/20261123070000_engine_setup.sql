-- First-run setup of an engine that has no admin account yet
-- (admin/services/first_run.py, the page at /admin/setup).
--
-- One row, id = 1. While the engine has no superuser, each boot stores the
-- SHA-256 digest of a new one-time setup code here (the code itself is only
-- printed in the engine's log), with its expiry and the count of wrong tries.
-- `claimed_at` is the lock that lets exactly one submission create the admin
-- account: it is taken with a conditional UPDATE, so two engine processes
-- cannot both win. `completed_at` closes setup for good: once it is set the
-- setup page never opens again, even if every admin is later removed.
-- `finished_at` records that the first admin walked through the remaining
-- steps (model key, API key) or left them.
CREATE TABLE IF NOT EXISTS public.engine_setup (
    id smallint PRIMARY KEY DEFAULT 1,
    code_hash text,
    code_expires_at timestamp with time zone,
    failed_attempts integer NOT NULL DEFAULT 0,
    claimed_at timestamp with time zone,
    completed_at timestamp with time zone,
    completed_by uuid,
    finished_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT engine_setup_single_row CHECK (id = 1)
);

COMMENT ON TABLE public.engine_setup IS
    'First-run setup state (one row). Holds a digest of the one-time setup code, never the code.';

-- Service role only: no client token may read the digest or reopen setup.
ALTER TABLE public.engine_setup ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "engine_setup_service_role_all" ON public.engine_setup;
CREATE POLICY "engine_setup_service_role_all" ON public.engine_setup
    AS PERMISSIVE FOR ALL TO service_role
    USING (true) WITH CHECK (true);

REVOKE ALL ON TABLE public.engine_setup FROM anon, authenticated;
GRANT ALL ON TABLE public.engine_setup TO service_role;
