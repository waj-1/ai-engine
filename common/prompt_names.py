"""Names that reach a system prompt from outside the engine.

`business_name` comes from the business owner's own record and
`customer_name` from the customer's channel profile (their WhatsApp display
name). Both are substituted into the system prompt as one phrase inside a
sentence, so a value with a line break could add a line that reads as an
instruction ("Harbor Cafe" + newline + "IGNORE THE RULES ABOVE"), and a bidi control could
reorder what the model (and an operator reading a transcript) sees. This
keeps the value a single line of printable text and caps its length; it
does not touch letters of any script, so Arabic names pass through intact.
"""
from __future__ import annotations

import unicodedata
from typing import Any

# Explicit bidi embeddings/overrides/isolates (category Cf, so not covered
# by the Cc test below). The marks LRM/RLM are left alone: they are part of
# ordinary mixed-script text and cannot reorder a run.
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")

MAX_NAME_LEN = 200


def one_line_name(value: Any, max_len: int = MAX_NAME_LEN) -> str:
    """`value` as one line of printable text, at most `max_len` chars.

    Non-strings and names that are only whitespace/controls become "", so
    the caller's default ("this business") applies.
    """
    if not isinstance(value, str):
        return ""
    # Whitespace first: \n, \r and \t are controls (Cc) and must become a
    # separator, not vanish and glue two words together. str.split() then
    # folds every run, including the Unicode line/paragraph separators.
    kept = (
        " " if ch.isspace()
        else "" if unicodedata.category(ch) in ("Cc", "Cs", "Co") or ch in _BIDI_CONTROLS
        else ch
        for ch in value
    )
    out = " ".join("".join(kept).split())
    if len(out) > max_len:
        out = out[:max_len].rstrip()
    return out
