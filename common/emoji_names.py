"""Plain-words names for the emoji in a customer's message.

Chat customers answer with emoji all the time: 👍 to an offered time, 👎,
😡😡😡, ❤️🙏. A customer-chat eval (2026-09-29) showed the model misreading them,
e.g. taking 👍 as a confirmed booking, or 😡😡😡 as small talk. Customers can
send any emoji, so every one is named from the Unicode database (👍 → "thumbs
up sign"), with no model call. The emoji customers send most also carry what
they usually mean in a booking chat (😭 → "sad or disappointed"): a name alone
was not enough for GPT-6 Luna, which answered 😭 after offered times with more
times and quoted 👌 back to the customer (prod evals 2026-10-22). The agent
adds the result to its prompt as a note; the customer's message itself is
never changed, so the transcript keeps what they sent.
"""
from __future__ import annotations

import unicodedata
from typing import List

import regex

# What a person sees as one character ("grapheme cluster"): 👍🏽, ❤️, 💇‍♂️,
# 1️⃣, 🇸🇦 and 🏴󠁧󠁢󠁥󠁮󠁧󠁿 each come out whole. The `regex` package carries newer
# Unicode data than Python's own `re`/`unicodedata` (3.11 ships Unicode 14).
_GRAPHEME = regex.compile(r"\X")
# A cluster is an emoji when Unicode marks part of it as a pictograph, or it
# is a flag (regional-indicator pair) or a keycap (1️⃣ ends in U+20E3).
_EMOJI = regex.compile(r"\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20E3")

# Parts of one emoji that are not emoji themselves: zero-width joiner,
# variation selectors (❤️ = ❤ + U+FE0F), skin tones, keycap mark, tag letters.
_ZWJ = 0x200D
_KEYCAP = 0x20E3
_GLUE = {_ZWJ, 0xFE0E, 0xFE0F, _KEYCAP, *range(0x1F3FB, 0x1F400), *range(0xE0020, 0xE0080)}
_FLAG_LETTERS = (0x1F1E6, 0x1F1FF)
_TAG_LETTERS = (0xE0061, 0xE007A)   # tag a-z: 🏴 + "gbeng" + cancel = England


# What the common emoji usually mean in a booking chat, keyed by the emoji
# without skin tone or variation selector (👍🏽 and ❤️ look up 👍 and ❤).
# Meanings, not instructions: the note's own rules say what to do with them.
_MEANINGS = {
    # "Not a yes to book" alone made Gemini start the booking over and list
    # times again (prod evals 2026-10-22); say what to do with the lean.
    **dict.fromkeys("👍👌✅✔☑", "agreement or OK; after an offer they lean yes, so ask them to confirm it in words"),
    "💯": "full agreement; after an offer they lean yes, so ask them to confirm it in words",
    **dict.fromkeys("👎❌✖🚫", "no, or that does not suit them"),
    **dict.fromkeys("🙏", "thanks, or please"),
    **dict.fromkeys("❤♥💕💖💗😍🥰😘🌹💐🤍💜💙💚🧡", "thanks or affection"),
    **dict.fromkeys("😊🙂😀😃😄😁☺🤗", "friendly or happy"),
    **dict.fromkeys("😂🤣😆😅", "laughing, or a bit embarrassed"),
    **dict.fromkeys("😭😢😞😔☹🙁😥😿💔", "sad or disappointed; after an offer, it does not work for them"),
    **dict.fromkeys("😡😠🤬👿", "angry or upset"),
    **dict.fromkeys("😤😒🙄😩😫", "frustrated or annoyed"),
    **dict.fromkeys("🤔🧐", "unsure, thinking it over"),
    **dict.fromkeys("😮😲😱😳", "surprised or shocked"),
    **dict.fromkeys("❓❔⁉", "a question, or they did not understand"),
    "🤷": "not sure, or no preference",
    "👋": "hello or goodbye",
    **dict.fromkeys("⏰⌚⏳🕐", "a question about time"),
    **dict.fromkeys("📅📆🗓", "a question about a date or appointment"),
    **dict.fromkeys("💇✂💈", "a haircut"),
    "💅": "nails",
    "💆": "a massage or facial",
    "💄": "makeup",
    "🧔": "a beard",
    **dict.fromkeys("🎂🎉🥳🎁", "a birthday or celebration"),
    **dict.fromkeys("🔥👏⭐🌟🤩", "great, excited"),
}


