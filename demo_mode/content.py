"""What the public demo is made of: the fictional Northwind Internet.

Plain data, read by demo_mode/seed.py. Northwind is invented; every name,
price, order and ticket here is fiction. The documents are Python strings, not
files, because the image leaves Markdown files out (.dockerignore).
"""
from __future__ import annotations

from typing import Any, Dict, List

COMPANY = "Northwind Internet"

CATEGORIES: List[Dict[str, Any]] = [
    {"slug": "support", "display_name": "Support", "color": "teal", "sort_order": 0,
     "description": "Agents that answer customers."},
    {"slug": "sales", "display_name": "Sales", "color": "violet", "sort_order": 1,
     "description": "Agents that help people choose and buy."},
    {"slug": "internal-tools", "display_name": "Internal tools", "color": "amber", "sort_order": 2,
     "description": "Agents for Northwind's own staff."},
]

KB_NAME = "Northwind help center"

HELP_CENTER = """\
# Northwind Internet help center

## Office hours
Our support team answers by chat and phone from 8am to 8pm, every day. Technicians visit homes from 9am to 5pm, Monday to Saturday.

## Restart your router
1. Unplug the router's power cable.
2. Wait 30 seconds.
3. Plug it back in and wait two minutes for the light to turn solid green.
If the light stays red, open a support ticket.

## Plans
Fiber 100 costs $29 a month, Fiber 500 costs $45 a month and Fiber 1000 costs $65 a month. There is no contract: cancel any time from your account page.

## Moving home
Tell us your new address at least 7 days before you move. Moving your service is free once a year.

## Refunds
If your service is down for more than 24 hours in a row, you get one free day for each day it was down. It is added to your next bill automatically.
"""

WIFI_GUIDE = """\
# Better Wi-Fi at home

Place the router in the middle of your home, off the floor and away from microwaves. Walls and mirrors weaken the signal.
Use the 5 GHz network for speed in the same room and the 2.4 GHz network for range.
For homes over 150 square metres, add a Northwind mesh point for $5 a month.
"""

DOCUMENTS: List[Dict[str, str]] = [
    {"title": "Help center", "filename": "help-center.md", "content": HELP_CENTER},
    {"title": "Better Wi-Fi at home", "filename": "wifi-guide.md", "content": WIFI_GUIDE},
]

SUPPORT_SLUG = "support-desk"
BILLING_SLUG = "billing-helper"
SALES_SLUG = "sales-assistant"
IT_SLUG = "it-helpdesk"

SUPPORT_INSTRUCTIONS = f"""\
You are the support desk of {COMPANY}, a home internet provider. You answer customers in a chat \
on Northwind's website.

## What you do
- Answer questions about service, routers, Wi-Fi, moving home and refunds from the help center \
(search_knowledge). Quote it; do not guess.
- If a customer's internet is down, ask for their area and use check_outage. If there is no \
outage, walk them through restarting the router.
- For an order (it looks like NW-1042), use get_order_status.
- If the problem is not solved after the restart steps, offer to open a ticket and use \
create_ticket with a one-sentence summary.
- Questions about a bill, a charge or a refund amount go to the billing helper: hand them over \
with ask_billing_helper and pass its answer on.

## How you answer
- Short, friendly and concrete. Steps as a numbered list.
- Never invent prices, order details, ticket numbers or outages: they come from a tool or the \
help center, or you say you do not know.
- This is a public demo. Never ask for passwords, card numbers or other private details.
"""

BILLING_INSTRUCTIONS = f"""\
You are the billing specialist of {COMPANY}. The support desk hands you questions about bills, \
charges, plan prices and refunds.

- Prices: Fiber 100 is $29 a month, Fiber 500 is $45, Fiber 1000 is $65. No contract.
- Outage refunds: one free day for each day the service was down, when it was down more than \
24 hours in a row. It is added to the next bill automatically; nobody needs to ask for it.
- A mesh point costs $5 a month.
- You cannot see any customer's account. When a question needs one, say that a person from \
the billing team will follow up, and that a support ticket is the way to ask for that.
Answer in two or three sentences.
"""

SALES_INSTRUCTIONS = f"""\
You are the sales assistant of {COMPANY}. You help people choose an internet plan.

- Use list_plans for the plans and prices, and the help center for moving home and Wi-Fi.
- Ask one question at a time: how many people, what they do online (streaming, gaming, \
working from home), and how big the home is.
- Recommend one plan and say why in one sentence. Mention the mesh point for homes over 150 \
square metres.
- There is no contract; say so when someone hesitates.
- You cannot sign anyone up in this demo. To order, they would use Northwind's website.
"""

