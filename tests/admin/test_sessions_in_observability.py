"""The standalone Sessions browser was retired — its conversation view is now
the Observability explorer's **Transcript** tab (backed by the same persisted
``ai_chat_messages``), and trace/session metrics live in the new **Details**
tab. These tests assert the consolidation:

* The explorer renders the three right-pane tabs (Transcript / Spans / Details)
  and no longer pulls in the old Traces/Sessions tab strip.
* All session routes 302 into the explorer; a session id resolves to its
  Laminar trace and deep-links the Transcript (``#trace=…``).
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def jinja_env():
    from admin.router import templates

    return templates.env


_BASE_CTX = {
    "ctx": {"email": "admin@waj.test"},
    "current_env": "dev",
    "request": None,
}


def test_explorer_renders_three_right_pane_tabs(jinja_env) -> None:
    tmpl = jinja_env.get_template("observability_explorer.html")
    html = tmpl.render(
        active="observability", groups=[], total=0, traces=[], tool_names=[],
        channels=[], agent_options=[], agent_kinds=[],
        hours_options=((24, "Last 24 hours"),), outcome_options=(("", "Any outcome"),),
        past_hours=24, search="", status="", outcome="", tool="", channel_slug="",
        agent_kind="", group_by_session=True, show_background=False,
        more_filters_active=False, list_unavailable=False, list_detail=None,
        tool_filter_failed=False, tool_filter_notice=None,
        **_BASE_CTX,
    )
    # Conversation · Call audio · Technical (spans + details live in Technical).
    assert "OX.setView('transcript')" in html
    assert "OX.setView('audio')" in html
    assert "OX.setView('technical')" in html
    assert 'id="ox-span-tree"' in html and 'id="ox-details"' in html
    # The retired Traces/Sessions top tab strip is gone.
    assert 'aria-label="Observability sections"' not in html
    assert "/admin/observability/sessions" not in html


def test_session_routes_redirect_into_explorer(client) -> None:
    # Bare session paths → explorer (no trace to resolve).
    for path in ("/admin/sessions", "/admin/observability/sessions"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, path
        assert r.headers["location"] == "/admin/observability/explorer", path


def test_session_id_deeplinks_to_its_trace(client, monkeypatch) -> None:
    """A session id resolves to its latest Laminar trace and deep-links the
    explorer Transcript via the #trace hash."""
    import admin.router as router

    class _Resp:
        data = [{"lmnr_trace_id": "trace-abc"}]

    class _Q:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def not_(self):  # pragma: no cover - attribute access form used instead
            return self
        def is_(self, *a, **k): return self
        def order(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return _Resp()

    # PostgREST builder exposes `.not_.is_(...)`; emulate that chain.
    _q = _Q()
    _q.not_ = _q  # type: ignore[attr-defined]

    class _Client:
        def table(self, *a, **k): return _q

    monkeypatch.setattr(router, "client_for", lambda env: _Client())

    r = client.get("/admin/observability/sessions/sess-123", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/observability/explorer#trace=trace-abc"
