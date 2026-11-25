"""Sample traffic: the demo talks to its own agents so the reports are never empty.

A visitor who opens Overview at any hour should find recent conversations on
every report. Without this the demo held five conversations once, after each
start and each nightly reset, so "the last 24 hours" was one burst, the
charts one spike, and "Needs attention" and "Handed to a person" always 0.

What it does
    Holds short, scripted customer conversations (`SCRIPTS`) with the demo's
    agents, spread across the day. Every turn is a real one: posted to the
    agent's own chat address (``/v1/agents/<slug>/chat``) inside this process,
    the way a website does it, answered by the real model with the real tools
    and knowledge, traced and saved like any visitor's. Nothing is written to
    the trace store by hand and no number is made up. Only the customer's
    lines are scripted; what the agent does with them is whatever it does.

How a visitor can tell
    The conversations arrive on a channel of their own, "Sample traffic"
    (`content.SAMPLE_CHANNEL`: Connections > Channels describes it, and
    Conversations can be filtered by it). A visitor's own chats in the portal
    arrive on "Northwind website". Each sample session is also marked
    (``metadata.sample`` = the script's id), which is what the insights sweep
    reads (demo_mode/insights.py).

Kept past the nightly reset
    These conversations hold nothing a visitor wrote, so the reset keeps them
    for ``DEMO_SAMPLE_KEEP_DAYS`` days and the 7-day and 30-day reports have
    data (demo_mode/retention.py has the rules). That needs more than the
    channel, which anyone can claim by sending its origin: every turn also
    carries a mark only this process can send, and a conversation is marked
    only while the agents are exactly as seeded.

When
    A short burst first (``DEMO_SAMPLE_BURST`` conversations) after a new
    install's first start and after each nightly reset, then a steady trickle:
    every ``DEMO_SAMPLE_MIN_GAP_SEC`` to ``DEMO_SAMPLE_MAX_GAP_SEC`` (20 to 40
    minutes) one conversation, sometimes two, during Northwind's support
    hours (08:00 to 20:00, ``DEMO_SAMPLE_UTC_OFFSET`` hours from UTC), and one
    at half that pace outside them, so the hourly charts have a shape.
    A few seconds pass between a conversation's turns.

Budget (it can never starve visitors)
    * ``DEMO_SAMPLE_DAILY_TURNS`` (default 200) turns a UTC day, counted in
      the database (`limits.take`), so several engine processes share it.
    * Every turn is ALSO one of the demo's daily turns (``DEMO_DAILY_TURNS``):
      the chat route takes it, as for a visitor.
    * It stops for the day once ``DEMO_SAMPLE_STOP_AT_PERCENT`` (default 60)
      of the demo's daily turns are used, by anyone: when visitors are busy
      the generator goes quiet first and the rest of the day is theirs.
    ``DEMO_SAMPLE_TRAFFIC=off`` switches it off.

Several processes, restarts
    A tick is claimed in the database before it is held: the day is cut into
    slots of the minimum gap and `demo_usage_take` hands each slot to one
    process, so two engine processes do not double the traffic. The start
    burst is claimed the same way (once a UTC day per database), so a restart
    does not repeat it; the process that ran the nightly reset holds the
    burst after it.

Never blocks the event loop
    It is an asyncio task on the engine's own loop (started by
    `demo_mode.reset.on_boot`). Its chat turns are ordinary awaited requests
    through the app; every database call it makes itself (the claims, the
    budget, the sign-in, the session mark) goes through `asyncio.to_thread`.

With tracing off it runs the same: the conversations are saved and listed
under Conversations from the database.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from demo_mode import content, settings

logger = logging.getLogger(__name__)

TURNS_BUCKET = "sample_turns"
SLOT_BUCKET = "sample_slot"
BURST_BUCKET = "sample_burst"

SUPPORT, SALES, BILLING, IT = (content.SUPPORT_SLUG, content.SALES_SLUG,
                               content.BILLING_SLUG, content.IT_SLUG)

# What a script is there to show. The label says what the customer's lines
# are written to bring about; whether it happens is up to the agent.
PLAIN = "plain"                    # a question answered from tools or knowledge
TICKET = "ticket"                  # ends with a support ticket (create_ticket)
BILLING_HANDOFF = "billing"        # the Support desk asks the Billing helper (ask_billing_helper)
TOOL_FAILURE = "tool_failure"      # a tool refuses the call (a malformed order number): a failed action
NOT_FOUND = "not_found"            # a tool answers "nothing found": a normal result, not a failure
PERSON = "person"                  # the customer wants a person (escalate_to_human): handed to a person
ARABIC = "arabic"                  # the customer writes in Arabic


def _s(script_id: str, agent: str, label: str, *messages: str) -> Dict[str, Any]:
    return {"id": script_id, "agent": agent, "label": label, "messages": tuple(messages)}


#: The pool. Every fact a customer states or asks about is one the demo's
#: tools and knowledge bases hold (demo_mode/northwind.py, demo_mode/content.py):
#: outages in Riverside and Old Town, orders NW-1042 and NW-1077, the plans and
#: prices, the districts served, the billing dates, the staff IT handbook.
SCRIPTS: Tuple[Dict[str, Any], ...] = (
    # --- Support desk ------------------------------------------------------
    _s("support-outage-riverside", SUPPORT, PLAIN,
       "My internet has been down since this morning in Riverside. Is there an outage?",
       "OK, thanks for checking."),
    _s("support-outage-old-town", SUPPORT, PLAIN,
       "Hi, nothing loads at home. I live in Old Town. Is something wrong on your side?"),
    _s("support-no-outage-restart", SUPPORT, PLAIN,
       "My internet stopped working an hour ago. I am in Hillcrest.",
       "How do I restart the router properly?",
       "The light is green again. Thank you!"),
    _s("support-ticket-red-light", SUPPORT, TICKET,
       "My internet is down in Hillcrest.",
       "I restarted the router the way you said and the light is still red. "
       "Can you open a ticket for a technician?"),
    _s("support-ticket-evening-drops", SUPPORT, TICKET,
       "My connection in Harbour drops every evening around nine.",
       "I have restarted the router twice already. Please open a ticket; my email is sam@example.com."),
    _s("support-order-router", SUPPORT, PLAIN,
       "Where is my order NW-1042?",
       "Great. How do I set the router up when it arrives?"),
    _s("support-order-upgrade", SUPPORT, PLAIN,
       "Did my plan upgrade go through? The order number is NW-1077."),
    _s("support-order-not-found", SUPPORT, NOT_FOUND,
       "Can you check order NW-2001 for me?",
       "Hm, I may have mistyped it. I will look for the confirmation email. Thanks."),
    _s("support-order-bad-number", SUPPORT, TOOL_FAILURE,
       "Where is my router? Please look up order NW-10423.",
       "Sorry, one digit too many. It is NW-1042."),
    _s("support-order-wrong-format", SUPPORT, TOOL_FAILURE,
       "Can you look up my order? The number on the email is 1042-B.",
       "I found it: NW-1042."),
    _s("support-bill-higher", SUPPORT, BILLING_HANDOFF,
       "Why is my bill higher this month? I moved up to Fiber 500 two weeks ago."),
    _s("support-late-fee", SUPPORT, BILLING_HANDOFF,
       "I missed the payment on the 15th. Is there a late fee?",
       "OK, I will pay today. Thanks."),
    _s("support-outage-refund", SUPPORT, BILLING_HANDOFF,
       "My internet was down for two whole days last week. Do I get money back?"),
    _s("support-charged-twice", SUPPORT, BILLING_HANDOFF,
       "I was charged twice this month. What happens now?",
       "Yes, please open a ticket for the billing team."),
    _s("support-wants-person", SUPPORT, PERSON,
       "This is the third time this week my internet drops in Hillcrest. I want to talk to a person."),
    _s("support-person-after-restart", SUPPORT, PERSON,
       "My internet keeps cutting out in Harbour and I have already restarted the router three times.",
       "I do not want another checklist. Please hand me over to someone on your team."),
    _s("support-person-moving", SUPPORT, PERSON,
       "I am moving house next month and I would rather sort it out with a real person. "
       "Can someone from your team take over this chat?"),
    _s("support-wifi-upstairs", SUPPORT, PLAIN,
       "The Wi-Fi is weak upstairs. Any tips?",
       "The house is about 180 square metres."),
    _s("support-phone-hours", SUPPORT, PLAIN,
       "When can I reach support by phone?"),
    _s("support-moving-home", SUPPORT, PLAIN,
       "I am moving to a flat in Old Town next month. What do I need to do, and does it cost anything?"),
    _s("support-arabic-outage", SUPPORT, ARABIC,
       "السلام عليكم، النت مقطوع عندي من الصبح في منطقة Riverside. فيه عطل؟",
       "تمام، شكراً لك."),
    # --- Sales assistant -----------------------------------------------------
    _s("sales-family-of-four", SALES, PLAIN,
       "Which plan do you recommend for a family of four that streams a lot?",
       "Two of us also work from home, and the house is about 200 square metres.",
       "Is there a contract?"),
    _s("sales-plans-and-prices", SALES, PLAIN,
       "What plans do you have and how much are they?"),
    _s("sales-gamer", SALES, PLAIN,
       "I live alone and play online games most evenings. Which plan should I take?",
       "I also want to host a small game server. Do I need a static IP, and what does it cost?"),
    _s("sales-installation", SALES, PLAIN,
       "How much does installation cost and how long does it take?",
       "Do you cover the Harbour district?"),
    _s("sales-sign-me-up", SALES, PLAIN,
       "I will take Fiber 500. Can you sign me up right now?"),
    _s("sales-not-covered", SALES, PLAIN,
       "Do you serve the Lakeside district?"),
    _s("sales-two-people", SALES, PLAIN,
       "It is just two of us: email, browsing and one film in the evening. What is the cheapest plan that works?",
       "Can we move up later if it is not enough?"),
    _s("sales-arabic-price", SALES, ARABIC,
       "مرحبا، كم سعر باقة Fiber 500 في الشهر؟ وهل فيه عقد؟"),
    # --- Billing helper (asked directly) --------------------------------------
    _s("billing-dates-and-late", BILLING, PLAIN,
       "When is my bill issued, and what happens if I pay late?"),
    _s("billing-cancel-mid-month", BILLING, PLAIN,
       "If I cancel in the middle of the month, do I get money back? And what about the router?"),
    _s("billing-short-outage", BILLING, PLAIN,
       "It was down for five hours yesterday. Do I get a refund?"),
    _s("billing-needs-account", BILLING, PLAIN,
       "Can you check why my last bill was higher than usual?"),
    _s("billing-change-card", BILLING, PLAIN,
       "I have a new bank card. How do I change it, and by when?"),
    # --- IT helpdesk (Northwind's own staff) -----------------------------------
    _s("it-forgot-password", IT, PLAIN,
       "I forgot my password and I am working from home. What do I do?",
       "I have also lost the phone I use for the sign-in code."),
    _s("it-vpn", IT, PLAIN,
       "How do I connect to the VPN from home?"),
    _s("it-phishing-clicked", IT, PLAIN,
       "I got an email asking me to log in to fix my mailbox, and I already clicked the link."),
    _s("it-lost-laptop", IT, PLAIN,
       "I left my work laptop on the train."),
    _s("it-slow-laptop", IT, PLAIN,
       "My laptop is really slow today.",
       "It has about 4 GB of free disk space."),
    _s("it-install-software", IT, PLAIN,
       "Can I install a PDF editor I found online?"),
    _s("it-new-starter", IT, PLAIN,
       "Someone joins my team in two weeks. When do I need to ask for their account?"),
)

#: The burst after a start or a reset opens with these, so the first minutes
#: already show a ticket, a billing hand-over, a failed action, a hand-over to
#: a person and every agent. The rest of the burst is picked at random.
BURST_FIRST = ("support-outage-riverside", "sales-plans-and-prices", "support-wants-person",
               "it-forgot-password", "support-order-bad-number", "billing-dates-and-late",
               "support-ticket-red-light", "support-outage-refund")

#: Shown on Overview in demo mode (admin/screens/overview.py).
OVERVIEW_NOTE = ("Most of the activity on this page is the demo's own sample traffic: scripted customer "
                 "messages answered live by the real agents, on the channel \"Sample traffic\". "
                 "Your own chats count too.")

_SUPPORT_HOURS = (8, 20)   # Northwind's support hours (the help center's "8am to 8pm")
_recent: Deque[str] = deque(maxlen=12)
_burst_requested = threading.Event()
_OFF = ("off", "0", "false", "no")


# --------------------------------------------------------------------------
# Settings (docs/deploy/demo.md lists them)
# --------------------------------------------------------------------------


def enabled() -> bool:
    """``DEMO_SAMPLE_TRAFFIC``: on by default in demo mode."""
    return settings.enabled() and (os.getenv("DEMO_SAMPLE_TRAFFIC") or "on").strip().lower() not in _OFF


def daily_turns() -> int:
    """``DEMO_SAMPLE_DAILY_TURNS``: turns a UTC day the generator may hold."""
    return settings._int("DEMO_SAMPLE_DAILY_TURNS", 200)


def stop_at_percent() -> int:
    """``DEMO_SAMPLE_STOP_AT_PERCENT``: the generator stops for the day once
    this share of ``DEMO_DAILY_TURNS`` is used (by visitors and itself)."""
    return min(100, settings._int("DEMO_SAMPLE_STOP_AT_PERCENT", 60))


def min_gap_sec() -> int:
    """``DEMO_SAMPLE_MIN_GAP_SEC``: the shortest wait between two ticks."""
    return settings._int("DEMO_SAMPLE_MIN_GAP_SEC", 1200, 5)


def max_gap_sec() -> int:
    """``DEMO_SAMPLE_MAX_GAP_SEC``: the longest wait between two ticks."""
    return max(min_gap_sec(), settings._int("DEMO_SAMPLE_MAX_GAP_SEC", 2400, 5))


def burst_size() -> int:
    """``DEMO_SAMPLE_BURST``: conversations held at once after a new
    install's first start and after each reset."""
    return min(len(SCRIPTS), settings._int("DEMO_SAMPLE_BURST", 8))


