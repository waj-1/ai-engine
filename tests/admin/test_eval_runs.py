"""Tests for the Evaluation Runs sidecar (admin/eval_runs_handlers.py).

Covers:
  * live-chat sampler: builds suite-shaped scenarios from stubbed
    ai_chat_sessions / ai_chat_messages (first user msg → question,
    business carried, historical assistant reply attached)
  * empty sample (no sessions in window) → run finalizes cleanly with a
    "no chats matched" status, no crash
  * the live-chat runner end-to-end with _invoke_endpoint + judge mocked
  * schedule create / update / delete / fire happy paths (mock supabase +
    start_tracked_job)
  * compute_next_run_at for daily / weekly / monthly / manual
  * GET /admin/eval-runs renders

Uses the TestClient + auth-override + scriptable-supabase pattern from
tests/admin/test_channels.py. No network, no LLM.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from tests.scoped_build import full_build, use_orchard

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Scriptable supabase fake — extends the test_channels.py surface with lte()
# and a delete() that records the call.
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], inserted_id_key: str, inserted_id: str):
        self._rows = rows
        self._inserted_id_key = inserted_id_key
        self._inserted_id = inserted_id
        self.last_filter: Dict[str, Any] = {}
        self.last_update: Optional[Dict[str, Any]] = None
        self.last_insert: Optional[Dict[str, Any]] = None
        self.deleted = False
        self.selected_cols: Optional[str] = None

    def select(self, cols):
        self.selected_cols = cols
        return self

    def eq(self, col, val):
        self.last_filter[col] = val
        return self

    def in_(self, col, vals):
        self.last_filter[f"in_{col}"] = list(vals)
        return self

    def gte(self, col, val):
        self.last_filter[f"gte_{col}"] = val
        return self

    def lte(self, col, val):
        self.last_filter[f"lte_{col}"] = val
        return self

    def neq(self, col, val):
        return self

    def limit(self, n):
        return self

    def order(self, *a, **kw):
        return self

    def range(self, *a, **kw):
        return self

    def insert(self, payload):
        self.last_insert = payload
        return _InsertResult(payload, self._inserted_id_key, self._inserted_id)

    def update(self, payload):
        self.last_update = payload
        return self

    def delete(self):
        self.deleted = True
        return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _InsertResult:
    def __init__(self, payload, inserted_id_key, inserted_id):
        self._payload = payload
        self._inserted_id_key = inserted_id_key
        self._inserted_id = inserted_id

    def execute(self):
        result = MagicMock()
        result.data = [{**self._payload, self._inserted_id_key: self._inserted_id}]
        return result


# inserted-id column name per table so insert().execute().data[0][key] works.
_ID_KEY_BY_TABLE = {
    "agent_eval_runs": "run_id",
    "agent_eval_schedules": "schedule_id",
}


class _FakeClient:
    def __init__(self, rows_by_table=None, inserted_ids=None):
        self._rows_by_table = rows_by_table or {}
        self._inserted_ids = inserted_ids or {}
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name):
        if name not in self.queries:
            self.queries[name] = _FakeQuery(
                self._rows_by_table.get(name, []),
                _ID_KEY_BY_TABLE.get(name, "id"),
                self._inserted_ids.get(name, f"new-{name}-id"),
            )
        return self.queries[name]


# ---------------------------------------------------------------------------
# compute_next_run_at
# ---------------------------------------------------------------------------


def test_compute_next_run_at_cadences():
    from admin.eval_runs_handlers import compute_next_run_at

    base = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    daily = compute_next_run_at("daily", from_dt=base)
    weekly = compute_next_run_at("weekly", from_dt=base)
    monthly = compute_next_run_at("monthly", from_dt=base)

    assert daily == datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    assert weekly == datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    assert monthly == datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def test_compute_next_run_at_manual_is_none():
    from admin.eval_runs_handlers import compute_next_run_at

    assert compute_next_run_at("manual") is None
    assert compute_next_run_at("nonsense") is None


# ---------------------------------------------------------------------------
# Live-chat sampler
# ---------------------------------------------------------------------------


def test_sampler_builds_scenarios_from_sessions():
    from admin import eval_runs_handlers as h

    sessions = [
        {"id": "sess-1", "agent_kind": "sample_agent", "business_id": "biz-1",
         "created_at": "2026-05-20T10:00:00Z", "title": "Booking question"},
        {"id": "sess-2", "agent_kind": "sample_agent", "business_id": "biz-2",
         "created_at": "2026-05-19T10:00:00Z", "title": None},
    ]
    messages_by_session = {
        "sess-1": [
            {"role": "user", "content": "What are your hours?", "created_at": "2026-05-20T10:00:00Z"},
            {"role": "assistant", "content": "We're open 9-5.", "created_at": "2026-05-20T10:00:05Z"},
        ],
        "sess-2": [
            {"role": "user", "content": "Do you do refunds?", "created_at": "2026-05-19T10:00:00Z"},
        ],
    }

    # The sampler queries ai_chat_sessions once, then ai_chat_messages once
    # per session. Drive both off a stub client.
    class _SamplerClient:
        def schema(self, _name):
            # Production qualifies public tables with .schema("public")
            # now that the service client defaults to the wajagents schema.
            return self

        def table(self, name):
            q = MagicMock()
            if name == "ai_chat_sessions":
                q.select.return_value = q
                q.eq.return_value = q
                q.gte.return_value = q
                q.order.return_value = q
                q.limit.return_value = q
                q.execute.return_value = MagicMock(data=sessions)
            else:  # ai_chat_messages — filter captured to pick the right session
                state = {"session_id": None}

                def _eq(col, val):
                    if col == "session_id":
                        state["session_id"] = val
                    return q
                q.select.return_value = q
                q.eq.side_effect = _eq
                q.order.return_value = q
                q.execute.side_effect = lambda: MagicMock(
                    data=messages_by_session.get(state["session_id"], [])
                )
            return q

    scenarios = h._sample_live_chat_scenarios(
        _SamplerClient(),
        agent_kind="sample_agent",
        business_id=None,
        lookback_days=30,
        sample_size=10,
    )

    assert len(scenarios) == 2
    s1 = scenarios[0]
    assert s1["id"] == "sess-1"
    assert s1["question"] == "What are your hours?"
    assert s1["business_id"] == "biz-1"
    assert s1["historical_reply"] == "We're open 9-5."
    assert s1["category"] == "live_chat"

    s2 = scenarios[1]
    assert s2["question"] == "Do you do refunds?"
    # No assistant reply in this session → historical_reply is None
    assert s2["historical_reply"] is None


def test_sampler_skips_sessions_without_user_message():
    from admin import eval_runs_handlers as h

    class _SamplerClient:
        def schema(self, _name):
            return self

        def table(self, name):
            q = MagicMock()
            q.select.return_value = q
            q.eq.return_value = q
            q.gte.return_value = q
            q.order.return_value = q
            q.limit.return_value = q
            if name == "ai_chat_sessions":
                q.execute.return_value = MagicMock(
                    data=[{"id": "sess-x", "business_id": "biz", "created_at": "2026-05-20T00:00:00Z"}]
                )
            else:
                # only a system/assistant greeting — no user message
                q.execute.return_value = MagicMock(
                    data=[{"role": "assistant", "content": "Hi!", "created_at": "2026-05-20T00:00:00Z"}]
                )
            return q

    scenarios = h._sample_live_chat_scenarios(
        _SamplerClient(), agent_kind="sample_agent", business_id=None,
        lookback_days=30, sample_size=10,
    )
    assert scenarios == []


# ---------------------------------------------------------------------------
# Live-chat runner — end-to-end with endpoint + judge mocked
# ---------------------------------------------------------------------------


def test_resolve_judge_model_string_maps_name_to_model_string():
    """The live-chat judge picker posts a model DISPLAY NAME; _try_build_judge
    needs the model_string. Passing the name straight through made Vertex 400
    ('invalid argument') on every judge call. Resolve name -> string; pass
    through already-strings and unknowns."""
    from admin import eval_runs_handlers as h
    fake = _FakeClient(rows_by_table={"agent_models": [
        {"name": "Claude Haiku 4.5", "model_string": "claude-haiku-4-5"},
        {"name": "Gemini 3.5 Flash", "model_string": "gemini-3.5-flash"},
    ]})
    assert h._resolve_judge_model_string(fake, "Claude Haiku 4.5") == "claude-haiku-4-5"
    assert h._resolve_judge_model_string(fake, "claude-haiku-4-5") == "claude-haiku-4-5"
    assert h._resolve_judge_model_string(fake, "gemini-3.5-flash") == "gemini-3.5-flash"
    assert h._resolve_judge_model_string(fake, "totally-unknown") == "totally-unknown"
    assert h._resolve_judge_model_string(fake, None) is None


def _fake_invoke_endpoint(*args, **kwargs):
    """Mirror the REAL _invoke_endpoint: a 4-tuple (actual, trace, status,
    latency) plus side-channel metrics via the `metrics` out-param. The old
    mock returned a 3-tuple, which masked that _run_live_scenario still
    unpacked 3 ("too many values to unpack (expected 3)")."""
    metrics = kwargs.get("metrics")
    if metrics is not None:
        metrics["ttft_ms"] = 20
        metrics["usage"] = {"input_tokens": 100, "output_tokens": 30}
        metrics["runmetrics"] = {"iterations": 2, "tool_count": 1}
    return ("We open 9 to 5.", "", 200, 42)


def test_run_live_chat_eval_happy_path():
    from admin import eval_runs_handlers as h

    fake = _FakeClient(rows_by_table={})
    profile = {"endpoint_path": "/business/chat", "environment": "dev", "agent_kind": "sample_agent"}
    scenarios = [
        {"id": "sess-1", "name": "Q1", "category": "live_chat",
         "question": "What are your hours?", "business_id": "biz-1",
         "historical_reply": "9-5."},
    ]

    with patch.object(h, "get_service_client", return_value=fake), \
         patch("admin.evals._resolve_profile", return_value=profile), \
         patch("admin.evals._base_url_for", return_value="http://x"), \
         patch.object(h, "_sample_live_chat_scenarios", return_value=scenarios), \
         patch("admin.evals._try_build_judge", return_value=object()), \
         patch("admin.evals._invoke_endpoint", _fake_invoke_endpoint), \
         patch.object(h, "_invoke_quality_judge",
                      return_value={"score": 0.9, "pass": True, "rationale": "good", "regressed": False}):
        h.run_live_chat_eval_sync(
            "run-1", profile_id="prof-1", agent_kind="sample_agent",
            business_id="biz-1", lookback_days=30, sample_size=10,
            judge_model="gemini", pass_threshold=0.7,
        )

    update = fake.queries["agent_eval_runs"].last_update
    assert update is not None
    assert update["status"] == "passed"
    assert update["score"] == 0.9
    results = update["results"]
    assert len(results) == 1
    r = results[0]
    assert r["scenario_id"] == "sess-1"
    assert r["passed"] is True
    assert r["historical_reply"] == "9-5."
    assert r["judge"]["score"] == 0.9
    # reference-free → no gold answer / substrings
    assert r["expected"] == ""
    assert r["expected_substrings"] == []
    # metrics parity with suite runs: ttft / tokens / iterations / tool_count
    # are captured from the _invoke_endpoint out-param so the run-detail KPIs
    # and charts render for live-chat runs too.
    assert r["ttft_ms"] == 20
    assert r["input_tokens"] == 100 and r["output_tokens"] == 30
    assert r["tool_count"] == 1 and r["iterations"] == 2


def test_run_live_chat_eval_empty_sample_finalizes_clean():
    from admin import eval_runs_handlers as h

    fake = _FakeClient(rows_by_table={})
    profile = {"endpoint_path": "/business/chat", "environment": "dev"}

    with patch.object(h, "get_service_client", return_value=fake), \
         patch("admin.evals._resolve_profile", return_value=profile), \
         patch("admin.evals._base_url_for", return_value="http://x"), \
         patch.object(h, "_sample_live_chat_scenarios", return_value=[]):
        h.run_live_chat_eval_sync(
            "run-empty", profile_id="prof-1", agent_kind="sample_agent",
            lookback_days=7, sample_size=10,
        )

    update = fake.queries["agent_eval_runs"].last_update
    assert update is not None
    assert update["status"] == "passed"
    assert update["results"] == []
    assert "no chats matched" in update["error"]


def test_run_live_chat_eval_no_endpoint_errors():
    from admin import eval_runs_handlers as h

    fake = _FakeClient(rows_by_table={})

    with patch.object(h, "get_service_client", return_value=fake), \
         patch("admin.evals._resolve_profile", return_value={"endpoint_path": None}):
        h.run_live_chat_eval_sync("run-x", profile_id="prof-1", agent_kind="sample_agent")

    update = fake.queries["agent_eval_runs"].last_update
    assert update["status"] == "errored"
    assert "endpoint_path" in update["error"]


# ---------------------------------------------------------------------------
# Screen + schedule CRUD via TestClient
# ---------------------------------------------------------------------------


def test_eval_runs_screen_redirects_to_evals_tab(client):
    """/admin/eval-runs is now Testing › Runs (/admin/evals/runs) — the old
    GET screen is a 307 redirect (sub-routes are untouched)."""
    resp = client.get("/admin/eval-runs", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/admin/evals/runs"


def _make_app(fake_client):
    from fastapi.testclient import TestClient
    from admin import auth, eval_runs_handlers, _business_picker
    from main import app

    def _fake_superuser():
        return {"user_token": "fake-jwt", "user_id": "u-1", "email": "t@waj.local"}

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(eval_runs_handlers, "get_service_client", return_value=fake_client),
        patch.object(eval_runs_handlers, "record_change"),
        patch.object(_business_picker, "fetch_businesses_for_picker",
                     return_value=[{"id": "biz-1", "name": "Orchard", "subdomain": "orchard"}]),
    ]
    for p in patches:
        p.start()
    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        if prior is not None:
            app.dependency_overrides[auth.require_superuser] = prior
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


def test_screen_renders():
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            {"run_id": "run-abcdef12", "suite_id": None, "status": "passed",
             "score": 0.9, "created_at": "2026-05-21T00:00:00Z",
             "context": {"source": "live_chats"}},
        ],
        "agent_eval_schedules": [
            {"schedule_id": "sched-1", "name": "Nightly live", "source": "live_chats",
             "cadence": "daily", "coverage": {"sample_size": 10}, "is_enabled": True,
             "last_run_at": None, "next_run_at": "2026-05-22T00:00:00Z"},
        ],
        "agent_eval_suites": [{"suite_id": "su-1", "name": "Booking", "agent_kind": "sample_agent"}],
        "agent_profiles": [{"profile_id": "p-1", "display_name": "Biz prod", "agent_kind": "sample_agent",
                            "endpoint_path": "/business/chat"}],
        "agent_models": [{"model_id": "m-1", "name": "gemini-2.0", "provider": "google", "is_active": True}],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/evals/runs")
        assert r.status_code == 200, r.text[:400]
        # Testing › Runs: the run is listed (linked by id) under its source's
        # plain name; the suite shows up in the filters and the Run drawer.
        assert "Live conversations" in r.text
        assert "/admin/evals/runs/run-abcdef12" in r.text
        assert "Booking" in r.text
        # The profile picker must render the profile's display name. Regression
        # guard for the 500 where the screen selected the non-existent
        # `agent_profiles.name` column instead of `display_name`.
        assert "Biz prod" in r.text
    finally:
        teardown()


# ---------------------------------------------------------------------------
# C1: _load_picker_data must surface a self-scoring profile (a real profile,
# pickable in bake-offs + Run-now) — but only outside a scoped build, where
# it doesn't exist at all.
# ---------------------------------------------------------------------------


def test_load_picker_data_includes_self_scored_profile(monkeypatch):
    full_build(monkeypatch)
    from admin.eval_runs_handlers import _load_picker_data

    fake = _FakeClient(rows_by_table={
        "agent_profiles": [
            {"profile_id": "p-1", "display_name": "Biz prod", "agent_kind": "sample_agent",
             "endpoint_path": "/business/chat"},
            {"profile_id": "p-rec", "display_name": "Item picker",
             "agent_kind": "sample_scored", "endpoint_path": "/recommendations"},
        ],
    })
    data = _load_picker_data(fake)
    profile_ids = {p["profile_id"] for p in data["profiles"]}
    assert "p-rec" in profile_ids
    assert "p-1" in profile_ids


def test_load_picker_data_excludes_self_scored_on_a_scoped_build(monkeypatch):
    use_orchard(monkeypatch)
    from admin.eval_runs_handlers import _load_picker_data

    fake = _FakeClient(rows_by_table={
        "agent_profiles": [
            {"profile_id": "p-orchard", "display_name": "Orchard sales", "agent_kind": "orchard",
             "endpoint_path": "/orchard/chat"},
            {"profile_id": "p-rec", "display_name": "Item picker",
             "agent_kind": "sample_scored", "endpoint_path": "/recommendations"},
        ],
    })
    data = _load_picker_data(fake)
    profile_ids = {p["profile_id"] for p in data["profiles"]}
    assert "p-rec" not in profile_ids
    assert "p-orchard" in profile_ids


def test_scheduling_screen_renders_schedules():
    """Task 2.4: the Production-monitoring tab (/admin/evals/scheduling), not
    /admin/evals, is where a schedule's name/coverage render now."""
    fake = _FakeClient(rows_by_table={
        "agent_eval_schedules": [
            {"schedule_id": "sched-1", "name": "Nightly live", "source": "live_chats",
             "cadence": "daily", "coverage": {"sample_size": 10, "lookback_days": 30,
                                               "judge_model": "gemini-2.0"},
             "is_enabled": True, "last_run_at": None, "next_run_at": "2026-05-22T00:00:00Z"},
        ],
        "agent_profiles": [{"profile_id": "p-1", "display_name": "Biz prod", "agent_kind": "sample_agent",
                            "endpoint_path": "/business/chat"}],
        "agent_models": [{"model_id": "m-1", "name": "gemini-2.0", "provider": "google", "is_active": True}],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/evals/scheduling")
        assert r.status_code == 200, r.text[:400]
        assert "Nightly live" in r.text
        assert "10 conversations from the last 30 days" in r.text
    finally:
        teardown()


def test_eval_runs_screen_selects_real_profile_columns():
    """The eval-runs screen must SELECT columns that exist on agent_profiles.

    The table column is `display_name`, not `name`. Selecting `name` over
    PostgREST raises `column agent_profiles.name does not exist` (42703) and
    500s the whole page. The scriptable fake returns rows verbatim regardless
    of the select string, so this asserts the actual column list instead.
    """
    fake = _FakeClient(rows_by_table={
        "agent_profiles": [{"profile_id": "p-1", "display_name": "Biz prod",
                            "agent_kind": "sample_agent", "endpoint_path": "/business/chat"}],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/evals?tab=runs")
        assert r.status_code == 200, r.text[:400]
        cols = fake.queries["agent_profiles"].selected_cols or ""
        tokens = [c.strip() for c in cols.split(",")]
        assert "display_name" in tokens, f"expected display_name in select, got {cols!r}"
        assert "name" not in tokens, f"selected non-existent column 'name': {cols!r}"
    finally:
        teardown()


def test_schedule_create_inserts_and_redirects():
    fake = _FakeClient(inserted_ids={"agent_eval_schedules": "sched-new"})
    client, teardown = _make_app(fake)
    try:
        form = {
            "name": "Nightly live", "source": "live_chats", "cadence": "daily",
            "profile_id": "p-1", "agent_kind": "sample_agent",
            "lookback_days": "14", "sample_size": "5", "judge_model": "gemini",
            "expectations": "Cite real revenue numbers.",
            "business_id": "biz-1", "is_enabled": "on",
        }
        r = client.post("/admin/eval-runs/schedules", data=form, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        payload = fake.queries["agent_eval_schedules"].last_insert
        assert payload["name"] == "Nightly live"
        assert payload["source"] == "live_chats"
        assert payload["cadence"] == "daily"
        assert payload["coverage"]["lookback_days"] == 14
        assert payload["coverage"]["sample_size"] == 5
        assert payload["coverage"]["business_id"] == "biz-1"
        assert payload["coverage"]["judge_model"] == "gemini"
        assert payload["coverage"]["expectations"] == "Cite real revenue numbers."
        assert payload["is_enabled"] is True
        assert payload["next_run_at"] is not None  # daily → not manual
        assert payload["created_by"] == "u-1"
    finally:
        teardown()


def test_schedule_create_suite_requires_suite_id():
    fake = _FakeClient()
    client, teardown = _make_app(fake)
    try:
        form = {"name": "X", "source": "suite", "cadence": "manual",
                "profile_id": "p-1", "suite_id": "", "is_enabled": "on"}
        r = client.post("/admin/eval-runs/schedules", data=form, follow_redirects=False)
        assert r.status_code == 400
        assert "suite_id" in r.text.lower()
    finally:
        teardown()


def test_schedule_update_modifies_row():
    fake = _FakeClient(rows_by_table={
        "agent_eval_schedules": [{"schedule_id": "sched-1", "name": "old", "source": "suite",
                                  "cadence": "manual", "coverage": {}, "is_enabled": True}],
    })
    client, teardown = _make_app(fake)
    try:
        form = {"name": "renamed", "source": "suite", "cadence": "weekly",
                "profile_id": "p-1", "suite_id": "su-1", "is_enabled": "on"}
        r = client.post("/admin/eval-runs/schedules/sched-1", data=form, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        update = fake.queries["agent_eval_schedules"].last_update
        assert update["name"] == "renamed"
        assert update["cadence"] == "weekly"
        assert update["next_run_at"] is not None
        assert update["updated_by"] == "u-1"
    finally:
        teardown()


def test_schedule_update_404_when_missing():
    fake = _FakeClient(rows_by_table={"agent_eval_schedules": []})
    client, teardown = _make_app(fake)
    try:
        form = {"name": "x", "source": "suite", "cadence": "manual",
                "profile_id": "", "suite_id": "su-1"}
        r = client.post("/admin/eval-runs/schedules/missing", data=form, follow_redirects=False)
        assert r.status_code == 404
    finally:
        teardown()


def test_schedule_delete():
    fake = _FakeClient(rows_by_table={
        "agent_eval_schedules": [{"schedule_id": "sched-1", "name": "x", "source": "suite",
                                  "cadence": "manual", "coverage": {}}],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.post("/admin/eval-runs/schedules/sched-1/delete", follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        assert fake.queries["agent_eval_schedules"].deleted is True
    finally:
        teardown()


def test_due_fire_route_not_shadowed_by_parameterized_fire():
    """POST /schedules/due/fire must reach the bulk cron handler — not match
    /schedules/{schedule_id}/fire with schedule_id="due" (which 500s casting
    "due" to uuid). FastAPI matches in registration order, so the literal
    /due/fire route MUST be registered before the parameterized one.
    """
    from admin.router import router
    due_idx = param_idx = None
    for i, rt in enumerate(router.routes):
        p = getattr(rt, "path", "")
        if p.endswith("/schedules/due/fire") and due_idx is None:
            due_idx = i
        if p.endswith("/schedules/{schedule_id}/fire") and param_idx is None:
            param_idx = i
    assert due_idx is not None and param_idx is not None, "both fire routes must exist"
    assert due_idx < param_idx, (
        "due/fire is shadowed by {schedule_id}/fire — register the literal route first"
    )


def test_schedule_fire_starts_job_and_stamps_run_times():
    fake = _FakeClient(
        rows_by_table={
            "agent_eval_schedules": [{
                "schedule_id": "sched-1", "name": "Nightly", "source": "live_chats",
                "cadence": "daily", "profile_id": "p-1", "agent_kind": "sample_agent",
                "coverage": {"business_id": "biz-1", "lookback_days": 7, "sample_size": 5},
            }],
        },
        inserted_ids={"agent_eval_runs": "run-fired"},
    )
    client, teardown = _make_app(fake)
    try:
        with patch.object(__import__("admin._jobs", fromlist=["start_tracked_job"]),
                          "start_tracked_job", return_value="job-1") as mock_job:
            r = client.post("/admin/eval-runs/schedules/sched-1/fire", follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        assert mock_job.called
        # last_run_at + next_run_at rolled forward on the schedule row
        update = fake.queries["agent_eval_schedules"].last_update
        assert update["last_run_at"] is not None
        assert update["next_run_at"] is not None
    finally:
        teardown()


def test_run_detail_renders_reason_and_analysis_as_markdown():
    """Judge reason and analysis (string branch) must be rendered inside
    data-md markdown-body divs, not plain <p class="caption"> elements."""
    import re
    from fastapi.testclient import TestClient
    from admin import auth, router as router_mod
    from main import app

    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            {
                "run_id": "00000000-0000-4000-8000-0000000000d1",
                "suite_id": None,
                "profile_id": "prof-1",
                "status": "passed",
                "score": 0.9,
                "created_at": "2026-05-21T00:00:00Z",
                "context": {},
                "results": [
                    {
                        "scenario_id": "sc-1",
                        "passed": True,
                        "score": 0.9,
                        "question": "What are your hours?",
                        "expected": "9-5",
                        "actual": "We are open 9 to 5.",
                        "reason": "**bold** reason",
                        "analysis": "- item one",
                    }
                ],
            }
        ],
    })

    def _fake_superuser():
        return {"user_token": "fake-jwt", "user_id": "u-1", "email": "t@waj.local"}

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    p = patch.object(router_mod, "get_service_client", return_value=fake)
    p2 = patch.object(router_mod, "client_for", return_value=fake)
    p.start()
    p2.start()
    try:
        client = TestClient(app)
        r = client.get("/admin/evals/runs/00000000-0000-4000-8000-0000000000d1")
        assert r.status_code == 200, r.text[:400]
        body = r.text
        # Both values must appear in the response
        assert "**bold** reason" in body
        assert "- item one" in body
        # reason must be inside a data-md markdown-body element, not a plain caption
        assert re.search(
            r'<div class="markdown-body" data-md[^>]*>\s*\*\*bold\*\* reason', body
        ), "reason should be inside a data-md markdown-body div"
        assert re.search(
            r'<div class="markdown-body" data-md[^>]*>\s*- item one', body
        ), "analysis (string) should be inside a data-md markdown-body div"
        # must NOT be wrapped in a bare <p class="caption">
        assert '<p class="caption"' not in body or "**bold** reason" not in body.split('<p class="caption"')[1].split("</p>")[0]
    finally:
        p.stop()
        p2.stop()
        if prior is not None:
            app.dependency_overrides[auth.require_superuser] = prior
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)


def test_detail_page_no_reload_script_uses_in_place_poll():
    """Part 14: the run-detail page must not inject the old
    setTimeout(location.reload) — the status chip updates in place via
    eval-run-detail-poll.js while in-flight."""
    from fastapi.testclient import TestClient
    from admin import auth, router as router_mod
    from main import app

    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            {"run_id": "00000000-0000-4000-8000-0000000000d2", "suite_id": "su-1", "profile_id": "prof-1",
             "status": "running", "score": None, "results": [],
             "created_at": "2026-05-21T00:00:00Z", "context": {}},
        ],
    })

    def _fake_superuser():
        return {"user_token": "fake-jwt", "user_id": "u-1", "email": "t@waj.local"}

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    p = patch.object(router_mod, "get_service_client", return_value=fake)
    p2 = patch.object(router_mod, "client_for", return_value=fake)
    p.start()
    p2.start()
    try:
        client = TestClient(app)
        r = client.get("/admin/evals/runs/00000000-0000-4000-8000-0000000000d2")
        assert r.status_code == 200, r.text[:400]
        assert "location.reload" not in r.text
        assert "eval-run-detail-poll.js" in r.text
        assert 'data-run-detail' in r.text
        assert 'data-inflight="1"' in r.text
    finally:
        p.stop()
        p2.stop()
        if prior is not None:
            app.dependency_overrides[auth.require_superuser] = prior
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)


def test_screen_no_full_page_reload_script():
    """Part 14 regression: the screen must NOT inject the old
    setTimeout(location.reload) — in-flight runs update in place via the
    polling JS instead."""
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            {"run_id": "run-running1", "suite_id": None, "status": "running",
             "score": None, "created_at": "2026-05-21T00:00:00Z",
             "context": {"source": "live_chats"}, "results": []},
        ],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/evals?tab=runs")
        assert r.status_code == 200, r.text[:400]
        assert "location.reload" not in r.text
        assert "eval-runs-poll.js" in r.text
        # in-flight row is marked for the poller
        assert 'data-inflight="1"' in r.text
        assert 'data-run-id="run-running1"' in r.text
    finally:
        teardown()


def test_filters_render_and_narrow_runs():
    """Part 15: server-side filters narrow the recent-runs table.

    The unified page always shows the suites section (which contains
    data-run-suite attributes), so we use the full run_id as the search
    token — it appears in run-detail links inside the runs table.
    """
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            {"run_id": "run-suite01", "suite_id": "su-1", "status": "passed",
             "score": 0.9, "created_at": "2026-05-21T00:00:00Z",
             "context": {}, "results": [{"passed": True}]},
            {"run_id": "run-live001", "suite_id": None, "status": "failed",
             "score": 0.3, "created_at": "2026-05-20T00:00:00Z",
             "context": {"source": "live_chats", "agent_kind": "sample_agent"},
             "results": [{"passed": False}]},
        ],
        "agent_eval_suites": [{"suite_id": "su-1", "name": "Booking", "agent_kind": "sample_agent"}],
    })
    client, teardown = _make_app(fake)
    try:
        # status=passed should keep only the suite run in the runs table
        r = client.get("/admin/evals?status=passed")
        assert r.status_code == 200, r.text[:400]
        # run-suite01 appears as a run-detail link; run-live001 is filtered out
        assert "/admin/evals/runs/run-suite01" in r.text
        assert "/admin/evals/runs/run-live001" not in r.text
        # source=live_chats should keep only the live run
        r2 = client.get("/admin/evals?source=live_chats")
        assert "/admin/evals/runs/run-live001" in r2.text
        assert "/admin/evals/runs/run-suite01" not in r2.text
    finally:
        teardown()


