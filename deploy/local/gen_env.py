#!/usr/bin/env python3
"""Write deploy/local/.env with secrets generated on THIS machine.

Nothing in it is committed: the JWT secret, the two tokens signed with it, the
database password and the admin password are all made here. Re-running keeps
an existing file (so a running stack's tokens stay valid) unless --force.

Prints the admin login once, at creation.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

import jwt

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"


def _token(role: str, secret: str) -> str:
    now = int(time.time())
    return jwt.encode({"role": role, "iss": "engine-local", "iat": now,
                       "exp": now + 10 * 365 * 24 * 3600}, secret, algorithm="HS256")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="overwrite an existing .env")
    # Not @example.com: the Team & access screen hides example.com / example.org
    # addresses as test accounts, so the admin would not see themselves.
    ap.add_argument("--admin-email", default="admin@engine.local")
    ap.add_argument("--gateway-port", type=int, default=54421)
    ap.add_argument("--db-port", type=int, default=54432)
    args = ap.parse_args(argv)
    if ENV_FILE.exists() and not args.force:
        print(f"{ENV_FILE} already exists; keeping it (pass --force to replace)")
        return 0

    jwt_secret = secrets.token_urlsafe(48)
    pg_password = secrets.token_urlsafe(24)
    admin_password = secrets.token_urlsafe(12)
    anon, service = _token("anon", jwt_secret), _token("service_role", jwt_secret)
    url = f"http://127.0.0.1:{args.gateway_port}"
    dsn = f"postgresql://postgres:{pg_password}@127.0.0.1:{args.db_port}/postgres"
    lines = {
        # the stack
        "POSTGRES_PASSWORD": pg_password,
        "JWT_SECRET": jwt_secret,
        "ANON_KEY": anon,
        "SERVICE_ROLE_KEY": service,
        "GATEWAY_PORT": str(args.gateway_port),
        "DB_PORT": str(args.db_port),
        # the engine: both of its databases are this one stack
        "ENVIRONMENT": "development",
        "SUPABASE_URL": url,
        "SUPABASE_ANON_KEY": anon,
        "SUPABASE_SERVICE_KEY": service,
        "SUPABASE_JWT_SECRET": jwt_secret,
        "AI_SUPABASE_URL": url,
        "AI_SUPABASE_ANON_KEY": anon,
        "AI_SUPABASE_SERVICE_KEY": service,
        "AI_DATABASE_URL": dsn,
        "ADMIN_EMAIL": args.admin_email,
        "ADMIN_PASSWORD": admin_password,
        "ENABLE_GENERIC_AGENTS": "1",
        "ENABLE_ADMIN_MCP": "1",
        # Local dev: let agents use MCP servers and HTTP tools on this machine.
        "MCP_ALLOW_LOCAL_ENDPOINTS": "1",
        "ALLOWED_ORIGINS": "*",
        "LMNR_DISABLED": "true",
    }
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in lines.items()))
    ENV_FILE.chmod(0o600)
    print(f"wrote {ENV_FILE}")
    print(f"admin login: {args.admin_email} / {admin_password}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
