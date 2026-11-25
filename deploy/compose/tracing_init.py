#!/usr/bin/env python3
"""Make the tracing store ready for the engine (STACK_TRACING=1 installs).

Run by deploy/compose/start_engine.sh after the init step, with engine.env
loaded, and only when the setup step turned tracing on (it then wrote
``TRACING_DATABASE_URL`` and the ``LMNR_*`` settings). It:

1. waits for Laminar's own migration runner (its frontend container) to
   finish: the ``project_api_keys`` table in Laminar's database, the
   ClickHouse tables the portal reads and the runner's "schema applied" marker;
2. stores this install's ingest key where Laminar looks keys up: a workspace,
   a project and the key's SHA3-256 digest (the key itself is never stored,
   the same as a key made on Laminar's own page). No browser step.

Safe to run again: the rows have fixed ids and are inserted once.

It never stops the engine from starting. When the store is not ready in
``TRACING_WAIT_SEC`` (default 900) it says so and exits 0: the engine runs,
its screens report the store as unavailable, and a restart tries again.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
PROJECT_ID = "00000000-0000-4000-8000-000000000002"
KEY_ID = "00000000-0000-4000-8000-000000000003"
#: ClickHouse tables that must exist before the engine exports a span: the two
#: every /admin observability screen reads, and the marker the migration
#: runner's container makes once Laminar's last migration has been applied
#: (some migrations rebuild `spans`, so "the table exists" is not "ready").
#: TRACING_READY_TABLES (comma separated) replaces the list, for a stack whose
#: migration runner makes no marker.
READY_TABLES = ("spans", "traces_replacing", "engine_schema_ready")


def ready_tables() -> tuple:
    raw = [t.strip() for t in (os.getenv("TRACING_READY_TABLES") or "").split(",") if t.strip()]
    return tuple(raw) or READY_TABLES


def key_digest(key: str) -> str:
    """How Laminar stores a project API key (app-server looks it up by this)."""
    return hashlib.sha3_256(key.encode("utf-8")).hexdigest()


def key_shorthand(key: str) -> str:
    return f"{key[:4]}...{key[-4:]}"


def clickhouse_tables(url: str, user: str, password: str, database: str) -> set[str]:
    """Names of the tables in `database`. Raises when ClickHouse does not answer."""
    sql = "SELECT name FROM system.tables WHERE database = {db:String} FORMAT TSV"
    query = urllib.parse.urlencode({"param_db": database, "readonly": "1"})
    req = urllib.request.Request(f"{url.rstrip('/')}/?{query}", data=sql.encode("utf-8"))
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return {line.strip() for line in resp.read().decode("utf-8").splitlines() if line.strip()}


def key_table_exists(dsn: str) -> bool:
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.project_api_keys') IS NOT NULL "
                        "AND to_regclass('public.subscription_tiers') IS NOT NULL")
            return bool(cur.fetchone()[0])
    finally:
        conn.close()


def store_key(dsn: str, key: str) -> bool:
    """Insert the workspace, project and key digest. True when the key row was
    added by this call, False when it was already there."""
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                        (WORKSPACE_ID, "engine"))
            cur.execute("INSERT INTO projects (id, name, workspace_id) VALUES (%s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING", (PROJECT_ID, "engine", WORKSPACE_ID))
            cur.execute("DELETE FROM project_api_keys WHERE id = %s AND hash <> %s",
                        (KEY_ID, key_digest(key)))
            cur.execute("INSERT INTO project_api_keys (id, project_id, name, hash, shorthand) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                        (KEY_ID, PROJECT_ID, "engine", key_digest(key), key_shorthand(key)))
            return cur.rowcount == 1
    finally:
        conn.close()


def wait_ready(dsn: str, ch: dict, timeout: int, sleep=time.sleep, clock=time.monotonic) -> str:
    """Block until both stores have their schema. Returns "" when ready, else
    what it was still waiting for."""
    deadline = clock() + timeout
    waiting = "the tracing store"
    while True:
        try:
            if not key_table_exists(dsn):
                waiting = "Laminar's database tables"
            else:
                missing = set(ready_tables()) - clickhouse_tables(**ch)
                if not missing:
                    return ""
                waiting = "ClickHouse tables: " + ", ".join(sorted(missing))
        except Exception as exc:  # noqa: BLE001 — not up yet
            waiting = f"{type(exc).__name__}: {exc}"
        if clock() >= deadline:
            return waiting
        sleep(3)


def main() -> int:
    dsn = (os.getenv("TRACING_DATABASE_URL") or "").strip()
    key = (os.getenv("LMNR_PROJECT_API_KEY") or "").strip()
    if not dsn or not key:
        print("[tracing] off on this install (the setup step ran without STACK_TRACING)", flush=True)
        return 0
    ch = {"url": os.getenv("LMNR_CLICKHOUSE_URL") or "http://lmnr-clickhouse:8123",
          "user": os.getenv("LMNR_CLICKHOUSE_USER") or "clickhouse",
          "password": os.getenv("LMNR_CLICKHOUSE_PASSWORD") or "clickhouse",
          "database": os.getenv("LMNR_CLICKHOUSE_DB") or "default"}
    try:
        timeout = max(10, int(os.getenv("TRACING_WAIT_SEC") or 900))
    except ValueError:
        timeout = 900
    print("[tracing] waiting for the tracing store's schema…", flush=True)
    waiting = wait_ready(dsn, ch, timeout)
    if waiting:
        print(f"[tracing] NOT READY after {timeout}s (still waiting for {waiting}). The engine "
              "starts without it: the portal reports the tracing store as unavailable until the "
              "engine is restarted with the store up.", flush=True)
        return 0
    try:
        added = store_key(dsn, key)
    except Exception as exc:  # noqa: BLE001 — never stop the engine for tracing
        print(f"[tracing] could not store the ingest key ({type(exc).__name__}: {exc}). "
              "Spans will be refused until the engine is restarted.", flush=True)
        return 0
    print("[tracing] ready" + (" (ingest key stored)" if added else " (ingest key already stored)"),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