def emoji_meaning(emoji: str) -> str:
    """What `emoji` usually means in a booking chat ("" when it is not in the
    table): 😭 → "sad or disappointed; …", 👍🏽 → the 👍 meaning."""
    base = "".join(ch for ch in emoji if ord(ch) not in _GLUE)
    return _MEANINGS.get(base, "")


def _is_flag_letter(cp: int) -> bool:
    return _FLAG_LETTERS[0] <= cp <= _FLAG_LETTERS[1]


def emoji_in(text: str) -> List[str]:
    """The emoji in `text`, in order, one string per emoji as a person sees it:
    skin tones, variation selectors, joined sequences (💇‍♂️), keycaps (1️⃣),
    flags (🇸🇦) and subdivision flags (🏴󠁧󠁢󠁥󠁮󠁧󠁿) each stay whole."""
    return [g for g in _GRAPHEME.findall(text or "") if _EMOJI.search(g)]


def emoji_name(emoji: str) -> str:
    """"thumbs up sign" for 👍, "flag SA" for 🇸🇦, "flag GB-ENG" for 🏴󠁧󠁢󠁥󠁮󠁧󠁿,
    "keycap 1" for 1️⃣, joined parts for a sequence ("haircut + male sign" for
    💇‍♂️). An emoji newer than Python's Unicode data is named "a newer emoji"."""
    cps = [ord(ch) for ch in emoji]
    if len(cps) == 2 and all(_is_flag_letter(cp) for cp in cps):
        return "flag " + "".join(chr(ord("A") + cp - _FLAG_LETTERS[0]) for cp in cps)
    tags = "".join(chr(cp - 0xE0000) for cp in cps if _TAG_LETTERS[0] <= cp <= _TAG_LETTERS[1])
    if tags:
        return f"flag {tags[:2].upper()}-{tags[2:].upper()}"
    if _KEYCAP in cps:
        return "keycap " + emoji[0]
    parts = [unicodedata.name(chr(cp), "").lower() for cp in cps if cp not in _GLUE]
    return " + ".join(p for p in parts if p) or "a newer emoji"


def is_emoji_only(text: str) -> bool:
    """True when nothing but emoji, spaces and punctuation is left."""
    rest = text or ""
    for e in emoji_in(rest):
        rest = rest.replace(e, "")
    return bool(emoji_in(text)) and not any(ch.isalnum() for ch in rest)


def emoji_note(text: str) -> str:
    """A note for the model naming the emoji in a customer's message, with
    their usual meaning where known, or "" when there are none. Repeats are
    counted, not listed ("😡 ×3")."""
    found = emoji_in(text)
    if not found:
        return ""
    counts: dict = {}
    for e in found:
        counts[e] = counts.get(e, 0) + 1
    parts = []
    for e, n in counts.items():
        meaning = emoji_meaning(e)
        label = f"{emoji_name(e)}: {meaning}" if meaning else emoji_name(e)
        parts.append(f"{e} ({label})" + (f" ×{n}" if n > 1 else ""))
    named = ", ".join(parts)
    # Luna quoted the emoji back ("“👌” isn't a clear yes"), breaking the
    # prompt's no-emoji rule; say it where the emoji is.
    words_only = "Reply in words only: do not use, repeat or quote any emoji."
    if is_emoji_only(text):
        return (
            f"THE CUSTOMER'S LATEST MESSAGE IS ONLY EMOJI: {named}. Work out what "
            "they mean from the conversation so far (agreement, a 'no', thanks, a "
            "greeting, frustration, a question) and reply to that, including how "
            "they feel. An emoji is never a confirmation: a booking change still "
            "needs a clear yes in words. Emoji have no language: reply in the "
            "language of the customer's most recent written message. "
            f"{words_only}"
        )
    return (
        f"The customer's latest message includes emoji: {named}. Take what they "
        f"mean into account. {words_only}"
    )