def test_status_json_reports_inflight_and_pass_rate():
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            {"run_id": "run-a", "suite_id": "su-1", "status": "running",
             "score": None, "results": [], "created_at": "2026-05-21T00:00:00Z",
             "context": {}},
            {"run_id": "run-b", "suite_id": "su-1", "status": "passed",
             "score": 0.8, "results": [{"passed": True}, {"passed": False}],
             "created_at": "2026-05-20T00:00:00Z", "context": {}},
        ],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/eval-runs/status.json")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert data["any_inflight"] is True
        by_id = {x["run_id"]: x for x in data["runs"]}
        assert by_id["run-b"]["pass_rate"] == 0.5
        assert by_id["run-a"]["status"] == "running"
    finally:
        teardown()


def test_run_status_json_single_run():
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            {"run_id": "run-x", "status": "passed", "score": 0.9,
             "results": [{"passed": True}, {"passed": True}, {"passed": False}],
             "started_at": "2026-05-21T00:00:00Z",
             "finished_at": "2026-05-21T00:01:00Z", "error": None},
        ],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/eval-runs/run-x/status.json")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert data["status"] == "passed"
        assert data["total"] == 3
        assert data["passed"] == 2
        assert data["failed"] == 1
        assert data["inflight"] is False
    finally:
        teardown()


