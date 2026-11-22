"""Where a number the model is about to say actually came from.

One turn, two questions, and only the second one is hard:

  * did a TOOL return these digits in this turn? — then they are as good as
    the system of record, and the voice may read them out whole;
  * did the CALLER say these digits? — then they are his own, and reading
    them back is the confirmation step, not a claim.

Anything else the model utters at account-number length it made up, and on a
payments call that is the most damaging thing this product can produce (live,
2026-08-19: an Arabic caller asked for the IBAN on file and heard twenty-two
invented digits, matching neither the seeded value nor the one he had just
dictated). `voice/live/speakable.py` decides what LOOKS like an identifier;
this module decides where it CAME FROM.

Why a ContextVar rather than a channel on the agent
---------------------------------------------------
`flow_events` — the delegate's other per-turn channel — is a list hung on the
agent instance and threaded into one client demo's tool factory closure context.
That works because exactly one agent needed it. Tool RESULTS are needed from
every agent that can be a voice brain, and those agents do not share a tool
loop: some hand-roll one, others run LangGraph's `create_react_agent` and
read `on_tool_end`. A ContextVar recorder lets each
of them report from wherever its results actually materialise, in one line,
with no plumbing through signatures that do not otherwise need it.

It is also, deliberately, **inert unless someone installs a recorder**. The
text chat path never opens one, so `record_tool_result` there is a dict
lookup and a return: no allocation, no copying of tool payloads, no cost.
`voice/live/delegate.py` opens exactly one per delegated turn.

Task/context mechanics, since this is the part that silently breaks: a
ContextVar `set()` inside a coroutine mutates the context of the *task* that
is running it, and `asyncio.create_task` / `asyncio.gather` / `wait_for` copy
that context into the child. Copying copies the REFERENCE, so a tool invoked
inside a `gather` mutates the same `TurnProvenance` the delegate is holding.
Two concurrent calls in one process are two different tasks with two
different contexts and cannot see each other's corpus. Sequential turns on
one connection share a context, which is why `turn_provenance()` resets the
var in a `finally` rather than leaving the last turn's tool results lying
around to verify the next turn's fabrication.
"""
from __future__ import annotations

import contextlib
import re
import logging
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

# Arabic-Indic (U+0660..) and Extended Arabic-Indic / Persian (U+06F0..)
# digits fold onto ASCII before anything is compared. `mock_mcp.tools`
# `verify_otp` does the same translation and explains the cost of not doing
# it: a correct OTP read back in Arabic numerals compared unequal and burned
# an attempt (live, 2026-07-31), desyncing the whole change flow. An IBAN is
# the same trap with a worse consequence — a tool result and an Arabic
# answer can each pick a different numeral set for the same number.
_DIGIT_FOLD = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# Per-turn caps. A tool can return a megabyte of rows; the corpus only ever
# needs to answer "does this ~20-digit run appear", so bound the number of
# blobs kept, the digits kept per blob, and — because this runs on the
# latency-critical voice path, where past ~8s the realtime service has
# already discarded the tool call — the number of characters SCANNED per
# result. Overflow drops the OLDEST tool result rather than the newest: an
# answer normally quotes what the last tool returned.
#
# The scan cap is set far above any payload a model could quote from: the
# LLM only ever sees the result AFTER `truncate_tool_result`, so digits
# beyond this point were never in its context to copy. Erring high anyway,
# because the failure mode of scanning too little is a CORRECT number
# looking fabricated, and that is the regression that would take the guard
# straight back out.
_MAX_BLOBS = 64
_MAX_SCAN_CHARS = 500_000
_MAX_DIGITS_PER_BLOB = 40_000
_MAX_CALLER_DIGITS = 4_000


def digits_only(text: Any) -> str:
    """Every digit in `text`, in order, folded to ASCII. Nothing else.

    This is the ONLY comparison key. Formatting is not provenance: a tool
    returning `SA44 2000 0001 2345 6789 1234` and an answer writing
    `SA4420000001234567891234` are the same number, and an agent that
    hyphenates, or an Arabic session that switches numeral sets, must not
    look like fabrication.
    """
    if text is None:
        return ""
    return "".join(
        ch for ch in str(text).translate(_DIGIT_FOLD) if "0" <= ch <= "9"
    )


