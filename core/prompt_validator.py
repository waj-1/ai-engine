"""Prompt-safety validator — the single source of truth for "is this prompt
safe to serve?"

WHY THIS EXISTS

`admin_mcp/tools_configure.py::prompts` already has the publishing path
(`create_version` / `serve` / `publish_from_repo`), but nothing on that path
checked a prompt before it became the version a live stack answers callers
with. On 2026-08-19 that gap mattered: publishing a repo persona
constant onto a client demo stack re-introduced a known defect — wording that made
the realtime model SPEAK the `ask_agent` tool call as text instead of
invoking it (`tool_calls=0`, no answer ever delivered) — and it was caught
only because a character count looked wrong afterwards, not because anything
refused the publish.

This module is that check. It holds the checks themselves; the rules they
apply to a NAMED prompt are data, supplied by the deployment's plugin
(`<plugin>.prompt_rules`, see core/plugins.py), because they are about that
deployment's own prompts:

  * Size ceilings — `size_budgets()`: prompt name -> (ceiling in characters,
    what it costs the caller). Latency budgets, ratcheted down as a prompt
    shrinks.
  * Bilingual parity — `bilingual_rules()` (a rule both language halves must
    assert), `forbidden_phrases()` (a reversed instruction that must never
    reappear) and `count_parity_prompt_names()` (prompts whose numbered rules
    must count the same in each language — the generative check, which fires
    with no pattern written for a new rule).

They read as module constants (`SIZE_BUDGETS`, `BILINGUAL_RULES`,
`FORBIDDEN_PHRASES`, `COUNT_PARITY_PROMPT_NAMES`,
`BILINGUAL_PARITY_PROMPT_NAMES`), loaded on first use. A build with no plugin
has none, so only the universal rule below runs.

NEW IN THIS FILE: the imperative-call rule (`check_ask_agent_imperative`).
Nothing pinned it before. CLAUDE.md records the exact incident: rewording
"you must **call** the `ask_agent` tool" to "goes to the `ask_agent` tool"
made a live realtime model narrate the tool call as speech instead of
invoking it — an imperative and a description are not interchangeable to a
realtime model, and this was discovered by manual probing, not by any test.

DESIGN

`validate_prompt(text, name=None) -> List[Problem]` is the entry point.
Problems are STRUCTURED (severity, rule, message), never a bare bool — a
caller decides what to do with an "error" (refuse), a "warning" (surface,
don't block) or an "info" (visibility only). Every check function is a pure
string-in, problems-out function: no network, no database, no Supabase
client — this module must be importable standalone, including from a test
process with no AI project reachable at all.

"Never fail wrong" governs two edges deliberately:
  * A check that finds nothing to say about a prompt (unknown name, no
    `ask_agent` mention) returns no problems — silence is not the same as
    "verified safe", and `validate_prompt` says so via an "info" problem
    when NO registered rule set applies to `name` at all.
  * An unexpected exception inside a check must not crash the caller (a
    validator bug should not become an outage), but it also must not make
    the prompt look validated — `validate_prompt` catches broadly and
    reports a "warning"-severity `validator_error` problem instead of
    either raising or silently returning an empty (looks-clean) list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Severity = str  # "error" | "warning" | "info" — plain strings, JSON-friendly


@dataclass(frozen=True)
class Problem:
    """One thing the validator found. `error` blocks a publish/serve/create
    unless a human explicitly overrides it; `warning` and `info` never
    block — they are reported so a human can look, not silently dropped."""

    severity: Severity
    rule: str
    message: str
    prompt: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity, "rule": self.rule,
                "message": self.message, "prompt": self.prompt}


def has_errors(problems: List[Problem]) -> bool:
    return any(p.severity == "error" for p in problems)


def to_dicts(problems: List[Problem]) -> List[Dict[str, Any]]:
    return [p.to_dict() for p in problems]


# ---------------------------------------------------------------------------
# Rule: size ceilings (latency budgets)
#
# name -> (ceiling in characters, what it costs the caller), from the
# plugin's `size_budgets()`. RATCHET THESE DOWN when a prompt shrinks — a
# ceiling left at the old size is not a budget, it is permission to grow back.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SizeBudget:
    ceiling: int
    cost: str


def _rules(fn: str) -> list:
    """Every plugin's `prompt_rules.<fn>()` result, in plugin order."""
    from core.plugins import call_hook, load_hook

    return call_hook(load_hook("prompt_rules"), fn)


