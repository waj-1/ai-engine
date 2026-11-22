"""Unit tests for voice/live/recorder.py — per-call audio capture.

The interesting behaviour is TIME, so every placement test drives the
recorder's clock explicitly (the module-level `time` name is swapped for a
fake — patching `time.monotonic` globally would break the event loop).
"""
from __future__ import annotations

import io
import wave
from array import array

import pytest

import voice.live.recorder as recorder_mod
from voice.live.recorder import VoiceCallRecorder, _prune_old


RATE = 16000


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(recorder_mod, "time", c)
    return c


def _rec(**overrides) -> VoiceCallRecorder:
    kw = dict(
        call_id="11111111-1111-1111-1111-111111111111",
        session_id="22222222-2222-2222-2222-222222222222",
        agent_kind="sample_voice",
        business_id=None,
        provider="azure_voice_live",
        simulated=False,
        input_sample_rate=RATE,
        output_sample_rate=RATE,
        sample_rate=RATE,
        max_sec=60,
    )
    kw.update(overrides)
    return VoiceCallRecorder(**kw)


def _pcm(seconds: float, value: int = 1000) -> bytes:
    return array("h", [value] * int(seconds * RATE)).tobytes()


def _parse_wav(wav_bytes: bytes):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return {
            "channels": w.getnchannels(),
            "rate": w.getframerate(),
            "frames": w.getnframes(),
            "pcm": w.readframes(w.getnframes()),
        }


# ---------------------------------------------------------------- placement


def test_outbound_burst_is_placed_at_wall_clock_not_appended(clock):
    """Provider audio arrives far faster than realtime — a 2s answer landing
    at t=1 must start at 1s in the recording, with silence before it."""
    rec = _rec()
    clock.t = 1.0
    rec.add_outbound(_pcm(2.0, value=2000))
    wav, duration_ms = rec._assemble_wav()
    assert duration_ms == 3000
    parsed = _parse_wav(wav)
    assert parsed["channels"] == 2
    assert parsed["rate"] == RATE
    samples = array("h")
    samples.frombytes(parsed["pcm"])
    right = samples[1::2]
    # First second: silence. After: the answer.
    assert right[int(0.5 * RATE)] == 0
    assert right[int(1.5 * RATE)] == 2000


def test_intra_track_gap_is_padded_with_silence(clock):
    rec = _rec()
    rec.add_inbound(_pcm(0.5))
    clock.t = 2.0
    rec.add_inbound(_pcm(0.5))
    wav, duration_ms = rec._assemble_wav()
    assert duration_ms == 2500
    samples = array("h")
    samples.frombytes(_parse_wav(wav)["pcm"])
    left = samples[0::2]
    assert left[int(0.25 * RATE)] == 1000   # first chunk
    assert left[int(1.0 * RATE)] == 0       # the gap
    assert left[int(2.25 * RATE)] == 1000   # second chunk


def test_burst_chunks_stay_contiguous_once_ahead_of_wall_clock(clock):
    """Two chunks of one fast burst: the second appends right after the
    first even though the wall clock has barely moved."""
    rec = _rec()
    rec.add_outbound(_pcm(1.0, value=1))
    clock.t = 0.1
    rec.add_outbound(_pcm(1.0, value=2))
    wav, duration_ms = rec._assemble_wav()
    assert duration_ms == 2000
    samples = array("h")
    samples.frombytes(_parse_wav(wav)["pcm"])
    right = samples[1::2]
    assert right[int(1.5 * RATE)] == 2      # no silence spliced between


def test_playback_flush_cuts_unheard_audio(clock):
    """A barge-in flush discards everything the caller had not heard yet —
    the recording must cut the same tail, or it plays answers over the
    interruption that killed them."""
    rec = _rec()
    rec.add_outbound(_pcm(5.0, value=3000))     # 5s queued instantly
    clock.t = 1.0
    rec.mark_playback_flush()                   # only 1s was ever heard
    rec.add_outbound(_pcm(1.0, value=-3000))    # the next answer
    wav, duration_ms = rec._assemble_wav()
    assert duration_ms == 2000
    samples = array("h")
    samples.frombytes(_parse_wav(wav)["pcm"])
    right = samples[1::2]
    assert right[int(0.5 * RATE)] == 3000
    assert right[int(1.5 * RATE)] == -3000


def test_per_call_cap_bounds_the_recording(clock):
    rec = _rec(max_sec=2)
    rec.add_inbound(_pcm(5.0))
    _wav, duration_ms = rec._assemble_wav()
    assert duration_ms == 2000


def test_wire_rate_is_resampled_to_recording_rate(clock):
    """24kHz wire audio (azure) lands at the 16kHz recording rate with the
    same duration."""
    rec = _rec(input_sample_rate=24000)
    one_sec_at_24k = array("h", [500] * 24000).tobytes()
    rec.add_inbound(one_sec_at_24k)
    assert abs(rec._in.fed_ms - 1000) < 5
    _wav, duration_ms = rec._assemble_wav()
    assert abs(duration_ms - 1000) < 5


