"""Prompt-only INBOUND agents (support, inquiries): the facts sheet and the
per-call case file.

The inbound agents used to delegate every turn to a brain that called tools
(verify, outage, diagnostics, bill, KB search, catalog…) — 2–4 s per answer.
Nothing real sits behind those tools: it is a simulation over fixtures. So
everything a tool would have returned is rendered HERE, before the call, into
two blocks the prompt carries:

  * the FACTS SHEET — the whole public catalog for one language: plans and
    prices per country, coverage per area, today's network notices, fees,
    policies and the handful of how-to steps callers ask for. Prices, fees,
    coverage and policies are rendered from the fixture JSON (the same data
    the old tools read); the how-to steps are lifted from the knowledge-base
    markdown by section, so neither is hand-duplicated.
  * the CASE FILE — what the agent "looks up" for THIS caller: the expected
    verification answers, the account record (plan, bills, payments, line
    check result, tickets), the technician slots, every action's terms, and
    the reference each action gets. References are decided here, from the
    case id — a realtime model must never mint one mid-call.

The agent then "checks" and answers from the prompt with no tool call at all,
and an action is simulated by saying it is done with the case file's
reference. Verification stays conversational: the case file holds the
expected answers and the prompt forbids any account detail before a match.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime.tools.agents_demo import data
from runtime.tools.agents_demo.brand import BRAND, render
from runtime.tools.agents_demo.common import reference, working_days_from

KB_DIR = Path(__file__).resolve().parents[3] / "scripts" / "kb_content" / "agents_demo"

# How-to steps lifted from the KB: (file prefix, zero-based `## ` section
# index, label key). The EN and AR files mirror each other section for
# section, so one index serves both languages.
_KB_STEPS = (
    ("08", 1, "lights"),
    ("08", 2, "restart"),
    ("08", 3, "bands"),
    ("09", 1, "esim"),
    ("03", 7, "wireless_setup"),
)

_LABELS: Dict[str, Dict[str, str]] = {
    "en": {
        "sheet": "Facts sheet — the whole public catalog of {brand}. Nothing outside it exists.",
        "prices": "Monthly prices: UAE in dirhams, Saudi Arabia in riyals.",
        "prices_one": "This caller is in {country}: every price below is monthly, for {country}, in {word}. "
                      "Quote ONLY these numbers. The other country's prices differ and are not on this sheet — "
                      "never convert or guess them.",
        "home": "Home internet", "mobile": "Mobile", "business": "Business", "addons": "Add-ons and devices",
        "contract": "contract", "no_contract": "no contract", "months": "months", "or": "or",
        "on24": "on a 24-month contract",
        "not_offered": "Not offered (say so plainly; never invent it): {items}. There are no discounts, promo "
                       "codes or loyalty prices — a 24-month price is a catalog price, not a discount.",
        "coverage": "Coverage by area (fiber / Home 5G Wireless)",
        "fiber_and_5g": "fiber and 5G", "only_5g": "5G only, no fiber",
        "planned": "fiber planned: {when}",
        "unknown_area": "An area not listed here: you cannot confirm coverage — offer to take their details "
                        "so the team checks the exact address.",
        "notices": "Network notices today (public — may be shared before verification)",
        "no_notices": "No other area has a known outage.",
        "notice": "{area}: {summary} Started {started}; expected fix {eta}; status: {status}.",
        "policies": "Fees and policies",
        "install": "Fiber installation: {days} working days after ordering; an adult must be home for the "
                   "visit; free on a 12- or 24-month contract, otherwise a one-time {fee}. Visit windows: {windows}.",
        "wireless": "Home 5G Wireless: delivered the {delivery}, no installation visit, no contract.",
        "tech": "Technician visit for a fault: free when the fault is on the network side; {fee} when it is in "
                "the home's own wiring or equipment.",
        "bills": "Bills are due {due} days after they are issued; a late fee of {late} applies after the due "
                 "date; service is restricted after {restrict} days overdue and suspended after {suspend}; "
                 "reconnection costs {reconnect}.",
        "refund": "A duplicate payment: refund to the original card or account within {days} working days, or "
                  "a bill credit applied immediately — the customer chooses. Other charges can be disputed "
                  "within {window} days of the bill date; a dispute is reviewed by the team.",
        "moving": "Moving house: {free} free relocation per contract year; it takes {days} working days, so ask "
                  "for at least {notice} working days' notice; plan, price and contract stay the same. No fiber "
                  "at the new address: switch to Home 5G Wireless, or cancel with no early-termination fee. "
                  "Mobile lines are not affected.",
        "pause": "Pausing home service (travel): up to {max} months a year at {fee} a month; contract and router "
                 "are kept and the contract end date moves later by the paused months.",
        "cancel": "Early-termination fee: half of the remaining monthly fees on the contract; none once the "
                  "contract has ended or on a no-contract service. Return the router and mesh units within "
                  "{days} days of cancelling.",
        "sim": "SIM or eSIM change: the first one in a calendar year is free, then {fee} each. An eSIM QR code "
               "is emailed to the address on file; the old physical SIM stops working once the eSIM activates. "
               "Every SIM change needs identity verification.",
        "access": "Only the account holder or an authorized user may discuss or change an account; the holder "
                  "adds an authorized user in the app or by calling.",
        "howto": "How-to steps (from the knowledge base)",
        "lights": "Router and fiber-box lights", "restart": "Restarting the router properly",
        "bands": "The two Wi-Fi networks", "placement": "Placing the router",
        "esim": "Activating an eSIM", "wireless_setup": "Home 5G Wireless self-setup",
    },
    "ar": {
        "sheet": "ورقة الحقائق — كل ما تقدّمه «{brand_ar}» للعامة. أي شي مو مذكور هنا غير موجود.",
        "prices": "الأسعار شهرية: في الإمارات بالدرهم، وفي السعودية بالريال.",
        "prices_one": "المتصل في {country}: كل الأسعار تحت شهرية وبال{word}. "
                      "اذكر هذه الأرقام فقط. أسعار الدولة الثانية مختلفة ومو موجودة هنا — "
                      "لا تحوّلها ولا تخمّنها.",
        "home": "الإنترنت المنزلي", "mobile": "الجوال", "business": "الأعمال", "addons": "الإضافات والأجهزة",
        "contract": "العقد", "no_contract": "بدون عقد", "months": "شهر", "or": "أو",
        "on24": "مع عقد 24 شهر",
        "not_offered": "غير متوفر (قلها بوضوح ولا تخترع شي): {items}. ما فيه خصومات ولا أكواد ترويجية ولا "
                       "أسعار ولاء — سعر عقد الـ 24 شهر سعر من الكتالوج، مو خصم.",
        "coverage": "التغطية حسب المنطقة (فايبر / الإنترنت المنزلي اللاسلكي 5G)",
        "fiber_and_5g": "فايبر و5G", "only_5g": "5G فقط، ما فيه فايبر",
        "planned": "الفايبر مخطط له: {when}",
        "unknown_area": "منطقة مو موجودة في هالقائمة: ما تقدر تأكد التغطية — اعرض تاخذ بياناته عشان الفريق "
                        "يتأكد من العنوان بالضبط.",
        "notices": "تنبيهات الشبكة اليوم (معلومات عامة — تقدر تقولها قبل التحقق)",
        "no_notices": "ما فيه انقطاع معروف في أي منطقة ثانية.",
        "notice": "{area}: {summary} البداية: {started}؛ الإصلاح المتوقع: {eta}؛ الحالة: {status}.",
        "policies": "الرسوم والسياسات",
        "install": "تركيب الفايبر: خلال {days} أيام عمل من الطلب؛ لازم يكون فيه شخص بالغ في البيت وقت الزيارة؛ "
                   "مجاني مع عقد 12 أو 24 شهر، وبدون عقد رسوم مرة وحدة {fee}. مواعيد الزيارة: {windows}.",
        "wireless": "الإنترنت المنزلي اللاسلكي 5G: يوصل خلال {delivery}، بدون زيارة تركيب وبدون عقد.",
        "tech": "زيارة الفني لعطل: مجانية إذا العطل من جهة الشبكة؛ {fee} إذا العطل في تمديدات البيت أو أجهزة "
                "العميل نفسه.",
        "bills": "الفاتورة تستحق بعد {due} يوم من إصدارها؛ رسوم تأخير {late} بعد تاريخ الاستحقاق؛ الخدمة تتقيد "
                 "بعد {restrict} يوم تأخير وتتوقف بعد {suspend} يوم؛ إعادة التوصيل {reconnect}.",
        "refund": "دفعة مكررة: استرجاع للبطاقة أو الحساب الأصلي خلال {days} أيام عمل، أو رصيد في الفاتورة فوراً "
                  "— العميل يختار. أي رسم ثاني يقدر يعترض عليه خلال {window} يوم من تاريخ الفاتورة، والفريق "
                  "يراجع الاعتراض.",
        "moving": "الانتقال لبيت جديد: نقل مجاني {free} مرة في كل سنة عقد؛ يستغرق {days} أيام عمل، فاطلب إشعار "
                  "قبلها بـ {notice} أيام عمل على الأقل؛ الباقة والسعر والعقد ما يتغيرون. إذا ما فيه فايبر في "
                  "العنوان الجديد: يتحول للإنترنت المنزلي اللاسلكي 5G، أو يلغي بدون رسوم إنهاء مبكر. خطوط "
                  "الجوال ما تتأثر.",
        "pause": "إيقاف الخدمة المنزلية مؤقتاً (للسفر): لحد {max} شهور في السنة بـ {fee} في الشهر؛ العقد والراوتر "
                 "يبقون، وتاريخ نهاية العقد يتأخر بعدد شهور الإيقاف.",
        "cancel": "رسوم الإنهاء المبكر: نص الرسوم الشهرية المتبقية في العقد؛ ما فيه رسوم إذا العقد خلص أو الخدمة "
                  "بدون عقد. يرجّع الراوتر وأجهزة الميش خلال {days} يوم من الإلغاء.",
        "sim": "تغيير الشريحة أو الشريحة الإلكترونية: أول تغيير في السنة مجاني، وبعده {fee} لكل تغيير. رمز QR "
               "للشريحة الإلكترونية يوصل على الإيميل المسجل؛ الشريحة العادية القديمة توقف أول ما تتفعل "
               "الإلكترونية. كل تغيير شريحة يحتاج تحقق من الهوية.",
        "access": "صاحب الحساب أو المستخدم المفوّض بس يقدر يتكلم عن الحساب أو يغيّر فيه؛ صاحب الحساب يضيف "
                  "مستخدم مفوّض من التطبيق أو بالاتصال.",
        "howto": "خطوات عملية (من قاعدة المعرفة)",
        "lights": "أضواء الراوتر وصندوق الفايبر", "restart": "إعادة تشغيل الراوتر بالطريقة الصحيحة",
        "bands": "شبكتين الواي فاي", "placement": "مكان الراوتر",
        "esim": "تفعيل الشريحة الإلكترونية", "wireless_setup": "إعداد الإنترنت المنزلي اللاسلكي 5G بنفسك",
    },
}


_OUTAGE_STATUS = {"en": {}, "ar": {"engineers on site": "المهندسين في الموقع", "planned": "مجدولة"}}


def _lang(locale: Optional[str]) -> str:
    return "ar" if str(locale or "").lower().startswith("ar") else "en"


_SHORT_COUNTRY = {"en": {"AE": "UAE", "SA": "KSA"}, "ar": {"AE": "الإمارات", "SA": "السعودية"}}


def _both(prices: Dict[str, float], lang: str, country: Optional[str] = None) -> str:
    """'289 dirhams (UAE) / 279 riyals (KSA)' for a per-country amount, or
    '200 dirhams / riyals' when both countries charge the same number. With
    `country`, only that country's amount: two numbers side by side is how a
    voice model ends up quoting the UAE number in riyals."""
    if country:
        return data.money(prices[country], country, lang)
    cur = data.catalog()["currencies"]
    if prices["AE"] == prices["SA"]:
        return f"{data.money(prices['AE'], 'AE', lang)} / {cur['SA'][f'word_{lang}']}"
    return " / ".join(f"{data.money(prices[c], c, lang)} ({_SHORT_COUNTRY[lang][c]})" for c in ("AE", "SA"))


def _strip_md(text: str) -> str:
    return re.sub(r"\*\*([^*]+)\*\*", r"\1", text).strip()


@lru_cache(maxsize=None)
def kb_sections(lang: str, prefix: str) -> List[str]:
    """The ``## `` sections (heading line included) of one KB article."""
    path = next((KB_DIR / lang).glob(f"{prefix}-*.md"))
    body = render(path.read_text(encoding="utf-8"))
    return [s.strip() for s in re.split(r"(?m)^(?=## )", body) if s.startswith("## ")]


