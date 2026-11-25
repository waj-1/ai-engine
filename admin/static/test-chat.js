/* ai_agent/admin/static/test-chat.js
 *
 * Floating bottom-right chat widget for sanity-testing an agent profile
 * from the admin portal. Renders only when the page template injects
 * `window.__waj_test_chat_config` with at minimum:
 *   { endpoint, agent_kind, profile_id, user_token }
 * Optional: { business_id_optional, profile_name, dashboard_url_optional,
 *             extra_body, cases, state_token, pin_profile }
 *
 * Endpoints do not share one body: the server (admin/services/test_chat.py,
 * also used by the admin MCP `chat` tool) supplies the kind's own fields in
 * `extra_body` (the AI-agents demo needs `agent` + `case_id`), a `cases`
 * picker, and `state_token` when the endpoint streams a `{type:"state"}`
 * token the next turn must echo back. `pin_profile` sends X-WAJ-Profile-Id so
 * the profile on screen (a draft copy, a specialist) answers, not the kind's
 * active one.
 *
 * Streams SSE responses (parsed manually so we can attach the operator's
 * Bearer JWT — EventSource cannot set custom headers). Falls back to
 * rendering the raw body when the endpoint returns JSON instead of SSE.
 *
 * Security note: embedding the operator's JWT in the page HTML is OK
 * here because the page itself requires superuser auth, and the JWT we
 * embed is the same one already delivered to the browser in the session
 * cookie that authenticated this admin page load.
 */