def test_channels_are_caller_left_assistant_right(clock):
    rec = _rec()
    rec.add_inbound(_pcm(1.0, value=1111))
    rec.add_outbound(_pcm(1.0, value=-2222))
    wav, _ = rec._assemble_wav()
    samples = array("h")
    samples.frombytes(_parse_wav(wav)["pcm"])
    assert samples[0::2][RATE // 2] == 1111
    assert samples[1::2][RATE // 2] == -2222


def test_capture_failure_disables_without_raising(clock):
    rec = _rec()
    rec._in.file.close()  # force the next write to blow up
    rec.add_inbound(_pcm(0.1))  # must not raise
    assert rec.disabled
    rec.add_inbound(_pcm(0.1))  # and stays inert


# ---------------------------------------------------------------- persistence


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, client, name):
        self._client = client
        self._name = name
        self._filters = {}
        self._op = None
        self._payload = None

    # chain plumbing -------------------------------------------------------
    def select(self, *_a, **_k):
        self._op = self._op or "select"
        return self

    def insert(self, row):
        self._op, self._payload = "insert", row
        return self

    def update(self, patch):
        self._op, self._payload = "update", patch
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def lt(self, col, val):
        self._filters[f"{col}__lt"] = val
        return self

    def in_(self, col, vals):
        self._filters[f"{col}__in"] = list(vals)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, _n):
        return self

    def execute(self):
        rows = self._client.tables.setdefault(self._name, [])
        if self._op == "insert":
            row = dict(self._payload)
            if self._name == "ai_voice_recordings":
                row.setdefault("recording_id", f"rec-{len(rows) + 1}")
            rows.append(row)
            # Snapshot: the stored dict mutates on update; the log must keep
            # what the insert actually carried.
            self._client.log.append(("insert", self._name, dict(row)))
            return _FakeResult([row])
        if self._op == "update":
            # Apply the patch to matching rows so tests can read final state.
            for r in rows:
                if all(r.get(k) == v for k, v in self._filters.items()):
                    r.update(self._payload)
            self._client.log.append(("update", self._name, self._payload, dict(self._filters)))
            return _FakeResult([])
        if self._op == "delete":
            self._client.log.append(("delete", self._name, dict(self._filters)))
            return _FakeResult([])
        # select — only the shapes the recorder uses
        out = rows
        if "created_at__lt" in self._filters:
            out = [r for r in rows if r.get("created_at", "9999") < self._filters["created_at__lt"]]
        return _FakeResult(list(out))


class _FakeClient:
    def __init__(self):
        self.tables = {}
        self.log = []

    def table(self, name):
        return _FakeTable(self, name)


class _InsertsFailClient(_FakeClient):
    """Selects/deletes work; every insert blows up — models a persist outage
    that must not also halt the retention sweep."""

    def table(self, name):
        t = _FakeTable(self, name)

        def _boom(_row):
            raise RuntimeError("insert refused")

        t.insert = _boom  # type: ignore[method-assign]
        return t


async def test_finalize_persists_row_and_chunks(clock):
    rec = _rec()
    rec.add_inbound(_pcm(1.0))
    rec.add_outbound(_pcm(1.0))
    client = _FakeClient()
    row = await rec.finalize_and_store(client=client)
    assert row is not None and row["status"] == "complete"
    stored = client.tables["ai_voice_recordings"][0]
    assert stored["call_id"] == rec.call_id
    assert stored["session_id"] == rec.session_id
    assert stored["channels"] == 2
    assert stored["duration_ms"] == 1000
    assert stored["status"] == "complete"  # flipped after the chunks landed
    chunks = client.tables["ai_voice_recording_chunks"]
    assert stored["chunk_count"] == len(chunks) >= 1
    import base64
    joined = b"".join(base64.b64decode(c["audio_b64"]) for c in chunks)
    assert joined[:4] == b"RIFF"
    assert len(joined) == stored["size_bytes"]
    parsed = _parse_wav(joined)
    assert parsed["channels"] == 2 and parsed["rate"] == RATE


async def test_row_is_uploading_until_the_last_chunk_lands(clock):
    """Write-order contract: the row precedes its chunks, so it must not
    claim `complete` while they upload — the Audio tab gates on `complete`
    and the .wav route caches for 24h, so a torn read would pin a truncated
    call in the operator's browser."""
    rec = _rec()
    rec.add_inbound(_pcm(1.0))
    client = _FakeClient()
    await rec.finalize_and_store(client=client)
    ops = [(op, table, *rest) for (op, table, *rest) in client.log
           if table in ("ai_voice_recordings", "ai_voice_recording_chunks")]
    assert ops[0][0] == "insert" and ops[0][1] == "ai_voice_recordings"
    assert ops[0][2]["status"] == "uploading"
    chunk_ops = [o for o in ops if o[1] == "ai_voice_recording_chunks"]
    assert chunk_ops, "no chunks uploaded"
    flip = ops[-1]
    assert flip[0] == "update" and flip[2] == {"status": "complete"}
    # Every chunk insert happened between the uploading insert and the flip.
    assert client.log.index(flip) > max(client.log.index(c) for c in chunk_ops)


