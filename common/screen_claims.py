"""Claims that something is "on your screen" — one pattern for every reader.

User voice test 2026-09-30: three business data answers said «موجودة على
شاشتك» ("it's on your screen") while the screen showed nothing but the same
prose. Three places read such a claim and must agree on what one is:

* the voice delegate drops an unbacked claim from what is spoken and shown
  (`voice/live/delegate.py`, `voice_screen_claims_need_backing`);
* an agent's brain may treat a tool-free draft that makes one as a stall and
  re-ask it;
* the eval check `screen_backed` grades it (`admin/services/eval_checks.py`).

They used to carry three regexes that had drifted apart (review I6, 2026-10-01).
"""
from __future__ import annotations

import re
from typing import List, Tuple

# EN: "on (your|the) screen", "on screen", "shown on screen", "check/see/look
# at your screen". AR: على/في + (ال)شاشة/شاشتك, and the attached forms
# بشاشتك، بالشاشة، عالشاشة، فالشاشة (with ة or ه).
SCREEN_CLAIM = re.compile(
    r"\bon (?:your |the )?screens?\b"
    r"|\b(?:check|see|look at|have a look at) (?:your|the) screen\b"
    # Letters only after the noun: «،» (U+060C) is in the Arabic block too.
    r"|(?:(?:على|في)\s+(?:ال)?|بال|ب|عال|فال)شاش(?:ة|ه|تك|تكم)(?![\u0621-\u064A])",
    re.I,
)

# A claim that points at a CARD (review and tap Confirm) rather than at data.
# A card made earlier in the call backs such a claim; it does not back an
# unrelated data claim later in the call.
CARD_REFERENCE = re.compile(
    r"\bconfirm\b|\bcard\b|\bproposal\b|تأكيد|تاكيد|أكد|اكد|البطاقة|بطاقة|الكرت|كرت|الاقتراح|اقتراح",
    re.I,
)

# Clause boundaries a claim is cut at: sentence ends, a spaced dash, a line
# break, and a comma (",␠" — never the thousands separator in "2,149") or
# the Arabic «،». Captured, so the text around a dropped clause keeps its
# own punctuation and line breaks.
_CLAUSE_SEP = re.compile(r"((?<=[.!?؟])\s+|\s+[—–]\s+|\s*\n+\s*|,\s+|\s*،\s*)")
_TERMINAL = ".!?؟"
_COMMA_SEP = re.compile(r"^\s*[,،]\s*$")


def mentions_screen(text: str) -> bool:
    return bool(text) and bool(SCREEN_CLAIM.search(text))


def _join(parts: List[str]) -> str:
    return "".join(parts).strip()


def _drop_phrase(text: str) -> str:
    """`text` with only the claim phrase(s) removed (spacing tidied)."""
    out = SCREEN_CLAIM.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([.!?؟,،])", r"\1", out)
    return out.strip()


def drop_screen_claims(text: str, *, allow_empty: bool = False) -> Tuple[str, int]:
    """(`text` without its on-screen clauses, how many were dropped).

    Only the clause that carries the claim goes: "You have 10 bookings today,
    the details are on your screen." keeps "You have 10 bookings today.". Line
    breaks and the other clauses' punctuation are kept as they were.

    Never empties an answer (unless `allow_empty`, for a caller that speaks
    a pointer after it): when every clause carries the claim, only the claim
    phrase itself is removed; when even that leaves nothing with a letter or
    digit in it, the text is returned unchanged — a harmless pointer beats
    turning a valid answer into the failure line.
    """
    if not text or not SCREEN_CLAIM.search(text):
        return text, 0
    pieces = _CLAUSE_SEP.split(text)
    # pieces = [clause, sep, clause, sep, …, clause]
    clauses = pieces[0::2]
    seps = pieces[1::2] + [""]
    claim = [bool(SCREEN_CLAIM.search(c)) for c in clauses]
    dropped = sum(1 for c, is_claim in zip(clauses, claim) if is_claim and c.strip())
    if not dropped:
        return text, 0

    out: List[str] = []
    for i, (clause, sep) in enumerate(zip(clauses, seps)):
        if not claim[i]:
            out.extend([clause, sep])
            continue
        # The claim clause and the separator after it go. When it was joined
        # to the clause before by a comma, that comma goes instead, and the
        # previous clause takes over the claim's sentence end.
        if out and _COMMA_SEP.match(out[-1]):
            out.pop()
            end = clause.rstrip()[-1:]
            out.append((end if end and end in _TERMINAL else "") + sep)
    result = _join(out)
    # A dangling separator left at the end ("…today،").
    result = re.sub(r"[\s,،—–]+$", "", result)
    if result and text.rstrip()[-1:] in _TERMINAL and result[-1] not in _TERMINAL:
        result += text.rstrip()[-1]
    elif result and result[-1] not in _TERMINAL and not result.endswith("|"):
        result += "."

    if re.search(r"\w", result):
        return result, dropped
    if allow_empty:
        return "", dropped
    phrase_only = _drop_phrase(text)
    if re.search(r"\w{2,}", phrase_only) and len(phrase_only.split()) >= 2:
        return phrase_only, dropped
    return text, 0