class TurnProvenance:
    """The digits that entered ONE turn from outside the model.

    Two corpora, joined differently, because they answer different questions.

    `tool_blobs` — one entry per tool result, kept separate. A tool returns
    an identifier contiguously inside its own payload; there is no reason to
    let a run splice across two results, and keeping them apart means it
    cannot.

    `caller_digits` — every digit the caller uttered this session, in order,
    concatenated into ONE run. That is not sloppiness, it is the requirement:
    a merchant dictating an IBAN pauses between groups, each pause arrives as
    a separate message (see the IBAN collection block in
    `runtime/prompts/voice.py`), and the 22-digit read-back he is owed is
    therefore a contiguous run of digits he said in order but never in one
    message. Substring-against-the-joined-run is exactly "the caller uttered
    these digits, in this order".

    What is deliberately NOT provenance: the ASSISTANT's own previous
    messages. Admitting them would launder a fabrication — say a wrong IBAN
    once and every later turn can cite the transcript as its source.
    """

    __slots__ = ("_tool_blobs", "_caller", "_caller_len", "tool_results", "tool_names")

    def __init__(self) -> None:
        self._tool_blobs: List[str] = []
        self._caller: List[str] = []
        self._caller_len = 0
        # Count of results recorded, INCLUDING ones carrying no digits. The
        # difference between "no tool ran" and "a tool ran and returned no
        # number" is worth having in a log line when a suppression is being
        # explained after the fact.
        self.tool_results = 0
        # The tools that ran this turn, in call order (Team Connect's audience
        # floor reads which data an answer was built from).
        self.tool_names: List[str] = []

    # -- write side ------------------------------------------------------

    def record_tool_result(self, payload: Any) -> None:
        self.tool_results += 1
        blob = digits_only(str(payload)[:_MAX_SCAN_CHARS])[:_MAX_DIGITS_PER_BLOB]
        if not blob:
            return
        self._tool_blobs.append(blob)
        if len(self._tool_blobs) > _MAX_BLOBS:
            del self._tool_blobs[0]

    def record_caller_text(self, text: Any) -> None:
        chunk = digits_only(text)
        if not chunk:
            return
        self._caller.append(chunk)
        self._caller_len += len(chunk)
        while self._caller_len > _MAX_CALLER_DIGITS and len(self._caller) > 1:
            self._caller_len -= len(self._caller.pop(0))

    # -- read side -------------------------------------------------------

    @property
    def caller_blob(self) -> str:
        return "".join(self._caller)

    def source_of(self, digits: str) -> Optional[str]:
        """`"tool"`, `"caller"`, or None — None meaning "the model made it up".

        Empty input is not verifiable and never claimed to be; callers guard
        the length floor before asking.
        """
        if not digits:
            return None
        for blob in self._tool_blobs:
            if digits in blob:
                return "tool"
        if digits in self.caller_blob:
            return "caller"
        return None

    def verifies(self, digits: str) -> bool:
        return self.source_of(digits) is not None


_CURRENT: ContextVar[Optional[TurnProvenance]] = ContextVar(
    "waj_turn_provenance", default=None
)


def current() -> Optional[TurnProvenance]:
    """The recorder for the turn running on this task, if any."""
    return _CURRENT.get()


def record_tool_result(payload: Any, *, tool_name: str = "") -> None:
    """Report one tool's raw result as provenance for the current turn.

    Called from every agent's tool loop, right where the result string is
    built. A no-op with no recorder installed (i.e. every text-chat turn),
    and it NEVER raises: provenance is a safety net, and a net that can drop
    a live call is worse than the risk it covers.
    """
    prov = _CURRENT.get()
    if prov is None:
        return
    try:
        prov.record_tool_result(payload)
        if tool_name:
            prov.tool_names.append(tool_name)
    except Exception:  # pragma: no cover - defensive
        logger.debug("tool provenance: failed to record %s", tool_name, exc_info=True)