async def test_chunk_failure_downgrades_and_reports_failed(clock):
    """A chunk-upload failure must leave the DB row `failed` AND return a
    summary that says so — not a dict still claiming `complete`."""
    rec = _rec()
    rec.add_inbound(_pcm(1.0))
    client = _FakeClient()
    orig_table = client.table

    def _table(name):
        t = orig_table(name)
        if name == "ai_voice_recording_chunks":
            def _boom(_row):
                raise RuntimeError("chunk insert refused")
            t.insert = _boom  # type: ignore[method-assign]
        return t

    client.table = _table  # type: ignore[method-assign]
    row = await rec.finalize_and_store(client=client)
    assert row is not None and row["status"] == "failed"
    assert row["size_bytes"] == 0
    assert client.tables["ai_voice_recordings"][0]["status"] == "failed"


async def test_mid_call_capture_failure_stores_truncation_note(clock):
    """A recorder that broke AFTER capturing audible audio stores what it
    has, but the row must say the recording is truncated."""
    rec = _rec()
    rec.add_inbound(_pcm(2.0))
    rec.disabled = True  # capture failed 2s in; the call kept going
    client = _FakeClient()
    row = await rec.finalize_and_store(client=client)
    assert row is not None and row["status"] == "complete"
    assert "truncated" in (row["note"] or "")


async def test_prune_runs_even_when_the_store_fails(clock):
    """A persist outage must not also halt retention — old audio keeps
    getting deleted."""
    rec = _rec()
    rec.add_inbound(_pcm(1.0))
    client = _InsertsFailClient()
    client.tables["ai_voice_recordings"] = [
        {"recording_id": "old-1", "created_at": "2020-01-01T00:00:00+00:00"},
    ]
    row = await rec.finalize_and_store(client=client)
    assert row is None  # nothing stored…
    deletes = [e for e in client.log if e[0] == "delete"]
    assert any(e[1] == "ai_voice_recordings" for e in deletes)  # …but pruned


async def test_finalize_without_audio_stores_an_empty_row(clock):
    rec = _rec()
    client = _FakeClient()
    row = await rec.finalize_and_store(client=client)
    assert row is not None and row["status"] == "empty"
    assert client.tables["ai_voice_recordings"][0]["size_bytes"] == 0
    assert client.tables.get("ai_voice_recording_chunks", []) == []


async def test_text_only_sim_run_stores_a_metadata_only_empty_row(clock):
    """A text-only sim run carries no wire audio (the sim provider's token
    10ms silences stay under the audibility floor). It must land as a
    `simulated` + `empty` row with NO chunks and NO synthesized speech —
    the transcript is the eval record, not a TTS reconstruction."""
    rec = _rec(provider="simulated", simulated=True)
    # What the simulated provider actually emits over a REAL multi-minute
    # eval run: one token 10ms silence per turn, arriving on a moving wall
    # clock. The clock advance is the discriminating part — timeline length
    # (wall-clock padding included) crosses any audibility floor, and a
    # gate on it would store minutes of silent 'complete' WAV. Only the
    # captured-audio measure keeps this a metadata-only row.
    for turn in range(1, 4):
        clock.t = turn * 20.0
        rec.add_outbound(b"\x00\x00" * 160)
    client = _FakeClient()
    row = await rec.finalize_and_store(client=client)
    assert row is not None
    assert row["status"] == "empty"
    assert row["simulated"] is True
    assert "synthesized" not in row
    assert client.tables["ai_voice_recordings"][0]["size_bytes"] == 0
    assert client.tables.get("ai_voice_recording_chunks", []) == []


async def test_live_mode_sim_call_records_its_real_audio(clock):
    """A live-mode eval/scenario run streams REAL audio — recorded like any
    call, just flagged `simulated` so the UI can badge it."""
    rec = _rec(simulated=True)
    rec.add_inbound(_pcm(1.0))
    rec.add_outbound(_pcm(1.0))
    client = _FakeClient()
    row = await rec.finalize_and_store(client=client)
    assert row is not None
    assert row["status"] == "complete"
    assert row["simulated"] is True
    assert client.tables["ai_voice_recording_chunks"]


async def test_finalize_survives_a_broken_db(clock):
    class _Broken:
        def table(self, _name):
            raise RuntimeError("supabase down")

    rec = _rec()
    rec.add_inbound(_pcm(1.0))
    assert await rec.finalize_and_store(client=_Broken()) is None


def test_prune_deletes_chunks_with_their_rows(clock):
    client = _FakeClient()
    client.tables["ai_voice_recordings"] = [
        {"recording_id": "old-1", "created_at": "2020-01-01T00:00:00+00:00"},
    ]
    _prune_old(client)
    deletes = [entry for entry in client.log if entry[0] == "delete"]
    # Chunks go per recording (bounded statements), rows in one sweep.
    assert any(e[1] == "ai_voice_recording_chunks"
               and e[2].get("recording_id") == "old-1" for e in deletes)
    assert any(e[1] == "ai_voice_recordings"
               and e[2].get("recording_id__in") == ["old-1"] for e in deletes)
