"""Conversations list noise, eval-run ↔ trace correlation and the slow-turn
rule (2026-10-14: 62 of the newest 100 prod rows were the admin MCP's own
calls, eval traffic was unlabelled, and timed-out turns never needed
attention)."""
from __future__ import annotations

import contextvars
from types import SimpleNamespace

import pytest


def _req(headers):
    return SimpleNamespace(headers=headers, state=SimpleNamespace())


class TestEvalTraceAttrs:
    def test_uuid_headers_are_tagged(self):
        from core.channel_resolver import eval_trace_attrs

        out = eval_trace_attrs(_req({"X-WAJ-Eval-Run": "11111111-2222-3333-4444-555555555555",
                                     "X-WAJ-Eval-Ref": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}))
        assert out == {"waj.eval.run_id": "11111111-2222-3333-4444-555555555555",
                       "waj.eval.ref": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}

    @pytest.mark.parametrize("value", ["", "not-a-uuid", "1' OR '1'='1", "x" * 36])
    def test_anything_else_is_dropped(self, value):
        from core.channel_resolver import eval_trace_attrs

        assert eval_trace_attrs(_req({"X-WAJ-Eval-Run": value})) == {}

    def test_root_span_attrs_carry_them(self):
        from core.channel_resolver import root_span_attrs

        attrs = root_span_attrs(_req({"X-WAJ-Eval-Ref": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}),
                                agent_kind="business")
        assert attrs["waj.eval.ref"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert "waj.eval.run_id" not in attrs


class TestRunnerHeaders:
    def test_override_headers_forward_the_correlation(self):
        from admin import evals

        def _inside():
            evals._EVAL_RUN_ID.set("run-1")
            evals._EVAL_REF.set("ref-1")
            return evals._override_headers(None, None, None)

        headers = contextvars.copy_context().run(_inside)
        assert headers == {"X-WAJ-Eval-Run": "run-1", "X-WAJ-Eval-Ref": "ref-1"}

    def test_no_headers_outside_a_run(self):
        from admin import evals

        assert contextvars.copy_context().run(
            lambda: evals._override_headers(None, None, None)) == {}

    def test_each_scenario_records_its_ref_and_sends_it(self, monkeypatch):
        from admin import evals

        sent = {}

        def fake_invoke(**kw):
            sent.update(evals._override_headers(None, None, None))
            raise TimeoutError("The read operation timed out")

        errors = []
        monkeypatch.setattr(evals, "_invoke_endpoint", fake_invoke)
        monkeypatch.setattr(evals, "_mark_span_error", lambda m: errors.append(m))

        def _inside():
            evals._EVAL_RUN_ID.set("run-9")
            return evals._run_scenario({"id": "s1", "question": "hi"}, profile_id="p",
                                       base_url="http://x", endpoint="/chat", judge=None,
                                       pass_threshold=0.7)

        result = contextvars.copy_context().run(_inside)
        assert result["eval_ref"] and sent["X-WAJ-Eval-Ref"] == result["eval_ref"]
        assert sent["X-WAJ-Eval-Run"] == "run-9"
        # A timed-out case is an error in its trace, not only in the results.
        assert errors and "timed out" in errors[0]


class TestListNoise:
    def _sql(self, include_background):
        from admin.screens.observability import _trace_list_sql

        return _trace_list_sql(env="prod", include_background=include_background, status=None)[0]

    def test_default_list_drops_mcp_eval_and_bare_llm_traffic(self):
        sql = self._sql(False)
        for token in ("ResponseStreamWriter", "openai.embeddings",
                      "gemini.generate_content_stream", "eval_suite::", "waj.eval.run_id"):
            assert token in sql

    def test_background_view_keeps_everything(self):
        sql = self._sql(True)
        for token in ("ResponseStreamWriter", "eval_suite::", "waj.eval.run_id"):
            assert token not in sql

    def test_overview_counts_the_same_traffic(self):
        from admin.screens.overview import _trace_subquery, outcome_sql

        sub = _trace_subquery("hours")
        assert "ResponseStreamWriter" in sub and "waj.eval.run_id" in sub
        assert "t.duration > 60" in outcome_sql("prod")[0]

    def test_eval_traces_are_marked_test_traffic(self):
        from admin.screens.observability import _annotate_trace_channel_meta

        rows = [{"metadata": '{"waj.agent_kind":"support_bot","waj.eval.run_id":"r"}'},
                {"metadata": "", "topSpanName": "eval_suite::checklist"},
                {"metadata": '{"waj.agent_kind":"sample_agent"}'}]
        _annotate_trace_channel_meta(rows)
        assert [r["simulated"] for r in rows] == [True, True, False]


class TestSlowTurns:
    def test_a_slow_turn_needs_attention(self):
        from admin.screens.conversations import _group_outcome
        from admin.screens.observability import group_traces_by_session

        fast = {"id": "a", "session_id": "s", "duration_ms": 4_000}
        slow = {"id": "b", "session_id": "s", "duration_ms": 130_000}
        assert _group_outcome(group_traces_by_session([fast])[0]) == "ok"
        assert _group_outcome(group_traces_by_session([slow, fast])[0]) == "attention"

    def test_trace_duration(self):
        from admin.screens.observability import _trace_duration_sec

        assert _trace_duration_sec({"startTime": "2026-10-14T08:00:00Z",
                                    "endTime": "2026-10-14T08:02:10Z"}) == 130
        assert _trace_duration_sec(None) is None


class TestEvalTraceIds:
    def test_groups_trace_ids_by_ref(self, monkeypatch):
        from admin.screens import observability as obs

        seen = {}

        def fake_query(sql, params):
            seen.update(params)
            return [{"id": "t1", "ref": "r1"}, {"id": "t2", "ref": "r1"},
                    {"id": "t3", "ref": "r2"}, {"id": "t4", "ref": ""}]

        monkeypatch.setattr(obs, "_ch_trace_query", fake_query)
        out = obs.eval_trace_ids("11111111-2222-3333-4444-555555555555", "2026-10-14T08:00:00Z")
        assert out == {"r1": ["t1", "t2"], "r2": ["t3"]}
        assert seen["run_id"] == "11111111-2222-3333-4444-555555555555"
        assert seen["hours"] >= 1

    def test_outage_is_none_not_empty(self, monkeypatch):
        from admin.screens import observability as obs

        def boom(*_a):
            raise RuntimeError("ClickHouse down")

        monkeypatch.setattr(obs, "_ch_trace_query", boom)
        assert obs.eval_trace_ids("11111111-2222-3333-4444-555555555555", None) is None
