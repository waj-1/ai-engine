"""Audio container helpers for the voice edge module.

Gemini TTS preview returns raw PCM L16 mono @ 24 kHz; we wrap it in a WAV
(RIFF) container so any HTMLAudioElement can play it back. Restored from the
original /tts endpoint (commit 61087577) and generalized to take the audio
parameters as arguments.
"""
from __future__ import annotations

import struct


def pcm_to_wav(pcm: bytes, *, sample_rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    """Wrap raw little-endian PCM in a minimal WAV (RIFF) container."""
    byte_rate = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    data_size = len(pcm)
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
    fmt = (
        b"fmt "
        + struct.pack(
            "<IHHIIHH",
            16,            # PCM fmt chunk size
            1,             # audio format = PCM
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits,
        )
    )
    data = b"data" + struct.pack("<I", data_size) + pcm
    return header + fmt + data


def wav_pcm(wav: bytes) -> bytes:
    """Return the PCM payload of a WAV, or the input unchanged if it isn't one.

    Walks the RIFF chunk list to find `data` rather than assuming it starts at
    byte 44. Our own `pcm_to_wav` output does begin there, but a provider's
    does not have to: Azure's TTS can prepend a `LIST`/`INFO` chunk, and
    slicing a fixed 44 bytes off that yields audio with metadata bytes read as
    samples — an audible click, or noise.

    Returning the input unchanged for a non-WAV is deliberate: callers use this
    to re-container provider audio, and a provider that hands back bare PCM
    should pass straight through rather than lose its audio to an exception.
    """
    if len(wav) < 12 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return wav
    pos = 12
    while pos + 8 <= len(wav):
        chunk_id = wav[pos:pos + 4]
        (size,) = struct.unpack("<I", wav[pos + 4:pos + 8])
        body = pos + 8
        if chunk_id == b"data":
            # Trust the buffer over the declared size: a stream truncated
            # mid-write (or a header written before the data length was known,
            # which is common for streamed TTS) would otherwise slice past the
            # end and silently drop the tail.
            return wav[body:body + size] if 0 < size <= len(wav) - body else wav[body:]
        # Chunks are word-aligned: an odd size carries a pad byte.
        pos = body + size + (size & 1)
    return b""


def concat_wavs(parts: list[bytes], *, sample_rate: int = 24000) -> bytes:
    """Join several WAVs into ONE valid WAV.

    Concatenating whole WAV files gives a body with a RIFF header every few
    kilobytes; every standard decoder plays the first segment and stops, so a
    multi-sentence reply arrived truncated with no error anywhere. Unwrap each
    part and re-wrap the PCM once.
    """
    if not parts:
        return pcm_to_wav(b"", sample_rate=sample_rate)
    if len(parts) == 1 and parts[0][:4] == b"RIFF":
        return parts[0]
    return pcm_to_wav(b"".join(wav_pcm(p) for p in parts), sample_rate=sample_rate)
