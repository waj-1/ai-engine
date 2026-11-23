#!/usr/bin/env python3
"""Write deploy/local/.env with secrets generated on THIS machine.

Nothing in it is committed: the JWT secret, the two tokens signed with it, the
database password, the admin password and SECRET_KEY (which encrypts the model
keys stored in the portal) are all made here. Re-running keeps an existing
file (so a running stack's tokens stay valid) unless --force; a file written
before SECRET_KEY existed gets one added.

Prints the admin login once, at creation. This hand-run path keeps a
generated admin login (the engine then never shows its first-run page); pass
--no-admin to leave it out and create the account on the first-run page
instead, as `docker compose up` at the repository root does.
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


def new_secret_key() -> str:
    """A SECRET_KEY: encrypts the model keys stored in the portal
    (core/provider_keys.py). Losing it makes those stored keys unreadable."""
    return secrets.token_urlsafe(48)


def add_secret_key(env_text: str, quote=lambda v: v) -> str:
    """``env_text`` with a SECRET_KEY line appended when it has none."""
    if any(line.startswith("SECRET_KEY=") for line in env_text.splitlines()):
        return env_text
    sep = "" if env_text.endswith("\n") or not env_text else "\n"
    return f"{env_text}{sep}SECRET_KEY={quote(new_secret_key())}\n"


def build_env(*, supabase_url: str, database_url_host: str, admin_email: str | None,
              admin_password: str | None = None, gateway_port: int = 54421,
              db_port: int = 54432) -> dict[str, str]:
    """Every setting the stack and the engine need, with fresh secrets.

    ``supabase_url`` is where the ENGINE reaches the gateway and
    ``database_url_host`` is ``host:port`` where it reaches Postgres: loopback
    addresses for an engine run on this machine, service names for one run
    inside the compose network (deploy/compose/setup.py).

    With ``admin_email`` None there is no admin login in the result at all:
    the init step then creates no account, and the engine's first-run page
    does (it asks for the setup code the engine prints when it starts).
    """
    jwt_secret = secrets.token_urlsafe(48)
    pg_password = secrets.token_urlsafe(24)
    admin = {}
    if admin_email:
        admin = {"ADMIN_EMAIL": admin_email,
                 "ADMIN_PASSWORD": admin_password or secrets.token_urlsafe(12)}
    anon, service = _token("anon", jwt_secret), _token("service_role", jwt_secret)
    dsn = f"postgresql://postgres:{pg_password}@{database_url_host}/postgres"
    return {
        # the stack
        "POSTGRES_PASSWORD": pg_password,
        "JWT_SECRET": jwt_secret,
        "ANON_KEY": anon,
        "SERVICE_ROLE_KEY": service,
        "GATEWAY_PORT": str(gateway_port),
        "DB_PORT": str(db_port),
        # the engine: both of its databases are this one stack
        "ENVIRONMENT": "development",
        "SUPABASE_URL": supabase_url,
        "SUPABASE_ANON_KEY": anon,
        "SUPABASE_SERVICE_KEY": service,
        "SUPABASE_JWT_SECRET": jwt_secret,
        "AI_SUPABASE_URL": supabase_url,
        "AI_SUPABASE_ANON_KEY": anon,
        "AI_SUPABASE_SERVICE_KEY": service,
        "AI_DATABASE_URL": dsn,
        **admin,
        # Encrypts model keys entered on Settings > Model keys.
        "SECRET_KEY": new_secret_key(),
        "ENABLE_GENERIC_AGENTS": "1",
        "ENABLE_ADMIN_MCP": "1",
        # Local dev: let agents use MCP servers and HTTP tools on this machine.
        "MCP_ALLOW_LOCAL_ENDPOINTS": "1",
        "ALLOWED_ORIGINS": "*",
        "LMNR_DISABLED": "true",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="overwrite an existing .env")
    # Not @example.com: the Team & access screen hides example.com / example.org
    # addresses as test accounts, so the admin would not see themselves.
    ap.add_argument("--admin-email", default="admin@engine.local")
    ap.add_argument("--no-admin", action="store_true",
                    help="write no admin login: create the account on the engine's first-run page")
    ap.add_argument("--gateway-port", type=int, default=54421)
    ap.add_argument("--db-port", type=int, default=54432)
    args = ap.parse_args(argv)
    if ENV_FILE.exists() and not args.force:
        print(f"{ENV_FILE} already exists; keeping it (pass --force to replace)")
        text = ENV_FILE.read_text()
        topped_up = add_secret_key(text)
        if topped_up != text:
            ENV_FILE.write_text(topped_up)
            print("added SECRET_KEY to it (model keys stored in the portal are encrypted with it)")
        return 0

    lines = build_env(
        supabase_url=f"http://127.0.0.1:{args.gateway_port}",
        database_url_host=f"127.0.0.1:{args.db_port}",
        admin_email=None if args.no_admin else args.admin_email,
        gateway_port=args.gateway_port, db_port=args.db_port,
    )
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in lines.items()))
    ENV_FILE.chmod(0o600)
    print(f"wrote {ENV_FILE}")
    if args.no_admin:
        print("no admin login written: start the engine and open it in a browser; "
              "the first-run page asks for the setup code the engine prints at start")
    else:
        print(f"admin login: {args.admin_email} / {lines['ADMIN_PASSWORD']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
