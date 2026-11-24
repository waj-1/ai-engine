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
// the first visit.
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
  var busy = false;

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
      box.textContent = item.text;
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
      label.textContent = "Something changed in the portal.";
      var reload = document.createElement("button");
      reload.type = "button";
      reload.className = "ghost ba-icon";
      reload.textContent = "Refresh this page";
      reload.addEventListener("click", function () { window.location.reload(); });
      changed.appendChild(label);
      changed.appendChild(reload);
      box.appendChild(changed);
    }
    return box;
  }

  function setOpen(open) {
    panel.hidden = !open;
    launcher.hidden = open;
    launcher.setAttribute("aria-expanded", open ? "true" : "false");
    put(OPEN_KEY, open ? "1" : "0");
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
  }

  async function ask(message) {
    if (busy || !message.trim()) return;
    setBusy(true);
    state.items.push({ role: "user", text: message });
    var reply = { role: "assistant", text: "", steps: [], pending: true };
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
        var detail = "";
        try { detail = (await resp.json()).detail || ""; } catch (e) { /* not JSON */ }
        throw new Error(detail || ("The assistant answered " + resp.status + "."));
      }
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
      reply.text = (reply.text ? reply.text + "\n\n" : "") + (err && err.message ? err.message : "The assistant is not reachable.");
    } finally {
      reply.pending = false;
      reply.working = "";
      setBusy(false);
      save();
      render();
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
  }

  launcher.addEventListener("click", function () { setOpen(true); input.focus(); });
  document.getElementById("ba-close").addEventListener("click", function () { setOpen(false); });
  document.getElementById("ba-new").addEventListener("click", function () {
    if (busy) return;
    state = fresh();
    save();
    render();
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

  // Open by itself on the first visit; afterwards, as the visitor left it.
  var first = !get(SEEN_KEY);
  put(SEEN_KEY, "1");
  setOpen(first || get(OPEN_KEY) === "1");
  resume();
})();
