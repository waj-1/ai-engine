"""Keep tool-call syntax the realtime model SPOKE out of the caller's screen.

Live, 2026-09-28 (Azure Voice Live, cascaded gpt-4.1-mini): the announce line
came back as "Let me check. functions.ask_agent", transcribed, forwarded to
the app and committed into the chat bubble. The persona already says "never
say a tool or function name aloud" (and its `ask_agent` rule is imperative,
which `core.prompt_validator` checks) — prompt rules do not hold on a realtime
model, so the edge scrubs what it forwards.

What makes this more than a regex: the provider streams the transcript as
deltas, and the leak arrived split across three of them — `" functions"`,
`".ask"`, `"_agent"`. A per-frame substitution never sees the whole token.
`ToolSyntaxScrubber` therefore holds back the TAIL of the stream while it
could still grow into tool syntax, and releases it (scrubbed) as soon as the
next delta proves otherwise, or at the turn's end.

Scope, deliberately narrow — this must fail toward showing speech, never
toward eating it:
  * `functions.<name>` (the OpenAI tool namespace the model leaked), with an
    optional `(…)` argument list;
  * the one tool name every voice persona declares, `ask_agent`, and a
    `<snake_case_name>(…)` call shape, the "call`ask_agent(query: …)`" form
    CLAUDE.md records from a live probe;
  * backticks around either;
  * Gemini Live's serialisation, which its OUTPUT transcription carried
    verbatim (2026-10, google_genai; the call RECORDING proved none of it was
    audible): `startcall:{query:…}` / `call:<name>{…}`, `ask_agent {…}` and
    `ask_agent\n{\n  "query": …\n}` blocks, a bare `{"query": …}` object, and
    `<ctrlNN>` control tokens. Gemini also cut one off mid-token and went
    straight on to the next (`startcall:{query:…link toone sec startcall:{…}`):
    a fresh tool opener inside an unclosed argument block ends that block.
Plain words are never touched — braces in prose included: a `{` is tool
syntax only after a tool opener or when its first key is `query`. The held
tail is at most one word (or one unclosed call, capped) — a caption lags by one
delta at worst.

This runs at the endpoint (`endpoints/voice_live.py`, the transcript branch of
the provider pump), the single choke point every provider's assistant
transcript passes through: the browser's caption frames, the chat bubble built
from them and the announce-then-stall tripwire's reply buffer all read the
scrubbed text.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_TOOL_NAMES = ("ask_agent",)

# Where a tool-syntax token STARTS:
#   * `functions.<path>` — the OpenAI tool namespace the model leaked; the
#     name alone is removed, with or without an argument list;
#   * the declared tool name(s) — `ask_agent` — likewise;
#   * `startcall:` (optionally followed by a tool name) — Gemini Live's own
#     tool-call serialisation, which its OUTPUT transcription carried verbatim
#     ("let me check startcall:{query:How much do I owe?}let me check");
#   * any other snake_case identifier ONLY when an argument list follows
#     immediately (no space) and looks like arguments (empty, or carrying
#     `:`, `=`, a quote or a brace): `get_bookings()`, `x_y(query: …)`.
#     Prose such as "the per_hour (rate) option" is left alone;
#   * `call:<name>{` — the same Gemini serialisation without the `start`;
#   * a bare JSON object whose first key is `query` — `{"query": "…"}` —
#     the argument block on its own (`ask_agent\n{\n  "query": …}`);
#   * a `<ctrl46>`-style control token.
# Arguments may be a `(…)` list or a `{…}` block, separated from a named
# start by whitespace (including newlines).
_NAMED_START = re.compile(
    r"`?(?:\bfunctions\.[A-Za-z_][\w.]*"
    r"|(?:\bcall:)?\b(?:" + "|".join(_TOOL_NAMES) + r")\b"
    r"|startcall:(?:[A-Za-z_][\w.]*)?)"
)
_GENERIC_START = re.compile(r"`?\b[a-z][a-z0-9]*_[a-z0-9_]+(?=\()")
_CALL_BRACE_START = re.compile(r"\bcall:[A-Za-z_][\w.]*(?=\{)")
_QUERY_OBJECT_START = re.compile(r"\{(?=\s*\"?query\"?\s*:)")
_CONTROL_TOKEN = re.compile(r"<ctrl\d+>|</?(?:start|end)_function_(?:call|response)>")
_QUERY_KEY = re.compile(r"\s*\"?query\"?\s*:\s*")
_ARGUMENTISH = re.compile(r"""^\(\s*\)$|[:="'{]""")
_HOLD_PREFIXES = ("functions.", "startcall:", "call:") + _TOOL_NAMES
# Never hold more than this: a stray "(" must not swallow a whole answer.
_MAX_HOLD = 240
_SPACES = re.compile(r"[ \t]{2,}")
_NEWLINES = re.compile(r"[ \t]*\n\s*")
_PAIRS = {"(": ")", "{": "}"}
# An argument block that never closes must not eat the rest of the turn
# (review of #188: `startcall:{query:…link toone sec, I'm checking your
# account…` with no `}` ever arriving dropped every word after it). Such a
# block is force-ended at the first sentence end — or, failing that, clause
# end — once at least `_MIN_QUERY` characters of argument text were held, or
# at `_BLOCK_CAP` characters (snapped to the next whitespace), whichever
# comes first; what follows is released as speech. A closer or a fresh tool
# opener arriving first still wins.
_MIN_QUERY = 8
_BLOCK_CAP = 120
_SENTENCE_END = re.compile(r"[?\u061f.!](?=\s)")
_CLAUSE_END = re.compile(r"[,\u060c](?=\s)")
# What a stream may END with while it could still become one of the shapes
# above (the token itself is not complete yet).
_PARTIAL_CONTROL = re.compile(r"<(?:c(?:t(?:r(?:l\d*)?)?)?)?")
_PARTIAL_QUERY_OBJECT = re.compile(r'\{\s*(?:"?(?:q(?:u(?:e(?:r(?:y(?:"?\s*)?)?)?)?)?)?)?')


