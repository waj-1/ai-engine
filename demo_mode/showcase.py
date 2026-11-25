"""The showcase: four use-case agents a product page talks to anonymously.

A public demo shows how agents are BUILT. The showcase shows what a finished
product made from such agents looks like: a product page on another site
(``DEMO_SHOWCASE_ORIGINS``) runs a live chat and voice demo of four agents of
the fictional home-internet and mobile provider Falak Connect
(runtime/tools/agents_demo/brand.py):

    collection   an outbound call about an overdue bill
    sales        an outbound call with an upgrade offer
    support      inbound customer support
    inquiries    inbound questions about plans and prices

They are ordinary portal agents (``custom`` kind, slugs in `AGENTS`), built
the platform's way: instructions, a model, a knowledge base (the Falak
Connect help center) and a voice setup, seeded by `demo_mode.seed` so they
come back after the nightly reset, read-only in the portal
(`demo_mode.protect`) so a visitor cannot break the product page. The only
thing a product page adds is the use case: a visitor picks a story card
(`cases`), and its case file (who the customer is, their record, what the
call should do) is handed to the agent for that conversation (`brief`). The
story cards, the customers and their records are the fixtures in
runtime/tools/agents_demo/data, the same ones the engine's sample-agents
portal reads.

The public entry is narrow (demo_mode/showcase_routes.py):

* only these four agents, only the curated cases;
* only from an allowed Origin (a browser cannot lie about it; any other
  client can, which is why the caps below never depend on it);
* its own caps, apart from the portal's: ``DEMO_SHOWCASE_DAILY_TURNS`` chat
  turns a day and ``DEMO_SHOWCASE_CHAT_PER_HOUR`` per visitor; for voice
  ``DEMO_SHOWCASE_VOICE_DAILY_SECONDS`` a day,
  ``DEMO_SHOWCASE_VOICE_MAX_CALL_SECONDS`` per call,
  ``DEMO_SHOWCASE_VOICE_MAX_CALLS`` at once and
  ``DEMO_SHOWCASE_VOICE_CALLS_PER_HOUR`` per visitor. The day's totals, the
  call length and the calls at once hold whatever address a caller claims;
  only the per-visitor limits key on the address.

Every refusal says why in words, with a short code in brackets first
(``[budget]``, ``[busy]``, ``[rate]``, ``[length]``, ``[off]``) so the
product page can show its own sentence.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable, Dict, List, Optional

from demo_mode import limits, settings

logger = logging.getLogger(__name__)

SEED_MARK = "demo_mode.showcase"
CATEGORY = {"slug": "falak-connect", "display_name": "Falak Connect use cases", "color": "blue",
            "sort_order": 3,
            "description": "Four agents a product page runs live: an end product built from agents "
                           "made on this platform."}
KB_NAME = "Falak Connect help center"
CHANNEL = {"slug": "product-page-showcase", "name": "Product page showcase", "kind": "embed",
           "description": "A product page on another site that runs the Falak Connect agents live. "
                          "It may talk to those four agents only."}

TURNS_BUCKET = "showcase_turns"
VOICE_BUCKET = "showcase_voice_seconds"
CHAT_LIMIT_BUCKET = "showcase_chat"
VOICE_LIMIT_BUCKET = "showcase_voice"
#: The token a showcase call sends as its first frame. Not a secret: the
#: showcase is anonymous by design; the frame is the voice protocol's.
CALL_TOKEN = "showcase"

STEP_SEC = 15
CLOSE_CODE = 4429
_TEARDOWN_SEC = 20.0

MSG_OFF = "[off] Voice calls are not available here right now. Try the chat."
MSG_BUDGET = "[budget] Today's voice minutes are used up. Try the chat, or come back tomorrow."
MSG_BUSY = "[busy] All voice lines are in use. Try again in a minute, or try the chat."
MSG_RATE = "[rate] You have placed a lot of calls this hour. Try the chat, or try again later."
MSG_CHAT_BUDGET = ("[budget] Today's chat messages for this demo are used up. Come back tomorrow "
                   "(they reset at midnight UTC).")
MSG_CHAT_RATE = "[rate] You are sending messages a little fast. Wait a few minutes and try again."
MSG_ORIGIN = "This page may not use the showcase."


assert all(len(m.encode()) <= 120 for m in (MSG_OFF, MSG_BUDGET, MSG_BUSY, MSG_RATE, MSG_ORIGIN))


def length_message() -> str:
    limit = settings.showcase_voice_max_call_seconds()
    span = f"{limit // 60} minutes" if limit >= 120 and limit % 60 == 0 else f"{limit} seconds"
    return f"[length] Demo calls end after {span}. Thanks for trying it."


@dataclass(frozen=True)
class ShowcaseAgent:
    key: str            # the product page's name for it, and the fixtures' prefix
    slug: str           # the portal agent
    display_name: str
    direction: str      # inbound | outbound
    voice: str          # "f" | "m": which voices it speaks with
    description: str


AGENTS: Dict[str, ShowcaseAgent] = {
    "collection": ShowcaseAgent(
        "collection", "falak-collections", "Falak Connect: overdue bills", "outbound", "f",
        "Calls a customer about an overdue bill: checks it is the right person, explains the "
        "amount and sends a payment link after a yes. A use case on the product page."),
    "sales": ShowcaseAgent(
        "sales", "falak-offers", "Falak Connect: upgrade offers", "outbound", "m",
        "Calls an existing customer with one offer that fits how they use the service, and "
        "sends the sign-up link after a yes. A use case on the product page."),
    "support": ShowcaseAgent(
        "support", "falak-support", "Falak Connect: customer support", "inbound", "f",
        "Answers customers about bills, outages, Wi-Fi and their plan, after checking who "
        "they are. A use case on the product page."),
    "inquiries": ShowcaseAgent(
        "inquiries", "falak-plans", "Falak Connect: plans and pricing", "inbound", "m",
        "Answers people who are not customers yet: plans, prices, coverage and installation. "
        "A use case on the product page."),
}
AGENT_BY_SLUG = {a.slug: a for a in AGENTS.values()}
SLUGS = frozenset(AGENT_BY_SLUG)

#: The curated use cases, three per agent, in the product page's order.
CASES: Dict[str, List[str]] = {
    "collection": ["C01", "C06", "C07"],
    "sales": ["O01", "O05", "O09"],
    "support": ["S04", "S02", "S10"],
    "inquiries": ["I01", "I06", "I05"],
}

#: The language & accent picker's tags. Each language's first tag is its default.
SPEECH_TAGS = ("en-US", "en-GB", "en-AU", "en-IN", "ar-SA", "ar-AE", "ar-EG", "ar-JO")
_DIALECTS = {"ar-SA": "Saudi", "ar-AE": "Emirati", "ar-EG": "Egyptian", "ar-JO": "Jordanian"}
_ACCENTS = {"en-US": "American", "en-GB": "British", "en-AU": "Australian", "en-IN": "Indian"}

#: Azure neural voices per tag (Azure Voice Live, cascaded). The English
#: "Multilingual" voices also speak Arabic, so a caller who switches language
#: mid-call is still answered; the regional Arabic voices give the accent.
_VOICES = {
    "f": {"en-US": "en-US-AvaMultilingualNeural", "en-GB": "en-GB-AdaMultilingualNeural",
          "en-AU": "en-AU-NatashaNeural", "en-IN": "en-IN-NeerjaNeural",
          "ar-SA": "ar-SA-ZariyahNeural", "ar-AE": "ar-AE-FatimaNeural",
          "ar-EG": "ar-EG-SalmaNeural", "ar-JO": "ar-JO-SanaNeural",
          "en": "en-US-AvaMultilingualNeural", "ar": "ar-AE-FatimaNeural"},
    "m": {"en-US": "en-US-AndrewMultilingualNeural", "en-GB": "en-GB-OllieMultilingualNeural",
          "en-AU": "en-AU-WilliamMultilingualNeural", "en-IN": "en-IN-PrabhatNeural",
          "ar-SA": "ar-SA-HamedNeural", "ar-AE": "ar-AE-HamdanNeural",
          "ar-EG": "ar-EG-ShakirNeural", "ar-JO": "ar-JO-TaimNeural",
          "en": "en-US-AndrewMultilingualNeural", "ar": "ar-AE-HamdanNeural"},
}


def normalize_tag(locale: Optional[str]) -> str:
    """A picker tag (``ar-AE``), or the default of the language named, or en-US."""
    raw = (locale or "").strip().replace("_", "-")
    parts = raw.split("-")
    tag = "-".join([parts[0].lower()] + [p.upper() for p in parts[1:]]) if raw else ""
    if tag in SPEECH_TAGS:
        return tag
    return "ar-SA" if tag.startswith("ar") else "en-US"


def lang_of(tag: str) -> str:
    return "ar" if tag.startswith("ar") else "en"


# --------------------------------------------------------------------------
# Origins
# --------------------------------------------------------------------------


def origin_allowed(origin: Optional[str]) -> bool:
    """Is this Origin one of ``DEMO_SHOWCASE_ORIGINS`` (or the demo itself)?"""
    value = (origin or "").strip().rstrip("/").lower()
    return bool(value) and value in settings.showcase_origins()


def external_origins() -> List[str]:
    """The product pages' origins, without the demo's own address."""
    own = settings.public_url().lower()
    return [o for o in settings.showcase_origins() if o != own]


# --------------------------------------------------------------------------
# Use cases: the story cards and the case file each conversation is handed
# --------------------------------------------------------------------------

_STORY_KEYS = (
    "id", "title_en", "title_ar", "headline_en", "headline_ar", "summary_en", "summary_ar",
    "who_en", "who_ar", "happened_en", "happened_ar", "goal_en", "goal_ar", "say_en", "say_ar",
    "say_more_en", "say_more_ar", "twist_en", "twist_ar", "great_en", "great_ar", "facts",
)
_EM_DASH = re.compile(r"\s*—\s*")


def _no_em_dash(value: Any) -> Any:
    if isinstance(value, str):
        comma = "\u060c " if re.search(r"[\u0600-\u06ff]", value) else ", "
        return _EM_DASH.sub(comma, value).strip(comma).strip()
    if isinstance(value, list):
        return [_no_em_dash(v) for v in value]
    if isinstance(value, dict):
        return {k: _no_em_dash(v) for k, v in value.items()}
    return value


def _overrides() -> Dict[str, Dict[str, Any]]:
    try:
        from demo_mode.showcase_stories import STORIES
    except Exception:  # noqa: BLE001 (the fixtures' own wording serves)
        return {}
    return STORIES


def case_record(key: str, case_id: str) -> Dict[str, Any]:
    """The fixture case `case_id` of agent `key`. LookupError when it is not
    one of the curated cases."""
    from runtime.tools.agents_demo import data

    if key not in AGENTS or case_id not in CASES[key]:
        raise LookupError(f"not a showcase case: {key}/{case_id}")
    for case in data.cases(key):
        if case["id"] == case_id:
            return case
    raise LookupError(f"no fixture case {key}/{case_id}")


def story(key: str, case_id: str) -> Dict[str, Any]:
    """One story card: the fixture's public fields, in the showcase's own
    wording where `demo_mode.showcase_stories` has it, never an em dash."""
    case = case_record(key, case_id)
    card = {k: case[k] for k in _STORY_KEYS if k in case}
    extra = dict(_overrides().get(case_id) or {})
    facts_ar = extra.pop("facts_ar", None)
    facts_en = extra.pop("facts_en", None)
    card.update({k: v for k, v in extra.items() if k in _STORY_KEYS})
    for patch in (facts_ar, facts_en):
        if patch and isinstance(card.get("facts"), list) and len(patch) == len(card["facts"]):
            card["facts"] = [{**fact, **(p or {})} for fact, p in zip(card["facts"], patch)]
    return _no_em_dash(card)


def stories(key: str) -> List[Dict[str, Any]]:
    return [story(key, cid) for cid in CASES[key]]


def _customer(case: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from runtime.tools.agents_demo import data

    return data.customer_for_case(case)


def _verification_line(key: str, case: Dict[str, Any]) -> str:
    cust = _customer(case) or {}
    dob = cust.get("dob", "")
    last4 = cust.get("id_last4", "")
    if key == "support":
        return ("VERIFICATION: before any account detail, the caller gives EITHER the account number "
                f"({cust.get('account_number', '')}) OR the registered mobile ({cust.get('mobile', '')}), "
                f"PLUS EITHER the date of birth ({dob}) OR the last 4 digits of the national ID ({last4}). "
                "Accept a match however it is said (any date format, Arabic or English digits). Never "
                "read these expected answers out, never hint at them, and after three failed tries "
                "offer a human colleague instead.")
    return ("Identity check: before any account detail, they confirm EITHER their date of birth "
            f"({dob}) OR the last 4 digits of their national ID ({last4}). Accept a match however it "
            "is said. Never read these expected answers out or hint at them. After three failed tries, "
            "say you cannot discuss the account on this call and offer a call back.")


def case_file(key: str, case: Dict[str, Any], lang: str) -> str:
    """The case file for one conversation, rendered from the fixtures, with
    the verification answers in it: these agents have no tools, so they check
    who they are talking to against the case file, in conversation."""
    from runtime.tools.agents_demo import collection, inbound, sales

    if key == "collection":
        text = collection.case_file(case)
    elif key == "sales":
        text = sales.case_file(case)
    else:
        text = inbound.case_file(key, case, lang)
    lines = []
    for line in text.splitlines():
        if ("confirmed ONLY by calling" in line or "You are NOT given the right answers" in line
                or "verify_customer" in line or "verify_identity" in line):
            line = _verification_line(key, case)
        lines.append(line)
    out = "\n".join(lines)
    if key in ("support", "inquiries"):
        country = inbound.case_country(key, case)
        out += "\n\n" + inbound.facts_sheet(lang, country)
    return out


def speech_line(tag: str) -> str:
    lang = lang_of(tag)
    if lang == "ar":
        return (f"Language: this conversation is in Arabic, {_DIALECTS.get(tag, 'Gulf')} dialect, light "
                "and clear. If the customer writes or speaks English, reply in English.")
    return (f"Language: this conversation is in English ({_ACCENTS.get(tag, 'neutral')} English). If "
            "the customer writes or speaks Arabic, reply in Arabic, in a light and clear Gulf dialect.")


def brief(key: str, case_id: str, locale: Optional[str]) -> str:
    """Everything one conversation is handed at its start."""
    tag = normalize_tag(locale)
    case = case_record(key, case_id)
    return speech_line(tag) + "\n\n" + case_file(key, case, lang_of(tag))


def greeting(key: str, case_id: str, locale: Optional[str]) -> str:
    """How the call opens (the realtime model says it, in the call's language)."""
    from runtime.tools.agents_demo.brand import BRAND

    tag = normalize_tag(locale)
    lang = lang_of(tag)
    brand = BRAND["name_ar"] if lang == "ar" else BRAND["name_en"]
    where = (f"Arabic, {_DIALECTS.get(tag, 'Gulf')} dialect" if lang == "ar"
             else f"English ({_ACCENTS.get(tag, 'neutral')})")
    agent = AGENTS[key]
    if agent.direction == "outbound":
        cust = _customer(case_record(key, case_id)) or {}
        name = cust.get("first_ar" if lang == "ar" else "first_en") or ""
        team = "accounts team" if key == "collection" else "customer team"
        return (f"This is an outbound call you are placing. Open it in one short sentence: say you are "
                f"an AI assistant calling from {brand}'s {team}, and ask whether you are speaking with "
                f"{name}. Say nothing about the account yet. Speak {where}.")
    role = "support line" if key == "support" else "plans and new connections line"
    return (f"Greet the caller in one short sentence: you are {brand}'s AI assistant on the {role}, "
            f"and ask how you can help. Speak {where}.")


# --------------------------------------------------------------------------
# The agents' configuration (seeded by demo_mode.seed)
# --------------------------------------------------------------------------

_COMMON_RULES = """\
## How you work
- You are an AI assistant with no personal name. In a chat, your first reply says you are an AI \
assistant. On a voice call the opening line has already said it: do not say it again.
- Each conversation starts with a section called "This conversation". It holds the case file: \
who the customer is, their record, today's date, what you may do and the reference of each \
action. Everything you state comes from it or from the knowledge base. Never invent a price, \
amount, date, reference or policy. If something is not there, say you do not have it and offer \
a colleague.
- Before any account detail, check who you are talking to as the case file says. Until that \
check passes, share nothing from the account, not even that a bill exists.
- An action (a link, a refund, a booking, a ticket) happens only after you state its exact terms \
and the customer says a clear yes. Then say it is done and give the reference from the case file, \
once. Nothing is really sent: this is a demo with fictional customers.
- Keep every reply short: one to three sentences. One question at a time. No lists unless asked. \
No trailing "anything else?" after every answer; ask it once, when the matter is settled.
- Reply in the language of the customer's latest message, in the dialect the case file names.
- Plain sentences only: no markdown, no bold, no bullet symbols, no dashes between clauses.
- If there is no "This conversation" section (someone is trying you in the portal), say you are a \
demo agent whose product page picks a customer for each conversation, and answer general \
questions about {brand} from the knowledge base."""

INSTRUCTIONS = {
    "collection": """\
You are an AI assistant on {brand}'s accounts team, on an outbound call to a customer whose \
bill is overdue. {brand} is a home internet and mobile provider in the UAE and Saudi Arabia.

## The call
1. You opened by asking if you are speaking with the customer. If it is someone else, say you \
will call back, ask when the customer is free, and share nothing about why you called.
2. Check their identity as the case file says.
3. State the remaining amount and the months it covers in one sentence, and ask to settle it \
today by a payment link sent by SMS to the registered mobile (say only its last four digits).
4. If they cannot pay it all: a promise to pay within the policy's days, or, only if they mention \
hardship or ask to split it, the instalment plan in the case file.
5. Be kind and calm. Never threaten, never waive or discount a fee, never send a link to another \
number.
""" + _COMMON_RULES,
    "sales": """\
You are an AI assistant on {brand}'s customer team, on an outbound call to an existing customer \
with one offer. {brand} is a home internet and mobile provider in the UAE and Saudi Arabia.

## The call
1. You opened by asking if you are speaking with the customer. If it is someone else, offer to \
call back and share nothing.
2. Check their identity as the case file says.
3. Ask ONE short question about the need the case file's signals point to, then offer THE OFFER \
in one sentence tied to their answer, with its exact price.
4. If they are interested, ask whether to send the details and sign-up link by SMS to the mobile \
ending in the case file's last four digits; send only after their yes.
5. If they say no, accept it. After a second no, thank them and end the call. Never invent a \
discount; there is none.
""" + _COMMON_RULES,
    "support": """\
You are {brand}'s AI customer-support assistant. {brand} is a home internet and mobile \
provider in the UAE and Saudi Arabia. Customers call or write about bills, payments, outages, \
Wi-Fi, their plan and moving home.

## Helping
- Ask what the problem is, then check who they are before any account detail.
- Network notices for an area are public: you may share them before the check.
- When they report a fault, say you are running a quick check on the line, then give the result \
from the case file.
- Offer a human colleague when they ask for one, when the problem repeats, or after three failed \
identity checks.
""" + _COMMON_RULES,
    "inquiries": """\
You are {brand}'s AI assistant for plans, prices and new connections. {brand} is a home \
internet and mobile provider in the UAE and Saudi Arabia. The people you talk to are not \
customers yet, so there is no identity check.

## Helping
- Learn what they need in one or two short questions (who lives there, what they use it for, \
the area), then recommend ONE plan with its exact monthly price and a one-line reason.
- Check coverage for their area from the facts sheet before you promise fiber.
- Prices are per country: quote only the caller's country's prices, as the facts sheet says.
- There are no discounts or promo codes. A 24-month price is a catalog price, not a discount.
- Offer to save them as a lead or book a callback only when they want one, as the case file says.
""" + _COMMON_RULES,
}


def instructions(key: str) -> str:
    from runtime.tools.agents_demo.brand import BRAND

    return INSTRUCTIONS[key].replace("{brand}", BRAND["name_en"])


def voice_config(key: str, model_id: str) -> Dict[str, Any]:
    """The agent's voice setup: one voice per language and accent."""
    agent = AGENTS[key]
    voices = dict(_VOICES[agent.voice])
    out: Dict[str, Any] = {
        "enabled": True,
        "model_id": model_id,
        "voice_id": voices["en-US"],
        "language": "en-US",
        "max_session_sec": settings.showcase_voice_max_call_seconds(),
        "max_concurrent_sessions": settings.showcase_voice_max_calls(),
        # The caller's mic hears the greeting's own echo; without this the
        # provider's turn detection cuts the greeting short.
        "greeting_input_mute_sec": 8,
        "greeting_trigger": ("Greet the caller in one short sentence: you are Falak Connect's AI "
                             "assistant, and ask how you can help."),
    }
    if settings.voice_provider() == "azure_voice_live":
        out["voice_by_language"] = voices
    return out


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------

_open_calls = 0
_sleep = asyncio.sleep
_now = time.monotonic


def reset_for_tests() -> None:
    global _open_calls
    _open_calls = 0


def open_calls() -> int:
    return _open_calls


def voice_seconds_left() -> int:
    return max(0, settings.showcase_voice_daily_seconds() - limits.used(VOICE_BUCKET))


def turns_left() -> int:
    return max(0, settings.showcase_daily_turns() - limits.used(TURNS_BUCKET))


def voice_state() -> str:
    """``open``, ``busy``, ``budget`` or ``off``: what the Call button can do now."""
    if not settings.showcase_voice_enabled():
        return "off"
    if voice_seconds_left() < STEP_SEC:
        return "budget"
    if _open_calls >= settings.showcase_voice_max_calls():
        return "busy"
    return "open"


def chat_state() -> str:
    return "budget" if turns_left() <= 0 else "open"


def take_chat_turn(ip: str) -> Optional[str]:
    """Count one chat turn. The refusal message, or None when it may run."""
    if not limits.hit(CHAT_LIMIT_BUCKET, ip=ip, session=None, limit=settings.showcase_chat_per_hour()):
        return MSG_CHAT_RATE
    if not limits.take(TURNS_BUCKET, settings.showcase_daily_turns(), 1):
        return MSG_CHAT_BUDGET
    return None


def _charge(seconds: int) -> bool:
    if seconds <= 0:
        return True
    return limits.take(VOICE_BUCKET, settings.showcase_voice_daily_seconds(), seconds)


def _charge_rest(seconds: int) -> None:
    if _charge(seconds):
        return
    left = voice_seconds_left()
    if left > 0:
        _charge(left)


async def _refuse(websocket: Any, reason: str) -> None:
    try:
        await websocket.accept()
        await websocket.close(code=CLOSE_CODE, reason=reason[:120])
    except Exception:  # noqa: BLE001 (the caller has already gone)
        pass


async def _meter(state: Dict[str, int]) -> str:
    limit = settings.showcase_voice_max_call_seconds()
    while True:
        wait = max(1, min(STEP_SEC, limit - state["charged"]))
        await _sleep(wait)
        if not await asyncio.to_thread(_charge, wait):
            return MSG_BUDGET
        state["charged"] += wait
        if state["charged"] >= limit:
            return length_message()


async def guard(websocket: Any, ip: str, run: Callable[[], Awaitable[None]]) -> None:
    """Serve one showcase call (`run`) under the showcase's caps."""
    global _open_calls
    if not settings.showcase_voice_enabled():
        return await _refuse(websocket, MSG_OFF)
    if await asyncio.to_thread(voice_seconds_left) < STEP_SEC:
        return await _refuse(websocket, MSG_BUDGET)
    # No await from here to the count going up: two handshakes cannot both
    # take the last line.
    if _open_calls >= settings.showcase_voice_max_calls():
        return await _refuse(websocket, MSG_BUSY)
    if not limits.hit(VOICE_LIMIT_BUCKET, ip=ip, session=None,
                      limit=settings.showcase_voice_calls_per_hour()):
        return await _refuse(websocket, MSG_RATE)
    _open_calls += 1
    started = _now()
    state = {"charged": 0}
    call = asyncio.ensure_future(run())
    meter = asyncio.ensure_future(_meter(state))
    try:
        await asyncio.wait({call, meter}, return_when=asyncio.FIRST_COMPLETED)
        if call.done():
            call.result()
            return
        reason = meter.result()
        logger.info("showcase voice: ending a call (%s)", reason)
        call.cancel()
        await asyncio.wait({call}, timeout=_TEARDOWN_SEC)
        try:
            await websocket.close(code=CLOSE_CODE, reason=reason[:120])
        except Exception:  # noqa: BLE001
            pass
    finally:
        _open_calls -= 1
        for task in (call, meter):
            if not task.done():
                task.cancel()
        rest = min(STEP_SEC, int(_now() - started) - state["charged"])
        if rest > 0:
            try:
                await asyncio.shield(asyncio.to_thread(_charge_rest, rest))
            except Exception:  # noqa: BLE001
                logger.warning("showcase voice: could not charge the end of a call", exc_info=True)


# --------------------------------------------------------------------------
# Seeding (called by demo_mode.seed.seed)
# --------------------------------------------------------------------------


def _first(resp: Any) -> Optional[Dict[str, Any]]:
    rows = list(getattr(resp, "data", None) or [])
    return rows[0] if rows else None


def _kb_spec() -> Dict[str, Any]:
    from runtime.tools.agents_demo.brand import render
    from runtime.tools.agents_demo.inbound import KB_DIR

    docs = []
    for lang in ("en", "ar"):
        for path in sorted((KB_DIR / lang).glob("*.md")):
            text = render(path.read_text(encoding="utf-8"))
            heading = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")),
                           path.stem)
            docs.append({"title": f"{heading} ({lang.upper()})", "filename": f"{lang}-{path.name}",
                         "content": text})
    return {"name": KB_NAME, "documents": docs,
            "description": "Falak Connect's help articles in English and Arabic: plans, coverage, "
                           "installation, bills, refunds, Wi-Fi and contracts (fictional)."}


