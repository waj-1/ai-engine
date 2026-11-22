"""Unit tests for the read-only Laminar ClickHouse client.

httpx is fully mocked — these never touch a real ClickHouse. They pin the
request shape (SQL in the body, RO auth, FORMAT JSON, parameterised values)
and the graceful-failure contract (every error path raises
ClickHouseUnavailable, never leaks an httpx exception to callers).
"""
from __future__ import annotations

import httpx
import pytest

from core import lmnr_clickhouse as ch


class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def test_query_posts_sql_with_auth_and_returns_data(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Resp(json_data={"data": [{"n": 5}], "rows": 1})

    monkeypatch.setattr(ch.httpx, "post", fake_post)
    monkeypatch.setenv("LMNR_CLICKHOUSE_URL", "http://ch-host:8123")
    monkeypatch.setenv("LMNR_CLICKHOUSE_USER", "ro")
    monkeypatch.setenv("LMNR_CLICKHOUSE_PASSWORD", "secret")
    monkeypatch.setenv("LMNR_CLICKHOUSE_DB", "default")

    rows = ch.query("SELECT count() AS n FROM traces_replacing FINAL")

    assert rows == [{"n": 5}]
    assert captured["url"] == "http://ch-host:8123"
    kw = captured["kwargs"]
    # SQL travels in the request body, not the URL.
    assert b"SELECT count()" in kw["content"]
    # RO credentials.
    assert kw["auth"] == ("ro", "secret")
    # JSON output format + database selected via query params.
    assert kw["params"]["default_format"] == "JSON"
    assert kw["params"]["database"] == "default"


def test_query_passes_parameters_as_param_prefixed(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["kwargs"] = kwargs
        return _Resp(json_data={"data": []})

    monkeypatch.setattr(ch.httpx, "post", fake_post)
    ch.query("SELECT {h:UInt32}", params={"h": 24})

    assert captured["kwargs"]["params"]["param_h"] == "24"


def test_query_raises_on_transport_error(monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(ch.httpx, "post", boom)
    with pytest.raises(ch.ClickHouseUnavailable):
        ch.query("SELECT 1")


def test_query_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(ch.httpx, "post",
                        lambda url, **kw: _Resp(status_code=500, text="Code: 47. Unknown column"))
    with pytest.raises(ch.ClickHouseUnavailable):
        ch.query("SELECT bad")


def test_query_raises_on_non_json_body(monkeypatch):
    class _BadJson(_Resp):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(ch.httpx, "post", lambda url, **kw: _BadJson(status_code=200))
    with pytest.raises(ch.ClickHouseUnavailable):
        ch.query("SELECT 1")


def test_query_returns_empty_list_when_data_missing(monkeypatch):
    monkeypatch.setattr(ch.httpx, "post", lambda url, **kw: _Resp(json_data={"rows": 0}))
    assert ch.query("SELECT 1 WHERE 0") == []


# --- Failure classification -------------------------------------------------
#
# The portal used to render every failure as "could not be reached", so a
# ClickHouse that was up and REJECTING a query (Code: 10 on the trace list,
# 2026-08-26 → 2026-09-29) sent the operator to check the network and the
# credentials for a month. Each failure now carries what actually went wrong.


def test_transport_error_is_classified_unreachable(monkeypatch):
    ch.clear_last_failure()

    def boom(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(ch.httpx, "post", boom)
    with pytest.raises(ch.ClickHouseUnavailable) as ei:
        ch.query("SELECT 1")
    assert ei.value.kind == "unreachable"
    assert ch.last_failure().kind == "unreachable"


@pytest.mark.parametrize("status", [401, 403, 516])
def test_auth_rejection_is_classified_auth(monkeypatch, status):
    ch.clear_last_failure()
    monkeypatch.setattr(ch.httpx, "post", lambda url, **kw: _Resp(
        status_code=status, text="Code: 516. DB::Exception: clickhouse: Authentication failed"))
    with pytest.raises(ch.ClickHouseUnavailable) as ei:
        ch.query("SELECT 1")
    assert ei.value.kind == "auth"


def test_query_error_is_classified_rejected_and_keeps_the_message(monkeypatch):
    ch.clear_last_failure()
    monkeypatch.setattr(ch.httpx, "post", lambda url, **kw: _Resp(
        status_code=500, text="Code: 10. DB::Exception: Not found column and(greater(...))"))
    with pytest.raises(ch.ClickHouseUnavailable) as ei:
        ch.query("SELECT bad")
    assert ei.value.kind == "rejected"
    failure = ch.last_failure()
    assert failure.kind == "rejected"
    assert "Code: 10" in failure.message


def test_last_failure_expires(monkeypatch):
    ch.clear_last_failure()
    monkeypatch.setattr(ch.httpx, "post", lambda url, **kw: _Resp(status_code=500, text="Code: 62."))
    with pytest.raises(ch.ClickHouseUnavailable):
        ch.query("SELECT bad")
    assert ch.last_failure(max_age_sec=60) is not None
    assert ch.last_failure(max_age_sec=0) is None
