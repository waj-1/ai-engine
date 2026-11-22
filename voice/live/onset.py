"""Caller speech-onset detection on the raw inbound audio — the barge-in a
provider cannot see.

A realtime provider reports `interrupted` only while it is still GENERATING.
Gemini Live generates speech several times faster than real time, so a
six-second sentence is finished upstream after a second or two and the rest
plays out of the browser's buffer — where nothing can cut it: the caller
talks over her, Gemini quietly opens a new turn, and the old sentence keeps
playing to its end before the reply. Live on a client outbound demo
(2026-10-05, gemini-3.1-flash-live-preview): 18 turns, `barge_ins=1`, and the
user's report "doesn't respond to interruptions". Gemini also never emits
`speech_started_idle`, so the endpoint's sustained-speech cut (Azure) has no
onset to arm on there.

So the endpoint listens itself. The browser captures with echoCancellation +
noiseSuppression (static/shared/voice-live.js), so what reaches us while she
is playing is overwhelmingly the caller, not her echo; a short SUSTAINED run
of frames above a level threshold is speech. The endpoint flushes playback on
it only while audio is actually still queued at the caller's side, so this
never changes ordinary turn-taking.

Pure and synchronous: fed PCM16-LE mono chunks, answers "did a sustained
onset just complete?". One instance per call.
"""
from __future__ import annotations

import math
from array import array


class SpeechOnsetDetector:
    """Fires once per run of speech that stays above `threshold_dbfs` for
    `min_speech_sec` (short gaps tolerated), then re-arms only after
    `rearm_silence_sec` of quiet — so one long utterance is one onset."""

    def __init__(self, *, sample_rate: int, min_speech_sec: float,
                 threshold_dbfs: float = -35.0, max_gap_sec: float = 0.12,
                 rearm_silence_sec: float = 0.4) -> None:
        self._rate = max(1, int(sample_rate))
        self._min = max(0.0, float(min_speech_sec))
        self._threshold = float(threshold_dbfs)
        self._max_gap = float(max_gap_sec)
        self._rearm = float(rearm_silence_sec)
        self._voiced = 0.0      # seconds of speech in the current run
        self._gap = 0.0         # seconds of quiet since the run's last voiced frame
        self._fired = False     # this run already produced its onset

    @staticmethod
    def level_dbfs(pcm16: bytes) -> float:
        n = len(pcm16) // 2
        if n == 0:
            return -120.0
        samples = array("h")
        samples.frombytes(pcm16[: n * 2])
        mean_sq = sum(s * s for s in samples) / n
        if mean_sq <= 0:
            return -120.0
        return 10.0 * math.log10(mean_sq / (32768.0 * 32768.0))

    def feed(self, pcm16: bytes) -> bool:
        """Account one chunk; True exactly when a sustained onset completes."""
        dur = (len(pcm16) // 2) / self._rate
        if dur <= 0:
            return False
        if self.level_dbfs(pcm16) >= self._threshold:
            self._voiced += dur
            self._gap = 0.0
        else:
            self._gap += dur
            if self._gap > self._max_gap and not self._fired:
                self._voiced = 0.0          # a blip, not speech: start over
            if self._gap >= self._rearm:
                self._voiced = 0.0
                self._fired = False         # the utterance ended: re-arm
        if not self._fired and self._voiced >= self._min:
            self._fired = True
            return True
        return False
