"""Transcript must read the conversation from the env the TRACE belongs to.

The explorer lists one shared telemetry store for both envs, but
ai_chat_messages live in per-env branch DBs. The lookup used the portal's
selected env (cookie, default staging) — so a prod conversation rendered as
the single-span telemetry fallback ("only one message"). The fix reads
waj.env off the trace and tries that env first, then falls back.
"""
from __future__ import annotations

from unittest.mock import patch

from admin.screens import observability as obs


def test_env_from_spans_reads_root_attr():
    assert obs._env_from_spans([{"waj_env": ""}, {"waj_env": "prod"}]) == "prod"
    assert obs._env_from_spans([{}]) is None


def test_transcript_prefers_trace_env_over_selected_env():
    spans = [{
        "span_id": "s1", "name": "sse", "span_type": 0,
        "start_time": "2026-06-11T10:00:00", "input": "", "output": "",
        "has_error": 0, "waj_session_id": "sess-1", "waj_env": "prod",
    }]
    calls = []

    def fake_persisted(session_id, env):
        calls.append(env)
        if env == "prod":
            return [
                {"role": "user", "content": "q1", "ts": "t1", "source": "persisted", "tokens": None},
                {"role": "assistant", "content": "a1", "ts": "t2", "source": "persisted", "tokens": 5},
                {"role": "user", "content": "q2", "ts": "t3", "source": "persisted", "tokens": None},
                {"role": "assistant", "content": "a2", "ts": "t4", "source": "persisted", "tokens": 7},
            ]
        return []

    with patch.object(obs, "ch_get_trace", return_value=None), \
         patch.object(obs, "_clickhouse_spans", return_value=spans), \
         patch.object(obs, "_persisted_conversation", side_effect=fake_persisted):
        out = obs.load_transcript_json("T1", env="staging")

    # trace's own env tried first and wins — the FULL conversation renders,
    # not the one-turn telemetry fallback.
    assert calls[0] == "prod"
    assert len(out["turns"]) == 4
    assert out.get("notice") in (None, "")


def test_transcript_falls_back_across_envs_when_trace_env_missing():
    spans = [{
        "span_id": "s1", "name": "sse", "span_type": 0,
        "start_time": "2026-06-11T10:00:00", "input": "", "output": "",
        "has_error": 0, "waj_session_id": "sess-2", "waj_env": "",
    }]

    def fake_persisted(session_id, env):
        return ([{"role": "user", "content": "hi", "ts": "t", "source": "persisted", "tokens": None}]
                if env == "prod" else [])

    with patch.object(obs, "ch_get_trace", return_value=None), \
         patch.object(obs, "_clickhouse_spans", return_value=spans), \
         patch.object(obs, "_persisted_conversation", side_effect=fake_persisted):
        out = obs.load_transcript_json("T2", env="staging")
    assert any(t["content"] == "hi" for t in out["turns"])
