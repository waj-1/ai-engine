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

PLANS_KB_NAME = "Northwind plans and pricing"
BILLING_KB_NAME = "Northwind billing and refunds"
IT_KB_NAME = "Northwind IT handbook"

PLANS_GUIDE = """\
# Northwind Internet plans

## The three plans
- Fiber 100: 100 Mbps download, $29 a month. Good for one or two people: browsing, email, one video stream.
- Fiber 500: 500 Mbps download, $45 a month. Good for families and working from home: several video calls and streams at once.
- Fiber 1000: 1000 Mbps download, $65 a month. Good for large households, gaming and big uploads.
Every plan has unlimited data and no contract. You can change plan once a month, and the new price starts on the next bill.

## What is included
A Wi-Fi router is included with every plan at no extra cost. It stays Northwind's property: return it when you cancel.

## Installation cost and time
Installation is free on every plan. A technician does it in one visit of about one hour.
"""

INSTALL_GUIDE = """\
# Installation and add-ons

## Installation cost and how long it takes
Installation costs nothing: it is free. A technician brings the router and connects the fiber box; the visit takes about one hour. Visits are from 9am to 5pm, Monday to Saturday, and the first free slot is usually within 5 working days.

## Add-ons
- Mesh point: $5 a month each, for homes over 150 square metres or with thick walls.
- Static IP address: $4 a month, for people who host a server or a camera system.

## Coverage
Northwind serves the Riverside, Hillcrest, Old Town and Harbour districts. Other districts can join a waiting list on the website.
"""

BILLING_GUIDE = """\
# Bills and payments

## When you are billed
Your bill is issued on the 1st of each month for the month ahead. Payment is taken on the 15th from the card or bank account on file.

## A higher bill than usual
The usual reasons are: a plan change (the first bill after an upgrade includes the difference for the rest of that month), a new add-on such as a mesh point or a static IP address, or a returned payment from the month before.

## Late payment
There is no late fee in the first 7 days. After 7 days a $5 late fee is added, and after 30 days the service is paused until the bill is paid.

## Changing how you pay
Change your card or bank account on the account page, at least 3 days before the 15th.
"""

REFUNDS_GUIDE = """\
# Refunds and credits

## Outages
If your service is down for more than 24 hours in a row, you get one free day for each day it was down. It is added to your next bill automatically; you do not need to ask. Outages shorter than 24 hours, and planned maintenance announced in advance, do not count.

## Cancelling
There is no contract and no cancellation fee. Cancel from your account page; the service runs to the end of the month you paid for, and nothing is refunded for the rest of that month. Return the router within 14 days, or a $60 router fee is charged.

## Charged twice
If a payment was taken twice, open a support ticket. The billing team checks it and returns the second payment within 5 working days.
"""

IT_ACCOUNTS = """\
# Northwind staff IT handbook: accounts and passwords

## Passwords
Passwords must be at least 14 characters. There is no scheduled change: change your password only when you think someone else may know it. Never share it, not even with the IT team.

## Forgotten password
Use "Forgot password" on the staff sign-in page. It sends a code to the phone registered for two-step sign-in. If you have also lost that phone, raise a request with the IT team from a colleague's computer.

## Two-step sign-in
Every staff account uses the Northwind Authenticator app. Approve a sign-in only when you started it yourself. If you get a prompt you did not start, deny it and report it to the IT team.

## New starters
A new starter's account is ready on their first morning. Their manager raises the request at least 5 working days before the start date.
"""

IT_DEVICES = """\
# Northwind staff IT handbook: laptops and the VPN

## Connect to the VPN
1. Open the Northwind Connect app on your laptop.
2. Sign in with your work email.
3. Approve the prompt in the Northwind Authenticator app on your phone.
You need the VPN for the billing system and the network maps. Email and chat work without it.

## A slow or frozen laptop
Restart it first. If it is still slow, check that it has at least 10 GB of free disk space, then raise a request with the IT team.

## Lost or stolen laptop
Report it to the IT team at once, at any hour, on the emergency line 5555. The team locks the laptop remotely. Do not wait until the next working day.

## Software
Install software only from the Northwind Software Centre. For anything that is not there, raise a request and say what you need it for.
"""

IT_SECURITY = """\
# Northwind staff IT handbook: security

## Suspicious email
Do not click links or open attachments. Use the "Report phishing" button in your mail app, then delete the email. If you already clicked or typed your password, change your password and tell the IT team at once.

## Customer data
Never copy customer data to a personal device or a personal email account. Share files only through the staff drive.

## Visitors and screens
Lock your screen when you leave your desk. Visitors use the guest Wi-Fi, never the staff network.

## Raising a request
Use the IT request form on the staff intranet. Urgent problems that stop you working: call the IT desk on 5550, from 8am to 6pm on working days.
"""

