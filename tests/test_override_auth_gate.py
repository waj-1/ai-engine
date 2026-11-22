"""The `X-WAJ-Model-Override` / `X-WAJ-Prompt-Override` headers let a caller
swap the LLM model or pin a prompt version. That power is operator-only (the
admin eval runner sends a verified superuser JWT). `_request_is_operator` is the
gate; it must fail safe (False) for anonymous / invalid / non-superuser callers
so public chat endpoints can't be steered to an arbitrary/expensive model.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi import HTTPException

import main


class _Req:
    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


def test_anonymous_request_is_not_operator():
    assert main._request_is_operator(_Req()) is False


def test_bearer_that_resolves_superuser_is_operator():
    with patch("admin.auth.resolve_superuser", return_value={"user_id": "u", "email": "a@b"}):
        assert main._request_is_operator(_Req(headers={"Authorization": "Bearer good"})) is True


def test_bearer_that_fails_superuser_is_not_operator():
    with patch("admin.auth.resolve_superuser", side_effect=HTTPException(status_code=403, detail="no")):
        assert main._request_is_operator(_Req(headers={"Authorization": "Bearer notsuper"})) is False


def test_cookie_token_is_considered():
    with patch("admin.auth.resolve_superuser", return_value={"user_id": "u"}) as rs:
        assert main._request_is_operator(_Req(cookies={"waj_admin_token": "ck"})) is True
        rs.assert_called_once_with("ck")


def test_lookup_error_fails_safe():
    with patch("admin.auth.resolve_superuser", side_effect=RuntimeError("db down")):
        assert main._request_is_operator(_Req(headers={"Authorization": "Bearer x"})) is False
