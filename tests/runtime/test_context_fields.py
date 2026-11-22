"""Tests for runtime/tools/_context.py — context_fields injection helpers."""

import logging


def test_resolve_context_ref_ctx_and_literal():
    from runtime.tools._context import resolve_context_ref

    ctx = {"user_token": "tok-1", "store_ids": ["s1", "s2"]}
    assert resolve_context_ref("$ctx.user_token", ctx) == "tok-1"
    assert resolve_context_ref("$ctx.store_ids", ctx) == ["s1", "s2"]
    assert resolve_context_ref("literal-value", ctx) == "literal-value"
    assert resolve_context_ref(42, ctx) == 42
    assert resolve_context_ref("$ctx.missing", ctx) is None


def test_effective_context_fields_precedence_tool_wins():
    from runtime.tools._context import effective_context_fields

    server = {"token": "$ctx.user_token", "businessId": "$ctx.business_id"}
    member = {"businessId": "$ctx.member_biz"}
    tool = {"token": "$ctx.tool_token"}
    merged = effective_context_fields(server, member, tool)
    assert merged == {"token": "$ctx.tool_token", "businessId": "$ctx.member_biz"}
    assert effective_context_fields(server, None, None) == server
    assert effective_context_fields(None, None, None) == {}


def test_strip_context_args_removes_props_and_required():
    from runtime.tools._context import strip_context_args

    schema = {
        "type": "object",
        "properties": {
            "token": {"type": "string"},
            "fromDate": {"type": "string"},
        },
        "required": ["token", "fromDate"],
    }
    stripped = strip_context_args(schema, {"token": "$ctx.user_token"})
    assert "token" not in stripped["properties"]
    assert stripped["required"] == ["fromDate"]
    # input untouched (copy semantics)
    assert "token" in schema["properties"]


def test_build_context_kwargs_omits_missing_ctx_keys():
    from runtime.tools._context import build_context_kwargs

    cf = {"token": "$ctx.user_token", "businessId": "$ctx.business_id", "mode": "strict"}
    ctx = {"user_token": "tok-1"}  # business_id missing
    kwargs = build_context_kwargs(cf, ctx)
    assert kwargs == {"token": "tok-1", "mode": "strict"}


def test_strip_context_args_returns_copy_when_empty():
    from runtime.tools._context import strip_context_args

    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    }
    result = strip_context_args(schema, {})
    # even with no context_fields, the result is an independent copy
    assert result is not schema
    assert result == schema
    # mutating the result must not touch the input
    result["properties"].pop("a")
    assert "a" in schema["properties"]


def test_build_context_kwargs_warns_on_missing_ctx_key(caplog):
    from runtime.tools._context import build_context_kwargs

    with caplog.at_level(logging.WARNING, logger="runtime.tools._context"):
        kwargs = build_context_kwargs({"token": "$ctx.user_token"}, {})
    assert kwargs == {}
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)
