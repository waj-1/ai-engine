"""Call mechanics shared by the four demo agents.

Two things here are deliberately CODE, not prompt:

  * The send yes-gate. A link (payment or offer) goes out only when the
    agent's previous completed turn asked the send question — naming the
    registered mobile's last four digits, or offering to send with a
    question mark — and only ONCE per call. In voice the edge feeds every
    completed assistant turn to `note_assistant_turn`; in text the agent
    feeds the previous assistant message from the chat history before each
    turn. Live finding this exists for: an outbound voice once said "it's
    sent" one turn after the pitch with no question asked, then sent it
    again.
  * Case references. A reference the agent may speak is decided before the
    conversation (from the case id), never minted by a tool mid-call — a
    realtime model has been heard inventing a ticket number before its tool
    call landed.

State dicts are plain JSON (they ride a signed token between text turns).
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from runtime.tools.agents_demo.data import DIGIT_MAP

# Any letter in any script — a reply with words, not just digits/punctuation.
HAS_LETTER = re.compile(r"[^\W\d_]")

_SEND_WORDS = ("send", "text you", "sms", "أرسل", "ارسل", "نرسل", "أبعث", "ابعث", "نبعث", "رسالة")
_NEGATIVE = re.compile(
    r"^\s*(no|nope|not now|don'?t|do not|لا|لأ|مو الحين|لا شكرا|لا تر[سص]ل)\b", re.IGNORECASE)


def reference(prefix: str, *parts: Any) -> str:
    """A stable, fictional reference: PREFIX-XXXXXX from the given parts."""
    raw = ":".join(str(p) for p in parts)
    return f"{prefix}-" + hashlib.sha256(raw.encode()).hexdigest()[:6].upper()


def note_assistant_turn(state: Dict[str, Any], text: str, last4: str) -> None:
    """One COMPLETED assistant turn. A turn that names the registered mobile's
    last four digits, or offers to send with a question, is the send-it
    question; the customer's answer comes in the NEXT turn, so a send in the
    same response as the question is never covered."""
    text = text or ""
    found = "".join(ch for ch in text.translate(DIGIT_MAP) if ch.isdigit())
    low = text.casefold()
    asks = ("?" in text or "؟" in text) and any(w in low for w in _SEND_WORDS)
    state["asked_send"] = bool(last4 and last4 in found) or asks


def note_customer_turn(state: Dict[str, Any], text: str) -> None:
    """Text mode only: the customer's reply to the send question. A bare
    number, an empty line or a plain "no" is not a yes."""
    text = text or ""
    state["reply_has_words"] = bool(HAS_LETTER.search(text))
    state["reply_negative"] = bool(_NEGATIVE.search(text.casefold()))


def send_gate(state: Dict[str, Any], last4: str, *, what: str = "link") -> Optional[Dict[str, Any]]:
    """None when a send may go out now, else the refusal to hand back."""
    if state.get("links"):
        return {"ok": False, "error": "already_sent", "sent_to": "••••" + last4,
                "detail": f"The {what} was already sent on this call — never send it again; "
                          "say it is on its way."}
    if not state.get("asked_send"):
        return {"ok": False, "error": "not_confirmed", "registered_mobile_last4": last4,
                "detail": f"NOTHING WAS SENT — never say it was. First ASK whether to send the {what} "
                          f"by SMS to the number ending {last4}, then wait for an explicit yes."}
    if state.get("reply_has_words") is False or state.get("reply_negative"):
        return {"ok": False, "error": "not_confirmed", "registered_mobile_last4": last4,
                "detail": "NOTHING WAS SENT. The customer's last reply was not a yes — ask again "
                          "or move on."}
    return None


def parse_date(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "").translate(DIGIT_MAP).strip()[:10])
    except ValueError:
        return None


def _two_digit_year(yy: int) -> int:
    return 1900 + yy if yy > date.today().year % 100 else 2000 + yy


def _dates_from_digits(d: str) -> set:
    """Every real date a bare digit run can mean, spoken day-first («ثمانية
    ثمانية أربعة وثمانين» = 8884) or year-first (19840808)."""
    out = set()

    def _add(y: int, m: int, dd: int) -> None:
        try:
            out.add(date(y, m, dd).isoformat())
        except ValueError:
            pass

    if len(d) == 8:
        _add(int(d[:4]), int(d[4:6]), int(d[6:]))
    for i in (1, 2):              # day digits
        for j in (1, 2):          # month digits
            rest = d[i + j:]
            if len(rest) in (2, 4):
                y = int(rest) if len(rest) == 4 else _two_digit_year(int(rest))
                _add(y, int(d[i:i + j]), int(d[:i]))
    return out


def candidate_dobs(value: Optional[str]) -> set:
    """ISO dates a spoken date of birth may mean: YYYY-MM-DD, DD/MM/YYYY,
    two-digit years, and bare digit runs."""
    raw = str(value or "").translate(DIGIT_MAP).strip()
    if not raw:
        return set()
    d = parse_date(raw)
    if d:
        return {d.isoformat()}
    parts = [p for p in re.split(r"[^0-9]+", raw) if p]
    out = set()
    if len(parts) == 3:
        a, b, c = parts
        for y, m, dd in ((a, b, c), (c, b, a)):   # Y-M-D or D-M-Y
            if len(y) in (2, 4) and len(m) <= 2 and len(dd) <= 2:
                yr = int(y) if len(y) == 4 else _two_digit_year(int(y))
                try:
                    out.add(date(yr, int(m), int(dd)).isoformat())
                except ValueError:
                    pass
    out |= _dates_from_digits("".join(parts))
    return out


def second_factor_matches(cust: Dict[str, Any], date_of_birth: Optional[str],
                          id_last4: Optional[str]) -> bool:
    """Date of birth OR ID last 4 — whichever FIELD the model put it in.

    The realtime model files what it heard: «ثمانية ثمانية أربعة وثمانين»
    (8/8/84, a date of birth) arrived as id_last4="8884" and failed against
    the right answer (live 2026-10-14). The value must still equal the
    customer's own date or digits; only the field is forgiven."""
    said = [v for v in (date_of_birth, id_last4) if v]
    for v in said:
        if cust["dob"] in candidate_dobs(v):
            return True
        if "".join(ch for ch in str(v).translate(DIGIT_MAP) if ch.isdigit())[-4:] == cust["id_last4"]:
            return True
    return False


