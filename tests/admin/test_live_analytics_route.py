"""Route tests for GET /admin/live/analytics.json.

load_analytics is mocked (its own logic is covered in test_live_analytics.py),
so these pin the HTTP contract: superuser gating, query-param parsing/validation,
and that the payload is returned verbatim as JSON.
"""
from __future__ import annotations

from unittest.mock import patch

_FAKE = {"ok": True, "window_hours": 6, "totals": {"volume": 3},
         "buckets": [], "series": {}, "per_agent": [], "top_tools": []}


def test_analytics_json_returns_payload(client):
    with patch("admin.screens.live_analytics.load_analytics", return_value=_FAKE):
        r = client.get("/admin/live/analytics.json?hours=6")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["totals"]["volume"] == 3


def test_analytics_json_forwards_filters():
    # Dedicated app instance so we can assert exact call args without the
    # session client's other patches interfering.
    from main import app
    from admin import auth
    from tests.admin.conftest import _fake_smoke_superuser
    from fastapi.testclient import TestClient

    app.dependency_overrides[auth.require_superuser] = _fake_smoke_superuser
    with patch("admin.screens.live_analytics.load_analytics", return_value=_FAKE) as m:
        with TestClient(app) as c:
            r = c.get("/admin/live/analytics.json"
                      "?hours=720&agent_kind=business&channel=waj_app"
                      "&status=error&environment=staging")
    assert r.status_code == 200
    m.assert_called_once_with(hours=720, agent_kind="business",
                              channel="waj_app", status="error",
                              environment="staging")


def test_analytics_json_rejects_bad_status(client):
    r = client.get("/admin/live/analytics.json?status=bogus")
    assert r.status_code == 422


def test_analytics_json_rejects_out_of_range_hours(client):
    r = client.get("/admin/live/analytics.json?hours=0")
    assert r.status_code == 422
