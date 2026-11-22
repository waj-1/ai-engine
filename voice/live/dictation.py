"""Server-side dictation capture: the endpoint, not the model, holds a value
the caller reads out across several turns.

WHY. A long identifier dictated in groups ("forty-four, twenty, oh one…")
otherwise lives only in the realtime model's own context: re-spoken, and so
re-generated and re-riskable, on every pause. On live calls that produced
three different numbers (what the caller said, what the model spoke back,
what the screen showed), miscounts nobody could audit (a collection turn
makes no tool call), and an ASR hallucination folded into the number. A
collector held by the endpoint answers "what does the buffer hold, what should
be said, can this fragment be trusted" as pure functions of the caller's own
words, and the endpoint speaks the answer through the same say-exactly path
relay custody uses. Deterministic renders ran about an order of magnitude
faster than a model-generated tally.

WHAT THE ENGINE OWNS. `endpoints/voice_live.py` owns the delivery: when the
collector opens (a delegated turn's `flow_signals` carrying the collector's
`open_signal`), feeding it every committed caller utterance, the
cancel-then-say-exactly delivery, the fail-safe that closes a read-back the
caller never heard, and the precedence rules against relay custody. The
switch is `voice_config.server_side_iban_capture` (default off).

WHAT A PLUGIN SUPPLIES. The collector itself: what is being dictated, how
many digits it has, how the tally and read-back are worded in each language,
and the message that hands the confirmed value to the agent's own tool. It
comes from the ``voice_capture`` plugin hook, whose ``dictation_capture()``
returns an object with:

  ``name``         short label for metrics (``<name>-<n>`` tool timers and
                   ``<name>_<action>`` modes);
  ``open_signal``  the flow signal that opens the collector;
  ``new_state()``  a fresh per-session state with ``open``, ``confirming``,
                   ``digits`` and ``reset()``;
  ``open_capture(state, *, now)``;
  ``feed(state, utterance, *, language, is_backchannel, now)`` returning a
                   decision with ``action`` (``ignore`` / ``close`` /
                   ``tally`` / ``confirm`` / ``commit``), ``spoken``,
                   ``digits`` and ``message``.

A build with no collector never opens one: the flag on such a build logs once
and the persona's own model-held tally runs, exactly as if it were off.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# The decisions that mean "the collector answered this commit itself".
CLAIMING_ACTIONS = ("tally", "confirm", "commit")


@dataclass
class ClosedCaptureState:
    """The state a session holds when its build has no collector: never open."""

    open: bool = False
    confirming: bool = False
    digits: str = ""

    def reset(self) -> None:
        self.open = False
        self.confirming = False
        self.digits = ""


def dictation_capture() -> Optional[Any]:
    """The plugin's collector, or None when this build has none.

    Read per session (not cached at import) so a test can swap the plugin
    list. When several plugins supply one, the last plugin's answer wins.
    """
    from core.plugins import call_hook, load_hook

    found = [c for c in call_hook(load_hook("voice_capture"), "dictation_capture") if c]
    return found[-1] if found else None
