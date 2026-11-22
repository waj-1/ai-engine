"""Per-call audio capture for the duplex voice edge.

The websocket endpoint feeds this from its two audio choke points (caller mic
frames in, provider speech frames out); at teardown the two mono tracks are
interleaved into ONE stereo WAV — left = caller, right = assistant — and
persisted to the AI project (`ai_voice_recordings` + chunked base64 bytes in
`ai_voice_recording_chunks`) so the observability explorer's Audio tab can
play the call back. Stored in the DB rather than a blob store deliberately:
the same code then works on local dev, the demo VM's bundled Supabase, and
prod with zero new infrastructure, and the retention prune keeps the table a
rolling window rather than an archive.

Timeline placement, not naive concatenation. Provider audio arrives FAR
faster than realtime (28s of speech in 6s, measured live 2026-08-04), so
appending chunks back to back would compress every answer into a chipmunk
burst. Each chunk is instead placed at max(wall-clock arrival offset,
current end of track) with silence padding the gaps — a burst plays
contiguously from the moment it started arriving, which is what the caller
actually heard. `mark_playback_flush()` additionally rewinds the assistant
track to "now" when the endpoint flushes the client's playback queue
(barge-in): everything past that instant was discarded unheard, and the next
answer overwrites it — without this the recording plays answers the caller
never received, over the caller's own interruption.

Simulated conversations record like any other call, with one deliberate
carve-out: a TEXT-ONLY simulated run (`?provider=simulated`) carries no real
audio on the wire — the caller's turns are JSON text frames and the simulated
provider emits a token 10ms of silence per turn — and we do NOT synthesize
speech for it. Its transcript is the eval artefact; TTS-ing it would spend
real synthesis money to fake audio nobody spoke. Such a call stores a
metadata-only row (`status: empty`, `simulated: true`) so the explorer's
Audio tab can say why there is nothing to play. Live-mode eval and scenario
runs DO stream real audio through the socket and get a normal recording,
flagged `simulated` so operators can tell bot-driven calls from real callers.

Failure posture matches the rest of the voice edge: NOTHING here may take
down a live call. Every public method swallows its own errors; a recorder
that breaks mid-call disables itself and the teardown records what it can.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct
import tempfile
import time
from array import array
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

try:  # Removed in Python 3.13; the image runs 3.11. The fallback below keeps
    import audioop  # noqa: F401  # the recorder alive (mono-rate calls never
except Exception:  # pragma: no cover  # resample anyway — azure/sim are 24k/24k).
    audioop = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_TABLE = "ai_voice_recordings"
_CHUNKS_TABLE = "ai_voice_recording_chunks"

# Raw bytes per chunk row. base64 inflates by 4/3, so this lands each insert
# around 2MB — comfortably inside PostgREST's request ceiling.
_CHUNK_RAW_BYTES = 1_500_000

# Below this much captured audio the call is considered to have carried none
# (the simulated provider's token 10ms silences land here too).
_MIN_AUDIBLE_MS = 250

# In-memory spool ceiling per track before tempfile spills to disk. Kept small
# relative to a long call (16kHz mono = ~1.9MB/min) so concurrent calls don't
# hold whole recordings in RAM.
_SPOOL_BYTES = 4 * 1024 * 1024

# Per-channel bytes interleaved per step during WAV assembly — bounds peak
# memory at a few MB however long the call ran. Must stay even (pcm16).
_INTERLEAVE_BLOCK = 1 << 20


def recording_enabled() -> bool:
    """Master switch. Read fresh (not frozen at import) like the budget's
    env reads, so tests and an operator restart can flip it."""
    return (os.getenv("VOICE_RECORDING_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def recording_max_seconds() -> int:
    try:
        return max(1, int(os.getenv("VOICE_RECORDING_MAX_SEC", "900")))
    except ValueError:
        return 900


def recording_sample_rate() -> int:
    try:
        return max(8000, int(os.getenv("VOICE_RECORDING_SAMPLE_RATE", "16000")))
    except ValueError:
        return 16000


def recording_retention_days() -> int:
    try:
        return max(1, int(os.getenv("VOICE_RECORDING_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def _resample(pcm: bytes, src_rate: int, dst_rate: int, state: Any) -> Tuple[bytes, Any]:
    """PCM16 mono src_rate -> dst_rate. `state` is audioop.ratecv's carry-over
    so chunk boundaries don't click; the no-audioop fallback is nearest-sample
    (fidelity is secondary — this is an observability artefact)."""
    pcm = pcm[: len(pcm) // 2 * 2]
    if src_rate == dst_rate or not pcm:
        return pcm, state
    if audioop is not None:
        return audioop.ratecv(pcm, 2, 1, src_rate, dst_rate, state)
    samples = array("h")
    samples.frombytes(pcm)
    n_out = int(len(samples) * dst_rate / src_rate)
    out = array("h", (samples[min(int(i * src_rate / dst_rate), len(samples) - 1)]
                      for i in range(n_out)))
    return out.tobytes(), None


class _Track:
    """One direction's timeline-aligned PCM, spooled to a temp file."""

    def __init__(self, rate: int, max_bytes: int) -> None:
        self.rate = rate
        self.max_bytes = max_bytes
        self.file = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)
        self.cursor = 0          # bytes written == current end of track
        self.state: Any = None   # ratecv carry-over
        self.fed_ms = 0.0        # audio fed (post-resample), gaps excluded

    def write_at_least_at(self, pcm: bytes, min_offset_bytes: int) -> None:
        """Append `pcm`, padding silence up to `min_offset_bytes` first when
        the track is behind the wall clock. Never seeks backward."""
        if self.cursor < min_offset_bytes:
            gap = min(min_offset_bytes, self.max_bytes) - self.cursor
            if gap > 0:
                self._write_zeros(gap)
        room = self.max_bytes - self.cursor
        if room <= 0:
            return
        chunk = pcm[:room]
        self.file.write(chunk)
        self.cursor += len(chunk)
        self.fed_ms += len(chunk) / 2.0 / self.rate * 1000.0

    def _write_zeros(self, nbytes: int) -> None:
        # Bounded writes so a multi-minute silence gap doesn't allocate it all.
        block = b"\x00" * 65536
        while nbytes > 0:
            step = min(nbytes, len(block))
            self.file.write(block[:step])
            self.cursor += step
            nbytes -= step

    def truncate_to(self, offset_bytes: int) -> None:
        if offset_bytes < self.cursor:
            self.file.seek(offset_bytes)
            self.file.truncate()
            self.cursor = offset_bytes
            # The resampler's carry-over belongs to a stream that just got cut.
            self.state = None

    def read_all(self) -> bytes:
        self.file.seek(0)
        return self.file.read()

    def close(self) -> None:
        try:
            self.file.close()
        except Exception:  # noqa: BLE001
            pass


