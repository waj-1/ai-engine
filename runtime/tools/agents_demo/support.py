"""Inbound SUPPORT tools — a simulation over the fixture customers.

The call's rules are enforced HERE, not only in the prompt:
  * verify first — every account tool refuses (`not_verified`) until
    `support_verify_customer` matched an identifier (account number or
    registered mobile) AND a second factor (date of birth or ID last 4);
  * a failed check never says WHICH part was wrong, and locks after three
    tries (`too_many_attempts`) — the caller is then offered a human;
  * someone calling about another person's account is refused
    (`not_account_holder`) unless they are an authorized user, however much
    of the holder's data they know;
  * outage checks are public (no account data), so they work before
    verification;
  * every state-changing action (refund, booking, pause/cancel/relocate/SIM
    change, handoff) happens at most once per call, and service changes are
    quoted first (`confirm=false`) and only done on `confirm=true`.

`state` is the per-conversation dict from `new_state()` (plain JSON).
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from runtime.tools.agents_demo import data
from runtime.tools.agents_demo.common import (
    note_failed_attempt, parse_date, reference, second_factor_matches, working_days_from,
)

MAX_VERIFY_ATTEMPTS = 3
SERVICE_ACTIONS = ("pause", "cancel", "relocate", "esim", "sim_replacement")
TICKET_CATEGORIES = ("connection", "billing", "equipment", "mobile", "relocation", "complaint", "other")


def new_state() -> Dict[str, Any]:
    return {"verified": None, "verify_failures": 0, "refunds": [], "bookings": [],
            "changes": [], "tickets": [], "handoffs": []}


_NOT_VERIFIED = {
    "ok": False, "error": "not_verified",
    "detail": "Verify the caller first: account number or registered mobile, PLUS date of birth "
              "or the last 4 digits of their national ID. Share nothing about the account before that.",
}


def _lang(locale: Optional[str]) -> str:
    return "ar" if str(locale or "").lower().startswith("ar") else "en"


def _verified(state: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    acct = state.get("verified")
    if not acct:
        return None, dict(_NOT_VERIFIED)
    return data.customer(acct), None


def _norm_dob(value: Optional[str]) -> str:
    """ISO from 'YYYY-MM-DD' or 'DD/MM/YYYY' / 'DD-MM-YYYY' (digits in any numerals)."""
    raw = str(value or "").translate(data.DIGIT_MAP).strip()
    d = parse_date(raw)
    if d:
        return d.isoformat()
    parts = [p for p in raw.replace("-", "/").replace(".", "/").split("/") if p]
    if len(parts) == 3 and len(parts[2]) == 4:
        try:
            return date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat()
        except ValueError:
            return ""
    return ""


def _name_matches(spoken: str, cust: Dict[str, Any]) -> bool:
    s = data._fold(spoken)
    if not s:
        return True
    names = [cust["name_en"], cust["name_ar"], cust["first_en"], cust["first_ar"],
             *(u.get("name_en", "") for u in cust.get("authorized_users", [])),
             *(u.get("name_ar", "") for u in cust.get("authorized_users", []))]
    return any(data._fold(n) and (data._fold(n) in s or s in data._fold(n)) for n in names)


def _plan_name(plan_id: str, lang: str) -> str:
    p = data.products().get(plan_id, {})
    return p.get(f"name_{lang}") or plan_id


def _services_view(cust: Dict[str, Any], lang: str) -> List[str]:
    out = []
    for s in cust["services"]:
        name = _plan_name(s["plan"], lang)
        if s["type"] == "mobile":
            out.append(f"{name} (mobile line ending {s['number'][-4:]}, {s.get('sim', 'physical')} SIM)")
        else:
            out.append(f"{name} (home internet, {s.get('router_' + lang) or s.get('router_en', '')})")
    return out


def verify_customer(state: Dict[str, Any], *, account_number: Optional[str] = None,
                    mobile: Optional[str] = None, date_of_birth: Optional[str] = None,
                    id_last4: Optional[str] = None, caller_name: Optional[str] = None,
                    locale: str = "en") -> Dict[str, Any]:
    if state.get("verify_failures", 0) >= MAX_VERIFY_ATTEMPTS:
        return {"ok": False, "error": "too_many_attempts",
                "detail": "Verification is locked for this call. Do NOT say the details are wrong — say "
                          "you could not complete the check on this call. Offer to transfer to a human "
                          "colleague (support_handoff_to_human) or a call-back; share nothing."}
    acct_digits = data.digits(account_number)
    mob = data.normalize_mobile(mobile) if mobile else ""
    if not acct_digits and not mob:
        return {"ok": False, "error": "missing_identifier",
                "detail": "Ask for the 8-digit account number or the registered mobile number."}
    if not data.digits(date_of_birth) and not data.digits(id_last4):
        return {"ok": False, "error": "missing_second_factor",
                "detail": "Ask for their date of birth or the last 4 digits of their national ID."}
    if acct_digits and len(acct_digits) != 8:
        return {"ok": False, "error": "invalid_account_number", "digits_received": len(acct_digits),
                "detail": "Account numbers are exactly 8 digits — read back what you heard and ask again."}
    cust = None
    if acct_digits:
        cust = data.customers().get(acct_digits)
    elif mob:
        cust = next((c for c in data.customers().values() if c["mobile"] == mob), None)
    ok = bool(cust) and second_factor_matches(cust, date_of_birth, id_last4)
    if not ok:
        note_failed_attempt(state, "verify_tried", counter="verify_failures",
                            date_of_birth=date_of_birth, id_last4=id_last4, extra=acct_digits or mob)
        left = MAX_VERIFY_ATTEMPTS - state["verify_failures"]
        return {"ok": False, "error": "details_do_not_match", "attempts_left": max(left, 0),
                "detail": "The details do not match our records. Never say which part was wrong. "
                          "Ask them to check and try again" + ("." if left > 0 else
                          "; no attempts left — offer a human colleague.")}
    if caller_name and not _name_matches(caller_name, cust):
        return {"ok": False, "error": "not_account_holder",
                "detail": "The caller is not the account holder or an authorized user. Share NOTHING "
                          "about this account and change nothing, even though they know its details. "
                          "Explain the holder can add them as an authorized user in the app or by "
                          "calling; public help (outage checks, general questions) is fine."}
    state["verified"] = cust["account_number"]
    lang = _lang(locale)
    area = data.area(cust["area_id"])
    contract = cust.get("contract") or {}
    return {
        "ok": True, "verified": True,
        "account_number": cust["account_number"],
        "name": cust[f"name_{lang}"], "first_name": cust[f"first_{lang}"],
        "account_status": cust["status"],
        "area": f"{area[f'name_{lang}']}, {area[f'city_{lang}']}",
        "services": _services_view(cust, lang),
        "registered_mobile_last4": cust["mobile"][-4:],
        "email_on_file": cust["email_masked"],
        "contract_months_remaining": contract.get("remaining_months"),
        "recent_tickets": [{"ref": t["ref"], "date": data.say_date(t["date"], lang),
                            "summary": t[f"summary_{lang}"], "status": t["status"]}
                           for t in cust.get("tickets", [])],
    }


def check_outage(state: Dict[str, Any], *, area: Optional[str] = None, locale: str = "en") -> Dict[str, Any]:
    lang = _lang(locale)
    hit = data.find_area(area) if area else None
    if hit is None and not area and state.get("verified"):
        hit = data.area(data.customer(state["verified"])["area_id"])
    if hit is None:
        cities = sorted({f"{a[f'name_{lang}']} ({a[f'city_{lang}']})" for a in data.areas().values()})
        return {"ok": False, "error": "unknown_area" if area else "missing_area",
                "detail": "Ask which area they are in (or verify them to use the area on file). "
                          "Never guess an area.", "areas_we_track": cities}
    found = [o for o in data.outages() if o["area_id"] == hit["id"]]
    where = f"{hit[f'name_{lang}']}, {hit[f'city_{lang}']}"
    if not found:
        return {"ok": True, "area": where, "outage": False,
                "detail": "No known outage in this area right now."}
    return {"ok": True, "area": where, "outage": True, "outages": [
        {"id": o["id"], "service": o["service"], "summary": o[f"summary_{lang}"],
         "started": o[f"started_{lang}"], "expected_fix": o[f"eta_{lang}"], "status": o["status"]}
        for o in found]}


def run_line_diagnostics(state: Dict[str, Any], *, locale: str = "en") -> Dict[str, Any]:
    cust, err = _verified(state)
    if err:
        return err
    lang = _lang(locale)
    diag = cust.get("diagnostics") or {"verdict": "ok"}
    out: Dict[str, Any] = {"ok": True, "verdict": diag["verdict"],
                           "line": diag.get(f"line_{lang}") or diag.get("line_en")}
    for key in ("wifi", "fix"):
        if diag.get(f"{key}_en"):
            out[key] = diag.get(f"{key}_{lang}") or diag[f"{key}_en"]
    out["needs_technician"] = bool(diag.get("needs_technician"))
    if out["needs_technician"]:
        out["visit_charge"] = "free — the fault is on the network side" if not diag.get("visit_charge") else \
            data.money(data.fees()["technician_visit_customer_side"][cust["country"]], cust["country"], lang)
    if diag.get("outage_id"):
        o = next((x for x in data.outages() if x["id"] == diag["outage_id"]), None)
        if o:
            out["outage"] = {"id": o["id"], "summary": o[f"summary_{lang}"], "expected_fix": o[f"eta_{lang}"]}
    return out


def _bill_view(bill: Dict[str, Any], country: str, lang: str) -> Dict[str, Any]:
    paid = sum(p["amount"] for p in bill.get("payments", []))
    view = {
        "month": data.month_name(bill["month"], lang),
        "total": data.money(bill["amount"], country, lang),
        "lines": [f"{i[f'label_{lang}']}: {data.money(i['amount'], country, lang)}" for i in bill["items"]],
        "status": bill["status"],
        "payments": [f"{p['ref']} — {data.money(p['amount'], country, lang)} on "
                     f"{data.say_date(p['date'], lang)} ({p[f'method_{lang}']})" for p in bill.get("payments", [])],
        "paid_total": data.money(paid, country, lang),
    }
    if bill.get("due_date"):
        view["due_date"] = data.say_date(bill["due_date"], lang)
    return view


def explain_bill(state: Dict[str, Any], *, month: Optional[str] = None, locale: str = "en") -> Dict[str, Any]:
    cust, err = _verified(state)
    if err:
        return err
    lang = _lang(locale)
    bills = cust["bills"]
    bill = bills[-1]
    if month:
        key = str(month).strip()[:7]
        bill = next((b for b in bills if b["month"] == key), None)
        if bill is None:
            return {"ok": False, "error": "no_such_bill",
                    "available_months": [data.month_name(b["month"], lang) for b in bills]}
    out = {"ok": True, **_bill_view(bill, cust["country"], lang)}
    idx = bills.index(bill)
    if idx > 0:
        prev = bills[idx - 1]
        diff = round(bill["amount"] - prev["amount"], 2)
        out["previous_bill"] = f"{data.month_name(prev['month'], lang)}: {data.money(prev['amount'], cust['country'], lang)}"
        out["change_vs_previous"] = ("+" if diff > 0 else "") + data.money(diff, cust["country"], lang)
    dup = cust.get("duplicate_payment")
    if dup and dup["bill_month"] == bill["month"]:
        out["duplicate_payment"] = (f"Charged twice: {', '.join(dup['refs'])} — "
                                    f"{data.money(dup['amount'], cust['country'], lang)} each.")
    return out


def request_refund(state: Dict[str, Any], *, refund_to: str = "original_payment_method",
                   locale: str = "en") -> Dict[str, Any]:
    cust, err = _verified(state)
    if err:
        return err
    lang = _lang(locale)
    if state.get("refunds"):
        return {"ok": False, "error": "already_requested", "reference": state["refunds"][0]["ref"],
                "detail": "The refund was already raised on this call — do not raise it again."}
    dup = cust.get("duplicate_payment")
    if not dup:
        return {"ok": False, "error": "no_duplicate_found",
                "detail": "No duplicate or wrong payment is on record. Do not promise a refund. If the "
                          "customer disputes a charge, open a billing ticket (support_open_ticket)."}
    to_credit = refund_to == "bill_credit"
    ref = reference("REF", cust["account_number"], dup["bill_month"])
    state["refunds"].append({"ref": ref, "amount": dup["amount"], "to": refund_to})
    return {"ok": True, "reference": ref, "amount": data.money(dup["amount"], cust["country"], lang),
            "refund_to": "bill credit — applied to the next bill immediately" if to_credit
            else "the original payment method",
            "timeline": "immediately" if to_credit else
            f"{data.policies()['refund_working_days']} working days"}


def _slots(today: date) -> List[Dict[str, str]]:
    out = []
    for d in working_days_from(today, 2):
        for w in data.technician_windows():
            out.append({"slot_id": f"{d.isoformat()}-{w['code']}", "date": d.isoformat(),
                        "window_en": w["en"], "window_ar": w["ar"]})
    return out


def book_technician(state: Dict[str, Any], *, slot_id: Optional[str] = None, locale: str = "en",
                    today: Optional[date] = None) -> Dict[str, Any]:
    cust, err = _verified(state)
    if err:
        return err
    lang = _lang(locale)
    if state.get("bookings"):
        b = state["bookings"][0]
        return {"ok": False, "error": "already_booked", "reference": b["ref"],
                "detail": "A visit is already booked on this call — never book a second one."}
    slots = _slots(today or date.today())
    diag = cust.get("diagnostics") or {}
    charge = ("free — the fault is on the network side" if diag.get("needs_technician") and not diag.get("visit_charge")
              else f"{data.money(data.fees()['technician_visit_customer_side'][cust['country']], cust['country'], lang)} "
                   "if the fault turns out to be in the home's own wiring or equipment; free if it is on the network side")
    if not slot_id:
        return {"ok": True, "booked": False, "visit_charge": charge,
                "available_slots": [{"slot_id": s["slot_id"],
                                     "when": f"{data.say_date(s['date'], lang)}, {s['window_' + lang]}"}
                                    for s in slots],
                "detail": "Offer two or three of these; book with slot_id once the customer picks one."}
    slot = next((s for s in slots if s["slot_id"] == str(slot_id).strip()), None)
    if slot is None:
        return {"ok": False, "error": "slot_unavailable",
                "available_slots": [s["slot_id"] for s in slots]}
    ref = reference("VIS", cust["account_number"], slot["slot_id"])
    state["bookings"].append({"ref": ref, "slot_id": slot["slot_id"]})
    return {"ok": True, "booked": True, "reference": ref,
            "when": f"{data.say_date(slot['date'], lang)}, {slot['window_' + lang]}",
            "visit_charge": charge, "detail": "An adult needs to be home for the visit."}


def change_service(state: Dict[str, Any], *, action: str, months: Optional[int] = None,
                   new_area: Optional[str] = None, move_date: Optional[str] = None,
                   confirm: bool = False, locale: str = "en", today: Optional[date] = None) -> Dict[str, Any]:
    cust, err = _verified(state)
    if err:
        return err
    lang = _lang(locale)
    action = (action or "").strip().lower()
    if action not in SERVICE_ACTIONS:
        return {"ok": False, "error": "invalid_action", "allowed": list(SERVICE_ACTIONS)}
    if any(c["action"] == action for c in state.get("changes", [])):
        return {"ok": False, "error": "already_done",
                "detail": f"The {action} was already done on this call — never do it twice."}
    country = cust["country"]
    today = today or date.today()
    contract = cust.get("contract") or {}
    home = next((s for s in cust["services"] if s["type"] == "home"), None)
    mobile = next((s for s in cust["services"] if s["type"] == "mobile"), None)
    quote: Dict[str, Any] = {"action": action}

    if action in ("pause", "cancel", "relocate") and home is None:
        return {"ok": False, "error": "no_home_service"}
    if action in ("esim", "sim_replacement") and mobile is None:
        return {"ok": False, "error": "no_mobile_line"}

    if action == "pause":
        left = data.policies()["pause_max_months_per_year"] - contract.get("pauses_used_this_year", 0)
        n = int(months or 0)
        if n < 1 or n > left:
            return {"ok": False, "error": "invalid_months", "max_months": left,
                    "detail": f"A pause is 1 to {left} months."}
        fee = data.fees()["pause_fee_per_month"][country]
        quote.update(months=n, fee_per_month=data.money(fee, country, lang),
                     total_fee=data.money(fee * n, country, lang),
                     effect="home internet paused, contract and router kept; the contract end date "
                            f"moves {n} month(s) later")
    elif action == "cancel":
        remaining = int(contract.get("remaining_months") or 0)
        price = data.price_of(home["plan"], country)
        etf = round(remaining * price * data.fees()["early_termination_share_of_remaining"], 2)
        quote.update(contract_months_remaining=remaining,
                     early_termination_fee=data.money(etf, country, lang) if etf else "none",
                     how_computed=(f"{remaining} remaining months × half of the "
                                   f"{data.money(price, country, lang)} monthly fee") if etf else
                     "contract already ended",
                     equipment="return the router within 14 days",
                     alternative=(f"pause for up to {data.policies()['pause_max_months_per_year']} months at "
                                  f"{data.money(data.fees()['pause_fee_per_month'][country], country, lang)} a month")
                     if etf else None)
    elif action == "relocate":
        dest = data.find_area(new_area)
        if dest is None:
            return {"ok": False, "error": "unknown_area",
                    "detail": "Ask for the new area; never guess coverage."}
        if dest["country"] != country:
            return {"ok": False, "error": "other_country",
                    "detail": "Service cannot move between countries — the new country needs a new account."}
        when = parse_date(move_date) if move_date else None
        used = contract.get("relocations_used_this_year", 0)
        quote.update(new_area=f"{dest[f'name_{lang}']}, {dest[f'city_{lang}']}",
                     fiber_available=dest["fiber"], wireless_5g_available=dest["wireless_5g"],
                     relocation_fee="free (first move this contract year)" if used < 1 else
                     "not free — a second move in the contract year is quoted by the team",
                     setup_time=f"{data.policies()['relocation_working_days']} working days")
        if not dest["fiber"]:
            quote["note"] = ("No fiber at the new address — they can switch to Home 5G Wireless or "
                             "cancel without the early-termination fee.")
        if when:
            if when < today:
                return {"ok": False, "error": "date_in_past"}
            quote["move_date"] = data.say_date(when.isoformat(), lang)
            if len(working_days_from(today, 5)) and when < working_days_from(today, 5)[-1]:
                quote["notice_warning"] = "Less than 5 working days' notice — the install may land after the move."
        elif confirm:
            return {"ok": False, "error": "missing_move_date", "detail": "Ask for the move date first."}
    elif action in ("esim", "sim_replacement"):
        free = cust.get("sim_changes_this_year", 0) < 1
        quote.update(line_ending=mobile["number"][-4:],
                     fee="free (first SIM change this year)" if free else
                     data.money(data.fees()["sim_replacement_after_first"][country], country, lang))
        if action == "esim":
            quote.update(how=f"a QR code is emailed to {cust['email_masked']}; scan it on the new phone",
                         note="the current physical SIM stops working as soon as the eSIM activates")
        else:
            quote.update(how="a new SIM card is delivered the next working day")

    if not confirm:
        return {"ok": True, "done": False, "quote": quote,
                "detail": "Tell the customer these terms and ask for an explicit yes; then call again "
                          "with confirm=true."}
    ref = reference("SRV", cust["account_number"], action)
    state["changes"].append({"action": action, "ref": ref})
    return {"ok": True, "done": True, "reference": ref, "summary": quote}


def open_ticket(state: Dict[str, Any], *, category: str, summary: str) -> Dict[str, Any]:
    cust, err = _verified(state)
    if err:
        return err
    category = (category or "").strip().lower()
    if category not in TICKET_CATEGORIES:
        return {"ok": False, "error": "invalid_category", "allowed": list(TICKET_CATEGORIES)}
    ref = reference("TCK", cust["account_number"], category, len(state["tickets"]))
    state["tickets"].append({"ref": ref, "category": category, "summary": summary})
    return {"ok": True, "reference": ref, "category": category,
            "detail": "Ticket opened; the team follows up within one working day."}


def handoff_to_human(state: Dict[str, Any], *, summary: str, reason: str = "") -> Dict[str, Any]:
    if state.get("handoffs"):
        return {"ok": False, "error": "already_handed_off", "reference": state["handoffs"][0]["ref"]}
    ref = reference("HND", state.get("verified") or "anon", summary[:40])
    state["handoffs"].append({"ref": ref, "summary": summary, "reason": reason})
    return {"ok": True, "reference": ref, "verified": bool(state.get("verified")),
            "expected_wait": "under 5 minutes",
            "detail": "A human colleague takes over with this summary, so the customer will not have "
                      "to repeat themselves."}


# ---------------------------------------------------------------------------
# LangChain wrappers
# ---------------------------------------------------------------------------

class VerifyInput(BaseModel):
    account_number: Optional[str] = Field(None, description="8-digit account number exactly as the caller said it, digits only.")
    mobile: Optional[str] = Field(None, description="Registered mobile number, if they gave that instead of the account number.")
    date_of_birth: Optional[str] = Field(None, description="Date of birth as YYYY-MM-DD.")
    id_last4: Optional[str] = Field(None, description="Last 4 digits of their national ID, if given instead of the date of birth.")
    caller_name: Optional[str] = Field(None, description="The caller's own name when they say they are calling for someone else.")


class OutageInput(BaseModel):
    area: Optional[str] = Field(None, description="Area or neighbourhood the caller named; omit to use the verified customer's area.")


class BillInput(BaseModel):
    month: Optional[str] = Field(None, description="Bill month as YYYY-MM; omit for the latest bill.")


class RefundInput(BaseModel):
    refund_to: str = Field("original_payment_method", description="original_payment_method | bill_credit — as the customer chose.")


class TechnicianInput(BaseModel):
    slot_id: Optional[str] = Field(None, description="Omit to list available slots; pass the chosen slot_id to book it.")


class ServiceChangeInput(BaseModel):
    action: str = Field(..., description="pause | cancel | relocate | esim | sim_replacement")
    months: Optional[int] = Field(None, description="pause only: number of months (1-3).")
    new_area: Optional[str] = Field(None, description="relocate only: the new area / neighbourhood.")
    move_date: Optional[str] = Field(None, description="relocate only: move date YYYY-MM-DD.")
    confirm: bool = Field(False, description="false = get the terms to read out; true = do it, ONLY after the customer's explicit yes to those terms.")


class TicketInput(BaseModel):
    category: str = Field(..., description="connection | billing | equipment | mobile | relocation | complaint | other")
    summary: str = Field(..., description="One or two sentences: the problem and what was tried.")


class HandoffInput(BaseModel):
    summary: str = Field(..., description="Handover summary for the human colleague: who, verified or not, the problem, what was tried, what they want.")
    reason: str = Field("", description="Why a human is needed (asked for one, repeated issue, verification locked, …).")


def _dump(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_support_tools(context: Optional[Dict[str, Any]] = None) -> List[StructuredTool]:
    context = context or {}
    state = context.get("call_state")
    if state is None:
        state = new_state()
    locale = context.get("locale") or "en"

    def _verify(account_number=None, mobile=None, date_of_birth=None, id_last4=None, caller_name=None):
        return _dump(verify_customer(state, account_number=account_number, mobile=mobile,
                                     date_of_birth=date_of_birth, id_last4=id_last4,
                                     caller_name=caller_name, locale=locale))

    def _outage(area=None):
        return _dump(check_outage(state, area=area, locale=locale))

    def _diag():
        return _dump(run_line_diagnostics(state, locale=locale))

    def _bill(month=None):
        return _dump(explain_bill(state, month=month, locale=locale))

    def _refund(refund_to="original_payment_method"):
        return _dump(request_refund(state, refund_to=refund_to, locale=locale))

    def _tech(slot_id=None):
        return _dump(book_technician(state, slot_id=slot_id, locale=locale))

    def _change(action, months=None, new_area=None, move_date=None, confirm=False):
        return _dump(change_service(state, action=action, months=months, new_area=new_area,
                                    move_date=move_date, confirm=confirm, locale=locale))

    def _ticket(category, summary):
        return _dump(open_ticket(state, category=category, summary=summary))

    def _handoff(summary, reason=""):
        return _dump(handoff_to_human(state, summary=summary, reason=reason))

    return [
        StructuredTool.from_function(
            func=_verify, name="support_verify_customer", args_schema=VerifyInput,
            description=("Verify the caller: account number OR registered mobile, PLUS date of birth OR ID "
                         "last 4. MUST succeed before any account detail is shared or changed. Returns the "
                         "customer's name, services, area and recent tickets.")),
        StructuredTool.from_function(
            func=_outage, name="support_check_outage", args_schema=OutageInput,
            description="Check for a known network outage in an area (public — works before verification)."),
        StructuredTool.from_function(
            func=_diag, name="support_run_line_diagnostics",
            description=("Run remote diagnostics on the verified customer's line and Wi-Fi: verdict, what it "
                         "means, the fix, and whether a technician is needed (and if the visit is free).")),
        StructuredTool.from_function(
            func=_bill, name="support_explain_bill", args_schema=BillInput,
            description=("The verified customer's bill, line by line, with payments, due date and the change "
                         "versus the previous bill. The ONLY source for any amount you state.")),
        StructuredTool.from_function(
            func=_refund, name="support_request_refund", args_schema=RefundInput,
            description=("Raise a refund for a duplicate payment on record. Only after the customer chose card "
                         "refund or bill credit. Once per call.")),
        StructuredTool.from_function(
            func=_tech, name="support_book_technician", args_schema=TechnicianInput,
            description=("Without slot_id: list technician visit slots and the visit charge. With slot_id: book "
                         "that slot (only the one the customer picked). Once per call.")),
        StructuredTool.from_function(
            func=_change, name="support_change_service", args_schema=ServiceChangeInput,
            description=("Pause, cancel, relocate the home service, or move a mobile line to eSIM / a new SIM. "
                         "Call with confirm=false first to get the exact terms (fees, dates, early-termination "
                         "fee), read them out, and only after an explicit yes call again with confirm=true.")),
        StructuredTool.from_function(
            func=_ticket, name="support_open_ticket", args_schema=TicketInput,
            description="Open a support ticket for the verified customer; returns the ticket reference."),
        StructuredTool.from_function(
            func=_handoff, name="support_handoff_to_human", args_schema=HandoffInput,
            description=("Transfer the conversation to a human colleague with a written summary (when they ask "
                         "for a human, the issue keeps coming back, or verification is locked). Once per call.")),
    ]
