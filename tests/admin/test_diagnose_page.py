"""`/admin/lmnr/diagnose` renders, and probes BOTH ends of the pipeline.

The page answers "why don't I see any traces?", and after the move to a
ClickHouse-only read path that question has two very different answers:

* **write side** — the SDK never exported the spans (bootstrap / API key / OTLP),
* **read side** — the spans are there but the portal can't reach ClickHouse.

Conflating them sends the operator to the wrong half, so the page reports each
separately. It also lost its `project_id` row: a Laminar "project" is a web-UI
concept with no ClickHouse analogue, and resolving one required the frontend's
NextAuth API — which the deployed build 404s.

These are render tests on purpose. The template gained a whole card and lost two
rows; a Jinja mistake there would otherwise only surface in production.
"""
from __future__ import annotations

from unittest.mock import patch


def _raising_query(exc: Exception):
    def _q(*_a, **_k):
        raise exc
    return _q


def test_diagnose_reports_clickhouse_reachable(client):
    with patch("core.lmnr_clickhouse.query", lambda *a, **k: [{"spans": "1234"}]):
        r = client.get("/admin/lmnr/diagnose")

    assert r.status_code == 200, r.text[:300]
    assert "ClickHouse reachable" in r.text
    assert "1234 spans / 24h" in r.text
    # The retired Laminar-frontend surface must not creep back.
    assert "Project id" not in r.text
    assert "Sign in to Laminar UI" not in r.text


def test_diagnose_explains_an_unreachable_clickhouse(client):
    with patch("core.lmnr_clickhouse.query",
               _raising_query(RuntimeError("connection refused"))):
        r = client.get("/admin/lmnr/diagnose")

    assert r.status_code == 200, r.text[:300]
    assert "ClickHouse UNREACHABLE" in r.text
    assert "connection refused" in r.text
    # Actionable, not just red: it names the vars to check.
    assert "LMNR_CLICKHOUSE_URL" in r.text


def test_diagnose_distinguishes_an_empty_store_from_a_broken_one(client):
    """Reachable but holding no recent spans is a WRITE-side fault."""
    with patch("core.lmnr_clickhouse.query", lambda *a, **k: [{"spans": "0"}]):
        r = client.get("/admin/lmnr/diagnose")

    assert r.status_code == 200
    assert "ClickHouse reachable" in r.text
    assert "no spans from the last 24h" in r.text


def test_diagnose_points_at_the_portal_for_reading_traces(client):
    """There is no second UI to send anyone to — the portal IS the trace viewer."""
    with patch("core.lmnr_clickhouse.query", lambda *a, **k: [{"spans": "5"}]):
        r = client.get("/admin/lmnr/diagnose")

    assert "/admin/observability" in r.text