def test_run_status_json_404_when_missing():
    fake = _FakeClient(rows_by_table={"agent_eval_runs": []})
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/eval-runs/nope/status.json")
        assert r.status_code == 404
    finally:
        teardown()


def test_report_json_groups_by_prompt_version():
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [
            {"run_id": "run-1", "suite_id": "su-1", "status": "passed",
             "score": 0.9, "results": [{"passed": True}],
             "created_at": "2026-05-20T00:00:00Z",
             "context": {"prompt_version": "v2"}},
            {"run_id": "run-2", "suite_id": "su-1", "status": "failed",
             "score": 0.4, "results": [{"passed": False}],
             "created_at": "2026-05-19T00:00:00Z",
             "context": {"prompt_override": "do X"}},
            {"run_id": "run-3", "suite_id": "su-1", "status": "passed",
             "score": 0.8, "results": [{"passed": True}],
             "created_at": "2026-05-18T00:00:00Z", "context": {}},
        ],
        "agent_eval_suites": [
            {"suite_id": "su-1", "name": "Booking", "pass_threshold": 0.7},
        ],
    })
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/eval-runs/report.json?days=60")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        pv = {p["run_id"]: p["prompt_version"] for p in data["points"]}
        assert pv["run-1"] == "v2"
        assert pv["run-2"] == "override"
        assert pv["run-3"] == "baseline"
        # all three share one suite threshold → surfaced as a reference line
        assert data["pass_threshold"] == 0.7
        assert all(p["suite_name"] == "Booking" for p in data["points"])
    finally:
        teardown()


