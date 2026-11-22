"""Security regression test for the read-only Laminar ClickHouse client.

``core.lmnr_clickhouse.query`` is the only client-side write guard between the
admin analytics dashboard and Laminar's ClickHouse: it forwards ``readonly=1``
on every HTTP request so a malformed (or maliciously crafted) ``SELECT`` cannot
mutate state even by accident. That invariant lives only in one dict literal and
was previously unasserted — this pins it so a regression dropping ``readonly=1``
(or ``default_format=JSON``) fails the suite.

httpx is fully mocked; this never touches a real ClickHouse.
"""
from __future__ import annotations

from core import lmnr_clickhouse as ch


class _Resp:
    """Minimal httpx.Response stand-in: 200 OK with an empty result set."""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {"data": []}
        self.text = text

    def json(self):
        return self._json


def test_query_sends_readonly_one_and_json_format(monkeypatch):
    """An arbitrary SELECT must carry readonly=1 (string) and default_format=JSON."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Resp(json_data={"data": [], "rows": 0})

    # Patch the module-level httpx seam so no real request is issued.
    monkeypatch.setattr(ch.httpx, "post", fake_post)
    # Match the env/config the existing test sets up so _config() resolves and
    # query() actually issues the request.
    monkeypatch.setenv("LMNR_CLICKHOUSE_URL", "http://ch-host:8123")
    monkeypatch.setenv("LMNR_CLICKHOUSE_USER", "ro")
    monkeypatch.setenv("LMNR_CLICKHOUSE_PASSWORD", "secret")
    monkeypatch.setenv("LMNR_CLICKHOUSE_DB", "default")

    ch.query("SELECT count() AS n FROM traces_replacing FINAL")

    params = captured["kwargs"]["params"]
    # The security invariant: readonly=1 is forwarded as the string "1".
    assert params["readonly"] == "1"
    assert isinstance(params["readonly"], str)
    # JSON output format so resp.json() parses a structured result.
    assert params["default_format"] == "JSON"
