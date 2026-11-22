"""Outbound SALES call — brain tools, pre-dial case file, direct voice tools.

The agent calls an existing customer with one relevant offer. Same two modes
as collection (see collection.py): a tool-using brain for text, or a
pre-fetched case file + instant action tools for voice.

Rules enforced HERE:
  * verify first, in BOTH modes — the customer profile and the offer link
    refuse until the identity check (`offer_verify_identity` in text,
    `verify_identity` in voice) matched date of birth or ID last 4; the case
    file never carries those answers; public catalog facts work any time;
  * catalog only — an unknown product is `unknown_product` with the list of
    what is not offered; no tool ever returns a discount, so there is none to
    give;
  * a product the customer already has is refused (`already_has`);
  * the send yes-gate and once-only link (common.send_gate);
  * loop-breakers: the not-interested counter (`next`: one_value_line, then
    end_call) and do-not-call, which ends the pitch for good.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from runtime.tools.agents_demo import data
from runtime.tools.agents_demo.collection import IDENTITY_LINE, verify_declaration
from runtime.tools.agents_demo.common import parse_date, reference, send_gate

OUTCOMES = ("interested", "not_interested", "already_has", "callback", "do_not_call")
MAX_ID_ATTEMPTS = 3

DirectTool = Tuple[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]


def new_state() -> Dict[str, Any]:
    return {"verified": False, "id_failures": 0, "links": [], "followups": [], "outcomes": [],
            "not_interested": 0, "do_not_call": False, "asked_send": False}


def _lang(locale: Optional[str]) -> str:
    return "ar" if str(locale or "").lower().startswith("ar") else "en"


def _norm_pid(value: Optional[str]) -> str:
    return (value or "").strip().lower().replace(" ", "_").replace("-", "_")


def has_products(cust: Dict[str, Any]) -> List[str]:
    """Plans the customer already uses (mesh units come with 1 Gig)."""
    have = [s["plan"] for s in cust["services"]]
    if "home_fiber_1g" in have:
        have.append("mesh_wifi_addon_included_units")
    return have


def offer_reference(case: Dict[str, Any]) -> str:
    return reference("OFR", case["id"], case["customer_id"])


def _price_line(pid: str, country: str, lang: str = "en") -> str:
    p = data.products()[pid]
    price = data.money(p["price"][country], country, lang)
    unit = p.get(f"price_unit_{lang}") or ("a month" if lang == "en" else "شهرياً")
    out = f"{price} {unit}"
    if p.get("price_24m_less"):
        less = data.money(p["price"][country] - p["price_24m_less"], country, lang)
        out += (f" on a 12-month contract, or {less} a month on a 24-month contract" if lang == "en"
                else f" بعقد 12 شهر، أو {less} شهرياً بعقد 24 شهر")
    return out


def case_file(case: Dict[str, Any], today: Optional[date] = None) -> str:
    """The pre-dial case file for one sales case, in plain text."""
    today = today or date.today()
    cust = data.customer_for_case(case)
    country = cust["country"]
    cur = data.catalog()["currencies"][country]
    area = data.area(cust["area_id"])
    offer_id = case["offer"]
    offer = data.products()[offer_id]
    lines = [
        f"Today: {today.isoformat()} ({data.say_date(today.isoformat())}).",
        f"You are calling {cust['name_en']} («{cust['name_ar']}») — {area['name_en']}, {area['city_en']} "
        f"({cur['country_en']}; prices are in {cur['word_en']}, «{cur['word_ar']}»).",
        IDENTITY_LINE,
        f"Registered mobile: {cust['mobile']} — its last four digits are {cust['mobile'][-4:]}.",
        "Services they already have: " + "; ".join(
            f"{data.products()[s['plan']]['name_en']} ({data.money(data.price_of(s['plan'], country), country)} a month)"
            for s in cust["services"]) + ".",
        "What we know about them: " + "; ".join(case.get("signals_en", [])) + " — in Arabic: "
        + "؛ ".join(case.get("signals_ar", [])) + ".",
        f"THE OFFER for this call: {offer['name_en']} («{offer['name_ar']}», id {offer_id}) — "
        f"{data.summary(offer, 'en', country)} Price: {_price_line(offer_id, country)}. Arabic: {data.summary(offer, 'ar', country)} "
        f"السعر: {_price_line(offer_id, country, 'ar')}.",
    ]
    if offer_id == "family_addon":
        each = offer["price"][country]
        lines.append("Family lines priced (the only totals you may say): " + "; ".join(
            f"{n} line{'s' if n > 1 else ''} = {data.money(each * n, country)} a month"
            for n in range(1, offer["max_lines"] + 1)) + ".")
    lines.append("Other catalog products (mention only if they ask, never more than one alternative):")
    for pid, p in data.products().items():
        if pid == offer_id or p["category"] in ("business", "business_addon") and offer["category"] != "business":
            continue
        lines.append(f"- {p['name_en']} («{p['name_ar']}», id {pid}): {data.summary(p, 'en', country)} "
                     f"Price: {_price_line(pid, country)}.")
    lines.append("Not offered at all: " + ", ".join(n["en"] for n in data.catalog()["not_offered"])
                 + ". There is NO discount, promo code or loyalty price on this call.")
    lines.append(f"Offer reference: {offer_reference(case)} — the only reference you ever give.")
    lines.append(
        "What this call should do: after verification, ask ONE short question about the need the signals "
        f"point to, then offer {offer['name_en']} in one sentence tied to their answer. If interested, ask "
        f"whether to send the details and sign-up link by SMS to the mobile ending {cust['mobile'][-4:]}; "
        f"send only after their yes. A call-back may be booked within {data.policies()['offer_followup_max_days']} days.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

_NOT_VERIFIED = {"ok": False, "error": "not_verified",
                 "detail": "Verify you are speaking with the customer first (date of birth or ID last 4)."}


def verify_identity(state: Dict[str, Any], case: Dict[str, Any], *, date_of_birth: Optional[str] = None,
                    id_last4: Optional[str] = None) -> Dict[str, Any]:
    from runtime.tools.agents_demo.common import LOCKED_DETAIL, note_failed_attempt, second_factor_matches

    cust = data.customer_for_case(case)
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
    return {"ok": False, "error": "details_do_not_match",
            "detail": "Does not match; never say which part. Ask once more or offer another time."}


def customer_profile(state: Dict[str, Any], case: Dict[str, Any], *, locale: str = "en") -> Dict[str, Any]:
    if not state.get("verified"):
        return dict(_NOT_VERIFIED)
    lang = _lang(locale)
    cust = data.customer_for_case(case)
    c = cust["country"]
    offer = data.products()[case["offer"]]
    return {"ok": True,
            "current_services": [f"{data.products()[s['plan']][f'name_{lang}']} — "
                                 f"{data.money(data.price_of(s['plan'], c), c, lang)}" for s in cust["services"]],
            "signals": case.get(f"signals_{lang}") or case.get("signals_en", []),
            "recommended_offer": {"id": case["offer"], "name": offer[f"name_{lang}"],
                                  "what_you_get": offer[f"summary_{lang}"], "price": _price_line(case["offer"], c, lang)},
            "registered_mobile_last4": cust["mobile"][-4:],
            "offer_reference": offer_reference(case)}


def catalog_lookup(case: Dict[str, Any], *, product_id: Optional[str] = None, locale: str = "en") -> Dict[str, Any]:
    lang = _lang(locale)
    c = data.customer_for_case(case)["country"]
    pid = _norm_pid(product_id)
    if not pid:
        return {"ok": True, "products": [{"id": k, "name": p[f"name_{lang}"], "price": _price_line(k, c, lang)}
                                         for k, p in data.products().items()],
                "not_offered": [n[lang] for n in data.catalog()["not_offered"]],
                "note": "No discounts or promo codes exist."}
    p = data.products().get(pid)
    if p is None:
        return {"ok": False, "error": "unknown_product", "available": list(data.products()),
                "not_offered": [n[lang] for n in data.catalog()["not_offered"]],
                "detail": "Not something we offer — say so; never describe or price it."}
    return {"ok": True, "id": pid, "name": p[f"name_{lang}"], "what_you_get": p[f"summary_{lang}"],
            "best_for": p[f"best_for_{lang}"], "price": _price_line(pid, c, lang),
            "note": "This is the only price; there is no discount."}


def send_offer_link(state: Dict[str, Any], case: Dict[str, Any], *, product_id: str,
                    mobile_last4: str) -> Dict[str, Any]:
    if not state.get("verified"):
        return dict(_NOT_VERIFIED)
    if state.get("do_not_call"):
        return {"ok": False, "error": "do_not_call", "detail": "They asked not to be contacted — send nothing."}
    cust = data.customer_for_case(case)
    last4 = cust["mobile"][-4:]
    pid = _norm_pid(product_id) or case["offer"]
    if pid not in data.products():
        return {"ok": False, "error": "unknown_product", "available": list(data.products())}
    if pid in has_products(cust):
        return {"ok": False, "error": "already_has", "detail": "They already have this — do not send it."}
    gate = send_gate(state, last4, what="offer details and sign-up link")
    if gate:
        return gate
    if data.digits(mobile_last4)[-4:] != last4:
        return {"ok": False, "error": "mobile_mismatch", "registered_mobile_last4": last4,
                "detail": "Details only go to the registered mobile ending registered_mobile_last4."}
    state["links"].append({"product": pid})
    return {"ok": True, "sent_to": "••••" + last4, "product": data.products()[pid]["name_en"],
            "offer_reference": offer_reference(case),
            "detail": "Sent by SMS; signing up from the link is their choice — nothing changes until they do."}


def book_followup(state: Dict[str, Any], case: Dict[str, Any], *, followup_date: str,
                  followup_time: Optional[str] = None, today: Optional[date] = None) -> Dict[str, Any]:
    if state.get("do_not_call"):
        return {"ok": False, "error": "do_not_call"}
    when = parse_date(followup_date)
    if when is None:
        return {"ok": False, "error": "invalid_date", "detail": "Use YYYY-MM-DD."}
    today = today or date.today()
    if when < today:
        return {"ok": False, "error": "date_in_past"}
    limit = data.policies()["offer_followup_max_days"]
    if when > today + timedelta(days=limit):
        return {"ok": False, "error": "too_far", "detail": f"Follow-ups are within {limit} days."}
    if state.get("followups"):
        return {"ok": False, "error": "already_booked", "detail": "A call-back is already booked on this call."}
    state["followups"].append({"date": when.isoformat(), "time": followup_time})
    return {"ok": True, "booked": True, "date": data.say_date(when.isoformat()),
            "weekday": when.strftime("%A"), "time": followup_time}


def log_outcome(state: Dict[str, Any], case: Dict[str, Any], *, outcome: str, note: str = "") -> Dict[str, Any]:
    outcome = (outcome or "").strip().lower()
    if outcome not in OUTCOMES:
        return {"ok": False, "error": "invalid_outcome", "allowed": list(OUTCOMES)}
    state["outcomes"].append({"outcome": outcome, "note": note})
    out: Dict[str, Any] = {"ok": True, "logged": True, "outcome": outcome}
    if outcome == "not_interested":
        state["not_interested"] = state.get("not_interested", 0) + 1
        out["next"] = "one_value_line" if state["not_interested"] == 1 else "end_call"
    if outcome == "do_not_call":
        state["do_not_call"] = True
        out["next"] = "end_call"
        out["detail"] = "Recorded: no more sales calls. Apologise, confirm it, and end the call — no pitch."
    return out


# ---------------------------------------------------------------------------
# Brain tools (text chat)
# ---------------------------------------------------------------------------

class VerifyInput(BaseModel):
    date_of_birth: Optional[str] = Field(None, description="Date of birth they gave, YYYY-MM-DD.")
    id_last4: Optional[str] = Field(None, description="Last 4 digits of their national ID, if given instead.")


class CatalogInput(BaseModel):
    product_id: Optional[str] = Field(None, description="A product id or what they asked about; omit for the whole catalog.")


class OfferLinkInput(BaseModel):
    product_id: str = Field(..., description="Product id of the offer.")
    mobile_last4: str = Field(..., description="Last four digits of the REGISTERED mobile (from offer_customer_profile).")


class FollowupInput(BaseModel):
    followup_date: str = Field(..., description="YYYY-MM-DD, within the next 14 days.")
    followup_time: Optional[str] = Field(None, description="Time of day if given, e.g. 16:00.")


class OutcomeInput(BaseModel):
    outcome: str = Field(..., description="interested | not_interested | already_has | callback | do_not_call")
    note: str = Field("", description="What they said, briefly.")


def _dump(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_sales_tools(context: Optional[Dict[str, Any]] = None) -> List[StructuredTool]:
    context = context or {}
    state = context.get("call_state")
    if state is None:
        state = new_state()
    case = data.resolve_case("sales", context.get("call_case_id"))
    locale = context.get("locale") or "en"

    def _verify(date_of_birth=None, id_last4=None):
        return _dump(verify_identity(state, case, date_of_birth=date_of_birth, id_last4=id_last4))

    def _profile():
        return _dump(customer_profile(state, case, locale=locale))

    def _catalog(product_id=None):
        return _dump(catalog_lookup(case, product_id=product_id, locale=locale))

    def _link(product_id, mobile_last4):
        return _dump(send_offer_link(state, case, product_id=product_id, mobile_last4=mobile_last4))

    def _followup(followup_date, followup_time=None):
        return _dump(book_followup(state, case, followup_date=followup_date, followup_time=followup_time))

    def _outcome(outcome, note=""):
        return _dump(log_outcome(state, case, outcome=outcome, note=note))

    return [
        StructuredTool.from_function(
            func=_verify, name="offer_verify_identity", args_schema=VerifyInput,
            description="Check you are speaking with the customer you called (date of birth or ID last 4)."),
        StructuredTool.from_function(
            func=_profile, name="offer_customer_profile",
            description=("After verification: their current services and prices, what we know about their needs, "
                         "the recommended offer with its exact price, and the registered mobile's last 4.")),
        StructuredTool.from_function(
            func=_catalog, name="offer_catalog", args_schema=CatalogInput,
            description=("Catalog facts and the exact price of a product (works before verification). Unknown "
                         "products come back as not offered. The ONLY source for any price.")),
        StructuredTool.from_function(
            func=_link, name="offer_send_link", args_schema=OfferLinkInput,
            description=("Send the offer details and sign-up link by SMS to the REGISTERED mobile — only after an "
                         "explicit yes to your question naming its last four digits. Once per call.")),
        StructuredTool.from_function(
            func=_followup, name="offer_book_followup", args_schema=FollowupInput,
            description="Book the call-back they asked for, within the next 14 days."),
        StructuredTool.from_function(
            func=_outcome, name="offer_log_outcome", args_schema=OutcomeInput,
            description=("Record how the offer landed once it is clear. not_interested twice or do_not_call means "
                         "end the call warmly.")),
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


def direct_tools(case: Dict[str, Any], state: Dict[str, Any]) -> List[DirectTool]:
    """The sales call's tools — identity check + actions — bound to this call's state."""
    last4 = data.customer_for_case(case)["mobile"][-4:]

    def _verify(a: Dict[str, Any]) -> Dict[str, Any]:
        return verify_identity(state, case, date_of_birth=a.get("date_of_birth") or None,
                               id_last4=a.get("id_last4") or None)

    def _link(a: Dict[str, Any]) -> Dict[str, Any]:
        return send_offer_link(state, case, product_id=str(a.get("product_id") or case["offer"]),
                               mobile_last4=str(a.get("mobile_last4") or last4))

    def _followup(a: Dict[str, Any]) -> Dict[str, Any]:
        return book_followup(state, case, followup_date=str(a.get("followup_date") or ""),
                             followup_time=a.get("followup_time"))

    def _outcome(a: Dict[str, Any]) -> Dict[str, Any]:
        return log_outcome(state, case, outcome=str(a.get("outcome") or ""), note=str(a.get("note") or ""))

    return [
        (verify_declaration(), _verify),
        (_decl("send_offer_link",
               "Send the offer details and sign-up link by SMS to the customer's REGISTERED mobile. Call it "
               "only after the customer said yes to receiving them.",
               {"product_id": _str("Product id from the case file", list(data.products())),
                "mobile_last4": _str("Last four digits of the registered mobile, from the case file.")},
               ["product_id", "mobile_last4"]), _link),
        (_decl("book_followup",
               "Book the call-back the customer asked for.",
               {"followup_date": _str("YYYY-MM-DD"), "followup_time": _str("Time of day if given, e.g. 16:00")},
               ["followup_date"]), _followup),
        (_decl("log_outcome",
               "Record how the offer landed, once it is clear.",
               {"outcome": _str("Outcome", list(OUTCOMES)), "note": _str("What they said, briefly.")},
               ["outcome"]), _outcome),
    ]