#: Every knowledge base the demo seeds: key -> name, description, documents.
KNOWLEDGE_BASES: Dict[str, Dict[str, Any]] = {
    "help": {"name": KB_NAME, "description": "Northwind's customer help pages (fictional).",
             "documents": DOCUMENTS},
    "plans": {"name": PLANS_KB_NAME,
              "description": "Northwind's plans, installation, add-ons and coverage (fictional).",
              "documents": [
                  {"title": "Internet plans", "filename": "plans.md", "content": PLANS_GUIDE},
                  {"title": "Installation and add-ons", "filename": "installation.md",
                   "content": INSTALL_GUIDE}]},
    "billing": {"name": BILLING_KB_NAME,
                "description": "How Northwind bills, late payment, refunds and cancelling (fictional).",
                "documents": [
                    {"title": "Bills and payments", "filename": "bills.md", "content": BILLING_GUIDE},
                    {"title": "Refunds and credits", "filename": "refunds.md",
                     "content": REFUNDS_GUIDE}]},
    "it": {"name": IT_KB_NAME,
           "description": "Northwind's handbook for its own staff: accounts, laptops, security (fictional).",
           "documents": [
               {"title": "Accounts and passwords", "filename": "it-accounts.md", "content": IT_ACCOUNTS},
               {"title": "Laptops and the VPN", "filename": "it-devices.md", "content": IT_DEVICES},
               {"title": "Security", "filename": "it-security.md", "content": IT_SECURITY}]},
}

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
- If the customer asks for a person, or is still stuck or upset after you tried to help, hand the \
chat to the support team with escalate_to_human and tell them what it returned.
- If a tool reports an error, say plainly what went wrong and what you need (for an order: the \
number as NW- and four digits). Never guess the result.

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
- For bills, payment dates, late fees, cancelling and anything else about money, look it up with \
search_knowledge before you answer. Never guess a fee or a date.
- Outage refunds: one free day for each day the service was down, when it was down more than \
24 hours in a row. It is added to the next bill automatically; nobody needs to ask for it.
- A mesh point costs $5 a month.
- You cannot see any customer's account. When a question needs one, say that a person from \
the billing team will follow up, and that a support ticket is the way to ask for that.
Answer in two or three sentences.
"""

SALES_INSTRUCTIONS = f"""\
You are the sales assistant of {COMPANY}. You help people choose an internet plan.

- Use list_plans for the plans and prices. For installation, add-ons, coverage, moving home \
and Wi-Fi, look it up with search_knowledge before you answer.
- Ask one question at a time: how many people, what they do online (streaming, gaming, \
working from home), and how big the home is.
- Recommend one plan and say why in one sentence. Mention the mesh point for homes over 150 \
square metres.
- There is no contract; say so when someone hesitates.
- You cannot sign anyone up in this demo. To order, they would use Northwind's website.
"""

IT_INSTRUCTIONS = f"""\
You are the internal IT helpdesk of {COMPANY}, for its own staff.

- Answer from the IT handbook: look the question up with search_knowledge first, and give \
the handbook's steps and numbers. When the handbook does not cover it, give general steps a \
non-expert can follow and say it is general advice.
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
- Give an agent Northwind's tools (check_outage, get_order_status, create_ticket, \
escalate_to_human, list_plans): \
attach the "Northwind tools" server with profile_attachments kind=mcp. Give it knowledge with \
profile_attachments kind=kb: "{KB_NAME}" (customer help pages), "{PLANS_KB_NAME}", \
"{BILLING_KB_NAME}" or "{IT_KB_NAME}" (for staff); kb_sources action=list shows them all. Add a specialist sub-agent: \
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
- A follow-up is about the agent you were just working on. "Name it Ali", "make it shorter", \
"give it the billing knowledge", "test it" or "delete it" all mean the agent from your last \
answer (or the one on the visitor's page): change THAT agent with update_profile, prompts or \
profile_attachments. Never create another agent for a follow-up. "What you did earlier" below \
lists the agents you made, with their ids.
- "Build", "create", "make" or "add" an agent always means a NEW agent, even when a similar \
one exists: build it, and give it a name that tells it apart from the others. Never answer a \
build request with "one already exists". You may mention the similar agent in one line at the \
end.
- A new agent is a draft, and a draft cannot be chatted with or tested. Right after you write \
its instructions, set it live (update_profile status "active"), and only then use chat or a \
test run.
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
     "tools": True, "knowledge": ["help", "billing"], "sub_agents": [BILLING_SLUG]},
    {"slug": BILLING_SLUG, "display_name": "Billing helper", "category": "support",
     "description": "A specialist the support desk asks about bills, prices and refunds.",
     "instructions": BILLING_INSTRUCTIONS, "status": "specialist", "delegatable": True,
     "knowledge": ["billing", "plans"]},
    {"slug": SALES_SLUG, "display_name": "Sales assistant", "category": "sales",
     "description": "Helps people choose a Northwind plan.",
     "instructions": SALES_INSTRUCTIONS, "status": "active", "tools": True,
     "knowledge": ["plans", "help"]},
    {"slug": IT_SLUG, "display_name": "IT helpdesk", "category": "internal-tools",
     "description": "Answers Northwind staff's everyday IT questions.",
     "instructions": IT_INSTRUCTIONS, "status": "active", "knowledge": ["it"]},
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