def _balanced_end(text: str, open_at: int) -> int:
    """Index just past the bracket matching the `(`/`{` at `open_at`; -1 if
    unclosed."""
    opener = text[open_at]
    closer = _PAIRS[opener]
    depth = 0
    for i in range(open_at, len(text)):
        ch = text[i]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _bound_unclosed(text: str, args_at: int, *, at_end: bool) -> tuple:
    """(end, reason) for an argument block opened at `args_at` that never
    closed, or (-1, None) to keep holding it (mid-stream, under the cap) /
    drop it (`at_end`, nothing to bound it with).

    Mid-stream the bound is only applied once the cap is reached — before
    that the closer may still arrive, and a `.` inside the query must not
    end the block early. At the turn's end it is applied to whatever there is.
    """
    body = args_at + 1
    if not at_end and len(text) - body < _BLOCK_CAP:
        return -1, None
    key = _QUERY_KEY.match(text, body)
    q0 = key.end() if key else body
    limit = body + _BLOCK_CAP
    for rx in (_SENTENCE_END, _CLAUSE_END):
        for m in rx.finditer(text, q0, min(len(text), limit + 1)):
            if m.start() - q0 >= _MIN_QUERY:
                return m.end(), "boundary"
    if len(text) > limit:
        ws = re.compile(r"\s").search(text, limit)
        if ws is not None:
            return ws.start(), "cap"
    return -1, None