def record_caller_text(text: Any) -> None:
    """Report text the CALLER authored (their message, their history rows).

    Ordering matters — see `TurnProvenance` — so call this oldest-first.
    """
    prov = _CURRENT.get()
    if prov is None:
        return
    try:
        prov.record_caller_text(text)
    except Exception:  # pragma: no cover - defensive
        logger.debug("tool provenance: failed to record caller text", exc_info=True)


@contextlib.contextmanager
def turn_provenance() -> Iterator[TurnProvenance]:
    """Install a fresh recorder for one turn and tear it down afterwards.

    The reset is load-bearing, not hygiene: several turns of one call run on
    the same task and therefore the same context, so a recorder left
    installed would let turn N's tool results vouch for turn N+1's numbers —
    which is precisely the "recalling an IBAN from earlier in the call"
    failure the brain prompt already forbids in words.
    """
    prov = TurnProvenance()
    token = _CURRENT.set(prov)
    try:
        yield prov
    finally:
        _CURRENT.reset(token)


# ---------------------------------------------------------------------------
# Business FIGURES: money, percentages, counts
# ---------------------------------------------------------------------------
# The identifier guard above covers account-number-length runs only. On
# 2026-09-28 a voice brain (gpt-5-mini) answered "Today's revenue
# is AED 4,320 … up compared with the same weekday last week" with NO tool
# call in the turn, for a business with zero bookings. A figure is short, so
# it cannot be vouched for digit-by-digit the way an IBAN can (the brain
# rounds, sums and converts legitimately); what CAN be checked structurally is
# the premise: a turn that called no tool has no data to quote. `find_figures`
# picks out the business-figure shapes, and the voice delegate / business
# agent refuse to speak one from a turn with zero tool results.
#
# Only shapes that assert a MEASUREMENT are matched — a number glued to a
# currency, a percent sign, or a count noun — so a clarifying question that
# repeats the caller's "1 to 2 pm" or a date is never touched. Numbers the
# caller said in this very message are exempt (echoing his own "set it to 150
# AED" asserts nothing). Spelled-out numbers ("four thousand") are not caught.
_NUM = r"[0-9٠-٩۰-۹](?:[0-9٠-٩۰-۹,٬٫.]*[0-9٠-٩۰-۹])?"
_CURRENCY = (
    r"(?:AED|SAR|USD|EUR|GBP|QAR|KWD|BHD|OMR|EGP|JOD|dhs?|dirhams?|riyals?|dinars?|"
    r"dollars?|pounds?|\$|€|£|د\.إ|ر\.س|درهم|دراهم|ريال|ريالات|دينار|جنيه|دولار)"
)
_COUNT_NOUN = (
    r"(?:bookings?|appointments?|clients?|customers?|visits?|sales|orders?|invoices?|"
    r"transactions?|guests?|members?|leads?|no-?shows?|cancell?ations?|services? sold|"
    r"حجز|حجوزات|موعد|مواعيد|عميل|عملاء|زبون|زباين|زبائن|زيارة|زيارات|فاتورة|فواتير|"
    r"طلب|طلبات|عملية|عمليات|إلغاء|إلغاءات)"
)
_FIGURE_PATTERNS = (
    # AED 4,320 / $120 / د.إ 90
    re.compile(rf"(?<![\w.]){_CURRENCY}\s?({_NUM})", re.I),
    # 4,320 AED / 4320 درهم
    re.compile(rf"(?<![\w.,])({_NUM})\s?{_CURRENCY}(?!\w)", re.I),
    # 12% / 12 percent / ١٢٪ / 12 بالمية
    re.compile(
        rf"(?<![\w.,])({_NUM})\s?(?:%|٪|percent\b|per cent\b|بالمية|بالمئة|بالمائة|في المية|في المئة)",
        re.I,
    ),
    # 3 bookings / 14 new clients / ٥ حجوزات
    re.compile(rf"(?<![\w.,])({_NUM})\s(?:\w+\s)?{_COUNT_NOUN}(?!\w)", re.I),
)