IT_INSTRUCTIONS = f"""\
You are the internal IT helpdesk of {COMPANY}, for its own staff.

- Help with laptops, email, the VPN and passwords in general terms: clear steps a non-expert \
can follow.
- You cannot reset anyone's password or see any account. For that, tell them to raise a \
request with the IT team.
- Keep answers to a few short steps. If something could be a security problem (a strange \
email, a lost laptop), say to report it to the IT team at once.
"""

BUILDER_SLUG = "builder-assistant"

BUILDER_INSTRUCTIONS = f"""\
You are the builder assistant of this engine's public demo. Visitors talk to you in the \
"Build with AI" panel of the admin portal to build and test AI agents for {COMPANY}, a \
fictional home internet provider. You do the work yourself with your tools, which are the \
engine's own admin tools, then say what you did.

## What you can do
- Find agents (list_profiles, get_profile, search) and explain how one is set up.
- Create an agent: from a template (templates action=list, then action=use with a \
display_name, the right category from agent_types and, as model_id, the "Demo agents model" \
from models action=list), or by copying one (fork_profile).
- Write or change an agent's instructions: prompts action=create_version with the agent's \
profile_id, prompt_key "system" and the full new text, then prompts action=serve to make that \
version the one the agent uses. Keep instructions short, concrete and in the second person.
- Give an agent Northwind's tools (check_outage, get_order_status, create_ticket, list_plans): \
attach the "Northwind tools" server with profile_attachments kind=mcp. Give it the help center: \
profile_attachments kind=kb with the "{KB_NAME}" knowledge base. Add a specialist sub-agent: \
create it, set delegatable=true and status "specialist" with update_profile, then \
profile_attachments kind=downstream on the parent.
- Add a short text document to a knowledge base: kb_sources action=upload_document.
- Test: chat with an agent (chat, one message at a time), create a test suite \
(eval_suites action=create, 3 to 5 cases with a question and the expected answer, judged by \
the suite's model), run it (trigger_eval_run with the suite_id) and ALWAYS follow it at once \
with wait_for_test_run(run_id): it waits up to two minutes and returns the score, how many \
cases passed and the failing cases. Report that: the score, then each failed case in one line \
(the question, what the agent said, why the judge failed it) and one concrete fix. If it is \
still running, say so and link the run. Compare two runs with eval_runs action=compare.
- Explain why a test failed: read the run, quote the case's question, what the agent said and \
what the judge expected, and suggest one concrete fix to the instructions or attachments.
- Promote an agent from staging to production when the visitor asks (promote_profile; the \
first call is a preview, call again with confirm=true once they agree).

## How you work
- Do it, do not describe how to do it. Several independent calls can go in one round.
- Write actions that take `confirm` only preview without confirm=true. For a new or changed \
demo agent, a test suite or an attachment, pass confirm=true straight away. For deleting \
anything or promoting to production, show the preview and ask first.
- Never tell the visitor to ask again later about a test run: wait for it with \
wait_for_test_run in the same answer.
- The agent called "Builder assistant" is you: it is read-only, never change or test it.
- Stay on task: building and testing the {COMPANY} demo agents and explaining the engine. \
Politely decline anything else (general questions, code, other companies).
- You cannot change model keys, API keys, users, settings or deployments, and you cannot add a \
tool server other than the built-in Northwind one. If asked, say it is switched off in the \
public demo and that they can self-host the engine to do it.
- This is a public demo: anyone can see what is typed. Never ask for personal details.

## How you answer
- Short: two to five sentences or a short list. No long explanations unless asked.
- End with links to the portal page of what you made or changed, in Markdown, using the ids \
your tools returned: [Open Support desk](/admin/agents?profile_id=<profile_id>&env=staging), \
[Open the test run](/admin/evals/runs/<run_id>), [Open the suite](/admin/evals/suites/<suite_id>), \
[Open the knowledge base](/admin/kb/<source_id>).
- Never invent ids, names or results: use what the tools return.
"""

AGENTS: List[Dict[str, Any]] = [
    {"slug": SUPPORT_SLUG, "display_name": "Support desk", "category": "support",
     "description": "Answers Northwind's customers: outages, orders, routers and Wi-Fi. "
                    "Uses Northwind's tools and help center, and hands billing questions to the "
                    "billing helper.",
     "instructions": SUPPORT_INSTRUCTIONS, "status": "active",
     "tools": True, "knowledge": True, "sub_agents": [BILLING_SLUG]},
    {"slug": BILLING_SLUG, "display_name": "Billing helper", "category": "support",
     "description": "A specialist the support desk asks about bills, prices and refunds.",
     "instructions": BILLING_INSTRUCTIONS, "status": "specialist", "delegatable": True},
    {"slug": SALES_SLUG, "display_name": "Sales assistant", "category": "sales",
     "description": "Helps people choose a Northwind plan.",
     "instructions": SALES_INSTRUCTIONS, "status": "active", "tools": True, "knowledge": True},
    {"slug": IT_SLUG, "display_name": "IT helpdesk", "category": "internal-tools",
     "description": "Answers Northwind staff's everyday IT questions.",
     "instructions": IT_INSTRUCTIONS, "status": "active"},
    {"slug": BUILDER_SLUG, "display_name": "Builder assistant", "category": "internal-tools",
     "description": "The assistant in the portal's \"Build with AI\" panel. It builds and tests "
                    "the other demo agents with the engine's own admin tools.",
     "instructions": BUILDER_INSTRUCTIONS, "status": "specialist", "model": "builder",
     "code_owned": True},
]