def kb_steps(lang: str, prefix: str, index: int) -> List[str]:
    """The list items (bulleted or numbered) of one KB section, or its first
    sentence when the section is prose — markdown stripped."""
    section = kb_sections(lang, prefix)[index]
    lines = section.splitlines()[1:]
    items = [_strip_md(re.sub(r"^(\s*[-*]|\s*\d+\.)\s+", "", ln)) for ln in lines
             if re.match(r"^\s*([-*]|\d+\.)\s+", ln)]
    if items:
        return items
    prose = " ".join(ln.strip() for ln in lines if ln.strip())
    return [_strip_md(re.split(r"(?<=[.!؟?])\s", prose)[0])] if prose else []


def _plan_line(pid: str, p: Dict[str, Any], lang: str, country: Optional[str] = None) -> str:
    t = _LABELS[lang]
    name = p[f"name_{lang}"] + (f" «{p['name_ar']}»" if lang == "en" else "")
    price = _both(p["price"], lang, country)
    if p.get(f"price_unit_{lang}"):
        price += f" {p[f'price_unit_{lang}']}"
    months = p.get("contract_months") or [0]
    terms = f" {t['or']} ".join(f"{m} {t['months']}" if m else t["no_contract"] for m in months)
    semi = "; " if lang == "en" else "؛ "
    line = f"- {name}: {price}{semi}{t['contract']}: {terms}. {data.summary(p, lang, country)}"
    if p.get("price_24m_less"):
        on24 = {c: p["price"][c] - p["price_24m_less"] for c in ("AE", "SA")}
        line += f" {t['on24'].capitalize() if lang == 'en' else t['on24']}: {_both(on24, lang, country)}."
    if p["category"] in ("home", "mobile", "business"):  # what a recommendation is reasoned from
        line += f" ({p[f'best_for_{lang}'].rstrip('.')})"
    return line


