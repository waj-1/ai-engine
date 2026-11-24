"""The demo admin: one account every visitor signs in as.

Made at start (`ensure_demo_user`) with `core.admin_account.ensure_admin_account`,
the same function the first-run page and the headless install use, as
``demo@example.com`` (a reserved domain: no real mailbox). Its password is
never typed or shown: it is derived from this install's own secret
(``SECRET_KEY``, else the service key), so every engine process derives the
same one and a restart keeps it, without storing it anywhere. "Try the demo"
signs in with it through the auth service like the login form does, so each
click gets a normal session of its own.

Visitors cannot change the password or the account: Team & access and every
account screen are refused in demo mode (demo_mode/policy.py).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Dict

from demo_mode.settings import DEMO_EMAIL

logger = logging.getLogger(__name__)


class DemoAccountError(RuntimeError):
    pass


def demo_password() -> str:
    secret = (os.getenv("SECRET_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
              or os.getenv("SUPABASE_JWT_SECRET") or "").strip()
    if not secret:
        raise DemoAccountError("demo mode needs SECRET_KEY (or SUPABASE_SERVICE_KEY) to make the demo account")
    digest = hmac.new(secret.encode("utf-8"), b"demo-mode:demo-account:v1", hashlib.sha256).hexdigest()
    return "Dm-" + digest[:40]


def ensure_demo_user() -> Dict[str, object]:
    """Create (or repair) the demo admin. Blocking: call off the event loop."""
    from core.admin_account import RestUserRows, ensure_admin_account
    from core.auth import get_main_client

    main = get_main_client()
    return ensure_admin_account(main, RestUserRows(main), DEMO_EMAIL, demo_password(),
                                log=lambda msg: logger.debug("demo account: %s", msg))


def sign_in() -> str:
    """A fresh session's access token for the demo admin. Blocking."""
    from core.auth import sign_in_with_password

    return sign_in_with_password(DEMO_EMAIL, demo_password())["access_token"]
