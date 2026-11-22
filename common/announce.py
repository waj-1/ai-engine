"""Lookup-announcement detection, shared across layers.

An assistant reply that declares a lookup ("one moment", «ثانية وحدة») and
then ends with no tool call behind it is a stall in the making — the caller
was promised something no later mechanism will deliver. TWO layers guard
against it and must agree on what counts as an announcement:

* `endpoints/voice_live.py` — the announce-then-stall tripwire: the realtime
  model announced but never called `ask_agent`, so the endpoint dispatches
  the committed question itself.
* a product agent's announce-without-tool-call guard one layer
  down: the DELEGATE'S OWN final answer was the announcement (live 2026-08-06:
  "I'm pulling the troubleshooting guide for error 119 now. One moment." —
  one model round, zero tool calls, caller left in dead air), so the agent
  gets one nudge round to actually call the tool.

This module is the single source of truth both import.
`tests/voice/test_announce_detector_agreement.py` holds the persona prompts
and this token set together; `tests/test_common_announce.py` pins the
endpoint's aliases to these objects so the detectors cannot re-fork.
"""
from __future__ import annotations

import re

# Arabic orthography the ASR varies freely between: ta-marbuta vs ha, the
# alef family, alef-maqsura vs ya, plus tashkeel. Enumerating spellings does
# not work — the token set listed "أيوه" and a live call transcribed the very
# same nod as "أيوة", which then read as a real utterance and dropped the
# answer the caller was waiting for. Normalise instead of enumerate.
AR_NORMALISE = str.maketrans({
    "ة": "ه", "أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ؤ": "و", "ئ": "ي",
})
AR_TASHKEEL = re.compile(r"[\u064b-\u0652\u0640]")


def normalize_intent_text(text: str) -> str:
    """Lowercase and drop spaces/hyphens for relay-intent matching.

    ASR spells the same domain term three different ways on one call —
    "IBAN", "i ban", "E-band" — and a custody trigger that misses the garble
    leaves the protected flow to the realtime model (observed live
    2026-07-31: it asked for the new IBAN with no OTP). Normalizing both
    sides lets "iban" cover "i ban"/"I-ban" and "e band" cover
    "e-band"/"eband" without enumerating every spacing variant.
    """
    return AR_TASHKEEL.sub(
        "", re.sub(r"[\s -]+", "", (text or "").lower())
    ).translate(AR_NORMALISE)


# Looking-it-up announce phrases, matched (normalized, contains) against the
# assistant's own completed reply. No legitimate reply declares a lookup and
# then ends with no tool call behind it. The list only needs the phrases the
# personas actually script; matching runs through normalize_intent_text on
# both sides, so spacing/tashkeel variants are covered without enumeration.
ANNOUNCE_TOKENS = (
    "one moment", "one sec", "just a sec", "let me see", "let me check",
    "let me pull", "pull that up", "let me look", "looking that up",
    "let me get", "i'll check", "checking that", "let me find",
    "ثواني", "ثانيه وحده", "ثانية وحدة", "لحظه", "لحظة", "دقيقه", "دقيقة",
    "خلني اشوف", "خلني أشوف", "اشوف لك", "أشوف لك", "اطلع لك", "أطلع لك",
    "خلني اطلع", "اتحقق", "أتحقق", "اشيك لك", "أشيك",
    # ── Improvised phrasings (added 2026-08-04) ────────────────────────────
    # The list above assumed "the phrases the personas actually script". The
    # persona told the model to "vary it every time", so it improvised past
    # the list and the tripwire went blind: 1 recovery in 14 live sessions,
    # while the caller got the permanent silence recovery exists to prevent.
    # The prompt is now pinned to an approved set (runtime/prompts/voice.py)
    # and tests/voice/test_announce_detector_agreement.py holds the two
    # together; these cover the near-misses either side of that set.
    #
    # Every addition must be a phrase that ONLY occurs when declaring a
    # lookup. Deliberately NOT added: bare "moment"/"second"/«ثانية», which
    # appear in ordinary answers ("the second branch", «شكراً مرة ثانية» on a
    # goodbye) and would arm the recovery on a closure turn — recovery then
    # talks over the persona's own farewell, on every call.
    "give me a sec", "give me a second", "gimme a sec",
    "hold on", "hang on", "bear with me",
    "take a look", "have a look", "let me confirm", "i'll confirm",
    "i'll look", "i'll pull", "i'll get that", "i'll find",
    "let me have a look", "let me just check",
    "لحظات", "خلني اتاكد", "خلني اتحقق", "خلني اجيب", "اجيب لك",
    "راح اشوف", "بشوف لك", "بشيك", "دقيقه وحده", "ثانيه وحده بس",
    # Levantine (a Levantine-speaking voice tenant, 2026-10-01).
    "خليني شوف", "خليني اشوف", "خليني اتاكد", "بشوفلك", "بشيكلك", "هلا بشوف",
)


def contains_announce(reply: str) -> bool:
    """Does this reply declare a lookup? Both guards' arming test."""
    norm = normalize_intent_text(reply)
    return any(normalize_intent_text(tok) in norm for tok in ANNOUNCE_TOKENS)


QUESTION_MARKS = ("?", "؟")
# Trailing punctuation/markup the model wraps a question in without changing
# that it IS one — a closing quote, a bracket, an ellipsis, whitespace.
QUESTION_TRAILING = " \t\r\n\"'»”’)]}…."


def reply_ends_in_question(reply: str) -> bool:
    """Did the model hand the turn back to the caller on purpose?

    Stall recovery (either layer) is right for a stall and WRONG for a
    deliberate question back — the persona asks one clarifying question on a
    vague problem, and offers ("want the breakdown?") rather than dumping
    detail. Recovering those answers a question the caller never got to
    answer, and talks over the persona.

    Deliberately shallow: a trailing `?`/`؟` is the one signal that survives
    every language and dialect here without a phrase list — and a phrase list
    is precisely the fragility the announce tier already taught us about.
    Anything subtler falls through, which is the conservative direction.
    """
    return reply.rstrip(QUESTION_TRAILING).endswith(QUESTION_MARKS)
