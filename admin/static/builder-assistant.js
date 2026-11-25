// The public demo's builder assistant panel ("Build with AI").
// Talks to POST /admin/demo/assistant/chat (admin/demo_handlers.py), which
// streams server-sent events: answer text, a `step` per tool call, `working`
// while a long step runs, `session` and `changed`.
//
// The conversation survives moving between portal pages: it is saved in the
// browser after every event, and a page that loads in the middle of a turn
// (the visitor followed a step's link) reads the rest of that turn from
// GET /admin/demo/assistant/history, because the turn keeps running on the
// server. "New" starts a fresh conversation. The panel opens by itself on
// the first visit, except on a phone, where a small hint points at the
// launcher instead, and unless the guided tour is on (#demo-tour-config in
// the page): then the tour's Welcome comes first and the tour opens the panel.
//
// Where the demo's voice is on, a microphone button talks to the same
// assistant (WS /admin/demo/assistant/voice, demo_mode/assistant_voice.py),
// in the same conversation. The call is the shared voice client
// (static/shared/voice-live.js) and its transcript the Call button's
// (voice-call.js); a spoken request's steps and full answer are read from
// the same turn record a typed one leaves (GET /admin/demo/assistant/history).
//
// Guided tour hooks (demo-tour.js listens; every event is wrapped in
// try/catch so a tour problem never breaks the chat):
//   window.demoAssistant.ask(prompt, { tourStep, title }), .open(), .close(), .isOpen()
//   `tour:ai-done` when a turn ends: { tools, actions, links, limited, limit, tourStep, text }
//   a "Start run" submit is marked in sessionStorage for eval-run-detail-poll.js
(function () {
  "use strict";
  var panel = document.getElementById("ba-panel");
  var launcher = document.getElementById("ba-launcher");
  if (!panel || !launcher) return;
  var log = document.getElementById("ba-log");
  var form = document.getElementById("ba-form");
  var input = document.getElementById("ba-input");
  var send = document.getElementById("ba-send");
  var chips = document.getElementById("ba-chips");
  var KEY = "demo-assistant:v2";
  var OPEN_KEY = "demo-assistant:open";
  var SEEN_KEY = "demo-assistant:seen";
  var FROM_CALL_KEY = "demo-assistant:from-call";
  // The width under which the panel is a full-screen sheet (portal.css).
  var SHEET = window.matchMedia("(max-width: 640px)");
  var hint = document.getElementById("ba-hint");
  var busy = false;
  var TOUR_ON = !!document.getElementById("demo-tour-config");
  // The cap answers (demo_mode/limits.py CAP_MESSAGE, RATE_MESSAGE), as text.
  var LIMIT_PATTERNS = [["daily", /reached today's limit/i], ["rate", /going a little fast for the public demo/i]];

  function tourEmit(name, detail) {
    try { window.dispatchEvent(new CustomEvent(name, { detail: detail || {} })); } catch (e) { /* optional */ }
  }

  // The first-visit hint on a phone: a small note by the launcher that never
  // blocks the page. It goes when the panel opens, when it is dismissed and
  // after a while.
  var hintTimer = null;
  function nudge(on) {
    if (!hint) return;
    hint.hidden = !on;
    launcher.classList.toggle("ba-launcher-nudge", on);
    clearTimeout(hintTimer);
    if (on) hintTimer = setTimeout(function () { nudge(false); }, 12000);
  }

  function storage() {
    try { localStorage.setItem("demo-assistant:probe", "1"); return localStorage; }
    catch (e) {
      try { return sessionStorage; } catch (e2) { return null; }
    }
  }
  var store = storage();
  function get(key) { try { return store ? store.getItem(key) : null; } catch (e) { return null; } }
  function put(key, value) { try { if (store) store.setItem(key, value); } catch (e) { /* full or blocked */ } }

  function fresh() { return { session: null, items: [] }; }
  function load() {
    try {
      var parsed = JSON.parse(get(KEY) || "null");
      return parsed && Array.isArray(parsed.items) ? parsed : fresh();
    } catch (e) { return fresh(); }
  }
  var state = load();
  function save() { put(KEY, JSON.stringify(state)); }

  function markdown(text) {
    if (window.marked && window.DOMPurify) {
      return window.DOMPurify.sanitize(window.marked.parse(text || ""));
    }
    var div = document.createElement("div");
    div.textContent = text || "";
    return div.innerHTML.replace(/\n/g, "<br>");
  }

  function safeHref(href) {
    return typeof href === "string" && href.indexOf("/admin/") === 0 ? href : null;
  }

  function stepNode(step) {
    var row = document.createElement("div");
    row.className = "ba-step " + (step.ok === undefined ? "run" : (step.ok ? "ok" : "bad"));
    var label = document.createElement("span");
    label.className = "ba-step-label";
    label.textContent = step.label || "A step";
    row.appendChild(label);
    if (step.note) {
      var note = document.createElement("span");
      note.className = "caption";
      note.textContent = step.note;
      row.appendChild(note);
    }
    (step.links || []).forEach(function (link) {
      var href = safeHref(link.href);
      if (!href) return;
      var a = document.createElement("a");
      a.href = href;
      a.textContent = link.text || "Open";
      row.appendChild(a);
    });
    return row;
  }

  function render() {
    log.textContent = "";
    if (!state.items.length) {
      var hello = document.createElement("div");
      hello.className = "ba-msg ba-bot";
      hello.innerHTML = markdown(
        "Hi! I build and test agents for **Northwind Internet**, a made-up internet provider, " +
        "using the engine's own admin tools. Ask me to create an agent, change its instructions, " +
        "give it tools or a knowledge base, or test it. Pick a suggestion below to start.");
      log.appendChild(hello);
    }
    state.items.forEach(function (item) { log.appendChild(itemNode(item)); });
    // The suggestions stay: a full row before the first message, a compact
    // scrolling row after it.
    chips.classList.toggle("ba-chips-compact", state.items.length > 0);
    log.scrollTop = log.scrollHeight;
  }

  function itemNode(item) {
    var box = document.createElement("div");
    if (item.role === "user") {
      box.className = "ba-msg ba-user";
      if (item.voice) {
        var said = document.createElement("span");
        said.className = "ba-said";
        said.textContent = "Spoken";
        box.appendChild(said);
      }
      box.appendChild(document.createTextNode(item.text));
      return box;
    }
    box.className = "ba-msg ba-bot";
    if (item.steps && item.steps.length) {
      var steps = document.createElement("div");
      steps.className = "ba-steps";
      item.steps.forEach(function (s) { steps.appendChild(stepNode(s)); });
      box.appendChild(steps);
    }
    if (item.pending && item.working) {
      box.appendChild(stepNode({ label: item.working + "…" }));
    }
    var text = document.createElement("div");
    text.innerHTML = markdown(item.text || (item.pending ? "…" : ""));
    box.appendChild(text);
    if (item.changed && !item.pending) {
      var changed = document.createElement("div");
      changed.className = "ba-changed";
      var label = document.createElement("span");
      label.className = "caption";
      var reload = document.createElement("button");
      reload.type = "button";
      reload.className = "ghost ba-icon";
      // The turn made an agent: the way on is that agent's page, not a
      // reload of this one.
      var made = createdAgent(item);
      if (made && !onPage(made.href)) {
        label.textContent = "Your new agent is ready.";
        reload.textContent = made.label;
        reload.addEventListener("click", function () { window.location.href = made.href; });
      } else {
        label.textContent = "Something changed in the portal.";
        // A reload would end an open call: offer it once the call is over
        // (callEnded renders again).
        reload.textContent = call ? "Refresh after the call" : "Refresh this page";
        reload.addEventListener("click", function () { window.location.reload(); });
      }
      reload.disabled = !!call;
      changed.appendChild(label);
      changed.appendChild(reload);
      box.appendChild(changed);
    }
    return box;
  }

  // The agent a finished turn created (a template used, an agent copied):
  // { href, label } of its page, from the answer's own link to it or the
  // steps' links, or null.
  var AGENT_LINK = /^\/admin\/agents\?(?:[^#]*&)?profile_id=[0-9a-fA-F-]{32,36}/;
  function createdAgent(item) {
    var steps = (item && item.steps) || [];
    var made = steps.some(function (st) {
      return st.ok === true && ((st.tool === "templates" && st.action === "use") || st.tool === "fork_profile");
    });
    if (!made) return null;
    var re = /\[([^\]]+)\]\((\/admin\/[^)\s]+)\)/g, m;
    while ((m = re.exec(String(item.text || "")))) {
      if (AGENT_LINK.test(m[2])) {
        return { href: m[2], label: /^open\b/i.test(m[1]) ? m[1] : "Open " + m[1] };
      }
    }
    for (var i = 0; i < steps.length; i++) {
      var links = steps[i].links || [];
      for (var j = 0; j < links.length; j++) {
        var h = safeHref(links[j] && links[j].href);
        if (h && AGENT_LINK.test(h)) return { href: h, label: "Open the agent" };
      }
    }
    return null;
  }
  function onPage(href) {
    var id = /profile_id=([0-9a-fA-F-]+)/.exec(href);
    return !!id && location.pathname === "/admin/agents" &&
      new RegExp("[?&]profile_id=" + id[1] + "(&|$)").test(location.search);
  }

  // "Tour step: <title> · Back to tour", under the panel's header, while the
  // last message came from a tour step. Built here so _base.html needs no markup.
  var strip = null;
  function showStrip(title) {
    try {
      if (!strip) {
        strip = document.createElement("div");
        strip.className = "ba-tour-strip";
        strip.setAttribute("role", "note");
        // A flag, "Tour step: <strong>title</strong>", then "Back to tour" (canvas 05).
        strip.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<path d="M5 21V4M5 4h11l-2 4 2 4H5"></path></svg>' +
          '<span class="ba-tour-strip-label">Tour step: <strong></strong></span>';
        var back = document.createElement("button");
        back.type = "button";
        back.className = "ba-tour-back";
        back.textContent = "Back to tour";
        back.addEventListener("click", function () {
          // On a phone the panel is a full-screen sheet: close it, so the
          // checklist (or the step's card) the tour opens is what shows.
          if (SHEET.matches) setOpen(false);
          tourEmit("tour:resume", {});
        });
        strip.appendChild(back);
        var head = panel.querySelector(".ba-head");
        if (head && head.nextSibling) panel.insertBefore(strip, head.nextSibling);
        else panel.insertBefore(strip, panel.firstChild);
      }
      strip.querySelector(".ba-tour-strip-label strong").textContent = title || "this step";
      strip.hidden = false;
    } catch (e) { /* optional */ }
  }
  function hideStrip() { if (strip) strip.hidden = true; }

  function limitOf(text) {
    for (var i = 0; i < LIMIT_PATTERNS.length; i++) {
      if (LIMIT_PATTERNS[i][1].test(String(text || ""))) return LIMIT_PATTERNS[i][0];
    }
    return null;
  }

  // One finished turn, for the tour. Only steps that succeeded count as tools
  // the assistant used; links are the steps' links plus portal links in the answer.
  function turnDone(reply, forcedLimit) {
    try {
      // Once per turn: a turn read back from the saved conversation on a
      // later page is never reported again.
      if (!reply || reply.reported) return;
      reply.reported = true;
      save();
      var steps = (reply && reply.steps) || [];
      var tools = [], actions = [], links = [];
      steps.forEach(function (st) {
        (st.links || []).forEach(function (l) { var h = safeHref(l && l.href); if (h && links.indexOf(h) < 0) links.push(h); });
        if (st.ok !== true || !st.tool) return;
        if (tools.indexOf(st.tool) < 0) tools.push(st.tool);
        var a = st.tool + (st.action ? ":" + st.action : "");
        if (actions.indexOf(a) < 0) actions.push(a);
        // What it was done to (profile_attachments kind; "downstream" for a new
        // sub-agent): "profile_attachments:add:kb", "templates:use:downstream".
        if (st.kind && actions.indexOf(a + ":" + st.kind) < 0) actions.push(a + ":" + st.kind);
      });
      var text = String((reply && reply.text) || "");
      var re = /\]\((\/admin\/[^)\s]+)\)/g, m;
      while ((m = re.exec(text))) { if (links.indexOf(m[1]) < 0) links.push(m[1]); }
      var limit = forcedLimit || limitOf(text);
      tourEmit("tour:ai-done", {
        tools: tools, actions: actions, links: links, limited: !!limit, limit: limit,
        tourStep: (reply && reply.tourStep) || null, text: text
      });
    } catch (e) { /* optional */ }
  }

  function setOpen(open, keep) {
    panel.hidden = !open;
    launcher.hidden = open;
    launcher.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) nudge(false);
    if (keep !== false) put(OPEN_KEY, open ? "1" : "0");
    document.body.classList.toggle("ba-open", open);
    if (open) { render(); }
  }

  function handle(event, reply) {
    var type = event.type;
    if (type === "answer" && event.content) {
      reply.text = (reply.text || "") + event.content;
      reply.working = "";
    } else if (type === "step") {
      reply.steps = reply.steps || [];
      reply.steps.push(event);
    } else if (type === "working") {
      reply.working = event.label || "";
    } else if (type === "session" && event.id) {
      state.session = event.id;
    } else if (type === "changed") {
      reply.changed = true;
    } else if (type === "error") {
      reply.text = (reply.text ? reply.text + "\n\n" : "") + (event.message || "Something went wrong.");
    }
  }

  function setBusy(value) {
    busy = value;
    send.disabled = value;
    if (mic && !call) mic.disabled = value;
  }

  async function ask(message, tourStep) {
    if (busy || !message.trim()) return;
    setBusy(true);
    var streaming = false;  // true once the answer's stream has opened
    var dropped = false;
    var httpStatus = 0;
    state.items.push({ role: "user", text: message });
    var reply = { role: "assistant", text: "", steps: [], pending: true, tourStep: tourStep || null };
    if (!tourStep) hideStrip();
    state.items.push(reply);
    save();
    render();
    try {
      var resp = await fetch("/admin/demo/assistant/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({ message: message, session_id: state.session, page: location.pathname + location.search })
      });
      if (!resp.ok || !resp.body) {
        httpStatus = resp.status;
        var detail = "";
        try { detail = (await resp.json()).detail || ""; } catch (e) { /* not JSON */ }
        throw new Error(detail || ("The assistant answered " + resp.status + "."));
      }
      streaming = true;
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      for (;;) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        var blocks = buffer.split("\n\n");
        buffer = blocks.pop();
        blocks.forEach(function (block) {
          block.split("\n").forEach(function (line) {
            if (line.indexOf("data:") !== 0) return;
            try { handle(JSON.parse(line.slice(5).trim()), reply); } catch (e) { /* keep-alive */ }
          });
        });
        save();  // after every event: a click on a step's link keeps the turn so far
        render();
      }
    } catch (err) {
      if (streaming && state.session) {
        // The connection dropped in the middle of the answer. The turn keeps
        // running on the server, so read the rest of it from there instead
        // of showing a network error.
        dropped = true;
      } else {
        reply.text = (reply.text ? reply.text + "\n\n" : "") + (err && err.message ? err.message : "The assistant is not reachable.");
      }
    } finally {
      if (dropped) {
        reply.pending = true;
        reply.working = "Still working";
        busy = false;
        save();
        render();
        resume();
      } else {
        reply.pending = false;
        reply.working = "";
        setBusy(false);
        save();
        render();
        // A 429 before the stream opened is a limit too, not a failure.
        turnDone(reply, httpStatus === 429 ? (limitOf(reply.text) || "rate") : null);
      }
    }
  }

  // A page that loaded in the middle of a turn: the turn kept running on the
  // server, so read the rest of it from there.
  async function resume() {
    var last = state.items[state.items.length - 1];
    if (!last || last.role !== "assistant" || !last.pending) return;
    if (!state.session) {
      last.pending = false;
      last.text = last.text || "This answer was interrupted. Please ask again.";
      save();
      render();
      turnDone(last);  // the tour stops waiting for it
      return;
    }
    setBusy(true);
    var deadline = Date.now() + 4 * 60 * 1000;
    try {
      while (Date.now() < deadline) {
        var resp = await fetch("/admin/demo/assistant/history?session_id=" + encodeURIComponent(state.session),
                               { credentials: "same-origin", headers: { "Accept": "application/json" } });
        if (!resp.ok) break;
        var data = await resp.json();
        var live = data.live;
        if (live) {
          last.text = live.answer || last.text;
          last.steps = live.steps || last.steps;
          last.changed = !!live.changed;
          if (live.done) break;
        } else {
          // Not in this server's memory: the saved conversation has the text.
          var msgs = (data.messages || []).filter(function (m) { return m.role === "assistant"; });
          if (msgs.length) last.text = msgs[msgs.length - 1].content || last.text;
          break;
        }
        last.working = "Still working";
        save();
        render();
        await new Promise(function (r) { setTimeout(r, 2500); });
      }
    } catch (e) { /* keep what we have */ }
    last.pending = false;
    last.working = "";
    setBusy(false);
    save();
    render();
    turnDone(last);
  }

  launcher.addEventListener("click", function () { setOpen(true); input.focus(); });
  if (hint) {
    var hintClose = hint.querySelector("[data-hint-close]");
    if (hintClose) hintClose.addEventListener("click", function () { nudge(false); });
  }
  document.getElementById("ba-close").addEventListener("click", function () { setOpen(false); });
  document.getElementById("ba-new").addEventListener("click", function () {
    if (busy || call) return;
    state = fresh();
    save();
    render();
    hideStrip();
  });
  chips.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-prompt]");
    if (btn) ask(btn.getAttribute("data-prompt"));
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value;
    input.value = "";
    ask(text);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  // ---------------------------------------------------------------------
  // Talking to the assistant
  // ---------------------------------------------------------------------
  var mic = document.getElementById("ba-mic");
  var voiceBox = document.getElementById("ba-voice");
  var voiceStatus = document.getElementById("ba-voice-status");
  var voiceLog = document.getElementById("ba-voice-log");
  var call = null;         // the open call (VoiceLiveClient)
  var heard = null;        // its transcript (VoiceCallTranscript)
  var failed = false;      // the call ended with a reason on screen
  var lastLive = null;     // the last turn record already on screen
  var voiceTurn = null;    // {id, reply}: the spoken turn being shown
  var watching = false;

  function loadOnce(src, present) {
    // Loaded ahead of the click: a phone only opens the microphone inside
    // the click itself, so nothing may be awaited then.
    if (!src || present()) return;
    var el = document.createElement("script");
    el.src = src;
    el.async = false;
    document.head.appendChild(el);
  }

  function sayStatus(text) { if (voiceStatus) voiceStatus.textContent = text; }

  function setPressed(open) {
    mic.setAttribute("aria-pressed", open ? "true" : "false");
    mic.setAttribute("aria-label", open ? "End the call" : "Talk to the assistant");
    mic.title = open ? "End the call" : "Talk to the assistant";
    mic.disabled = !open && busy;
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  async function readLive() {
    if (!state.session) return null;
    try {
      var resp = await fetch("/admin/demo/assistant/history?session_id=" + encodeURIComponent(state.session),
                             { credentials: "same-origin", headers: { "Accept": "application/json" } });
      if (!resp.ok) return null;
      return (await resp.json()).live || null;
    } catch (e) { return null; }
  }

  // A spoken request's turn record: the visitor's words, the steps as they
  // run, then the full answer (the voice says only its first sentence).
  function showLive(live) {
    if (!voiceTurn || voiceTurn.id !== live.id) {
      state.items.push({ role: "user", text: live.message || "…", voice: true });
      var fresh = { role: "assistant", text: "", steps: [], pending: true, working: "Working" };
      state.items.push(fresh);
      voiceTurn = { id: live.id, reply: fresh };
    }
    var reply = voiceTurn.reply;
    reply.steps = live.steps || [];
    reply.changed = !!live.changed;
    if (live.done) {
      reply.text = live.answer || reply.text;
      reply.pending = false;
      reply.working = "";
      lastLive = live.id;
    }
    save();
    render();
    // A spoken request counts for the tour like a typed one.
    if (live.done) turnDone(reply);
  }

  async function watchTurn() {
    if (watching || !state.session) return;
    watching = true;
    var started = Date.now();
    var misses = 0;
    try {
      while (Date.now() - started < 4 * 60 * 1000) {
        var live = await readLive();
        if (live && live.voice && live.id !== lastLive) {
          misses = 0;
          showLive(live);
          if (live.done) break;
        } else {
          misses += 1;
          // Nothing new: the assistant answered by itself (a greeting, a
          // thank-you), or the call is over.
          if (misses > 8 || (!call && misses > 2)) break;
        }
        await sleep(1500);
      }
    } finally { watching = false; }
  }

  function callEnded() {
    call = null;
    setPressed(false);
    if (!failed) sayStatus("Call ended.");
    // Drop the new-tab targets the call gave the panel's links.
    render();
  }

  // During a call, a link in the panel (a step's "Open ...", a link in an
  // answer) opens in a new tab, so following it neither navigates away from
  // the call nor, on a phone, closes the sheet (mobile.js leaves a
  // target=_blank link alone). Capture phase: this runs before mobile.js's
  // document listener reads the target. render() rebuilds the links, so the
  // target goes when the call ends.
  panel.addEventListener("click", function (e) {
    if (!call) return;
    var a = e.target.closest("a[href]");
    if (!a || !panel.contains(a)) return;
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener");
    put(FROM_CALL_KEY, String(Date.now()));
  }, true);

  function onFrame(frame) {
    if (frame.type === "error") {
      failed = true;
      sayStatus(frame.message || "The call failed.");
      // No microphone: end the call rather than keep a silent line open.
      if (/microphone/i.test(frame.message || "") && call) call.disconnect();
      return;
    }
    if (frame.type === "ready") {
      if (!state.session && frame.session_id) { state.session = frame.session_id; save(); }
      readLive().then(function (live) { if (live && !lastLive) lastLive = live.id; });
    } else if (frame.type === "agent_status") {
      sayStatus(frame.text || "Working…");
      if (voiceTurn && voiceTurn.reply.pending) { voiceTurn.reply.working = frame.text || ""; render(); }
      watchTurn();
    } else if (frame.type === "transcript" && frame.role === "user" && frame.final) {
      watchTurn();
    } else if (frame.type === "display") {
      watchTurn();
    } else if (frame.type === "turn_end" && call) {
      sayStatus("Listening. Ask for an agent, a change or a test.");
    }
    if (heard) {
      heard.handle(frame);
      // The holding line is its own line: the answer that follows it is not
      // run on into it.
      if (frame.type === "transcript" && frame.out_of_band) heard.handle({ type: "turn_end" });
    }
  }

  function startCall() {
    if (!window.VoiceLiveClient) {
      sayStatus("The microphone is still loading. Try again in a moment.");
      voiceBox.hidden = false;
      return;
    }
    var scheme = window.location.protocol === "https:" ? "wss://" : "ws://";
    var url = scheme + window.location.host + "/admin/demo/assistant/voice?page=" +
      encodeURIComponent(location.pathname + location.search);
    if (state.session) url += "&sessionId=" + encodeURIComponent(state.session);
    voiceBox.hidden = false;
    voiceLog.textContent = "";
    heard = window.VoiceCallTranscript ? window.VoiceCallTranscript(voiceLog) : null;
    failed = false;
    call = new window.VoiceLiveClient({
      url: url,
      onState: function (s) {
        if (s === "connecting") sayStatus("Connecting…");
        else if (s === "listening") sayStatus("Listening. Ask for an agent, a change or a test.");
        else if (s === "closed") callEnded();
      },
      onEvent: onFrame,
    });
    setPressed(true);
    render();
    call.connect();
  }

  if (mic) {
    loadOnce(mic.getAttribute("data-voice-src"), function () { return !!window.VoiceLiveClient; });
    loadOnce(mic.getAttribute("data-call-src"), function () { return !!window.VoiceCallTranscript; });
    mic.addEventListener("click", function () {
      if (call) { call.disconnect(); callEnded(); return; }
      if (busy) return;
      startCall();
    });
    window.addEventListener("pagehide", function () { if (call) call.disconnect(); });
  }

  // The guided tour's handle on the panel (demo mode only: this file loads
  // nowhere else). ask() returns false when a turn is still running.
  window.demoAssistant = {
    ask: function (prompt, opts) {
      opts = opts || {};
      setOpen(true);
      if (opts.tourStep) showStrip(opts.title || opts.tourStep); else hideStrip();
      if (busy) { input.value = String(prompt || ""); input.focus(); return false; }
      ask(String(prompt || ""), opts.tourStep || null);
      return true;
    },
    open: function () { setOpen(true); },
    close: function () { setOpen(false); },
    isOpen: function () { return !panel.hidden; }
  };

  // The mark lives for one page: any page but a run's (where the submit
  // lands) means the start failed, so a later run is not taken for it.
  try {
    if (!/^\/admin\/evals\/runs\/[^/]+\/?$/.test(location.pathname)) sessionStorage.removeItem("demo-tour:run-submit");
  } catch (e) { /* blocked */ }

  // A test run started from this tab: eval-run-detail-poll.js, on the run
  // page the submit lands on, tells the tour it is the visitor's own run.
  document.addEventListener("submit", function (e) {
    try {
      var f = e.target;
      var action = (f && f.getAttribute && f.getAttribute("action")) || "";
      if (/\/admin\/(eval-runs\/trigger|evals\/run)$/.test(action)) {
        sessionStorage.setItem("demo-tour:run-submit", JSON.stringify({ at: Date.now(), from: location.pathname }));
      }
    } catch (err) { /* optional */ }
  }, true);

  // Open by itself on the first visit; afterwards, as the visitor left it.
  // On a phone the panel is a full-screen sheet, so a first visit gets a small
  // hint by the launcher instead. A tab opened from a link during a call
  // (above) starts closed on a phone, so the page asked for is what shows,
  // without changing how the calling tab left the panel. With the tour on,
  // the tour's Welcome comes first: the panel stays closed until the tour
  // (or the visitor) opens it, and there is no first-visit hint. A reload
  // with no turn running keeps it closed (below).
  var first = !get(SEEN_KEY);
  put(SEEN_KEY, "1");
  // This page loaded after every finished turn: what they changed is on it
  // already, so their "Refresh this page" (or "Open" the new agent) offer
  // is not shown again.
  var stale = false;
  state.items.forEach(function (item) {
    if (item.role === "assistant" && !item.pending && item.changed) { item.changed = false; stale = true; }
    if (item.role === "assistant" && !item.pending) item.reported = true;
  });
  if (stale) save();
  // A reload brings the page back, not the panel: it reopens by itself only
  // while a turn is still running (its answer is still to come).
  var last = state.items[state.items.length - 1];
  var running = !!(last && last.role === "assistant" && last.pending);
  var reloaded = false;
  try {
    var nav = window.performance && window.performance.getEntriesByType && window.performance.getEntriesByType("navigation")[0];
    reloaded = !!nav && nav.type === "reload";
  } catch (e) { /* old browser: as before */ }
  var quiet = reloaded && !running;
  var fromCall = Date.now() - Number(get(FROM_CALL_KEY) || 0) < 15000;
  if (fromCall) put(FROM_CALL_KEY, "0");
  if (SHEET.matches && fromCall) {
    setOpen(false, false);
  } else if (quiet) {
    setOpen(false);
  } else if (TOUR_ON) {
    setOpen(get(OPEN_KEY) === "1");
  } else if (SHEET.matches && first) {
    setOpen(false, false);
    nudge(true);
  } else {
    setOpen(first || get(OPEN_KEY) === "1");
  }
  resume();
})();
