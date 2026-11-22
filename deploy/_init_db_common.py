#!/usr/bin/env python3
"""Shared one-shot, idempotent DB initialiser for the standalone engine
stacks (deploy/local, and each deployment's own).

Each `deploy/<client>/init_db.py` is a thin wrapper that builds an
:class:`InitConfig` with its client-specific values and calls :func:`main`.
All the actual logic — applying migrations, extracting + replaying
db-functions SQL, seeding the admin user, running the per-client seed/ingest
scripts — lives here so the two clients can't drift.

Two modes: the default full init (everything below), and
``--migrations-only``, which applies schema and stops before anything that
writes data. The redeploy path uses the latter on every rolled image
(each deployment's rollout script) — the bundled Supabase has no other migration
runner, so without it the demo's schema silently drifts behind its code.

Runs INSIDE the `<client>-init` container (the engine image) against the
BUNDLED Supabase. Safe to re-run: every step is insert-if-missing /
CREATE OR REPLACE / IF NOT EXISTS.

Steps (in order, see :meth:`DBInitializer.run`):
  0. Wait for Postgres to accept connections.
  1. Apply ai_agent/supabase/migrations/**.sql in timestamp order (raw SQL).
  2. Apply ai_agent/supabase/db-functions/** (RPCs, RLS, triggers, validation)
     by extracting the SQL the .ts wrappers emit — no Node/Prisma needed.
  3. Create the minimal `public.user` table + Postgres roles GoTrue/PostgREST
     expect, then a GoTrue admin user + a superuser `user` row for /admin login.
  4. Seed a default agent_models row (a fresh AI DB has none; the seed script
     needs one).
  5. Seed the client: config.seed_script.
  6. Ingest the client's KB: config.ingest_script (needs GOOGLE_API_KEY).

The engine image bakes core/ runtime/ etc. at /app; the compose file mounts
scripts/ + supabase/ (which .dockerignore keeps OUT of the image) + this
module + the client wrapper at /app so the seeders and SQL are reachable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psycopg2

APP = Path("/app")
MIGRATIONS_DIR = APP / "supabase" / "migrations"
DBFUNC_DIR = APP / "supabase" / "db-functions"

# The canonical list of agent/ai tables that get the service_role RLS policy
# lives in db-functions/rls/agent_tables.ts — that file is what the real
# (Supabase CLI) deploy runs. This module can't execute TypeScript, so it
# reads the table names straight out of that file's `TABLES` array instead of
# keeping a second copy.
#
# It used to keep a hand-maintained duplicate, which silently drifted: the
# five tables added by the skills (#1391) and bake-off (#1398) migrations
# landed in agent_tables.ts but not here, so on every standalone stack those
# tables came up with RLS disabled and no `set_updated_at` trigger. Parsing
# the one file removes the class of bug rather than re-syncing the copy.
RLS_TABLES_TS = DBFUNC_DIR / "rls" / "agent_tables.ts"

# Only used when agent_tables.ts can't be read (it's mounted read-only into
# the init container, so that should not happen — but a missing mount must
# not take the whole stack down). Deliberately the pre-drift list: tables
# added after this fallback was written are picked up from the .ts file.
_FALLBACK_RLS_TABLES = [
    'agent_profiles', 'agent_profile_versions', 'agent_models', 'agent_tools',
    'agent_prompts', 'agent_prompt_versions', 'agent_data_sources', 'agent_mcp_servers',
    'agent_channels', 'agent_env_vars', 'agent_deployment_targets', 'agent_traces',
    'agent_e2e_runs', 'agent_eval_suites', 'agent_eval_runs', 'agent_eval_schedules',
    'agent_bakeoffs',
    'agent_test_runs', 'agent_audit_log', 'admin_jobs', 'test_fixtures',
    'ai_chat_sessions', 'ai_chat_messages', 'ai_kb_pages', 'ai_kb_embeddings',
    'ai_conversation_insights', 'ai_background_leases',
    'agent_skills', 'agent_skill_versions', 'agent_skill_resources', 'agent_profile_skills',
    'ai_voice_usage_daily',
    'ai_voice_budgets', 'ai_voice_scenarios', 'ai_voice_scenario_runs',
    'ai_voice_recordings', 'ai_voice_recording_chunks',
    'api_keys',
    'provider_keys',
    'agent_types',
    'engine_setup',
]


def load_rls_tables(ts_path: Path | None = None) -> list[str]:
    """Read the `TABLES` arrays out of db-functions/rls/*.ts.

    With no ``ts_path``: agent_tables.ts (the engine's tables) first, then
    every other file in that directory (e.g. a product's own
    product_tables.ts), in name order, de-duplicated. The real deploy's
    runner applies every file there too. A missing or unparsable
    agent_tables.ts still means the fallback list; another file that does not
    parse is skipped with a warning. With ``ts_path``: that one file.

    Returns the table names in declaration order, de-duplicated. Falls back to
    :data:`_FALLBACK_RLS_TABLES` if the file is missing or its shape changed
    (an empty parse means the regex stopped matching — treat that as a parse
    failure, not as "no tables", or the stack would come up with RLS off
    everywhere).

    `InitConfig.extra_rls_tables` still appends tables a client's init names
    that no file lists, and
    `_apply_service_role_rls` skips tables that don't exist in this DB, so a
    client seeing another client's table name here is a no-op.

    `ts_path` defaults to :data:`RLS_TABLES_TS`, resolved on each call rather
    than bound at def time so tests can point it at the repo checkout.
    """
    if ts_path is not None:
        return _load_one_rls_file(ts_path)
    tables = _load_one_rls_file(RLS_TABLES_TS)
    for extra in sorted(RLS_TABLES_TS.parent.glob("*.ts")):
        if extra.name == RLS_TABLES_TS.name:
            continue
        names = _parse_rls_tables(extra)
        if not names:
            print(f"[init] WARNING: no TABLES entries parsed from {extra}; skipping it", flush=True)
        tables.extend(names)
    return list(dict.fromkeys(tables))


def _parse_rls_tables(ts_path: Path) -> list[str]:
    try:
        text = ts_path.read_text(encoding="utf-8")
    except OSError:
        return []
    match = re.search(r"const\s+TABLES\s*(?::[^=]*)?=\s*\[(.*?)\]", text, flags=re.DOTALL)
    return re.findall(r"['\"]([a-z_][a-z0-9_]*)['\"]", match.group(1)) if match else []


def _load_one_rls_file(ts_path: Path) -> list[str]:
    try:
        text = ts_path.read_text(encoding="utf-8")  # missing mount / unreadable
    except OSError as exc:
        print(
            f"[init] WARNING: cannot read {ts_path} ({exc}); "
            f"using fallback RLS table list",
            flush=True,
        )
        return list(_FALLBACK_RLS_TABLES)

    match = re.search(r"const\s+TABLES\s*(?::[^=]*)?=\s*\[(.*?)\]", text, flags=re.DOTALL)
    names = re.findall(r"['\"]([a-z_][a-z0-9_]*)['\"]", match.group(1)) if match else []
    if not names:
        print(
            f"[init] WARNING: no TABLES entries parsed from {ts_path}; "
            f"using fallback RLS table list",
            flush=True,
        )
        return list(_FALLBACK_RLS_TABLES)

    return list(dict.fromkeys(names))


# ──────────────────────────────────────────────────────────────────────────
# Roles GoTrue / PostgREST expect (supabase/postgres ships most; be safe)
# ──────────────────────────────────────────────────────────────────────────
# The PostgREST/auth roles + the `auth` schema are created at DB-init time by
# deploy/<client>/db-init/01-roles-and-schemas.sql (so GoTrue/PostgREST can
# start before this runs). This is a defensive re-assert in case the init
# script was skipped (e.g. an existing volume) — fully idempotent.
ROLE_BOOTSTRAP = """
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
END
$$;

GRANT anon, authenticated, service_role TO authenticator;
GRANT usage ON SCHEMA public TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;
"""


# ──────────────────────────────────────────────────────────────────────────
# 4. Minimal `user` table for admin-portal superuser auth
# ──────────────────────────────────────────────────────────────────────────
USER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public."user" (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_id     uuid UNIQUE,
    email       text,
    permission  jsonb NOT NULL DEFAULT '{}'::jsonb,
    role        text,
    business_id uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
-- Columns the admin portal's Team & access screen (admin/router.py
-- users_screen) and global search read off the main project's `user` table.
-- Without them that screen 500s on every bundled stack with
-- "column user.phone_number does not exist". ADD COLUMN IF NOT EXISTS so a
-- stack created before these were added is repaired on its next init run.
ALTER TABLE public."user" ADD COLUMN IF NOT EXISTS phone_number text;
ALTER TABLE public."user" ADD COLUMN IF NOT EXISTS country_code text;
ALTER TABLE public."user" ADD COLUMN IF NOT EXISTS permission_level text;
ALTER TABLE public."user" ADD COLUMN IF NOT EXISTS is_deleted boolean NOT NULL DEFAULT false;
ALTER TABLE public."user" ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_service_role_all ON public."user";
CREATE POLICY user_service_role_all ON public."user"
    AS PERMISSIVE FOR ALL TO service_role USING (true) WITH CHECK (true);
GRANT ALL ON public."user" TO service_role;
"""


def _extract_template_literals(ts_text: str) -> list[str]:
    """Pull every backtick-delimited template literal out of a .ts file."""
    # Non-greedy match of `...` allowing newlines. The db-function .ts files use
    # plain template literals with no ${} interpolation in their SQL bodies.
    return re.findall(r"`([^`]*)`", ts_text, flags=re.DOTALL)


@dataclass(frozen=True)
class InitConfig:
    """Per-client differences the shared initialiser needs.

    Everything else (the actual SQL, ordering, retries) is identical across
    clients and lives in :class:`DBInitializer`.
    """

    client_name: str  # log prefix + human label, e.g. "local"
    seed_script: str  # path (relative to /app) of the client seeder
    ingest_script: str  # path (relative to /app) of the KB ingest script
    admin_email_default: str  # ADMIN_EMAIL fallback
    admin_password_default: str  # ADMIN_PASSWORD fallback
    postgres_password_default: str  # POSTGRES_PASSWORD fallback
    # Client tables to RLS-protect on top of the ones `load_rls_tables()` reads
    # out of rls/agent_tables.ts (e.g. ("tenant_leads",)). Overlap is harmless —
    # the two lists are merged and de-duplicated.
    extra_rls_tables: tuple[str, ...] = field(default_factory=tuple)
    # Local markdown files to ingest via `ingest_script --file <path>` AFTER
    # the main ingest runs (e.g. a hand-authored troubleshooting doc that
    # isn't part of the crawled/committed corpus). The main ingest resets
    # the client's KB source before re-ingesting it (crawl mode's
    # reset_sources), so these must run SECOND or a fresh-stack boot would
    # seed the doc and then immediately wipe it. Each is `--file`'s
    # upsert-by-path, non-fatal (a flaky ingest shouldn't block stack
    # bring-up) — e.g. a client's own troubleshooting guide.
    extra_ingest_files: tuple[str, ...] = field(default_factory=tuple)


class SqlUserRows:
    """``public."user"`` over a psycopg2 connection: how this init step writes
    the admin's row for ``core.admin_account.ensure_admin_account``."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def find(self, auth_id: str):
        with self._conn.cursor() as cur:
            cur.execute('SELECT id FROM public."user" WHERE auth_id=%s LIMIT 1', (auth_id,))
            row = cur.fetchone()
        return str(row[0]) if row else None

    def update(self, auth_id: str, fields: dict) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                'UPDATE public."user" SET email=%s, permission=%s::jsonb, role=%s WHERE auth_id=%s',
                (fields["email"], json.dumps(fields["permission"]), fields["role"], auth_id),
            )

    def insert(self, fields: dict):
        with self._conn.cursor() as cur:
            cur.execute(
                'INSERT INTO public."user" (auth_id, email, permission, role) '
                'VALUES (%s, %s, %s::jsonb, %s) RETURNING id',
                (fields["auth_id"], fields["email"], json.dumps(fields["permission"]), fields["role"]),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None


class DBInitializer:
    """Runs the full init sequence for one client, parameterised by InitConfig."""

    def __init__(self, config: InitConfig) -> None:
        self.config = config
        # `INIT_DATABASE_URL` is what the <client>-init service sets. The
        # ENGINE service carries the same DSN under `AI_DATABASE_URL`, and the
        # deploy-time `--migrations-only` run (a deployment's rollout script) runs
        # in a one-off container off that service — see the note there for why
        # it can't use the init service.
        dsn = os.getenv("INIT_DATABASE_URL") or os.getenv("AI_DATABASE_URL")
        if not dsn:
            raise SystemExit(
                "no target database: set INIT_DATABASE_URL (or AI_DATABASE_URL)"
            )
        self.dsn = dsn
        # Read for parity with the historical module-level constant; the DSN
        # already carries the password, so this is not consumed downstream.
        self.postgres_password = os.getenv(
            "POSTGRES_PASSWORD", config.postgres_password_default
        )
        # Read at run time (not import time) so the list reflects the
        # agent_tables.ts that is actually mounted into this container.
        self.rls_tables = list(
            dict.fromkeys(load_rls_tables() + list(config.extra_rls_tables))
        )
        self.updated_at_tables = self.rls_tables + ['ai_user_memory']

    def log(self, msg: str) -> None:
        print(f"[{self.config.client_name}-init] {msg}", flush=True)

    # ──────────────────────────────────────────────────────────────────────
    # 0. Wait for Postgres
    # ──────────────────────────────────────────────────────────────────────
    def wait_for_db(self, timeout: int = 120) -> None:
        self.log("waiting for Postgres…")
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                conn = psycopg2.connect(self.dsn)
                conn.close()
                self.log("Postgres is up.")
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(2)
        raise SystemExit(f"Postgres not reachable after {timeout}s: {last_err}")

    def connect(self):
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = True
        return conn

    def reload_postgrest_schema(self, conn) -> None:
        """Tell PostgREST to reload its schema cache.

        PostgREST caches the DB schema at startup. Since the <client>-init
        container applies the migrations AFTER PostgREST is already running,
        PostgREST 404s on the new tables ("JSON could not be generated") until
        it reloads. PostgREST LISTENs on the `pgrst` channel for this NOTIFY by
        default.
        """
        self.log("reloading PostgREST schema cache (NOTIFY pgrst, 'reload schema')…")
        with conn.cursor() as cur:
            cur.execute("NOTIFY pgrst, 'reload schema';")
        time.sleep(3)  # give PostgREST a moment to pick it up before seeding

    # ──────────────────────────────────────────────────────────────────────
    # 1. Roles
    # ──────────────────────────────────────────────────────────────────────
    def bootstrap_roles(self, conn) -> None:
        self.log("re-asserting Postgres roles (anon/authenticated/service_role grants)…")
        with conn.cursor() as cur:
            try:
                cur.execute(ROLE_BOOTSTRAP)
            except Exception as exc:  # noqa: BLE001 — authenticator may not exist yet
                self.log(f"  (role re-assert partial, non-fatal: {exc})")
        self.log("roles ready.")

    # ──────────────────────────────────────────────────────────────────────
    # 2. Apply migrations
    # ──────────────────────────────────────────────────────────────────────
    def apply_migrations(self, conn) -> None:
        # Skip macOS AppleDouble sidecars (._*.sql) that a Mac `tar` bundles —
        # they are binary and break the utf-8 read / aren't real migrations.
        files = sorted(f for f in MIGRATIONS_DIR.glob("*.sql") if not f.name.startswith("._"))
        if not files:
            raise SystemExit(f"no migrations found under {MIGRATIONS_DIR}")
        self.log(f"applying {len(files)} migrations…")
        for f in files:
            sql = f.read_text(encoding="utf-8")
            # ADD VALUE on an enum can't run inside a txn block; autocommit handles it.
            with conn.cursor() as cur:
                try:
                    cur.execute(sql)
                    self.log(f"  ✓ {f.name}")
                except Exception as exc:  # noqa: BLE001
                    # Idempotent re-runs: most objects use IF NOT EXISTS, but a
                    # few bare CREATEs will raise "already exists" on a re-run.
                    msg = str(exc).lower()
                    if "already exists" in msg or "duplicate" in msg:
                        self.log(f"  ↻ {f.name} (already applied: {exc.__class__.__name__})")
                    else:
                        raise

    # ──────────────────────────────────────────────────────────────────────
    # 3. Apply db-functions (extract SQL from the .ts wrappers)
    # ──────────────────────────────────────────────────────────────────────
    def apply_db_functions(self, conn) -> None:
        """Replay the SQL emitted by db-functions/{rpc,validation,triggers}/*.ts
        and add the service_role RLS policy from rls/agent_tables.ts.

        RPC + validation .ts files are pure CREATE OR REPLACE strings → safe to
        run. RLS + triggers are generated per-table in TS loops; we reproduce
        the RLS service_role policy here (the only RLS the engine relies on) and
        the set_updated_at trigger from validation.
        """
        self.log("applying db-functions…")

        # 3a. RPCs + validation: every template literal is runnable SQL.
        for sub in ("rpc", "validation", "triggers"):
            d = DBFUNC_DIR / sub
            if not d.is_dir():
                continue
            for f in sorted(x for x in d.glob("*.ts") if not x.name.startswith("._")):
                for lit in _extract_template_literals(f.read_text(encoding="utf-8")):
                    stmt = lit.strip()
                    # Skip non-SQL literals (paths, identifiers) — real SQL
                    # starts with a SQL keyword.
                    if not re.match(r"(?is)^\s*(CREATE|ALTER|DROP|DO|GRANT|COMMENT|SELECT|UPDATE|INSERT)\b", stmt):
                        continue
                    with conn.cursor() as cur:
                        try:
                            cur.execute(stmt)
                        except Exception as exc:  # noqa: BLE001
                            # Best-effort replay: extraction is imperfect and
                            # re-runs hit already-existing objects, so we log
                            # and continue rather than abort the whole init.
                            # Previously this was swallowed with a terse note;
                            # classify + include the failing statement so a real
                            # (non-idempotent) failure is visible in the logs.
                            emsg = str(exc).lower()
                            stmt_head = " ".join(stmt.split())[:160]
                            if "already exists" in emsg or "duplicate" in emsg:
                                self.log(
                                    f"  ↻ {sub}/{f.name}: idempotent skip "
                                    f"({exc.__class__.__name__}): {stmt_head}"
                                )
                            else:
                                self.log(
                                    f"  ! WARNING {sub}/{f.name}: db-object create "
                                    f"failed and was skipped: {exc} :: {stmt_head}"
                                )
                self.log(f"  ✓ {sub}/{f.name}")

        # 3b. validation: attach set_updated_at trigger to tables with the col.
        self._attach_updated_at_triggers(conn)

        # 3c. RLS: service_role ALL policy on every agent/ai table (mirrors
        #     rls/agent_tables.ts). The engine uses the service key → bypasses
        #     RLS, but we ship the policy so the DB matches the real schema.
        self._apply_service_role_rls(conn)
        self.log("db-functions applied.")

    def _apply_service_role_rls(self, conn) -> None:
        with conn.cursor() as cur:
            for t in self.rls_tables:
                if not self._table_exists(cur, t):
                    continue
                cur.execute(f'ALTER TABLE "public"."{t}" ENABLE ROW LEVEL SECURITY;')
                cur.execute(f'DROP POLICY IF EXISTS "{t}_service_role_all" ON "public"."{t}";')
                cur.execute(
                    f'CREATE POLICY "{t}_service_role_all" ON "public"."{t}" '
                    f'AS PERMISSIVE FOR ALL TO service_role USING (true) WITH CHECK (true);'
                )

    def _attach_updated_at_triggers(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                'CREATE OR REPLACE FUNCTION "public"."set_updated_at"() '
                'RETURNS TRIGGER LANGUAGE plpgsql AS $f$ '
                'BEGIN NEW.updated_at = now(); RETURN NEW; END; $f$;'
            )
            for t in self.updated_at_tables:
                if not self._table_exists(cur, t):
                    continue
                cur.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name=%s AND column_name='updated_at'",
                    (t,),
                )
                if cur.fetchone() is None:
                    continue
                cur.execute(f'DROP TRIGGER IF EXISTS set_updated_at ON "public"."{t}";')
                cur.execute(
                    f'CREATE TRIGGER set_updated_at BEFORE UPDATE ON "public"."{t}" '
                    f'FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();'
                )

    @staticmethod
    def _table_exists(cur, name: str) -> bool:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
            (name,),
        )
        return cur.fetchone() is not None

    # ──────────────────────────────────────────────────────────────────────
    # 4. Minimal `user` table for admin-portal superuser auth
    # ──────────────────────────────────────────────────────────────────────
    def ensure_user_table(self, conn) -> None:
        self.log("ensuring public.user table (admin auth)…")
        with conn.cursor() as cur:
            cur.execute(USER_TABLE_SQL)

    # ──────────────────────────────────────────────────────────────────────
    # 5. Default agent_models row (a fresh AI DB has none)
    # ──────────────────────────────────────────────────────────────────────
    def ensure_default_model(self, conn) -> None:
        model = os.getenv("AGENT_MAIN_MODEL", "gemini-3-flash-preview")
        self.log(f"ensuring default agent_models row ({model})…")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM public.agent_models WHERE model_string=%s AND is_active=true LIMIT 1",
                (model,),
            )
            if cur.fetchone():
                self.log("  ↻ model row already present.")
                return
            cur.execute(
                "INSERT INTO public.agent_models (name, provider, model_string, environment, is_active) "
                "VALUES (%s, %s, %s, 'staging', true)",
                (model, "google_genai", model),
            )
            self.log("  ✓ model row created.")

    # ──────────────────────────────────────────────────────────────────────
    # 6. GoTrue admin user + superuser `user` row (idempotent)
    # ──────────────────────────────────────────────────────────────────────
    def ensure_admin_user(self, conn) -> bool:
        """Create (or repair) the admin account. True when it exists afterwards.

        The work is ``core.admin_account.ensure_admin_account``, shared with
        the first-run page; only the row is written differently here, over
        this step's own Postgres connection (autocommit psycopg2).
        """
        email = os.getenv("ADMIN_EMAIL", self.config.admin_email_default)
        password = os.getenv("ADMIN_PASSWORD", self.config.admin_password_default)
        self.log(f"ensuring admin superuser ({email})…")

        # supabase-py talks to GoTrue via the kong gateway (SUPABASE_URL).
        from core.admin_account import AdminAccountError, ensure_admin_account
        from core.auth import get_main_client  # engine package, baked into image

        try:
            ensure_admin_account(get_main_client(), SqlUserRows(conn), email, password, log=self.log)
        except AdminAccountError as exc:
            self.log(f"  ! skipping superuser row ({exc}). /admin login may not work — see README.")
            return False
        return True

    # ──────────────────────────────────────────────────────────────────────
    # 7. Seed client + ingest KB (via the existing scripts)
    # ──────────────────────────────────────────────────────────────────────
    def run_script(self, path: str, *, required: bool, extra_args: list[str] | None = None) -> None:
        args = [sys.executable, path, *(extra_args or [])]
        self.log(f"running {' '.join(args[1:])}…")
        proc = subprocess.run(args, cwd=str(APP))
        if proc.returncode != 0:
            msg = f"{path} exited {proc.returncode}"
            if required:
                raise SystemExit(msg)
            self.log(f"  ! {msg} (non-fatal, continuing)")
        else:
            self.log(f"  ✓ {' '.join(args[1:])} done.")

    def run(self, *, migrations_only: bool = False) -> int:
        self.wait_for_db()
        conn = self.connect()
        self.bootstrap_roles(conn)
        self.apply_migrations(conn)
        self.apply_db_functions(conn)
        self.ensure_user_table(conn)
        if migrations_only:
            # Schema ONLY — the mode the deploy calls on every rolled image.
            # It stops here on purpose: the default-model row, the admin user,
            # the client seeder and the KB ingest all write DATA, and a demo
            # stack's data is live state (portal edits, the merchant's IBAN,
            # a crawled KB). Re-running them mid-rehearsal would revert it,
            # and the ingest is a slow network crawl besides.
            #
            # PostgREST still has to be told: it caches the schema, so a column
            # added above stays invisible to it until this NOTIFY — that is the
            # PGRST204 "could not find the 'status' column" class of failure
            # this whole mode exists to prevent.
            self.reload_postgrest_schema(conn)
            conn.close()
            self.log("migrations-only: schema synced (no seed / admin / KB steps).")
            return 0
        self.ensure_default_model(conn)
        # PostgREST started before these tables existed — refresh its schema
        # cache so the seed scripts (which go through PostgREST) can see them.
        self.reload_postgrest_schema(conn)
        self.ensure_admin_user(conn)
        conn.close()

        # The seed script reads AI_SUPABASE_* via core.auth.get_service_client().
        self.run_script(self.config.seed_script, required=True)

        if os.getenv("SKIP_KB_INGEST", "").strip().lower() in ("1", "true", "yes"):
            self.log("SKIP_KB_INGEST set — skipping doc ingest.")
        else:
            # KB ingest needs a live GOOGLE_API_KEY (+ network for crawl-based
            # clients). Non-fatal so the stack still comes up if ingest is flaky.
            self.run_script(self.config.ingest_script, required=False)
            # Local hand-authored docs (e.g. a client's troubleshooting guide)
            # run AFTER the main ingest above, in `--file` mode, which
            # upserts by path instead of resetting the source — running
            # these first would just get wiped by the main ingest's
            # reset_sources. Each is independently non-fatal.
            for extra_file in self.config.extra_ingest_files:
                self.run_script(self.config.ingest_script, required=False,
                                 extra_args=["--file", extra_file])

        self.log("✅ init complete.")
        return 0


def main(config: InitConfig, argv: list[str] | None = None) -> int:
    """Entrypoint every `deploy/<client>/init_db.py` wrapper calls.

    Lives here rather than in each wrapper so the CLI contract can't drift
    between clients — `--migrations-only` has to mean the same thing on every
    client, because the deploy scripts invoke it by name.

    ``--migrations-only`` applies schema (migrations, db-functions, roles,
    `public.user`) and stops before anything that writes data. That is the mode
    a redeploy wants: see :meth:`DBInitializer.run`.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    migrations_only = "--migrations-only" in args
    if migrations_only:
        args.remove("--migrations-only")
    if args:
        raise SystemExit(
            f"unknown argument(s): {' '.join(args)} — only --migrations-only is supported"
        )
    return DBInitializer(config).run(migrations_only=migrations_only)