def case_country(agent_key: str, case: Optional[Dict[str, Any]]) -> Optional[str]:
    """'AE' / 'SA' for the caller of an inbound case: inquiries cases name it,
    support cases inherit it from the customer's area on file."""
    if not case:
        return None
    if case.get("country") in ("AE", "SA"):
        return case["country"]
    cust = data.customer_for_case(case)
    if cust and cust.get("country") in ("AE", "SA"):
        return cust["country"]
    if cust and cust.get("area_id"):
        return data.area(cust["area_id"]).get("country")
    return None


@lru_cache(maxsize=None)
def facts_sheet(lang: str = "en", country: Optional[str] = None) -> str:
    """The compact public catalog in one language (EN or AR) — prices for
    `country` only when the caller's country is known, else both."""
    lang = _lang(lang)
    t = _LABELS[lang]
    cat, pol, fees = data.catalog(), data.policies(), data.fees()
    if country:
        cur = cat["currencies"][country]
        prices_line = t["prices_one"].format(country=cur[f"country_{lang}"], word=cur[f"word_{lang}"])
    else:
        prices_line = t["prices"]
    out = [t["sheet"].format(brand=BRAND["name_en"], brand_ar=BRAND["name_ar"]), prices_line]

    groups = (("home", ("home",)), ("mobile", ("mobile",)), ("business", ("business",)),
              ("addons", ("home_addon", "mobile_addon", "business_addon", "device")))
    for key, cats in groups:
        out.append(f"\n### {t[key]}")
        out += [_plan_line(pid, p, lang, country) for pid, p in data.products().items() if p["category"] in cats]
    semi = "; " if lang == "en" else "؛ "
    comma = ", " if lang == "en" else "، "
    out.append(t["not_offered"].format(items=semi.join(n[lang] for n in cat["not_offered"])))

    out.append(f"\n### {t['coverage']}")
    by_city: Dict[str, List[str]] = {}
    for a in data.areas().values():
        what = t["fiber_and_5g"] if a["fiber"] else t["only_5g"]
        if not a["fiber"] and a.get(f"fiber_planned_{lang}"):
            what += f" ({t['planned'].format(when=a[f'fiber_planned_{lang}'])})"
        by_city.setdefault(f"{a[f'city_{lang}']}{comma}{cat['currencies'][a['country']][f'country_{lang}']}",
                           []).append(f"{a[f'name_{lang}']}: {what}")
    out += [f"- {city} — " + semi.join(names) for city, names in by_city.items()]
    out.append(t["unknown_area"])

    out.append(f"\n### {t['notices']}")
    for o in data.outages():
        a = data.area(o["area_id"])
        out.append("- " + t["notice"].format(area=f"{a[f'name_{lang}']}{comma}{a[f'city_{lang}']}",
                                             summary=o[f"summary_{lang}"], started=o[f"started_{lang}"],
                                             eta=o[f"eta_{lang}"],
                                             status=_OUTAGE_STATUS[lang].get(o["status"], o["status"])))
    out.append(t["no_notices"])

    windows = ("; " if lang == "en" else "، ").join(w[lang] for w in data.technician_windows())
    delivery = pol["wireless_delivery"] if lang == "en" else "يوم العمل التالي"
    out.append(f"\n### {t['policies']}")
    out += ["- " + s for s in (
        t["install"].format(days=pol["fiber_install_working_days"] if lang == "en" else "2 إلى 5",
                            fee=_both(fees["installation_no_contract"], lang, country), windows=windows),
        t["wireless"].format(delivery=delivery),
        t["tech"].format(fee=_both(fees["technician_visit_customer_side"], lang, country)),
        t["bills"].format(due=pol["bill_due_days"], late=_both(fees["late_fee"], lang, country),
                          restrict=pol["restriction_after_overdue_days"],
                          suspend=pol["suspension_after_overdue_days"],
                          reconnect=_both(fees["reconnection_fee"], lang, country)),
        t["refund"].format(days=pol["refund_working_days"] if lang == "en" else "5 إلى 7",
                           window=pol["dispute_window_days"]),
        t["moving"].format(free=pol["relocation_free_per_contract_year"],
                           days=pol["relocation_working_days"] if lang == "en" else "3 إلى 5",
                           notice=pol["relocation_notice_working_days"]),
        t["pause"].format(max=pol["pause_max_months_per_year"], fee=_both(fees["pause_fee_per_month"], lang, country)),
        t["cancel"].format(days=pol["equipment_return_days"]),
        t["sim"].format(fee=_both(fees["sim_replacement_after_first"], lang, country)),
        t["access"],
    )]

    out.append(f"\n### {t['howto']}")
    for prefix, index, key in _KB_STEPS:
        steps = kb_steps(lang, prefix, index)
        out.append(f"- {t[key]}: " + " | ".join(steps))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Case files
