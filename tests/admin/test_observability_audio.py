"""Tests for the observability explorer's Audio tab backend.

Covers `load_voice_audio_json` (trace → session → recordings resolution) and
`load_recording_wav` (chunk reassembly), with ClickHouse and the Supabase client
stubbed — same posture as the other explorer tests.
"""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

SESSION_ID = "33333333-3333-3333-3333-333333333333"
CALL_ID = "44444444-4444-4444-4444-444444444444"


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filters = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return _FakeResult(self._rows)


class _FakeClient:
    """Maps table name → rows to return (each call gets a fresh query)."""

    def __init__(self, tables):
        self._tables = tables
        self.queries = []

    def table(self, name):
        q = _FakeQuery(self._tables.get(name, []))
        self.queries.append((name, q))
        return q


def _recording_row(**over):
    row = {
        "recording_id": "rec-1",
        "call_id": CALL_ID,
        "provider": "azure_voice_live",
        "simulated": False,
        "sample_rate": 16000,
        "duration_ms": 12_000,
        "size_bytes": 1234,
        "status": "complete",
        "started_at": "2026-08-08T10:00:00+00:00",
    }
    row.update(over)
    return row


# ------------------------------------------------------------- audio.json


def _load_audio(trace_meta, recordings, spans=None):
    from admin.screens import observability as obs

    # `metadata` arrives from ClickHouse as a JSON string on real rows; a dict is
    # accepted too (see `_trace_meta_attrs`), which keeps these cases terse.
    trace = {"id": "t-1", "metadata": trace_meta, "sessionId": None}
    client = _FakeClient({"ai_voice_recordings": recordings})
    with patch.object(obs, "ch_get_trace", return_value=trace), \
         patch.object(obs, "_clickhouse_spans",
                      return_value=spans if spans is not None
                      else [{"waj_session_id": SESSION_ID, "waj_env": "prod"}]), \
         patch.object(obs, "_recordings_client", return_value=client):
        return obs.load_voice_audio_json("t-1", env="prod"), client


def test_audio_json_lists_session_recordings_with_playback_urls():
    data, _client = _load_audio({}, [_recording_row()])
    assert data["session_id"] == SESSION_ID
    assert len(data["recordings"]) == 1
    rec = data["recordings"][0]
    assert rec["status"] == "complete"
    assert rec["url"].startswith("/admin/observability/explorer/recording/rec-1.wav")
    assert data["notice"] is None


def test_audio_json_flags_the_selected_traces_call():
    data, _ = _load_audio(
        {"waj.voice.call_id": CALL_ID},
        [_recording_row(), _recording_row(recording_id="rec-2", call_id="other")],
    )
    flags = {r["id"]: r["is_current_trace"] for r in data["recordings"]}
    assert flags == {"rec-1": True, "rec-2": False}


def test_audio_json_queries_by_the_coerced_session_id():
    """The trace carries the RAW session id; recordings are stored under the
    coerced UUID — same mapping as the transcript reader."""
    from core.chat_persistence import coerce_session_id

    raw = "voice:orchard:tenant-1"
    data, client = _load_audio({}, [], spans=[{"waj_session_id": raw}])
    assert data["recordings"] == []
    queried = [q.filters.get("session_id")
               for name, q in client.queries if name == "ai_voice_recordings"]
    assert coerce_session_id(raw) in queried
    assert raw not in queried


def test_audio_json_without_a_session_returns_a_notice():
    data, _ = _load_audio({}, [], spans=[])
    assert data["recordings"] == []
    assert "No chat session" in data["notice"]


def test_audio_json_empty_recording_carries_no_url():
    data, _ = _load_audio({}, [_recording_row(status="empty")])
    assert data["recordings"][0]["url"] is None


# --------------------------------------------------------------- .wav bytes


def test_recording_wav_reassembles_chunks_in_seq_order():
    from admin.screens import observability as obs

    wav = b"RIFF" + b"x" * 100
    chunks = [
        {"seq": 1, "audio_b64": base64.b64encode(wav[50:]).decode()},
        {"seq": 0, "audio_b64": base64.b64encode(wav[:50]).decode()},
    ]
    client = _FakeClient({
        "ai_voice_recordings": [_recording_row(chunk_count=2)],
        "ai_voice_recording_chunks": chunks,
    })
    with patch.object(obs, "_recordings_client", return_value=client):
        got, row = obs.load_recording_wav("rec-1")
    assert got == wav
    assert row["status"] == "complete"


def test_recording_wav_refuses_a_torn_file():
    """Fewer chunks than the row declares = a torn read (or a failed
    upload's leftovers) — serving it would cache a truncated call in the
    operator's browser for a day."""
    from admin.screens import observability as obs

    client = _FakeClient({
        "ai_voice_recordings": [_recording_row(chunk_count=3)],
        "ai_voice_recording_chunks": [
            {"seq": 0, "audio_b64": base64.b64encode(b"RIFFxxxx").decode()},
        ],
    })
    with patch.object(obs, "_recordings_client", return_value=client):
        assert obs.load_recording_wav("rec-1") == (None, None)


