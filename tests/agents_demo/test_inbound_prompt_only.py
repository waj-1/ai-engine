"""The prompt-only inbound agents (support, inquiries): the case file and the
facts sheet rendered into the prompt, NO tools in voice or text, the
verification-before-details rule, and the portal plumbing that carries the
selected case."""
from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import pytest

from runtime.tools.agents_demo import data, inbound

TODAY = date(2026, 10, 7)  # a Wednesday
INBOUND = ("support", "inquiries")
_ARABIC = re.compile(r"[؀-ۿ]")
_ARABIC_INDIC = re.compile(r"[٠-٩۰-۹]")


def _cases():
    return [(key, case["id"]) for key in INBOUND for case in data.cases(key)]


# ------------------------------------------------------------------ case files
@pytest.mark.parametrize("key,case_id", _cases())
@pytest.mark.parametrize("lang", ["en", "ar"])
def test_every_inbound_case_renders_in_both_languages(key, case_id, lang):
    case = data.resolve_case(key, case_id)
    text = inbound.case_file(key, case, lang, today=TODAY)
    assert "{" not in text and "}" not in text and "<<" not in text  # safe inside format_map
    assert not _ARABIC_INDIC.search(text)  # numerals stay 0-9 for the voice
    assert bool(_ARABIC.search(text)) == (lang == "ar") or key == "support"  # support EN quotes «name_ar»
    assert "2026-10-07" in text
    # Every action the agent may complete carries a reference decided HERE.
    refs = re.findall(r"\b(?:REF|VIS|SRV|TCK|HND|LEAD|CB)-[0-9A-F]{6}\b", text)
    assert refs and len(set(refs)) == len(refs)
    assert set(refs) <= set(inbound.case_references(key, case).values())


@pytest.mark.parametrize("case_id", [c["id"] for c in data.cases("support")])
def test_support_case_file_hides_the_verification_answers_but_keeps_the_record(case_id):
    case = data.resolve_case("support", case_id)
    cust = data.customer(case["customer_id"])
    for lang in ("en", "ar"):
        text = inbound.case_file("support", case, lang, today=TODAY)
        head, record = re.split(r"ACCOUNT RECORD|سجل الحساب", text, maxsplit=1)
        # The expected answers are NOT in the prompt: only the verify tool
        # knows them (a realtime model told them judged wrong ones correct).
        for value in (cust["account_number"], cust["mobile"], cust["dob"],
                      "/".join(reversed(cust["dob"].split("-")))):
            assert value not in text, (case_id, lang, value)
        assert cust["id_last4"] not in head and "support_verify_customer" in head
        # The record carries what the tools used to return.
        for bill in cust["bills"]:
            assert data._num(bill["amount"]) in record
        if cust.get("duplicate_payment"):
            assert all(r in record for r in cust["duplicate_payment"]["refs"])
        for ticket in cust.get("tickets", []):
            assert ticket["ref"] in record


@pytest.mark.parametrize("case_id", [c["id"] for c in data.cases("support")])
def test_support_story_card_numbers_are_backed_by_the_prompt(case_id):
    """What the tester's card shows (amounts, dates, refs) is what the agent
    can find in its case file or the facts sheet."""
    case = data.resolve_case("support", case_id)
    prompt = inbound.case_file("support", case, "en", today=TODAY) + inbound.facts_sheet("en")
    folded = prompt.replace(",", "")
    cust = data.customer(case["customer_id"])
    # Identity answers live only behind the verify tool, never in the prompt.
    secret = " ".join((cust["account_number"], cust["mobile"], cust["id_last4"], cust["dob"]))
    for fact in case["facts"]:
        for number in re.findall(r"\d+(?:\.\d+)?", fact["value"]):
            if number in secret.replace("-", " ") or number in secret:
                continue
            assert number in folded, (case_id, fact["label_en"], number)


def test_references_are_stable_and_language_independent():
    case = data.resolve_case("support", "S04")
    en = inbound.case_file("support", case, "en", today=TODAY)
    ar = inbound.case_file("support", case, "ar", today=TODAY)
    refs = inbound.case_references("support", case)
    assert refs == inbound.case_references("support", case)
    assert refs["refund"] in en and refs["refund"] in ar
    other = inbound.case_references("support", data.resolve_case("support", "S03"))
    assert set(refs.values()).isdisjoint(other.values())