# ---------------------------------------------------------------------------

_CASE_LABELS: Dict[str, Dict[str, str]] = {
    "en": {
        "today": "Today: {iso} ({spoken}).",
        "verify": "VERIFICATION — the caller gives EITHER the account number OR the registered mobile, PLUS "
                  "EITHER the date of birth OR the last 4 digits of the national ID. You are NOT given the right "
                  "answers: they are confirmed ONLY by calling support_verify_customer with exactly what the "
                  "caller said.",
        "holder": "Account holder: {name} «{name_ar}» ({pronoun}). Authorized users: {auth}. Anyone else — "
                  "even with every detail right — is told nothing about this account and nothing is changed.",
        "none": "none", "she": "she", "he": "he",
        "record": "ACCOUNT RECORD — only after verification succeeded:",
        "area": "Area on file: {area} ({country}; amounts in {word}, «{word_ar}»). Account status: {status}.",
        "services": "Services: {items}.",
        "home_svc": "{plan} (home internet, {router})",
        "mobile_svc": "{plan} (mobile line ending {last4}, {sim} SIM{phone})",
        "contract": "Contract: {plan}, {months} months from {start}; {left} months remaining.",
        "no_contract": "Contract: none (no-contract service).",
        "email": "Email on file: {email}.",
        "bill": "{month} bill: {total} — {lines}; {status}.",
        "bill_paid": "paid",
        "payments": "payments on record: {items}",
        "due": "due {date}",
        "bill_change": "Change versus {prev}: {diff} ({prev_total} → {total}).",
        "duplicate": "DUPLICATE PAYMENT on record: the {month} bill was charged twice — {refs}, {amount} each.",
        "no_duplicate": "No duplicate or wrong payment on record — no refund is owed; a disputed charge becomes a "
                        "billing ticket.",
        "tickets": "Earlier tickets: {items} — never repeat a step these already tried.",
        "ticket": "{ref} ({date}, {status}): {summary}",
        "line": "LINE CHECK (run when they report a fault — narrate it briefly: “let me run a quick check on your "
                "line… okay”): {line}",
        "wifi": "Wi-Fi: {wifi}",
        "fix": "What fixes it: {fix}",
        "outage": "Their area has a live outage: {summary} Expected fix {eta}. No technician, no router reset.",
        "tech_needed": "Technician needed: YES — the visit is {charge}.",
        "tech_free": "free (the fault is on the network side)",
        "tech_not_needed": "Technician needed: no — the steps above should fix it; if a visit is still booked "
                           "and the fault turns out to be in the home's own wiring or equipment it costs {fee}.",
        "no_fault": "No fault found on the line. A technician visit is only needed if they still report a "
                    "problem; it costs {fee} if the fault is in the home's own wiring or equipment.",
        "slots": "Technician slots (offer two or three, book only the one they pick): {slots}.",
        "actions": "ACTIONS — each only after verification, after you stated its exact terms and the caller said "
                   "an explicit yes, at most once per call. Say it is done and give its reference exactly as "
                   "written here; never invent another reference, slot, amount or date.",
        "a_refund": "Refund of the duplicate payment ({amount}): first ask “back to your card, or as a bill "
                    "credit?”; card = {days} working days, bill credit = immediately. Reference {ref}.",
        "a_visit": "Technician visit: reference {ref}; an adult needs to be home.",
        "a_pause": "Pause home service: 1 to {max} months at {fee} a month (e.g. 3 months = {three}); contract and "
                   "router kept; the contract end date moves later. Reference {ref}.",
        "a_cancel": "Cancel home service: early-termination fee {etf} ({how}); return the router within {days} "
                    "days. Always also offer the pause alternative. Reference {ref}.",
        "a_cancel_free": "Cancel home service: no early-termination fee (no contract running); return the router "
                         "within {days} days. Reference {ref}.",
        "a_relocate": "Relocate home service: {fee}; {days} working days; check the new area in the facts "
                      "sheet's coverage list — no fiber there means Home 5G Wireless or cancelling with no "
                      "early-termination fee; confirm the move date before booking. Reference {ref}.",
        "a_relocate_free": "free (first move this contract year)",
        "a_relocate_paid": "not free — a second move this contract year is quoted by the team",
        "a_esim": "Move the mobile line ending {last4} to an eSIM: {fee}; the QR code is emailed to {email}; the "
                  "current physical SIM stops working once the eSIM activates. Reference {ref}.",
        "a_sim": "Replacement physical SIM for the line ending {last4}: {fee}; delivered the next working day. "
                 "Reference {ref}.",
        "sim_free": "free (first SIM change this year)",
        "a_ticket": "Open a ticket (a disputed charge, anything else that needs the team): reference {ref}; the "
                    "team follows up within one working day.",
        "a_handoff": "Hand over to a human colleague (they ask for one, a repeated problem, or verification "
                     "failed three times — this one works even unverified): reference {ref}; expected wait under "
                     "5 minutes; they will not have to repeat themselves.",
    },
    "ar": {
        "today": "اليوم: {iso} ({spoken}).",
        "verify": "التحقق — المتصل يعطي إما رقم الحساب أو الجوال المسجل، ومعه إما تاريخ الميلاد أو آخر 4 أرقام "
                  "من الهوية. الإجابات الصحيحة مو عندك: تتأكد منها فقط باستدعاء support_verify_customer بالضبط "
                  "باللي قاله المتصل.",
        "holder": "صاحب الحساب: {name_ar} ({name}) ({pronoun}). المستخدمون المفوّضون: {auth}. أي شخص ثاني — حتى لو "
                  "عنده كل التفاصيل صح — ما يُقال له شي عن الحساب ولا يتغير شي.",
        "none": "لا يوجد", "she": "هي", "he": "هو",
        "record": "سجل الحساب — فقط بعد نجاح التحقق:",
        "area": "المنطقة المسجلة: {area} ({country}؛ المبالغ بالـ{word_ar}). حالة الحساب: {status}.",
        "services": "الخدمات: {items}.",
        "home_svc": "{plan} (إنترنت منزلي، {router})",
        "mobile_svc": "{plan} (خط جوال ينتهي بـ {last4}، شريحة {sim}{phone})",
        "contract": "العقد: {plan}، {months} شهر من {start}؛ باقي {left} شهر.",
        "no_contract": "العقد: لا يوجد (خدمة بدون عقد).",
        "email": "الإيميل المسجل: {email}.",
        "bill": "فاتورة {month}: {total} — {lines}؛ {status}.",
        "bill_paid": "مدفوعة",
        "payments": "الدفعات المسجلة: {items}",
        "due": "تستحق في {date}",
        "bill_change": "الفرق عن {prev}: {diff} ({prev_total} ← {total}).",
        "duplicate": "دفعة مكررة مسجلة: فاتورة {month} انخصمت مرتين — {refs}، كل وحدة {amount}.",
        "no_duplicate": "ما فيه دفعة مكررة أو خاطئة مسجلة — ما فيه استرجاع مستحق؛ الرسم المعترض عليه يصير بلاغ "
                        "فواتير.",
        "tickets": "بلاغات سابقة: {items} — لا تعيد أي خطوة جربوها قبل.",
        "ticket": "{ref} ({date}، {status}): {summary}",
        "line": "فحص الخط (سوّه لما يبلّغ عن عطل — قلها بسرعة: «لحظة أفحص الخط… تمام»): {line}",
        "wifi": "الواي فاي: {wifi}",
        "fix": "الحل: {fix}",
        "outage": "في منطقته انقطاع حالي: {summary} الإصلاح المتوقع {eta}. لا فني ولا إعادة تشغيل للراوتر.",
        "tech_needed": "يحتاج فني: نعم — الزيارة {charge}.",
        "tech_free": "مجانية (العطل من جهة الشبكة)",
        "tech_not_needed": "يحتاج فني: لا — الخطوات فوق تكفي؛ إذا انحجزت زيارة وطلع العطل في تمديدات البيت أو "
                           "أجهزته تكلف {fee}.",
        "no_fault": "ما فيه عطل في الخط. زيارة الفني بس إذا ظل يبلّغ عن مشكلة؛ تكلف {fee} إذا العطل في تمديدات "
                    "البيت أو أجهزته.",
        "slots": "مواعيد الفني (اعرض اثنين أو ثلاثة، واحجز بس اللي يختاره): {slots}.",
        "actions": "الإجراءات — كل إجراء فقط بعد التحقق، وبعد ما تذكر شروطه بالضبط ويقول المتصل موافقة صريحة، "
                   "ومرة وحدة بس في المكالمة. قل إنه تم وأعطِ المرجع بالضبط زي ما هو مكتوب هنا؛ لا تخترع مرجع "
                   "أو موعد أو مبلغ أو تاريخ ثاني.",
        "a_refund": "استرجاع الدفعة المكررة ({amount}): اسأل أول «ترجع على البطاقة ولا رصيد في الفاتورة؟»؛ البطاقة "
                    "= {days} أيام عمل، الرصيد = فوراً. المرجع {ref}.",
        "a_visit": "زيارة فني: المرجع {ref}؛ لازم يكون شخص بالغ في البيت.",
        "a_pause": "إيقاف الخدمة المنزلية مؤقتاً: من شهر إلى {max} شهور بـ {fee} في الشهر (مثلاً 3 شهور = {three})؛ "
                   "العقد والراوتر يبقون؛ تاريخ نهاية العقد يتأخر. المرجع {ref}.",
        "a_cancel": "إلغاء الخدمة المنزلية: رسوم إنهاء مبكر {etf} ({how})؛ يرجّع الراوتر خلال {days} يوم. اعرض "
                    "دايماً بديل الإيقاف المؤقت. المرجع {ref}.",
        "a_cancel_free": "إلغاء الخدمة المنزلية: ما فيه رسوم إنهاء مبكر (ما فيه عقد ساري)؛ يرجّع الراوتر خلال "
                         "{days} يوم. المرجع {ref}.",
        "a_relocate": "نقل الخدمة المنزلية: {fee}؛ {days} أيام عمل؛ شيك المنطقة الجديدة في قائمة التغطية بورقة "
                      "الحقائق — إذا ما فيه فايبر يتحول لـ 5G اللاسلكي أو يلغي بدون رسوم إنهاء مبكر؛ أكّد تاريخ "
                      "الانتقال قبل الحجز. المرجع {ref}.",
        "a_relocate_free": "مجاني (أول نقل في سنة العقد)",
        "a_relocate_paid": "مو مجاني — النقل الثاني في نفس سنة العقد يسعّره الفريق",
        "a_esim": "تحويل الخط اللي ينتهي بـ {last4} لشريحة إلكترونية: {fee}؛ رمز QR يوصل على {email}؛ الشريحة "
                  "العادية الحالية توقف أول ما تتفعل الإلكترونية. المرجع {ref}.",
        "a_sim": "شريحة بديلة للخط اللي ينتهي بـ {last4}: {fee}؛ توصل يوم العمل التالي. المرجع {ref}.",
        "sim_free": "مجاني (أول تغيير شريحة هالسنة)",
        "a_ticket": "فتح بلاغ (اعتراض على رسم، أو أي شي يحتاج الفريق): المرجع {ref}؛ الفريق يتابع خلال يوم عمل.",
        "a_handoff": "تحويل لزميل بشري (إذا طلب، أو مشكلة متكررة، أو فشل التحقق 3 مرات — هذا يصير حتى بدون "
                     "تحقق): المرجع {ref}؛ الانتظار المتوقع أقل من 5 دقايق؛ ما يحتاج يعيد كلامه.",
    },
}


