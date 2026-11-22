"""Create (or refresh) a local dev superuser so the admin portal login works.

`supabase db reset` wipes auth.users + public.user, which leaves the admin
portal with no account to sign in as (email/password 401s; a token would
403 since require_superuser checks permission->>'is_superuser'). Run this
after a reset to restore a working login.

    python scripts/create_dev_superuser.py
    python scripts/create_dev_superuser.py --email me@company.example --password 'pw'

It is idempotent: if the auth user already exists it just (re)asserts the
password + the superuser flag. Uses the MAIN project's SERVICE key (admin
API) against whatever SUPABASE_URL points at (auth + public.user live in
the MAIN project) — intended for LOCAL dev only.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

DEFAULT_EMAIL = "admin@waj.local"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=os.getenv("DEV_SUPERUSER_EMAIL", DEFAULT_EMAIL))
    ap.add_argument("--password", default=os.getenv("DEV_SUPERUSER_PASSWORD"))
    args = ap.parse_args()

    if not args.password:
        print("refusing: a password is required. Pass --password or set DEV_SUPERUSER_PASSWORD.")
        return 2

    env = os.getenv("ENVIRONMENT", "").lower()
    if env not in ("development", "dev", ""):
        print(f"refusing: ENVIRONMENT={env!r} is not a dev environment. This script is dev-only.")
        return 2

    url = os.getenv("SUPABASE_URL", "")
    if "127.0.0.1" not in url and "localhost" not in url:
        print(f"refusing: SUPABASE_URL={url!r} is not local. This script is dev-only.")
        return 2

    from core.auth import get_main_client

    client = get_main_client()

    # 1. Create or find the GoTrue auth user (email pre-confirmed).
    auth_id = None
    try:
        created = client.auth.admin.create_user({
            "email": args.email,
            "password": args.password,
            "email_confirm": True,
        })
        auth_id = created.user.id
        print(f"created auth user {args.email} ({auth_id})")
    except Exception as exc:
        # Already exists — find it and reset the password so the operator
        # knows the credential.
        print(f"create_user said: {exc}; looking up existing user…")
        page = client.auth.admin.list_users()
        users = page if isinstance(page, list) else getattr(page, "users", [])
        for u in users:
            if getattr(u, "email", None) == args.email:
                auth_id = u.id
                break
        if not auth_id:
            print("could not create or find the auth user")
            return 1
        client.auth.admin.update_user_by_id(auth_id, {"password": args.password})
        print(f"reset password for existing auth user {args.email} ({auth_id})")

    # 2. Upsert the public.user row with the superuser flag, linked by auth_id.
    existing = (
        client.schema("public").table("user").select("id").eq("auth_id", auth_id).limit(1).execute().data
    )
    payload = {
        "auth_id": auth_id,
        "email": args.email,
        "permission": {"is_superuser": True},
        "role": "admin",
    }
    if existing:
        client.schema("public").table("user").update(payload).eq("auth_id", auth_id).execute()
        print("updated public.user superuser row")
    else:
        client.schema("public").table("user").insert(payload).execute()
        print("inserted public.user superuser row")

    print(f"\n✅ Sign in at /admin/login with:\n   email:    {args.email}\n   password: {args.password}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
