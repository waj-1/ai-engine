"""Tests for the prompt-version override A/B feature.

Covers four layers without touching a real DB or LLM:

1. ContextVar round-trip in `core.prompt_resolver` — `set` flips the
   resolver to a pinned version, `clear` puts it back.
2. The `prompt_override_middleware` in main.py parses the header,
   surfaces the override to the request scope, and clears on the way
   out.
3. The eval runner forwards `prompt_override` as the
   `X-WAJ-Prompt-Override` header on each endpoint call.
4. The A/B compare POST handler inserts two `agent_eval_runs` rows and
   launches both via `start_tracked_job` (so they're tracked in
   admin_jobs and cancellable) with the right `prompt_override` payloads.

Patterns mirror the rest of `tests/admin/`: a `_null_client` MagicMock
stands in for Supabase, FastAPI dependency overrides bypass auth, and
`monkeypatch` substitutes the network seams.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resolver_mock_client(prompt_id: str, version_to_id: dict):
    """Stub a Supabase client that satisfies the override lookup path.

    `agent_prompts` returns one row keyed by (agent_kind, slug); each
    `agent_prompt_versions` query for a version_number returns the
    matching prompt_version_id. The active prompt fetch (`select content
    eq prompt_version_id`) returns the content stamped on the id.
    """
    state = {"filters": {}, "table": None}

    class _Builder:
        def __init__(self, table_name):
            state["table"] = table_name
            self._filters = {}

        def select(self, *args, **kwargs):
            return self

        def eq(self, col, val):
            self._filters[col] = val
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            result = MagicMock()
            if state["table"] == "agent_prompts":
                if (
                    self._filters.get("agent_kind") == "support_bot"
                    and self._filters.get("slug") == "system"
                ):
                    result.data = [{"prompt_id": prompt_id}]
                else:
                    result.data = []
            elif state["table"] == "agent_prompt_versions":
                vn = self._filters.get("version_number")
                pid = self._filters.get("prompt_version_id")
                if vn is not None and self._filters.get("prompt_id") == prompt_id:
                    if vn in version_to_id:
                        result.data = [{"prompt_version_id": version_to_id[vn]}]
                    else:
                        result.data = []
                elif pid is not None:
                    # Active-version content fetch.
                    result.data = [{"content": f"content::{pid}"}]
                else:
                    result.data = []
            else:
                result.data = []
            return result

    def _table(name):
        return _Builder(name)

    client = MagicMock()
    client.table.side_effect = _table
    return client


# ---------------------------------------------------------------------------
# 1. ContextVar round-trip
# ---------------------------------------------------------------------------


def test_set_and_clear_override_round_trip():
    """set → resolver picks override; clear → resolver picks active."""
    from core import prompt_resolver as pr

    pr.PromptResolver.invalidate()
    fake_client = _make_resolver_mock_client(
        prompt_id="pid-1", version_to_id={7: "v7-id", 3: "v3-id"},
    )
    with patch.object(pr, "get_service_client", return_value=fake_client):
        # No override → resolver uses the active prompt_version_id we pass.
        out = pr.PromptResolver.resolve(
            prompt_version_id="active-id",
            fallback="fb",
            agent_kind="support_bot",
            prompt_key="system",
        )
        assert out == "content::active-id"

        token = pr.set_prompt_override({"support_bot/system": 7})
        try:
            pr.PromptResolver.invalidate()
            out = pr.PromptResolver.resolve(
                prompt_version_id="active-id",
                fallback="fb",
                agent_kind="support_bot",
                prompt_key="system",
            )
            # Override replaced the version_id used for content fetch.
            assert out == "content::v7-id"
        finally:
            pr.clear_prompt_override()

        pr.PromptResolver.invalidate()
        # After clear, we go back to the active path.
        out = pr.PromptResolver.resolve(
            prompt_version_id="active-id",
            fallback="fb",
            agent_kind="support_bot",
            prompt_key="system",
        )
        assert out == "content::active-id"


def test_override_falls_through_when_prompt_key_missing_on_resolve_call():
    """Resolver call without `agent_kind`/`prompt_key` ignores any override."""
    from core import prompt_resolver as pr

    pr.PromptResolver.invalidate()
    fake_client = _make_resolver_mock_client(
        prompt_id="pid-1", version_to_id={7: "v7-id"},
    )
    with patch.object(pr, "get_service_client", return_value=fake_client):
        pr.set_prompt_override({"support_bot/system": 7})
        try:
            # No agent_kind/prompt_key → resolver should NOT apply override.
            out = pr.PromptResolver.resolve(
                prompt_version_id="active-id",
                fallback="fb",
            )
            assert out == "content::active-id"
        finally:
            pr.clear_prompt_override()


def test_override_with_unknown_version_falls_back_to_active():
    """A pinned version_number that doesn't exist must NOT crash —
    resolver logs and falls through to the supplied active id."""
    from core import prompt_resolver as pr

    pr.PromptResolver.invalidate()
    fake_client = _make_resolver_mock_client(
        prompt_id="pid-1", version_to_id={1: "v1-id"},  # 999 missing
    )
    with patch.object(pr, "get_service_client", return_value=fake_client):
        pr.set_prompt_override({"support_bot/system": 999})
        try:
            out = pr.PromptResolver.resolve(
                prompt_version_id="active-id",
                fallback="fb",
                agent_kind="support_bot",
                prompt_key="system",
            )
            assert out == "content::active-id"
        finally:
            pr.clear_prompt_override()


# ---------------------------------------------------------------------------
# 2. Middleware
# ---------------------------------------------------------------------------


def test_middleware_parses_header_and_clears(client):
    """A request carrying the header sees the override during dispatch
    and cleared afterwards.

    We mount a one-shot probe route on the app to inspect the ContextVar
    from inside a real request lifecycle — confirming both the set on
    entry and the clear on exit work end-to-end through Starlette's
    middleware stack.
    """
    from core import prompt_resolver as pr
    from main import app

    captured = {}

    @app.get("/__probe_prompt_override__")
    def _probe():
        captured["seen"] = pr.get_prompt_override()
        return {"ok": True}

    try:
        # No header → no override visible.
        r = client.get("/__probe_prompt_override__")
        assert r.status_code == 200, r.text
        assert captured.get("seen") is None
        # Outer scope was untouched.
        assert pr.get_prompt_override() is None

        # Non-operator callers: the header is IGNORED (the override is
        # operator-gated so a public endpoint can't be steered). See
        # tests/test_override_auth_gate.py for the gate itself.
        captured.clear()
        r = client.get(
            "/__probe_prompt_override__",
            headers={"X-WAJ-Prompt-Override": json.dumps({"support_bot/system": 7})},
        )
        assert r.status_code == 200, r.text
        assert captured.get("seen") is None

        # Operator callers: the override is applied during dispatch and cleared
        # afterwards. Patch the operator check (its verified-JWT + superuser
        # lookup is covered separately) to exercise the set/clear lifecycle.
        captured.clear()
        with patch("main._request_is_operator", return_value=True):
            r = client.get(
                "/__probe_prompt_override__",
                headers={"X-WAJ-Prompt-Override": json.dumps({"support_bot/system": 7})},
            )
        assert r.status_code == 200, r.text
        assert captured.get("seen") == {"support_bot/system": 7}
        # Cleared after the response returned.
        assert pr.get_prompt_override() is None
    finally:
        # Drop the probe route so the smoke session client stays clean.
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/__probe_prompt_override__"
        ]


def test_middleware_ignores_malformed_header(client):
    """Non-JSON, non-dict, and non-int values are dropped silently."""
    from core import prompt_resolver as pr
    from main import app

    captured = {}

    @app.get("/__probe_prompt_override_bad__")
    def _probe():
        captured["seen"] = pr.get_prompt_override()
        return {"ok": True}

    try:
        # Non-JSON.
        r = client.get(
            "/__probe_prompt_override_bad__",
            headers={"X-WAJ-Prompt-Override": "not-json{"},
        )
        assert r.status_code == 200
        assert captured.get("seen") is None

        # Valid JSON but a list, not a dict.
        captured.clear()
        r = client.get(
            "/__probe_prompt_override_bad__",
            headers={"X-WAJ-Prompt-Override": json.dumps([1, 2, 3])},
        )
        assert r.status_code == 200
        assert captured.get("seen") is None

        # Dict but values aren't ints — coerced/dropped.
        captured.clear()
        r = client.get(
            "/__probe_prompt_override_bad__",
            headers={"X-WAJ-Prompt-Override": json.dumps({"support_bot/system": "abc"})},
        )
        assert r.status_code == 200
        # Empty cleaned dict → no override set at all.
        assert captured.get("seen") is None
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/__probe_prompt_override_bad__"
        ]


# ---------------------------------------------------------------------------
# 3. Eval runner forwards header
# ---------------------------------------------------------------------------


def test_invoke_endpoint_forwards_prompt_override_header(monkeypatch):
    """The runner sets `X-WAJ-Prompt-Override` exactly when non-empty."""
    from admin import evals

    seen_headers = []

    class _FakeStreamResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def iter_text(self):
            # Mirror the agent SSE wire format: one `data:` frame per event.
            yield 'data: {"type": "answer", "content": "hi"}\n\n'

    class _FakeClient:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def stream(self, method, url, json=None, headers=None):
            # The runner now drains the SSE stream via httpx's streaming API
            # rather than a buffered .post().text read.
            seen_headers.append(dict(headers or {}))
            return _FakeStreamResp()

    monkeypatch.setattr(evals.httpx, "Client", _FakeClient)

    # With override.
    out, _, status, _ = evals._invoke_endpoint(
        base_url="http://agent",
        endpoint="/support/chat/stream",
        scenario={"id": "s", "question": "hi", "business_id": "b"},
        run_context={},
        user_token="jwt-abc",
        prompt_override={"support_bot/system": 7},
    )
    assert status == 200
    assert seen_headers[-1].get("X-WAJ-Prompt-Override") == '{"support_bot/system": 7}'
    assert seen_headers[-1].get("Authorization") == "Bearer jwt-abc"

    # Without override → header absent.
    evals._invoke_endpoint(
        base_url="http://agent",
        endpoint="/support/chat/stream",
        scenario={"id": "s", "question": "hi", "business_id": "b"},
        run_context={},
        user_token="jwt-abc",
        prompt_override=None,
    )
    assert "X-WAJ-Prompt-Override" not in seen_headers[-1]

    # Empty dict → header absent (treated as "no override").
    evals._invoke_endpoint(
        base_url="http://agent",
        endpoint="/support/chat/stream",
        scenario={"id": "s", "question": "hi", "business_id": "b"},
        run_context={},
        user_token="jwt-abc",
        prompt_override={},
    )
    assert "X-WAJ-Prompt-Override" not in seen_headers[-1]


# ---------------------------------------------------------------------------
# 4. A/B handler creates two runs
# ---------------------------------------------------------------------------


def _ab_db_factory(prompt_row):
    """Mock client that returns the prompt row on agent_prompts lookup and
    surfaces inserted run rows so the test can assert their context."""
    inserts: list[dict] = []

    class _Insert:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            run_id = f"run-{len(inserts) + 1}"
            inserts.append(self._payload)
            r = MagicMock()
            r.data = [{"run_id": run_id, **self._payload}]
            return r

    class _Builder:
        def __init__(self, table_name):
            self._table = table_name
            self._filters = {}

        def select(self, *a, **k):
            return self

        def eq(self, col, val):
            self._filters[col] = val
            return self

        def order(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def insert(self, payload):
            return _Insert(payload)

        def execute(self):
            result = MagicMock()
            if self._table == "agent_prompts":
                result.data = [prompt_row]
            else:
                result.data = []
            return result

    client = MagicMock()
    client.table.side_effect = lambda name: _Builder(name)
    return client, inserts


def test_prompt_compare_post_launches_two_runs(client):
    """Submitting the compare form inserts two runs and redirects to the
    side-by-side view with both run_ids in the query string."""
    from admin import prompt_handlers

    prompt_row = {
        "prompt_id": "pid-1",
        "agent_kind": "support_bot",
        "slug": "system",
    }
    db, inserts = _ab_db_factory(prompt_row)

    fired: list[dict] = []

    def _fake_start(fn, **kwargs):
        fired.append({"fn": fn, "kwargs": kwargs})
        return f"job-{len(fired)}"

    with patch.object(prompt_handlers, "get_service_client", return_value=db), \
         patch.object(prompt_handlers, "start_tracked_job", side_effect=_fake_start), \
         patch.object(prompt_handlers, "record_change"):
        r = client.post(
            "/admin/prompts/pid-1/compare",
            data={
                "suite_id": "suite-1",
                "profile_id": "prof-1",
                "version_a": "3",
                "version_b": "7",
                "ctx_business_id": "biz-1",
            },
            follow_redirects=False,
        )

    assert r.status_code in (302, 303), r.text[:400]
    location = r.headers["location"]
    assert location.startswith("/admin/evals/compare?a=")
    assert "b=" in location

    # Two rows inserted, both with the right shape.
    assert len(inserts) == 2
    a, b = inserts
    assert a["suite_id"] == "suite-1"
    assert a["profile_id"] == "prof-1"
    assert a["status"] == "queued"
    assert a["context"]["_prompt_override"] == {"support_bot/system": 3}
    assert a["context"]["business_id"] == "biz-1"
    assert b["context"]["_prompt_override"] == {"support_bot/system": 7}

    # Both A/B runs are launched via the tracked-job path (so they land in
    # admin_jobs and are cooperatively cancellable), each carrying the
    # matching override kwarg + eval_run kind.
    assert len(fired) == 2
    for f in fired:
        assert f["fn"] is prompt_handlers.run_eval_suite_sync
        assert f["kwargs"]["kind"] == "eval_run"
        assert f["kwargs"]["related_entity_type"] == "agent_eval_runs"
        # run_id and related_entity_id point at the same inserted run row.
        assert f["kwargs"]["related_entity_id"] == f["kwargs"]["run_id"]
    overrides_fired = [f["kwargs"].get("prompt_override") for f in fired]
    assert {"support_bot/system": 3} in overrides_fired
    assert {"support_bot/system": 7} in overrides_fired


def test_prompt_compare_post_allows_same_version_on_both_sides(client):
    """A==B is permitted (operator may want to test the A/B plumbing
    itself) — the handler still launches two runs."""
    from admin import prompt_handlers

    prompt_row = {"prompt_id": "pid-1", "agent_kind": "support_bot", "slug": "system"}
    db, inserts = _ab_db_factory(prompt_row)

    with patch.object(prompt_handlers, "get_service_client", return_value=db), \
         patch.object(prompt_handlers, "start_tracked_job", return_value="job-x"), \
         patch.object(prompt_handlers, "record_change"):
        r = client.post(
            "/admin/prompts/pid-1/compare",
            data={
                "suite_id": "suite-1",
                "profile_id": "prof-1",
                "version_a": "5",
                "version_b": "5",
            },
            follow_redirects=False,
        )

    assert r.status_code in (302, 303), r.text[:400]
    assert len(inserts) == 2
    assert (
        inserts[0]["context"]["_prompt_override"]
        == inserts[1]["context"]["_prompt_override"]
        == {"support_bot/system": 5}
    )


def test_prompt_compare_get_renders_form(client):
    """The GET form loads suites/versions/profiles/businesses and
    renders the new 'Launch A/B' button."""
    from admin import prompt_handlers

    prompt_row = {"prompt_id": "pid-1", "agent_kind": "support_bot", "slug": "system"}

    class _Builder:
        def __init__(self, name):
            self._name = name

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def order(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            r = MagicMock()
            if self._name == "agent_prompts":
                r.data = [prompt_row]
            elif self._name == "agent_eval_suites":
                r.data = [{"suite_id": "suite-1", "name": "support-smoke",
                           "pass_threshold": 0.8, "is_active": True}]
            elif self._name == "agent_prompt_versions":
                r.data = [
                    {"version_number": 2, "is_active": True, "created_at": "2026-05-20"},
                    {"version_number": 1, "is_active": False, "created_at": "2026-05-19"},
                ]
            elif self._name == "agent_profiles":
                r.data = [{"profile_id": "prof-1", "slug": "support_default",
                           "environment": "dev", "endpoint_path": "/support/chat/stream"}]
            else:
                r.data = []
            return r

    db = MagicMock()
    db.table.side_effect = lambda name: _Builder(name)

    with patch.object(prompt_handlers, "get_service_client", return_value=db), \
         patch("admin._business_picker.fetch_businesses_for_picker", return_value=[
             {"id": "biz-1", "name": "Orchard", "subdomain": "orchard"},
         ]):
        r = client.get("/admin/prompts/pid-1/compare")

    assert r.status_code == 200, r.text[:300]
    body = r.text
    assert "Launch A/B" in body
    assert "Version A" in body
    assert "Version B" in body
    assert "support-smoke" in body
    assert "support_default" in body