def find_figures(text: Any) -> List[str]:
    """Digits (ASCII-folded) of each business figure stated in `text`."""
    if not text:
        return []
    found: List[str] = []
    for pattern in _FIGURE_PATTERNS:
        for match in pattern.finditer(str(text)):
            digits = digits_only(match.group(1))
            if digits:
                found.append(digits)
    return found


def caller_numbers(text: Any) -> set:
    """Each number the caller said, as folded digits — the exemption set."""
    return {digits_only(m.group(0)) for m in re.finditer(_NUM, str(text or ""))}


def unsourced_figures(text: Any, *, tool_results: int, caller_text: Any = "",
                      vouched: Any = ()) -> List[str]:
    """Figures in `text` that nothing in the turn can have produced.

    Non-empty only when the turn recorded ZERO tool results: a figure from a
    turn that queried nothing is invented. Exempt: a number the caller said
    himself in this message, and `vouched` — numbers an EARLIER turn of the
    same session got from a tool (or its answer derived from that data), so
    "what was that number again?" is not treated as invention. Assistant
    history with no tool behind it never vouches: admitting it would let one
    fabrication vouch for every later turn (see `TurnProvenance`).
    """
    if tool_results:
        return []
    said = caller_numbers(caller_text) | set(vouched or ())
    return [d for d in find_figures(text) if d not in said]


# ---------------------------------------------------------------------------
# Action claims: "the card is ready" with no card
# ---------------------------------------------------------------------------
# Retest 2026-09-28 (V7): STT heard «غدا» ("tomorrow") for «غداء» ("lunch");
# the voice brain called no tool and answered «جهزت اقتراح استراحة لسارة…
# البطاقة جاهزة على شاشتك» — 2 of 2 times — and no proposal existed.
#
# Matched: preparing-a-change verbs ("I've prepared / set up … for you",
# «جهزت», «تم تجهيز») and card/ready wording ("the card is ready",
# «البطاقة جاهزة», «جاهز للمراجعة»). NOT matched: generic verbs ("I added up
# your bookings", "I created a summary"). The card/ready wording is ALSO the
# reminder our own prompts prescribe after a spoken "yes" ("it's ready to
# review — tap Confirm"), so callers apply this only when the CALL has no
# card yet (see `BusinessRuntimeAgent.voice_proposals_made`); with a card on
# screen the same words are a reminder, not a claim (re-review round 3).
_ACTION_CLAIM = re.compile(
    r"(?:\bI(?:['’]ve| have)? (?:prepared|set up|drafted)\b"
    r"|\bI(?:['’]ve| have)? created (?:a|the|your) [\w ]{0,30}proposal\b"
    # Eval 2026-09-30: "Done — I blocked Sara tomorrow…", "…as a proposal
    # for you to review" with no propose call.
    r"|\bI(?:['’]ve| have)? (?:blocked|booked|scheduled)\b|\bas a proposal\b"
    r"|\bproposal (?:is )?(?:ready|on your screen|for you to review)\b"
    r"|\bhere(?:'s| is) the card\b|\bthe card is (?:ready|on your screen)\b"
    r"|\b(?:it'?s|is) ready (?:for you )?to review\b|\bready for your review\b"
    r"|جهزت|جهّزت|جهزتها|جهّزتها|جهزتلك|جهّزت لك|تم تجهيز|البطاقة جاهزة|البطاقه جاهزه"
    r"|جاهزة? للمراجعة|أضفت اقتراح|اضفت اقتراح|سويت لك اقتراح"
    # Eval 2026-09-29: «تمت جدولة اقتراح … جاهز على شاشتك» with no tool call.
    r"|تمت? جدولة|تم حجز|تم حجب|جاهزة? على شاشتك|جاهزة? على الشاشة"
    r"|قمت بتحضير|حضرت|حضّرت|تم تحضير"
    # User voice test 2026-10-03 (a rename with no tool to land on): «بعمل
    # اقتراح لتغيير اسم … وهالقترح موجود على شاشتك للمراجعة والضغط على
    # Confirm», "I've made a proposal, it's on your screen, press Confirm".
    r"|\bI(?:['’]ve| have| just|['’]m| am)? (?:made|making|put together|sent|submitted)\b[^.?!]{0,25}\bproposal\b"
    r"|\bproposal\b[^.?!]{0,40}\bon (?:your|the) screen\b"
    r"|\b(?:tap|press|click|hit) (?:the |on )?confirm\b"
    r"|(?:عملت|بعمل|رح أعمل|رح اعمل|راح أعمل|سويت|بسوي|قدمت|أرسلت|ارسلت)\s+(?:لك\s+|لكم\s+)?(?:ال)?اقتراح"
    r"|(?:الاقتراح|الإقتراح|هالاقتراح|هالقترح|هالإقتراح|المقترح)[^.؟!]{0,30}(?:موجود|جاهز|ظاهر|طالع)"
    r"|(?:اضغط|إضغط|الضغط|اكبس|كبسة|دوس)\s+(?:على\s+)?(?:زر\s+)?(?:confirm|تأكيد|تاكيد|أكد|اكد))",
    re.I,
)


