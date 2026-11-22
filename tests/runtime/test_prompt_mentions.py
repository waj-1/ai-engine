"""PromptResolver expands @[type:name] mention tokens to `name`; inert otherwise."""
from core.prompt_resolver import _expand_mentions


def test_expands_tool_mcp_kb_tokens():
    out = _expand_mentions("Always call @[tool:get_sales_summary] then @[mcp:Sales Docs]; see @[kb:Onboarding].")
    assert out == "Always call `get_sales_summary` then `Sales Docs`; see `Onboarding`."


def test_token_free_content_unchanged():
    s = "Plain prompt with {locale} and a JSON example {\"a\": 1} and an @ sign."
    assert _expand_mentions(s) == s


def test_malformed_tokens_left_literal():
    for s in ["@[", "@[tool:]", "@[foo:bar]", "@[tool]", "email@[example]"]:
        # none are valid @[tool|mcp|kb:<nonempty>] tokens → unchanged
        assert _expand_mentions(s) == s


def test_resolve_applies_expansion(monkeypatch):
    from core.prompt_resolver import PromptResolver
    # fallback path (no version id) → returns fallback, then expands.
    out = PromptResolver.resolve(None, fallback="Use @[tool:get_clients].")
    assert out == "Use `get_clients`."