def turn_delay_sec() -> int:
    """``DEMO_SAMPLE_TURN_DELAY_SEC``: the longest pause between two turns of
    one conversation (the customer "typing"); the shortest is a quarter of it."""
    return settings._int("DEMO_SAMPLE_TURN_DELAY_SEC", 20)


def utc_offset_hours() -> int:
    """``DEMO_SAMPLE_UTC_OFFSET``: Northwind's clock, in hours from UTC
    (its support hours decide when the traffic is denser)."""
    raw = (os.getenv("DEMO_SAMPLE_UTC_OFFSET") or "").strip()
    try:
        return max(-12, min(14, int(raw))) if raw else 0
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# The schedule (pure: tests drive it with a seeded `random.Random`)
# --------------------------------------------------------------------------


def support_hours(now: datetime) -> bool:
    local = now.astimezone(timezone.utc) + timedelta(hours=utc_offset_hours())
    return _SUPPORT_HOURS[0] <= local.hour < _SUPPORT_HOURS[1]


def next_gap(now: datetime, rng: random.Random) -> float:
    """Seconds until the next tick: the setting's range in support hours,
    twice that outside them."""
    gap = rng.uniform(min_gap_sec(), max_gap_sec())
    return gap if support_hours(now) else gap * 2


def conversations_for_tick(now: datetime, rng: random.Random) -> int:
    """One conversation, or two on about four ticks in ten in support hours."""
    return 2 if support_hours(now) and rng.random() < 0.4 else 1


