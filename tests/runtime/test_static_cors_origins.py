"""Unit tests for ``core.cors.is_static_allowed_origin``.

These lock in the first-party CORS allow-list so an apex origin
(e.g. ``https://tenants.example``) can never again 400 a preflight and silently break
the landing-page sales chat, while look-alike domains stay denied.
"""

import pytest

from core.cors import is_static_allowed_origin


@pytest.mark.parametrize(
    "origin",
    [
        # Public marketing site (hosts the sales chat) — apex + subdomains.
        "https://company.example",
        "https://www.company.example",
        "https://docs.company.example",
        # Tenant origins — apex must be covered too.
        "https://tenants.example",
        "https://www.tenants.example",
        "https://tenant.tenants.example",
        # Staging origins.
        "https://staging.example",
        "https://foo.staging.example",
        # Local development (any port / scheme).
        "http://localhost",
        "http://localhost:3009",
        "http://127.0.0.1:8000",
        "https://localhost:3000",
    ],
)
def test_allowed_origins(origin: str) -> None:
    assert is_static_allowed_origin(origin) is True


@pytest.mark.parametrize(
    "origin",
    [
        # Unrelated domains.
        "https://evil.com",
        "https://example.com",
        # Look-alikes / suffix & prefix attacks must NOT match.
        "https://company.example.evil.com",
        "https://tenants.example.evil.com",
        "https://notwaj.ai",
        "https://xstaging.example",
        # company.example over plain http is not first-party-allowed (https only).
        "http://company.example",
        # Junk / empty.
        "",
        "null",
        "not-a-url",
    ],
)
def test_denied_origins(origin: str) -> None:
    assert is_static_allowed_origin(origin) is False


def test_non_string_is_denied() -> None:
    # Defensive: the middleware may hand us a non-str in odd cases.
    assert is_static_allowed_origin(None) is False  # type: ignore[arg-type]