_REGISTRY: Dict[str, Any] = {}


def _registry() -> Dict[str, Any]:
    """The rule registries, read from the plugins on first use (not at
    import: a plugin's rule module imports this one for its dataclasses)."""
    if not _REGISTRY:
        sizes: Dict[str, SizeBudget] = {}
        for found in _rules("size_budgets"):
            for key, budget in found.items():
                sizes.setdefault(key, budget)
        bilingual = tuple(r for found in _rules("bilingual_rules") for r in found)
        forbidden = tuple(f for found in _rules("forbidden_phrases") for f in found)
        counted = tuple(n for found in _rules("count_parity_prompt_names") for n in found)
        by_prompt: Dict[str, List[BilingualRule]] = {}
        for rule in bilingual:
            by_prompt.setdefault(rule.prompt, []).append(rule)
        forbidden_by_prompt: Dict[str, List[ForbiddenPhrase]] = {}
        for phrase in forbidden:
            forbidden_by_prompt.setdefault(phrase.prompt, []).append(phrase)
        _REGISTRY.update(
            SIZE_BUDGETS=sizes,
            BILINGUAL_RULES=bilingual,
            FORBIDDEN_PHRASES=forbidden,
            COUNT_PARITY_PROMPT_NAMES=counted,
            _BILINGUAL_RULES_BY_PROMPT=by_prompt,
            _FORBIDDEN_BY_PROMPT=forbidden_by_prompt,
            BILINGUAL_PARITY_PROMPT_NAMES=tuple(sorted(
                set(by_prompt) | set(forbidden_by_prompt) | set(counted))),
        )
    return _REGISTRY