# ---------------------------------------------------------------------------
# The realtime PERSONA's own words (user voice test 2026-10-01, 13:31): asked
# «هاتي سجلات الفواتير» ("bring the invoice records") she called no tool and
# said «الحين تشوف سجلات الفواتير … قدامك», then «الأرقام موجودة على الشاشة»,
# then «ممكن تحتاج تسوي تحديث للصفحة» — four turns, no ask_agent. Every guard
# above runs on the brain's answer; a persona turn with no tool call has no
# answer to guard. These patterns let the endpoint treat such a turn as a
# stall: it claims data or a result it never fetched, or blames the client.
# ---------------------------------------------------------------------------
_AR_LETTER = r"\u0621-\u064A"
PERSONA_RESULT_CLAIM = re.compile(
    # Screen claims (the shared pattern), "here they are", updated/sent,
    # refresh-the-page. NOT the generic "here's the …", "you can see …" or
    # "all set": a closing, a direction or a "here's the thing" use them
    # (re-review R-C1).
    SCREEN_CLAIM.pattern
    + r"|\bhere (?:they|it) (?:are|is)\b|\bhere (?:are|is) your\b"
    r"|\b(?:it'?s|they'?re|that'?s) (?:been )?(?:updated|sent|loaded)\b"
    r"|\brefresh(?:ing)? (?:the |your )?(?:page|screen|app)\b|\breload(?:ing)? (?:the |your )?(?:page|app)\b"
    rf"|(?<![{_AR_LETTER}])(?:الحين|هلأ|هلق|هسا|هسه|الآن|توا)\s+(?:\S+\s+){{0,2}}?(?:تشوف|بتشوف|راح تشوف|تقدر تشوف|ظاهر|ظاهرة|طالع|طالعة)"
    rf"|(?<![{_AR_LETTER}])(?:قدامك|أمامك|امامك)(?![{_AR_LETTER}])"
    rf"|(?:تحديث|حدّث|حدث|حدثي|حدّثي)\s+(?:لل|ال)?(?:صفحة|الصفحة|التطبيق|الشاشة)"
    rf"|(?<![{_AR_LETTER}])(?:تم التحديث|تم الإرسال|انعمل|اتعمل|انرسل)(?![{_AR_LETTER}])",
    re.I,
)

# A clause that is not a claim even with claim words in it:
# - directions to a place in the app or on a device ("under the Reports tab",
#   «من قائمة التقارير», "your terminal will show the receipt on the screen");
# - thanks and closings ("You're welcome … have a great day").
_NOT_A_CLAIM = re.compile(
    r"\b(?:under|in|from|on|via) (?:the |your )?[\w ]{0,25}(?:tab|menu|page|section|settings|app's)\b"
    r"|\b(?:terminal|device|machine|printer|receipt|pos)\b|\byou'?re welcome\b|\bhave a (?:great|good|nice)\b"
    r"|\bgoodbye\b|\bbye\b|\bthank"
    rf"|(?<![{_AR_LETTER}])(?:من|في|تحت|ب)\s*(?:ال)?(?:قائمة|صفحة|تبويب|إعدادات|اعدادات|الإعدادات|الاعدادات|قسم|تطبيق)"
    rf"|(?<![{_AR_LETTER}])(?:الجهاز|جهاز|التيرمنال|الطرفية|الإيصال|الايصال|العفو|يسلمو|مع السلامة|بالتوفيق)(?![{_AR_LETTER}])",
    re.I)

