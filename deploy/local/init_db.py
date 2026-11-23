#!/usr/bin/env python3
"""Prepare the local stack's database: migrations, db-functions, the admin user
(when ADMIN_EMAIL and ADMIN_PASSWORD are set; otherwise the engine's first-run
page creates it).

Run from the repository root, with deploy/local/.env loaded and the stack up:

    set -a; . deploy/local/.env; set +a
    python deploy/local/init_db.py

Idempotent. Reuses the steps of deploy/_init_db_common.py (the same ones the
bundled demo stacks run inside a container), pointed at this checkout instead
of /app, and stops before any client seed or knowledge-base ingest: the
engine's own boot seeders fill in the rest on first start.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "deploy"))

import _init_db_common as common  # noqa: E402

# The shared module assumes the engine image's layout (/app). Point it here.
common.APP = REPO
common.MIGRATIONS_DIR = REPO / "supabase" / "migrations"
common.DBFUNC_DIR = REPO / "supabase" / "db-functions"
common.RLS_TABLES_TS = common.DBFUNC_DIR / "rls" / "agent_tables.ts"


def admin_login() -> tuple[str, str] | None:
    """The admin login to create, or None when none is configured (the
    account is then created on the engine's first-run page). Setting only one
    of the two is a mistake worth stopping for."""
    email = (os.getenv("ADMIN_EMAIL") or "").strip()
    password = os.getenv("ADMIN_PASSWORD") or ""
    if bool(email) != bool(password.strip()):
        raise SystemExit("set both ADMIN_EMAIL and ADMIN_PASSWORD, or neither "
                         "(with neither, the admin account is created on the first-run page)")
    return (email, password) if email else None


def main() -> int:
    for name in ("AI_DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        if not os.getenv(name):
            raise SystemExit(f"{name} is not set — load deploy/local/.env first (see the docstring)")
    admin = admin_login()
    config = common.InitConfig(
        client_name="local", seed_script="", ingest_script="",
        admin_email_default=admin[0] if admin else "",
        admin_password_default=admin[1] if admin else "",
        postgres_password_default="",
    )
    init = common.DBInitializer(config)
    init.wait_for_db()
    conn = init.connect()
    init.bootstrap_roles(conn)
    init.apply_migrations(conn)
    init.apply_db_functions(conn)
    init.ensure_user_table(conn)
    init.ensure_default_model(conn)
    init.reload_postgrest_schema(conn)
    if admin:
        init.ensure_admin_user(conn)
    else:
        init.log("no ADMIN_EMAIL / ADMIN_PASSWORD: no admin account created here. "
                 "The engine's first-run page creates it (the setup code is in the engine's log).")
    conn.close()
    init.log("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