def __getattr__(name: str) -> Any:
    # SIZE_BUDGETS, BILINGUAL_RULES, FORBIDDEN_PHRASES, COUNT_PARITY_PROMPT_NAMES
    # and BILINGUAL_PARITY_PROMPT_NAMES read as module constants.
    reg = _registry()
    if name in reg:
        return reg[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def check_size_budget(name: Optional[str], text: str) -> List[Problem]:
    if not name:
        return []
    budget = _registry()["SIZE_BUDGETS"].get(name)
    if budget is None:
        return []
    if len(text) > budget.ceiling:
        return [Problem(
            "error", "size_budget",
            f"{name}: {len(text):,} chars > {budget.ceiling:,} ceiling "
            f"(costs {budget.cost}). The fix is almost never a bigger "
            f"ceiling — look for the rule you are about to add stated "
            f"somewhere else already, and for prose that EXPLAINS a rule "
            f"rather than giving it.",
            prompt=name,
        )]
    return []


# ---------------------------------------------------------------------------
# Rule: bilingual parity (registered rule pairs, forbidden reverted phrases,
# numbered-rule-count parity)
#
# The registered pairs and phrases come from the plugin. The shape they
# guard against: an edit that rewrites one language half of a prompt and
# leaves the other asserting the opposite.
# ---------------------------------------------------------------------------

# Arabic block (U+0600-U+06FF) — enough to classify a line's language here.
_ARABIC = re.compile(r"[؀-ۿ]")
# A markdown-ish numbered rule: "1. Something". Sub-items are indented
# further and intentionally excluded; both languages indent them the same
# way.
_NUMBERED = re.compile(r"^ {0,3}\d+\.\s+\S")


@dataclass(frozen=True)
class BilingualRule:
    """One rule that both language halves of a prompt must assert."""

    rule_id: str
    prompt: str
    en: Tuple[str, ...]
    ar: Tuple[str, ...]
    why: str


@dataclass(frozen=True)
class ForbiddenPhrase:
    """An instruction that was REVERSED and must never reappear."""

    phrase: str
    prompt: str
    why: str





def _normalized_ws(text: str) -> str:
    """Whitespace-collapsed view for forbidden-phrase matching.

    Prompt text hard-wraps across source lines (and an operator's DB edit can
    re-wrap it anywhere), so an exact-substring test can never match a phrase
    that happens to wrap mid-phrase — the persona's demonstrated device
    question wrapped as "...what exactly is it\\nshowing or doing?" at the
    time it was registered, which an exact check would have declared absent
    from the very text it was written to forbid. Matching on
    `" ".join(text.split())` (the same normalization the prompt
    scenario tests use) hardens every registered
    phrase at once."""
    return " ".join(text.split())


def _numbered_items_by_language(prompt_text: str) -> Tuple[int, int]:
    """(english_count, arabic_count) of top-level numbered rules."""
    english = arabic = 0
    for line in prompt_text.splitlines():
        if not _NUMBERED.match(line):
            continue
        if _ARABIC.search(line):
            arabic += 1
        else:
            english += 1
    return english, arabic


def check_bilingual_parity(name: Optional[str], text: str) -> List[Problem]:
    if not name:
        return []
    problems: List[Problem] = []
    reg = _registry()
    for rule in reg["_BILINGUAL_RULES_BY_PROMPT"].get(name, ()):
        for needle in rule.en:
            if needle not in text:
                problems.append(Problem(
                    "error", "bilingual_missing_half",
                    f"{rule.rule_id}: the ENGLISH half is missing from "
                    f"{name} — {needle!r}. {rule.why}",
                    prompt=name,
                ))
        for needle in rule.ar:
            if needle not in text:
                problems.append(Problem(
                    "error", "bilingual_missing_half",
                    f"{rule.rule_id}: the ARABIC half is missing from "
                    f"{name} — {needle!r}. An edit that rewrites one "
                    f"language and not the other leaves the prompt "
                    f"asserting two different rules at once. {rule.why}",
                    prompt=name,
                ))
    # Whitespace-normalized on BOTH sides: prompts hard-wrap across source
    # lines and DB edits re-wrap freely, so an exact-substring test misses a
    # phrase that wraps mid-phrase (see _normalized_ws).
    normalized_text = _normalized_ws(text)
    for forbidden in reg["_FORBIDDEN_BY_PROMPT"].get(name, ()):
        if _normalized_ws(forbidden.phrase) in normalized_text:
            problems.append(Problem(
                "error", "bilingual_forbidden_phrase",
                f"{name} has regrown a reverted instruction: "
                f"{forbidden.phrase!r}. Why it was removed: {forbidden.why}",
                prompt=name,
            ))
    if name in reg["COUNT_PARITY_PROMPT_NAMES"]:
        english, arabic = _numbered_items_by_language(text)
        if not (english and arabic):
            problems.append(Problem(
                "error", "bilingual_numbered_count",
                f"{name}: expected numbered rules in both languages, got "
                f"EN={english} AR={arabic} — the prompt shape changed enough "
                f"that the numbered-rule parity check can no longer read it.",
                prompt=name,
            ))
        elif english != arabic:
            problems.append(Problem(
                "error", "bilingual_numbered_count",
                f"{name} enumerates {english} numbered rules in English but "
                f"{arabic} in Arabic. One language gained or lost a rule on "
                f"its own — the prompt now asserts different things "
                f"depending on the caller's language.",
                prompt=name,
            ))
    return problems


def has_registered_rules(name: Optional[str]) -> bool:
    """Whether `name` has ANY size-budget or bilingual-parity rule
    registered — i.e. whether `validate_prompt` checks anything beyond the
    universal `ask_agent` rule for this name. Exposed so a caller (the
    drift check) can distinguish "differs from the repo constant, but this
    module actually validated it" from "differs from the repo constant, and
    nothing here can confirm it's safe" — the latter must not be reported
    as clean."""
    reg = _registry()
    return bool(name) and (name in reg["SIZE_BUDGETS"] or name in reg["BILINGUAL_PARITY_PROMPT_NAMES"])


# ---------------------------------------------------------------------------
# Rule: `ask_agent` dispatch instructions must be imperative
#
# NEW — the reason this module exists. CLAUDE.md records the incident:
# rewording "you must **call** the `ask_agent` tool" to "goes to the
# `ask_agent` tool" made a live realtime model narrate the tool call as
# TEXT instead of invoking it (`tool_calls=0`, no answer ever delivered).
# Applies to ANY prompt that mentions `ask_agent` at all — this rule is
# content-shaped, not indexed by a specific named repo constant, so it is
# the one check `validate_prompt` always runs regardless of `name`.
# ---------------------------------------------------------------------------

_ASK_AGENT = "ask_agent"
_WINDOW = 90  # chars either side of one `ask_agent` occurrence to search
_SENTENCE_BOUNDARY = re.compile(r"[.!?\n]")

# "call", "calls", "called" — the imperative verb CLAUDE.md pins. Deliberately
# NOT "-ing": "Calling it is silent machinery" (real repo text, describing
# the ACT of calling) uses the gerund one sentence after the actual
# instruction, and would otherwise launder a genuinely descriptive-only
# instruction into looking imperative. استدع matches استدعِ / استدعاء (the
# Arabic imperative "invoke!" used the same way in Arabic voice prompts, e.g.
# "استدعِ `ask_agent` فوراً").
_IMPERATIVE_CALL = re.compile(r"\bcall(?:s|ed)?\b|استدع", re.IGNORECASE)

# The regression shape: describing ask_agent as a destination the request
# arrives at, rather than instructing the model to invoke it.
_DESCRIPTIVE_DISPATCH = re.compile(
    r"\b(?:goes?|going|is\s+sent|are\s+sent|sends?|sending|routes?|routing|"
    r"reaches?|reaching)\b\s+(?:to\s+)?(?:the\s+)?`?ask_agent`?",
    re.IGNORECASE,
)


def _sentence_around(text: str, idx: int) -> str:
    """The sentence containing the character at `idx` — from just after the
    nearest preceding '.', '!', '?' or newline (searched up to `_WINDOW`
    chars back) to just before the next one (searched up to `_WINDOW` chars
    forward). Bounded on both sides so neither a distant unrelated "call"
    nor a distant unrelated "goes to" can supply evidence about a SPECIFIC
    `ask_agent` mention, and so the NEXT sentence — e.g. a gerund aside like
    "Calling it is silent machinery" describing the act in the abstract,
    not instructing this call — never counts as evidence for THIS one."""
    lo_bound = max(0, idx - _WINDOW)
    hi_bound = min(len(text), idx + len(_ASK_AGENT) + _WINDOW)
    before = text[lo_bound:idx]
    last_boundary = None
    for last_boundary in _SENTENCE_BOUNDARY.finditer(before):
        pass
    start = lo_bound + (last_boundary.end() if last_boundary else 0)
    after = text[idx:hi_bound]
    next_boundary = _SENTENCE_BOUNDARY.search(after)
    end = idx + (next_boundary.start() if next_boundary else len(after))
    return text[start:end]


def _sentences_around_ask_agent(text: str) -> List[str]:
    """One sentence per literal `ask_agent` occurrence, independent of every
    other occurrence — a prompt can legitimately mention `ask_agent` many
    times (once to instruct the call, elsewhere to describe its `spoken`
    result), and judging the whole prompt as one blob would let an unrelated
    sentence elsewhere launder a genuinely descriptive-only instruction."""
    sentences = []
    start = 0
    while True:
        idx = text.find(_ASK_AGENT, start)
        if idx == -1:
            break
        sentences.append(_sentence_around(text, idx))
        start = idx + len(_ASK_AGENT)
    return sentences


def check_ask_agent_imperative(name: Optional[str], text: str) -> List[Problem]:
    """Any prompt that tells the model to dispatch a question to `ask_agent`
    must phrase that as an imperative — "call the `ask_agent` tool" /
    `استدعِ` — never only as a description of where the request goes.
    N/A (no problems) when the prompt never mentions `ask_agent` at all."""
    if _ASK_AGENT not in text:
        return []
    sentences = _sentences_around_ask_agent(text)
    any_imperative = any(_IMPERATIVE_CALL.search(s) for s in sentences)
    label = name or "(unnamed prompt)"
    if not any_imperative:
        return [Problem(
            "error", "ask_agent_imperative_missing",
            f"{label}: mentions `ask_agent` but no sentence imperatively "
            f"instructs the model to CALL it (English 'call', Arabic "
            f"'استدعِ'). This is the exact shape of a known live regression: "
            f"rewording 'you must call the `ask_agent` tool' into a "
            f"description of where the request goes made a realtime model "
            f"SPEAK the tool call as text instead of invoking it "
            f"(tool_calls=0, no answer ever delivered).",
            prompt=name,
        )]
    descriptive_hits = [
        s.strip() for s in sentences
        if _DESCRIPTIVE_DISPATCH.search(s) and not _IMPERATIVE_CALL.search(s)
    ]
    if descriptive_hits:
        return [Problem(
            "warning", "ask_agent_descriptive_present",
            f"{label}: an imperative call instruction exists, but this "
            f"prompt ALSO describes dispatching to `ask_agent` in passive/"
            f"descriptive terms near {descriptive_hits[0]!r} — the exact "
            f"phrasing shape a past regression replaced the imperative "
            f"with entirely. Not blocking (an imperative instruction is "
            f"still present), but worth wording clear of it.",
            prompt=name,
        )]
    return []


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def validate_prompt(text: str, name: Optional[str] = None) -> List[Problem]:
    """Is `text` (the CANDIDATE content of the prompt named `name`, not
    necessarily anything already written to a database) safe to serve?

    `name` should be the canonical constant name (e.g.
    "SUPPORT_SYSTEM_PROMPT") when the caller knows it — `publish_from_repo`
    always does, because it resolves one from a dotted `constant` path.
    `create_version` and `serve` in admin_mcp accept arbitrary content with
    no such provenance, so they call this with `name=None`: the universal
    `ask_agent` rule still runs (it is content-shaped, not name-indexed),
    but the size/bilingual-parity registries — which are indexed by name —
    do not, and `validate_prompt` says so via an "info" problem rather than
    silently reporting a clean bill of health.
    """
    problems: List[Problem] = []
    try:
        problems.extend(check_ask_agent_imperative(name, text))
        if name:
            problems.extend(check_size_budget(name, text))
            problems.extend(check_bilingual_parity(name, text))
            if not has_registered_rules(name):
                problems.append(Problem(
                    "info", "no_registered_rules",
                    f"no size-budget or bilingual-parity rules are "
                    f"registered for {name!r} — only the universal "
                    f"ask_agent check ran.",
                    prompt=name,
                ))
        else:
            problems.append(Problem(
                "info", "no_prompt_name",
                "no prompt name supplied — only the universal ask_agent "
                "check ran; size and bilingual-parity rules are indexed by "
                "name and were skipped.",
                prompt=None,
            ))
    except Exception as exc:  # noqa: BLE001 — never fail wrong: a validator
        # bug must not silently pass a publish (that would be worse than no
        # validator at all), but it also must not hard-crash the caller.
        problems.append(Problem(
            "warning", "validator_error",
            f"prompt validator raised {type(exc).__name__}: {exc} — treat "
            f"this prompt as UNVALIDATED, not as safe-by-default.",
            prompt=name,
        ))
    return problems