def note_failed_attempt(state: Dict[str, Any], key: str, *, counter: str,
                        date_of_birth: Optional[str], id_last4: Optional[str], extra: str = "") -> bool:
    """Count one failed identity attempt, unless it repeats one already
    counted (a re-heard or re-sent value is not a new guess). Returns
    whether it counted."""
    # The same date in another spelling is the same guess.
    parts = [",".join(sorted(candidate_dobs(v))) or "".join(
        ch for ch in str(v or "").translate(DIGIT_MAP) if ch.isdigit()) for v in (date_of_birth, id_last4)]
    said = f"{extra}#" + "|".join(parts)
    tried = state.setdefault(key, [])
    if said in tried:
        return False
    tried.append(said)
    state[counter] = state.get(counter, 0) + 1
    return True


# Said to the CALLER when attempts run out: the last value may well have
# been right, so the agent must never claim it did not match.
LOCKED_DETAIL = ("Verification is closed for this call. Do NOT say the details are wrong or do not match — "
                 "say you could not complete the check on this call, then offer a call-back (log_case_note "
                 "callback) and close politely.")


def working_days_from(start: date, n: int) -> List[date]:
    """The next ``n`` working days after ``start`` (Friday + Saturday off —
    the common Gulf weekend for field visits in this fiction)."""
    out: List[date] = []
    d = start
    while len(out) < n:
        d = d + timedelta(days=1)
        if d.weekday() not in (4, 5):
            out.append(d)
    return out
