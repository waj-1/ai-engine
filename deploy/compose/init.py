#!/usr/bin/env python3
"""Second step of `docker compose up`: the schema, and the admin account when
one was chosen.

Runs in the engine image once the database is healthy, with engine.env
loaded. It waits for the auth service and the REST layer to answer through
the gateway, then runs the same steps as deploy/local/init_db.py (migrations,
db-functions, the admin user). Safe to run again: every `up` runs it.

Two kinds of install:

* ADMIN_EMAIL and ADMIN_PASSWORD were set before the first `up` (a headless
  install). The account is created here, and unlike the hand-run script this
  step FAILS when it was not: the engine starts only after this step
  succeeds, so a stack nobody could log in to is reported here instead of at
  the login page.
* Neither was set. No account is created and nothing is invented: the engine
  starts with no admin, prints a one-time setup code in its log, and its
  first-run page creates the account.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "local"))


def wait_for_gateway(base: str, anon_key: str, timeout: int = 180) -> None:
    """Block until the auth service and the REST layer answer through the gateway."""
    checks = {"auth": f"{base}/auth/v1/health", "rest": f"{base}/rest/v1/"}
    deadline = time.monotonic() + timeout
    pending = dict(checks)
    last = ""
    while pending and time.monotonic() < deadline:
        for name, url in list(pending.items()):
            req = urllib.request.Request(url, headers={"apikey": anon_key})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status < 500:
                        del pending[name]
            except urllib.error.HTTPError as exc:
                if exc.code < 500:      # answered: 401/404 still means "up"
                    del pending[name]
                last = f"{name}: HTTP {exc.code}"
            except Exception as exc:  # noqa: BLE001 — not up yet
                last = f"{name}: {exc}"
        if pending:
            time.sleep(2)
    if pending:
        raise SystemExit(f"[init] the stack did not come up in {timeout}s "
                         f"(still waiting for {', '.join(pending)}; last error: {last})")


def admin_exists(dsn: str, email: str) -> bool:
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM public."user" WHERE email=%s '
                        "AND (permission->>'is_superuser')::boolean LIMIT 1", (email,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def main() -> int:
    for name in ("AI_DATABASE_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY"):
        if not os.getenv(name):
            raise SystemExit(f"[init] {name} is not set: engine.env was not loaded")
    print("[init] waiting for the auth service and the REST layer…", flush=True)
    wait_for_gateway(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])

    import init_db  # deploy/local/init_db.py

    headless = init_db.admin_login() is not None
    code = init_db.main()
    if code:
        return code
    if headless:
        if not admin_exists(os.environ["AI_DATABASE_URL"], os.environ["ADMIN_EMAIL"]):
            print("[init] FAILED: the admin account was not created, so nobody could log in. "
                  "The lines above say what the auth service answered.", flush=True)
            return 1
    else:
        print("[init] no admin account was created (ADMIN_EMAIL / ADMIN_PASSWORD were not set). "
              "Open the engine in a browser: its first-run page creates the account and asks "
              "for the setup code printed by `docker compose logs engine`.", flush=True)
    print("[init] ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
