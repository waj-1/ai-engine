---
path: <source>/<area>/<slug>     # globally unique, stable; e.g. product-docs/billing/update-a-card
title: <Human title>
type: user_guide                 # user_guide | feature_explanation | navigation_help | business_concept | troubleshooting | faq
language: en
source: <source-tag>             # must map to a knowledge source: scripts/_kb_target.py
---

## Overview
One short paragraph: what the feature is and who uses it. Keep each section above
50 characters and below ~1500 characters; the ingester splits on these `##` headings.

## How it works / Steps
Concrete, user-facing detail. Prefer task-oriented language ("To update a card, …").

## Notes & limitations
Edge cases, permissions, gotchas. Optional.