def _ensure_category(client) -> Optional[str]:
    found = _first(client.table("agent_types").select("agent_type_id")
                   .eq("slug", CATEGORY["slug"]).limit(1).execute())
    if not found:
        found = _first(client.table("agent_types").insert(dict(CATEGORY)).execute())
    return (found or {}).get("agent_type_id")


def _ensure_agents(client, env: str, model_id: str, voice_model_id: Optional[str],
                   kb_id: Optional[str], category_id: Optional[str]) -> Dict[str, str]:
    """{slug: profile_id}. Code-owned: an existing agent is brought back to
    this configuration on every seed (they are read-only in the portal)."""
    from demo_mode import seed as demo_seed

    ids: Dict[str, str] = {}
    for key, agent in AGENTS.items():
        spec = {"slug": agent.slug, "instructions": instructions(key), "status": "active"}
        kbs = [kb_id] if (kb_id and agent.direction == "inbound") else []
        voice = voice_config(key, voice_model_id) if voice_model_id else None
        found = _first(client.table("agent_profiles").select("profile_id")
                       .eq("agent_kind", "custom").eq("slug", agent.slug).eq("environment", env)
                       .limit(1).execute())
        if found:
            ids[agent.slug] = found["profile_id"]
            demo_seed._converge(client, found["profile_id"], spec, {"agent": model_id})
            patch: Dict[str, Any] = {"display_name": agent.display_name,
                                     "description": agent.description,
                                     "data_source_ids": kbs, "agent_type_id": category_id}
            if voice:
                patch["voice_config"] = voice
            client.table("agent_profiles").update(patch).eq("profile_id", found["profile_id"]).execute()
            continue
        version_id = demo_seed._ensure_instructions(client, agent.slug, agent.display_name,
                                                    spec["instructions"])
        row = {
            "agent_kind": "custom", "slug": agent.slug, "display_name": agent.display_name,
            "description": agent.description, "environment": env,
            "endpoint_path": f"/v1/agents/{agent.slug}/chat", "model_main_id": model_id,
            "prompt_version_ids": {"system": version_id}, "tool_ids": [], "data_source_ids": kbs,
            "mcp_server_ids": [], "downstream_profile_ids": [], "delegatable": False,
            "memory_config": {"enabled": False}, "is_active": True, "status": "active",
            "agent_type_id": category_id,
        }
        if voice:
            row["voice_config"] = voice
        ids[agent.slug] = _first(client.table("agent_profiles").insert(row).execute())["profile_id"]
    return ids


