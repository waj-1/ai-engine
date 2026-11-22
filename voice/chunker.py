"""Accumulate streamed LLM tokens and flush sentence-sized chunks for TTS.

Flushing on a sentence boundary keeps prosody natural while letting the first
audio packet leave quickly. Boundaries cover Latin (. ! ?), Arabic question
mark (؟) and comma (،), and newlines. A max-char cap forces a flush when the
model produces a long run with no punctuation.
"""
from __future__ import annotations

from typing import List

_BOUNDARY_CHARS = frozenset(".!?؟،\n")


class SentenceChunker:
    def __init__(self, max_chars: int = 240) -> None:
        self._buf: str = ""
        self._max_chars = max_chars

    def feed(self, text: str) -> List[str]:
        """Append text; return any sentences completed by this addition."""
        self._buf += text
        out: List[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            chunk = self._buf[:cut].strip()
            self._buf = self._buf[cut:].lstrip()
            if chunk:
                out.append(chunk)
        return out

    def _find_cut(self) -> int | None:
        """Index *after* the first boundary char, or the max-char cap."""
        for i, ch in enumerate(self._buf):
            if ch in _BOUNDARY_CHARS:
                # A "." flanked by digits is a decimal point, not a sentence
                # end. Without this, "SAR 1.2 million" flushes as two chunks
                # ("… SAR 1." / "2 million …") and TTS speaks a wrong figure
                # with confident sentence-final prosody — the exact hazard
                # class speakable's truncation digit-guard exists to prevent.
                # A trailing "1." at end-of-buffer is left uncut too: the next
                # feed() may deliver the rest of the number; flush() still
                # emits it if the stream really ended there.
                if (
                    ch == "."
                    and i > 0
                    and self._buf[i - 1].isdigit()
                    and (i + 1 >= len(self._buf) or self._buf[i + 1].isdigit())
                ):
                    continue
                return i + 1
        if len(self._buf) >= self._max_chars:
            return self._max_chars
        return None

    def flush(self) -> str:
        """Return and clear whatever remains (no trailing boundary)."""
        rem = self._buf.strip()
        self._buf = ""
        return rem
