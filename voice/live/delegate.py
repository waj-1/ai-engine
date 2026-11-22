"""The ask_agent bridge: run the REAL agent, return something safe to speak.

The realtime model never answers a substantive question itself. It calls
`ask_agent`, and this module runs the same `run_turn` the text chat uses —
same profile, same prompt, same MCP tools, same charts, same persistence and
telemetry — then hands back a `spoken` string the model is instructed to read
verbatim.

Failures are contained, never raised: a voice session that drops dead because
a tool timed out is worse than one that says "I couldn't pull that up".

Two channels for an agent-authored `spoken_summary`, one live and one dormant:
`run_turn` (`common/chat_session.py`) captures an agent's `turn_meta` event
for its own telemetry/memory bookkeeping and never re-yields it to its
caller — see the `# consumed by run_turn, not forwarded` comments in
the product agents — so the `turn_meta` branch below is
currently dead in production; it's kept because it costs nothing and
reactivates for free if that contract ever changes. The one route that IS
structurally open is a *top-level* event: `run_turn`'s dispatch loop only
special-cases a fixed set of `type`s and forwards everything else verbatim
(a bare `yield event` at the bottom of its loop), so an agent that wants its
`spoken_summary` to reach the voice layer today must emit a top-level
`{"type": "spoken_summary", "content": "..."}` event, not a `turn_meta` one.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from common.chat_session import run_turn
from common.screen_claims import CARD_REFERENCE, drop_screen_claims
from core.observability import root_span
from core.tool_provenance import TurnProvenance, current as current_provenance
from core.tool_provenance import (
    UNSOURCED_FIGURES_LINE, claims_prepared_change, line_for, unprepared_change_line,
    turn_provenance, unsourced_figures,
)
from voice.chunker import SentenceChunker
from voice.live.speakable import guard_identifiers, on_screen_for, scrub_internal_ids, to_speakable

logger = logging.getLogger(__name__)

# The prefetch runs BEFORE the turn's own timeout guard, so it needs its
# own bound: a hung embedding or RPC would otherwise block the turn with
# nothing to stop it. Measured retrieval is ~122ms p50 (82ms embed + ~40ms
# vector search), so this is ~16x headroom — and past ~8s the realtime
# service has already discarded the tool call, making a longer wait
# worthless. Deliberately NOT an operator lever: a bound settable to 0 or
# to something huge would reintroduce exactly the silent-when-wrong
# failure this change exists to remove.
_PREFETCH_TIMEOUT_SEC = 2.0

# Per-stage timing on the delegated turn. `delegate_ms` is one aggregate
# number, and diagnosing "retrieval is slow" out of it meant raw ClickHouse
# spelunking — so the delegate threads a plain dict through `extra_context`
# (`stage_timings`), lets each stage owner add its wall time (`run_turn`'s
# history load in common/chat_session.py; the memory/LLM/tool stages in the
# agent's own tool loop), and stamps whatever accumulated onto
# the `voice.agent_turn` root span at turn end under this prefix. The
# conventions are voice/live/metrics.py's exactly: numbers are NUMERIC span
# attributes under `waj.voice.live.*`, read in ClickHouse with
# simpleJSONExtractFloat — never span durations, and never NEW spans
# (point-span noise was a deliberate past decision; these ride the one root
# span the delegate already opens). Every write is guarded: a timing failure
# must never break a turn, and when nothing fills the dict nothing is stamped.
_STAGE_ATTR_PREFIX = "waj.voice.live.stage."


def _stamp_stage_timings(span: Any, timings: Dict[str, Any]) -> None:
    """Best-effort: stamp accumulated stage timings onto the open root span.

    Called while the span is still open (an attribute set after close is
    dropped), on every exit path — the normal `done` break, an agent
    exception, and the wait_for cancellation alike — so a timed-out turn
    still reports the stages that completed, which is exactly the turn
    whose breakdown you want.
    """
    if span is None or not timings:
        return
    try:
        set_attribute = getattr(span, "set_attribute", None)
        if set_attribute is None:
            return
        for key, value in timings.items():
            if value is None:
                continue
            try:
                set_attribute(
                    _STAGE_ATTR_PREFIX + str(key),
                    value if isinstance(value, (str, int, float, bool)) else str(value),
                )
            except Exception:
                continue
    except Exception:
        logger.debug("voice delegate: stage-timing stamp failed", exc_info=True)


# Spoken when the agent errored or timed out. This is the ONE case where the
# voice layer speaks words the agent didn't author — it states a failure and
# asserts no facts, so it cannot misreport a figure.
_FAILURE_SPOKEN = {
    "en": "Sorry, I couldn't pull that up just now.",
    "ar": "أعتذر، ما قدرت أجيب لك هذي المعلومة الحين.",
}
FAILURE_SPOKEN = _FAILURE_SPOKEN["en"]

# Spoken INSTEAD of a withheld answer: one shared copy with the business
# agent (core.tool_provenance). Kept as functions here for the callers.
def unbacked_claim_spoken_for(language: Optional[str] = None, kind: Optional[str] = None) -> str:
    return unprepared_change_line(language, kind)


def unsourced_figures_spoken_for(language: Optional[str] = None) -> str:
    return line_for(UNSOURCED_FIGURES_LINE, language)


def failure_spoken_for(
    language: Optional[str] = None, *, override: Optional[str] = None
) -> str:
    """The failure line for `language`, or an operator's own wording.

    Per-language because this is spoken on EVERY error, timeout and
    empty-answer path — the moments a caller is already unsure — and an
    English apology mid-Arabic call reads as a second failure. Same
    primary-subtag keying and same English fallback as the rest of the module.
    """
    if override:
        return override
    primary = (language or "").split("-")[0].strip().lower()
    return _FAILURE_SPOKEN.get(primary, _FAILURE_SPOKEN["en"])


# "It's on your screen" / «موجودة على شاشتك»: one pattern shared with the
# agents' brains and the eval checks (common/screen_claims.py). The voice brain
# is told to keep tables off the spoken channel, so on a data turn it said
# this while `display` carried nothing but the same prose (user voice test
# 2026-09-30: three data answers, an empty screen each time).
_VISUAL = re.compile(r"^\s*\|.*\|.*$|```", re.M)


@dataclass
class DelegateResult:
    spoken: str
    display: str
    charts: List[Dict[str, Any]] = field(default_factory=list)
    error: bool = False
    # Sentences already handed to `on_sentence` (and therefore already spoken).
    # The caller uses this to avoid delivering `spoken` a second time, which
    # would make the assistant repeat the whole answer after streaming it.
    streamed: List[str] = field(default_factory=list)
    # Protected-flow markers the turn's tools recorded ("send_otp",
    # "verify_otp", "verify_otp_ok") — drained from the agent's per-session
    # flow-event channel. The relay-mode endpoint keys flow custody on these;
    # empty means the turn touched no protected flow.
    flow_signals: List[str] = field(default_factory=list)
    # Whole `{"type": "proposal", "proposal": {...}}` events the agent
    # surfaced via its proposal channel (an agent's extract_proposals). Wire
    # shape kept verbatim so the endpoint can forward frames the browser
    # already parses. Proposals are validated suggestions for the USER
    # to approve on screen — they are never executed by the voice layer,
    # which is why they are returned on the error path too.
    proposals: List[Dict[str, Any]] = field(default_factory=list)
    # Whole `{"type": "links", …}` events (a CSV the merchant asked for) —
    # forwarded as `links` frames, never spoken; they back a screen claim.
    links: List[Dict[str, Any]] = field(default_factory=list)
    # Set from the agent's `voice_turn_stats` event. These are the numbers
    # spec §6 grades the change on: how often the prefetch hit, and how often
    # the turn ended without a synthesis call.
    prefetch_hit: bool = False
    rendered_tool: Optional[str] = None
    # True when `rendered_tool`'s turn ended on a FAILED call's truthful
    # decline (an agent's deterministic failure render)
    # rather than a successful render — same event, a second field, so an
    # operator watching `waj.voice.live.rendered_tool` can tell "sped up a
    # success" apart from "a live tool call failed and we spoke the truth
    # about it" instead of the two looking identical on that one attribute.
    rendered_failure: bool = False
    # How many account-number-shaped runs this turn wanted to say that no
    # tool result and no caller utterance vouched for. Non-zero means the
    # brain fabricated an identifier and the guard withheld it — the state
    # a live Arabic call reached on 2026-08-19 with nothing to stop it. Kept
    # on the result (not just in the log) so an endpoint or a scenario run
    # can assert on it without scraping stderr.
    #
    # A DETERMINISTICALLY RENDERED turn (`rendered_tool` set) is guarded like
    # any other — the rendered digits come from the tool result, so the tool
    # result has to be in the corpus before the render ends the turn. See the
    # `record_tool_result` call ahead of an agent's render shortcut.
    identifiers_suppressed: int = 0
    # Business figures (money / % / counts) the answer stated from a turn
    # with ZERO tool results — withheld, and the whole answer replaced by a
    # no-fact line. Only agents that opt in (`voice_requires_figure_provenance`)
    # are checked. Live 2026-09-28: "Today's revenue is
    # AED 4,320" with no data call, for a business with no bookings.
    figures_suppressed: int = 0
    # True when the answer claimed a change/card was prepared but the turn
    # produced no proposal — withheld and replaced (opt-in:
    # `voice_requires_action_provenance`).
    action_claim_suppressed: bool = False
    # On-screen claims ("it's on your screen") removed because the turn sent
    # nothing visual — no table, no chart, no card (opt-in:
    # `voice_screen_claims_need_backing`).
    screen_claims_suppressed: int = 0


def _with_turn_provenance(fn: Callable[..., Awaitable["DelegateResult"]]):
    """Open one `TurnProvenance` around a delegated turn.

    A decorator rather than a `with` block inside the function purely so the
    turn body below stays at one indent level and diffable — the recorder has
    to be installed BEFORE `asyncio.wait_for` creates the task that runs the
    agent, because that is the moment the context is copied into the child
    (see `core/tool_provenance.py`). Everything the agent's tool loop records
    from inside that task lands on the object this yields.
    """

    @functools.wraps(fn)
    async def _wrapped(*args: Any, **kwargs: Any) -> "DelegateResult":
        with turn_provenance():
            return await fn(*args, **kwargs)

    return _wrapped


@_with_turn_provenance
async def run_delegated_turn(
    *,
    agent: Any,
    profile: Any,
    message: str,
    identity: Any,
    timeout_sec: float = 20.0,
    on_sentence: Optional[Callable[[str], Awaitable[None]]] = None,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    language: Optional[str] = None,
    failure_spoken: Optional[str] = None,
    on_screen: Optional[str] = None,
    trace_attributes: Optional[Dict[str, Any]] = None,
    extra_context: Optional[Dict[str, Any]] = None,
    prefetch_enabled: bool = False,
    prefetch_match_count: int = 3,
    deterministic_render: bool = False,
) -> DelegateResult:
    """Run one real agent turn and reduce it to {spoken, display, charts}.

    `trace_attributes` are the `waj.*` root-span attributes the caller built
    with `root_span_attrs` (agent_kind, channel, env, session_id). The HTTP
    chat endpoints get their tagged request-root span from `sse_response`; a
    delegated voice turn has no HTTP request, so the equivalent root is opened
    here. Without it the turn's LLM/tool spans land as separate UNTAGGED
    top-level traces, which a scoped build's trace explorer drops (it keeps only
    rows tagged with the scope's agent_kind) — voice traffic then shows
    nothing at all in /admin observability.

    `on_sentence`, when given, is awaited with each COMPLETE sentence of the
    agent's answer as it streams, before the turn finishes. Sentences are
    handed over verbatim -- this is still the reader contract, just delivered
    in pieces.

    What that is worth is smaller than it sounds, and the number matters
    because people keep tuning the chunker for it: measured over 85 streamed
    calls on a client demo VM (2026-08-19), the first sentence went out a
    median of 68ms ahead of the whole answer, flat across 1-, 2- and
    3-sentence answers. The brain does not dribble the answer out -- probed
    the same day against the same gpt-5-mini row, it spends 1.0-1.6s producing
    nothing and then emits the ENTIRE answer in 154-539ms. Streaming can only
    ever win that burst, and it already wins most of it; the long answers are
    where the rest of it is (a 10-sentence answer led by 3.7s). See the
    `stream_sentences` comment in voice/live/config.py.

    A failing `on_sentence` never aborts the turn: partial speech is a
    nice-to-have, the complete `spoken` in the return value is the guarantee.

    `on_progress`, when given, is awaited with each of the agent's own
    `thought` events ("Using tool: …", "Retrieved data from …") as the turn
    runs — already localized display strings, never spoken. The endpoint
    forwards them as `agent_status` frames so a client can show live
    thinking/tool activity while the caller waits. Same containment contract
    as `on_sentence`: a failing callback is logged and the turn continues.

    `language` (BCP-47) selects the per-language wording `to_speakable`
    uses (the on-screen pointer) and is carried on the result. Identifier
    MASKING used to live on this path — it was removed on 2026-08-19 when
    a client's screen began mirroring the spoken transcript, at which
    point masking hid the number in BOTH channels instead of moving it
    from ear to screen. `to_speakable` still rounds absurd decimal
    precision for speech, which is a separate, non-overlapping shape.

    What replaced masking, later the same day and for a much worse symptom,
    is the PROVENANCE guard: an account-number-shaped run may be spoken only
    if this turn's tool results or the caller's own words contain those
    digits. Masking withheld every long identifier including the correct
    one, which is why it had to go; the guard withholds only the ones
    nothing vouches for, so "what IBAN is on file?" still gets answered in
    full — it just cannot be answered with an invented number, which is what
    a live Arabic call did at 12:33 on 2026-08-19 (twenty-two digits
    matching neither the merchant's file nor what he had just dictated).
    See `core/tool_provenance.py` for what counts as vouching, and
    `speakable.guard_identifiers` for what happens when nothing does.

    `prefetch_enabled`, when true, calls the agent's own
    `prefetch_voice_context(message, match_count=prefetch_match_count)`
    hook BEFORE `run_turn` starts, so a documentation question can be
    answered in one LLM round trip instead of two; the result (or its
    absence) lands in `extra_context["prefetched_docs"]`. Bound by
    `_PREFETCH_TIMEOUT_SEC` and contained like every other callback here — a
    missing hook, a `None`/empty result, a raise or a timeout all leave the
    turn exactly as it would run with prefetch off. `deterministic_render`
    is passed straight through to `extra_context["deterministic_render"]`
    (always set, never gated on prefetch) for the agent to consult.

    The two interact, and the order is load-bearing. Prefetched KB text is
    NOT provenance — it is retrieved context, never a tool result, so it
    vouches for nothing and is never recorded. A deterministically rendered
    answer, by contrast, is built FROM a tool result and therefore depends
    on that result already being in the corpus: an agent that renders and
    ends its loop must record the result BEFORE it breaks, or the guard
    withholds the very digits the renderer exists to speak exactly.
    """
    failure_line = failure_spoken_for(language, override=failure_spoken)
    on_screen_line = on_screen_for(language, override=on_screen)
    # Installed by the decorator; the fallback keeps a direct call to the
    # undecorated function (a test importing the inner object) working
    # rather than raising on a None.
    provenance: TurnProvenance = current_provenance() or TurnProvenance()
    # The caller's own utterance is provenance for HIS digits. `run_turn`
    # adds the session's earlier user rows in order (see its
    # `record_caller_text` block) — the dictation flow needs both, because a
    # merchant reading out an IBAN pauses between groups and the 22-digit
    # read-back he is owed spans several messages.
    provenance.record_caller_text(message)
    # De-duplicated by DIGITS, not counted per pass: one turn is guarded
    # several times over (each streamed sentence, the finished spoken text,
    # the screen text) and the same fabricated number showing up in three of
    # them is one fabrication.
    withheld_digits: set[str] = set()
    # Figure provenance (see `DelegateResult.figures_suppressed`). Opt-in per
    # agent: an agent without a data-tool loop (a docs-only persona, a bot
    # quoting a price list in its prompt) would otherwise lose legitimate
    # figures. Once one figure is withheld the rest of the answer is not
    # streamed either — the prose around an invented number ("up compared
    # with last week") is invented with it.
    figure_guard = bool(getattr(agent, "voice_requires_figure_provenance", False))
    figures = {"withheld": 0}

    # Action provenance (V7): a claim that a change is prepared needs a
    # proposal from THIS turn. Same opt-in / latch shape as the figures.
    action_guard = bool(getattr(agent, "voice_requires_action_provenance", False))
    claims = {"withheld": False}

    def _unbacked_claim(text: str) -> bool:
        # A card from an earlier turn of this call is on screen: "it's ready
        # to review — tap Confirm" is then the reminder our prompts ask for,
        # not a claim (re-review round 3).
        if not action_guard or proposals or (
                getattr(agent, "voice_proposals_made", 0)
                and not getattr(agent, "voice_turn_new_change", False)):
            return False
        if claims["withheld"]:
            return True
        if not claims_prepared_change(text):
            return False
        claims["withheld"] = True
        logger.warning(
            "voice delegate: withheld an answer claiming a prepared change "
            "from a turn that produced no proposal"
        )
        return True

    def _unsourced(text: str) -> bool:
        if not figure_guard:
            return False
        if figures["withheld"] and not provenance.tool_results:
            return True
        bad = unsourced_figures(
            text, tool_results=provenance.tool_results, caller_text=message,
            # Figures an earlier tool-backed turn of this call produced
            # (the agent keeps them; see BusinessRuntimeAgent).
            vouched=getattr(agent, "voice_vouched_figures", None) or (),
        )
        if not bad:
            return False
        figures["withheld"] = len(bad)
        logger.warning(
            "voice delegate: withheld an answer stating %d business figure(s) "
            "from a turn with no tool result", len(bad),
        )
        return True

    # Internal record ids (UUIDs) are never spoken or shown — structural, not
    # a prompt rule (live 2026-09-29: "Branch ID: ea22654d-… — it's on your
    # screen"). Opt-in per agent: `voice_scrub_internal_ids`.
    scrub_ids = bool(getattr(agent, "voice_scrub_internal_ids", False))

    # On-screen claims need something ON the screen (opt-in). A table or a
    # code block in the answer, a chart, or a card this turn back it. A card
    # from earlier in the call backs only a claim about that card ("review it
    # and tap Confirm"), not a later, unrelated data claim.
    screen_guard = bool(getattr(agent, "voice_screen_claims_need_backing", False))
    screen = {"dropped": 0}

    def _screen_backed(text: str) -> bool:
        if charts or proposals or links or _VISUAL.search("".join(answer_parts)):
            return True
        return (bool(getattr(agent, "voice_proposals_made", 0))
                and not getattr(agent, "voice_turn_new_change", False)
                and bool(CARD_REFERENCE.search(text or "")))

    def _screen(text: str, *, allow_empty: bool = False) -> str:
        """`allow_empty` for one streamed sentence: the rest of the answer
        carries it. The whole answer is never emptied (the failure line)."""
        if not screen_guard or _screen_backed(text):
            return text
        out, dropped = drop_screen_claims(text, allow_empty=allow_empty)
        if dropped:
            # One claim is usually seen several times (a streamed sentence,
            # the spoken text, the display): count the most any one text had.
            screen["dropped"] = max(screen["dropped"], dropped)
            logger.warning(
                "voice delegate: dropped %d on-screen claim(s) from a turn that sent "
                "nothing to the screen", dropped,
            )
        return out

    def _guard(text: str, *, regroup: bool = True) -> str:
        """Withhold anything account-shaped this turn cannot account for."""
        if scrub_ids:
            text = scrub_internal_ids(text)
        guarded, dropped = guard_identifiers(
            text, verified=provenance.verifies, language=language, regroup=regroup,
        )
        for digits in dropped:
            if digits not in withheld_digits:
                withheld_digits.add(digits)
                # WARNING, not debug: this only ever fires when the brain
                # tried to state an account number nothing in the turn
                # produced. The digits themselves stay OUT of the log — they
                # are the one thing in this event that is definitely wrong,
                # and log lines get pasted into tickets and read as fact.
                logger.warning(
                    "voice delegate: withheld an unverified %d-digit "
                    "identifier — %d tool result(s) recorded this turn",
                    len(digits), provenance.tool_results,
                )
        return guarded

    answer_parts: List[str] = []
    streamed: List[str] = []
    chunker = SentenceChunker()
    charts: List[Dict[str, Any]] = []
    proposals: List[Dict[str, Any]] = []
    links: List[Dict[str, Any]] = []
    spoken_summary = ""
    prefetch_hit = False
    rendered_tool: Optional[str] = None
    rendered_failure = False
    errored = False
    error_message = ""
    # voice_turn is applied AFTER the merge: it is the delegate's own
    # contract with the agents, and a caller-supplied context must not be
    # able to switch it off.
    turn_extra: Dict[str, Any] = dict(extra_context or {})
    turn_extra["voice_turn"] = True
    turn_extra["deterministic_render"] = bool(deterministic_render)
    # Per-stage wall-time accounting (see _stamp_stage_timings above). Set
    # AFTER the merge like voice_turn: it is the delegate's channel, and a
    # caller-supplied context must not be able to substitute its own dict.
    stage_timings: Dict[str, Any] = {}
    turn_extra["stage_timings"] = stage_timings

    # Retrieve BEFORE the brain's first call so a documentation question costs
    # one LLM round trip instead of two. Contained absolutely: a missing hook,
    # a None result or any exception leaves the turn exactly as it was, with
    # the agent's own retrieval tool still bound and callable.
    if prefetch_enabled:
        _hook = getattr(agent, "prefetch_voice_context", None)
        if _hook is not None:
            _prefetch_started = time.monotonic()
            try:
                _prefetched = await asyncio.wait_for(
                    _hook(message, match_count=prefetch_match_count),
                    timeout=_PREFETCH_TIMEOUT_SEC,
                )
            except Exception:
                # Covers both a raising hook and asyncio.TimeoutError (on
                # 3.11 an alias of the builtin TimeoutError, a subclass of
                # Exception) from the wait_for bound above.
                logger.debug("voice delegate: prefetch failed", exc_info=True)
                _prefetched = None
            # Recorded on the failure/timeout path too — a prefetch that
            # burned its full _PREFETCH_TIMEOUT_SEC bound is precisely the
            # cost this attribute exists to make visible.
            try:
                stage_timings["prefetch_ms"] = round(
                    (time.monotonic() - _prefetch_started) * 1000.0, 1
                )
            except Exception:
                logger.debug("voice delegate: prefetch timing failed", exc_info=True)
            if _prefetched:
                turn_extra["prefetched_docs"] = _prefetched

    async def _flush_tail() -> None:
        """Speak whatever the chunker still holds. Idempotent (flush clears).

        Called the MOMENT the agent's generation is over rather than after
        `run_turn` finishes, because those are not the same instant: run_turn
        awaits `asyncio.gather(_persist_transcript(), _emit_telemetry())`
        before it yields `done` (see common/chat_session.py — prod measured
        ~0.58s p50 of post-answer bookkeeping). Flushing after the loop put
        the answer's LAST sentence behind two Supabase round-trips that it
        does not depend on. Measured on a client demo VM 2026-08-19 (local
        Supabase, so a best case): the tail sat 62ms behind the stream there.
        """
        if on_sentence is None or errored:
            return
        tail = _screen(_guard(to_speakable(chunker.flush(), on_screen=on_screen_line)), allow_empty=True)
        if not tail.strip() or not any(c.isalnum() for c in tail):
            return
        if _unsourced(tail) or _unbacked_claim(tail):
            return
        if streamed and streamed[-1] == tail:
            return
        try:
            await on_sentence(tail)
        except Exception:
            logger.debug("voice delegate: final on_sentence failed", exc_info=True)
        else:
            streamed.append(tail)

    async def _consume() -> None:
        nonlocal spoken_summary, prefetch_hit, rendered_tool, rendered_failure, errored, error_message
        with contextlib.ExitStack() as _stack:
            _turn_span = _stack.enter_context(
                root_span("voice.agent_turn", trace_attributes)
            )
            # Registered AFTER the span is entered, so ExitStack's LIFO
            # unwinding runs the stamp BEFORE the span closes — on the
            # normal `done` break, an exception, and the wait_for
            # cancellation alike (see _stamp_stage_timings).
            _stack.callback(
                lambda: _stamp_stage_timings(_turn_span, stage_timings)
            )
            async for event in run_turn(
                agent=agent, message=message, identity=identity, profile=profile,
                # Agents key off voice_turn to skip the title/suggestions LLM
                # round-trips: chips are untappable on a call, and both calls are
                # awaited inside the turn, squarely in time-to-first-speech.
                # The rest of turn_extra is caller-supplied per-connection
                # context (the record/screen the client is on) — see the
                # extra_context parameter.
                extra_context=turn_extra,
            ):
                etype = event.get("type")
                if etype == "answer":
                    chunk = event.get("content") or ""
                    answer_parts.append(chunk)
                    if on_sentence is not None and chunk:
                        for sentence in chunker.feed(chunk):
                            # Guarded HERE, not once over the finished answer:
                            # a streamed sentence is already in the caller's
                            # ear by the time the answer is assembled, so a
                            # check that only ran at the end would protect
                            # nothing on the providers that stream.
                            speakable = _screen(_guard(
                                to_speakable(sentence, on_screen=on_screen_line)
                            ), allow_empty=True)
                            # Streaming-only hygiene the whole-answer path gets for
                            # free by transforming ONCE over the full text:
                            #  - a sentence with no letters/digits (a stray fence
                            #    backtick, punctuation debris) is noise, skip it;
                            #  - consecutive identical speakables collapse -- a
                            #    3-row markdown table otherwise streams as
                            #    "The figures are on your screen." three times.
                            if not any(c.isalnum() for c in speakable):
                                continue
                            if streamed and streamed[-1] == speakable:
                                continue
                            if _unsourced(speakable) or _unbacked_claim(speakable):
                                continue
                            try:
                                await on_sentence(speakable)
                            except Exception:
                                # Delivery failed -- do NOT record it as streamed.
                                # The caller falls back to whole-answer delivery
                                # keyed on `streamed`, so recording an undelivered
                                # sentence here would suppress that fallback and
                                # the provider would wait forever for a tool
                                # result that never comes (reviewed as Critical).
                                logger.debug(
                                    "voice delegate: on_sentence failed; "
                                    "falling back to the whole answer",
                                    exc_info=True,
                                )
                            else:
                                streamed.append(speakable)
                elif etype == "thought":
                    if on_progress is not None:
                        text = str(event.get("content") or "").strip()
                        if text:
                            try:
                                # Clamp: a thought is a one-line status, and
                                # this rides the same socket as audio frames.
                                await on_progress(text[:200])
                            except Exception:
                                logger.debug(
                                    "voice delegate: on_progress failed",
                                    exc_info=True,
                                )
                elif etype == "chart":
                    charts.append(event)
                elif etype == "observation":
                    # Some agents already surface every tool payload as a
                    # top-level `observation`
                    # event, and `run_turn` forwards any type it doesn't
                    # special-case — so this is provenance for free on those
                    # agents, and a second net under the explicit
                    # `record_tool_result` calls for any agent that grows a
                    # tool loop without one. Truncated to 500 chars at the
                    # source, hence a supplement and not a replacement.
                    provenance.record_tool_result(event.get("result"))
                elif etype == "proposal":
                    proposals.append(event)
                elif etype == "links" and event.get("files"):
                    links.append(event)
                elif etype == "turn_meta":
                    # Dormant today (see module docstring) — `run_turn` doesn't
                    # forward `turn_meta`. Kept in case that contract changes.
                    summary = (event.get("metadata") or {}).get("spoken_summary")
                    if summary:
                        spoken_summary = str(summary).strip()
                elif etype == "spoken_summary":
                    # The live channel: a top-level event type `run_turn` doesn't
                    # special-case, so its dispatch loop forwards it unchanged.
                    summary = event.get("content")
                    if summary:
                        spoken_summary = str(summary).strip()
                elif etype == "voice_turn_stats":
                    # Also a forwarded-verbatim top-level event (Task 8) --
                    # only an agent with KB prefetch yields it; every other agent leaves
                    # these at their defaults, not an error.
                    prefetch_hit = bool(event.get("prefetch_hit"))
                    rendered_tool = event.get("rendered_tool") or None
                    rendered_failure = bool(event.get("rendered_failure"))
                elif etype == "usage":
                    # Every streaming agent in this repo yields `usage` right
                    # after its final answer text and before run_turn's
                    # persistence gather — the earliest event that means
                    # "generation is over". Flush here so the answer's last
                    # sentence is not charged the bookkeeping (see
                    # _flush_tail). An agent that never emits `usage` simply
                    # falls through to the post-loop backstop below, i.e. to
                    # today's behaviour.
                    await _flush_tail()
                elif etype == "error":
                    errored = True
                    error_message = str(event.get("message") or "agent error")
                elif etype == "done":
                    await _flush_tail()
                    break

    try:
        await asyncio.wait_for(_consume(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        errored = True
        error_message = f"agent turn exceeded {timeout_sec}s"
        logger.warning("voice delegate: %s", error_message)
    except Exception as exc:  # provider/agent blew up — contain it
        errored = True
        # Log the real exception detail server-side only. `error_message`
        # feeds `display` below, which reaches the client-facing transcript
        # -- raw internal exception text (stack traces, repr of internal
        # objects, DB error strings) has no business landing in front of a
        # caller on a live demo.
        error_message = "agent turn failed"
        logger.exception("voice delegate: agent turn failed (%s: %s)", type(exc).__name__, exc)

    # Backstop, for an agent that emits neither `usage` nor `done`. Keeps the
    # old guarantee whatever the agent's event vocabulary, and is idempotent:
    # on the common path the in-loop call already emptied the chunker, so this
    # is a no-op. (A timeout/exception sets `errored`, and `_flush_tail`
    # declines then — same as before: a half-generated sentence is not an
    # answer, and the failure line is spoken instead.)
    await _flush_tail()

    # Drain the agent's per-session flow-event channel exactly once per
    # turn — on the error path too: a failed verify still opened the flow,
    # and losing the signal would strand relay-mode custody.
    flow_signals: List[str] = []
    events = getattr(agent, "flow_events", None)
    if isinstance(events, list) and events:
        flow_signals = list(events)
        events.clear()

    raw_display = "".join(answer_parts).strip()
    # The screen gets the same withholding as the ear, and for the same
    # reason: a client screen may mirror the spoken transcript, the endpoint
    # pushes `display` besides, and a number too dangerous to say is too
    # dangerous to render. `regroup=False` — the agent's own layout is fine
    # on a screen; only the withholding matters there. (The PERSISTED
    # transcript is written inside `run_turn` from the agent's own answer
    # events and is deliberately untouched: what the model actually said is
    # the evidence you want when working out why it said it.)
    display = _guard(raw_display, regroup=False)
    if not errored and _unbacked_claim(raw_display):
        line = unbacked_claim_spoken_for(language, getattr(agent, "voice_pending_change_kind", None))
        return DelegateResult(
            spoken=line, display=line, charts=charts, error=False, streamed=[],
            flow_signals=flow_signals, proposals=proposals, links=links,
            prefetch_hit=prefetch_hit, rendered_tool=rendered_tool,
            rendered_failure=rendered_failure,
            identifiers_suppressed=len(withheld_digits),
            figures_suppressed=figures["withheld"], action_claim_suppressed=True,
        )
    if not errored and _unsourced(raw_display):
        # The screen must not show the invented figure either, and nothing
        # already streamed counts: `streamed` is emptied so the endpoint
        # delivers this line (a prefix that got out was figure-free).
        line = unsourced_figures_spoken_for(language)
        return DelegateResult(
            spoken=line, display=line, charts=charts, error=False, streamed=[],
            flow_signals=flow_signals, proposals=proposals, links=links,
            prefetch_hit=prefetch_hit, rendered_tool=rendered_tool,
            rendered_failure=rendered_failure,
            identifiers_suppressed=len(withheld_digits),
            figures_suppressed=figures["withheld"],
        )
    if errored:
        # `error_message` is for the log only. It is whatever the agent put in
        # its error event, and one agent used to put str(e) there —
        # Azure's raw 400 body reached the caller's screen on 2026-09-30.
        logger.warning("voice delegate: turn failed (%s)", error_message[:200])
        return DelegateResult(
            spoken=failure_line,
            display=display or failure_line,
            charts=charts,
            error=True,
            flow_signals=flow_signals,
            proposals=proposals, links=links,
            prefetch_hit=prefetch_hit,
            rendered_tool=rendered_tool,
            rendered_failure=rendered_failure,
            identifiers_suppressed=len(withheld_digits),
        )
    if spoken_summary:
        # Still model output, and the one string the delegate speaks without
        # rendering it through `to_speakable` — so it gets the check too.
        spoken = _guard(spoken_summary)
    elif not raw_display and charts:
        # Charts-only turn: to_speakable("") is "" — there's no answer text
        # for the deterministic transform to render, so a naive fallback
        # would speak nothing while data appears on screen (reads as a
        # broken assistant in a live demo). ON_SCREEN asserts no facts —
        # it's the same fact-safe pointer to_speakable itself appends when
        # an answer contains a table/code block — so it stays inside the
        # verbatim-safety contract.
        spoken = on_screen_line
    else:
        # Guarded from the RAW answer, not the already-guarded `display`, so
        # a verified identifier is regrouped from the digits the agent wrote
        # rather than from a string a previous pass has been through.
        spoken = _guard(to_speakable(raw_display, on_screen=on_screen_line))

    spoken = _screen(spoken)
    display = _screen(display)
    if (screen_guard and on_screen_line and spoken.endswith(on_screen_line)
            and _VISUAL.search(raw_display)):
        # A table went to the screen and `to_speakable` appended the pointer:
        # the model's own "it's on your screen" is then said twice (voice
        # eval 2026-10-01). Keep the pointer, drop the model's copy.
        body, _dropped = drop_screen_claims(spoken[: -len(on_screen_line)].rstrip(), allow_empty=True)
        spoken = f"{body} {on_screen_line}".strip()

    if not spoken.strip():
        # The agent completed without raising but produced nothing to say and
        # nothing to show. Handing the model an empty `spoken` makes it read
        # nothing: the caller hears dead air with no error, and the transcript
        # pane gets an empty bubble. Reproduced against a real client profile
        # whose MCP tools were unbound — the agent logged "resolved to ZERO
        # tools" and returned an empty answer, which is a misconfiguration
        # this repo has shipped before. Treat it as the failure it is.
        logger.warning(
            "voice delegate: agent produced no spoken text and no charts; "
            "reporting failure rather than speaking nothing"
        )
        return DelegateResult(
            spoken=failure_line, display=display, charts=charts, error=True,
            flow_signals=flow_signals, proposals=proposals, links=links,
            prefetch_hit=prefetch_hit, rendered_tool=rendered_tool,
            rendered_failure=rendered_failure,
            identifiers_suppressed=len(withheld_digits),
        )

    return DelegateResult(
        spoken=spoken,
        display=display,
        charts=charts,
        error=False,
        streamed=list(streamed),
        flow_signals=flow_signals,
        proposals=proposals, links=links,
        prefetch_hit=prefetch_hit,
        rendered_tool=rendered_tool,
        rendered_failure=rendered_failure,
        identifiers_suppressed=len(withheld_digits),
        screen_claims_suppressed=screen["dropped"],
    )