def claims_prepared_change(text: Any) -> bool:
    """Does `text` tell the merchant a change/card has been prepared?"""
    return bool(text) and bool(_ACTION_CLAIM.search(str(text)))


# ---------------------------------------------------------------------------
# The lines spoken INSTEAD of a withheld answer — one copy, used by both the
# agent itself (layer 1) and the voice delegate backstop (layer 2). Each
# asserts no fact and names a next step.
# ---------------------------------------------------------------------------
UNSOURCED_FIGURES_LINE = {
    "en": "I haven't pulled those numbers from your data yet, so I won't guess. Ask me again and I'll check.",
    "ar": "لسا ما سحبت هالأرقام من بياناتك، وما بدي خمّن. اسألني كمان مرة وبشيّكلك.",
}
# Generic on purpose: the staff/day/time wording it used to carry is a block
# card's, and it fired on a new service after the caller gave the price
# (user voice test 2026-10-01). `unprepared_change_line` names the fields of
# the change actually being prepared when the caller knows it.
UNPREPARED_CHANGE_LINE = {
    "en": "I haven't prepared that change yet. Tell me what's still missing and I'll prepare it.",
    "ar": "لسا ما جهزت هالتغيير. قلّي شو الناقص وبجهزه.",
}
_UNPREPARED_BY_FAMILY = {
    "service": {
        "en": ("I haven't prepared that service yet. I need its name, price, duration and category — "
               "tell me what's missing and I'll prepare it."),
        "ar": "لسا ما جهزت الخدمة. بدي اسمها وسعرها ومدتها وفئتها — قلّي شو الناقص وبجهزها.",
    },
    "schedule": {
        "en": ("I haven't prepared that change yet. Tell me which staff, which day and what time, and "
               "I'll prepare it."),
        "ar": "لسا ما جهزت هالتغيير. قلّي أي موظف، وأي يوم، وأي ساعة، وبجهزه.",
    },
}
_FAMILY_OF_KIND = {
    "create_service": "service", "update_service": "service",
    "block_time": "schedule", "update_block": "schedule", "remove_block": "schedule",
    "time_off": "schedule", "set_weekly_shift": "schedule", "set_date_shift": "schedule",
}


def unprepared_change_line(language: Optional[str], kind: Optional[str] = None) -> str:
    """The withheld-claim line, naming the fields of `kind` (a proposal kind
    or a propose_* tool name) when known; the generic line otherwise."""
    family = _FAMILY_OF_KIND.get((kind or "").replace("propose_", "", 1))
    return line_for(_UNPREPARED_BY_FAMILY[family] if family else UNPREPARED_CHANGE_LINE, language)


# A spoken "yes, save it" while a card is on screen: voice never saves.
SPOKEN_YES_LINE = {
    "en": "Nothing is saved by voice — tap Confirm on the card on your screen to save it.",
    "ar": "ما بينحفظ إشي بالصوت — اضغط تأكيد على البطاقة اللي على شاشتك لينحفظ.",
}


def line_for(table: Dict[str, str], language: Optional[str]) -> str:
    """`table`'s line for `language` (primary subtag), English otherwise."""
    primary = (language or "").split("-")[0].strip().lower()
    return table.get(primary, table["en"])
