"""Eval runner kinds a plugin adds (admin/services/eval_runners.py).

The engine ships `chat` and `voice_live`; anything else a suite names comes
from a plugin's `eval_runners` hook. These tests use a fictional runner, so
they hold in a build with no plugin at all.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _hook_with(runners=None, scorers=None):
    module = SimpleNamespace(runners=lambda: dict(runners or {}),
                             scorers=lambda: dict(scorers or {}))
    return lambda hook: [module] if hook == "eval_runners" else []


def test_a_build_without_plugins_has_only_the_engine_runner_kinds(monkeypatch):
    from admin.services import eval_runners
    from core import plugins

    monkeypatch.setattr(plugins, "load_hook", lambda hook: [])
    assert eval_runners.runner_kinds() == ("chat", "voice_live")
    assert eval_runners.runner_for("chat") is None
    assert eval_runners.plugin_scorers() == {}


def test_a_plugin_adds_a_runner_kind_but_cannot_replace_chat(monkeypatch):
    from admin.services import eval_runners
    from core import plugins

    monkeypatch.setattr(plugins, "load_hook", _hook_with({"sample": object()}))
    assert eval_runners.runner_kinds() == ("chat", "voice_live", "sample")
    monkeypatch.setattr(plugins, "load_hook", _hook_with({"chat": object()}))
    with pytest.raises(ValueError):
        eval_runners.plugin_runners()


def test_a_plugin_runner_invokes_and_its_scorer_grades_without_a_judge(monkeypatch):
    from admin import evals
    from admin.services import eval_scorers
    from core import plugins

    calls = {}

    def invoke(**kw):
        calls["invoke"] = kw["endpoint"]
        return json.dumps({"ids": ["a"], "usage": {"input_tokens": 7, "output_tokens": 3}}), "", 200, 5

    def scorer(output, reference):
        ok = json.loads(output)["ids"] == reference["ids"]
        return {"passed": ok, "score": 1.0 if ok else 0.0, "detail": {"checked": True}}

    runner = SimpleNamespace(
        invoke=invoke, default_scorer="sample_ids",
        usage_from_output=lambda out: json.loads(out)["usage"])
    monkeypatch.setattr(plugins, "load_hook", _hook_with({"sample": runner}))
    monkeypatch.setitem(eval_scorers.SCORERS, "sample_ids", scorer)
    monkeypatch.setitem(eval_scorers.RUNNER_DEFAULT_SCORER, "sample", "sample_ids")
    monkeypatch.setattr(evals, "_invoke_endpoint",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("chat invoker used")))

    result = evals._run_scenario(
        {"id": "s1", "payload": {"catalog": {"ids": ["a"]}}, "expected_answer": "ignored"},
        profile_id="p", base_url="http://x", endpoint="/sample", judge=None,
        pass_threshold=0.7, runner_kind="sample")

    assert calls["invoke"] == "/sample"
    assert result["passed"] is True and result["score"] == 1.0
    assert result["scorer_detail"] == {"checked": True}
    assert result["judge"] is None
    assert (result["input_tokens"], result["output_tokens"]) == (7, 3)
    assert eval_scorers.runner_kind_needs_judge("sample") is False
    assert eval_scorers.runner_kind_needs_judge("chat") is True


def test_a_plugin_runner_plays_turns_and_its_reply_checks_gate_the_result(monkeypatch):
    from admin import evals
    from core import plugins

    def run_turns(scenario, turn_specs, *, metrics, **kw):
        metrics["usage"] = {"input_tokens": 1, "output_tokens": 1}
        return "transcript", "", 200, 9, ["turn 2: language"]

    runner = SimpleNamespace(run_turns=run_turns,
                             invoke=lambda **kw: (_ for _ in ()).throw(AssertionError("single turn used")))
    monkeypatch.setattr(plugins, "load_hook", _hook_with({"sample": runner}))

    result = evals._run_scenario(
        {"id": "s2", "turns": [{"question": "hi"}, {"question": "and then"}]},
        profile_id="p", base_url="http://x", endpoint="/sample", judge=None,
        pass_threshold=0.7, runner_kind="sample")

    assert result["passed"] is False
    assert result["reply_check_failures"] == ["turn 2: language"]


def test_the_suite_editor_offers_each_runner_kind_with_its_label(monkeypatch):
    from admin.services import eval_runners
    from core import plugins

    monkeypatch.setattr(plugins, "load_hook", _hook_with({"sample": SimpleNamespace(label="Sample run")}))
    options = eval_runners.runner_options()
    assert [k for k, _ in options] == ["chat", "voice_live", "sample"]
    assert options[-1] == ("sample", "Sample run")