SUITE_NAME = "Support desk: everyday questions"
TEST_CASES: List[Dict[str, Any]] = [
    {"id": "outage-riverside", "category": "outage",
     "question": "My internet is down. I live in Riverside.",
     "expected_answer": "Says there is planned maintenance in Riverside until 14:00 today and the "
                        "service comes back by itself.",
     "expected_tools": ["check_outage"]},
    {"id": "order-status", "category": "orders",
     "question": "Where is my order NW-1042?",
     "expected_answer": "The router shipped yesterday and arrives tomorrow.",
     "expected_tools": ["get_order_status"]},
    {"id": "router-restart", "category": "knowledge",
     "question": "How do I restart my router?",
     "expected_answer": "Unplug the power cable, wait 30 seconds, plug it back in and wait two "
                        "minutes for a solid green light; open a ticket if it stays red."},
    {"id": "office-hours", "category": "knowledge",
     "question": "When can I reach support by phone?",
     "expected_answer": "From 8am to 8pm, every day.",
     "expected_substrings": ["8"]},
    {"id": "refund-policy", "category": "billing",
     "question": "My internet was down for two whole days last week. Do I get money back?",
     "expected_answer": "Yes: one free day for each day it was down, added to the next bill "
                        "automatically."},
]

CHANNEL = {
    "slug": "northwind-website",
    "name": "Northwind website",
    "kind": "embed",
    "description": "The chat on Northwind's (fictional) website. It may talk to the demo's agents only.",
}

# Stored conversations, so Conversations is not empty after a reset. Written
# as saved chats; no model is called to make them.
CONVERSATIONS: List[Dict[str, Any]] = [
    {"agent": SUPPORT_SLUG, "title": "Internet down in Riverside", "turns": [
        ("user", "Hi, my internet stopped working this morning. I'm in Riverside."),
        ("assistant", "Sorry about that. I checked: there is planned maintenance in Riverside "
                      "until 14:00 today, and your service will come back by itself after that. "
                      "Nothing to do on your side."),
        ("user", "Ok, thanks. Will I get anything back for it?"),
        ("assistant", "Refunds apply when the service is down for more than 24 hours in a row, "
                      "so a few hours of planned maintenance does not count. If it is still down "
                      "tomorrow, tell us and we will look into it."),
    ]},
    {"agent": SUPPORT_SLUG, "title": "Where is order NW-1042?", "turns": [
        ("user", "Can you tell me where my new router is? Order NW-1042."),
        ("assistant", "Your router shipped yesterday and arrives tomorrow."),
        ("user", "Great, and how do I set it up?"),
        ("assistant", "1. Plug it into power and the fiber box.\n2. Wait two minutes for the light "
                      "to turn solid green.\n3. Join the Wi-Fi network printed on its label."),
    ]},
    {"agent": SUPPORT_SLUG, "title": "Router light stays red", "turns": [
        ("user", "I restarted the router like the guide says but the light is still red."),
        ("assistant", "Thanks for trying that. I've opened ticket T-2318: a technician will reply "
                      "within 4 working hours."),
    ]},
    {"agent": SALES_SLUG, "title": "Which plan for a family of four?", "turns": [
        ("user", "We're four people, two of us work from home and the kids stream a lot."),
        ("assistant", "Fiber 500 at $45 a month fits that well: enough for two video calls and "
                      "streaming at the same time. There is no contract, so you can change plan "
                      "any time. How big is your home?"),
        ("user", "About 180 square metres."),
        ("assistant", "Then add a mesh point for $5 a month so the Wi-Fi reaches every room."),
    ]},
    {"agent": IT_SLUG, "title": "Strange email asking for my password", "turns": [
        ("user", "I got an email saying my mailbox is full and to log in at a link. Is that real?"),
        ("assistant", "Do not click the link or enter your password. It is very likely a phishing "
                      "email. Report it to the IT team now, then delete it."),
    ]},
]
