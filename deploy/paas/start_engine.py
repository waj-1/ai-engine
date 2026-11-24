#!/usr/bin/env python3
"""Start the engine on a platform with no shared volume (Render, Railway).

`docker compose up` keeps this install's secrets in a Docker volume that every
service mounts. A platform that runs each service on its own cannot share a
volume, so there the platform makes the secrets and hands them to each service
as environment variables. This script turns those variables into the settings
the engine reads, applies the database schema, then starts the engine.

It needs:

  JWT_SECRET          signs the tokens the REST layer and the auth service accept
  POSTGRES_PASSWORD   the database password
  SECRET_KEY          encrypts the model keys saved in the portal
  GATEWAY_HOST        where the gateway answers (GATEWAY_PORT, default 8000),
                      or GATEWAY_URL for a full address
  DB_HOST             where Postgres answers (DB_PORT, default 5432)

Optional: ADMIN_EMAIL and ADMIN_PASSWORD (both, or neither: with neither, the
engine's first-run page creates the account), PORT (default 8000), HOST
(default 0.0.0.0), INIT_WAIT_SEC (default 300) and any setting the engine
itself reads. A setting the platform already provides is kept.

The two API tokens are signed here from JWT_SECRET at every start. Nothing
compares them as text on these platforms (the gateway there has no key check,
it is only reachable on the private network), so they do not have to be stored.

Exits non-zero when a step fails, so the platform restarts the service.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "local"))
import gen_env  # noqa: E402

# Made by gen_env for the compose stack only, or meant for a developer's own
# machine: not passed on.
NOT_FOR_THE_ENGINE = ("POSTGRES_PASSWORD", "JWT_SECRET", "ANON_KEY", "SERVICE_ROLE_KEY",
                      "GATEWAY_PORT", "DB_PORT", "MCP_ALLOW_LOCAL_ENDPOINTS")
# An empty value is not "unset" to every library, so empty ones are dropped.
OPTIONAL = ("GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT", "PORTAL_NAME", "EMBED_PROVIDER", "EMBED_BASE_URL",
            "EMBED_MODEL", "EMBED_API_KEY", "ADMIN_EMAIL", "ADMIN_PASSWORD")


def need(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"[start] {name} is not set. See the header of {Path(__file__).name}.")
    return value


def engine_env() -> dict[str, str]:
    """The engine's settings, from the platform's secrets and addresses."""
    jwt_secret, pg_password = need("JWT_SECRET"), need("POSTGRES_PASSWORD")
    secret_key = need("SECRET_KEY")
    gateway = (os.getenv("GATEWAY_URL") or "").strip().rstrip("/")
    if not gateway:
        gateway = f"http://{need('GATEWAY_HOST')}:{(os.getenv('GATEWAY_PORT') or '8000').strip()}"
    db = f"{need('DB_HOST')}:{(os.getenv('DB_PORT') or '5432').strip()}"

    # The same list of settings as the compose stack, so the two cannot drift.
    defaults = gen_env.build_env(supabase_url=gateway, database_url_host=db, admin_email=None)
    for name in NOT_FOR_THE_ENGINE:
        defaults.pop(name, None)

    anon = gen_env._token("anon", jwt_secret)
    service = gen_env._token("service_role", jwt_secret)
    derived = {
        "SUPABASE_URL": gateway,
        "SUPABASE_ANON_KEY": anon,
        "SUPABASE_SERVICE_KEY": service,
        "SUPABASE_JWT_SECRET": jwt_secret,
        "AI_SUPABASE_URL": gateway,
        "AI_SUPABASE_ANON_KEY": anon,
        "AI_SUPABASE_SERVICE_KEY": service,
        # A generated password may hold characters a URL gives a meaning to.
        "AI_DATABASE_URL": f"postgresql://postgres:{quote(pg_password, safe='')}@{db}/postgres",
        "SECRET_KEY": secret_key,
    }
    env = dict(os.environ)
    for name in OPTIONAL:
        if not (env.get(name) or "").strip():
            env.pop(name, None)
    for name, value in defaults.items():
        if name not in derived:
            env.setdefault(name, value)
    env.update(derived)
    env.setdefault("INIT_WAIT_SEC", "300")
    return env


def main() -> int:
    env = engine_env()
    os.chdir(REPO)
    init = subprocess.run([sys.executable, str(HERE.parent / "compose" / "init.py")], env=env)
    if init.returncode:
        return init.returncode
    port = (env.get("PORT") or "8000").strip()
    host = (env.get("HOST") or "0.0.0.0").strip()
    allow = env.get("FORWARDED_ALLOW_IPS") or "127.0.0.1"
    os.execvpe("uvicorn", ["uvicorn", "main:app", "--host", host, "--port", port,
                           "--workers", "1", "--proxy-headers",
                           f"--forwarded-allow-ips={allow}"], env)
    return 1


if __name__ == "__main__":
    sys.exit(main())