def test_case_specific_content():
    s04 = inbound.case_file("support", data.resolve_case("support", "S04"), "en", today=TODAY)
    assert "DUPLICATE PAYMENT" in s04 and "PAY-55120" in s04 and "Refund of the duplicate payment" in s04
    s03 = inbound.case_file("support", data.resolve_case("support", "S03"), "en", today=TODAY)
    assert "no refund is owed" in s03 and "Refund of the duplicate" not in s03
    assert "+75 riyals" in s03  # the change versus the previous bill, computed here
    s02 = inbound.case_file("support", data.resolve_case("support", "S02"), "en", today=TODAY)
    assert "live outage" in s02 and "slots" not in s02  # no technician during an outage
    s06 = inbound.case_file("support", data.resolve_case("support", "S06"), "en", today=TODAY)
    assert "Technician needed: YES" in s06 and "free (the fault is on the network side)" in s06
    assert "Thursday 8 October 2026, 9 am to 12 noon" in s06  # next working days, Fri/Sat skipped
    s08 = inbound.case_file("support", data.resolve_case("support", "S08"), "en", today=TODAY)
    assert "1,186.50 riyals" in s08 and "Pause home service" in s08
    s07 = inbound.case_file("support", data.resolve_case("support", "S07"), "en", today=TODAY)
    assert "eSIM" in s07 and "m•••@mail.example" in s07 and "Cancel home service" not in s07
    s10 = inbound.case_file("support", data.resolve_case("support", "S10"), "en", today=TODAY)
    assert "Mona Nasser" in s10 and "Anyone else" in s10
    i07 = inbound.case_file("inquiries", data.resolve_case("inquiries", "I07"), "en", today=TODAY)
    assert "Sunday 11 October 2026" in i07 and "LEAD-" in i07 and "CB-" in i07
    assert "Lama" not in i07  # a prospect's details are never pre-known


# ------------------------------------------------------------------ facts sheet
@pytest.mark.parametrize("lang", ["en", "ar"])
def test_facts_sheet_is_rendered_from_the_catalog(lang):
    sheet = inbound.facts_sheet(lang)
    assert "{" not in sheet and "<<" not in sheet and not _ARABIC_INDIC.search(sheet)
    for pid, product in data.products().items():
        assert product[f"name_{lang}"] in sheet, pid
        for country in ("AE", "SA"):
            assert data._num(product["price"][country]) in sheet, (pid, country)
    for area in data.areas().values():
        assert area[f"name_{lang}"] in sheet
    for outage in data.outages():
        assert outage[f"summary_{lang}"] in sheet
    for item in data.catalog()["not_offered"]:
        assert item[lang] in sheet


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_facts_sheet_stays_compact(lang):
    import tiktoken

    tokens = len(tiktoken.get_encoding("o200k_base").encode(inbound.facts_sheet(lang)))
    assert tokens <= {"en": 2600, "ar": 3100}[lang], tokens


def test_kb_steps_come_from_the_kb_and_both_languages_mirror_it():
    for prefix in {p for p, _i, _k in inbound._KB_STEPS}:
        assert len(inbound.kb_sections("en", prefix)) == len(inbound.kb_sections("ar", prefix)), prefix
    for prefix, index, _key in inbound._KB_STEPS:
        en, ar = inbound.kb_steps("en", prefix, index), inbound.kb_steps("ar", prefix, index)
        assert en and len(en) == len(ar), (prefix, index)
        assert all(step in inbound.facts_sheet("en") for step in en)
    assert "Wait 30 seconds." in inbound.facts_sheet("en")


def test_policy_numbers_match_the_kb():
    """The facts sheet's policy numbers are catalog values; the KB states the
    same ones, so the two can never tell a caller different things."""
    kb = " ".join(p.read_text(encoding="utf-8") for p in (inbound.KB_DIR / "en").glob("*.md"))
    pol = data.policies()
    assert f"within {pol['equipment_return_days']} days" in kb
    assert f"at least {pol['relocation_notice_working_days']} working days" in kb
    assert f"{pol['refund_working_days']} working days" in kb
    assert f"within {pol['dispute_window_days']} days" in kb
    assert f"{pol['fiber_install_working_days']} working days" in kb


# ------------------------------------------------------------------ prompts
def test_prompts_carry_the_verification_rule_and_only_the_verify_tool():
    from runtime.prompts import agents_demo as p

    for text in (p.SUPPORT_DIRECT_VOICE_PERSONA, p.SUPPORT_DIRECT_SYSTEM_PROMPT):
        assert "Verification — before ANY account detail" in text
        assert "support_verify_customer" in text and "never judge a match yourself" in text
        assert "share NOTHING from the ACCOUNT RECORD" in text
        assert "never which part" in text and "another person's account".upper() in text.upper()
    for text in (p.SUPPORT_DIRECT_VOICE_PERSONA, p.INQUIRIES_DIRECT_VOICE_PERSONA,
                 p.SUPPORT_DIRECT_SYSTEM_PROMPT, p.INQUIRIES_DIRECT_SYSTEM_PROMPT):
        assert "ask_agent" not in text and "{available_tools}" not in text
        assert "{facts_sheet}" in text and "{case_file}" in text
        # The case file comes last: every call in a language shares the long prefix.
        assert text.rindex("{case_file}") > text.rindex("{facts_sheet}")
        assert "never invent a reference" in text.lower() or "Never invent a reference" in text
    for persona in (p.SUPPORT_DIRECT_VOICE_PERSONA, p.INQUIRIES_DIRECT_VOICE_PERSONA):
        assert "anything else" in persona  # the trailing offer is banned BY NAME
        assert "numerals" in persona and "{language}" in persona
        assert "Never introduce" in persona  # never re-greet
        assert "Never end a turn on \"let me check\"" in persona
        assert "never speak" in persona.lower() and "function call" in persona