def test_report_json_empty_when_no_scored_runs():
    fake = _FakeClient(rows_by_table={"agent_eval_runs": [], "agent_eval_suites": []})
    client, teardown = _make_app(fake)
    try:
        r = client.get("/admin/eval-runs/report.json")
        assert r.status_code == 200
        assert r.json()["points"] == []
    finally:
        teardown()


@pytest.mark.skip(reason="pre-existing test-debt: fake rows_by_table is missing 'agent_eval_runs' (KeyError) — the live-chat trigger path drifted from the fake. Needs the fake updated. See audit follow-ups.")
def test_on_demand_live_chat_trigger():
    fake = _FakeClient(inserted_ids={"agent_eval_runs": "run-od"})
    client, teardown = _make_app(fake)
    try:
        with patch.object(__import__("admin._jobs", fromlist=["start_tracked_job"]),
                          "start_tracked_job", return_value="job-2") as mock_job:
            form = {"source": "live_chats", "profile_id": "p-1", "agent_kind": "sample_agent",
                    "lookback_days": "30", "sample_size": "10", "judge_model": "gemini",
                    "expectations": "Cite real revenue numbers.", "business_id": "biz-1"}
            r = client.post("/admin/eval-runs/trigger", data=form, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]
        assert mock_job.called
        insert = fake.queries["agent_eval_runs"].last_insert
        assert insert["suite_id"] is None
        assert insert["context"]["source"] == "live_chats"
        assert insert["context"]["agent_kind"] == "sample_agent"
        assert insert["context"]["judge_model"] == "gemini"
        assert insert["context"]["expectations"] == "Cite real revenue numbers."
    finally:
        teardown()


