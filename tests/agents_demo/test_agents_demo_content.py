"""Content integrity for the AI-agents demo portal: fixtures, story cards,
KB, prompts, and the hard rule that the new portal never names another
client's brand."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from runtime.tools.agents_demo import data
from runtime.tools.agents_demo.brand import BRAND, render

ROOT = Path(__file__).resolve().parents[2]

# Every file this portal ADDS. The word below is assembled so this file does
# not contain it either.
_FORBIDDEN = "gei" + "dea"
PORTAL_PATHS = [
    "runtime/tools/agents_demo", "runtime/agents/agents_demo.py", "runtime/prompts/agents_demo.py",
    "admin/seed_agents_demo.py", "endpoints/agents_portal.py", "templates/agents", "static/agents",
    "scripts/ingest_agents_demo_kb.py", "scripts/kb_content/agents_demo",
    "scripts/agents_demo_inbound_prompt_only.py",
    "supabase/migrations/20261007120000_add_agents_demo_agent_kinds.sql",
    "manual_tests/probe_agents_demo_chat.py", "manual_tests/probe_agents_demo_voice.py",
    "tests/agents_demo",
]
_ARABIC_INDIC = re.compile(r"[٠-٩۰-۹]")
STORY_KEYS = ("title", "headline", "summary", "who", "happened", "goal", "twist", "great")
SUMMARY_MAX_WORDS_EN = 40
SUMMARY_MAX_CHARS = 200


def _portal_files():
    for rel in PORTAL_PATHS:
        p = ROOT / rel
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
        for f in files:
            if "__pycache__" not in f.parts:
                yield f


def test_portal_files_never_name_another_client():
    hits = [str(f.relative_to(ROOT)) for f in _portal_files()
            if _FORBIDDEN in f.read_text(encoding="utf-8", errors="ignore").lower()]
    assert hits == []


def test_brand_is_named_in_one_place_only():
    """Fixtures and KB carry placeholders; only brand.py spells the name."""
    name = BRAND["name_en"]
    offenders = [str(f.relative_to(ROOT)) for f in _portal_files()
                 if f.suffix in (".json", ".md", ".html", ".js", ".css")
                 and (name in f.read_text(encoding="utf-8") or BRAND["name_ar"] in f.read_text(encoding="utf-8"))]
    assert offenders == []


@pytest.mark.parametrize("agent", list(data.AGENTS))
def test_every_case_has_a_complete_bilingual_story(agent):
    for case in data.cases(agent):
        for key in STORY_KEYS:
            for lang in ("en", "ar"):
                text = case[f"{key}_{lang}"]
                assert text and "{{" not in text, (case["id"], key, lang)
        for lang in ("en", "ar"):
            assert 2 <= len(case[f"say_{lang}"]) <= 3, case["id"]
        assert 2 <= len(case["facts"]) <= 5, case["id"]


@pytest.mark.parametrize("agent", list(data.AGENTS))
def test_every_case_has_a_short_summary(agent):
    """One short paragraph — "who you are" + "what happened" — is what the
    landing page and the portal show instead of the two separate blocks."""
    for case in data.cases(agent):
        en, ar = case.get("summary_en", ""), case.get("summary_ar", "")
        assert en.strip() and ar.strip(), case["id"]
        assert len(en.split()) <= SUMMARY_MAX_WORDS_EN, (case["id"], len(en.split()))
        for lang, text in (("en", en), ("ar", ar)):
            assert len(text) <= SUMMARY_MAX_CHARS, (case["id"], lang, len(text))
            assert "\n" not in text and len(re.findall(r"[.!?؟]( |$)", text)) <= 2, (case["id"], lang)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


# Unicode emoji/pictograph ranges, kept independent of common.emoji_names so
# the check does not rely on the code it might one day be asked to verify.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U0001FC00-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF"
    "\u2300-\u23FF\uFE0F\u200D\U000E0020-\U000E007F]"
)


@pytest.mark.parametrize("agent", list(data.AGENTS))
def test_served_story_text_has_no_emoji(agent):
    from endpoints.agents_portal import public_cases

    cases = public_cases(agent)
    assert cases and all("emoji" not in c for c in cases)
    hits = [(c["id"], s) for c in cases for s in _strings(c) if _EMOJI.search(s)]
    assert hits == []


@pytest.mark.parametrize("agent", list(data.AGENTS))
def test_arabic_uses_western_digits(agent):
    for case in data.cases(agent):
        blob = " ".join(str(v) for k, v in case.items() if k.endswith("_ar"))
        assert not _ARABIC_INDIC.search(blob), case["id"]


def test_case_counts_and_ids():
    assert [len(data.cases(a)) for a in data.AGENTS] == [10, 8, 9, 9]
    for agent in data.AGENTS:
        ids = [c["id"] for c in data.cases(agent)]
        assert len(ids) == len(set(ids))


@pytest.mark.parametrize("agent", ["support", "collection", "sales"])
def test_cases_point_at_real_customers_and_facts_match(agent):
    for case in data.cases(agent):
        cust = data.customer_for_case(case)
        assert cust, case["id"]
        dob = "/".join(reversed(cust["dob"].split("-")))
        for fact in case["facts"]:
            label = fact["label_en"].lower()
            if "date of birth" in label and "mum" not in label:
                assert fact["value"] == dob, case["id"]
            if "id last 4" in label:
                assert fact["value"] == cust["id_last4"], case["id"]


def test_sales_offers_exist_and_are_not_already_owned():
    from runtime.tools.agents_demo.sales import has_products

    for case in data.cases("sales"):
        assert case["offer"] in data.products()
        if case["outcome"] != "already_has":
            assert case["offer"] not in has_products(data.customer_for_case(case)), case["id"]


def test_resolve_case():
    assert data.resolve_case("collection", None)["id"] == "C01"
    assert data.resolve_case("sales", "o09")["id"] == "O09"
    with pytest.raises(LookupError):
        data.resolve_case("collection", "C99")
    with pytest.raises(LookupError):
        data.cases("nope")


def test_customers_are_well_formed():
    mobiles = [c["mobile"] for c in data.customers().values()]
    assert len(set(mobiles)) == len(mobiles)
    for acct, c in data.customers().items():
        assert len(acct) == 8 and acct == c["account_number"]
        assert c["area_id"] in data.areas()
        for b in c["bills"]:
            assert round(sum(i["amount"] for i in b["items"]), 2) == b["amount"]


def test_kb_corpus_is_bilingual_and_renders():
    en = sorted(p.name for p in (ROOT / "scripts/kb_content/agents_demo/en").glob("*.md"))
    ar = sorted(p.name for p in (ROOT / "scripts/kb_content/agents_demo/ar").glob("*.md"))
    assert en == ar and len(en) == 12
    for p in (ROOT / "scripts/kb_content/agents_demo").rglob("*.md"):
        text = render(p.read_text(encoding="utf-8"))
        assert "{{" not in text and text.startswith("# ")
        assert not _ARABIC_INDIC.search(text), p.name


def test_kb_prices_match_the_catalog():
    text = (ROOT / "scripts/kb_content/agents_demo/en/01-plans-and-pricing.md").read_text(encoding="utf-8")
    for pid in ("home_fiber_300", "home_fiber_500", "home_fiber_1g", "home_5g_wireless", "mobile_plus"):
        p = data.products()[pid]
        assert str(p["price"]["AE"]) in text and str(p["price"]["SA"]) in text, pid


def test_brand_render():
    assert render("Welcome to {{brand}} / {{ brand_ar }}") == f"Welcome to {BRAND['name_en']} / {BRAND['name_ar']}"
    assert render("no tokens") == "no tokens"


# ------------------------------------------------------------------ prompts
_PERSONA_VARS = {"{language}", "{arabic_dialect}", "{english_accent}"}


def test_prompts_validate_and_carry_only_known_placeholders():
    from core.prompt_validator import has_errors, validate_prompt
    from runtime.prompts import agents_demo as p

    inbound = {"agents_support", "agents_inquiries"}
    for kind, slug, _desc, content in p.prompt_specs():
        assert not has_errors(validate_prompt(content, name=None)), (kind, slug)
        if kind in inbound:  # prompt-only: both blocks rendered in, no tool catalog
            allowed = {"{facts_sheet}", "{case_file}"} | (_PERSONA_VARS if slug != "system" else set())
            assert {"{facts_sheet}", "{case_file}"} <= set(re.findall(r"\{[a-z_]+\}", content)), (kind, slug)
        else:
            allowed = ({"{available_tools}"} if slug == "system"
                       else _PERSONA_VARS | {"{callee_name}", "{case_file}"})
        assert set(re.findall(r"\{[a-z_]+\}", content)) <= allowed, (kind, slug)
        assert "<<" not in content and BRAND["name_en"] in content


def test_inbound_personas_keep_the_single_dispatch_rule():
    from runtime.prompts.agents_demo import INQUIRIES_VOICE_PERSONA, SUPPORT_VOICE_PERSONA

    for persona in (SUPPORT_VOICE_PERSONA, INQUIRIES_VOICE_PERSONA):
        assert re.search(r"[Cc]all\s+`ask_agent` with what they said,\s+immediately", persona)
        assert "استدعِ `ask_agent` فوراً" in persona
        assert "anything else" in persona  # the trailing offer is banned BY NAME
        assert "A dictated IBAN is COLLECTED" not in persona  # IBAN protocol dropped


def test_outbound_personas_are_prefetched_context_personas():
    from runtime.prompts.agents_demo import COLLECTION_CALL_PERSONA, SALES_CALL_PERSONA

    for persona in (COLLECTION_CALL_PERSONA, SALES_CALL_PERSONA):
        assert "{case_file}" in persona and "{callee_name}" in persona
        assert "ask_agent" not in persona
        assert "CALL FIRST, SPEAK AFTER" in persona
        assert "numerals" in persona


def test_greeting_triggers_are_english_instructions():
    from runtime.prompts.agents_demo import GREETING_TRIGGERS

    for trigger in GREETING_TRIGGERS.values():
        assert not re.search(r"[؀-ۿ]", trigger)