def test_seed_maps_serve_the_prompt_only_versions():
    from runtime.prompts import agents_demo as p

    assert p.SYSTEM_PROMPTS["support"] is p.SUPPORT_DIRECT_SYSTEM_PROMPT
    assert p.VOICE_PERSONAS["inquiries"] is p.INQUIRIES_DIRECT_VOICE_PERSONA
    assert p.LEGACY_INBOUND_SYSTEM_PROMPTS["support"] is p.SUPPORT_SYSTEM_PROMPT


# ------------------------------------------------------------------ the agent
@pytest.fixture
def prompt_only_profile(monkeypatch):
    def _init(self, agent_kind, environment=None, profile_slug=None):
        self.profile = SimpleNamespace(profile_id="p", slug="s", data_source_ids=["kb-1"], model_main={},
                                       is_fallback=False, resolved_prompts=None, prompt_version_ids={},
                                       voice_config={"prefetched_context": True, "enabled": True})

    monkeypatch.setattr("runtime.agent.RuntimeAgent.__init__", _init)


@pytest.mark.parametrize("key", INBOUND)
def test_prompt_only_agent_has_no_tools_in_voice_or_text(prompt_only_profile, key):
    from runtime.agents.agents_demo import create_demo_agent

    agent = create_demo_agent(key, locale="ar-SA", call_case=data.cases(key)[-1]["id"])
    assert agent.prompt_only is True
    assert agent._build_tools({"call_state": agent.state, "locale": "ar"}) == []
    # Support keeps exactly one tool, the identity check, in both channels.
    want = ["support_verify_customer"] if key == "support" else []
    ctx = {"call_state": agent.state, "locale": "ar"}
    assert [t.name for t in agent._code_tools(ctx)] == want
    assert [d["name"] for d, _fn in agent.direct_voice_tools()] == want
    variables = agent.persona_variables
    assert set(variables) == {"facts_sheet", "case_file", "arabic_dialect", "english_accent"}
    assert variables["facts_sheet"] == inbound.facts_sheet("ar", inbound.case_country(key, agent.case))
    assert inbound.case_country(key, agent.case) is not None
    assert variables["case_file"] == inbound.case_file(key, agent.case, "ar")
    assert agent._prompt_variables().items() <= variables.items()


def test_prompt_only_agent_defaults_to_the_first_case_and_rejects_unknown(prompt_only_profile):
    from runtime.agents.agents_demo import create_demo_agent

    assert create_demo_agent("support").case["id"] == "S01"
    with pytest.raises(LookupError):
        create_demo_agent("inquiries", call_case="I99")


class _NoToolsLLM:
    """Answers once; fails the test if anything tries to bind tools."""

    def __init__(self):
        self.seen = None

    def bind_tools(self, tools):
        names = [t.name for t in tools]
        if names != ["support_verify_customer"]:  # the identity check is the only tool
            raise AssertionError(f"tools bound on a prompt-only turn: {names}")
        self.bound = names
        return self

    async def astream_events(self, messages, version="v2"):
        self.seen = messages
        chunk = SimpleNamespace(content="Sure — let me pull that up… okay.", tool_call_chunks=[])
        yield {"event": "on_chat_model_stream", "data": {"chunk": chunk}}
        yield {"event": "on_chat_model_end",
               "data": {"output": SimpleNamespace(content=chunk.content, tool_calls=[], usage_metadata=None)}}


async def test_text_turn_is_one_tool_less_model_call(prompt_only_profile):
    from runtime.agents.agents_demo import create_demo_agent

    agent = create_demo_agent("support", call_case="S04")
    agent.llm = _NoToolsLLM()
    seen = {}

    def _prepare(ctx, all_tools):
        seen.update(ctx=ctx, tools=all_tools)
        prompt = "RULES\n{facts_sheet}\n{case_file}".format(**ctx["prompt_variables"])
        return {"system_prompt": prompt, "tools": [], "tools_by_name": {}}

    agent.prepare = _prepare
    events = [e async for e in agent.run_stream("My card was charged twice", {"previous_messages": []})]
    assert seen["tools"] == []
    assert "PAY-55121" in seen["ctx"]["prompt_variables"]["case_file"]
    assert seen["ctx"]["fallback_system_prompt"].startswith("You are the AI customer-support assistant")
    usage = next(e for e in events if e["type"] == "usage")
    assert usage["tool_count"] == 0 and usage["iterations"] == 1
    turn_ctx = agent.llm.seen[1].content
    assert "pass to a tool" not in turn_ctx and "Already established" not in turn_ctx