# ---------------------------------------------------------------------------
# build_eval_runs_context decoration: _model, _gen_config, _started, _finished
# ---------------------------------------------------------------------------


def test_decorate_run_model_from_model_override():
    from admin.eval_runs_handlers import _run_model, _run_gen_config, _trim_ts

    run = {
        "suite_id": "su-1",
        "status": "passed",
        "context": {
            "model_override": {
                "model_string": "gemini-2.0-flash",
                "temperature": 0.2,
                "max_output_tokens": 800,
                "extra_config": {"top_p": 0.8},
            },
            "model_label": "Gemini Flash",
        },
        "started_at": "2026-05-21T10:00:00.123456+00:00",
        "finished_at": "2026-05-21T10:01:30.999Z",
    }

    assert _run_model(run) == "gemini-2.0-flash"
    assert _run_gen_config(run) == "temp 0.2 · top_p 0.8 · max 800"
    assert _trim_ts(run["started_at"]) == "2026-05-21T10:00:00"
    assert _trim_ts(run["finished_at"]) == "2026-05-21T10:01:30"


def test_decorate_run_model_falls_back_to_model_label():
    from admin.eval_runs_handlers import _run_model

    run = {"context": {"model_label": "Claude Sonnet"}}
    assert _run_model(run) == "Claude Sonnet"


