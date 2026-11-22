"""Outbound COLLECTION call — brain tools, pre-dial case file, direct voice tools.

The agent calls a customer about an overdue balance. Two ways to serve it:

  * TEXT (and any delegated voice): a brain with tools. Verification is
    enforced in CODE — `collection_get_balance`, links, plans and promises
    refuse (`not_verified`) until `collection_verify_identity` matched the
    dialled customer's date of birth or ID last 4.
  * VOICE (pre-fetched context): the automation that dials the call fetches
    the case BEFORE dialling and renders it into the persona as a case file
    (`case_file`); the realtime model answers from it and calls only the
    instant tools from `direct_tools` (0 ms, local). Verification stays in
    CODE here too: the case file does NOT carry the identity answers, the
    model calls `verify_identity` with what the person said, and the action
    tools refuse until it matched (a realtime model told the answers judged
    wrong ones "correct" — 2026-10-08). The send yes-gate
    (`common.send_gate`) holds in code in both modes.

Decided HERE, not by the model: what the case file contains, the call's goal
for this record (a confirmed payment is never pressed for money; a balance
under review is not chased), the instalment amounts, and the case reference
the agent gives for any follow-up.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from runtime.tools.agents_demo import data
from runtime.tools.agents_demo.common import (
    LOCKED_DETAIL, note_failed_attempt, parse_date, reference, second_factor_matches, send_gate,
)

NOTE_CATEGORIES = ("dispute", "hardship", "callback", "wrong_person", "other")
ESCALATION_REASONS = ("dispute", "unmatched_payment", "hardship", "identity_mismatch", "other")
MAX_ID_ATTEMPTS = 3

DirectTool = Tuple[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]

# The case file names the check, never its answers: those stay in code.
IDENTITY_LINE = ("Identity check: their date of birth OR the last 4 digits of their national ID, confirmed "
                 "ONLY by calling verify_identity with what they said — the right answers are not given to you.")


def new_state() -> Dict[str, Any]:
    return {"verified": False, "id_failures": 0, "links": [], "plans": [], "promises": [],
            "notes": [], "escalations": [], "asked_send": False}


def _lang(locale: Optional[str]) -> str:
    return "ar" if str(locale or "").lower().startswith("ar") else "en"


def case_reference(case: Dict[str, Any]) -> str:
    """Assigned when the case was opened — known before dialling, spoken from
    the case file, never minted by a tool mid-call."""
    return reference("COL", case["id"], case["customer_id"])


def record(case: Dict[str, Any]) -> Dict[str, Any]:
    """The collection record for the dialled customer, all figures computed here."""
    cust = data.customer_for_case(case)
    bills = cust["bills"]
    due = round(sum(b["amount"] for b in bills if b["status"] != "paid" or b.get("due_date")), 2)
    # Only bills that are (or were) in collection count: unpaid / partial,
    # plus a paid one that carried a due date (a late-settled balance).
    in_scope = [b for b in bills if b["status"] in ("unpaid", "partial") or
                (b["status"] == "paid" and b.get("due_date"))]
    due = round(sum(b["amount"] for b in in_scope), 2)
    paid = round(sum(p["amount"] for b in in_scope for p in b.get("payments", [])), 2)
    remaining = round(due - paid, 2)
    late_fees = round(sum(i["amount"] for b in in_scope for i in b["items"]
                          if "late" in i["label_en"].lower()), 2)
    pays = [p for b in in_scope for p in b.get("payments", [])]
    by_ref: Dict[str, Dict[str, Any]] = {}
    for p in pays:  # one payment can be split across bills — merge by reference
        if p["ref"] in by_ref:
            by_ref[p["ref"]]["amount"] = round(by_ref[p["ref"]]["amount"] + p["amount"], 2)
        else:
            by_ref[p["ref"]] = dict(p)
    min_plan = data.policies()["payment_plan_min_balance"][cust["country"]]
    n = data.policies()["payment_plan_max_instalments"]
    return {
        "customer": cust, "country": cust["country"],
        "months": [b["month"] for b in in_scope if b["status"] != "paid"] or [b["month"] for b in in_scope],
        "bills": in_scope, "amount_due": due, "amount_paid": paid, "remaining": remaining,
        "late_fees": late_fees, "payments": list(by_ref.values()),
        "status": "paid" if remaining <= 0 else ("partial" if paid > 0 else "open"),
        "plan_eligible": remaining >= min_plan, "plan_instalments": n,
        "plan_instalment_amount": round(remaining / n, 2) if remaining > 0 else 0,
    }


def _goal(case: Dict[str, Any], rec: Dict[str, Any], lang: str = "en") -> str:
    cust, country = rec["customer"], rec["country"]
    last4 = cust["mobile"][-4:]
    rem = data.money(rec["remaining"], country)
    if rec["status"] == "paid":
        p = rec["payments"][-1]
        return (f"This balance is already PAID and confirmed (reference {p['ref']}, "
                f"{data.money(p['amount'], country)} on {data.say_date(p['date'])}). After verification say "
                "the payment is confirmed and the case is closed, apologise for the call, and ask for nothing.")
    if rec["status"] == "partial":
        return (f"Partial payment: after verification say BOTH — {data.money(rec['amount_paid'], country)} already "
                f"paid and {rem} remaining — then ask to settle the rest today by a payment link sent by SMS "
                f"to the registered mobile ending {last4}.")
    return (f"After verification state the remaining {rem} and the months it covers in ONE sentence, then ask "
            f"to settle it today by a payment link sent by SMS to the registered mobile ending {last4}. If they "
            "cannot pay it all now: a promise to pay within 7 days, or — only if they mention hardship or "
            f"ask to split it — the {rec['plan_instalments']}-instalment plan.")


def case_file(case: Dict[str, Any], today: Optional[date] = None) -> str:
    """The pre-dial case file for one collection case, in plain text."""
    today = today or date.today()
    rec = record(case)
    cust, country = rec["customer"], rec["country"]
    area = data.area(cust["area_id"])
    cur = data.catalog()["currencies"][country]
    lines = [
        f"Today: {today.isoformat()} ({data.say_date(today.isoformat())}).",
        f"You are calling {cust['name_en']} («{cust['name_ar']}») — {area['name_en']}, {area['city_en']} "
        f"({cur['country_en']}; amounts are in {cur['word_en']}, «{cur['word_ar']}»).",
        IDENTITY_LINE,
        f"Registered mobile: {cust['mobile']} — its last four digits are {cust['mobile'][-4:]}.",
        "Services: " + "; ".join(data.products()[s["plan"]]["name_en"] for s in cust["services"]) + ".",
        f"Account status: {cust['status']}"
        + (" — service is RESTRICTED (speed reduced / outgoing calls barred) because the balance is over 30 "
           "days overdue; it lifts once the balance is paid." if cust["status"] == "restricted" else "."),
        f"Case reference: {case_reference(case)} — the only reference you ever give; every follow-up from "
        "this call is logged under it.",
    ]
    if cust.get("household_en"):
        lines.append(f"Household: {cust['household_en']}")
    bill_lines = []
    for b in rec["bills"]:
        items = ", ".join(f"{i['label_en']} {data._num(i['amount'])}" for i in b["items"])
        bill_lines.append(f"{data.month_name(b['month'], 'en')} («{data.month_name(b['month'], 'ar')}»): "
                          f"{data._num(b['amount'])} ({items}); status {b['status']}")
    lines.append("Bills in collection: " + "; ".join(bill_lines) + ".")
    lines.append(f"Totals: amount due {data.money(rec['amount_due'], country)}; already paid "
                 f"{data.money(rec['amount_paid'], country)}; REMAINING {data.money(rec['remaining'], country)}"
                 + (f" (includes {data.money(rec['late_fees'], country)} in late fees)" if rec["late_fees"] else "")
                 + ".")
    if rec["payments"]:
        lines.append("Payments on record (CONFIRMED): " + "; ".join(
            f"{p['ref']} — {data.money(p['amount'], country)} on {data.say_date(p['date'])} ({p['date']}), "
            f"{p['method_en']}" for p in rec["payments"]) + ".")
    else:
        lines.append("Payments on record: NONE.")
    if rec["plan_eligible"] and rec["remaining"] > 0:
        lines.append(f"Payment plan (only if they cannot pay in full): {rec['plan_instalments']} monthly "
                     f"instalments of {data.money(rec['plan_instalment_amount'], country)}, the first one today.")
    pol = data.policies()
    lines.append(f"Policy: a promise to pay may be at most {pol['promise_to_pay_max_days']} days out; a later "
                 "date is recorded but the team follows up per policy. Payment links go ONLY to the registered "
                 "mobile. No amount, fee or late fee can be waived or discounted on this call.")
    lines.append("What this call should do: " + _goal(case, rec))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The simulation's actions (shared by brain tools and direct voice tools)
# ---------------------------------------------------------------------------

_NOT_VERIFIED = {"ok": False, "error": "not_verified",
                 "detail": "Verify you are speaking with the customer first (date of birth or ID last 4). "
                           "Share nothing about the account before that."}


def verify_identity(state: Dict[str, Any], case: Dict[str, Any], *, date_of_birth: Optional[str] = None,
                    id_last4: Optional[str] = None, speaking_with: Optional[str] = None) -> Dict[str, Any]:
    cust = data.customer_for_case(case)
    if speaking_with and not any(data._fold(n) and data._fold(n) in data._fold(speaking_with)
                                 for n in (cust["first_en"], cust["first_ar"], cust["name_en"], cust["name_ar"])):
        return {"ok": False, "error": "not_the_customer",
                "detail": "You are not speaking with the customer. Disclose NOTHING — not the amount, not "
                          "that it is about a bill. Ask when they can be reached and log a wrong_person note."}
    if state.get("id_failures", 0) >= MAX_ID_ATTEMPTS:
        return {"ok": False, "error": "too_many_attempts", "detail": LOCKED_DETAIL}
    if not data.digits(date_of_birth) and not data.digits(id_last4):
        return {"ok": False, "error": "missing_detail",
                "detail": "Ask for their date of birth or the last 4 digits of their national ID."}
    if second_factor_matches(cust, date_of_birth, id_last4):
        state["verified"] = True
        return {"ok": True, "verified": True, "name": cust["name_en"], "name_ar": cust["name_ar"]}
    note_failed_attempt(state, "id_tried", counter="id_failures",
                        date_of_birth=date_of_birth, id_last4=id_last4)
    out = {"ok": False, "error": "details_do_not_match",
           "detail": "Does not match. Never say which part; ask them to check once more."}
    if state["id_failures"] >= MAX_ID_ATTEMPTS:
        out["next"] = "offer_callback"
    return out


def get_balance(state: Dict[str, Any], case: Dict[str, Any], *, locale: str = "en") -> Dict[str, Any]:
    if not state.get("verified"):
        return dict(_NOT_VERIFIED)
    lang = _lang(locale)
    rec = record(case)
    c = rec["country"]
    out = {
        "ok": True, "amount_due": data.money(rec["amount_due"], c, lang),
        "amount_paid": data.money(rec["amount_paid"], c, lang),
        "remaining": data.money(rec["remaining"], c, lang),
        "months": [data.month_name(m, lang) for m in rec["months"]],
        "bills": [{"month": data.month_name(b["month"], lang), "amount": data.money(b["amount"], c, lang),
                   "lines": [f"{i[f'label_{lang}']}: {data.money(i['amount'], c, lang)}" for i in b["items"]],
                   "status": b["status"]} for b in rec["bills"]],
        "status": rec["status"], "account_status": rec["customer"]["status"],
        "registered_mobile_last4": rec["customer"]["mobile"][-4:],
        "case_reference": case_reference(case),
    }
    if rec["late_fees"]:
        out["late_fees_included"] = data.money(rec["late_fees"], c, lang)
    if rec["plan_eligible"] and rec["remaining"] > 0:
        out["payment_plan_option"] = (f"{rec['plan_instalments']} monthly instalments of "
                                      f"{data.money(rec['plan_instalment_amount'], c, lang)}")
    return out


def payment_status(state: Dict[str, Any], case: Dict[str, Any], *, reference_given: Optional[str] = None,
                   paid_on: Optional[str] = None, locale: str = "en") -> Dict[str, Any]:
    if not state.get("verified"):
        return dict(_NOT_VERIFIED)
    lang = _lang(locale)
    rec = record(case)
    c = rec["country"]
    confirmed = [{"reference": p["ref"], "amount": data.money(p["amount"], c, lang),
                  "date": data.say_date(p["date"], lang)} for p in rec["payments"]]
    if rec["status"] == "paid":
        return {"ok": True, "state": "confirmed", "payments": confirmed, "case_state": "closed"}
    if reference_given or paid_on:
        ref_ok = reference_given and any(data.digits(reference_given) == data.digits(p["ref"]) for p in rec["payments"])
        if not ref_ok:
            return {"ok": True, "state": "not_found", "case_state": "open", "payments_on_record": confirmed,
                    "remaining": data.money(rec["remaining"], c, lang),
                    "detail": "No payment matching that is on record. Bank transfers can take up to 2 working "
                              "days to appear. Do not confirm anything; escalate as unmatched_payment."}
    if rec["status"] == "partial":
        return {"ok": True, "state": "partial", "payments": confirmed,
                "remaining": data.money(rec["remaining"], c, lang), "case_state": "open"}
    return {"ok": True, "state": "not_found", "case_state": "open",
            "detail": "No payment on record for this balance."}


def send_payment_link(state: Dict[str, Any], case: Dict[str, Any], *, mobile_last4: str,
                      instalment: bool = False) -> Dict[str, Any]:
    if not state.get("verified"):
        return dict(_NOT_VERIFIED)
    rec = record(case)
    last4 = rec["customer"]["mobile"][-4:]
    if rec["remaining"] <= 0:
        return {"ok": False, "error": "nothing_due", "detail": "Nothing is due — say so, ask for nothing."}
    if any(e["reason"] in ("dispute", "unmatched_payment") for e in state.get("escalations", [])):
        return {"ok": False, "error": "under_review",
                "detail": "This balance is under review — do not press for payment."}
    gate = send_gate(state, last4, what="payment link")
    if gate:
        return gate
    if data.digits(mobile_last4)[-4:] != last4:
        return {"ok": False, "error": "mobile_mismatch", "registered_mobile_last4": last4,
                "detail": "Links only go to the registered mobile; its last four digits are "
                          "registered_mobile_last4 — say those, never another number."}
    amount = rec["plan_instalment_amount"] if (instalment and state.get("plans")) else rec["remaining"]
    state["links"].append({"amount": amount})
    return {"ok": True, "sent_to": "••••" + last4, "amount": data.money(amount, rec["country"]),
            "detail": "Sent by SMS. The balance updates and the case closes once the payment is confirmed."}


def set_up_payment_plan(state: Dict[str, Any], case: Dict[str, Any], *, instalments: int = 3,
                        confirm: bool = False, today: Optional[date] = None) -> Dict[str, Any]:
    if not state.get("verified"):
        return dict(_NOT_VERIFIED)
    rec = record(case)
    c = rec["country"]
    if rec["remaining"] <= 0:
        return {"ok": False, "error": "nothing_due"}
    if not rec["plan_eligible"]:
        return {"ok": False, "error": "not_eligible",
                "detail": f"Plans start at a balance of {data.money(data.policies()['payment_plan_min_balance'][c], c)}."}
    n = int(instalments or 0)
    if n < 2 or n > rec["plan_instalments"]:
        return {"ok": False, "error": "invalid_instalments", "max_instalments": rec["plan_instalments"]}
    if state.get("plans"):
        return {"ok": False, "error": "already_set_up", "detail": "The plan is already in place on this call."}
    today = today or date.today()
    each = round(rec["remaining"] / n, 2)
    dates = [today + timedelta(days=30 * i) for i in range(n)]
    terms = {"instalments": n, "each": data.money(each, c),
             "dates": [data.say_date(d.isoformat()) for d in dates],
             "first_due": "today", "note": "Service restriction lifts once the first instalment is paid."}
    if not confirm:
        return {"ok": True, "done": False, "terms": terms,
                "detail": "Read these terms and ask for an explicit yes; then call again with confirm=true."}
    state["plans"].append({"instalments": n, "each": each})
    return {"ok": True, "done": True, "terms": terms, "case_reference": case_reference(case)}


def record_promise(state: Dict[str, Any], case: Dict[str, Any], *, promise_date: str,
                   today: Optional[date] = None) -> Dict[str, Any]:
    if not state.get("verified"):
        return dict(_NOT_VERIFIED)
    when = parse_date(promise_date)
    if when is None:
        return {"ok": False, "error": "invalid_date", "detail": "Use YYYY-MM-DD."}
    today = today or date.today()
    if when < today:
        return {"ok": False, "error": "date_in_past"}
    limit = data.policies()["promise_to_pay_max_days"]
    within = when <= today + timedelta(days=limit)
    state["promises"].append({"date": when.isoformat()})
    return {"ok": True, "recorded": True, "promise_date": data.say_date(when.isoformat()),
            "within_policy": within,
            "detail": (f"Within the {limit}-day window." if within else
                       f"Later than the {limit}-day window — recorded, and the team will follow up per policy.")}


def log_case_note(state: Dict[str, Any], case: Dict[str, Any], *, category: str, text: str) -> Dict[str, Any]:
    category = (category or "").strip().lower()
    if category not in NOTE_CATEGORIES:
        return {"ok": False, "error": "invalid_category", "allowed": list(NOTE_CATEGORIES)}
    # A callback / wrong-person note discloses nothing, so it works before
    # verification — it is how a call ends when the customer is unavailable.
    if category not in ("callback", "wrong_person") and not state.get("verified"):
        return dict(_NOT_VERIFIED)
    state["notes"].append({"category": category, "text": text})
    return {"ok": True, "logged": True, "category": category}


def escalate_for_review(state: Dict[str, Any], case: Dict[str, Any], *, reason: str,
                        detail: str = "") -> Dict[str, Any]:
    reason = (reason or "").strip().lower()
    if reason not in ESCALATION_REASONS:
        return {"ok": False, "error": "invalid_reason", "allowed": list(ESCALATION_REASONS)}
    if reason != "identity_mismatch" and not state.get("verified"):
        return dict(_NOT_VERIFIED)
    ref = case_reference(case)
    state["escalations"].append({"reason": reason, "detail": detail, "ref": ref})
    return {"ok": True, "escalated": True, "reason": reason, "case_reference": ref,
            "detail": "Raised for review under the case reference; the team follows up within 2 working days. "
                      "Collection on this balance pauses while it is reviewed."}


# ---------------------------------------------------------------------------
# Brain tools (text chat)
# ---------------------------------------------------------------------------

class VerifyInput(BaseModel):
    date_of_birth: Optional[str] = Field(None, description="Date of birth the person gave, YYYY-MM-DD.")
    id_last4: Optional[str] = Field(None, description="Last 4 digits of their national ID, if given instead.")
    speaking_with: Optional[str] = Field(None, description="The name of the person who answered, ONLY if they said they are someone else.")


class StatusInput(BaseModel):
    reference_given: Optional[str] = Field(None, description="Payment reference the customer gave, if any.")
    paid_on: Optional[str] = Field(None, description="Date they say they paid, YYYY-MM-DD, if any.")


class LinkInput(BaseModel):
    mobile_last4: str = Field(..., description="Last four digits of the REGISTERED mobile (from collection_get_balance).")
    instalment: bool = Field(False, description="true = link for the first plan instalment instead of the full balance.")


class PlanInput(BaseModel):
    instalments: int = Field(3, description="Number of monthly instalments (2 or 3).")
    confirm: bool = Field(False, description="false = get the terms to read out; true = set it up after an explicit yes.")


class PromiseInput(BaseModel):
    promise_date: str = Field(..., description="Date they promise to pay, YYYY-MM-DD.")


class NoteInput(BaseModel):
    category: str = Field(..., description="dispute | hardship | callback | wrong_person | other")
    text: str = Field(..., description="What was said, briefly (when to call back, what is disputed …).")


class EscalateInput(BaseModel):
    reason: str = Field(..., description="dispute | unmatched_payment | hardship | identity_mismatch | other")
    detail: str = Field("", description="One line of context for the reviewer.")


def _dump(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_collection_tools(context: Optional[Dict[str, Any]] = None) -> List[StructuredTool]:
    context = context or {}
    state = context.get("call_state")
    if state is None:
        state = new_state()
    case = data.resolve_case("collection", context.get("call_case_id"))
    locale = context.get("locale") or "en"

    def _verify(date_of_birth=None, id_last4=None, speaking_with=None):
        return _dump(verify_identity(state, case, date_of_birth=date_of_birth, id_last4=id_last4,
                                     speaking_with=speaking_with))

    def _balance():
        return _dump(get_balance(state, case, locale=locale))

    def _status(reference_given=None, paid_on=None):
        return _dump(payment_status(state, case, reference_given=reference_given, paid_on=paid_on, locale=locale))

    def _link(mobile_last4, instalment=False):
        return _dump(send_payment_link(state, case, mobile_last4=mobile_last4, instalment=instalment))

    def _plan(instalments=3, confirm=False):
        return _dump(set_up_payment_plan(state, case, instalments=instalments, confirm=confirm))

    def _promise(promise_date):
        return _dump(record_promise(state, case, promise_date=promise_date))

    def _note(category, text):
        return _dump(log_case_note(state, case, category=category, text=text))

    def _escalate(reason, detail=""):
        return _dump(escalate_for_review(state, case, reason=reason, detail=detail))

    return [
        StructuredTool.from_function(
            func=_verify, name="collection_verify_identity", args_schema=VerifyInput,
            description=("Check you are speaking with the customer you called: their date of birth or ID last 4. "
                         "MUST succeed before any amount, month or detail is said. If someone else answered, "
                         "pass speaking_with.")),
        StructuredTool.from_function(
            func=_balance, name="collection_get_balance",
            description=("The verified customer's overdue balance: amount due, paid, remaining, months, bill lines, "
                         "the registered mobile's last 4, the payment-plan option and the case reference.")),
        StructuredTool.from_function(
            func=_status, name="collection_payment_status", args_schema=StatusInput,
            description=("Whether a payment is on record (confirmed / partial / not_found). The ONLY basis for "
                         "saying anything is paid or closed.")),
        StructuredTool.from_function(
            func=_link, name="collection_send_payment_link", args_schema=LinkInput,
            description=("Send the payment link by SMS to the REGISTERED mobile. Only after the customer said an "
                         "explicit yes to your question naming that mobile's last four digits. Once per call.")),
        StructuredTool.from_function(
            func=_plan, name="collection_set_up_payment_plan", args_schema=PlanInput,
            description=("Instalment plan for a customer who cannot pay in full (hardship / asks to split): "
                         "confirm=false for the terms, confirm=true after their yes.")),
        StructuredTool.from_function(
            func=_promise, name="collection_record_promise", args_schema=PromiseInput,
            description="Record the date the customer promises to pay."),
        StructuredTool.from_function(
            func=_note, name="collection_log_note", args_schema=NoteInput,
            description=("Log a note: dispute, hardship, callback request, or wrong_person (someone else answered). "
                         "callback and wrong_person work before verification.")),
        StructuredTool.from_function(
            func=_escalate, name="collection_escalate", args_schema=EscalateInput,
            description=("Raise the case for human review (disputed charge, payment not on record, hardship beyond "
                         "the plan, identity mismatch). Returns the case reference to give the customer.")),
    ]


# ---------------------------------------------------------------------------
# Direct voice tools (pre-fetched context)
# ---------------------------------------------------------------------------

def _decl(name: str, description: str, props: Dict[str, Dict[str, Any]], required: List[str]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}


def _str(description: str, enum: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "string", "description": description}
    if enum:
        out["enum"] = list(enum)
    return out


def verify_declaration() -> Dict[str, Any]:
    return _decl("verify_identity",
                 "Check the identity details the person just gave against the account. Call it as soon as they "
                 "give a date of birth or ID digits, BEFORE saying anything about them; only verified true "
                 "means they are verified.",
                 {"date_of_birth": _str("Date of birth they said, as YYYY-MM-DD."),
                  "id_last4": _str("Last 4 digits of their national ID, as they said them.")}, [])


def direct_tools(case: Dict[str, Any], state: Dict[str, Any]) -> List[DirectTool]:
    """The collection call's tools — identity check + actions — bound to this call's state."""
    last4 = data.customer_for_case(case)["mobile"][-4:]

    def _verify(a: Dict[str, Any]) -> Dict[str, Any]:
        return verify_identity(state, case, date_of_birth=a.get("date_of_birth") or None,
                               id_last4=a.get("id_last4") or None)

    def _link(a: Dict[str, Any]) -> Dict[str, Any]:
        return send_payment_link(state, case, mobile_last4=str(a.get("mobile_last4") or last4),
                                 instalment=str(a.get("for_instalment") or "").lower() in ("true", "yes", "1"))

    def _plan(a: Dict[str, Any]) -> Dict[str, Any]:
        return set_up_payment_plan(state, case, instalments=int(a.get("instalments") or 3), confirm=True)

    def _promise(a: Dict[str, Any]) -> Dict[str, Any]:
        return record_promise(state, case, promise_date=str(a.get("promise_date") or ""))

    def _note(a: Dict[str, Any]) -> Dict[str, Any]:
        return log_case_note(state, case, category=str(a.get("category") or ""), text=str(a.get("text") or ""))

    def _escalate(a: Dict[str, Any]) -> Dict[str, Any]:
        return escalate_for_review(state, case, reason=str(a.get("reason") or ""), detail=str(a.get("detail") or ""))

    return [
        (verify_declaration(), _verify),
        (_decl("send_payment_link",
               "Send the payment link by SMS to the customer's REGISTERED mobile. Call it only after the "
               "customer said yes to receiving it.",
               {"mobile_last4": _str("Last four digits of the registered mobile, from the case file."),
                "for_instalment": _str("'true' to send the first instalment of an agreed plan instead of the full balance.")},
               ["mobile_last4"]), _link),
        (_decl("set_up_payment_plan",
               "Set up the instalment plan from the case file, after the customer agreed to its terms.",
               {"instalments": _str("Number of instalments, as agreed.", ["2", "3"])},
               ["instalments"]), _plan),
        (_decl("record_promise_to_pay",
               "Record the date the customer promised to pay.",
               {"promise_date": _str("YYYY-MM-DD")}, ["promise_date"]), _promise),
        (_decl("log_case_note",
               "Log a note on the case: dispute, hardship, call-back request, or that someone else answered.",
               {"category": _str("Kind of note", list(NOTE_CATEGORIES)),
                "text": _str("What was said, briefly.")}, ["category", "text"]), _note),
        (_decl("escalate_for_review",
               "Raise the case for human review under the case reference.",
               {"reason": _str("Why", list(ESCALATION_REASONS)),
                "detail": _str("One line of context for the reviewer.")}, ["reason"]), _escalate),
    ]