# ------------------------------------------------------------------ voice edge
def _cfg(**kw):
    from voice.live.config import LiveVoiceConfig

    cfg = LiveVoiceConfig()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_voice_edge_declares_no_tools_for_a_prompt_only_agent(prompt_only_profile):
    from endpoints.voice_live import _direct_tools_for
    from runtime.agents.agents_demo import create_demo_agent
    from voice.live.providers import azure_voice_live, gemini_live

    cfg = _cfg(prefetched_context=True, kb_prefetch_enabled=True, answer_watchdog_sec=8.0)
    handlers = _direct_tools_for(cfg, create_demo_agent("inquiries"))
    assert handlers == {} and cfg.direct_tools == [] and cfg.tools_disabled is True
    # Every ask_agent-shaped mechanism stood down, as in the action-tool mode.
    assert cfg.kb_prefetch_enabled is False and cfg.answer_watchdog_sec == 0.0
    assert gemini_live._function_declarations(cfg) == []
    payload = azure_voice_live._session_payload(cfg, "persona")
    session = payload.get("session", payload)
    assert session["tools"] == [] and session["tool_choice"] == "none"
    # Support declares only its identity check — ask_agent machinery still stands down.
    scfg = _cfg(prefetched_context=True, kb_prefetch_enabled=True, answer_watchdog_sec=8.0)
    shandlers = _direct_tools_for(scfg, create_demo_agent("support"))
    assert list(shandlers) == ["support_verify_customer"]
    assert [d["name"] for d in gemini_live._function_declarations(scfg)] == ["support_verify_customer"]
    assert scfg.kb_prefetch_enabled is False and scfg.answer_watchdog_sec == 0.0
    wrong = shandlers["support_verify_customer"]({"account_number": "41209908", "date_of_birth": "1970-01-01"})
    assert wrong["ok"] is False


def test_voice_edge_unchanged_for_delegated_and_outbound():
    from endpoints.voice_live import _direct_tools_for
    from voice.live.providers import gemini_live

    delegated = _cfg(prefetched_context=False)
    assert _direct_tools_for(delegated, object()) == {} and delegated.tools_disabled is False
    assert [d["name"] for d in gemini_live._function_declarations(delegated)] == ["ask_agent"]

    class _Outbound:
        def direct_voice_tools(self):
            return [({"name": "send_payment_link", "description": "x", "parameters": {}}, lambda a: {})]

    outbound = _cfg(prefetched_context=True)
    assert list(_direct_tools_for(outbound, _Outbound())) == ["send_payment_link"]
    assert outbound.tools_disabled is False
    assert [d["name"] for d in gemini_live._function_declarations(outbound)] == ["send_payment_link"]


# The caller's country decides the price sheet. With both countries' numbers
# side by side ("99 dirhams (UAE) / 95 riyals (KSA)") Gemini Live quoted the
# UAE number in riyals on an Arabic inquiries call.
_OTHER_CURRENCY = {
    ("en", "AE"): ("riyal", "SAR"), ("en", "SA"): ("dirham", "AED"),
    ("ar", "AE"): ("ريال",), ("ar", "SA"): ("درهم",),
}


@pytest.mark.parametrize("lang,country", sorted(_OTHER_CURRENCY))
def test_country_sheet_quotes_only_that_countrys_currency(lang, country):
    sheet = inbound.facts_sheet(lang, country)
    for word in _OTHER_CURRENCY[(lang, country)]:
        assert word not in sheet, (lang, country, word)
    backup = data.products()["biz_backup_5g"]
    if backup:
        assert data.money(backup["price"][country], country, lang) in sheet


@pytest.mark.parametrize("key,case_id", _cases())
def test_every_inbound_case_knows_its_callers_country(key, case_id):
    case = data.resolve_case(key, case_id)
    assert inbound.case_country(key, case) in ("AE", "SA")


def test_agent_renders_the_callers_country_sheet():
    case = data.resolve_case("inquiries", "I02")  # Riyadh
    assert inbound.case_country("inquiries", case) == "SA"
    sheet = inbound.facts_sheet("ar", "SA")
    assert "درهم" not in sheet and "ريال" in sheet
