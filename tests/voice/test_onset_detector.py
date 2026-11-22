"""voice/live/onset.py — the endpoint-side barge-in onset detector."""
from __future__ import annotations

import math
from array import array

from voice.live.onset import SpeechOnsetDetector

RATE = 16000


def _tone(sec: float, dbfs: float) -> bytes:
    amp = 32767 * (10 ** (dbfs / 20)) * math.sqrt(2)
    n = int(RATE * sec)
    return array("h", (int(amp * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(n))).tobytes()


def _feed(det, pcm: bytes, chunk_sec: float = 0.02):
    step = int(RATE * chunk_sec) * 2
    return [det.feed(pcm[i:i + step]) for i in range(0, len(pcm), step)]


def _det(**kw):
    return SpeechOnsetDetector(sample_rate=RATE, min_speech_sec=0.3, threshold_dbfs=-35.0, **kw)


def test_level_of_silence_and_tone():
    assert SpeechOnsetDetector.level_dbfs(b"\0" * 640) == -120.0
    assert abs(SpeechOnsetDetector.level_dbfs(_tone(0.1, -20)) - -20) < 0.5


def test_sustained_speech_fires_once():
    det = _det()
    fired = _feed(det, _tone(1.0, -20))
    assert fired.count(True) == 1
    assert fired.index(True) == 14          # 15 x 20ms = 0.3s


def test_short_blip_and_quiet_audio_never_fire():
    det = _det()
    assert not any(_feed(det, _tone(0.2, -20) + _tone(0.3, -60) + _tone(0.2, -20)))
    assert not any(_feed(_det(), _tone(2.0, -45)))   # residual echo / room noise


def test_rearms_after_silence():
    det = _det()
    pcm = _tone(0.5, -20) + _tone(0.5, -80) + _tone(0.5, -20)
    assert _feed(det, pcm).count(True) == 2


def test_short_gap_inside_speech_keeps_one_onset():
    det = _det()
    pcm = _tone(0.2, -20) + _tone(0.06, -80) + _tone(0.2, -20)
    assert _feed(det, pcm).count(True) == 1
