"""Claims that a booking action happened — checked against what actually ran.

Prod customer-chat evals (2026-10-19): the agent told customers «تم حجز
موعدك…» and "Your appointment is booked" in turns where no booking
tool had run, so the customer believed they had an appointment the business
never saw. Prompt rules cut this (5 → 1 per run) but cannot stop it. A reply that
claims an action no tool completed this turn is replaced before it is sent:

* the tool was not run, or refused for want of a confirmation → ask the
  customer to confirm that action;
* the tool ran and failed → apologise and hand off to the team.

Passive wording ("your haircut is booked") counts too, unless the turn looked
up the customer's bookings, where it is how an existing booking is described.

Only first-person "done" claims count ("I've booked", "we reserved", «حجزت
لك», «تم الإلغاء»). A question ("Shall I book…?", «أحجز لك؟») and a reading of
an existing booking ("you have a haircut booked tomorrow", "your past bookings
are cancelled") are not claims.
"""
from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

# Tool name → the action it completes.
ACTION_TOOLS: Dict[str, str] = {
    "submit_booking": "book",
    "reschedule_booking": "move",
    "cancel_booking": "cancel",
    "add_service_to_booking": "add",
}

_I = r"\b(?:I|we)(?:'ve|’ve| have)?\s+(?:just\s+|now\s+|already\s+)?"
_AR_END = r"(?![ء-ي])"

_CLAIMS: Dict[str, re.Pattern] = {
    "book": re.compile(
        _I + r"(?:booked|reserved|scheduled|confirmed|set (?:that|it|this) up)\b"
        r"|\byou(?:'re|’re| are)\s+(?:all\s+)?(?:set|booked)\b"
        r"|\ball set\b"
        r"|(?:حجزت|حجزنا)\s*(?:لك|لكم)" + _AR_END +
        # «تم تأكيد حجزك» went to a customer at NXT (2026-10-31) with no booking
        # made: the possessive forms were missing.
        r"|تم\s*،?\s*(?:حجز|الحجز|(?:تأكيد|تاكيد|تثبيت)\s+(?:الحجز|حجزك|حجزكم|الموعد|موعدك|موعدكم))",
        re.I,
    ),
    "cancel": re.compile(
        _I + r"cancel(?:l)?ed\b"
        r"|(?:ألغيت|الغيت|ألغينا|الغينا)" + _AR_END +
        r"|تم\s+(?:إلغاء|الغاء)",
        re.I,
    ),
    "move": re.compile(
        _I + r"(?:moved|rescheduled|updated|changed)\b"
        r"|(?:غيرت|غيّرت|نقلت|عدلت|عدّلت)\s+(?:لك\s+)?(?:الموعد|موعدك)"
        r"|تم\s+(?:تغيير|نقل|تعديل)\s+(?:الموعد|موعدك)",
        re.I,
    ),
    "add": re.compile(
        _I + r"added\b"
        r"|(?:أضفت|اضفت|أضفنا|اضفنا)" + _AR_END +
        r"|تم\s+(?:إضافة|اضافة)",
        re.I,
    ),
}

# "Your haircut is booked for tomorrow" is a claim after a booking turn, but
# the same words describe an existing booking the agent just looked up. They
# count only when the turn read no bookings.
_PASSIVE: Dict[str, re.Pattern] = {
    "book": re.compile(r"\b(?:is|are|has been|have been)\s+(?:now\s+)?(?:booked|reserved|confirmed)\b", re.I),
    "cancel": re.compile(r"\b(?:is|are|has been|have been)\s+(?:now\s+)?cancel(?:l)?ed\b", re.I),
    # "is now with Sara" too: the 2026-10-22 live test's false claim.
    "move": re.compile(
        r"\b(?:is|are|has been|have been)\s+(?:now\s+)?(?:moved|rescheduled|updated|changed)\b"
        r"|\b(?:is|are)\s+now\s+with\b",
        re.I,
    ),
}
_BOOKING_READS = {"list_customer_bookings"}

# Order a reply's claims are answered in when it makes more than one.
_KINDS = ("book", "move", "cancel", "add")

