#!/usr/bin/env python3
"""First step of `docker compose up`: make this install's secrets.

Runs once, in the engine image, before any other service starts. It writes
three files into the state volume (``ENGINE_STATE_DIR``, /run/engine):

  stack.env        what the database, auth service and gateway read at start
  engine.env       what the engine and the init step read
  postgrest.conf   the REST layer's settings (its image has no shell to read
                   an env file with, so it is given a config file instead)

Nothing here is committed or passed through the host: the JWT secret, the two
tokens signed with it, the database password, SECRET_KEY (which encrypts the
model keys stored in the portal) are generated in the container and kept in
the volume. A second run
keeps the existing files, so a restarted stack's tokens and passwords stay
valid; an install made before SECRET_KEY existed gets one added. `docker compose down -v` deletes the volume, and
the next `up` starts a new install with new secrets.

The admin account. There is no default login. With ADMIN_EMAIL and
ADMIN_PASSWORD both set before the first `up`, they are kept here and the init
step creates that account (a headless install). With neither, nothing about an
admin is written: the engine starts with no account, prints a one-time setup
code (`docker compose logs engine`) and its first-run page creates the account.
Setting only one of the two stops the stack with a message.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))
import gen_env  # noqa: E402

# Where the services reach each other inside the compose network.
GATEWAY_URL = "http://gateway:8000"
DB_HOST = "db:5432"
# A model served on the host machine (Ollama's default port), from a container.
HOST_OLLAMA_URL = "http://host.docker.internal:11434/v1"


def _env_file(pairs: dict[str, str]) -> str:
    """A file a POSIX shell can source: every value quoted."""
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in pairs.items())


def render(admin_email: str | None, admin_password: str | None) -> dict[str, str]:
    """{file name: content} for a new install. No admin_email: no admin login."""
    env = gen_env.build_env(
        supabase_url=GATEWAY_URL, database_url_host=DB_HOST,
        admin_email=admin_email, admin_password=admin_password,
    )
    pg, jwt_secret = env["POSTGRES_PASSWORD"], env["JWT_SECRET"]
    stack = {
        "POSTGRES_PASSWORD": pg,
        "JWT_SECRET": jwt_secret,
        "SUPABASE_ANON_KEY": env["ANON_KEY"],
        "SUPABASE_SERVICE_KEY": env["SERVICE_ROLE_KEY"],
        "GOTRUE_JWT_SECRET": jwt_secret,
        "GOTRUE_DB_DATABASE_URL": f"postgres://postgres:{pg}@{DB_HOST}/postgres?search_path=auth",
    }
    engine = {k: v for k, v in env.items()
              if k not in ("POSTGRES_PASSWORD", "JWT_SECRET", "ANON_KEY", "SERVICE_ROLE_KEY",
                           "GATEWAY_PORT", "DB_PORT")}
    engine["OLLAMA_BASE_URL"] = HOST_OLLAMA_URL
    postgrest = (
        f'db-uri = "postgres://authenticator:{pg}@{DB_HOST}/postgres"\n'
        'db-schemas = "public"\n'
        'db-anon-role = "anon"\n'
        f'jwt-secret = "{jwt_secret}"\n'
        "db-use-legacy-gucs = false\n"
        'server-host = "0.0.0.0"\n'
        "server-port = 3000\n"
    )
    return {"stack.env": _env_file(stack), "engine.env": _env_file(engine),
            "postgrest.conf": postgrest}


def main() -> int:
    state = Path(os.getenv("ENGINE_STATE_DIR", "/run/engine"))
    state.mkdir(parents=True, exist_ok=True)
    marker = state / "engine.env"
    if marker.exists():
        print(f"[setup] {state} already holds this install's secrets; keeping them")
        text = marker.read_text(encoding="utf-8")
        topped_up = gen_env.add_secret_key(text, quote=shlex.quote)
        if topped_up != text:
            marker.write_text(topped_up, encoding="utf-8")
            print("[setup] added SECRET_KEY: model keys stored in the portal are encrypted with it")
        return 0
    email = (os.getenv("ADMIN_EMAIL") or "").strip() or None
    password = (os.getenv("ADMIN_PASSWORD") or "").strip() or None
    if bool(email) != bool(password):
        print("[setup] set both ADMIN_EMAIL and ADMIN_PASSWORD, or neither. With neither, the "
              "admin account is created in the browser, on the engine's first-run page.")
        return 1
    files = render(email, password)
    # engine.env is written last: it is the marker that setup finished, so a
    # run interrupted halfway is redone instead of leaving a partial install.
    for name in ("stack.env", "postgrest.conf", "engine.env"):
        path = state / name
        path.write_text(files[name], encoding="utf-8")
        # Read by services that run as different users (postgres, kong, the
        # engine's own). The volume is mounted into those containers only.
        path.chmod(0o644)
    print(f"[setup] wrote this install's secrets to {state}")
    if email:
        print(f"[setup] admin login: {email} / (the ADMIN_PASSWORD you set)")
    else:
        print("[setup] no admin account yet, and no default login. When the stack is up, open "
              "the engine in a browser: the first-run page creates the account and asks for "
              "the setup code printed by `docker compose logs engine`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