class VoiceCallRecorder:
    """Captures one /voice/live call. Every public method is best-effort —
    a failure disables further capture but never raises to the caller."""

    def __init__(
        self,
        *,
        call_id: str,
        session_id: str,
        agent_kind: str,
        business_id: Optional[str],
        provider: str,
        simulated: bool,
        input_sample_rate: int,
        output_sample_rate: int,
        sample_rate: Optional[int] = None,
        max_sec: Optional[int] = None,
    ) -> None:
        self.call_id = call_id
        self.session_id = session_id
        self.agent_kind = agent_kind
        self.business_id = business_id
        self.provider = provider
        self.simulated = simulated
        self.rate = int(sample_rate or recording_sample_rate())
        self.max_bytes = int(max_sec or recording_max_seconds()) * self.rate * 2
        self.started_at = datetime.now(timezone.utc)
        self._t0 = time.monotonic()
        self._in_rate = int(input_sample_rate or self.rate)
        self._out_rate = int(output_sample_rate or self.rate)
        self._in = _Track(self.rate, self.max_bytes)
        self._out = _Track(self.rate, self.max_bytes)
        self.disabled = False

    # ------------------------------------------------------------------ feed

    def _offset_bytes(self) -> int:
        return int((time.monotonic() - self._t0) * self.rate) * 2

    def _feed(self, track: _Track, pcm: bytes, src_rate: int) -> None:
        if self.disabled or not pcm:
            return
        try:
            resampled, track.state = _resample(pcm, src_rate, self.rate, track.state)
            track.write_at_least_at(resampled, self._offset_bytes())
        except Exception:  # noqa: BLE001 - capture must never hurt the call
            self.disabled = True
            logger.warning("voice recording: capture failed; disabling for this call",
                           exc_info=True)

    def add_inbound(self, pcm: bytes) -> None:
        """Caller mic audio, as received from the client websocket."""
        self._feed(self._in, pcm, self._in_rate)

    def add_outbound(self, pcm: bytes) -> None:
        """Assistant audio, as forwarded to the client websocket."""
        self._feed(self._out, pcm, self._out_rate)

    def mark_playback_flush(self) -> None:
        """The endpoint told the client to discard its playback queue: audio
        already written past 'now' was never heard — cut it so the next answer
        lands where the caller actually experienced it."""
        if self.disabled:
            return
        try:
            self._out.truncate_to(self._offset_bytes())
        except Exception:  # noqa: BLE001
            self.disabled = True
            logger.warning("voice recording: flush truncate failed; disabling",
                           exc_info=True)

    # -------------------------------------------------------------- teardown

    @property
    def captured_ms(self) -> float:
        """Milliseconds of audio actually FED to the tracks — wall-clock
        silence padding excluded. This, not timeline length, is the
        audibility test: a text-only sim run's tracks are almost all padding
        (its only real input is a token 10ms silence per turn), and judging
        by timeline length would store minutes of silent 'complete' WAV for
        exactly the calls documented to land as metadata-only rows."""
        return max(self._in.fed_ms, self._out.fed_ms)

    def _assemble_wav_file(self) -> Tuple[Any, int, int]:
        """Interleave the two mono spools into a stereo WAV in a THIRD spool.

        Returns (file, size_bytes, duration_ms). Block-wise on purpose: a
        long call's tracks are tens of MB, and materializing whole-track
        copies (bytes + arrays + the joined WAV) transiently held several
        full-length copies of the audio — a disconnect wave tearing down a
        few calls at once could OOM the container and hurt the live calls
        this module swears never to touch. Peak memory here is a few MB
        regardless of call length. array('h') slice assignment does the
        interleave at C speed; machine byte order is little-endian on every
        deployment target, matching WAV's."""
        n = max(self._in.cursor, self._out.cursor)
        n -= n % 2
        data_size = n * 2  # two channels
        out = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)
        header = _wav_header(data_size, self.rate)
        out.write(header)
        self._in.file.seek(0)
        self._out.file.seek(0)
        pos = 0
        while pos < n:
            step = min(_INTERLEAVE_BLOCK, n - pos)
            left = array("h")
            left.frombytes(self._in.file.read(step).ljust(step, b"\x00"))
            right = array("h")
            right.frombytes(self._out.file.read(step).ljust(step, b"\x00"))
            stereo = array("h", bytes(step * 2))
            stereo[0::2] = left
            stereo[1::2] = right
            out.write(stereo.tobytes())
            pos += step
        duration_ms = int(n / 2 * 1000 / self.rate)
        return out, len(header) + data_size, duration_ms

    def _assemble_wav(self) -> Tuple[bytes, int]:
        """Whole-file flavour of `_assemble_wav_file`, for tests/diagnostics
        — production persistence streams from the file and never holds the
        WAV in one bytes object."""
        f, _size, duration_ms = self._assemble_wav_file()
        try:
            f.seek(0)
            return f.read(), duration_ms
        finally:
            f.close()

    async def finalize_and_store(
        self, *, client: Any = None
    ) -> Optional[Dict[str, Any]]:
        """Assemble the recording and persist it. Returns the row as actually
        stored (final status), or None when nothing landed. Never raises.

        A text-only simulated run lands here with (near) no captured audio
        and is stored as a metadata-only `empty` row — deliberately NOT
        synthesized from its transcript (the transcript is the eval artefact;
        see the module docstring)."""
        wav_file: Any = None
        try:
            audible = self.captured_ms >= _MIN_AUDIBLE_MS
            if audible:
                wav_file, size_bytes, duration_ms = await asyncio.to_thread(
                    self._assemble_wav_file
                )
                status = "complete"
            else:
                # Metadata-only row: nothing worth playing. `failed` only when
                # the recorder broke before capturing anything audible — a
                # break AFTER capture stores what it has, marked via `note`.
                size_bytes, duration_ms = 0, 0
                status = "failed" if self.disabled else "empty"
            row = {
                "call_id": self.call_id,
                "session_id": self.session_id,
                "agent_kind": self.agent_kind,
                "business_id": self.business_id,
                "provider": self.provider,
                "simulated": self.simulated,
                "sample_rate": self.rate,
                "channels": 2,
                "duration_ms": duration_ms,
                "size_bytes": size_bytes,
                "status": status,
                "note": ("capture failed mid-call; the recording is truncated"
                         if self.disabled and status == "complete" else None),
                "started_at": self.started_at.isoformat(),
            }
            stored = await asyncio.to_thread(_persist, row, wav_file, client)
            # The retention sweep runs whether or not THIS call stored — a
            # persist outage must not also halt deletion of old audio. (Known
            # limitation: with VOICE_RECORDING_ENABLED=0 no recorder exists,
            # so nothing prunes at all — see .env.example.)
            await asyncio.to_thread(_prune_old, client)
            return stored
        except Exception:  # noqa: BLE001 - teardown must not raise into the endpoint
            logger.warning("voice recording: finalize failed", exc_info=True)
            return None
        finally:
            self._in.close()
            self._out.close()
            if wav_file is not None:
                try:
                    wav_file.close()
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------- persistence


