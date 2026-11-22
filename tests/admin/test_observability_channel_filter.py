"""Tests for the channel + agent_kind filter on observability views.

Each streaming request opens an explicit root span
(``common/sse.py:sse_response`` → ``core/observability.py:root_span``) tagged
with ``waj.channel.slug`` / ``waj.channel.kind`` / ``waj.agent_kind``. Those also
land in the trace's **metadata** (via ``Laminar.set_trace_metadata``), which is
what rides on the ``traces_replacing`` row the explorer lists — so the screens
read chips and filters straight off the trace aggregate, with no per-trace span
query. These tests cover:

* ``load_traces_list(channel_slug=...)`` narrows by trace metadata
* ``load_traces_list(agent_kind=...)`` narrows by trace metadata
* both filters together = AND semantics
* every trace row carries ``channel_slug`` + ``agent_kind`` keys in the
  rendered context (even with no filter) so the chip column renders
* the explorer JSON HTML shell accepts both query params and surfaces a
  filtered trace list

ClickHouse is stubbed via ``tests.ch_fake`` — no network calls.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.ch_fake import ch_trace_row, make_ch_query


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _ch(traces):
    """A fake ``core.lmnr_clickhouse.query`` serving ``traces`` to the trace
    list. Nothing else is stubbed: chips + filters read the trace metadata that
    is already on each row, so no span query is involved."""
    return make_ch_query(traces=traces)


def _trace(
    trace_id,
    *,
    channel_slug=None,
    channel_kind=None,
    agent_kind=None,
):
    """A ``traces_replacing`` row. channel/agent attrs go in ``metadata`` as the
    JSON string the column holds (or "" for a legacy/untagged trace)."""
    extra = {"waj.channel.kind": channel_kind} if channel_kind else None
    return ch_trace_row(
        trace_id,
        channel_slug=channel_slug,
        agent_kind=agent_kind,
        extra_meta=extra,
        in_tokens=5,
        out_tokens=7,
        top_span_name="agent.invoke",
    )


# ---------------------------------------------------------------------------
# load_traces_list filter semantics
# ---------------------------------------------------------------------------


def test_load_traces_list_filters_by_channel_slug():
    """Only traces whose metadata carries the matching
    ``waj.channel.slug`` survive."""
    from admin.screens import observability as obs

    traces = [
        _trace("T-app", channel_slug="waj_app", agent_kind="support_bot"),
        _trace("T-web", channel_slug="waj_web", agent_kind="business"),
        _trace("T-legacy"),  # no metadata (pre-root-span-tagging)
    ]
    ch_query = _ch(traces)

    with patch("core.lmnr_clickhouse.query", ch_query):
        data = obs.load_traces_list(channel_slug="waj_app")

    assert [t["id"] for t in data["traces"]] == ["T-app"]
    assert data["channel_slug"] == "waj_app"
    assert data["total"] == 1


def test_load_traces_list_filters_by_agent_kind():
    """Only traces whose metadata carries the matching ``waj.agent_kind``
    survive."""
    from admin.screens import observability as obs

    traces = [
        _trace("T-sup", channel_slug="waj_app", agent_kind="support_bot"),
        _trace("T-biz", channel_slug="waj_app", agent_kind="business"),
        _trace("T-sales", channel_slug="waj_web", agent_kind="sales"),
    ]
    ch_query = _ch(traces)

    with patch("core.lmnr_clickhouse.query", ch_query):
        data = obs.load_traces_list(agent_kind="support_bot")

    assert [t["id"] for t in data["traces"]] == ["T-sup"]
    assert data["agent_kind"] == "support_bot"


def test_load_traces_list_filters_both_and_semantics():
    """Setting both filters narrows to only traces matching BOTH."""
    from admin.screens import observability as obs

    traces = [
        _trace("T-app-sup", channel_slug="waj_app", agent_kind="support_bot"),
        _trace("T-app-biz", channel_slug="waj_app", agent_kind="business"),
        _trace("T-web-sup", channel_slug="waj_web", agent_kind="support_bot"),
    ]
    ch_query = _ch(traces)

    with patch("core.lmnr_clickhouse.query", ch_query):
        data = obs.load_traces_list(channel_slug="waj_app", agent_kind="support_bot")

    assert [t["id"] for t in data["traces"]] == ["T-app-sup"]


def test_load_traces_list_annotates_every_row_with_channel_meta():
    """Even with no filter set, each rendered trace must carry
    ``channel_slug`` + ``agent_kind`` keys so the chip column renders."""
    from admin.screens import observability as obs

    traces = [
        _trace("T-a", channel_slug="waj_app", agent_kind="support_bot"),
        _trace("T-b"),  # legacy trace (no metadata)
    ]
    ch_query = _ch(traces)

    with patch("core.lmnr_clickhouse.query", ch_query):
        data = obs.load_traces_list()

    rows = {t["id"]: t for t in data["traces"]}
    assert rows["T-a"]["channel_slug"] == "waj_app"
    assert rows["T-a"]["agent_kind"] == "support_bot"
    # Legacy trace gets None for both — template falls back to '—'.
    assert rows["T-b"]["channel_slug"] is None
    assert rows["T-b"]["agent_kind"] is None


def test_load_traces_list_filter_excludes_legacy_traces():
    """Traces with no metadata (pre-root-span-tagging) MUST be dropped when a
    channel filter is active — "show me waj_app traces" means ONLY waj_app."""
    from admin.screens import observability as obs

    traces = [_trace("T-app", channel_slug="waj_app"), _trace("T-legacy")]
    ch_query = _ch(traces)

    with patch("core.lmnr_clickhouse.query", ch_query):
        data = obs.load_traces_list(channel_slug="waj_app")

    assert [t["id"] for t in data["traces"]] == ["T-app"]


def test_load_traces_list_no_filter_returns_everything():
    """When no channel/agent filter is set, every row ClickHouse returned
    surfaces (modulo chip annotation)."""
    from admin.screens import observability as obs

    traces = [
        _trace("T-a", channel_slug="waj_app"),
        _trace("T-b", channel_slug="waj_app"),
        _trace("T-c", channel_slug="waj_app"),
    ]
    ch_query = _ch(traces)

    with patch("core.lmnr_clickhouse.query", ch_query):
        data = obs.load_traces_list()

    assert {t["id"] for t in data["traces"]} == {"T-a", "T-b", "T-c"}


def test_trace_meta_attrs_tolerates_bad_metadata():
    """Metadata parsing is defensive: dict passthrough, blank/None/garbage → {}."""
    from admin.screens import observability as obs

    assert obs._trace_meta_attrs({"metadata": '{"waj.agent_kind": "sales"}'}) == {
        "waj.agent_kind": "sales"
    }
    assert obs._trace_meta_attrs({"metadata": {"waj.agent_kind": "biz"}}) == {
        "waj.agent_kind": "biz"
    }
    assert obs._trace_meta_attrs({"metadata": ""}) == {}
    assert obs._trace_meta_attrs({"metadata": None}) == {}
    assert obs._trace_meta_attrs({"metadata": "not json"}) == {}
    assert obs._trace_meta_attrs({}) == {}


# ---------------------------------------------------------------------------
# Channel picker helper
# ---------------------------------------------------------------------------


def test_channel_picker_returns_slug_name_pairs():
    """Operator-facing dropdown gets {slug, name} rows."""
    from admin import _channel_picker

    fake_rows = [
        {"slug": "waj_app", "name": "WAJ App"},
        {"slug": "mobile_app", "name": "Mobile App"},
        {"slug": "", "name": "should-be-dropped"},  # empty slug filtered
    ]
    with patch("core.channels.list_active", return_value=fake_rows):
        out = _channel_picker.fetch_channels_for_picker()

    assert {row["slug"] for row in out} == {"waj_app", "mobile_app"}
    assert all("name" in row for row in out)


def test_channel_picker_agent_kinds_constant():
    """The picker's kinds are the deployment's own (agent_catalog.picker_kinds)."""
    from admin._channel_picker import AGENT_KINDS
    from admin.agent_kinds import catalog_tuple

    assert AGENT_KINDS == catalog_tuple("picker_kinds")


# ---------------------------------------------------------------------------
# Explorer HTML shell endpoint
# ---------------------------------------------------------------------------


def _ensure_explorer_loaded():
    from admin import observability_explorer_handlers  # noqa: F401


def test_explorer_endpoint_accepts_channel_filters(client):
    """The HTML shell accepts ``?channel_slug=...&agent_kind=...`` and passes
    both through to ``load_traces_list``."""
    _ensure_explorer_loaded()

    traces = [
        _trace("T-app-sup", channel_slug="waj_app", agent_kind="support_bot"),
        _trace("T-app-biz", channel_slug="waj_app", agent_kind="business"),
        _trace("T-web-sup", channel_slug="waj_web", agent_kind="support_bot"),
    ]
    ch_query = _ch(traces)

    from admin.screens import observability as obs

    with patch("core.lmnr_clickhouse.query", ch_query):
        with patch(
            "core.channels.list_active",
            return_value=[{"slug": "waj_app", "name": "WAJ App"}],
        ):
            r = client.get(
                "/admin/observability/explorer"
                "?channel_slug=waj_app&agent_kind=support_bot"
            )

    assert r.status_code == 200, r.text[:200]
    # The filtered trace must be present.
    assert "T-app-sup" in r.text
    # The non-matching traces must NOT appear in the row list.
    assert "T-app-biz" not in r.text
    assert "T-web-sup" not in r.text
    # Filter chips populated.
    assert "All channels" in r.text  # placeholder option always there
    assert "WAJ App" in r.text  # channel option rendered from picker


def test_explorer_endpoint_renders_channel_chip(client):
    """When a trace has ``channel_slug``, the row renders the chip next to the
    root-span name."""
    _ensure_explorer_loaded()

    traces = [_trace("T-app", channel_slug="waj_app", agent_kind="support_bot")]
    ch_query = _ch(traces)

    from admin.screens import observability as obs

    with patch("core.lmnr_clickhouse.query", ch_query):
        with patch("core.channels.list_active", return_value=[]):
            r = client.get("/admin/observability/explorer")

    assert r.status_code == 200
    # The row names the channel in words (no picker name → humanised slug).
    assert "cv-channel" in r.text
    assert "Waj app" in r.text


def test_explorer_endpoint_no_filter_returns_all(client):
    """Without the new query params, every trace surfaces."""
    _ensure_explorer_loaded()

    traces = [
        _trace("T-1", channel_slug="waj_app"),
        _trace("T-2", channel_slug="waj_web"),
    ]
    ch_query = _ch(traces)

    from admin.screens import observability as obs

    with patch("core.lmnr_clickhouse.query", ch_query):
        with patch("core.channels.list_active", return_value=[]):
            r = client.get("/admin/observability/explorer")

    assert r.status_code == 200
    assert "T-1" in r.text
    assert "T-2" in r.text
