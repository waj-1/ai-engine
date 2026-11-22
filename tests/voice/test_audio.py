import struct
from voice.audio import pcm_to_wav


def test_pcm_to_wav_riff_header_and_sizes():
    pcm = b"\x01\x02" * 100  # 200 bytes of fake PCM L16
    wav = pcm_to_wav(pcm, sample_rate=24000, channels=1, bits=16)

    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert wav[12:16] == b"fmt "
    data_idx = wav.index(b"data")
    declared = struct.unpack("<I", wav[data_idx + 4 : data_idx + 8])[0]
    assert declared == len(pcm)
    assert wav.endswith(pcm)
    assert struct.unpack("<I", wav[4:8])[0] == 36 + len(pcm)


def test_pcm_to_wav_byte_rate_for_24k_mono_16bit():
    wav = pcm_to_wav(b"\x00" * 10, sample_rate=24000, channels=1, bits=16)
    fmt_idx = wav.index(b"fmt ")
    byte_rate = struct.unpack("<I", wav[fmt_idx + 16 : fmt_idx + 20])[0]
    assert byte_rate == 24000 * 1 * 2


# --- wav_pcm / concat_wavs -------------------------------------------------


def test_wav_pcm_round_trips_our_own_container():
    from voice.audio import pcm_to_wav, wav_pcm

    pcm = bytes(range(256)) * 4
    assert wav_pcm(pcm_to_wav(pcm, sample_rate=16000)) == pcm


def test_wav_pcm_skips_a_metadata_chunk_before_data():
    """Azure TTS can prepend LIST/INFO. Slicing a fixed 44 bytes would read
    metadata as samples — an audible click."""
    import struct

    from voice.audio import wav_pcm

    pcm = b"\x01\x02\x03\x04" * 8
    info = b"LIST" + struct.pack("<I", 4) + b"INFO"
    body = (
        b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
        + info
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )
    wav = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    assert wav_pcm(wav) == pcm


def test_wav_pcm_passes_bare_pcm_through_untouched():
    from voice.audio import wav_pcm

    assert wav_pcm(b"not a riff at all") == b"not a riff at all"


def test_wav_pcm_tolerates_a_truncated_declared_size():
    import struct

    from voice.audio import wav_pcm

    pcm = b"\xAA\xBB" * 10
    body = (
        b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
        # declares far more data than the buffer holds (a stream cut mid-write)
        + b"data" + struct.pack("<I", 999999) + pcm
    )
    wav = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    assert wav_pcm(wav) == pcm


def test_concat_wavs_emits_exactly_one_riff_header():
    """The bug: /voice/speak concatenated whole WAVs, so every decoder played
    the first sentence and stopped."""
    from voice.audio import concat_wavs, pcm_to_wav, wav_pcm

    a, b, c = b"\x01\x02" * 8, b"\x03\x04" * 8, b"\x05\x06" * 8
    out = concat_wavs(
        [pcm_to_wav(a, sample_rate=16000), pcm_to_wav(b, sample_rate=16000),
         pcm_to_wav(c, sample_rate=16000)],
        sample_rate=16000,
    )
    assert out.count(b"RIFF") == 1
    assert out.count(b"data") == 1
    assert wav_pcm(out) == a + b + c


def test_concat_wavs_declares_the_real_length():
    import struct

    from voice.audio import concat_wavs, pcm_to_wav

    pcm = b"\x07\x08" * 32
    out = concat_wavs([pcm_to_wav(pcm), pcm_to_wav(pcm)])
    (riff_size,) = struct.unpack("<I", out[4:8])
    assert riff_size == len(out) - 8
    assert struct.unpack("<I", out[40:44])[0] == len(pcm) * 2


def test_concat_wavs_handles_the_empty_and_single_cases():
    from voice.audio import concat_wavs, pcm_to_wav

    assert concat_wavs([]).count(b"RIFF") == 1
    one = pcm_to_wav(b"\x01\x02" * 4)
    assert concat_wavs([one]) == one