#: One test suite per demo agent: slug -> name and cases. The Support desk's
#: is the original five (SUITE_NAME / TEST_CASES above).
SUITES: List[Dict[str, Any]] = [
    {"agent": SUPPORT_SLUG, "name": SUITE_NAME, "cases": TEST_CASES},
    {"agent": SALES_SLUG, "name": "Sales assistant: choosing a plan", "cases": [
        {"id": "plan-prices", "category": "plans",
         "question": "What plans do you have and how much are they?",
         "expected_answer": "Fiber 100 at $29 a month, Fiber 500 at $45 and Fiber 1000 at $65.",
         "expected_tools": ["list_plans"]},
        {"id": "no-contract", "category": "plans",
         "question": "Do I have to sign a contract?",
         "expected_answer": "No. There is no contract and you can cancel any time."},
        {"id": "big-home", "category": "wifi",
         "question": "My house is 200 square metres. Will the Wi-Fi reach every room?",
         "expected_answer": "Suggests adding a mesh point for $5 a month, because the home is "
                            "over 150 square metres."},
        {"id": "installation", "category": "knowledge",
         "question": "How much does installation cost and how long does it take?",
         "expected_answer": "Installation is free and the technician's visit takes about one hour."},
        {"id": "cannot-sign-up", "category": "limits",
         "question": "Great, sign me up for Fiber 500 right now.",
         "expected_answer": "Says it cannot sign anyone up here and that ordering is done on "
                            "Northwind's website."},
    ]},
    {"agent": BILLING_SLUG, "name": "Billing helper: bills and refunds", "cases": [
        {"id": "price-500", "category": "prices",
         "question": "How much is Fiber 500?",
         "expected_answer": "$45 a month, with no contract.",
         "expected_substrings": ["45"]},
        {"id": "refund-three-days", "category": "refunds",
         "question": "My service was down for three days in a row. What do I get back?",
         "expected_answer": "Three free days, added to the next bill automatically, with nothing "
                            "to ask for."},
        {"id": "refund-short-outage", "category": "refunds",
         "question": "It was down for five hours yesterday. Do I get a refund?",
         "expected_answer": "No: a refund applies only when the service is down for more than "
                            "24 hours in a row."},
        {"id": "bill-dates", "category": "knowledge",
         "question": "When is my bill issued and when is the payment taken?",
         "expected_answer": "The bill is issued on the 1st of each month and payment is taken on "
                            "the 15th."},
        {"id": "no-account-access", "category": "limits",
         "question": "Can you check why my last bill was higher than usual?",
         "expected_answer": "Says it cannot see any customer's account, may give the usual "
                            "reasons, and says a person from the billing team will follow up "
                            "through a support ticket."},
    ]},
    {"agent": IT_SLUG, "name": "IT helpdesk: staff questions", "cases": [
        {"id": "vpn", "category": "knowledge",
         "question": "How do I connect to the VPN from home?",
         "expected_answer": "Open the Northwind Connect app, sign in with the work email and "
                            "approve the prompt in the Northwind Authenticator app."},
        {"id": "password-reset", "category": "limits",
         "question": "I forgot my password. Can you reset it for me?",
         "expected_answer": "Says it cannot reset passwords, and points to \"Forgot password\" on "
                            "the staff sign-in page or a request to the IT team."},
        {"id": "phishing", "category": "security",
         "question": "I got an email asking me to log in to fix my mailbox. What should I do?",
         "expected_answer": "Do not click the link; report it (the Report phishing button or the "
                            "IT team) and delete it."},
        {"id": "lost-laptop", "category": "security",
         "question": "I left my work laptop on the train.",
         "expected_answer": "Report it to the IT team at once, at any hour, on the emergency "
                            "line 5555, so they can lock it remotely."},
        {"id": "password-policy", "category": "knowledge",
         "question": "How often do I have to change my password?",
         "expected_answer": "There is no scheduled change: only when someone else may know it. "
                            "Passwords must be at least 14 characters."},
    ]},
]