def _ensure_channel(client) -> Optional[str]:
    """The product page's channel: its origins (so the engine's CORS and the
    voice Origin gate admit it) and these four agents only."""
    kinds = [f"custom:{slug}" for slug in sorted(SLUGS)]
    row = {**CHANNEL, "is_active": True, "allowed_origins": external_origins(),
           "allowed_agent_kinds": kinds, "auth_required": False, "rate_limit_rpm": 30, "config": {}}
    found = _first(client.table("agent_channels").select("channel_id")
                   .eq("slug", CHANNEL["slug"]).limit(1).execute())
    if found:
        client.table("agent_channels").update(
            {k: row[k] for k in ("allowed_origins", "allowed_agent_kinds", "is_active")}
        ).eq("channel_id", found["channel_id"]).execute()
        channel_id = found["channel_id"]
    else:
        channel_id = (_first(client.table("agent_channels").insert(row).execute()) or {}).get("channel_id")
    # The demo's own website channel names the agents the portal may call
    # from its own pages; add these four so the portal's Test and Call work.
    from demo_mode import content

    own = _first(client.table("agent_channels").select("channel_id, allowed_agent_kinds")
                 .eq("slug", content.CHANNEL["slug"]).limit(1).execute())
    if own and own.get("allowed_agent_kinds"):
        merged = list(own["allowed_agent_kinds"])
        merged += [k for k in kinds if k not in merged]
        if merged != own["allowed_agent_kinds"]:
            client.table("agent_channels").update({"allowed_agent_kinds": merged}) \
                .eq("channel_id", own["channel_id"]).execute()
    try:
        from core import channels as core_channels

        core_channels.invalidate()
    except Exception:  # noqa: BLE001 (the cache expires by itself)
        pass
    return channel_id


def ensure(client, env: str, models: Dict[str, str], voice_model_id: Optional[str]) -> Dict[str, Any]:
    """Seed the showcase. A no-op unless ``DEMO_SHOWCASE`` is on."""
    if not settings.showcase_enabled():
        return {}
    from demo_mode import seed as demo_seed

    category_id = _ensure_category(client)
    kb_id = demo_seed._ensure_knowledge_base(client, _kb_spec())
    agents = _ensure_agents(client, env, models["agent"], voice_model_id, kb_id, category_id)
    channel_id = _ensure_channel(client)
    return {"agents": agents, "knowledge_id": kb_id, "channel_id": channel_id}


def agent_page_ids() -> Dict[str, str]:
    """{key: profile_id} of the staging agents, for links into the portal.
    Blocking."""
    from core.auth import get_service_client

    rows = (get_service_client().table("agent_profiles").select("slug, profile_id")
            .eq("agent_kind", "custom").eq("environment", "staging").in_("slug", sorted(SLUGS))
            .execute().data or [])
    return {AGENT_BY_SLUG[r["slug"]].key: r["profile_id"] for r in rows if r.get("slug") in AGENT_BY_SLUG}


def today() -> date:
    return date.today()
