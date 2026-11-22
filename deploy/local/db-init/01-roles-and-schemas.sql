-- Runs once, at first DB init (mounted into /docker-entrypoint-initdb.d/).
-- Creates the roles + schemas the bundled Supabase services (GoTrue, PostgREST,
-- Kong) expect, BEFORE any of them connect — so there's no start-order deadlock.
-- The application schema (agent tables, KB, memory, the `user` table) is applied
-- later by the init step via the engine migrations + db-functions.

-- PostgREST / RLS roles -----------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN
    CREATE ROLE anon NOLOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN
    CREATE ROLE authenticated NOLOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='service_role') THEN
    CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authenticator') THEN
    CREATE ROLE authenticator NOINHERIT LOGIN;
  END IF;
END
$$;

-- PostgREST logs in as `authenticator` (then switches to anon/authenticated/
-- service_role per the JWT `role` claim). Give it a login password. The
-- supabase/postgres image runs this with POSTGRES_PASSWORD in the environment;
-- :'pw' below binds it from a psql variable the entrypoint passes through.
\set pw `echo "$POSTGRES_PASSWORD"`
ALTER ROLE authenticator WITH PASSWORD :'pw';

GRANT anon, authenticated, service_role TO authenticator;
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;

-- GoTrue's schema (it owns + migrates it; it just needs it to pre-exist) ----
CREATE SCHEMA IF NOT EXISTS auth;
GRANT ALL ON SCHEMA auth TO postgres;
