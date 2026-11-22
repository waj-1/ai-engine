"""`X-WAJ-Profile-Version` lets a caller pin which `agent_profile_versions`
snapshot serves a request (used by the eval runner to compare profile
versions). Like the model/prompt overrides, it must be operator-gated —
`_request_is_operator` is the same fail-safe check covered by
`tests/test_override_auth_gate.py`.

These tests call `profile_version_override_middleware` directly (rather than
through a TestClient) with a stubbed `_request_is_operator`, mirroring the
brief's approach. Tests are `async def` so pytest-asyncio (asyncio_mode=auto,
see pytest.ini) awaits the middleware coroutine directly rather than reaching
for the deprecated `asyncio.get_event_loop().run_until_complete(...)`.
"""
from unittest.mock import patch

from starlette.requests import Request

import main
from core.profile_version_resolver import clear_profile_version, get_profile_version


async def test_middleware_sets_version_for_operator():
    clear_profile_version()
    captured = {}

    async def call_next(_req):
        captured["v"] = get_profile_version()

        class R:  # minimal response
            pass

        return R()

    scope = {"type": "http", "headers": [(b"x-waj-profile-version", b"ver-9")], "method": "GET", "path": "/"}
    req = Request(scope)
    with patch.object(main, "_request_is_operator", return_value=True):
        await main.profile_version_override_middleware(req, call_next)
    assert captured["v"] == "ver-9"
    # Cleared after the response returned.
    assert get_profile_version() is None


async def test_middleware_ignores_non_operator():
    clear_profile_version()
    captured = {}

    async def call_next(_req):
        captured["v"] = get_profile_version()

        class R:
            pass

        return R()

    scope = {"type": "http", "headers": [(b"x-waj-profile-version", b"ver-9")], "method": "GET", "path": "/"}
    req = Request(scope)
    with patch.object(main, "_request_is_operator", return_value=False):
        await main.profile_version_override_middleware(req, call_next)
    assert captured["v"] is None