#: Playbooks (skills) the agents read when a task needs them.
SKILLS: List[Dict[str, Any]] = [
    {"slug": "calm-an-upset-customer", "display_name": "Calm an upset customer",
     "description": "How to answer a customer who is angry about an outage or a bill.",
     "agents": [SUPPORT_SLUG],
     "content": """\
# Calm an upset customer

Use this when a customer is angry, repeats themselves, or writes in capitals.

1. Say sorry once, in one short sentence, and name the problem in their words.
2. Say what you checked and what you found. Use a tool first; never guess.
3. Say the next step and when it happens. Give a time, not "soon".
4. If you cannot fix it, open a ticket and give the ticket number.

Do not explain company policy before step 3. Do not say "calm down". Do not offer money back unless the refund rule applies.
"""},
    {"slug": "compare-two-plans", "display_name": "Compare two plans",
     "description": "How to compare two Northwind plans for someone who cannot decide.",
     "agents": [SALES_SLUG],
     "content": """\
# Compare two plans

Use this when someone is choosing between two plans.

1. Ask the one thing you do not know yet: people at home, what they do online, or the size of the home.
2. Compare only the two plans they named: speed, price a month, and one sentence on who each suits.
3. Recommend one and say why in one sentence.
4. Mention that there is no contract, so they can change plan later.

Keep it to five lines. Do not list the third plan unless they ask.
"""},
    {"slug": "report-a-security-problem", "display_name": "Report a security problem",
     "description": "What to tell a staff member who may have a security problem.",
     "agents": [IT_SLUG],
     "content": """\
# Report a security problem

Use this for a strange email, a lost device, a sign-in prompt nobody started, or customer data in the wrong place.

1. Tell them to stop: do not click, reply, or keep using the device.
2. Tell them who to tell and how fast: the IT team at once; the emergency line 5555 for a lost or stolen laptop.
3. Say what the IT team will do next (lock the device, reset the account).
4. Never ask them to send you a password or a code.
"""},
]

#: Scheduled test runs shown on Testing > Schedules. Nothing outside the demo
#: fires them: the nightly reset's own test round (demo_mode/warmup.py) runs
#: each suite once a day and records it against its schedule.
SCHEDULES: List[Dict[str, Any]] = [
    {"agent": SUPPORT_SLUG, "name": "Support desk: every night", "cadence": "daily"},
    {"agent": SALES_SLUG, "name": "Sales assistant: every night", "cadence": "daily"},
    {"agent": BILLING_SLUG, "name": "Billing helper: every week", "cadence": "weekly"},
    {"agent": IT_SLUG, "name": "IT helpdesk: every week", "cadence": "weekly"},
]

#: Voice checks for the Support desk's calls (Testing > Voice checks). They are
#: shown in the demo; running one is switched off there (it places a real call).
VOICE_CHECKS: List[Dict[str, Any]] = [
    {"slug": "support-desk-greeting", "title": "The agent greets the caller first",
     "steps": [
         {"type": "expect_transcript", "role": "assistant", "pattern": "", "from_start": True,
          "within_sec": 20}],
     "notes": "No caller speech: checks that the greeting plays when the call connects."},
    {"slug": "support-desk-outage", "title": "A spoken outage question gets the outage answer",
     "steps": [
         {"type": "expect_transcript", "role": "assistant", "pattern": "", "from_start": True,
          "within_sec": 20},
         {"type": "say", "text": "Hi, my internet is down. I live in Riverside."},
         {"type": "expect_transcript", "role": "assistant", "pattern": "(?i)maintenance|riverside",
          "within_sec": 40}],
     "notes": "The agent should call check_outage and say there is planned maintenance in Riverside."},
    {"slug": "support-desk-office-hours", "title": "A question answered from the help center",
     "steps": [
         {"type": "expect_transcript", "role": "assistant", "pattern": "", "from_start": True,
          "within_sec": 20},
         {"type": "say", "text": "When can I reach support by phone?"},
         {"type": "expect_transcript", "role": "assistant", "pattern": "(?i)8|eight",
          "within_sec": 40}],
     "notes": "The answer is in the help center: 8am to 8pm, every day."},
]

CHANNEL = {
    "slug": "northwind-website",
    "name": "Northwind website",
    "kind": "embed",
    "description": "The chat on Northwind's (fictional) website. It may talk to the demo's agents only.",
}

#: The channel the demo's own sample conversations arrive on
#: (demo_mode/sample_traffic.py). Its "website" is an address under the
#: reserved `.example` domain that only the demo itself sends as its origin.
SAMPLE_CHANNEL = {
    "slug": "sample-traffic",
    "name": "Sample traffic",
    "kind": "embed",
    "description": "Conversations the demo holds with its own agents around the clock, so the "
                   "reports always have recent activity: scripted customer messages, real agent "
                   "replies. Not visitors.",
}
SAMPLE_ORIGIN = "https://sample-traffic.northwind.example"

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