def _tokens(text: str, bound: "str | None" = None):
    """Yield (start, end_or_-1, args_at, forced) for every tool-syntax token
    in `text`. `forced` names why an unclosed block was force-ended
    ("boundary" / "cap", see `_bound_unclosed`), else None. `bound` is None
    (never force-end), "cap" (mid-stream) or "end" (the turn ended).

    end == -1 means an argument list opened and has not closed yet (the
    streaming scrubber holds from `start`); `args_at` is then the index of
    its opening bracket. An unclosed argument list that another token STARTS
    inside is cut there instead: Gemini truncated a `startcall:{query:…` and
    went straight on to the next one, so waiting for its `}` would hold — and
    then drop — everything after it.
    """
    candidates = []
    for m in _NAMED_START.finditer(text):
        candidates.append((m.start(), m.end(), "named"))
    for m in _GENERIC_START.finditer(text):
        candidates.append((m.start(), m.end(), "generic"))
    for m in _CALL_BRACE_START.finditer(text):
        candidates.append((m.start(), m.end(), "named"))
    for m in _QUERY_OBJECT_START.finditer(text):
        candidates.append((m.start(), m.start(), "object"))
    for m in _CONTROL_TOKEN.finditer(text):
        candidates.append((m.start(), m.end(), "control"))
    candidates.sort()
    pos = 0
    for idx, (start, name_end, kind) in enumerate(candidates):
        if start < pos:
            continue
        if kind == "control":
            yield start, name_end, None, None
            pos = name_end
            continue
        j = name_end
        if kind == "named":
            while j < len(text) and text[j].isspace():
                j += 1
        if j < len(text) and text[j] in _PAIRS and (kind != "generic" or text[j] == "("):
            close = _balanced_end(text, j)
            if close == -1:
                later = next((c[0] for c in candidates[idx + 1:]
                              if c[0] > j and c[2] == "named"), None)
                if later is None:
                    forced_end, reason = (
                        _bound_unclosed(text, j, at_end=(bound == "end"))
                        if bound else (-1, None)
                    )
                    if forced_end == -1:
                        yield start, -1, j, None
                        return
                    yield start, forced_end, j, reason
                    pos = forced_end
                    continue
                close = later
            elif kind == "generic" and not _ARGUMENTISH.search(text[j:close]):
                continue  # prose with a parenthetical, not a call
            end = close
        elif kind == "named":
            end = name_end
        else:
            continue
        if end < len(text) and text[end] == "`":
            end += 1
        yield start, end, None, None
        pos = end


def _tidy(text: str) -> str:
    return _SPACES.sub(" ", _NEWLINES.sub("\n", text))


def scrub_tool_syntax(text: str, *, unclosed: str = "keep") -> str:
    """`text` with every complete tool-syntax token removed.

    `unclosed` says what to do with a call whose argument list never closed:
    "keep" it (mid-stream — the scrubber holds it instead), "drop" it — the
    turn ended inside it: it is force-ended at a sentence/clause boundary or
    the block cap when there is one (`_bound_unclosed`), else everything from
    the call on goes — or "name" — remove only the name and its opening
    bracket (plus a `query:` key after a `{`) — the hold cap was hit; the
    rest may be real speech.
    """
    return _scrub(text, unclosed=unclosed)[0]


def _scrub(text: str, *, unclosed: str, upto: "int | None" = None,
           bound: "str | None" = None) -> tuple:
    """(scrubbed text[:upto], how the last block was force-ended or None)."""
    if not text:
        return text, None
    if bound is None and unclosed == "drop":
        bound = "end"
    forced = None
    toks = list(_tokens(text, bound))
    if upto is not None:
        toks = [t for t in toks if t[0] < upto]
        text = text[:upto]
    out, pos, changed = [], 0, False
    for start, end, args_at, why in toks:
        forced = why or forced
        if end == -1:
            if unclosed == "keep":
                break
            out.append(text[pos:start])
            if unclosed == "drop":
                pos = len(text)
                forced = "dropped"
            else:
                forced = "cap"
                pos = args_at + 1
                if text[args_at] == "{":
                    key = _QUERY_KEY.match(text, pos)
                    if key:
                        pos = key.end()
            changed = True
            break
        out.append(text[pos:start])
        pos, changed = end, True
    if not changed:
        return text, forced
    out.append(text[pos:])
    return _tidy("".join(out)), forced


