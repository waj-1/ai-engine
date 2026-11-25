"""The Call button's transcript (admin/static/voice-call.js), run in Node
against a minimal document: which rows a stream of wire frames leaves on
screen. Skipped where Node is not installed.

The first stream is what a call with a cascaded Azure Voice Live model sent
on 2026-11-24 (fragments joined here only where the split does not matter).
The old page wrote every fragment over the previous one and never closed the
agent's row, so the same call showed one "Agent: !" above three "You:" rows.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[2] / "admin" / "static" / "voice-call.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
function Node(tag) { this.tag = tag; this.children = []; this.parentNode = null;
  this.style = {}; this.attrs = {}; this._text = ""; }
Node.prototype = {
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
  insertBefore(c, ref) { c.parentNode = this;
    this.children.splice(this.children.indexOf(ref), 0, c); return c; },
  setAttribute(k, v) { this.attrs[k] = v; },
  get lastChild() { return this.children[this.children.length - 1]; },
  get textContent() { return this.tag === "#text" ? this._text
    : this._text + this.children.map(c => c.textContent).join(""); },
  set textContent(v) { this._text = v; if (this.tag !== "#text") this.children = []; },
};
const doc = { createElement: t => new Node(t), createTextNode: t => { const n = new Node("#text"); n._text = t; return n; },
  querySelector: () => null };
const win = {};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { window: win, document: doc });
const log = new Node("div"); log.ownerDocument = doc;
const t = win.VoiceCallTranscript(log);
const out = [];
for (const frames of JSON.parse(fs.readFileSync(0, "utf8"))) {
  for (const f of frames) t.handle(f);
  out.push(log.children.map(c => [c.attrs["data-voice-call-turn"], c.textContent]));
}
process.stdout.write(JSON.stringify(out));
"""


def _rows(*steps):
    """Feed each list of frames in turn; the rows on screen after each."""
    done = subprocess.run(
        ["node", "-e", _HARNESS, str(JS)], input=json.dumps(list(steps)),
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return [[tuple(r) for r in rows] for rows in json.loads(done.stdout)]


def _say(*parts, **extra):
    """An assistant response: fragments, the empty final, then turn_end."""
    frames = []
    for i, text in enumerate(parts):
        frame = {"type": "transcript", "role": "assistant", "text": text, "final": False}
        if i == 0 and "reply" in extra:
            frame["reply"] = extra["reply"]
        frames.append(frame)
    frames.append({"type": "transcript", "role": "assistant", "text": "", "final": True})
    end = {"type": "turn_end"}
    if "reply" in extra:
        end["reply"] = extra["reply"]
    frames.append(end)
    return frames


def _heard(text):
    return [{"type": "transcript", "role": "user", "text": text, "final": True},
            {"type": "interrupted", "reason": "input_end"}]


GREETING = _say("Hello", "!", " How", " can", " I", " assist", " you", " today", "?", reply=False)


def test_a_recorded_call_reads_as_alternating_turns():
    frames = (
        [{"type": "ready", "session_id": "s", "greeting": True}] + GREETING
        + _heard("Hello, can you hear me?")
        + _say("Yes", ",", " I", " can hear you.", " How can I help you today?", reply=True)
        + _heard("Great, how long does a technician visit take?")
        # The lookup: a response with no speech, then the answer as text
        # (`display`) and as speech.
        + [{"type": "turn_end", "reply": True},
           {"type": "agent_status", "text": "Using knowledge..."},
           {"type": "display", "text": "I'm sorry, I don't know how long a visit takes."},
           {"type": "interrupted", "reason": "answer_ready"}]
        + _say("I'm sorry,", " I don't know", " how long a visit takes.", reply=False)
        + _heard("Sorry stop there, thank you goodbye.")
        + _say("You're", " welcome.", " Goodbye", "!", reply=True)
    )
    assert _rows(frames)[0] == [
        ("assistant", "Agent: Hello! How can I assist you today?"),
        ("user", "You: Hello, can you hear me?"),
        ("assistant", "Agent: Yes, I can hear you. How can I help you today?"),
        ("user", "You: Great, how long does a technician visit take?"),
        # Once: the spoken transcript, not the `display` text as well.
        ("assistant", "Agent: I'm sorry, I don't know how long a visit takes."),
        ("user", "You: Sorry stop there, thank you goodbye."),
        ("assistant", "Agent: You're welcome. Goodbye!"),
    ]


def test_a_row_grows_as_its_fragments_arrive():
    first, second = _rows(GREETING[:2], GREETING[2:4])
    assert first == [("assistant", "Agent: Hello!")]
    assert second == [("assistant", "Agent: Hello! How can")]


def test_an_answer_in_two_responses_stays_one_row():
    frames = (_heard("What are your hours?")
              + _say("One moment.", reply=True) + _say("Eight to eight.", reply=False))
    assert _rows(frames)[0] == [
        ("user", "You: What are your hours?"),
        ("assistant", "Agent: One moment.\nEight to eight."),
    ]


def test_caller_text_that_arrives_after_the_reply_started_goes_above_it():
    """A recognizer that transcribes finished utterances only: the reply's
    first caption (`reply: true`) can beat the caller's own words."""
    reply = _say("Yes,", " I can hear you.", reply=True)
    during, after = _rows(
        GREETING + reply[:1],
        _heard("Can you hear me?") + reply[1:],
    )
    assert during == [
        ("assistant", "Agent: Hello! How can I assist you today?"),
        ("assistant", "Agent: Yes,"),
    ]
    assert after == [
        ("assistant", "Agent: Hello! How can I assist you today?"),
        ("user", "You: Can you hear me?"),
        ("assistant", "Agent: Yes, I can hear you."),
    ]


def test_caller_text_that_arrives_after_the_whole_reply_goes_above_it():
    frames = (GREETING + _say("Yes, I can hear you.", reply=True) + _heard("Can you hear me?")
              + _heard("Thanks.") + _say("You're welcome.", reply=True))
    assert _rows(frames)[0] == [
        ("assistant", "Agent: Hello! How can I assist you today?"),
        ("user", "You: Can you hear me?"),
        ("assistant", "Agent: Yes, I can hear you."),
        ("user", "You: Thanks."),
        ("assistant", "Agent: You're welcome."),
    ]


def test_a_failed_transcription_does_not_misplace_the_next_utterance():
    """The endpoint closes an utterance whose transcription failed with an
    empty user final. The next utterance belongs at the end."""
    frames = (_say("Sorry, I did not catch that.", reply=True)
              + [{"type": "transcript", "role": "user", "text": "", "final": True}]
              + _heard("I asked about my bill.") + _say("Of course.", reply=True))
    assert _rows(frames)[0] == [
        ("assistant", "Agent: Sorry, I did not catch that."),
        ("user", "You: I asked about my bill."),
        ("assistant", "Agent: Of course."),
    ]


def test_a_provider_that_never_marks_a_fragment_final():
    """Gemini Live: every fragment of both sides is non-final and there is no
    `reply`; `turn_end` closes the caller's row, the caller's next words close
    the agent's."""
    def part(role, text):
        return {"type": "transcript", "role": role, "text": text, "final": False}
    frames = [
        part("user", "What are "), part("user", "your hours?"),
        part("assistant", "Eight "), part("assistant", "to eight."), {"type": "turn_end"},
        part("user", "Thanks."),
        part("assistant", "You're welcome."), {"type": "turn_end"},
    ]
    assert _rows(frames)[0] == [
        ("user", "You: What are your hours?"),
        ("assistant", "Agent: Eight to eight."),
        ("user", "You: Thanks."),
        ("assistant", "Agent: You're welcome."),
    ]
