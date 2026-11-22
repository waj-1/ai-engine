"""Turn an agent's markdown answer into something safe to read aloud.

The voice layer is a reader, not a summarizer (see the duplex-voice-edge
spec): it may only speak text the real agent produced. This is the
deterministic fallback used when the agent didn't author its own
`spoken_summary` — no LLM, no paraphrase, no recomputation. Anything visual
(a table, a code block) is dropped and acknowledged with a pointer to the
screen, where the authoritative rendering already is.

Lesson from a real prod eval (dd54e622, long_multi_topic_marathon_ar): an
unrounded float ("7.34394211340815 بالمية") was read aloud at fifteen
significant digits — nonsense for the caller, and a bug independent of
anything else on this path. `to_speakable` therefore rounds excess
fractional precision to 2 digits before a number is ever spoken.

This module used to ALSO mask long account identifiers (an IBAN or PAN was
spoken as "ending in 1 2 3 4 (the full number is on your screen)"). That was
removed on 2026-08-19: once a client's Live tab began mirroring the SPOKEN
transcript rather than rendering the delegate's `display` text, the pointer
was a lie — masking hid the number in BOTH channels instead of moving it
from the ear to the screen, so "what IBAN is on file?" answered with a
masked string and directed the merchant to a screen showing that same masked
string. The caller-dictated direction is unaffected: the merchant reads their
own new IBAN aloud and the agent reads it back to confirm.

What survived that deletion, and is now used for something else: the
DETECTION half. `find_identifiers` is the old `mask_identifiers`' pattern set
with the rewriting removed — the same three shapes, the same hard-won guards
against a decimal figure and a spoken list of numbers, each of which cost a
prod eval to find. Nothing here rewrites a number any more on its own
authority; `guard_identifiers` rewrites one only when its caller can prove
the digits came from somewhere other than the model (see
`core/tool_provenance.py`), and even then only to REGROUP the same digits or
to withhold ones nothing vouches for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from core.tool_provenance import digits_only

# Spoken when an answer's substance is visual (a table, a chart) and therefore
# cannot be read aloud faithfully. Per-language for the same reason
# `progress_spoken_by_language` exists: an English sentence dropped into an
# Arabic call is jarring, and for a charts-only turn this line is the ENTIRE
# spoken output, so the caller hears nothing BUT the wrong language.
_ON_SCREEN = {
    "en": "The figures are on your screen.",
    "ar": "الأرقام ظاهرة لك على الشاشة.",
}
# The English text stays importable under the old name: it is the documented
# fallback and several call sites (and tests) reference it directly.
ON_SCREEN = _ON_SCREEN["en"]


def on_screen_for(language: Optional[str] = None, *, override: Optional[str] = None) -> str:
    """The on-screen pointer for `language`, or an operator's own wording.

    falls back to English rather than saying nothing.
    """
    if override:
        return override
    primary = (language or "").split("-")[0].strip().lower()
    return _ON_SCREEN.get(primary, _ON_SCREEN["en"])

_CODE_FENCE = re.compile(r"```.*?```", re.S)
# A row is a line that STARTS with a pipe and has another one: the model
# drops the closing pipe on a table's last row (voice eval 2026-10-01,
# "| Test Stylist | 0") and the row was spoken.
_TABLE_ROW = re.compile(r"^\s*\|.*\|.*$", re.M)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
# Emphasis markers only. A lone `_` is left alone so snake_case identifiers
# survive intact.
_EMPHASIS = re.compile(r"\*\*|__|\*")
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{2,}")


# Root cause behind the same prod bug (dd54e622): the mask guard above stops
# a decimal figure being mistaken for an identifier, but the figure that
# triggered it -- "7.34394211340815 بالمية" -- should never have been SPOKEN
# at absurd float precision in the first place; no caller says fifteen
# significant digits, and reading them aloud is its own bug independent of
# masking. `_EXCESS_PRECISION` finds a decimal number with more than 2
# fractional digits and `_round_decimals_for_speech` rounds it to 2 for
# SPEECH ONLY -- this runs inside `to_speakable`, never touching the
# `display` text callers keep for the screen (see `voice/live/delegate.py`:
# separate variable than `display`). An identifier-shaped run has no decimal
# point, so it can never match this pattern -- rounding and masking guard
# different, non-overlapping shapes, which is why both are kept: rounding
# means the repro's 14-digit fractional run no longer exists to trip the
# mask guard at all, and the mask guard is what protects any caller of
_EXCESS_PRECISION = re.compile(r"(?<![\d.])(-?\d[\d,]*)\.(\d{3,})(?!\d)")


def _round_decimals_for_speech(text: str) -> str:
    def _round(match: "re.Match[str]") -> str:
        int_part, frac = match.group(1), match.group(2)
        try:
            value = float(f"{int_part.replace(',', '')}.{frac}")
        except ValueError:
            return match.group(0)
        # Reformat from the rounded float rather than truncating the
        # original strings, so a carry (".999" -> ".00" with the integer
        # part bumped) lands correctly instead of silently dropping it.
        return format(value, ",.2f" if "," in int_part else ".2f")

    return _EXCESS_PRECISION.sub(_round, text)


# ---------------------------------------------------------------------------
# Identifier detection — recovered from `mask_identifiers`, deleted 2026-08-19
# in 46bd4f3 (#133); `git show c69aef4:voice/live/speakable.py` is the last
# version that has it. The three patterns and their guards come back verbatim
# and the rewriting they used to drive does not; the only change is widening
# the lookaround character classes to Arabic-Indic/Persian digits, explained
# at the end of this comment. Each guard was written after a specific live
# failure, so read them before "simplifying" one.
# ---------------------------------------------------------------------------
#
# Two shapes, because an agent writes account numbers both ways.
#
# _PREFIXED — an IBAN-style country prefix followed by its digits, which may be
# grouped with spaces or hyphens ("SA44 2000 0001 2345 6789 1234" is the
# STANDARD human rendering, and an earlier pattern — uppercase letters
# followed by UNBROKEN digits — missed it, along with the lowercase form).
# The two letters must sit flush against the first digit, exactly as an IBAN is
# written: that is what stops an ordinary two-letter word before a long number
# ("in 1234567890...") from being read as an identifier.
#
# _DIGITS_ONLY_RUN / _DIGITS_ONLY_GROUPED — a card PAN or settlement reference
# with no letter prefix at all. The 14-digit floor sits ABOVE the longest thing
# we must not flag and at/below the shortest thing we must: a Gulf mobile number
# in full international form is ~12 digits, a card PAN is 14-19, and an IBAN's
# digit portion is 22.
#
# Length alone is NOT enough, and this is the trap a review caught: a rule of
# "14+ digits, separators allowed anywhere" matches a spoken LIST of small
# figures ("Branches did 12 14 9 22 31 8 44 17 5 3") and would suppress the
# whole list. For a payments data assistant whose job is speaking numbers,
# that is a worse bug than the one being fixed. So a separated run only counts
# as an identifier when it is grouped the way identifiers actually are:
# UNIFORM four-digit groups (the standard rendering of a PAN or an IBAN's
# digits), at least four of them. An unbroken run needs no such evidence —
# nobody writes a list without separators.
#
# Second trap, found the same way (a real prod eval, dd54e622,
# long_multi_topic_marathon_ar): a digit run is ALSO not an account number
# when it is the fractional (or integer) part of an ordinary decimal figure.
# An unrounded model output like "7.34394211340815 بالمية" ("7.[14 fractional
# digits] percent") has a 14-digit run sitting right after the decimal point,
# which satisfies "14+ digits, no letters" just as well as a real settlement
# reference — and got masked into "7.[ending in 3 4 3 9 ...] بالمية", nonsense
# read aloud on a live call. A decimal separator (ASCII '.' or Arabic '٫',
# U+066B) touching either end of the run, or a percent sign ('%'/'٪') touching
# either end, is a strong "this is a quantity, not an identifier" signal, so
# both digit-only patterns refuse to match there. (`_PREFIXED` needs no
# equivalent guard — a decimal number never carries a letter prefix.)
# `to_speakable` also rounds excessive fractional precision before this ever
# runs, which removes the 14+ digit run in the first place; this guard is the
# defense-in-depth layer for whoever calls `find_identifiers` directly.
# Lookbehind: a decimal separator or percent sign RIGHT before the run means
# the run is a fractional tail ("7.34394211340815") or is glued to a percent
# ("%34394211340815", however unlikely) -- not an identifier, block
# unconditionally. Lookahead is narrower: a bare trailing '.'/'٫' is ordinary
# sentence punctuation after a genuine account number ("Ref 40200000012345.")
# and must NOT block the match -- only a decimal separator followed by MORE
# digits (the run is the INTEGER part of a decimal) counts as the same signal.
# A trailing percent sign, like a leading one, blocks unconditionally.
#
# The character classes carry Arabic-Indic and Persian digits alongside ASCII
# because `\d` already matches them (Python's `re` is Unicode-aware for `\d`
# on str) while `0-9` in the lookarounds did not — an Arabic-numeral decimal
# would have slipped past the very guard written for an Arabic call.
_ID_ADJACENT = "A-Za-z0-9٠-٩۰-۹"
_DIGITS_ONLY_RUN = re.compile(
    rf"(?<![{_ID_ADJACENT}.٫%٪])(\d{{14,34}})(?![{_ID_ADJACENT}])(?![.٫]\d)(?![%٪])"
)
_DIGITS_ONLY_GROUPED = re.compile(
    rf"(?<![{_ID_ADJACENT}.٫%٪])(\d{{4}}(?:[ -]\d{{4}}){{3,8}})(?![{_ID_ADJACENT}])(?![.٫]\d)(?![%٪])"
)
_PREFIXED = re.compile(
    rf"(?<![{_ID_ADJACENT}])([A-Za-z]{{2}})(\d(?:[ -]?\d){{9,33}})(?![{_ID_ADJACENT}])"
)
# ...and the one shape the recovered set did not cover, added 2026-08-19 for
# the live fabrication that prompted this guard. The number the caller heard
# came out as pairs — "إس إيه 12 35 36 42 41 82 71 82 75 82 14" — and while
# THAT rendering is the persona speaking (downstream of anything here), a
# brain writing "SA 1235 3642 ..." or "SA 12 35 36 ..." is one space away
# from slipping past both `_PREFIXED` (letters must sit flush against the
# first digit) and `_DIGITS_ONLY_GROUPED` (four-digit groups only).
#
# The separated prefix is admitted ONLY for UPPERCASE letters, and that
# asymmetry is the whole safety of it: `_PREFIXED`'s flush rule exists to
# stop an ordinary two-letter word running into a long number ("in 12 34 56
# 78 90 12 34 56"), and dropping the rule wholesale would reinstate exactly
# that. An uppercase two-letter token immediately before twenty digits is an
# ISO country code, not a word. There is deliberately no equivalent for a
# BARE pair-grouped run with no prefix: uniform two-digit groups are also
# what a spoken list of small figures looks like, which is the false positive
# a review caught once already and is not worth re-opening for a shape
# nothing has been observed to emit.
_PREFIXED_SPACED = re.compile(
    rf"(?<![{_ID_ADJACENT}])([A-Z]{{2}}) (\d(?:[ -]?\d){{9,33}})(?![{_ID_ADJACENT}])"
)

# What the voice says INSTEAD of an identifier nothing in the turn vouches
# for. It asserts no fact — the same contract as `_FAILURE_SPOKEN` in the
# delegate and `ON_SCREEN` above — and it names a next step, because a
# sentence that simply stops where a number should be reads as the line
# dropping. Per-language for the reason every spoken string here is: an
# English sentence dropped into an Arabic call is heard as a second failure.
_UNVERIFIED = {
    "en": "a number I couldn't confirm — let me check it and come back to you",
    "ar": "رقم ما قدرت أتأكد منه — خلني أتحقق منه وأرجع لك",
}


def unverified_phrase_for(language: Optional[str] = None) -> str:
    primary = (language or "").split("-")[0].strip().lower()
    return _UNVERIFIED.get(primary, _UNVERIFIED["en"])


@dataclass(frozen=True)
class Identifier:
    """One account-number-shaped run found in an answer."""

    start: int
    end: int
    raw: str
    letters: str
    digits: str  # ASCII-folded, separators removed — the comparison key


def find_identifiers(text: str) -> List[Identifier]:
    """Every account-identifier-shaped run in `text`, left to right.

    Detection only. Whether a given run may be SPOKEN is a provenance
    question this module cannot answer — see `guard_identifiers`.
    """
    if not text:
        return []
    found: List[Identifier] = []
    claimed: List[Tuple[int, int]] = []

    def _claim(start: int, end: int) -> bool:
        # `_PREFIXED` runs first and wins its span; the digit-only patterns
        # must not then re-report the same digits as a second identifier.
        for s, e in claimed:
            if start < e and s < end:
                return False
        claimed.append((start, end))
        return True

    # Prefixed shapes first — they own the whole `SA44 2000 …` span, so the
    # digit-only patterns below cannot re-report the same digits as a second,
    # letterless identifier.
    for pattern in (_PREFIXED, _PREFIXED_SPACED):
        for match in pattern.finditer(text):
            if _claim(match.start(), match.end()):
                found.append(
                    Identifier(
                        start=match.start(),
                        end=match.end(),
                        raw=match.group(0),
                        letters=match.group(1),
                        digits=digits_only(match.group(2)),
                    )
                )
    for pattern in (_DIGITS_ONLY_RUN, _DIGITS_ONLY_GROUPED):
        for match in pattern.finditer(text):
            if _claim(match.start(1), match.end(1)):
                found.append(
                    Identifier(
                        start=match.start(1),
                        end=match.end(1),
                        raw=match.group(1),
                        letters="",
                        digits=digits_only(match.group(1)),
                    )
                )
    found.sort(key=lambda i: i.start)
    return found


def group_for_speech(letters: str, digits: str) -> str:
    """The one canonical spoken layout for an identifier: groups of four.

    A pure function of the characters, which is the point. The realtime model
    generates speech rather than reading a string, and left to itself it
    picks its own grouping — a live Arabic call read a 22-digit IBAN back in
    2-digit pairs where the persona specifies 4, which is both harder to
    follow and easier to lose a digit in. Grouping is not a judgement call,
    so the model does not get a vote on it.

    Grouping `letters + digits` as one string (rather than the digits alone)
    reproduces the standard IBAN rendering for free — "SA" plus the two check
    digits is itself the first group of four, giving
    `SA44 2000 0001 2345 6789 1234` — and degrades sensibly for a bare PAN.
    Never called on digits that have not already been verified, so it can
    only ever re-space a number, never change one.
    """
    body = f"{letters}{digits}"
    return " ".join(body[i:i + 4] for i in range(0, len(body), 4))


def guard_identifiers(
    text: str,
    *,
    verified: Callable[[str], bool],
    language: Optional[str] = None,
    regroup: bool = True,
) -> Tuple[str, List[str]]:
    """Withhold any identifier `verified` does not vouch for. Returns
    `(text, withheld_digit_strings)`.

    The digits rather than a count, because one turn is guarded several
    times over — each streamed sentence, the finished spoken text, the screen
    text — and the same fabricated number appearing in three of those passes
    is ONE fabrication, not three. The caller de-duplicates on these.

    `verified` takes the ASCII-folded digit string and answers whether it
    entered this turn from outside the model — a tool result, or the caller's
    own words (`core.tool_provenance.TurnProvenance.verifies`).

    The two outcomes, and why they are not symmetric:

      * VERIFIED — spoken in full, regrouped into fours. Not masked, not
        pointed at a screen: speech is the merchant's only channel, and
        "ending in 1234" is what made this layer answer "what IBAN is on
        file?" with nothing at all until masking was deleted.
      * UNVERIFIED — replaced by `unverified_phrase_for(language)`. NOT
        replaced by "the number we think you meant": choosing which of a
        turn's identifiers the model was trying to say is a guess, and a
        guess read out as an account number is the same failure with the
        engine's name on it — silently, since nobody sees a substitution
        happen. The guard fails toward not stating a number.

    `regroup=False` leaves verified runs exactly as the agent wrote them —
    used for the screen text, where the agent's own layout is fine and only
    the withholding matters.
    """
    if not text:
        return text, []
    identifiers = find_identifiers(text)
    if not identifiers:
        return text, []

    phrase = unverified_phrase_for(language)
    out = text
    withheld: List[str] = []
    # Right to left so each splice leaves earlier offsets valid.
    for ident in reversed(identifiers):
        if verified(ident.digits):
            if regroup:
                out = out[:ident.start] + group_for_speech(
                    ident.letters, ident.digits
                ) + out[ident.end:]
            continue
        withheld.append(ident.digits)
        out = out[:ident.start] + phrase + out[ident.end:]
    withheld.reverse()
    return out, withheld


def to_speakable(
    answer: str, *, max_chars: int = 700, on_screen: Optional[str] = None
) -> str:
    """Render `answer` as plain speakable text. Never adds new facts.

    `on_screen` is the already-resolved pointer for this session's language
    (see `on_screen_for`); omitted, it stays English for back-compat.
    """
    if not answer:
        return ""

    had_visual = bool(_TABLE_ROW.search(answer)) or bool(_CODE_FENCE.search(answer))
    had_bullets = bool(_BULLET.search(answer))

    text = _CODE_FENCE.sub(" ", answer)
    text = _TABLE_ROW.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)

    lines = [line.strip() for line in text.split("\n")]
    if had_bullets:
        text = "\n".join(lines).strip()
    else:
        text = " ".join(lines).strip()

    text = _round_decimals_for_speech(text)

    if had_visual:
        text = f"{text} {on_screen or ON_SCREEN}".strip()

    return _truncate(text, max_chars)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    space = cut.rfind(" ")
    # Never cut at a space that has a digit on BOTH sides -- that space is
    # very likely a thousands separator inside a space-grouped numeral
    # (common in Arabic/French formatting, e.g. "1 234 567"), not a real
    # word boundary. Cutting there keeps a truncated PREFIX ("1 234") that
    # reads as a complete, different, smaller number when spoken, instead of
    # dropping the figure cleanly. `,` is comma-grouped ("1,234,567") and has
    # no spaces to mis-cut at, so it was never at risk here. Keep walking
    # backward past any such space to find a genuine boundary.
    while (
        space > 0
        and cut[space - 1].isdigit()
        and space + 1 < len(text)
        and text[space + 1].isdigit()
    ):
        space = cut.rfind(" ", 0, space)
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "…"


# ---------------------------------------------------------------------------
# Internal ids never reach a caller
# ---------------------------------------------------------------------------
# Live 2026-09-29 (a merchant voice tenant): "You have one branch: Main Branch. Branch
# ID: ea22654d-00ca-4379-b83e-05c5e40ef45e — it's on your screen." A record
# id means nothing to a merchant, reads as noise when spoken, and a prompt
# rule against it is exactly the kind that does not hold on this path. So
# the delegate strips them STRUCTURALLY from the spoken text, every streamed
# sentence and the display (opt-in per agent: `voice_scrub_internal_ids`).
# UUID-shaped only (full, or a truncated 8-4 prefix): account numbers, TIDs
# and prices are digits, not hex-with-dashes, and are the provenance guard's
# business above.
# A full UUID, or a TRUNCATED one ("ea22654d-…") — never a plain
# digits-dash code like an invoice/reference number (20260929-0012).
_UUIDISH = (r"(?:\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
            r"|\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{1,12}(?:-[0-9a-fA-F]{1,12})*)?(?:…|\.\.\.))")
_ID_LABEL = (r"(?:(?:\b[\w\-]+\s+){0,1}(?:ID|Id|id|IDs|ids|identifier)\b\s*[:#=]?\s*"
             r"|(?:رقم\s+)?(?:المعرف|معرف|المعرّف|التعريف|الآيدي)\s*[:：]?\s*)")
_ID_RUN = re.compile(rf"(?:{_ID_LABEL})?`?{_UUIDISH}`?")
_ID_PARENS = re.compile(rf"\s*[(\[][^()\[\]]*?{_UUIDISH}[^()\[\]]*?[)\]]")
_SENTENCE = re.compile(r"[^.!?؟\n]*(?:[.!?؟]+|\n|$)")
_POINTER_ONLY = re.compile(
    r"^(?:(?:and|,|;|:)\s*)?(?:it'?s|they'?re|that'?s|which is|shown)?\s*(?:shown\s+)?"
    r"(?:on|in) (?:your|the) screen|^(?:موجود[ةه]?|ظاهر[ةه]?)?\s*(?:لك\s+)?على (?:ال)?شاشت?ك?$",
    re.I,
)


def has_internal_id(text: str) -> bool:
    return bool(text) and bool(re.search(_UUIDISH, text))


def scrub_internal_ids(text: str) -> str:
    """`text` with every UUID-shaped id (and its "Branch ID:" label) removed.

    A sentence left with nothing but an on-screen pointer or a word or two
    of debris is dropped whole. Text without an id is returned unchanged.
    """
    if not has_internal_id(text):
        return text
    out: List[str] = []
    for match in _SENTENCE.finditer(text):
        sentence = match.group(0)
        if not sentence:
            continue
        if not has_internal_id(sentence):
            out.append(sentence)
            continue
        cleaned = _ID_PARENS.sub("", sentence)
        cleaned = _ID_RUN.sub("", cleaned)
        core = re.sub(r"[\s—–\-:,;،.!?؟*`]+", " ", cleaned).strip()
        if len(core.split()) < 3 or _POINTER_ONLY.search(core):
            if sentence.endswith("\n"):
                out.append("\n")
            continue
        cleaned = re.sub(r"\s*[—–]\s*(?=[.,;،!?؟]|$)", "", cleaned)
        cleaned = re.sub(r"^\s*[—–\-:,;،]\s*", " ", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([.,;،!?؟])", r"\1", cleaned)
        out.append(cleaned)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
