"""Fixture store + agent registry for the AI-agents demo portal.

Everything the simulation knows lives in ``data/*.json`` beside this module:
the product catalog (prices per country), areas (coverage, outages), the
fictional customers, and one use-case file per agent. The tools, the portal's
story cards and the outbound case files all read it through here, so the card
a tester reads and the record the agent answers from can never disagree.

Strings carry ``{{brand…}}`` placeholders; they are rendered from ``brand.py``
once, at load time.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime.tools.agents_demo.brand import render_obj

_DIR = Path(__file__).with_name("data")


@dataclass(frozen=True)
class DemoAgent:
    key: str            # URL segment + use-case file prefix
    kind: str           # ai_agent_kind enum value
    direction: str      # "inbound" | "outbound"
    persona_en: str     # the agent's spoken name
    persona_ar: str
    voice_id: str       # Gemini Live prebuilt voice
    gender: str         # "f" | "m" — Arabic self-reference forms


AGENTS: Dict[str, DemoAgent] = {
    "support": DemoAgent("support", "agents_support", "inbound", "Lina", "لينا", "Kore", "f"),
    "inquiries": DemoAgent("inquiries", "agents_inquiries", "inbound", "Adam", "آدم", "Puck", "m"),
    "collection": DemoAgent("collection", "agents_collection", "outbound", "Salma", "سلمى", "Aoede", "f"),
    "sales": DemoAgent("sales", "agents_sales", "outbound", "Rami", "رامي", "Charon", "m"),
}
AGENT_BY_KIND: Dict[str, DemoAgent] = {a.kind: a for a in AGENTS.values()}
AGENT_KINDS = tuple(a.kind for a in AGENTS.values())


@lru_cache(maxsize=None)
def _load(name: str) -> Dict[str, Any]:
    with (_DIR / f"{name}.json").open(encoding="utf-8") as fh:
        return render_obj(json.load(fh))


def catalog() -> Dict[str, Any]:
    return _load("catalog")


def products() -> Dict[str, Dict[str, Any]]:
    return catalog()["products"]


def policies() -> Dict[str, Any]:
    return catalog()["policies"]


def fees() -> Dict[str, Any]:
    return catalog()["fees"]


def areas() -> Dict[str, Dict[str, Any]]:
    return _load("areas")["areas"]


def outages() -> List[Dict[str, Any]]:
    return _load("areas")["outages"]


def technician_windows() -> List[Dict[str, str]]:
    return _load("areas")["technician_windows"]


def customers() -> Dict[str, Dict[str, Any]]:
    return _load("customers")["customers"]


def cases(agent_key: str) -> List[Dict[str, Any]]:
    if agent_key not in AGENTS:
        raise LookupError(f"unknown demo agent {agent_key!r}")
    return _load(f"{agent_key}_cases")["cases"]


def resolve_case(agent_key: str, case_id: Optional[str]) -> Dict[str, Any]:
    """The use case being played. Blank → the agent's first case (a voice
    scenario runner cannot pass one). Unknown → LookupError, which the voice
    endpoint turns into CLOSE_FORBIDDEN."""
    all_cases = cases(agent_key)
    wanted = (case_id or "").strip().upper()
    if not wanted:
        return all_cases[0]
    for case in all_cases:
        if case["id"] == wanted:
            return case
    raise LookupError(f"unknown {agent_key} use case {case_id!r}")


def customer(account_number: str) -> Dict[str, Any]:
    return customers()[account_number]


def customer_for_case(case: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    acct = case.get("customer_id")
    return customers().get(acct) if acct else None


# ---------------------------------------------------------------------------
# Text normalisation (spoken input arrives in any numerals, any case)
# ---------------------------------------------------------------------------

# Arabic-Indic (٠-٩) and Extended Arabic-Indic (۰-۹) digits → ASCII. An Arabic
# call's transcript can carry either; the fixtures are ASCII.
DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})


def digits(value: Optional[str]) -> str:
    return re.sub(r"[^0-9]", "", str(value or "").translate(DIGIT_MAP))


def normalize_mobile(value: Optional[str]) -> str:
    """Local 05XXXXXXXX form from any spoken/written variant (+971 / 00966 …)."""
    d = digits(value)
    for cc in ("00971", "00966", "971", "966"):
        if d.startswith(cc) and len(d) == len(cc) + 9:
            return "0" + d[len(cc):]
    if d.startswith("5") and len(d) == 9:
        return "0" + d
    return d


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text or "")).casefold()
    t = re.sub("[إأآا]", "ا", t).replace("ة", "ه").replace("ى", "ي")
    t = re.sub(r"[ً-ْـ]", "", t)  # tashkeel + tatweel
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def find_area(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best area match for a spoken/written area or city name, else None.

    Longest alias wins, so "Al Reem Island" never resolves to a shorter
    alias that happens to be a substring of another area's."""
    q = _fold(text or "")
    if not q:
        return None
    best = None
    best_len = 0
    for area_id, area in areas().items():
        names = [area_id, area["name_en"], area["name_ar"], *area.get("aliases", [])]
        for name in names:
            n = _fold(name)
            if n and (n == q or n in q or (len(q) >= 4 and q in n)) and len(n) > best_len:
                best, best_len = {"id": area_id, **area}, len(n)
    return best


def area(area_id: str) -> Dict[str, Any]:
    return {"id": area_id, **areas()[area_id]}


# ---------------------------------------------------------------------------
# Rendering helpers — the model is handed words, never codes to convert
# ---------------------------------------------------------------------------

MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December")
MONTHS_AR = ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو",
             "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر")


def month_name(code: str, lang: str) -> str:
    """'2026-09' → 'September 2026' / 'سبتمبر 2026'."""
    year, month = code.split("-")[:2]
    names = MONTHS_AR if lang == "ar" else MONTHS_EN
    return f"{names[int(month) - 1]} {year}"


def say_date(iso: str, lang: str = "en") -> str:
    """'2026-10-05' → '5 October 2026' / '5 أكتوبر 2026'."""
    year, month, day = iso.split("-")[:3]
    names = MONTHS_AR if lang == "ar" else MONTHS_EN
    return f"{int(day)} {names[int(month) - 1]} {year}"


def _num(amount: float) -> str:
    if float(amount).is_integer():
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def money(amount: float, country: str, lang: str = "en") -> str:
    """'723 dirhams' / '723 درهم' — the spoken form, never a currency code."""
    cur = catalog()["currencies"][country]
    word = cur["word_ar"] if lang == "ar" else cur["word_en"]
    return f"{_num(amount)} {word}"


def summary(product: Dict[str, Any], lang: str, country: Optional[str] = None) -> str:
    """A product's one-line description; the per-country variant when the
    caller's country is known and the text quotes prices."""
    if country and product.get(f"summary_{lang}_{country}"):
        return product[f"summary_{lang}_{country}"]
    return product[f"summary_{lang}"]


def price_of(product_id: str, country: str) -> float:
    return products()[product_id]["price"][country]
