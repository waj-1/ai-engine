"""common/emoji_names: plain names for the emoji a customer sends."""
import pytest

from common.emoji_names import emoji_in, emoji_meaning, emoji_name, emoji_note, is_emoji_only


@pytest.mark.parametrize("text, expected", [
    ("👍", ["👍"]),
    ("😡😡😡", ["😡", "😡", "😡"]),
    ("Thanks 👍🏽", ["👍🏽"]),                       # skin tone: one emoji
    ("❤️🙏", ["❤️", "🙏"]),                         # variation selector: one emoji
    ("💇‍♂️ tomorrow?", ["💇‍♂️"]),                    # zero-width-joined: one emoji
    ("🇸🇦 هل تتكلمون عربي؟", ["🇸🇦"]),               # two flag letters: one flag
    ("⏰ tomorrow?", ["⏰"]),
    ("1️⃣", ["1️⃣"]),                                 # keycap
    ("↔️ ▶️ Ⓜ️ ⤴️", ["↔️", "▶️", "Ⓜ️", "⤴️"]),        # outside the old block ranges
    ("🏴󠁧󠁢󠁥󠁮󠁧󠁿", ["🏴󠁧󠁢󠁥󠁮󠁧󠁿"]),                                 # subdivision flag: one emoji
    ("\U0001FA77", ["\U0001FA77"]),                 # pink heart: newer than Python 3.11's data
    ("no emoji here: 10:00, AED 30, option 5", []),
])
def test_emoji_in_keeps_each_emoji_whole(text, expected):
    assert emoji_in(text) == expected


@pytest.mark.parametrize("emoji, name", [
    ("👍", "thumbs up sign"),
    ("👍🏽", "thumbs up sign"),
    ("😡", "pouting face"),
    ("❤️", "heavy black heart"),
    ("💇‍♂️", "haircut + male sign"),
    ("🇸🇦", "flag SA"),
    ("🏴󠁧󠁢󠁥󠁮󠁧󠁿", "flag GB-ENG"),
    ("1️⃣", "keycap 1"),
    ("↔️", "left right arrow"),
])
def test_emoji_name(emoji, name):
    assert emoji_name(emoji) == name


@pytest.mark.parametrize("text, only", [
    ("👍", True), ("😡😡😡", True), ("❤️ 🙏!", True),
    ("تمام 👍", False), ("😢 I missed my appointment", False), ("hello", False), ("", False),
])
def test_is_emoji_only(text, only):
    assert is_emoji_only(text) is only


def test_emoji_only_note_names_it_and_guards_confirmation():
    note = emoji_note("👍")
    assert "ONLY EMOJI" in note
    assert "👍 (thumbs up sign: agreement or OK; after an offer they lean yes, so ask them to confirm it in words)" in note
    assert "never a confirmation" in note


def test_repeats_are_counted():
    assert "😡 (pouting face: angry or upset) ×3" in emoji_note("😡😡😡")


def test_mixed_message_note_names_the_emoji():
    note = emoji_note("😢 I missed my appointment")
    assert "😢 (crying face: sad or disappointed" in note
    assert "ONLY EMOJI" not in note


def test_no_emoji_no_note():
    assert emoji_note("What time do you open?") == ""


def test_an_emoji_newer_than_pythons_data_is_still_noted():
    # Python 3.11 names emoji up to Unicode 14; a newer one is still found and
    # gets a note, just without its name.
    note = emoji_note("\U0001FA77")
    assert "ONLY EMOJI" in note and "(a newer emoji)" in note


@pytest.mark.parametrize("emoji, meaning", [
    ("😭", "sad or disappointed"),
    ("👍🏽", "confirm it in words"),      # skin tone looks up the plain emoji
    ("❤️", "thanks or affection"),        # variation selector too
    ("👎", "does not suit them"),
    ("🤔", "unsure"),
])
def test_common_emoji_carry_a_meaning(emoji, meaning):
    # Prod evals 2026-10-22: with names only, GPT-6 Luna answered 😭 after
    # offered times with more times, ignoring the disappointment.
    assert meaning in emoji_meaning(emoji)


def test_an_unlisted_emoji_keeps_just_its_name():
    assert emoji_meaning("🦒") == ""
    assert "🦒 (giraffe face)" in emoji_note("🦒")


def test_the_note_says_not_to_echo_emoji():
    # Luna quoted the emoji back: "“👌” isn't a clear yes".
    for text in ("👌", "😢 I missed my appointment"):
        assert "do not use, repeat or quote any emoji" in emoji_note(text)