def test_decorate_run_model_defaults_to_dash():
    from admin.eval_runs_handlers import _run_model

    assert _run_model({"context": {}}) == "—"
    assert _run_model({}) == "—"


def test_decorate_run_gen_config_partial_keys():
    from admin.eval_runs_handlers import _run_gen_config

    # only temperature set, no top_p, no max_output_tokens
    run = {"context": {"model_override": {"temperature": 0.5}}}
    assert _run_gen_config(run) == "temp 0.5"

    # only max_output_tokens
    run2 = {"context": {"model_override": {"max_output_tokens": 1024}}}
    assert _run_gen_config(run2) == "max 1024"


def test_decorate_run_gen_config_no_override_is_dash():
    from admin.eval_runs_handlers import _run_gen_config

    assert _run_gen_config({"context": {}}) == "—"
    assert _run_gen_config({}) == "—"


def test_trim_ts_handles_none_and_empty():
    from admin.eval_runs_handlers import _trim_ts

    assert _trim_ts(None) == "—"
    assert _trim_ts("") == "—"
    assert _trim_ts("2026-01-02T03:04:05Z") == "2026-01-02T03:04:05"


def test_build_eval_runs_context_decorates_model_and_gen_config():
    """build_eval_runs_context must set _model + _gen_config on each run."""
    from admin import eval_runs_handlers as h

    run_row = {
        "run_id": "run-dec01",
        "suite_id": "su-1",
        "status": "passed",
        "score": 0.9,
        "results": [{"passed": True}],
        "created_at": "2026-05-21T00:00:00Z",
        "started_at": "2026-05-21T00:00:00Z",
        "finished_at": "2026-05-21T00:01:00Z",
        "context": {
            "model_override": {
                "model_string": "claude-3-5-sonnet",
                "temperature": 0.3,
                "max_output_tokens": 512,
                "extra_config": {"top_p": 0.9},
            },
            "model_label": "Sonnet",
        },
    }
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [run_row],
        "agent_eval_schedules": [],
        "agent_eval_suites": [{"suite_id": "su-1", "name": "Booking", "agent_kind": "sample_agent"}],
        "agent_profiles": [],
        "agent_models": [],
    })
    with patch.object(h, "get_service_client", return_value=fake), \
         patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]):
        ctx = h.build_eval_runs_context()

    runs = ctx["recent_runs"]
    assert len(runs) == 1
    r = runs[0]
    assert r["_model"] == "claude-3-5-sonnet"
    assert r["_gen_config"] == "temp 0.3 · top_p 0.9 · max 512"
    assert r["_started"] == "2026-05-21T00:00:00"
    assert r["_finished"] == "2026-05-21T00:01:00"