def case_references(agent_key: str, case: Dict[str, Any]) -> Dict[str, str]:
    """Every reference this call may give, decided before the call from the case id."""
    if agent_key == "support":
        acct = case["customer_id"]
        return {name: reference(prefix, case["id"], acct, name) for name, prefix in (
            ("refund", "REF"), ("visit", "VIS"), ("pause", "SRV"), ("cancel", "SRV"), ("relocate", "SRV"),
            ("esim", "SRV"), ("sim", "SRV"), ("ticket", "TCK"), ("handoff", "HND"))}
    return {name: reference(prefix, case["id"], name) for name, prefix in (
        ("lead", "LEAD"), ("callback", "CB"), ("handoff", "HND"))}


_WEEKDAYS = {"en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
             "ar": ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")}
_STATUS = {"en": {}, "ar": {"active": "فعّال", "restricted": "مقيّد", "suspended": "موقوف",
                            "closed": "مغلق", "open": "مفتوح", "paid": "مدفوعة", "unpaid": "غير مدفوعة",
                            "partial": "مدفوعة جزئياً"}}


def say_day(d: date, lang: str) -> str:
    """'Thursday 8 October 2026' / 'الخميس 8 أكتوبر 2026'."""
    return f"{_WEEKDAYS[lang][d.weekday()]} {data.say_date(d.isoformat(), lang)}"


def _status(value: str, lang: str) -> str:
    return _STATUS[lang].get(value, value)


def _slots(today: date, lang: str) -> List[str]:
    out = []
    comma = ", " if lang == "en" else "، "
    for d in working_days_from(today, 2):
        for w in data.technician_windows():
            out.append(f"{say_day(d, lang)}{comma}{w[lang]}")
    return out


def support_case_file(case: Dict[str, Any], lang: str = "en", today: Optional[date] = None) -> str:
    lang = _lang(lang)
    t = _CASE_LABELS[lang]
    today = today or date.today()
    cust = data.customer(case["customer_id"])
    country = cust["country"]
    cur = data.catalog()["currencies"][country]
    area = data.area(cust["area_id"])
    refs = case_references("support", case)
    money = lambda amount: data.money(amount, country, lang)  # noqa: E731
    plan = lambda pid: data.products()[pid][f"name_{lang}"]  # noqa: E731
    sep = "; " if lang == "en" else "؛ "
    comma = ", " if lang == "en" else "، "

    auth = ", ".join(u.get(f"name_{lang}") or u.get("name_en", "") for u in cust.get("authorized_users", [])) \
        or t["none"]
    lines = [
        t["today"].format(iso=today.isoformat(), spoken=say_day(today, lang)),
        t["verify"].format(acct=cust["account_number"], mobile=cust["mobile"],
                           dob=data.say_date(cust["dob"], lang),
                           dob_num="/".join(reversed(cust["dob"].split("-"))), id4=cust["id_last4"]),
        t["holder"].format(name=cust["name_en"], name_ar=cust["name_ar"],
                           pronoun=t["she"] if cust.get("gender") == "f" else t["he"], auth=auth),
        "",
        t["record"],
        t["area"].format(area=f"{area[f'name_{lang}']}{comma}{area[f'city_{lang}']}",
                         country=cur[f"country_{lang}"], word=cur["word_en"], word_ar=cur["word_ar"],
                         status=_status(cust["status"], lang)),
    ]
    services = []
    for s in cust["services"]:
        if s["type"] == "mobile":
            phone = f", {s[f'phone_{lang}']}" if s.get(f"phone_{lang}") else ""
            sim = s.get("sim", "physical")
            sim = {"physical": "عادية", "esim": "إلكترونية"}.get(sim, sim) if lang == "ar" else sim
            services.append(t["mobile_svc"].format(plan=plan(s["plan"]), last4=s["number"][-4:], sim=sim,
                                                   phone=phone))
        else:
            services.append(t["home_svc"].format(plan=plan(s["plan"]),
                                                 router=s.get(f"router_{lang}") or s.get("router_en", "")))
    lines.append(t["services"].format(items=sep.join(services)))
    contract = cust.get("contract")
    if contract:
        lines.append(t["contract"].format(plan=plan(contract["plan"]), months=contract["months"],
                                          start=data.month_name(contract["started"][:7], lang),
                                          left=contract["remaining_months"]))
    else:
        lines.append(t["no_contract"])
    lines.append(t["email"].format(email=cust["email_masked"]))

    bills = cust["bills"]
    for b in reversed(bills):
        items = (", " if lang == "en" else "، ").join(f"{i[f'label_{lang}']} {money(i['amount'])}" for i in b["items"])
        pays = (", " if lang == "en" else "، ").join(
            f"{p['ref']} {money(p['amount'])} {data.say_date(p['date'], lang)} ({p[f'method_{lang}']})"
            for p in b.get("payments", []))
        status = _status(b["status"], lang)
        if pays:
            status += sep + t["payments"].format(items=pays)
        if b.get("due_date"):
            status += sep + t["due"].format(date=data.say_date(b["due_date"], lang))
        lines.append("- " + t["bill"].format(month=data.month_name(b["month"], lang), total=money(b["amount"]),
                                             lines=items, status=status))
    if len(bills) >= 2:
        last, prev = bills[-1], bills[-2]
        diff = round(last["amount"] - prev["amount"], 2)
        if diff:
            lines.append("- " + t["bill_change"].format(prev=data.month_name(prev["month"], lang),
                                                        diff=("+" if diff > 0 else "-") + money(abs(diff)),
                                                        prev_total=money(prev["amount"]),
                                                        total=money(last["amount"])))
    dup = cust.get("duplicate_payment")
    if dup:
        lines.append(t["duplicate"].format(month=data.month_name(dup["bill_month"], lang),
                                           refs=" + ".join(dup["refs"]), amount=money(dup["amount"])))
    else:
        lines.append(t["no_duplicate"])
    if cust.get("tickets"):
        lines.append(t["tickets"].format(items=sep.join(
            t["ticket"].format(ref=tk["ref"], date=data.say_date(tk["date"], lang), status=_status(tk["status"], lang),
                               summary=tk[f"summary_{lang}"].rstrip(".")) for tk in cust["tickets"])))

    diag = cust.get("diagnostics") or {}
    lines.append(t["line"].format(line=diag.get(f"line_{lang}") or diag.get("line_en", "")))
    if diag.get("wifi_en"):
        lines.append(t["wifi"].format(wifi=diag.get(f"wifi_{lang}") or diag["wifi_en"]))
    if diag.get("fix_en"):
        lines.append(t["fix"].format(fix=diag.get(f"fix_{lang}") or diag["fix_en"]))
    outage = next((o for o in data.outages() if o["id"] == diag.get("outage_id")), None)
    tech_fee = money(data.fees()["technician_visit_customer_side"][country])
    if outage:
        lines.append(t["outage"].format(summary=outage[f"summary_{lang}"], eta=outage[f"eta_{lang}"]))
    elif diag.get("needs_technician"):
        lines.append(t["tech_needed"].format(charge=t["tech_free"] if not diag.get("visit_charge") else tech_fee))
    elif diag.get("fix_en"):
        lines.append(t["tech_not_needed"].format(fee=tech_fee))
    else:
        lines.append(t["no_fault"].format(fee=tech_fee))
    if not outage:
        lines.append(t["slots"].format(slots=sep.join(_slots(today, lang))))

    lines += ["", t["actions"]]
    pol, fees = data.policies(), data.fees()
    home = next((s for s in cust["services"] if s["type"] == "home"), None)
    mobile = next((s for s in cust["services"] if s["type"] == "mobile"), None)
    if dup:
        lines.append("- " + t["a_refund"].format(amount=money(dup["amount"]), ref=refs["refund"],
                                                 days=pol["refund_working_days"] if lang == "en" else "5 إلى 7"))
    if home and not outage:
        lines.append("- " + t["a_visit"].format(ref=refs["visit"]))
    if home:
        pause_fee = fees["pause_fee_per_month"][country]
        lines.append("- " + t["a_pause"].format(max=pol["pause_max_months_per_year"], fee=money(pause_fee),
                                                three=money(pause_fee * 3), ref=refs["pause"]))
        remaining = int((contract or {}).get("remaining_months") or 0)
        if remaining:
            price = data.price_of(home["plan"], country)
            etf = round(remaining * price * fees["early_termination_share_of_remaining"], 2)
            how = (f"{remaining} remaining months × half of the {money(price)} monthly fee" if lang == "en"
                   else f"{remaining} شهر متبقي × نص الرسوم الشهرية {money(price)}")
            lines.append("- " + t["a_cancel"].format(etf=money(etf), how=how, days=pol["equipment_return_days"],
                                                     ref=refs["cancel"]))
        else:
            lines.append("- " + t["a_cancel_free"].format(days=pol["equipment_return_days"], ref=refs["cancel"]))
        used = (contract or {}).get("relocations_used_this_year", 0)
        lines.append("- " + t["a_relocate"].format(
            fee=t["a_relocate_free"] if used < pol["relocation_free_per_contract_year"] else t["a_relocate_paid"],
            days=pol["relocation_working_days"] if lang == "en" else "3 إلى 5", ref=refs["relocate"]))
    if mobile:
        sim_fee = (t["sim_free"] if cust.get("sim_changes_this_year", 0) < 1
                   else money(fees["sim_replacement_after_first"][country]))
        lines.append("- " + t["a_esim"].format(last4=mobile["number"][-4:], fee=sim_fee,
                                               email=cust["email_masked"], ref=refs["esim"]))
        lines.append("- " + t["a_sim"].format(last4=mobile["number"][-4:], fee=sim_fee, ref=refs["sim"]))
    lines.append("- " + t["a_ticket"].format(ref=refs["ticket"]))
    lines.append("- " + t["a_handoff"].format(ref=refs["handoff"]))
    return "\n".join(lines)


_INQ_LABELS: Dict[str, Dict[str, str]] = {
    "en": {
        "caller": "The caller is a prospect: you do not know their name, area or needs until they tell you — "
                  "never assume them. No identity check is needed.",
        "callbacks": "A sales callback can be booked within {days} days; the team calls the mobile they gave. "
                     "The days: {list}.",
        "actions": "ACTIONS — only with their consent, at most once per call. Say it is done and give its "
                   "reference exactly as written here; never invent another one.",
        "lead": "Save them as a lead for the sales team: first get their name and mobile (a mobile is 05 "
                "followed by 8 digits), read the number back digit by digit and get a yes. Reference {ref}.",
        "callback": "Book a sales callback (only after the lead is saved): confirm the day and time they asked "
                    "for. Reference {ref}.",
        "handoff": "Hand over to a human colleague if they insist on one: reference {ref}; expected wait under "
                   "5 minutes.",
    },
    "ar": {
        "caller": "المتصل عميل محتمل: ما تعرف اسمه ولا منطقته ولا احتياجه لين يقول لك — لا تفترض شي. ما يحتاج "
                  "تحقق من الهوية.",
        "callbacks": "تقدر تحجز اتصال من فريق المبيعات خلال {days} يوم؛ الفريق يتصل على الجوال اللي أعطاك. "
                     "الأيام: {list}.",
        "actions": "الإجراءات — بس بموافقته، ومرة وحدة في المكالمة. قل إنه تم وأعطِ المرجع بالضبط زي ما هو مكتوب "
                   "هنا؛ لا تخترع مرجع ثاني.",
        "lead": "حفظه كعميل محتمل لفريق المبيعات: خذ اسمه وجواله أول (الجوال 05 وبعده 8 أرقام)، اقرأ الرقم رقم رقم "
                "وخذ تأكيده. المرجع {ref}.",
        "callback": "حجز اتصال من المبيعات (بعد حفظه كعميل محتمل): أكّد اليوم والوقت اللي طلبه. المرجع {ref}.",
        "handoff": "تحويل لزميل بشري إذا أصر: المرجع {ref}؛ الانتظار المتوقع أقل من 5 دقايق.",
    },
}


def inquiries_case_file(case: Dict[str, Any], lang: str = "en", today: Optional[date] = None) -> str:
    lang = _lang(lang)
    t, c = _INQ_LABELS[lang], _CASE_LABELS[lang]
    today = today or date.today()
    refs = case_references("inquiries", case)

    days = data.policies()["offer_followup_max_days"]
    return "\n".join([
        c["today"].format(iso=today.isoformat(), spoken=say_day(today, lang)),
        t["caller"],
        t["callbacks"].format(days=days, list=(", " if lang == "en" else "، ").join(
            say_day(today + timedelta(days=n), lang) for n in range(1, days + 1))),
        "",
        t["actions"],
        "- " + t["lead"].format(ref=refs["lead"]),
        "- " + t["callback"].format(ref=refs["callback"]),
        "- " + t["handoff"].format(ref=refs["handoff"]),
    ])


def case_file(agent_key: str, case: Dict[str, Any], lang: str = "en", today: Optional[date] = None) -> str:
    """The pre-rendered case file for one inbound case, in one language."""
    if agent_key == "support":
        return support_case_file(case, lang, today)
    if agent_key == "inquiries":
        return inquiries_case_file(case, lang, today)
    raise LookupError(f"{agent_key!r} is not an inbound demo agent")