# The caller saying the SCREEN is empty (re-review R-C1: «ما في شي ثاني» and
# «لا ما في شي، شكراً» are the answers to "anything else?", and "I don't see
# a difference" is a comparison — none of them may arm a re-fetch).
# Either a not-seeing verb tied to the screen/display, or a short, bare
# «مش شايف إشي» / «فيش إشي» with nothing else in it.
_AR_NOT_SEEN = r"(?:مش|ما|مو|مب)\s*(?:شايف|شايفة|شايفه|أشوف|اشوف|بشوف|ظاهر|ظاهرة|ظاهرين|طالع|طالعة|بيبين|مبين)"
NOT_SEEING = re.compile(
    rf"(?<![{_AR_LETTER}]){_AR_NOT_SEEN}[^.؟?!]{{0,25}}(?:على|ع|في|ب)\s*(?:ال)?شاش"
    rf"|(?<![{_AR_LETTER}])(?:فيش|مافيش|ما فيش|ما في|مافي)\s*(?:إشي|اشي|شي|شيء|حاجة)\s*(?:على|ع|في|ب)\s*(?:ال)?شاش"
    r"|\b(?:I )?(?:don'?t|do not|can'?t|cannot) see (?:anything|it|them|that|a thing|nothing)"
    r"(?: (?:on|in) (?:the|my) (?:screen|app|display)| on screen)\b"
    r"|\bnothing (?:is )?(?:on|showing on|shows on) (?:the |my )?(?:screen|display|app)\b"
    r"|\b(?:the|my) (?:screen|display) is (?:empty|blank)\b",
    re.I,
)
_BARE_NOT_SEEN = re.compile(
    rf"^\W*(?:لا\W+)?(?:{_AR_NOT_SEEN}|فيش|مافيش|ما فيش)\s*(?:إشي|اشي|شي|حاجة|ولا إشي|ولا اشي)?\W*$", re.I)

# A farewell or thanks from the CALLER never arms the persona-claim tier
# (tier 1/2 keep the same posture through their lexical and topic gates).
FAREWELL = re.compile(
    r"\b(?:thanks|thank you|bye|goodbye|that'?s all|nothing else|no more|have a)\b"
    rf"|(?<![{_AR_LETTER}])(?:شكرا|شكراً|مشكور|مشكورة|يسلمو|يسلموا|تسلم|تسلمي|يعطيك العافية|الله يعطيك"
    rf"|مع السلامة|باي|بس هيك|خلص|خلاص|ثاني|تاني|غيره|غير هيك)(?![{_AR_LETTER}])",
    re.I)


# A clause that ASKS the caller about their screen is not a claim: "Which
# terminal is it, and what's showing on the screen?".
_ASKS = re.compile(
    r"\b(?:what|which|where|is it|are they|do you|does it|can you see|could you|anything)\b"
    rf"|(?<![{_AR_LETTER}])(?:شو|وش|ايش|إيش|أي|اي|هل|وين|كيف|شايف|شايفة|بتشوف\s*\S*\s*[؟?])(?![{_AR_LETTER}])",
    re.I)


def persona_claims_a_result(text: str, *, card_shown: bool = False) -> bool:
    """A persona turn that claims data, a screen or an update it never
    fetched, or blames the client. Clause by clause; a clause that asks, gives
    directions, thanks, or (with a card already shown) points at that card is
    not a claim."""
    if not text or not PERSONA_RESULT_CLAIM.search(text) or _NOT_A_CLAIM.search(text) and _is_closing(text):
        return False
    clauses = _CLAUSE_SEP.split(text)[0::2]
    for c in clauses:
        if not PERSONA_RESULT_CLAIM.search(c) or _ASKS.search(c) or _NOT_A_CLAIM.search(c):
            continue
        if card_shown and CARD_REFERENCE.search(c):
            continue
        return True
    return False


def _is_closing(text: str) -> bool:
    return bool(re.search(r"\byou'?re welcome\b|\bhave a (?:great|good|nice)\b|\bbye\b|العفو|مع السلامة", text, re.I))


def caller_sees_nothing(text: str) -> bool:
    """The caller says the screen is empty — never a farewell or comparison."""
    if not text or FAREWELL.search(text):
        return False
    return bool(NOT_SEEING.search(text) or _BARE_NOT_SEEN.search(text.strip()))


def is_farewell(text: str) -> bool:
    return bool(text) and bool(FAREWELL.search(text))