def _client_or_default(client: Any) -> Any:
    if client is not None:
        return client
    from core.auth import get_service_client

    return get_service_client()


def _wav_header(data_size: int, sample_rate: int, channels: int = 2) -> bytes:
    """A RIFF header declaring `data_size` bytes of pcm16 to follow.

    `voice/audio.pcm_to_wav` needs the whole payload in hand; this is its
    streaming counterpart (same fields) so assembly never holds the file."""
    bits = 16
    byte_rate = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    return (
        b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", data_size)
    )


def _persist(row: Dict[str, Any], wav_file: Any, client: Any) -> Optional[Dict[str, Any]]:
    """Store the row, stream its chunks, then flip the status to final.

    The row is inserted as `uploading` and flips to `complete` only AFTER the
    last chunk lands: a long call uploads dozens of ~2MB chunk inserts over
    many seconds, the Audio tab gates playback on `complete`, and the .wav
    route caches for 24h — serving a mid-upload join as complete would pin a
    truncated call in the operator's browser. A chunk failure downgrades the
    row to `failed` instead. Returns the row as actually stored (final
    status), or None when nothing landed."""
    try:
        client = _client_or_default(client)
        final_status = str(row.get("status") or "empty")
        expect_chunks = wav_file is not None and final_status == "complete"
        chunk_count = 0
        if expect_chunks:
            size = int(row.get("size_bytes") or 0)
            chunk_count = max(1, -(-size // _CHUNK_RAW_BYTES))
        ins = dict(
            row,
            status=("uploading" if expect_chunks else final_status),
            chunk_count=chunk_count,
        )
        inserted = client.table(_TABLE).insert(ins).execute().data or []
        recording_id = (inserted[0] or {}).get("recording_id") if inserted else None
        if not recording_id:
            return None
        if expect_chunks:
            try:
                wav_file.seek(0)
                seq = 0
                while True:
                    chunk = wav_file.read(_CHUNK_RAW_BYTES)
                    if not chunk:
                        break
                    client.table(_CHUNKS_TABLE).insert({
                        "recording_id": recording_id,
                        "seq": seq,
                        "audio_b64": base64.b64encode(chunk).decode("ascii"),
                    }).execute()
                    seq += 1
                client.table(_TABLE).update(
                    {"status": final_status}
                ).eq("recording_id", recording_id).execute()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "voice recording: chunk upload failed; marking row failed",
                    exc_info=True,
                )
                client.table(_TABLE).update(
                    {"status": "failed", "size_bytes": 0, "chunk_count": 0}
                ).eq("recording_id", recording_id).execute()
                client.table(_CHUNKS_TABLE).delete().eq(
                    "recording_id", recording_id
                ).execute()
                return dict(row, recording_id=recording_id, status="failed",
                            size_bytes=0, chunk_count=0)
        return dict(row, recording_id=recording_id, status=final_status,
                    chunk_count=chunk_count)
    except Exception:  # noqa: BLE001 - same fail-open posture as the budget writer
        logger.warning("voice recording: persist unavailable", exc_info=True)
        return None


def _prune_old(client: Any) -> None:
    """Opportunistic retention sweep — recordings are a rolling window."""
    try:
        client = _client_or_default(client)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=recording_retention_days())).isoformat()
        rows = (
            client.table(_TABLE)
            .select("recording_id")
            .lt("created_at", cutoff)
            .limit(50)
            .execute()
            .data or []
        )
        ids = [r["recording_id"] for r in rows if r.get("recording_id")]
        if not ids:
            return
        # Chunks are deleted per recording, not in one .in_() sweep: a batch
        # of 50 recordings is thousands of ~2MB rows, and an all-or-nothing
        # delete that hits a statement timeout would abort before the
        # recordings delete and re-attempt the same doomed batch forever.
        for rid in ids:
            client.table(_CHUNKS_TABLE).delete().eq("recording_id", rid).execute()
        client.table(_TABLE).delete().in_("recording_id", ids).execute()
        logger.info("voice recording: pruned %d recording(s) past retention", len(ids))
    except Exception:  # noqa: BLE001
        logger.debug("voice recording: prune skipped", exc_info=True)