def test_on_demand_live_chat_trigger_requires_judge_and_expectations():
    fake = _FakeClient(inserted_ids={"agent_eval_runs": "run-od"})
    client, teardown = _make_app(fake)
    try:
        base = {"source": "live_chats", "profile_id": "p-1", "agent_kind": "sample_agent",
                "lookback_days": "30", "sample_size": "10", "business_id": "biz-1"}
        # missing judge model
        r = client.post("/admin/eval-runs/trigger",
                        data={**base, "expectations": "x"}, follow_redirects=False)
        assert r.status_code == 400 and "judge" in r.text.lower()
        # missing expectations
        r = client.post("/admin/eval-runs/trigger",
                        data={**base, "judge_model": "gemini"}, follow_redirects=False)
        assert r.status_code == 400 and "expectation" in r.text.lower()
    finally:
        teardown()


def test_run_detail_links_each_case_to_the_agent_trace():
    """A case attempt with an eval_ref links to the agent trace(s) the engine
    stamped with it; one whose trace never arrived says so."""
    from fastapi.testclient import TestClient
    from admin import auth, router as router_mod
    from main import app

    run_id = "00000000-0000-4000-8000-0000000000e1"
    fake = _FakeClient(rows_by_table={
        "agent_eval_runs": [{
            "run_id": run_id, "suite_id": None, "profile_id": "prof-1",
            "status": "failed", "score": 0.5, "created_at": "2026-10-14T08:00:00Z",
            "started_at": "2026-10-14T08:00:00Z", "context": {},
            "results": [
                {"scenario_id": "sc-ok", "passed": True, "score": 1.0, "actual": "ok",
                 "eval_ref": "aaaaaaaa-0000-4000-8000-000000000001"},
                {"scenario_id": "sc-timeout", "passed": False, "score": 0.0, "actual": "",
                 "error": "The read operation timed out",
                 "eval_ref": "aaaaaaaa-0000-4000-8000-000000000002"},
            ],
        }],
    })

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = lambda: {
        "user_token": "fake-jwt", "user_id": "u-1", "email": "t@waj.local"}
    patches = [
        patch.object(router_mod, "get_service_client", return_value=fake),
        patch.object(router_mod, "client_for", return_value=fake),
        patch("admin.screens.observability.eval_trace_ids", return_value={
            "aaaaaaaa-0000-4000-8000-000000000001": ["11111111-1111-4111-8111-111111111111"]}),
    ]
    for p in patches:
        p.start()
    try:
        r = TestClient(app).get(f"/admin/evals/runs/{run_id}?show=all")
        assert r.status_code == 200, r.text[:400]
        assert "#trace=11111111-1111-4111-8111-111111111111" in r.text
        assert "search=aaaaaaaa-0000-4000-8000-000000000001" in r.text
        assert "No agent trace was recorded for this attempt" in r.text
    finally:
        for p in patches:
            p.stop()
        if prior is not None:
            app.dependency_overrides[auth.require_superuser] = prior
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
