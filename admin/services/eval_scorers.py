"""Pluggable eval scorers keyed by suite runner_kind / scenario assert type,
and the deterministic reply checks.

`chat` suites keep the substring+judge path in admin/evals.py. A non-chat
scorer comes from a plugin's eval runner (admin/services/eval_runners.py):
it grades a response against the scenario's reference data on its own, so
the suite needs no LLM judge.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from admin.services.eval_runners import plugin_runners, plugin_scorers

# registry: scenario.assert.type (or a runner_kind's default, see below) ->
# scorer(response_text, reference) -> result. `_run_scenario` resolves
# through this dict, so a new non-chat scorer only needs a plugin entry (and
# a runner `default_scorer` if it should apply suite-wide without every
# scenario repeating `assert.type`).
SCORERS: Dict[str, Callable[..., Dict[str, Any]]] = plugin_scorers()

# suite.runner_kind -> default scorer key when a scenario doesn't declare its
# own `assert.type`. 'chat' has no entry here — it resolves to no scorer, so
# chat suites always take the legacy substring/judge path in admin/evals.py.
RUNNER_DEFAULT_SCORER: Dict[str, str] = {
    kind: runner.default_scorer
    for kind, runner in plugin_runners().items()
    if getattr(runner, "default_scorer", None)
}


# -- Deterministic reply checks ----------------------------------------------
# A scenario's optional `reply_checks` dict is a HARD gate applied after the
# judge/substring verdict (like expected_tools): things code can check exactly,
# so a judge's opinion can't override them: the reply's script (did it
# answer in the customer's language?) and emoji use.
#
#   {"language": "ar" | "en",   # share of the reply's LETTERS in that script
#    "max_emoji": 0,            # emoji allowed in the reply
#    "must_not_contain": [...]} # case-insensitive fragments that must be absent

_ARABIC_LETTER_RANGES = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                         (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
# An "ar" reply may still name a service in Latin letters ("Classic Haircut");
# an "en" reply may quote one Arabic word back. So the rule is a share of the
# letters, not "no letters of the other script at all".
_MIN_SHARE_FOR_LANGUAGE = {"ar": 0.5, "en": 0.9}


def _in_ranges(cp: int, ranges) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def count_emoji(text: str) -> int:
    """Emoji in `text`, counted the way a person sees them: the same detector
    the agents use on customer messages (common.emoji_names), so a flag
    (🇸🇦), a keycap (1️⃣), a joined family (👨‍👩‍👧) or a skin tone each count
    once, and emoji outside a few hand-picked blocks (⏰) are not missed."""
    from common.emoji_names import emoji_in

    return len(emoji_in(text))


def script_shares(text: str) -> Dict[str, float]:
    """Share of the alphabetic characters that are Arabic script ("ar") and
    plain ASCII letters ("en"). Every letter counts toward the total, so a
    reply in another script (Cyrillic, Greek, accented Latin) lowers both
    shares instead of being left out of the sum."""
    arabic = latin = total = 0
    for ch in text or "":
        if not ch.isalpha():
            continue
        total += 1
        if _in_ranges(ord(ch), _ARABIC_LETTER_RANGES):
            arabic += 1
        elif ch.isascii():
            latin += 1
    if not total:
        return {"ar": 0.0, "en": 0.0}
    return {"ar": arabic / total, "en": latin / total}


def reply_check_failures(text: str, checks: Dict[str, Any]) -> List[str]:
    """Human-readable reasons `text` fails `checks`; [] when it passes."""
    failures: List[str] = []
    if not isinstance(checks, dict) or not checks:
        return failures
    language = checks.get("language")
    if language in _MIN_SHARE_FOR_LANGUAGE:
        share = script_shares(text).get(language, 0.0)
        if share < _MIN_SHARE_FOR_LANGUAGE[language]:
            failures.append(
                f"language: expected a reply in {language!r}, "
                f"but only {share:.0%} of its letters are in that script")
    max_emoji = checks.get("max_emoji")
    if isinstance(max_emoji, int):
        found = count_emoji(text)
        if found > max_emoji:
            failures.append(f"emoji: {found} in the reply, at most {max_emoji} allowed")
    lowered = (text or "").lower()
    for fragment in checks.get("must_not_contain") or []:
        if fragment and str(fragment).lower() in lowered:
            failures.append(f"must_not_contain: the reply contains {fragment!r}")
    return failures


def runner_kind_needs_judge(runner_kind: str) -> bool:
    """Whether a suite of this runner_kind requires an LLM evaluator model.

    A runner kind with a built-in, deterministic scorer (i.e. it has a
    RUNNER_DEFAULT_SCORER entry, supplied by a plugin runner) needs NO
    judge: it decides pass/score itself.
    Every other kind (`chat`, legacy, unknown) is judged, so the comparison /
    bake-off pre-flight must still demand an evaluator_model_id for those —
    preserving today's behavior. This is the single source of truth for
    "does this suite need a judge", consulted by both the bake-off pre-flight
    (admin/services/bakeoffs.py) and start_suite_run's comparison guard
    (admin/services/evals.py)."""
    return (runner_kind or "chat") not in RUNNER_DEFAULT_SCORER


_SELF_GRADING_FIELDS = ("checks", "expected_tools", "forbidden_tools", "expected_substrings")
_JUDGED_FIELDS = ("expected_answer", "expected", "rubric")


def suite_needs_judge(suite: Dict[str, Any]) -> bool:
    """Whether a comparison run of this suite needs an evaluator model.

    A runner kind with a built-in scorer never does (runner_kind_needs_judge).
    Neither does a chat suite whose every case is graded by facts alone
    (`checks`, tool assertions, required substrings) and asks for no judged
    answer: a judge would have nothing to grade, yet a model run-off of such a
    suite was refused until one was set (2026-11-22). A suite with no cases,
    or with one case that needs judging, still does.
    """
    if not runner_kind_needs_judge((suite or {}).get("runner_kind") or "chat"):
        return False
    scenarios = (suite or {}).get("scenarios") or []
    if not scenarios:
        return True
    for sc in scenarios:
        if not isinstance(sc, dict) or any(sc.get(f) for f in _JUDGED_FIELDS):
            return True
        turns = [t for t in (sc.get("turns") or []) if isinstance(t, dict)]
        if any(t.get(f) for t in turns for f in _JUDGED_FIELDS):
            return True
        graded = any(sc.get(f) for f in _SELF_GRADING_FIELDS) \
            or any(t.get("checks") for t in turns)
        if not graded:
            return True
    return False


# How the comparison pre-flights (start_suite_run, bake-offs) describe a
# usable judge. Any model the engine can call builds one
# (admin/e2e_runner._build_judge): Gemini, Claude, OpenAI or an
# OpenAI-compatible base URL, Azure OpenAI, a local Ollama model.
JUDGE_MODEL_HINT = (
    "Pick one under the suite's Settings → Judge model (/admin/evals/suites/<suite>); "
    "any model this engine can call works as a judge, local models included"
)


def suite_judge_problem(client: Any, suite: Dict[str, Any]) -> Optional[str]:
    """Why a comparison run of this suite can't be graded by a judge, or None.

    None when the suite needs no judge (`suite_needs_judge`), or its judge is
    a model this engine can build. Otherwise a short reason: no judge set,
    or the judge row's provider is one the engine cannot call. A missing key is not checked here (that is the operator's install,
    not the suite): such a run says per case why it was not judged.
    """
    if not suite_needs_judge(suite or {}):
        return None
    model_id = (suite or {}).get("evaluator_model_id")
    if not model_id:
        return "no judge model is set"
    try:
        rows = (client.table("agent_models").select("model_id, name, provider, model_string")
                .eq("model_id", model_id).limit(1).execute().data or [])
    except Exception:
        return None  # can't look it up here; the run itself reports what happens
    if not rows:
        return None  # not found here; the run itself reports what happens
    row = rows[0]
    from runtime.llm_factory import SUPPORTED_PROVIDERS

    provider = str(row.get("provider") or "google_genai").lower()
    if provider not in SUPPORTED_PROVIDERS and provider != "auto":
        return (f"its judge model {row.get('name') or row.get('model_string') or model_id} "
                f"uses the provider {provider!r}, which this engine cannot call")
    return None
