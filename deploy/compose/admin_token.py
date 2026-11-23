#!/usr/bin/env python3
"""Print an access token for this install's admin account.

    docker compose exec -T engine sh deploy/compose/with_env.sh python deploy/compose/admin_token.py

Signs in to the auth service with the admin email and password kept in the
state volume, and prints the token: what `/v1/agents/*` and `/mcp` accept in
`Authorization: Bearer …`. It expires after an hour; run it again for a new one.

Only for an install started with ADMIN_EMAIL and ADMIN_PASSWORD. An install
whose admin was created on the first-run page keeps no password here: use an
API key (Settings > API keys) instead.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    try:
        base, anon = os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"]
        body = {"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}
    except KeyError as exc:
        if exc.args[0] in ("ADMIN_EMAIL", "ADMIN_PASSWORD"):
            print("this install keeps no admin password (the account was created on the "
                  "first-run page). Use an API key instead: Settings > API keys in the portal.",
                  file=sys.stderr)
        else:
            print(f"{exc.args[0]} is not set: run this through deploy/compose/with_env.sh", file=sys.stderr)
        return 1
    req = urllib.request.Request(
        f"{base}/auth/v1/token?grant_type=password", data=json.dumps(body).encode(),
        headers={"apikey": anon, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(json.load(resp)["access_token"])
    except Exception as exc:  # noqa: BLE001 — say what the auth service answered
        print(f"could not sign in as {body['email']}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
