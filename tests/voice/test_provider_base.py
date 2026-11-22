import pytest
from voice.providers.base import AudioChunk, TranscriptResult, VoiceCaps, VoiceProvider


def test_dataclasses_carry_expected_fields():
    t = TranscriptResult(text="hi", model="m", language="en")
    assert t.is_final is True
    a = AudioChunk(data=b"x", mime="audio/wav", seq=0, sample_rate=24000)
    assert a.seq == 0


def test_voiceprovider_is_abstract():
    with pytest.raises(TypeError):
        VoiceProvider()  # cannot instantiate the ABC directly


def test_default_caps_are_all_false():
    assert VoiceCaps() == VoiceCaps(False, False, False)
