#!/usr/bin/env python3
"""Mint a long-lived superuser JWT for the AI-engine admin MCP server.

The token is HS256-signed with SUPABASE_JWT_SECRET (the main Supabase
project's JWT secret) and carries token_use=mcp so mint-script tokens are
identifiable in logs. It passes admin/MCP auth for the user whose auth_id
is given — that user must have permission->>is_superuser = true.

Usage:
    SUPABASE_JWT_SECRET=... python scripts/mint_mcp_token.py \
        --auth-id <auth.users id> --email admin@company.example --days 90

Put the output in your shell env as WAJ_AI_ENGINE_MCP_TOKEN (referenced by
the repo .mcp.json). Revoke by flipping is_superuser off or rotating the
project JWT secret.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

import jwt


def mint_token(*, sub: str, email: str = "", days: int = 90,
               secret: Optional[str] = None) -> str:
    secret = secret or os.getenv("SUPABASE_JWT_SECRET")
    if not secret:
        print("SUPABASE_JWT_SECRET is not set", file=sys.stderr)
        raise SystemExit(2)
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": email,
        "role": "authenticated",
        "aud": "authenticated",
        "iat": now,
        "exp": now + days * 86400,
        "token_use": "mcp",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-id", required=True,
                        help="auth.users id (the user row's auth_id) to act as")
    parser.add_argument("--email", default="", help="email claim (for audit readability)")
    parser.add_argument("--days", type=int, default=90, help="token lifetime in days")
    args = parser.parse_args()
    print(mint_token(sub=args.auth_id, email=args.email, days=args.days))


if __name__ == "__main__":
    main()
