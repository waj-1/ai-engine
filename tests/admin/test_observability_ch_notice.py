"""The observability banner must say WHY ClickHouse failed.

It used to be one fixed string — "could not be reached. Check
LMNR_CLICKHOUSE_URL / credentials" — for every failure. On 2026-09-29 the
ClickHouse was up and rejecting the trace-list SQL (Code: 10), and the banner
pointed at the network for a month. `_ch_notice()` now reads the failure the
client recorded and names the real cause.
"""
from __future__ import annotations

import pytest

from core import lmnr_clickhouse as ch


@pytest.fixture(autouse=True)
def _clean_failure():
    ch.clear_last_failure()
    yield
    ch.clear_last_failure()


def _record(kind: str, message: str) -> None:
    ch._record_failure(ch.ClickHouseUnavailable(message, kind=kind))


def test_rejected_query_names_the_clickhouse_error_not_connectivity():
    import admin.screens.observability as obs

    _record("rejected", "ClickHouse returned 500: Code: 10. DB::Exception: Not found column and(...)")
    notice = obs._ch_notice()

    assert "rejected" in notice
    assert "Code: 10" in notice
    assert "could not be reached" not in notice


def test_unreachable_keeps_the_connectivity_hint():
    import admin.screens.observability as obs

    _record("unreachable", "ClickHouse request failed: connection refused")
    notice = obs._ch_notice()

    assert "could not be reached" in notice
    assert "LMNR_CLICKHOUSE_URL" in notice
    assert "connection refused" in notice


def test_auth_failure_points_at_credentials():
    import admin.screens.observability as obs

    _record("auth", "ClickHouse returned 516: Code: 516. Authentication failed")
    notice = obs._ch_notice()

    assert "LMNR_CLICKHOUSE_PASSWORD" in notice
    assert "could not be reached" not in notice


def test_no_recorded_failure_falls_back_to_the_generic_notice():
    import admin.screens.observability as obs

    assert obs._ch_notice() == obs._CH_NOTICE


def test_long_error_text_is_truncated():
    import admin.screens.observability as obs

    _record("rejected", "Code: 10. " + "x" * 5000)
    assert len(obs._ch_notice()) < 800
