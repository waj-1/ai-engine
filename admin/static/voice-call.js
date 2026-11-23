/* Test call for an agent that is called at its own address
 * (WS /v1/agents/{slug}/voice) — the Voice tab's "Call" button.
 *
 * The call itself is the shared duplex client (static/shared/voice-live.js):
 * same socket, same wire protocol as every other voice front end. This file
 * only builds the address, hands the operator's token over as the first
 * `auth` frame (a browser cannot set a header on a WebSocket), pins the
 * profile being viewed so a draft can be called, and prints the transcript.
 */
(function () {
  "use strict";
  var cfg = window.__voice_call_config;
  var startBtn = document.querySelector("[data-voice-call-start]");
  var endBtn = document.querySelector("[data-voice-call-end]");
  var statusEl = document.querySelector("[data-voice-call-status]");
  var logEl = document.querySelector("[data-voice-call-log]");
  if (!cfg || !startBtn || !window.VoiceLiveClient) return;

  var client = null;
  var open = { user: null, assistant: null };  // the line a partial transcript is filling

  function setStatus(text) { if (statusEl) statusEl.textContent = text; }

  function line(role, text, final) {
    if (!logEl || !text) return;
    var el = open[role];
    if (!el) {
      el = document.createElement("p");
      el.className = "caption";
      el.style.margin = "var(--space-xxs) 0";
      var who = document.createElement("strong");
      who.textContent = role === "user" ? "You: " : "Agent: ";
      el.appendChild(who);
      el.appendChild(document.createTextNode(""));
      logEl.appendChild(el);
      open[role] = el;
    }
    // textContent only: a transcript is never parsed as HTML.
    el.lastChild.textContent = text;
    if (final) open[role] = null;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function ended() {
    client = null;
    startBtn.hidden = false;
    if (endBtn) endBtn.hidden = true;
  }

  function start() {
    var scheme = window.location.protocol === "https:" ? "wss://" : "ws://";
    var url = scheme + window.location.host + cfg.path;
    if (cfg.profile_id) url += "?profileId=" + encodeURIComponent(cfg.profile_id);
    if (logEl) logEl.textContent = "";
    open = { user: null, assistant: null };
    client = new window.VoiceLiveClient({
      url: url,
      auth: { type: "auth", token: cfg.token },
      onState: function (state) {
        if (state === "connecting") setStatus("Connecting…");
        else if (state === "listening") setStatus("On the call. Speak now.");
        else if (state === "closed") { setStatus("Call ended."); ended(); }
      },
      onEvent: function (frame) {
        if (frame.type === "transcript") line(frame.role, frame.text, frame.final);
        else if (frame.type === "error") setStatus(frame.message || "The call failed.");
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