# A refusal for want of a confirmation is "not done yet", not a failure.
_NOT_CONFIRMED = {"confirmation_required", "confirmation required"}

_ARABIC = re.compile(r"[ء-ي]")

_ASK = {
    "en": {
        "book": "Just to confirm before I go ahead: shall I book this for you?",
        "move": "Just to confirm before I go ahead: shall I move your booking to the new time?",
        "cancel": "Just to confirm before I go ahead: shall I cancel this booking for you?",
        "add": "Just to confirm before I go ahead: shall I add this service to your booking?",
    },
    "ar": {
        "book": "للتأكيد قبل ما أكمل: أحجز لك الموعد؟",
        "move": "للتأكيد قبل ما أكمل: أغيّر لك الموعد للوقت الجديد؟",
        "cancel": "للتأكيد قبل ما أكمل: ألغي لك الموعد؟",
        "add": "للتأكيد قبل ما أكمل: أضيف الخدمة لموعدك؟",
    },
}
_FAILED = {
    "en": "Sorry, I couldn't complete that just now. A member of our team will follow up with you here.",
    "ar": "عذراً، ما قدرت أكمل الطلب الحين. واحد من فريقنا بيتواصل معك هنا.",
}


def claimed_actions(
    text: str, *, read_bookings: bool = False, attempted: Iterable[str] = (),
) -> Set[str]:
    """The actions `text` says were done. With `read_bookings`, passive
    wording ("is booked") is taken as describing what was looked up, except
    for an action in `attempted` (its tool ran this turn and did not
    succeed): then "has been moved" can only mean the change."""
    text = text or ""
    attempted = set(attempted)
    kinds = {kind for kind, pattern in _CLAIMS.items() if pattern.search(text)}
    kinds |= {
        kind for kind, pattern in _PASSIVE.items()
        if (not read_bookings or kind in attempted) and pattern.search(text)
    }
    return kinds


def _outcome(result: str) -> str:
    """'done' | 'not_confirmed' | 'failed' for one write tool's result."""
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict):
        return "failed" if not (result or "").strip() else "done"
    error = data.get("error")
    if error:
        return "not_confirmed" if str(error).strip().lower() in _NOT_CONFIRMED else "failed"
    if data.get("success") is False or str(data.get("status") or "").lower() == "error":
        return "failed"
    return "done"


def tool_outcomes(results: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """Per action, the best outcome this turn ('done' beats a retry's failure)."""
    rank = {"done": 2, "failed": 1, "not_confirmed": 0}
    best: Dict[str, str] = {}
    for tool, result in results:
        kind = ACTION_TOOLS.get(tool)
        if kind is None:
            continue
        outcome = _outcome(result)
        if kind not in best or rank[outcome] > rank[best[kind]]:
            best[kind] = outcome
    return best


def guard_action_claims(
    reply: str, results: Iterable[Tuple[str, str]],
) -> Tuple[str, Optional[str]]:
    """`(reply, None)` when every claimed action completed this turn, else
    `(replacement, reason)`: an ask for a claim no tool completed, or the
    hand-off when the tool ran and failed. `results` are the turn's
    `(tool_name, result)` pairs."""
    results = list(results)
    outcomes = tool_outcomes(results)
    # Live test 2026-10-22: the move was refused (no confirmation) in a turn
    # that also read the bookings, and "has been moved to Sara" went out.
    claims = claimed_actions(
        reply,
        read_bookings=any(t in _BOOKING_READS for t, _ in results),
        attempted=[k for k, outcome in outcomes.items() if outcome != "done"],
    )
    if not claims:
        return reply, None
    unbacked: List[str] = [k for k in _KINDS if k in claims and outcomes.get(k) != "done"]
    if not unbacked:
        return reply, None
    lang = "ar" if _ARABIC.search(reply) else "en"
    if any(outcomes.get(k) == "failed" for k in unbacked):
        return _FAILED[lang], "failed:" + ",".join(unbacked)
    return _ASK[lang][unbacked[0]], "unbacked:" + ",".join(unbacked)
