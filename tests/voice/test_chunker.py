from voice.chunker import SentenceChunker


def test_feed_emits_completed_sentences_only():
    c = SentenceChunker()
    assert c.feed("Hello ther") == []
    assert c.feed("e. How ar") == ["Hello there."]
    # A boundary char emits immediately (low-latency streaming), even at end of buffer.
    assert c.feed("e you?") == ["How are you?"]
    assert c.flush() == ""


def test_arabic_boundaries():
    c = SentenceChunker()
    out = c.feed("مرحبا بك؟ كيف حالك،")
    assert out == ["مرحبا بك؟", "كيف حالك،"]
    assert c.flush() == ""


def test_max_chars_forces_a_flush_without_punctuation():
    c = SentenceChunker(max_chars=10)
    out = c.feed("abcdefghijklmno")  # 15 chars, no boundary
    assert out == ["abcdefghij"]
    assert c.flush() == "klmno"


def test_flush_is_idempotent_and_clears_buffer():
    c = SentenceChunker()
    c.feed("Leftover")
    assert c.flush() == "Leftover"
    assert c.flush() == ""
