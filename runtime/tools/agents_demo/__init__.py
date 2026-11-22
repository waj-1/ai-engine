"""Generic AI-agents demo portal — fixture-backed tools for four agents.

A SIMULATION. Every customer, account, amount, outage and reference here is
fictional test data about the made-up provider named in ``brand.py``; nothing
is looked up, sent, charged, booked or logged anywhere real.

Layout:
  brand.py       the ONE place the fictional company is named
  data.py        fixture loader + the agent registry (keys, kinds, cases)
  common.py      shared call mechanics: digit folding, the send yes-gate,
                 case references, money / month rendering
  support.py     inbound support tools (verify, outage, diagnostics, bill,
                 refund, technician, service changes, ticket, handoff)
  inquiries.py   inbound business-inquiry tools (catalog, coverage, compare,
                 lead, callback)
  collection.py  outbound collection: brain tools, the pre-dial case file and
                 the direct voice tools
  sales.py       outbound sales: same three parts
  kb.py          knowledge-base retrieval (shared by all four agents)
"""
