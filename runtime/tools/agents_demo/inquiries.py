"""Inbound BUSINESS-INQUIRY tools — catalog, coverage, comparison, leads.

No identity is needed: prospects ask about public plans and coverage. The
rules enforced HERE:
  * prices come only from the catalog (`inquiry_list_plans` /
    `inquiry_compare_plans`); anything not in it is `not_offered`, so there is
    no discount, TV package or student price to quote;
  * coverage comes only from the area table — an area it does not know is
    `unknown_area`, never a guess;
  * a lead needs a name and a well-formed mobile; a callback needs a lead and
    a date within the next 14 days. Nothing is sent or stored anywhere real.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from runtime.tools.agents_demo import data
from runtime.tools.agents_demo.common import parse_date, reference

CATEGORIES = ("home", "mobile", "business", "device", "addon")
_CATEGORY_MATCH = {
    "home": ("home",), "mobile": ("mobile",), "business": ("business", "business_addon"),
    "device": ("device",), "addon": ("home_addon", "mobile_addon", "business_addon"),
}


def new_state() -> Dict[str, Any]:
    return {"leads": [], "callbacks": []}


def _lang(locale: Optional[str]) -> str:
    return "ar" if str(locale or "").lower().startswith("ar") else "en"


def _country(value: Optional[str]) -> Optional[str]:
    v = data._fold(value or "")
    if not v:
        return None
    if v in ("ae", "uae", "emirates", "united arab emirates", "الامارات", "الإمارات") or "امارات" in v \
            or "emirat" in v or "dubai" in v or "abu dhabi" in v or "sharjah" in v:
        return "AE"
    if v in ("sa", "ksa", "saudi", "saudi arabia", "السعوديه", "السعودية") or "سعودي" in v or "saudi" in v \
            or "riyadh" in v or "jeddah" in v:
        return "SA"
    hit = data.find_area(value)
    return hit["country"] if hit else None


def _product_view(pid: str, p: Dict[str, Any], country: Optional[str], lang: str) -> Dict[str, Any]:
    countries = [country] if country else ["AE", "SA"]
    view: Dict[str, Any] = {
        "id": pid, "name": p[f"name_{lang}"], "what_you_get": p[f"summary_{lang}"],
        "best_for": p[f"best_for_{lang}"],
        "monthly_price": {c: data.money(p["price"][c], c, lang) for c in countries},
    }
    if p.get(f"price_unit_{lang}"):
        view["price_unit"] = p[f"price_unit_{lang}"]
    months = p.get("contract_months") or [0]
    view["contract"] = ("no contract" if months == [0] else
                        " or ".join(f"{m} months" if m else "no contract" for m in months))
    if p.get("price_24m_less"):
        view["price_on_24_month_contract"] = {
            c: data.money(p["price"][c] - p["price_24m_less"], c, lang) for c in countries}
    return view


def list_plans(*, category: Optional[str] = None, country: Optional[str] = None,
               locale: str = "en") -> Dict[str, Any]:
    lang = _lang(locale)
    c = _country(country)
    cat = (category or "").strip().lower()
    if cat and cat not in CATEGORIES:
        folded = data._fold(cat)
        nope = next((n for n in data.catalog()["not_offered"]
                     if any(w in data._fold(n["en"]) or w in data._fold(n["ar"]) for w in folded.split())), None)
        return {"ok": False, "error": "not_offered" if nope else "unknown_category",
                "not_offered": [n[lang] for n in data.catalog()["not_offered"]],
                "categories": list(CATEGORIES),
                "detail": "Say plainly it is not something we offer; never describe or price it."}
    kinds = _CATEGORY_MATCH.get(cat) if cat else None
    plans = [_product_view(pid, p, c, lang) for pid, p in data.products().items()
             if kinds is None or p["category"] in kinds]
    return {"ok": True, "country": c or "both", "plans": plans,
            "not_offered": [n[lang] for n in data.catalog()["not_offered"]],
            "note": "These are the only plans and prices that exist; there are no discounts or promo codes."}


def check_coverage(*, area: str, locale: str = "en") -> Dict[str, Any]:
    lang = _lang(locale)
    hit = data.find_area(area)
    if hit is None:
        tracked = sorted({f"{a[f'name_{lang}']} ({a[f'city_{lang}']})" for a in data.areas().values()})
        return {"ok": False, "error": "unknown_area", "areas_we_track": tracked,
                "detail": "We have no coverage record for that area — never guess. Offer to take their "
                          "details so the team checks the exact address."}
    pol = data.policies()
    out: Dict[str, Any] = {
        "ok": True, "area": f"{hit[f'name_{lang}']}, {hit[f'city_{lang}']}", "country": hit["country"],
        "fiber": hit["fiber"], "wireless_5g": hit["wireless_5g"],
    }
    if hit["fiber"]:
        out["fiber_installation"] = f"{pol['fiber_install_working_days']} working days after ordering"
    elif hit.get(f"fiber_planned_{lang}"):
        out["fiber_planned"] = hit[f"fiber_planned_{lang}"]
    if hit["wireless_5g"]:
        out["wireless_5g_delivery"] = pol["wireless_delivery"]
    out["available_home_plans"] = [data.products()[p][f"name_{lang}"] for p in
                                   (("home_fiber_300", "home_fiber_500", "home_fiber_1g") if hit["fiber"] else ())
                                   + (("home_5g_wireless",) if hit["wireless_5g"] else ())]
    outage = [o for o in data.outages() if o["area_id"] == hit["id"]]
    if outage:
        out["current_outage"] = outage[0][f"summary_{lang}"]
    return out


def compare_plans(*, plan_ids: List[str], country: Optional[str] = None, locale: str = "en") -> Dict[str, Any]:
    lang = _lang(locale)
    ids = [str(p).strip().lower() for p in (plan_ids or []) if str(p).strip()]
    unknown = [p for p in ids if p not in data.products()]
    if unknown or len(ids) < 2:
        return {"ok": False, "error": "unknown_plan" if unknown else "need_two_plans", "unknown": unknown,
                "plan_ids": list(data.products())}
    c = _country(country) or "AE"
    views = [_product_view(pid, data.products()[pid], c, lang) for pid in ids]
    prices = [data.products()[pid]["price"][c] for pid in ids]
    out = {"ok": True, "country": c, "plans": views}
    if len(ids) == 2:
        diff = abs(prices[1] - prices[0])
        cheaper = views[0]["name"] if prices[0] <= prices[1] else views[1]["name"]
        out["monthly_difference"] = data.money(diff, c, lang)
        out["cheaper"] = cheaper
    return out


def capture_lead(state: Dict[str, Any], *, name: str, mobile: str, interest: str,
                 area: Optional[str] = None) -> Dict[str, Any]:
    if not (name or "").strip():
        return {"ok": False, "error": "missing_name"}
    mob = data.normalize_mobile(mobile)
    if not (len(mob) == 10 and mob.startswith("05")):
        return {"ok": False, "error": "invalid_mobile", "digits_received": len(data.digits(mobile)),
                "detail": "A mobile here is 05 followed by 8 digits. Read back what you heard and ask again."}
    if state.get("leads"):
        lead = state["leads"][0]
        lead.update(name=name.strip(), mobile=mob, interest=interest, area=area)
        return {"ok": True, "updated": True, "reference": lead["ref"]}
    ref = reference("LEAD", mob, interest)
    state["leads"].append({"ref": ref, "name": name.strip(), "mobile": mob, "interest": interest, "area": area})
    return {"ok": True, "reference": ref, "mobile_last4": mob[-4:],
            "detail": "Lead saved for the sales team."}


def book_callback(state: Dict[str, Any], *, callback_date: str, callback_time: Optional[str] = None,
                  topic: str = "", today: Optional[date] = None) -> Dict[str, Any]:
    if not state.get("leads"):
        return {"ok": False, "error": "no_lead",
                "detail": "Take their name and mobile first (inquiry_capture_lead)."}
    if state.get("callbacks"):
        return {"ok": False, "error": "already_booked", "reference": state["callbacks"][0]["ref"]}
    when = parse_date(callback_date)
    today = today or date.today()
    if when is None:
        return {"ok": False, "error": "invalid_date", "detail": "Use YYYY-MM-DD."}
    if when < today:
        return {"ok": False, "error": "date_in_past"}
    if when > today + timedelta(days=data.policies()["offer_followup_max_days"]):
        return {"ok": False, "error": "too_far", "detail": "Callbacks are booked within the next 14 days."}
    lead = state["leads"][0]
    ref = reference("CB", lead["mobile"], when.isoformat())
    state["callbacks"].append({"ref": ref, "date": when.isoformat(), "time": callback_time, "topic": topic})
    return {"ok": True, "reference": ref, "date": data.say_date(when.isoformat()), "time": callback_time,
            "to_mobile_ending": lead["mobile"][-4:]}


# ---------------------------------------------------------------------------
# LangChain wrappers
# ---------------------------------------------------------------------------

class PlansInput(BaseModel):
    category: Optional[str] = Field(None, description="home | mobile | business | device | addon — or what the prospect asked for (e.g. 'tv'); omit for everything.")
    country: Optional[str] = Field(None, description="UAE or Saudi Arabia (or a city), when known — prices differ by country.")


class CoverageInput(BaseModel):
    area: str = Field(..., description="Area / neighbourhood (and city) the prospect named.")


class CompareInput(BaseModel):
    plan_ids: List[str] = Field(..., description="Two or more plan ids from inquiry_list_plans, e.g. ['home_fiber_500', 'home_fiber_1g'].")
    country: Optional[str] = Field(None, description="UAE or Saudi Arabia (or a city).")


class LeadInput(BaseModel):
    name: str = Field(..., description="The prospect's name as they gave it.")
    mobile: str = Field(..., description="Their mobile, digits as dictated and read back.")
    interest: str = Field(..., description="What they want, in a few words (e.g. 'SME bundle for a café').")
    area: Optional[str] = Field(None, description="Their area / city, if known.")


class CallbackInput(BaseModel):
    callback_date: str = Field(..., description="YYYY-MM-DD, within the next 14 days.")
    callback_time: Optional[str] = Field(None, description="Time of day if given, e.g. 16:00.")
    topic: str = Field("", description="What the call is about.")


def _dump(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_inquiries_tools(context: Optional[Dict[str, Any]] = None) -> List[StructuredTool]:
    context = context or {}
    state = context.get("call_state")
    if state is None:
        state = new_state()
    locale = context.get("locale") or "en"

    def _plans(category=None, country=None):
        return _dump(list_plans(category=category, country=country, locale=locale))

    def _coverage(area):
        return _dump(check_coverage(area=area, locale=locale))

    def _compare(plan_ids, country=None):
        return _dump(compare_plans(plan_ids=plan_ids, country=country, locale=locale))

    def _lead(name, mobile, interest, area=None):
        return _dump(capture_lead(state, name=name, mobile=mobile, interest=interest, area=area))

    def _callback(callback_date, callback_time=None, topic=""):
        return _dump(book_callback(state, callback_date=callback_date, callback_time=callback_time, topic=topic))

    return [
        StructuredTool.from_function(
            func=_plans, name="inquiry_list_plans", args_schema=PlansInput,
            description=("The plan catalog with monthly prices per country, what each includes and who it fits, "
                         "plus what we do NOT offer. The ONLY source for any plan or price you mention.")),
        StructuredTool.from_function(
            func=_coverage, name="inquiry_check_coverage", args_schema=CoverageInput,
            description="Fiber / 5G wireless coverage for an area, install or delivery time, and plans available there."),
        StructuredTool.from_function(
            func=_compare, name="inquiry_compare_plans", args_schema=CompareInput,
            description="Side-by-side comparison of plans with the exact monthly price difference."),
        StructuredTool.from_function(
            func=_lead, name="inquiry_capture_lead", args_schema=LeadInput,
            description="Save the prospect as a lead for the sales team (name + mobile read back first)."),
        StructuredTool.from_function(
            func=_callback, name="inquiry_book_callback", args_schema=CallbackInput,
            description="Book a callback from the sales team for a captured lead, within the next 14 days."),
    ]