def test_recording_wav_missing_or_incomplete_returns_none():
    from admin.screens import observability as obs

    with patch.object(obs, "_recordings_client",
                      return_value=_FakeClient({"ai_voice_recordings": []})):
        assert obs.load_recording_wav("nope") == (None, None)

    failed = _FakeClient({"ai_voice_recordings": [_recording_row(status="failed")]})
    with patch.object(obs, "_recordings_client", return_value=failed):
        assert obs.load_recording_wav("rec-1") == (None, None)


def test_recording_wav_survives_a_broken_db():
    from admin.screens import observability as obs

    broken = MagicMock()
    broken.table.side_effect = RuntimeError("supabase down")
    with patch.object(obs, "_recordings_client", return_value=broken):
        assert obs.load_recording_wav("rec-1") == (None, None)


# ------------------------------------------------------------ route wiring


def test_explorer_audio_routes_registered_before_the_catch_all():
    """New /observability/explorer/* paths must beat the {trace_id} catch-all
    or Starlette routes them into the trace-detail handler."""
    import admin.observability_explorer_handlers  # noqa: F401 - registers routes
    from admin.router import router

    paths = [getattr(r, "path", "") for r in router.routes]
    audio_json = paths.index("/admin/observability/explorer/trace/{trace_id}/audio.json")
    wav = paths.index("/admin/observability/explorer/recording/{recording_id}.wav")
    catch_all = paths.index("/admin/observability/{trace_id}")
    assert audio_json < catch_all
    assert wav < catch_all


def test_explorer_template_carries_the_audio_tab():
    tpl = Path("admin/templates/observability_explorer.html").read_text(encoding="utf-8")
    assert 'id="ox-view-audio"' in tpl
    assert 'id="ox-audio"' in tpl
    assert "audio.json" in tpl
    assert "loadAudio" in tpl
    # Error responses must not be cached as "no recordings", and a
    # mid-upload row renders as in-progress, not as a failure.
    assert "resp.ok" in tpl
    assert "uploading" in tpl


# ------------------------------------------------- simulated-session tagging


def _trace(meta: dict) -> dict:
    import json as _json
    return {"id": "t-1", "metadata": _json.dumps(meta)}


def test_annotate_marks_simulated_traces():
    """Bot-driven voice calls stamp waj.voice.simulated on their spans; the
    list annotation must surface it so sim sessions get their chip."""
    from admin.screens.observability import _annotate_trace_channel_meta

    traces = [
        _trace({"waj.agent_kind": "orchard", "waj.voice.simulated": "true"}),
        _trace({"waj.agent_kind": "orchard"}),
    ]
    _annotate_trace_channel_meta(traces)
    assert traces[0]["simulated"] is True
    assert traces[1]["simulated"] is False


def test_session_group_inherits_the_simulated_flag():
    from admin.screens.observability import (
        _annotate_trace_channel_meta,
        group_traces_by_session,
    )

    traces = [
        _trace({"waj.session_id": "s-1", "waj.voice.simulated": "true"}),
        _trace({"waj.session_id": "s-2"}),
    ]
    _annotate_trace_channel_meta(traces)
    groups = {g["session_id"]: g for g in group_traces_by_session(traces)}
    assert groups["s-1"]["simulated"] is True
    assert groups["s-2"]["simulated"] is False


def test_trace_list_template_renders_the_sim_chip():
    tpl = Path("admin/templates/observability_explorer.html").read_text(encoding="utf-8")
    # One row per conversation now; a simulated call reads "Test call".
    assert "g.simulated" in tpl
    assert "Test call" in tpl


async def test_live_mode_eval_runs_self_declare_sim(monkeypatch):
    """The live-mode voice eval driver must tag its calls ?simRun=1 — the
    skip path (no TTS key) returns the built URL, which is a cheap seam to
    pin the flag without opening a socket."""
    from admin.voice_eval_driver import run_voice_eval_scenario

    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    result = await run_voice_eval_scenario(
        {"slug": "s", "title": "t"},
        {"agent_kind": "orchard", "mode": "live"},
        caller_llm=None,
        target="ws://example.test",
    )
    assert result["status"] == "skipped"
    assert "simRun=1" in result["target"]
    assert "provider=simulated" not in result["target"]


def test_audio_json_names_the_provider_in_plain_words():
    data, _ = _load_audio({}, [_recording_row(provider="google_genai"),
                               _recording_row(recording_id="rec-2", provider="azure_voice_live"),
                               _recording_row(recording_id="rec-3", provider="some_new_slug")])
    names = {r["id"]: r["provider"] for r in data["recordings"]}
    assert names == {"rec-1": "Gemini Live", "rec-2": "Azure Voice Live", "rec-3": None}
