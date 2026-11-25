/* Test call for an agent that is called at its own address
 * (WS /v1/agents/{slug}/voice) — the Voice tab's "Call" button.
 *
 * The call itself is the shared duplex client (static/shared/voice-live.js):
 * same socket, same wire protocol as every other voice front end. This file
 * only builds the address, hands the operator's token over as the first
 * `auth` frame (a browser cannot set a header on a WebSocket), pins the
 * profile being viewed so a draft can be called, and shows the transcript.
 */
(function () {
  "use strict";
  /* The call's transcript: one row per turn, each side its own rows, in the
   * order the words were said.
   *
   * What the wire sends (voice/live/protocol.py), and what follows from it:
   *  - `transcript.text` is a FRAGMENT, not the text so far. A row is the sum
   *    of its fragments. (Writing each fragment over the last one left a
   *    single "Agent: ?" on screen for a whole call.)
   *  - The agent's `final` frame is empty, and one answer is often several
   *    provider responses (a lookup, then the answer), each closed by its own
   *    `turn_end`. Neither ends the agent's row: the caller speaking does.
   *    A later response in the same row starts on a new line.
   *  - The caller's row ends on `final`, or on `turn_end` for a provider that
   *    never marks a fragment final.
   *  - A recognizer that transcribes only finished utterances can deliver the
   *    caller's words AFTER the reply to them has started. `reply: true` (on
   *    the reply's first caption, and on its `turn_end`) says a response
   *    answers new caller speech; when no caller text came before it, the
   *    text is still owed and is put above that reply when it arrives.
   *  - `display` (a looked-up answer as text) is not drawn: the same answer
   *    arrives as the agent's spoken transcript, and the row shows what was
   *    said. Drawing both would show one answer twice.
   */
  function CallTranscript(logEl) {
    var doc = logEl.ownerDocument;
    var user = null, userText = "";
    var bot = null, botText = "", botBreak = false;
    var heard = false;      // caller text arrived that no reply has claimed yet
    var owed = false;       // a reply started before its caller text arrived
    var anchor = null;      // first agent row of that reply
    var claimed = false;    // the response in flight already settled `reply`

    function row(role, before) {
      var el = doc.createElement("p");
      el.className = "caption";
      el.setAttribute("data-voice-call-turn", role);
      el.style.margin = "var(--space-xxs) 0";
      el.style.whiteSpace = "pre-wrap";
      var who = doc.createElement("strong");
      who.textContent = role === "user" ? "You: " : "Agent: ";
      el.appendChild(who);
      // A text node only: a transcript is never parsed as markup.
      el.appendChild(doc.createTextNode(""));
      if (before && before.parentNode === logEl) logEl.insertBefore(el, before);
      else logEl.appendChild(el);
      return el;
    }
    function write(el, text) {
      el.lastChild.textContent = text;
      logEl.scrollTop = logEl.scrollHeight;
    }
    function closeBot() { bot = null; botText = ""; botBreak = false; }
    function closeUser() { user = null; userText = ""; }

    // A response that answers new caller speech has started.
    function replyStarted() {
      if (claimed) return;
      claimed = true;
      if (heard) { heard = false; return; }
      // The caller spoke, their words are not here yet: the agent's previous
      // row is over, and the next agent row is the one to put them above.
      closeBot();
      owed = true;
      anchor = null;
    }

    function userFragment(frame) {
      var text = frame.text || "";
      if (!user) {
        if (!text) {
          // An empty final with nothing open: the utterance was noise, or its
          // transcription failed. Nothing is owed any more.
          if (frame.final) { owed = false; anchor = null; }
          return;
        }
        if (owed) {
          user = row("user", anchor);
          owed = false;
          anchor = null;
        } else {
          closeBot();
          user = row("user", null);
          heard = true;
        }
      }
      userText += text;
      write(user, userText);
      if (frame.final) closeUser();
    }

    function botFragment(frame) {
      if (frame.reply === true) replyStarted();
      var text = frame.text || "";
      if (!text) return;
      if (!bot) {
        bot = row("assistant", null);
        if (owed && !anchor) anchor = bot;
      }
      if (botBreak && botText) botText += "\n";
      botBreak = false;
      botText += text;
      write(bot, botText);
    }

    function turnEnd(frame) {
      if (frame.reply === true) replyStarted();
      claimed = false;
      closeUser();
      if (bot) botBreak = true;
    }

    return {
      handle: function (frame) {
        if (!frame) return;
        if (frame.type === "transcript") {
          if (frame.role === "user") userFragment(frame);
          else if (frame.role === "assistant") botFragment(frame);
        } else if (frame.type === "turn_end") {
          turnEnd(frame);
        }
      },
      reset: function () {
        logEl.textContent = "";
        closeUser(); closeBot();
        heard = false; owed = false; anchor = null; claimed = false;
      },
    };
  }
  window.VoiceCallTranscript = CallTranscript;

  var cfg = window.__voice_call_config;
  var startBtn = document.querySelector("[data-voice-call-start]");
  var endBtn = document.querySelector("[data-voice-call-end]");
  var statusEl = document.querySelector("[data-voice-call-status]");
  var logEl = document.querySelector("[data-voice-call-log]");
  if (!cfg || !startBtn || !window.VoiceLiveClient) return;

  var client = null;
  var transcript = logEl ? CallTranscript(logEl) : null;

  function setStatus(text) { if (statusEl) statusEl.textContent = text; }

  function ended() {
    client = null;
    startBtn.hidden = false;
    if (endBtn) endBtn.hidden = true;
  }

  function start() {
    var scheme = window.location.protocol === "https:" ? "wss://" : "ws://";
    var url = scheme + window.location.host + cfg.path;
    if (cfg.profile_id) url += "?profileId=" + encodeURIComponent(cfg.profile_id);
    if (transcript) transcript.reset();
    client = new window.VoiceLiveClient({
      url: url,
      auth: { type: "auth", token: cfg.token },
      onState: function (state) {
        if (state === "connecting") setStatus("Connecting…");
        else if (state === "listening") setStatus("On the call. Speak now.");
        else if (state === "closed") { setStatus("Call ended."); ended(); }
      },
      onEvent: function (frame) {
        if (frame.type === "error") setStatus(frame.message || "The call failed.");
        else if (transcript) transcript.handle(frame);
      },
    });
    startBtn.hidden = true;
    if (endBtn) endBtn.hidden = false;
    client.connect();
  }

  startBtn.addEventListener("click", start);
  if (endBtn) endBtn.addEventListener("click", function () {
    if (client) client.disconnect();
    setStatus("Call ended.");
    ended();
  });
  window.addEventListener("pagehide", function () { if (client) client.disconnect(); });
})();