(function () {
  "use strict";

  var cfg = window.__waj_test_chat_config;
  if (!cfg || !cfg.endpoint || !cfg.profile_id) {
    return;
  }
  if (document.getElementById("waj-test-chat-root")) {
    return; // already mounted
  }

  var STORAGE_KEY = "waj_test_chat::" + cfg.profile_id;
  var state = loadState();
  var sessionId = uuidv4();

  injectStyles();

  // --- Root container -----------------------------------------------------
  var root = document.createElement("div");
  root.id = "waj-test-chat-root";
  document.body.appendChild(root);

  // --- Launcher chip ------------------------------------------------------
  var launcher = document.createElement("button");
  launcher.type = "button";
  launcher.className = "waj-tc-launcher";
  launcher.setAttribute("aria-label", "Open test agent chat");
  launcher.innerHTML =
    '<span class="waj-tc-launcher-dot"></span>' +
    '<span>Test agent</span>';
  root.appendChild(launcher);

  // --- Panel --------------------------------------------------------------
  var panel = document.createElement("div");
  panel.className = "waj-tc-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Test agent chat");
  // A demo-tenant build (core.deployment.demo_tenant_build) resolves its merchant context server-side
  // (the demo-merchant fallback), so a WAJ business picker is N/A — show a note
  // but keep a hidden <select> so the rest of the widget is unchanged.
  var cases = Array.isArray(cfg.cases) ? cfg.cases : [];
  var stateToken = null;  // the demo's per-conversation state (verified, sent…)
  var ctxbarHtml = cases.length
    ? '<div class="waj-tc-ctxbar"><label class="waj-tc-ctxbar-label">case</label><select class="waj-tc-case" aria-label="case"></select><select class="waj-tc-business" hidden><option value="" selected></option></select></div>'
    : cfg.no_business_scope
    ? '<select class="waj-tc-business" hidden><option value="" selected></option></select>'
    : cfg.hide_business
    ? '<div class="waj-tc-ctxbar"><span class="waj-tc-ctxbar-label">Demo merchant — auto-resolved (no business needed)</span><select class="waj-tc-business" hidden><option value="" selected></option></select></div>'
    : '<div class="waj-tc-ctxbar"><label class="waj-tc-ctxbar-label">business</label><select class="waj-tc-business" aria-label="business"><option value="">— select a business —</option></select></div>';
  panel.innerHTML = [
    '<header class="waj-tc-header">',
    '  <div class="waj-tc-title">',
    '    <span class="waj-tc-title-eyebrow">TEST</span>',
    '    <span class="waj-tc-title-name"></span>',
    '  </div>',
    '  <span class="waj-tc-head-actions">',
    '    <a class="waj-tc-call" hidden>Call</a>',
    '    <button type="button" class="waj-tc-close" aria-label="Close">×</button>',
    '  </span>',
    '</header>',
    ctxbarHtml,
    cfg.pin_profile ? '<div class="waj-tc-ctxbar"><span class="waj-tc-ctxbar-label waj-tc-answering"></span></div>' : '',
    '<div class="waj-tc-messages" aria-live="polite"></div>',
    '<form class="waj-tc-composer">',
    '  <textarea class="waj-tc-input" rows="2" placeholder="Send a message…" data-tour="test-input"></textarea>',
    '  <button type="submit" class="waj-tc-send" aria-label="Send">Send</button>',
    '</form>',
    '<div class="waj-tc-status" role="status"></div>',
  ].join("");
  root.appendChild(panel);

  var titleEl = panel.querySelector(".waj-tc-title-name");
  var closeBtn = panel.querySelector(".waj-tc-close");
  // A voice-enabled agent (the public demo gives every agent a voice): the
  // Voice tab's Call, one click away. Only a same-portal path is used.
  var callLink = panel.querySelector(".waj-tc-call");
  if (typeof cfg.call_href === "string" && cfg.call_href.indexOf("/admin/") === 0) {
    callLink.href = cfg.call_href;
    callLink.title = "Talk to this agent with your microphone";
    callLink.hidden = false;
  }
  var bizInput = panel.querySelector(".waj-tc-business");
  var messagesEl = panel.querySelector(".waj-tc-messages");
  var composer = panel.querySelector(".waj-tc-composer");
  var inputEl = panel.querySelector(".waj-tc-input");
  var sendBtn = panel.querySelector(".waj-tc-send");
  var statusEl = panel.querySelector(".waj-tc-status");

  titleEl.textContent = cfg.profile_name || cfg.agent_kind || "agent";
  var answeringEl = panel.querySelector(".waj-tc-answering");
  if (answeringEl) {
    answeringEl.textContent = "Answers come from this agent — " +
      (cfg.profile_name || cfg.agent_kind || "agent") + " — even if it's a draft.";
  }
  var caseInput = panel.querySelector(".waj-tc-case");
  if (caseInput) {
    cases.forEach(function (c, i) {
      var opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.label || c.id;
      if ((state.case_id && state.case_id === c.id) || (!state.case_id && i === 0)) opt.selected = true;
      caseInput.appendChild(opt);
    });
    caseInput.addEventListener("change", function () {
      // A different case is a different conversation: new session, no state.
      state.case_id = caseInput.value;
      saveState();
      sessionId = uuidv4();
      stateToken = null;
      messagesEl.innerHTML = "";
      setStatus("New conversation for case " + caseInput.value, "info");
    });
  }

  // Populate the business select from cfg.businesses (passed via the
  // page's window.__waj_test_chat_config). Each entry is {id, name,
  // subdomain}. Pre-select the previously-used business from state, or
  // the optional cfg fallback.
  var businesses = Array.isArray(cfg.businesses) ? cfg.businesses : [];
  var preselect = state.business_id || cfg.business_id_optional || "";
  businesses.forEach(function (b) {
    var opt = document.createElement("option");
    opt.value = b.id;
    opt.textContent = b.name + (b.subdomain ? " (" + b.subdomain + ")" : "");
    if (b.id === preselect) opt.selected = true;
    bizInput.appendChild(opt);
  });
  // If state had a uuid that's not in the list (stale or different env),
  // append it as a free-form option so the operator still sees what was
  // selected last time instead of silently losing the value.
  if (preselect && !businesses.some(function (b) { return b.id === preselect; })) {
    var stale = document.createElement("option");
    stale.value = preselect;
    stale.textContent = preselect.slice(0, 8) + "… (not in list)";
    stale.selected = true;
    bizInput.appendChild(stale);
  }

  setOpen(!!state.open);

  // --- Events -------------------------------------------------------------
  launcher.addEventListener("click", function () { setOpen(true); });
  closeBtn.addEventListener("click", function () { setOpen(false); });
  bizInput.addEventListener("change", function () {
    state.business_id = bizInput.value.trim();
    saveState();
  });
  composer.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    sendMessage(text);
  });
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      composer.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  // --- Behavior -----------------------------------------------------------
  // The demo's guided tour listens for this (demo-tour.js); elsewhere nothing
  // does, so it is a no-op. Never lets a tour problem reach the chat.
  function tourReplied(text) {
    try {
      // The demo's cap answers as a normal reply: say so, so a step can stay open.
      var limited = /reached today's limit|going a little fast for the public demo/.test(String(text || ""));
      var tab = document.querySelector("[data-agent-slug]");
      var slug = (tab && tab.getAttribute("data-agent-slug")) || cfg.profile_id;
      window.dispatchEvent(new CustomEvent("tour:test-chat-sent", {
        detail: { profile: slug, profile_id: cfg.profile_id, limited: limited,
                  limit: limited ? (/going a little fast/.test(String(text || "")) ? "rate" : "daily") : null }
      }));
    } catch (e) { /* the tour is optional */ }
  }

  function setOpen(open) {
    state.open = !!open;
    saveState();
    panel.classList.toggle("waj-tc-open", state.open);
    launcher.classList.toggle("waj-tc-launcher-hidden", state.open);
    if (state.open) {
      setTimeout(function () { inputEl.focus(); }, 50);
    }
  }

  function setStatus(msg, variant) {
    statusEl.textContent = msg || "";
    statusEl.className = "waj-tc-status" + (variant ? " waj-tc-status-" + variant : "");
  }

  function appendMessage(role, text) {
    var wrap = document.createElement("div");
    wrap.className = "waj-tc-msg waj-tc-msg-" + role;
    var bubble = document.createElement("div");
    bubble.className = "waj-tc-bubble";
    bubble.textContent = text || "";
    wrap.appendChild(bubble);
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return bubble;
  }

  // Assistant replies are markdown (same renderer as transcripts: marked +
  // DOMPurify, both loaded by _base.html). User input and errors stay
  // textContent so they are never parsed as HTML. Falls back to plain text
  // when the CDN libs didn't load.
  function renderAssistantMarkdown(bubble, text) {
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
      bubble.textContent = text || "";
      return;
    }
    bubble.classList.add("markdown-body");
    bubble.innerHTML = DOMPurify.sanitize(
      marked.parse(text || "", { breaks: true, gfm: true })
    );
  }

  function sendMessage(text) {
    appendMessage("user", text);
    var assistantBubble = appendMessage("assistant", "");
    assistantBubble.classList.add("waj-tc-bubble-pending");
    setStatus("Sending…", "info");
    sendBtn.disabled = true;

    var businessId = (bizInput.value || "").trim() || null;
    var body = {
      message: text,
      session_id: sessionId,
      locale: "en",
      business_id: businessId,
      agent_kind: cfg.agent_kind,
    };
    var extra = cfg.extra_body || {};
    Object.keys(extra).forEach(function (k) { body[k] = extra[k]; });
    if (caseInput && caseInput.value) body.case_id = caseInput.value;
    if (cfg.state_token && stateToken) body.state = stateToken;
    var headers = {
      "Authorization": "Bearer " + cfg.user_token,
      "Content-Type": "application/json",
      "Accept": "text/event-stream, application/json;q=0.9, */*;q=0.5",
    };
    if (cfg.pin_profile && cfg.profile_id) headers["X-WAJ-Profile-Id"] = cfg.profile_id;

    fetch(cfg.endpoint, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body),
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.text().then(function (txt) {
          throw new Error("HTTP " + resp.status + " — " + (txt || resp.statusText || "request failed"));
        });
      }
      var ct = (resp.headers.get("Content-Type") || "").toLowerCase();
      var isSSE = ct.indexOf("text/event-stream") !== -1;
      if (!isSSE || !resp.body || !resp.body.getReader) {
        // Fall back: render the raw body once.
        return resp.text().then(function (txt) {
          // If the body looks like SSE despite the content-type (some
          // proxies strip it) try to extract chunks; otherwise show raw.
          var extracted = extractSseAnswers(txt);
          renderAssistantMarkdown(assistantBubble, extracted || txt || "(empty response)");
          assistantBubble.classList.remove("waj-tc-bubble-pending");
          setStatus("Done", "ok");
          tourReplied(extracted || txt);
        });
      }
      return streamSSE(resp.body.getReader(), assistantBubble);
    }).catch(function (err) {
      assistantBubble.classList.remove("waj-tc-bubble-pending");
      assistantBubble.classList.add("waj-tc-bubble-error");
      assistantBubble.textContent = String(err && err.message ? err.message : err);
      setStatus("Error", "err");
    }).then(function () {
      sendBtn.disabled = false;
    });
  }

  function streamSSE(reader, bubble) {
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    var acc = "";
    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) {
          // Flush trailing buffer if it contains a final event.
          if (buffer.trim()) processBuffer(buffer + "\n\n");
          bubble.classList.remove("waj-tc-bubble-pending");
          if (!acc) {
            bubble.textContent = bubble.textContent || "(no answer events)";
          }
          setStatus("Done", "ok");
          if (acc) tourReplied(acc);
          return;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        var idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          var raw = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          handleSseBlock(raw);
        }
        return pump();
      });
    }
    function processBuffer(buf) {
      var parts = buf.split("\n\n");
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].trim()) handleSseBlock(parts[i]);
      }
    }
    function handleSseBlock(block) {
      var lines = block.split("\n");
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (line.indexOf("data:") !== 0) continue;
        var raw = line.slice(5).replace(/^\s+/, "");
        if (!raw || raw === "[DONE]") continue;
        var evt;
        try { evt = JSON.parse(raw); } catch (_) { continue; }
        if (!evt || typeof evt !== "object") continue;
        if (evt.type === "answer" && typeof evt.content === "string") {
          acc += evt.content;
          renderAssistantMarkdown(bubble, acc);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (evt.type === "state" && typeof evt.token === "string") {
          stateToken = evt.token;
        } else if (evt.type === "error" && evt.message) {
          bubble.classList.add("waj-tc-bubble-error");
          bubble.textContent = String(evt.message);
        }
      }
    }
    return pump();
  }

  function extractSseAnswers(body) {
    if (!body) return "";
    var out = "";
    var blocks = body.split(/\n\n+/);
    for (var i = 0; i < blocks.length; i++) {
      var lines = blocks[i].split("\n");
      for (var j = 0; j < lines.length; j++) {
        var line = lines[j];
        if (line.indexOf("data:") !== 0) continue;
        var raw = line.slice(5).trim();
        if (!raw || raw === "[DONE]") continue;
        try {
          var evt = JSON.parse(raw);
          if (evt && evt.type === "answer" && typeof evt.content === "string") {
            out += evt.content;
          }
        } catch (_) { /* ignore */ }
      }
    }
    return out;
  }

  // --- Helpers ------------------------------------------------------------
  function loadState() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") return parsed;
      }
    } catch (_) { /* ignore */ }
    return { open: false, business_id: "" };
  }
  function saveState() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (_) { /* ignore */ }
  }
  function uuidv4() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function injectStyles() {
    if (document.getElementById("waj-test-chat-styles")) return;
    // Every color comes from the portal's semantic tokens (portal.css) so
    // the widget tracks light/dark automatically. No hardcoded surfaces —
    // a literal #fff here turns into white-on-white text in dark mode.
    var css = [
      "#waj-test-chat-root{position:fixed;right:24px;bottom:24px;z-index:9999;font-family:var(--font-ui,'Inter',system-ui,sans-serif);}",
      ".waj-tc-launcher{display:inline-flex;align-items:center;gap:8px;padding:10px 16px;border-radius:var(--radius-pill,999px);background:var(--accent,#006A6A);color:var(--accent-fg,#fff);border:0;font-weight:600;font-size:12px;letter-spacing:0.3px;box-shadow:0 6px 16px rgba(0,0,0,0.22);cursor:pointer;transition:transform .12s ease, box-shadow .12s ease;}",
      ".waj-tc-launcher:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(0,0,0,0.3);}",
      ".waj-tc-launcher-hidden{display:none;}",
      ".waj-tc-launcher-dot{width:8px;height:8px;border-radius:50%;background:currentColor;opacity:0.85;}",
      ".waj-tc-panel{display:none;flex-direction:column;width:380px;height:520px;background:var(--surface,#FFF);border:1px solid var(--border,#E5E7EB);border-radius:var(--radius-card,14px);box-shadow:0 12px 36px rgba(0,0,0,0.25);overflow:hidden;}",
      ".waj-tc-panel.waj-tc-open{display:flex;}",
      ".waj-tc-header{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--accent-soft,#F1F9F7);border-bottom:1px solid var(--border,#E5E7EB);}",
      ".waj-tc-title{display:flex;align-items:center;gap:8px;min-width:0;}",
      ".waj-tc-title-eyebrow{font-size:9.5px;font-weight:700;letter-spacing:1.4px;color:var(--accent,#006A6A);background:var(--surface,#fff);border:1px solid var(--border-strong,#B7DCD7);border-radius:999px;padding:1px 6px;}",
      ".waj-tc-title-name{font-size:12px;font-weight:600;color:var(--fg-1,#1B1C17);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
      ".waj-tc-head-actions{display:flex;align-items:center;gap:6px;}",
      ".waj-tc-call{font-size:12px;font-weight:600;color:var(--accent,#006A6A);border:1px solid var(--border-strong,#B7DCD7);border-radius:999px;padding:2px 10px;text-decoration:none;background:var(--surface,#fff);}",
      ".waj-tc-call[hidden]{display:none;}",
      ".waj-tc-call:hover{background:var(--surface-sunken,#F3F4F6);}",
      ".waj-tc-close{background:transparent;border:0;color:var(--fg-2,#787771);font-size:20px;line-height:1;cursor:pointer;padding:2px 6px;border-radius:6px;}",
      ".waj-tc-close:hover{background:var(--surface-sunken,#F3F4F6);color:var(--fg-1,#1B1C17);}",
      ".waj-tc-ctxbar{display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--border,#E5E7EB);background:var(--surface,#fff);}",
      ".waj-tc-ctxbar-label{font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--fg-3,#92918A);}",
      ".waj-tc-business,.waj-tc-case{flex:1;font-size:12px;padding:4px 8px;border:1px solid var(--border,#E5E7EB);border-radius:6px;background:var(--surface,#fff);color:var(--fg-1,#1B1C17);}",
      ".waj-tc-answering{text-transform:none;letter-spacing:0;font-weight:400;font-size:11px;color:var(--fg-2,#5F5E57);}",
      ".waj-tc-messages{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:var(--bg,#F9FBFB);}",
      ".waj-tc-msg{display:flex;}",
      ".waj-tc-msg-user{justify-content:flex-end;}",
      ".waj-tc-msg-assistant{justify-content:flex-start;}",
      ".waj-tc-bubble{max-width:82%;padding:8px 12px;border-radius:12px;font-size:12px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word;}",
      ".waj-tc-msg-user .waj-tc-bubble{background:var(--accent,#006A6A);color:var(--accent-fg,#fff);border-bottom-right-radius:4px;}",
      ".waj-tc-msg-assistant .waj-tc-bubble{background:var(--surface,#fff);color:var(--fg-1,#1B1C17);border:1px solid var(--border,#E5E7EB);border-bottom-left-radius:4px;}",
      // Rendered markdown switches off the plain-text pre-wrap (block
      // elements bring their own spacing) and clamps wide content.
      ".waj-tc-bubble.markdown-body{white-space:normal;font-size:12px;}",
      ".waj-tc-bubble.markdown-body pre{max-width:100%;overflow-x:auto;}",
      ".waj-tc-bubble.markdown-body img{max-width:100%;}",
      ".waj-tc-bubble-pending::after{content:'…';color:var(--fg-3,#92918A);}",
      ".waj-tc-bubble-error{background:var(--error-soft,#FEE2E2) !important;color:var(--error,#DC2626) !important;border-color:var(--error,#DC2626) !important;}",
      ".waj-tc-composer{display:flex;gap:6px;padding:8px;border-top:1px solid var(--border,#E5E7EB);background:var(--surface,#fff);}",
      ".waj-tc-input{flex:1;resize:none;border:1px solid var(--border,#E5E7EB);border-radius:8px;padding:6px 8px;font-family:inherit;font-size:12px;outline:none;background:var(--surface,#fff);color:var(--fg-1,#1B1C17);}",
      ".waj-tc-input:focus{border-color:var(--accent,#006A6A);box-shadow:0 0 0 2px var(--accent-soft,rgba(0,106,106,0.15));}",
      ".waj-tc-send{background:var(--accent,#006A6A);color:var(--accent-fg,#fff);border:0;border-radius:8px;padding:0 14px;font-weight:600;font-size:12px;cursor:pointer;}",
      ".waj-tc-send:disabled{opacity:0.6;cursor:wait;}",
      ".waj-tc-status{padding:4px 14px;font-size:10px;color:var(--fg-3,#92918A);min-height:16px;border-top:1px solid var(--border,#E5E7EB);background:var(--surface,#fff);}",
      ".waj-tc-status-ok{color:var(--success,#16A34A);}",
      ".waj-tc-status-err{color:var(--error,#DC2626);}",
      ".waj-tc-status-info{color:var(--accent,#006A6A);}",
    ].join("");
    var s = document.createElement("style");
    s.id = "waj-test-chat-styles";
    s.textContent = css;
    document.head.appendChild(s);
  }
})();