def _could_grow_into_tool_syntax(word: str) -> bool:
    core = word.lstrip("`")
    if not core:
        return bool(word)
    low = core.lower()
    if any(p.startswith(low) or low.startswith(p) for p in _HOLD_PREFIXES):
        # "f" / "func" / "functions" / "functions.ask" / "ask_age" … A one-
        # letter word ("a") is held too: a provider may stream one character
        # per delta, and a released "f" can never be recalled. The cost is
        # one delta of caption lag on words that start the same way.
        return True
    # A snake_case identifier still being streamed: "get_bus" may become
    # "get_business(…)". Only identifier characters — a normal word with an
    # apostrophe or trailing punctuation is released.
    return bool(re.fullmatch(r"[a-z][a-z0-9]*_[a-z0-9_]*", core))


class ToolSyntaxScrubber:
    """Per-stream scrubber: `feed` each delta, `flush` at the turn's end."""

    __slots__ = ("_held", "_orphan_closer", "_ends_ws")

    def __init__(self) -> None:
        self.reset()

    @property
    def holding(self) -> bool:
        return bool(self._held)

    def feed(self, delta: str) -> str:
        text = self._held + (delta or "")
        if self._orphan_closer:
            # A block was force-ended; its real `}` may still arrive. Strip it
            # (and only it) — the words before it were already released.
            at = text.find(self._orphan_closer)
            if at != -1:
                text = text[:at] + text[at + 1:]
                self._orphan_closer = ""
        if len(text) > _MAX_HOLD:
            self._held = ""
            out, forced = _scrub(text, unclosed="name")
            return self._emit(out, forced)
        toks = list(_tokens(text, "cap"))
        cut = self._safe_cut(text, toks)
        for start, end, _a, _f in toks:
            if start < cut < end:
                cut = start  # never split a token between release and hold
        self._held = text[cut:]
        # Anything unclosed in the released part was cut short by the token
        # the hold starts at (see `_tokens`), so it goes.
        out, forced = _scrub(text, unclosed="drop", upto=cut, bound="cap")
        return self._emit(out, forced)

    def flush(self) -> str:
        """Release what is held: legitimate words come out, tool syntax —
        including a call cut off mid-arguments — does not."""
        held, self._held = self._held, ""
        out, forced = _scrub(held, unclosed="drop")
        out = self._emit(out, forced)
        self._orphan_closer, self._ends_ws = "", False
        return out

    def reset(self) -> None:
        self._held = ""
        self._orphan_closer = ""
        self._ends_ws = False

    def _emit(self, out: str, forced: "str | None") -> str:
        if forced:
            # Visible in prod: how often Gemini leaves a block unclosed.
            logger.info("voice.live: unclosed tool-call block force-ended (reason=%s)", forced)
            if forced != "dropped":
                self._orphan_closer = "}"
        if out and self._ends_ws:
            # A token removed between two chunks leaves "check " + " one":
            # the space before it was already sent, drop the one after.
            out = out.lstrip(" \t")
        if out:
            self._ends_ws = out[-1].isspace()
        return out

    @staticmethod
    def _safe_cut(text: str, toks: list) -> int:
        """Index up to which `text` can be released now."""
        for start, end, _args_at, _forced in toks:
            if end == -1:
                return start  # an argument list is still streaming
            if not text[end:].strip() and text[start:end].rstrip("`")[-1:] not in (")", "}", ">"):
                # A tool name with nothing after it but whitespace: its
                # argument block may still arrive (`ask_agent\n{…}`).
                return start
        # The stream may END in the first characters of a control token or a
        # bare `{"query":` object, which no word boundary separates from what
        # comes before.
        for opener, partial in (("<", _PARTIAL_CONTROL), ("{", _PARTIAL_QUERY_OBJECT)):
            at = text.rfind(opener)
            if at != -1 and partial.fullmatch(text, at):
                return at
        # The last word — everything after the last whitespace — may still be
        # growing. Hold it only if it could become tool syntax.
        ws = max(text.rfind(" "), text.rfind("\n"), text.rfind("\t"))
        tail = text[ws + 1:]
        if tail and _could_grow_into_tool_syntax(tail):
            return ws + 1
        return len(text)
