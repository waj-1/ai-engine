"""Eval runner kinds: the engine's own, plus any a plugin adds.

A suite's `runner_kind` says how its scenarios reach the agent and how they
are graded. The engine ships two:

  chat        the profile's SSE chat endpoint, graded by substrings, the
              LLM judge and per-turn `checks` (admin/evals.py);
  voice_live  the duplex voice edge, with a simulated caller.

A plugin adds its own through the "eval_runners" hook:
`runners() -> {kind: runner}` and `scorers() -> {assert_type: scorer}`.
A runner is any object; every attribute is optional:

  invoke(*, base_url, endpoint, scenario, run_context, user_token,
         prompt_override, model_override, profile_version_id, metrics,
         environment) -> (output, trace, http_status, latency_ms)
      replaces the chat request for one scenario;
  run_turns(scenario, turn_specs, *, metrics, **invoke_kwargs)
      -> (transcript, trace, http_status, latency_ms, reply_check_failures)
      plays a scenario's `turns` as one conversation;
  after_run(run_context, *, since, run_id)
      runs once when the run's attempts are done (cleanup), never raises;
  usage_from_output(output) -> {input_tokens, output_tokens} | None
      token usage read from a non-SSE response body;
  default_scorer: str
      the `scorers()` key that grades the kind's scenarios when a scenario
      names no `assert.type`. A kind with a default scorer needs no judge.
  label: str
      how the suite editor's "How cases are run" picker names the kind.

A scorer is `scorer(output, reference) -> {passed, score, detail}`, where
`reference` is the scenario's `payload["catalog"]` (or `catalog`).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

ENGINE_RUNNER_KINDS: Tuple[str, ...] = ("chat", "voice_live")


def _merged(fn: str) -> Dict[str, Any]:
    from core.plugins import call_hook, load_hook

    out: Dict[str, Any] = {}
    for found in call_hook(load_hook("eval_runners"), fn):
        for key, value in found.items():
            if key in ENGINE_RUNNER_KINDS and fn == "runners":
                raise ValueError(f"a plugin may not replace the engine's {key!r} runner")
            out.setdefault(key, value)
    return out


def plugin_runners() -> Dict[str, Any]:
    """{runner_kind: runner} from the plugins."""
    return _merged("runners")


def plugin_scorers() -> Dict[str, Callable[..., Dict[str, Any]]]:
    """{assert_type: scorer} from the plugins."""
    return _merged("scorers")


def runner_kinds() -> Tuple[str, ...]:
    """Every runner_kind a suite may name in this build."""
    return ENGINE_RUNNER_KINDS + tuple(k for k in plugin_runners() if k not in ENGINE_RUNNER_KINDS)


_ENGINE_LABELS = {
    "chat": "Chat — the agent answers in text (default)",
    "voice_live": "Voice call — a simulated caller talks to the voice agent",
}


def runner_options() -> list:
    """[(runner_kind, label)] for the suite editor, engine kinds first."""
    runners = plugin_runners()
    return [(kind, _ENGINE_LABELS.get(kind) or getattr(runners.get(kind), "label", "") or kind)
            for kind in runner_kinds()]


def runner_for(kind: Optional[str]) -> Optional[Any]:
    """The plugin runner for `kind`; None for the engine's own kinds."""
    return plugin_runners().get(kind or "chat")