def pick(count: int, rng: random.Random, recent: Optional[Deque[str]] = None,
         first: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """`count` scripts at random, none that was held recently (the last 12)
    and none twice. `first` names the scripts to open with, in order."""
    recent = _recent if recent is None else recent
    by_id = {s["id"]: s for s in SCRIPTS}
    chosen: List[Dict[str, Any]] = [by_id[i] for i in first if i in by_id][:count]
    taken = {s["id"] for s in chosen}
    pool = [s for s in SCRIPTS if s["id"] not in taken and s["id"] not in recent]
    if len(pool) < count - len(chosen):   # a small pool: repeats beat silence
        pool = [s for s in SCRIPTS if s["id"] not in taken]
    rng.shuffle(pool)
    chosen += pool[:max(0, count - len(chosen))]
    for s in chosen:
        recent.append(s["id"])
    return chosen


def slot_of(now: datetime) -> int:
    """The 1-based slot of the UTC day `now` falls in (slots of the minimum gap)."""
    now = now.astimezone(timezone.utc)
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    return seconds // max(1, min_gap_sec()) + 1


# --------------------------------------------------------------------------
# Claims and budget (blocking: the loop calls them through to_thread)
# --------------------------------------------------------------------------


def claim_slot(now: Optional[datetime] = None) -> bool:
    """Take this slot of the day for this process. False when another process
    (or this one, earlier) already has it or a later one.

    The day's row counts up to the newest slot claimed: taking slot `s` asks
    for exactly the units missing up to `s` with `s` as the limit, which the
    database grants to one caller only."""
    from demo_mode import limits

    slot = slot_of(now or datetime.now(timezone.utc))
    used = limits.used(SLOT_BUCKET)
    if used >= slot:
        return False
    return limits.take(SLOT_BUCKET, slot, slot - used)


def claim_boot_burst() -> bool:
    """The start burst, once a UTC day per database: a restart does not hold
    it again, a new install (an empty database) does."""
    from demo_mode import limits

    return limits.take(BURST_BUCKET, 1)


def may_spend() -> bool:
    """One turn of the generator's budget, or False when it must stay quiet:
    its own daily turns are used, or the whole demo has used so much of the
    day's cap that the rest is kept for visitors."""
    from demo_mode import limits

    cap = settings.daily_turns()
    if limits.used(limits.TURNS_BUCKET) >= cap * stop_at_percent() / 100.0:
        return False
    return limits.take(TURNS_BUCKET, daily_turns())


def mark_session(session_id: str, script_id: str) -> None:
    """Write ``metadata.sample`` on the conversation's session row. Never raises."""
    from demo_mode import insights, seed

    try:
        client = seed._client()
        row = seed._first(client.table("ai_chat_sessions").select("metadata")
                          .eq("id", session_id).limit(1).execute())
        if row is None:
            return
        meta = dict(row.get("metadata") or {})
        meta[insights.SAMPLE_KEY] = script_id
        client.table("ai_chat_sessions").update({"metadata": meta}).eq("id", session_id).execute()
    except Exception as exc:  # noqa: BLE001 — a mark never stops the traffic
        logger.warning("demo sample traffic: session not marked (%s)", type(exc).__name__)


def request_burst() -> None:
    """Ask this process's generator for a burst now (after a reset). Safe
    from any thread."""
    _burst_requested.set()


# --------------------------------------------------------------------------
# Holding conversations (async, on the engine's loop)
# --------------------------------------------------------------------------


def chat_headers(token: str, mark: Optional[str] = None) -> Dict[str, str]:
    """What a sample chat sends: the demo admin's token and the sample
    channel's own origin, the way a website's page names itself. `mark` (a
    script id) adds this process's own mark, which is what lets the nightly
    reset tell these turns from a visitor's (demo_mode/retention.py): anyone
    can send the origin, only this process can send the mark."""
    from demo_mode import retention

    return {"Authorization": f"Bearer {token}", "Origin": content.SAMPLE_ORIGIN,
            "Accept": "text/event-stream, application/json;q=0.9, */*;q=0.5",
            **(retention.mark_headers(mark) if mark else {})}


async def _post(slug: str, body: Dict[str, Any], headers: Dict[str, str], env: str) -> Tuple[int, str]:
    from admin_mcp.tools_dispatch import _post_chat

    status, _ctype, text, _target = await _post_chat(f"/v1/agents/{slug}/chat", body, headers, env, 120)
    return status, text


async def hold_conversation(script: Dict[str, Any], token: str, env: str, *,
                            rng: random.Random, pause: float) -> int:
    """Hold one scripted conversation. Returns the turns answered (0 when the
    budget said no before the first)."""
    from admin_mcp.tools_dispatch import parse_sse
    from demo_mode import limits, retention

    session_id = str(uuid.uuid4())
    # The reset keeps this conversation only if the agents are exactly as
    # seeded for the whole of it (a visitor may have edited them): checked
    # now, with freshly loaded profiles, and again after the last turn.
    pristine = await asyncio.to_thread(retention.pristine_now, fresh_profiles=True)
    headers = chat_headers(token, script["id"] if pristine else None)
    answered = 0
    for index, message in enumerate(script["messages"]):
        if index and pause > 0:
            await asyncio.sleep(rng.uniform(pause / 4, pause))
        if not await asyncio.to_thread(may_spend):
            break
        try:
            status, text = await _post(script["agent"], {"message": message, "session_id": session_id},
                                       headers, env)
        except Exception as exc:  # noqa: BLE001 — one turn never stops the generator
            logger.warning("demo sample traffic: %s failed (%s)", script["id"], type(exc).__name__)
            break
        if status >= 400:
            logger.warning("demo sample traffic: %s answered %s", script["id"], status)
            break
        parsed = parse_sse(text)
        if (parsed.get("reply") or "").strip() == limits.CAP_MESSAGE:
            break   # the demo's daily cap is used: no model was called
        session_id = parsed.get("session_id") or session_id
        answered += 1
        if answered == 1:
            await asyncio.to_thread(mark_session, session_id, script["id"])
    if answered and pristine and await asyncio.to_thread(retention.pristine_now) == pristine:
        await asyncio.to_thread(retention.stamp_session, session_id, pristine)
    return answered


async def hold(scripts: Sequence[Dict[str, Any]], *, rng: random.Random, pause: float) -> int:
    """Hold these conversations one after another. Returns the turns answered."""
    from demo_mode import account, seed

    if not scripts:
        return 0
    try:
        token = await asyncio.to_thread(account.sign_in)
    except Exception as exc:  # noqa: BLE001 — try again at the next tick
        logger.warning("demo sample traffic: sign-in failed (%s)", type(exc).__name__)
        return 0
    env = seed._env()
    turns = 0
    for script in scripts:
        answered = await hold_conversation(script, token, env, rng=rng, pause=pause)
        if not answered:
            break   # no budget (or the agent is not there): stop this round
        turns += answered
        if pause > 0:
            await asyncio.sleep(rng.uniform(pause / 4, pause))
    logger.info("demo sample traffic: %d conversation(s), %d turn(s): %s",
                len(scripts), turns, ", ".join(s["id"] for s in scripts))
    return turns


async def burst(rng: random.Random) -> int:
    """The opening round: the labelled cases first, quickly."""
    return await hold(pick(burst_size(), rng, first=BURST_FIRST), rng=rng,
                      pause=min(4.0, float(turn_delay_sec())))


async def _wait(seconds: float) -> bool:
    """Sleep up to `seconds`. True as soon as a burst is asked for."""
    left = seconds
    while left > 0:
        if _burst_requested.is_set():
            return True
        step = min(5.0, left)
        await asyncio.sleep(step)
        left -= step
    return _burst_requested.is_set()


async def run_loop(rng: Optional[random.Random] = None) -> None:
    """The generator. Returns at once when it is switched off; otherwise runs
    until cancelled. No failure inside it ends it."""
    if not enabled():
        return
    rng = rng or random.Random()
    logger.info("demo sample traffic: on (%d turns a day at most, a tick every %d to %d s)",
                daily_turns(), min_gap_sec(), max_gap_sec())
    # Let the start finish (the agents and the channel are seeded before this
    # task is made; the first requests of a new process are slow).
    await asyncio.sleep(min(20.0, float(min_gap_sec())))
    try:
        if burst_size() and await asyncio.to_thread(claim_boot_burst):
            await burst(rng)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("demo sample traffic: start burst failed (%s: %s)", type(exc).__name__, exc)
    while True:
        try:
            asked = await _wait(next_gap(datetime.now(timezone.utc), rng))
            if asked:
                _burst_requested.clear()
                await burst(rng)
                continue
            if not await asyncio.to_thread(claim_slot):
                continue   # another process holds this slot's conversation
            now = datetime.now(timezone.utc)
            await hold(pick(conversations_for_tick(now, rng), rng), rng=rng,
                       pause=float(turn_delay_sec()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the next tick tries again
            logger.warning("demo sample traffic: tick failed (%s: %s)", type(exc).__name__, exc)
            await asyncio.sleep(30)
