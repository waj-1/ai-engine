"""core.prompt_validator applies the rules a plugin registers for a named
prompt (hook "prompt_rules"). Fictional prompt names and rules, so these
hold in a build with no plugin."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.prompt_validator as v

_PROMPT = "SAMPLE_PROMPT"


@pytest.fixture
def rules(monkeypatch):
    from core import plugins

    module = SimpleNamespace(
        size_budgets=lambda: {_PROMPT: v.SizeBudget(200, "time to first word")},
        bilingual_rules=lambda: (v.BilingualRule(
            rule_id="sample-greets-once", prompt=_PROMPT,
            en=("Greet once",), ar=("سلّم مرة",), why="a re-greeting test"),),
        forbidden_phrases=lambda: (v.ForbiddenPhrase(
            phrase="Always greet twice", prompt=_PROMPT, why="reversed"),),
        count_parity_prompt_names=lambda: (_PROMPT,),
    )
    monkeypatch.setattr(plugins, "load_hook", lambda hook: [module] if hook == "prompt_rules" else [])
    monkeypatch.setattr(v, "_REGISTRY", {})
    yield
    v._REGISTRY.clear()


_GOOD = "1. Greet once.\n2. Answer.\n١. سلّم مرة.\n٢. أجب.\n"


def _rule_ids(text):
    return sorted({p.rule for p in v.validate_prompt(text, _PROMPT) if p.severity == "error"})


def test_a_prompt_that_keeps_its_registered_rules_passes(rules):
    assert _rule_ids(_GOOD) == []
    assert v.has_registered_rules(_PROMPT)
    assert v.SIZE_BUDGETS[_PROMPT].ceiling == 200


def test_each_registered_rule_fails_its_own_breach(rules):
    assert _rule_ids(_GOOD + "x" * 300) == ["size_budget"]
    assert _rule_ids(_GOOD.replace("سلّم مرة", "مرحبا")) == ["bilingual_missing_half"]
    assert _rule_ids(_GOOD + "Always greet twice") == ["bilingual_forbidden_phrase"]
    assert _rule_ids(_GOOD + "3. Close.\n") == ["bilingual_numbered_count"]


def test_without_a_plugin_only_the_universal_rule_runs(monkeypatch):
    from core import plugins

    monkeypatch.setattr(plugins, "load_hook", lambda hook: [])
    monkeypatch.setattr(v, "_REGISTRY", {})
    try:
        assert v.SIZE_BUDGETS == {} and v.BILINGUAL_RULES == ()
        problems = v.validate_prompt("x" * 50_000, _PROMPT)
        assert [p.rule for p in problems] == ["no_registered_rules"]
        assert v.validate_prompt("This goes to the `ask_agent` tool.", _PROMPT)[0].rule \
            == "ask_agent_imperative_missing"
    finally:
        v._REGISTRY.clear()
