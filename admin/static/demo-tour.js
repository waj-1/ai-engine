// The public demo's guided tour (spec: oss-publish/demo-tour/spec.md).
//
// Loaded by _base.html only when demo mode and DEMO_TOUR are both on, after
// demo-tour-steps.js, which sets window.DEMO_TOUR_CATALOG (the 17 steps and
// every word of copy). This file owns the logic and the UI:
//
//   - state in localStorage "demo-tour:v1", keyed by the nightly reset id
//     (falls back to sessionStorage, then to memory, when storage throws);
//   - step completion on every page load, whether or not any tour UI is open
//     ("by hand"), from the catalog's `done` rules;
//   - the Welcome and Finish dialogs (real modals), the checklist drawer
//     (non-modal), coach marks (non-modal cards with a spotlight ring), the
//     Code step card, the resume card on Overview, toasts, the sidebar entry
//     and the phone pill;
//   - the AI handoff through window.demoAssistant (builder-assistant.js).
//
// Nothing here writes to the server. Events in: tour:test-chat-sent,
// tour:ai-done, tour:palette-used, tour:run-started, tour:run-finished,
// tour:saved, tour:resume, tour:restart. Event out: tour:step-done.
(function () {
  "use strict";

  // ------------------------------------------------------------ pure logic
  // No DOM, no storage: exported as window.__demoTourLogic so
  // tests/admin/test_demo_tour_logic.py can run it under node.
  var LOGIC = {
    // Saved progress belongs to this demo data only.
    sameReset: function (saved, resetId) {
      return !!saved && typeof saved === "object" && saved.reset_id === resetId;
    },
    // Saved progress this page may keep. A date (YYYY-MM-DD) is the server's
    // fallback when it could not read the data's generation for a moment:
    // it says nothing about a wipe, so progress saved under a generation
    // is kept rather than dropped by one such page.
    keepsProgress: function (saved, resetId) {
      if (LOGIC.sameReset(saved, resetId)) return true;
      var date = /^\d{4}-\d{2}-\d{2}$/;
      return !!saved && typeof saved === "object" && typeof saved.reset_id === "string" &&
        date.test(String(resetId)) && !!saved.reset_id && saved.reset_id !== "none" && !date.test(saved.reset_id);
    },
    // The profile id in an agent page's link (/admin/agents?…profile_id=…), or null.
    agentLink: function (href) {
      var m = /^\/admin\/agents\?(?:[^#]*&)?profile_id=([0-9a-fA-F-]{32,36})/.exec(String(href || ""));
      return m ? m[1].toLowerCase() : null;
    },
    aiHit: function (list, tools, actions) {
      return Array.isArray(list) && list.some(function (x) { return tools.indexOf(x) >= 0 || actions.indexOf(x) >= 0; });
    },
    // A spec ({on:"ai"} rule or step.ai) with `actions` matches by "tool:action"
    // (a read such as templates:list never ticks A3); an entry that is a list
    // needs all of its actions in the turn (A4: an MCP server and a knowledge
    // base). Without `actions`, by tool name.
    aiSpecHit: function (sp, tools, actions) {
      if (!sp) return false;
      tools = tools || []; actions = actions || [];
      if (Array.isArray(sp.actions) && sp.actions.length) {
        return sp.actions.some(function (a) {
          return Array.isArray(a) ? a.every(function (x) { return actions.indexOf(x) >= 0; }) : actions.indexOf(a) >= 0;
        });
      }
      return LOGIC.aiHit(sp.tools, tools, actions);
    },
    // Is the agent or run named in an event's detail one this browser made?
    isMine: function (st, detail) {
      if (!detail) return false;
      if (detail.run_id) return !!(st.runs && st.runs[detail.run_id]);
      var made = st.made || {};
      if (detail.profile && (made.agents || []).indexOf(detail.profile) >= 0) return true;
      return !!detail.profile_id && (made.agent_ids || []).indexOf(detail.profile_id) >= 0;
    },
    // A rule with `grew: key` needs detail[key] above the count the tab loaded with.
    grew: function (st, r, detail) {
      if (!r || !r.grew) return true;
      var b = st.flags && st.flags.base && st.flags.base[detail.profile_id];
      var before = b && typeof b[r.grew] === "number" ? b[r.grew] : 0;
      return Number(detail[r.grew] || 0) > before;
    },
    // Fold another tab's saved progress into `st` (steps, made, runs): two
    // tabs never undo each other. Returns true when anything was added.
    mergeStates: function (st, other, resetId, known) {
      if (!LOGIC.sameReset(other, resetId)) return false;
      var changed = false;
      Object.keys(other.steps || {}).forEach(function (id) {
        if (!st.steps[id] && (!known || known[id])) { st.steps[id] = other.steps[id]; changed = true; }
      });
      var om = other.made || {};
      ["agents", "runs", "agent_ids"].forEach(function (k) {
        (Array.isArray(om[k]) ? om[k] : []).forEach(function (v) {
          if (st.made[k].indexOf(v) < 0) { st.made[k].push(v); changed = true; }
        });
      });
      Object.keys(other.runs || {}).forEach(function (id) {
        var o = other.runs[id];
        if (!o) return;
        if (!st.runs[id]) { st.runs[id] = o; changed = true; }
        else if (o.finished && !st.runs[id].finished) { st.runs[id].finished = o.finished; changed = true; }
      });
      if (other.finish_shown && !st.finish_shown) { st.finish_shown = true; changed = true; }
      return changed;
    }
  };
  window.__demoTourLogic = LOGIC;

  var CAT = window.DEMO_TOUR_CATALOG;
  if (!CAT || !Array.isArray(CAT.steps) || !CAT.steps.length) return;
  if (window.__demoTour) return;  // loaded twice: keep the first

  var KEY = "demo-tour:v1";
  var PENDING_CLICK_MS = 5 * 60 * 1000;
  var LOAD_ID = Math.random().toString(36).slice(2);  // this page load
  var RUN_STALE_MS = 15 * 60 * 1000;
  var ANCHOR_WAIT_MS = 4000;
  var PHONE = "(max-width: 639.98px)";
  var TERMINAL = { passed: 1, failed: 1, errored: 1, cancelled: 1, error: 1, completed: 1, done: 1 };
  var CODE_LABEL = { portal: "Portal", mcp: "MCP", curl: "curl", python: "Python", ci: "CI" };
  var CODE_ORDER = ["portal", "mcp", "curl", "python", "ci"];

  // ---------------------------------------------------------------- config
  var config = {};
  try {
    var cfgEl = document.getElementById("demo-tour-config");
    config = JSON.parse((cfgEl && cfgEl.textContent) || "{}") || {};
  } catch (e) { config = {}; }
  var RESET_ID = String(config.reset_id || "none");

  var STEPS = CAT.steps;
  var BY_ID = {};
  STEPS.forEach(function (s) { BY_ID[s.id] = s; });
  var TRACKS = Array.isArray(CAT.tracks) ? CAT.tracks : [];
  var TRACK_BY_ID = {};
  TRACKS.forEach(function (t) { TRACK_BY_ID[t.id] = t; });
  var TOTAL = STEPS.length;

  // ------------------------------------------------------------------ copy
  var COPY = CAT.copy || {};
  function fill(str, vars) {
    return String(str).replace(/\{(\w+)\}/g, function (m, k) {
      return vars && vars[k] !== undefined && vars[k] !== null ? String(vars[k]) : m;
    });
  }
  function t(path, fallback, vars) {
    var cur = COPY;
    var parts = path.split(".");
    for (var i = 0; i < parts.length; i++) {
      if (cur && typeof cur === "object" && parts[i] in cur) cur = cur[parts[i]];
      else { cur = undefined; break; }
    }
    if (typeof cur !== "string" || !cur) cur = fallback;
    return fill(cur, vars);
  }

  // --------------------------------------------------------------- storage
  var memory = {};
  function pickStore() {
    var probe = "demo-tour:probe";
    try { localStorage.setItem(probe, "1"); localStorage.removeItem(probe); return localStorage; } catch (e) { /* blocked */ }
    try { sessionStorage.setItem(probe, "1"); sessionStorage.removeItem(probe); return sessionStorage; } catch (e2) { /* blocked */ }
    return null;
  }
  var store = pickStore();
  // With both storages blocked, the tab's window.name carries the state from
  // page to page for the session (only when it is empty or already ours).
  var NAME_PREFIX = "demo-tour:v1=";
  function nameRead() {
    try { var n = String(window.name || ""); return n.indexOf(NAME_PREFIX) === 0 ? n.slice(NAME_PREFIX.length) : null; } catch (e) { return null; }
  }
  function nameWrite(value) {
    try { var n = String(window.name || ""); if (!n || n.indexOf(NAME_PREFIX) === 0) window.name = NAME_PREFIX + value; } catch (e) { /* nothing more to try */ }
  }
  function readRaw() {
    try { if (store) return store.getItem(KEY); } catch (e) { store = null; }
    return memory[KEY] || nameRead() || null;
  }
  function writeRaw(value) {
    memory[KEY] = value;
    try { if (store) { store.setItem(KEY, value); return; } } catch (e) { store = null; }
    nameWrite(value);
  }

  function freshState() {
    return {
      key: KEY, reset_id: RESET_ID, goal: null, code_lang: "mcp", offer_ai: true,
      status: "new", steps: {}, active: null, made: { agents: [], runs: [], agent_ids: [] },
      hidden: { overview_card: false },
      ui: { drawer: false, hide_done: false, open_tracks: {}, expanded: null },
      pending_click: null, runs: {}, ai: null, limited: {}, finish_shown: false,
      flags: {}
    };
  }
  function load() {
    var s = null;
    try { s = JSON.parse(readRaw() || "null"); } catch (e) { s = null; }
    if (!LOGIC.keepsProgress(s, RESET_ID)) return freshState();
    var base = freshState();
    Object.keys(base).forEach(function (k) { if (!(k in s)) s[k] = base[k]; });
    ["steps", "runs", "limited", "hidden", "flags"].forEach(function (k) { if (!s[k] || typeof s[k] !== "object") s[k] = base[k]; });
    if (!s.made || typeof s.made !== "object") s.made = base.made;
    if (!Array.isArray(s.made.agents)) s.made.agents = [];
    if (!Array.isArray(s.made.runs)) s.made.runs = [];
    if (!Array.isArray(s.made.agent_ids)) s.made.agent_ids = [];
    if (!s.ui || typeof s.ui !== "object") s.ui = base.ui;
    if (!s.ui.open_tracks) s.ui.open_tracks = {};
    if (s.active && !BY_ID[s.active.step]) s.active = null;
    return s;
  }
  var state = load();
  // Another tab may have saved since this page loaded: fold its progress in
  // (steps, made, runs) so two tabs never undo each other.
  function mergeFrom(other) { return LOGIC.mergeStates(state, other, state.reset_id, BY_ID); }
  function save(fresh) {
    if (state.active) {
      // When the walkthrough last moved: a card that comes up on the next page
      // soon after takes focus, as a card shown in place does.
      var key = state.active.step + ":" + state.active.card;
      if (state.active.key !== key) { state.active.key = key; state.active.at = Date.now(); }
    }
    if (!fresh) safe(function () { mergeFrom(JSON.parse(readRaw() || "null")); });
    writeRaw(JSON.stringify(state));
  }

  // ------------------------------------------------------------- variants
  // A step may carry `alt`: fields that stand in when Overview has no "Needs
  // attention" list (D2: no tracing store, or no traffic yet). The server's
  // `tracing` gives the first guess; each visit to Overview settles it.
  var ALT_KEYS = ["title", "summary", "short", "start", "cards", "done", "doneWhen", "code", "ai"];
  function attentionShown() {
    if (typeof state.flags.attention === "boolean") return state.flags.attention;
    return config.tracing !== false;
  }
  function applyVariants() {
    var primary = attentionShown();
    STEPS.forEach(function (s) {
      if (!s.alt) return;
      if (!s.__primary) {
        s.__primary = {};
        ALT_KEYS.forEach(function (k) { s.__primary[k] = s[k]; });
      }
      var use = primary ? s.__primary : s.alt;
      ALT_KEYS.forEach(function (k) { if (k in use || k in s.__primary) s[k] = k in use ? use[k] : s.__primary[k]; });
    });
  }
  // On Overview: is the list there? Returns true when that changed the steps.
  function learnAttention() {
    if (String(location.pathname).replace(/\/+$/, "") !== "/admin/live") return false;
    if (!document.querySelector('[data-tour="overview-kpis"]')) return false;  // not the full Overview
    // The list with at least one conversation to open (an empty list, "no
    // failed actions", leaves nothing to do there either).
    var has = !!document.querySelector('[data-tour="needs-attention"] a[href*="#trace="]');
    var before = attentionShown();
    state.flags.attention = has;
    save();
    if (before === has) return false;
    applyVariants();
    return true;
  }
  applyVariants();

  // --------------------------------------------------------------- helpers
  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else if (k.indexOf("on") === 0 && typeof v === "function") n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? "" : v);
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }
  function btn(label, cls, onclick, extra) {
    var a = { type: "button", class: cls || "", onclick: onclick };
    if (extra) Object.keys(extra).forEach(function (k) { a[k] = extra[k]; });
    return el("button", a, [label]);
  }
  var uid = 0;
  function nextId(p) { uid += 1; return "dt-" + p + "-" + uid; }
  function reducedMotion() {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; }
  }
  function isPhone() {
    try { return window.matchMedia(PHONE).matches; } catch (e) { return window.innerWidth < 640; }
  }
  function safe(fn, fallback) {
    try { return fn(); } catch (e) { return fallback; }
  }
  function visible(node) {
    if (!node || !node.isConnected) return false;
    if (node.closest("[hidden]")) return false;
    // Inside a closed <details> (a shut menu) only its summary shows, though
    // Chrome still reports boxes for the rest.
    var shut = node.parentElement && node.parentElement.closest("details:not([open])");
    if (shut) {
      var sum = shut.querySelector(":scope > summary");
      if (!sum || !sum.contains(node)) return false;
    }
    var r = node.getClientRects();
    return r.length > 0 && (r[0].width > 0 || r[0].height > 0);
  }
  // The anchor element: the first visible [data-tour=name]; with `pick`
  // {attr, value} the one whose attr equals value (value may be fn(made)),
  // else the first visible one.
  function findAnchor(name, pick) {
    if (!name) return null;
    var all = document.querySelectorAll('[data-tour="' + String(name).replace(/"/g, "") + '"]');
    var want = null;
    if (pick && pick.attr) {
      want = safe(function () { return typeof pick.value === "function" ? pick.value(state.made) : pick.value; }, null);
    }
    var first = null;
    for (var i = 0; i < all.length; i++) {
      if (!visible(all[i])) continue;
      if (!first) first = all[i];
      if (want !== null && want !== undefined && all[i].getAttribute(pick.attr) === String(want)) return all[i];
    }
    // A card that names its element (the visitor's agent, the Support desk
    // suite) never falls back to some other row.
    if (pick && pick.attr) return null;
    return first;
  }
  // A modal <dialog> (the New agent dialog) sits in the top layer and makes
  // the rest of the page inert, so a card for an anchor inside one is
  // mounted inside that dialog, where it can be seen and clicked.
  function hostFor(anchor) {
    var d = anchor && anchor.closest && anchor.closest("dialog[open]");
    if (d && safe(function () { return d.matches(":modal"); }, true)) return d;
    if (!anchor) {
      // A centred card while a page dialog is modal: inside it, or unseen.
      var open = document.querySelectorAll("dialog[open]");
      for (var i = open.length - 1; i >= 0; i--) {
        if (safe(function () { return open[i].matches(":modal"); }, false)) return open[i];
      }
    }
    return document.body;
  }
  // A later card whose anchor is already on screen (the page moved ahead of
  // the walkthrough, e.g. picking a template jumps the dialog to its name step).
  function laterCardHere() {
    if (!state.active) return -1;
    var step = BY_ID[state.active.step];
    var cards = (step && step.cards) || [];
    for (var i = state.active.card + 1; i < cards.length; i++) {
      if (cards[i].anchor && cardAnchor(cards[i])) return i;
    }
    return -1;
  }
  function cardAnchor(card) { return card ? findAnchor(card.anchor, card.pick) : null; }
  function myAgent() {
    var a = state.made.agents;
    return a.length ? a[a.length - 1] : "support-desk";
  }
  function subst(code) {
    var repo = String(config.repo_url || "").replace(/\/+$/, "");
    return String(code || "").replace(/\{BASE\}/g, location.origin).replace(/\{AGENT\}/g, myAgent())
      .replace(/\{REPO\}/g, repo);
  }
  function stepTrack(step) { return TRACK_BY_ID[step.track] || { id: step.track, title: step.track || "", letter: "" }; }
  function trackSteps(trackId) { return STEPS.filter(function (s) { return s.track === trackId; }); }
  function isDone(id) { return !!state.steps[id]; }
  function doneCount() { return STEPS.filter(function (s) { return isDone(s.id); }).length; }
  function trackOrder() {
    var ids = TRACKS.map(function (tr) { return tr.id; });
    STEPS.forEach(function (s) { if (ids.indexOf(s.track) < 0) ids.push(s.track); });
    if (state.goal && ids.indexOf(state.goal) >= 0) {
      ids.splice(ids.indexOf(state.goal), 1);
      ids.unshift(state.goal);
    }
    return ids;
  }
  function orderedSteps() {
    var out = [];
    trackOrder().forEach(function (tid) { out = out.concat(trackSteps(tid)); });
    return out;
  }
  function nextStep(afterId) {
    var list = orderedSteps();
    var start = 0;
    if (afterId) {
      var i = list.findIndex(function (s) { return s.id === afterId; });
      if (i >= 0) start = i + 1;
    }
    for (var k = 0; k < list.length; k++) {
      var s = list[(start + k) % list.length];
      if (!isDone(s.id)) return s;
    }
    return null;
  }
  function stepLabel(step) {
    var list = trackSteps(step.track);
    var tr = stepTrack(step);
    return t("coach.stepOf", "{track} · {n} of {total}",
             { track: tr.eyebrow || tr.title, n: list.indexOf(step) + 1, total: list.length });
  }
  // A step's start page: a path, null (stay here) or a Promise of either.
  function startPath(step) {
    var p = safe(function () { return typeof step.start === "function" ? step.start(state.made) : step.start; }, null);
    return Promise.resolve(p).then(function (v) { return v ? subst(v) : null; }, function () { return null; });
  }
  function samePlace(path) {
    if (!path) return true;
    var u;
    try { u = new URL(path, location.origin); } catch (e) { return true; }
    if (u.pathname.replace(/\/$/, "") !== location.pathname.replace(/\/$/, "")) return false;
    var want = u.searchParams;
    var have = new URLSearchParams(location.search);
    var ok = true;
    want.forEach(function (v, k) { if (k !== "env" && have.get(k) !== v) ok = false; });
    // /admin/agents is both the list and (with profile_id) one agent's page.
    if (have.get("profile_id") && !want.get("profile_id")) ok = false;
    return ok;
  }
  function goTo(path) {
    if (path) window.location.assign(path);
  }
  function aiAvailable(step) {
    return !!(step && step.ai && step.ai.prompt && state.offer_ai !== false && window.demoAssistant &&
              typeof window.demoAssistant.ask === "function" && !missingFor(step));
  }
  // The first step this one needs done first (A4, A5, C1 work on the agent the
  // visitor made in A3, never on the shared ones), or null.
  function missingFor(step) {
    var req = (step && Array.isArray(step.requires)) ? step.requires : [];
    for (var i = 0; i < req.length; i++) if (!isDone(req[i]) && BY_ID[req[i]]) return BY_ID[req[i]];
    return null;
  }
  function needsText(req) {
    return t("coach.needs", "This step changes an agent of your own, so the shared agents stay the same for everyone. Do “{title}” first.", { title: req.title });
  }
  // A tour turn is running (for a few minutes at most): one at a time.
  function aiBusy() {
    return !!(state.ai && !state.ai.missed && Date.now() - (state.ai.at || 0) < 4 * 60 * 1000);
  }
  // The daily cap and the hourly pace limit say different things: "try again
  // tomorrow" would be wrong after "wait a few minutes" in the assistant.
  function limitText(kind) {
    if (kind === "rate") return t("limitRate", "The demo is busy right now. Do it by hand, or try again in a few minutes");
    return t("limit", "The demo has reached today's limit. Do it by hand, or try again tomorrow");
  }
  function viaLabel(via) {
    if (via === "ai") return t("checklist.viaAi", "via AI");
    if (via === "walkthrough") return t("checklist.viaWalkthrough", "via walkthrough");
    return t("checklist.viaHand", "by hand");
  }

  // ------------------------------------------------------------ live region
  var live = el("div", { class: "dt-sr", "aria-live": "polite", role: "status" });
  function announce(text) {
    live.textContent = "";
    setTimeout(function () { live.textContent = text; }, 30);
  }

  // ------------------------------------------------------------ completion
  function requirementsMet(step) {
    var req = Array.isArray(step.requires) ? step.requires : [];
    for (var i = 0; i < req.length; i++) if (!isDone(req[i])) return false;
    var any = Array.isArray(step.requiresAny) ? step.requiresAny : [];
    if (any.length && !any.some(isDone)) return false;
    return true;
  }
  function pushUnique(list, v) { if (v && list.indexOf(v) < 0) list.push(v); }
  function recordMade(step, detail, via) {
    if (!step.records) return;
    if (step.records === "agent") {
      var tab = document.querySelector("[data-agent-tab][data-profile-id]");
      if (via !== "ai" && tab) pushUnique(state.made.agent_ids, tab.getAttribute("data-profile-id"));
      if (via === "ai") {
        // The assistant made it: its step links name the agent page. Read
        // the slug from that page so "your agent" works for A4, A5 and C1.
        ((detail && detail.links) || []).forEach(function (href) {
          var m = /[?&]profile_id=([0-9a-f-]{36})/i.exec(String(href));
          if (!m || String(href).indexOf("/admin/") !== 0) return;
          pushUnique(state.made.agent_ids, m[1]);
          fetch(href, { credentials: "same-origin" }).then(function (r) { return r.ok ? r.text() : ""; }).then(function (html) {
            var sm = /data-agent-slug="([^"]+)"/.exec(html || "");
            if (sm) { pushUnique(state.made.agents, sm[1]); save(); renderAll(); }
          }).catch(function () { /* the slug stays unknown: Support desk is used */ });
        });
        return;
      }
    }
    var value = safe(function () {
      return typeof step.recordFrom === "function" ? step.recordFrom(location, document, detail || {}) : null;
    }, null);
    if (!value) return;
    var list = step.records === "run" ? state.made.runs : state.made.agents;
    if (list.indexOf(value) < 0) list.push(value);
  }
  function complete(id, via, detail) {
    var step = BY_ID[id];
    if (!step || isDone(id) || !requirementsMet(step)) return false;
    if (via !== "ai" && via !== "walkthrough") via = (state.active && state.active.step === id) ? "walkthrough" : "hand";
    state.steps[id] = { via: via, at: Math.floor(Date.now() / 1000) };
    delete state.limited[id];
    if (state.ai && state.ai.step === id) state.ai = null;
    recordMade(step, detail, via);
    if (state.status === "new" || state.status === "dismissed") {
      // A step done before the visitor met the tour still counts; it does not
      // start the tour for them.
    }
    save();
    safe(function () {
      window.dispatchEvent(new CustomEvent("tour:step-done", { detail: { step: id, via: via === "hand" ? "hand" : via } }));
    });
    onStepDone(step, via, detail);
    return true;
  }
  function showFinishOnce() {
    if (state.finish_shown || doneCount() !== TOTAL || finish.modal.isOpen()) return;
    state.finish_shown = true;
    save();
    finish.open(null);
  }
  // The agent an AI turn made (A3 via AI): { href, label } of its page, from
  // the turn's links and the answer's own link text, or null.
  function madeAgent(detail) {
    var links = (detail && Array.isArray(detail.links)) ? detail.links : [];
    var href = null;
    for (var i = 0; i < links.length; i++) {
      if (LOGIC.agentLink(links[i])) { href = links[i]; break; }
    }
    if (!href) return null;
    var label = t("toast.openAgent", "Open your agent");
    var re = /\[([^\]]+)\]\((\/admin\/[^)\s]+)\)/g, m;
    while ((m = re.exec(String(detail.text || "")))) {
      if (m[2] === href || LOGIC.agentLink(m[2]) === LOGIC.agentLink(href)) {
        label = /^open\b/i.test(m[1]) ? m[1] : t("toast.openNamed", "Open {name}", { name: m[1] });
        break;
      }
    }
    return { href: href, label: label };
  }
  function onStepDone(step, via, detail) {
    renderAll();
    var all = doneCount() === TOTAL;
    var walking = state.active && state.active.step === step.id;
    var reading = false;
    if (walking) {
      // A reading card still ahead (A1, B1, D1) waits for Next, and its
      // toast comes with Next; anything else closes the walkthrough now.
      var cards = step.kind === "code" ? [{ advance: { on: "next" } }] : (step.cards || []);  // a code card stays open after Copy
      for (var i = step.kind === "code" ? 0 : state.active.card; i < cards.length; i++) {
        if (cards[i] && cards[i].advance && cards[i].advance.on === "next") { reading = true; break; }
      }
      if (!reading) endWalkthrough(false);
      else coach.refresh();
    }
    if (all && !state.finish_shown) {
      state.status = "done";
      save();
      // Marked shown only once it is on screen: a page change in between
      // (the palette opening a result) shows it on the next page instead.
      setTimeout(showFinishOnce, walking ? 600 : 200);
      return;
    }
    if (!reading && (state.status === "active" || walking)) {
      var open = via === "ai" && step.records === "agent" ? madeAgent(detail) : null;
      toast.show(step, via, nextStep(step.id), open);
    }
  }

  function rules(step) { return Array.isArray(step.done) ? step.done : []; }

  function checkPages() {
    STEPS.forEach(function (step) {
      if (isDone(step.id)) return;
      rules(step).forEach(function (r) {
        if (r.on === "page" && typeof r.match === "function" && safe(function () { return r.match(location, document); }, false)) {
          complete(step.id, null);
        }
      });
    });
    // Click-then-page: the click was recorded on the previous page.
    var pc = state.pending_click;
    if (pc) {
      if (Date.now() - pc.at > PENDING_CLICK_MS || isDone(pc.step)) { state.pending_click = null; save(); return; }
      var step = BY_ID[pc.step];
      var rule = step && rules(step).filter(function (r) { return r.on === "click-then-page" && r.anchor === pc.anchor; })[0];
      if (!rule) { state.pending_click = null; save(); return; }
      var hit = typeof rule.match === "function" ? safe(function () { return rule.match(location, document); }, false)
                                                 : location.pathname !== pc.from;
      // A step that records an agent needs one this browser has not counted
      // yet (an agent page opened later is not the one just created).
      if (hit && step.records === "agent") {
        var tab = document.querySelector("[data-agent-tab][data-profile-id]");
        if (!tab || state.made.agent_ids.indexOf(tab.getAttribute("data-profile-id")) >= 0) hit = false;
      }
      if (hit) {
        state.pending_click = null;
        complete(step.id, pc.walkthrough ? "walkthrough" : null);
        save();
      } else if (pc.load !== LOAD_ID) {
        // Only the very next page counts: anything else means it did not go through.
        state.pending_click = null;
        save();
      }
    }
  }

  function onAnchorClick(e) {
    var target = e.target && e.target.closest && e.target.closest("[data-tour]");
    if (!target) return;
    var name = target.getAttribute("data-tour");
    STEPS.forEach(function (step) {
      if (isDone(step.id)) return;
      rules(step).forEach(function (r) {
        if (r.anchor !== name) return;
        if (r.on === "click") { if (!r.mine || onMyAgentPage()) complete(step.id, null); }
        else if (r.on === "click-then-page") {
          state.pending_click = { step: step.id, anchor: name, at: Date.now(), from: location.pathname, load: LOAD_ID,
                                  walkthrough: !!(state.active && state.active.step === step.id) };
          save();
          // A same-page result (a dialog, an htmx swap) confirms a moment later.
          setTimeout(checkPages, 1200);
        }
      });
    });
    coach.onAnchorClick(name, target);
  }

  // Is this page one of the visitor's own agents?
  function onMyAgentPage() {
    var tab = document.querySelector("[data-agent-tab]");
    if (!tab) return false;
    return state.made.agent_ids.indexOf(tab.getAttribute("data-profile-id")) >= 0 ||
           state.made.agents.indexOf(tab.getAttribute("data-agent-slug")) >= 0;
  }
  // Row counts on an agent tab as it loaded before any save, so a save can be
  // told to have added something (A5: a sub-agent, not a re-save).
  var COUNTS = { toolsets: '[data-attached-list="toolset"] [data-row]', knowledge: '[data-attached-list="knowledge"] [data-row]',
                 subagents: "#form-subagents [data-row]" };
  function recordBaselines() {
    var tab = document.querySelector("[data-agent-tab][data-profile-id]");
    if (!tab || document.querySelector("[data-flash]")) return;
    var id = tab.getAttribute("data-profile-id");
    var base = state.flags.base || (state.flags.base = {});
    Object.keys(COUNTS).forEach(function (k) {
      var host = k === "subagents" ? document.getElementById("form-subagents") : document.querySelector('[data-attached-list="' + (k === "toolsets" ? "toolset" : "knowledge") + '"]');
      if (!host) return;
      (base[id] || (base[id] = {}))[k] = document.querySelectorAll(COUNTS[k]).length;
    });
  }
  function grew(r, detail) { return LOGIC.grew(state, r, detail); }
  // Is the agent or run named in an event's detail one this browser made?
  function isMine(detail) { return LOGIC.isMine(state, detail); }
  function onEvent(name, detail) {
    detail = detail || {};
    STEPS.forEach(function (step) {
      if (isDone(step.id)) return;
      rules(step).forEach(function (r) {
        if (r.on !== "event" || r.name !== name) return;
        // A save on "your agent" (A4 needs A3's agent) counts only for an
        // agent this browser made; a run only when this browser started it.
        var mineOnly = r.mine || (name === "tour:saved" && (step.requires || []).indexOf("A3") >= 0);
        if (mineOnly && !isMine(detail)) return;
        if (typeof r.match === "function" && !safe(function () { return r.match(detail, location, document); }, false)) return;
        if (!grew(r, detail)) return;
        if (detail.limited) {
          state.limited[step.id] = detail.limit === "rate" ? "rate" : "daily";
          save();
          renderAll();
          coach.refresh();
          return;
        }
        complete(step.id, null, detail);
      });
    });
    if (!detail.limited) coach.onEvent(name, detail);
  }

  // A <details data-tour=anchor> opened (B3: a failed case).
  function openRuleHit(r, target) {
    if (!r || r.on !== "open" || !target || target.getAttribute("data-tour") !== r.anchor || !target.open) return false;
    if (r.attr && r.attr.name) {
      var v = target.getAttribute(r.attr.name);
      var ok = Array.isArray(r.attr.values) ? r.attr.values.indexOf(v) >= 0 : v === r.attr.value;
      if (!ok) return false;
    }
    return true;
  }
  function onToggle(e) {
    var target = e.target;
    if (!target || target.tagName !== "DETAILS" || !target.open) return;
    STEPS.forEach(function (step) {
      if (isDone(step.id)) return;
      if (rules(step).some(function (r) { return openRuleHit(r, target); })) complete(step.id, null);
    });
    var card = currentCard();
    if (card && openRuleHit(card.advance, target)) coach.advance(false);
  }

  // A tool call names a step's work: by tool ("fork_profile") or by
  // "tool:action" ("templates:use", from HOOKS' `actions`).
  function onAiDone(detail) {
    detail = detail || {};
    var tools = Array.isArray(detail.tools) ? detail.tools : [];
    var actions = Array.isArray(detail.actions) ? detail.actions : [];
    var target = detail.tourStep && BY_ID[detail.tourStep] ? detail.tourStep : null;
    if (detail.limited) {
      if (target) state.limited[target] = detail.limit === "rate" ? "rate" : "daily";
      if (target || (state.ai && !detail.tourStep)) state.ai = null;
      save();
      renderAll();
      coach.refresh();
      return;
    }
    if (target) {
      // A turn the tour asked for: only that step can tick.
      var step = BY_ID[target];
      if (!isDone(target)) {
        var ok = rules(step).some(function (r) { return r.on === "ai" && LOGIC.aiSpecHit(r, tools, actions); }) ||
                 LOGIC.aiSpecHit(step.ai, tools, actions);
        if (ok) complete(target, "ai", detail);
      }
      if (!isDone(target)) state.ai = { step: target, missed: true };
      else if (state.ai && state.ai.step === target) state.ai = null;
      save();
      renderAll();
      coach.refresh();
      return;
    }
    // The visitor's own message: only a step's own `{on:"ai"}` rule counts,
    // and with HOOKS' actions present, by action when the rule names any.
    STEPS.forEach(function (s2) {
      if (isDone(s2.id)) return;
      var hit = rules(s2).some(function (r) { return r.on === "ai" && LOGIC.aiSpecHit(r, tools, actions); });
      if (hit) complete(s2.id, "ai", detail);
    });
  }

  function askAi(step) {
    if (!aiAvailable(step) || aiBusy()) return;
    var sent = safe(function () { return window.demoAssistant.ask(subst(step.ai.prompt), { tourStep: step.id, title: step.title }); }, false);
    if (sent === false) return;  // a turn is still running: the prompt waits in the input
    delete state.limited[step.id];
    state.ai = { step: step.id, missed: false, at: Date.now() };
    if (state.status !== "done") state.status = "active";
    save();
    renderAll();
    coach.refresh();
  }

  // ----------------------------------------------------------- run polling
  var pollTimer = null, polling = false;
  function openRuns() {
    var now = Date.now();
    return Object.keys(state.runs).filter(function (id) {
      var r = state.runs[id];
      return r && !r.finished && now - (r.at || 0) < RUN_STALE_MS;
    });
  }
  function pollRuns() {
    pollTimer = null;
    var ids = openRuns();
    if (!ids.length || polling) return;
    if (document.hidden) { pollTimer = setTimeout(pollRuns, 5000); return; }
    polling = true;
    Promise.all(ids.map(function (id) {
      return fetch("/admin/eval-runs/" + encodeURIComponent(id) + "/status.json",
                   { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : (r.status === 404 ? { status: "gone" } : null); })
        .then(function (data) {
          if (!data) return;
          if (data.status === "gone") { state.runs[id].finished = "gone"; save(); return; }
          if (TERMINAL[String(data.status || "").toLowerCase()]) runFinished(id, data.status);
        })
        .catch(function () { /* offline: try again */ });
    })).then(function () {
      polling = false;
      if (openRuns().length && !pollTimer) pollTimer = setTimeout(pollRuns, 5000);
      renderAll();
    });
  }
  function schedulePoll(ms) {
    if (pollTimer || polling || !openRuns().length) return;
    pollTimer = setTimeout(pollRuns, ms || 5000);
  }
  function runStarted(detail) {
    var id = detail && detail.run_id;
    if (!id) return;
    if (!state.runs[id]) state.runs[id] = { href: detail.href || ("/admin/evals/runs/" + encodeURIComponent(id)), at: Date.now(), finished: null };
    if (state.made.runs.indexOf(id) < 0) state.made.runs.push(id);
    save();
    renderAll();
    coach.refresh();
    schedulePoll(5000);
  }
  function runFinished(id, status) {
    var r = state.runs[id];
    if (!r || r.finished) return;
    r.finished = status || "done";
    save();
    safe(function () {
      window.dispatchEvent(new CustomEvent("tour:run-finished", { detail: { run_id: id, status: status, source: "tour" } }));
    });
  }
  function runningRun() {
    var ids = openRuns();
    return ids.length ? state.runs[ids[ids.length - 1]] : null;
  }

  // ----------------------------------------------------------------- icons
  var ICON = {
    sparkle: '<path d="M8 2.2l1.2 3.1 3.1 1.2-3.1 1.2L8 10.8 6.8 7.7 3.7 6.5l3.1-1.2z"/><path d="M12.6 10.4l.5 1.3 1.3.5-1.3.5-.5 1.3-.5-1.3-1.3-.5 1.3-.5z"/>',
    check: '<polyline points="3.5,8.5 6.5,11.5 12.5,4.5"/>',
    copy: '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3.5 10.5h-.2A1.3 1.3 0 012 9.2V3.3A1.3 1.3 0 013.3 2h5.9a1.3 1.3 0 011.3 1.3v.2"/>',
    rocket: '<path d="M9 2.8c2-1.3 4.5-1.3 4.5-.3s0 2.5-1.3 4.5S9 10.5 7.5 11L5 8.5C5.5 7 7 4 9 2.8z"/><path d="M5 8.5l-2.3.8L4.5 11M7.5 11l.8 2.3L10 11.5"/>',
    terminal: '<rect x="1.8" y="2.5" width="12.4" height="11" rx="1.6"/><polyline points="4.5,6.5 6.5,8.3 4.5,10"/><line x1="8" y1="10.2" x2="11" y2="10.2"/>',
    code: '<polyline points="5.5,4.5 2,8 5.5,11.5"/><polyline points="10.5,4.5 14,8 10.5,11.5"/><line x1="9.2" y1="3" x2="6.8" y2="13"/>',
    github: '<path d="M6 13.5c-3 .9-3-1.5-4.2-1.8M10 15v-2.3a2 2 0 00-.6-1.6c2-.2 4.1-1 4.1-4.5a3.5 3.5 0 00-1-2.4 3.2 3.2 0 00-.1-2.4s-.8-.2-2.5 1a8.6 8.6 0 00-4.6 0C3.6 1.6 2.8 1.8 2.8 1.8a3.2 3.2 0 00-.1 2.4 3.5 3.5 0 00-1 2.4c0 3.5 2.1 4.3 4.1 4.5A2 2 0 005.2 12.7V15"/>',
    chevron: '<polyline points="4.5,6 8,9.5 11.5,6"/>',
    flag: '<path d="M3.5 14V2.5"/><path d="M3.5 3h8l-1.6 2.6L11.5 8.2h-8"/>'
  };
  function icon(name, cls) {
    var span = document.createElement("span");
    span.className = "dt-icon" + (cls ? " " + cls : "");
    span.setAttribute("aria-hidden", "true");
    span.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" ' +
      'stroke-linecap="round" stroke-linejoin="round">' + (ICON[name] || "") + "</svg>";
    return span;
  }
  // Plain text with `backticks` rendered as code; nothing else is parsed.
  function rich(text) {
    var frag = document.createDocumentFragment();
    String(subst(text || "")).split("`").forEach(function (part, i) {
      if (!part) return;
      frag.appendChild(i % 2 ? el("code", { class: "dt-code-inline", text: part }) : document.createTextNode(part));
    });
    return frag;
  }
  function richEl(tag, cls, text) {
    var n = el(tag, { class: cls });
    n.appendChild(rich(text));
    return n;
  }
  function stepMinutes(s) { return typeof s.minutes === "number" ? s.minutes : null; }
  function trackMinutes(tr) {
    if (typeof tr.minutes === "number") return tr.minutes;
    var sum = 0, any = false;
    trackSteps(tr.id).forEach(function (s) { if (typeof s.minutes === "number") { sum += s.minutes; any = true; } });
    return any ? sum : null;
  }
  function trackName(tr) { return (tr && (tr.goal || tr.title)) || ""; }

  // ------------------------------------------------------------ code block
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }
  function legacyCopy(text) {
    var ta = el("textarea", { class: "dt-sr", "aria-hidden": "true" });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* nothing more to try */ }
    ta.remove();
  }
  function onCopied(stepId) {
    var step = BY_ID[stepId];
    if (step && rules(step).some(function (r) { return r.on === "copy"; })) complete(stepId, null);
  }
  // big: the Code step card's labelled button. iconOnly: a small icon button
  // over the top-right corner of a code block (coach cards, Finish).
  function copyButton(getText, label, stepId, big, iconOnly) {
    var name = t("codeCard.copy", "Copy") + (label ? " " + label : "");
    var b = el("button", { type: "button", class: "dt-copy" + (big ? " dt-copy-big" : "") + (iconOnly ? " dt-copy-icon" : ""),
                           "aria-label": name, title: iconOnly ? t("codeCard.copy", "Copy") : null });
    var text = el("span", { text: t("codeCard.copy", "Copy"), class: iconOnly ? "dt-sr" : null });
    var ic = icon("copy");
    b.appendChild(ic);
    b.appendChild(text);
    b.addEventListener("click", function () {
      copyText(getText()).then(function () {
        text.textContent = t("codeCard.copied", "Copied");
        if (iconOnly) { b.classList.add("dt-copied"); b.title = t("codeCard.copied", "Copied"); ic.replaceWith(ic = icon("check")); }
        announce(t("codeCard.copied", "Copied"));
        setTimeout(function () {
          text.textContent = t("codeCard.copy", "Copy");
          if (iconOnly) { b.classList.remove("dt-copied"); b.title = t("codeCard.copy", "Copy"); ic.replaceWith(ic = icon("copy")); }
        }, 2000);
        if (stepId) onCopied(stepId);
      });
    });
    return b;
  }

  // tabs = [{id, label, code}]. opts: {label: text before the tabs,
  // copyInRow: one Copy button beside the tabs (Code step card)}.
  function codeTabs(tabs, stepId, defaultId, opts) {
    opts = opts || {};
    var wrap = el("div", { class: "dt-code" + (opts.copyInRow ? " dt-code-big" : "") });
    if (!tabs.length) return wrap;
    var row = el("div", { class: "dt-code-row" });
    if (opts.label) row.appendChild(el("span", { class: "dt-code-label", text: opts.label }));
    var list = el("div", { class: "dt-seg-tabs", role: "tablist", "aria-label": opts.label || t("coach.inCode", "Same step, in code") });
    row.appendChild(list);
    var panels = [], buttons = [], current = 0;
    var initial = tabs.findIndex(function (x) { return x.id === defaultId; });
    if (initial < 0) initial = 0;
    var rowCopy = null;
    function select(i, focus) {
      current = i;
      buttons.forEach(function (b, k) {
        var on = k === i;
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
        panels[k].hidden = !on;
      });
      if (rowCopy) rowCopy.hidden = tabs[i].id === "portal";
      if (focus) buttons[i].focus();
    }
    tabs.forEach(function (tab, i) {
      var tid = nextId("tab"), pid = nextId("panel");
      var b = el("button", { type: "button", role: "tab", id: tid, "aria-controls": pid, class: "dt-seg-tab" }, [tab.label]);
      b.addEventListener("click", function () { select(i, false); });
      b.addEventListener("keydown", function (e) {
        var n = null;
        if (e.key === "ArrowRight") n = (i + 1) % tabs.length;
        else if (e.key === "ArrowLeft") n = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") n = 0;
        else if (e.key === "End") n = tabs.length - 1;
        if (n !== null) { e.preventDefault(); select(n, true); }
      });
      buttons.push(b);
      list.appendChild(b);
      var text = subst(tab.code);
      var isPath = tab.id === "portal";
      var panel = el("div", { role: "tabpanel", id: pid, "aria-labelledby": tid, class: "dt-panel", tabindex: "0" }, [
        el("pre", { class: isPath ? "dt-pre dt-pre-path" : "dt-pre" }, [el("code", { text: text })])
      ]);
      if (!isPath && !opts.copyInRow) panel.appendChild(copyButton(function () { return text; }, tab.label, stepId, false, true));
      panels.push(panel);
    });
    if (opts.copyInRow) {
      row.appendChild(el("span", { class: "dt-spacer" }));
      rowCopy = copyButton(function () { return subst(tabs[current].code); }, "", stepId, true);
      row.appendChild(rowCopy);
    }
    wrap.appendChild(row);
    panels.forEach(function (p) { wrap.appendChild(p); });
    select(initial, false);
    return wrap;
  }
  function stepCodeTabs(step) {
    var code = step.code || {};
    var tabs = [];
    CODE_ORDER.forEach(function (k) { if (code[k]) tabs.push({ id: k, label: t("codeTabs." + k, CODE_LABEL[k]), code: code[k] }); });
    Object.keys(code).forEach(function (k) {
      if (CODE_ORDER.indexOf(k) < 0 && typeof code[k] === "string") tabs.push({ id: k, label: k, code: code[k] });
    });
    return tabs;
  }
  function defaultLang(tabs) {
    var want = state.code_lang || "mcp";
    if (tabs.some(function (x) { return x.id === want; })) return want;
    var nonPortal = tabs.filter(function (x) { return x.id !== "portal"; })[0];
    return nonPortal ? nonPortal.id : (tabs.length ? tabs[0].id : null);
  }

  // ---------------------------------------------------------------- modal
  // A real modal: focus trap, Esc, focus return to the trigger.
  function Modal(cls) {
    this.cls = cls;
    this.node = null;
    this.trigger = null;
    this.onClose = null;
  }
  Modal.prototype.open = function (build, trigger, onClose) {
    this.close(true);
    this.trigger = trigger || document.activeElement;
    this.onClose = onClose || null;
    var self = this;
    var dialog = el("div", { class: "dt-modal " + this.cls, role: "dialog", "aria-modal": "true", tabindex: "-1" });
    var titleId = nextId("title");
    dialog.setAttribute("aria-labelledby", titleId);
    build(dialog, titleId);
    var overlay = el("div", { class: "dt-overlay" }, [dialog]);
    overlay.addEventListener("mousedown", function (e) { if (e.target === overlay) self.dismiss(); });
    overlay.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); self.dismiss(); return; }
      if (e.key !== "Tab") return;
      var f = focusables(dialog);
      if (!f.length) { e.preventDefault(); dialog.focus(); return; }
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    document.body.appendChild(overlay);
    document.documentElement.classList.add("dt-modal-open");
    this.node = overlay;
    var auto = dialog.querySelector("[data-autofocus]") || focusables(dialog)[0] || dialog;
    setTimeout(function () { try { auto.focus({ preventScroll: true }); } catch (e) { auto.focus(); } }, 20);
  };
  Modal.prototype.dismiss = function () {
    var cb = this.onClose;
    this.close(false);
    if (cb) cb();
  };
  Modal.prototype.close = function (silent) {
    if (!this.node) return;
    this.node.remove();
    this.node = null;
    document.documentElement.classList.remove("dt-modal-open");
    var trig = this.trigger;
    this.trigger = null;
    if (!silent && trig && trig.isConnected && typeof trig.focus === "function") trig.focus();
  };
  Modal.prototype.isOpen = function () { return !!this.node; };
  function focusables(root) {
    return Array.prototype.slice.call(root.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])'))
      .filter(function (n) { return n.offsetParent !== null || n === document.activeElement; });
  }
  function closeX(label, onclick) {
    return el("button", { type: "button", class: "dt-x", "aria-label": label, onclick: onclick }, ["×"]);
  }
  function aiButton(step, cls, label) {
    var b = btn("", "dt-btn " + (cls || ""), function () { askAi(step); });
    if (aiBusy()) { b.disabled = true; b.title = t("coach.working", "The Builder assistant is working on it."); }
    b.appendChild(icon("sparkle"));
    b.appendChild(document.createTextNode(label || t("checklist.letAi", "Let AI do it")));
    return b;
  }

  // -------------------------------------------------------------- welcome
  var welcome = new Modal("dt-welcome");
  function openWelcome(trigger) {
    var goalParam = safe(function () { return new URLSearchParams(location.search).get("goal"); }, null);
    var chosenGoal = state.goal || (goalParam && TRACK_BY_ID[goalParam] ? goalParam : (TRACKS[0] && TRACKS[0].id));
    var chosenLang = state.code_lang || "mcp";
    var exploring = false;
    welcome.open(function (d, titleId) {
      d.appendChild(el("div", { class: "dt-grabber", "aria-hidden": "true" }));
      d.appendChild(el("div", { class: "dt-modal-head" }, [
        el("div", {}, [
          el("p", { class: "dt-eyebrow", text: t("welcome.eyebrow", "Public demo · Northwind Internet") }),
          el("h2", { id: titleId, class: "dt-h dt-h-xl", text: t("welcome.title", "What do you want to see first?") }),
          richEl("p", "dt-lead", t("welcome.body", "A guided tour of the engine on four sample agents. Every step shows the click in the portal and the call behind it."))
        ])
      ]));
      var body = el("div", { class: "dt-modal-body" });
      var fs = el("fieldset", { class: "dt-goals" }, [el("legend", { class: "dt-sr", text: t("welcome.goalLabel", "What do you want to see first?") })]);
      var startBtn;
      TRACKS.forEach(function (tr) {
        var id = nextId("goal");
        var input = el("input", { type: "radio", name: "dt-goal", value: tr.id, id: id, class: "dt-radio" });
        if (tr.id === chosenGoal) input.checked = true;
        input.addEventListener("change", function () { chosenGoal = tr.id; syncStart(); });
        var n = trackSteps(tr.id).length;
        var mins = trackMinutes(tr);
        var meta = tr.meta || (t("welcome.steps", "{n} steps", { n: n }) + (mins ? " · " + t("checklist.minutes", "{n} min", { n: mins }) : ""));
        fs.appendChild(el("label", { class: "dt-goal", for: id }, [
          el("span", { class: "dt-goal-text" }, [
            el("strong", { text: trackName(tr) }),
            el("span", { class: "dt-goal-blurb", text: tr.blurb || "" }),
            el("span", { class: "dt-goal-meta", text: meta })
          ]),
          input
        ]));
      });
      body.appendChild(fs);
      var langs = [["mcp", t("welcome.langs.mcp", "MCP tool")], ["curl", t("welcome.langs.curl", "curl")], ["python", t("welcome.langs.python", "Python")]];
      var langLabelId = nextId("lang");
      var seg = el("div", { class: "dt-seg-tabs dt-seg-radio", role: "radiogroup", "aria-labelledby": langLabelId });
      langs.forEach(function (l) {
        var input = el("input", { type: "radio", name: "dt-lang", value: l[0] });
        if (l[0] === chosenLang) input.checked = true;
        input.addEventListener("change", function () { chosenLang = l[0]; });
        seg.appendChild(el("label", { class: "dt-seg-tab" }, [input, l[1]]));
      });
      var aiId = nextId("ai");
      var ai = el("input", { type: "checkbox", id: aiId });
      ai.checked = state.offer_ai !== false;
      body.appendChild(el("div", { class: "dt-options" }, [
        el("div", { class: "dt-row-field" }, [el("span", { class: "dt-label", id: langLabelId, text: t("welcome.langLabel", "Show calls as") }), seg]),
        el("label", { class: "dt-check", for: aiId }, [ai, el("span", { text: t("welcome.aiLabel", "Offer “Let AI do it” on each step") })])
      ]));
      d.appendChild(body);
      startBtn = btn("", "primary dt-btn dt-btn-l dt-start", function () {
        state.goal = chosenGoal;
        state.code_lang = chosenLang;
        state.offer_ai = ai.checked;
        state.status = "active";
        state.ui.open_tracks = {};
        state.ui.open_tracks[chosenGoal] = true;
        state.ui.expanded = null;
        state.ui.drawer = true;
        state.flags.welcomed = true;
        save();
        welcome.close(true);
        drawer.open(true);
      }, { "data-autofocus": "1" });
      function syncStart() {
        var tr = TRACK_BY_ID[chosenGoal];
        startBtn.textContent = t("welcome.start", "Start: {goal}", { goal: trackName(tr), track: trackName(tr) });
        startBtn.appendChild(el("span", { class: "dt-chev-r", "aria-hidden": "true", text: "›" }));
      }
      syncStart();
      var explore = btn(t("welcome.explore", "Explore on my own"), "ghost dt-btn dt-btn-l dt-explore", function () {
        state.code_lang = chosenLang;
        state.offer_ai = ai.checked;
        exploring = true;
        welcome.dismiss();
      });
      d.appendChild(el("div", { class: "dt-modal-foot" }, [
        el("p", { class: "dt-foot-note", text: t("welcome.footer", t("publicNote", "Progress is kept in this browser and clears with the nightly reset.")) }),
        el("div", { class: "dt-actions" }, [explore, startBtn])
      ]));
    }, trigger, function () {
      // Explore on my own or Esc. On the first visit the checklist opens
      // anyway (once): it is how the demo is found again. Later, only the
      // sidebar entry (the phone pill) stays.
      if (state.status === "new") state.status = "dismissed";
      var firstTime = !state.flags.welcomed;
      state.flags.welcomed = true;
      save();
      renderAll();
      // On a phone both are full-screen sheets: the checklist comes first
      // (one tap closes it), the assistant is a tap on its launcher.
      if (exploring && state.offer_ai !== false && !(firstTime && isPhone()) &&
          window.demoAssistant && typeof window.demoAssistant.open === "function") {
        safe(function () { window.demoAssistant.open(); });
      }
      if (firstTime) {
        if (!state.goal) state.goal = chosenGoal;  // the track the Welcome had picked
        drawer.open(true);
        // Shown on this page only: the next page opens without it.
        state.ui.drawer = false;
        save();
        return;
      }
      var entry = isPhone() ? document.getElementById("dt-pill") : document.getElementById("dt-entry");
      if (entry && visible(entry)) entry.focus();
    });
  }

  // --------------------------------------------------------------- finish
  var finish = {
    modal: new Modal("dt-finish"),
    open: function (trigger) {
      var F = COPY.finish || {};
      var ways = Array.isArray(F.ways) && F.ways.length ? F.ways : [
        { title: "Run it on your machine", code: "git clone {REPO}.git\ncd ai-engine && docker compose up", body: "Only Docker needed." },
        { title: "Deploy it", body: "Render, Railway or Coolify run the same image." },
        { title: "Build from Claude Code", body: "Skills that build, test and improve agents for you." }
      ];
      var deployLinks = Array.isArray(F.deployLinks) ? F.deployLinks : [];
      var icons = ["terminal", "rocket", "code"];
      var n = doneCount();
      var viaAi = STEPS.filter(function (s) { return state.steps[s.id] && state.steps[s.id].via === "ai"; }).length;
      var all = n === TOTAL;
      this.modal.open(function (d, titleId) {
        var head = el("div", { class: "dt-modal-head dt-finish-head" }, [
          el("span", { class: "dt-finish-tile", "aria-hidden": "true" }, [icon("check")]),
          el("div", {}, [
            el("h2", { id: titleId, class: "dt-h dt-h-xl", text: all ? t("finish.title", "You've seen the whole engine") : t("finish.titlePartial", "Run your own copy") }),
            richEl("p", "dt-lead", t("finish.body", "Everything here resets tonight. Your own copy keeps it."))
          ]),
          closeX(t("finish.closeX", "Close"), function () { finish.modal.dismiss(); })
        ]);
        d.appendChild(head);
        var stat = function (big, small) { return el("div", { class: "dt-stat" }, [el("strong", { text: String(big) }), el("span", { text: small })]); };
        var stats = el("div", { class: "dt-stats" }, [
          all ? stat(t("finish.stats.done", "Done"), t("finish.stats.doneSub", "all four tracks")) : null,
          stat(n + " / " + TOTAL, t("finish.stats.steps", "steps")),
          stat(viaAi, t("finish.stats.viaAi", "done via AI")),
          stat(state.made.agents.length, t("finish.stats.agents", "agents you built")),
          stat(state.made.runs.length, t("finish.stats.runs", "test runs"))
        ]);
        var grid = el("div", { class: "dt-ways" });
        ways.slice(0, 3).forEach(function (w, i) {
          var card = el("section", { class: "dt-way" }, [
            el("h3", { class: "dt-way-title" }, [icon(w.icon || icons[i] || "code"), w.title || ""])
          ]);
          if (w.code) {
            card.appendChild(el("div", { class: "dt-way-code" }, [
              el("pre", { class: "dt-pre" }, [el("code", { text: subst(w.code) })]),
              copyButton(function () { return subst(w.code); }, w.title, null, false, true)
            ]));
          }
          var links = Array.isArray(w.links) ? w.links : (!w.code && deployLinks.length ? deployLinks : []);
          if (links.length) {
            card.appendChild(el("div", { class: "dt-way-links" }, links.map(function (l) {
              return el("a", { class: "btn dt-btn", href: subst(l.href), target: "_blank", rel: "noopener", text: l.label });
            })));
          }
          if (w.body) card.appendChild(richEl("p", "dt-way-body", w.body));
          if (w.href && !links.length) card.appendChild(el("a", { class: "dt-way-more", href: subst(w.href), target: "_blank", rel: "noopener", text: w.label || w.title }));
          grid.appendChild(card);
        });
        d.appendChild(el("div", { class: "dt-modal-body" }, [stats, grid]));
        var starHref = subst(t("finish.starHref", config.repo_url || ""));
        var docsHref = subst(t("finish.docsHref", config.repo_url ? String(config.repo_url).replace(/\/$/, "") + "#readme" : "/admin/help"));
        var star = starHref ? el("a", { class: "btn dt-btn dt-btn-l dt-btn-dark", href: starHref, target: "_blank", rel: "noopener" }, [icon("github"), t("finish.star", "Star on GitHub")]) : null;
        var cmpLabel = typeof F.compare === "string" ? F.compare : (F.compare && F.compare.label);
        var cmpHref = F.compareHref || (F.compare && F.compare.href);
        var compare = cmpLabel && cmpHref ? el("a", { class: "dt-link", href: subst(cmpHref), target: "_blank", rel: "noopener", text: cmpLabel }) : null;
        d.appendChild(el("div", { class: "dt-modal-foot dt-finish-foot" }, [
          star,
          el("a", { class: "btn dt-btn dt-btn-l", href: docsHref, target: "_blank", rel: "noopener", text: t("finish.docs", "Read the docs") }),
          compare,
          el("span", { class: "dt-spacer" }),
          btn(t("finish.close", "Keep exploring"), "ghost dt-btn dt-btn-l", function () { finish.modal.dismiss(); }, { "data-autofocus": "1" })
        ]));
      }, trigger);
    }
  };

  // --------------------------------------------------------------- toasts
  var toast = {
    node: null, timer: null,
    show: function (step, via, next, open) {
      this.hide();
      var self = this;
      var list = trackSteps(step.track);
      var lead = via === "ai" ? t("toast.done", "Step done via AI:") : t("toast.doneHand", "Step done:");
      var detailText = t("toast.detail", "{title} · {n} of {total} in {track}",
                         { title: step.title, n: list.indexOf(step) + 1, total: list.length, track: stepTrack(step).title });
      var box = el("div", { class: "dt-toast", role: "group", "aria-label": lead }, [
        el("span", { class: "dt-toast-check", "aria-hidden": "true" }, [icon("check")]),
        el("span", { class: "dt-toast-text" }, [el("strong", { text: lead }), " " + detailText])
      ]);
      if (open) {
        // The way to what the step just made (A3 via AI: the new agent).
        box.appendChild(el("a", { class: "dt-toast-next", href: open.href, text: open.label }));
      } else if (next) {
        box.appendChild(btn(t("toast.next", "Next step"), "dt-toast-next", function () {
          self.hide();
          startStep(next.id);
        }));
      }
      box.appendChild(el("button", { type: "button", class: "dt-x", "aria-label": t("toast.dismiss", "Dismiss"),
                                     onclick: function () { self.hide(); } }, ["×"]));
      toastRegion.appendChild(box);
      this.node = box;
      announce(lead + " " + step.title);
      var arm = function () { clearTimeout(self.timer); self.timer = setTimeout(function () { self.hide(); }, 10000); };
      box.addEventListener("mouseenter", function () { clearTimeout(self.timer); });
      box.addEventListener("focusin", function () { clearTimeout(self.timer); });
      box.addEventListener("mouseleave", arm);
      arm();
    },
    hide: function () {
      clearTimeout(this.timer);
      if (this.node) { this.node.remove(); this.node = null; }
    }
  };
  var toastRegion = el("div", { class: "dt-toasts" });

  // --------------------------------------------------------------- drawer
  var drawer = {
    node: null,
    isOpen: function () { return !!(this.node && !this.node.hidden); },
    open: function (focus) {
      if (state.status === "new" || state.status === "dismissed") { state.status = "active"; }
      state.ui.drawer = true;
      save();
      this.render();
      this.node.hidden = false;
      document.documentElement.classList.add("dt-drawer-open");
      requestAnimationFrame(function () { drawer.node.classList.add("dt-in"); });
      syncEntry();
      if (focus) {
        var target = this.node.querySelector(".dt-step.dt-expanded .dt-step-actions button") || this.node.querySelector(".dt-drawer-title");
        if (target) setTimeout(function () { try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); } }, 30);
      }
      coach.position();
    },
    close: function (returnFocus) {
      state.ui.drawer = false;
      save();
      if (this.node) {
        this.node.classList.remove("dt-in");
        this.node.hidden = true;
      }
      document.documentElement.classList.remove("dt-drawer-open");
      syncEntry();
      if (returnFocus) {
        var e = isPhone() ? document.getElementById("dt-pill") : document.getElementById("dt-entry");
        if (e && visible(e)) e.focus();
      }
      coach.position();
    },
    toggle: function () { if (this.isOpen()) this.close(true); else this.open(true); },
    ensure: function () {
      if (this.node) return;
      this.node = el("aside", { class: "dt-drawer", id: "dt-drawer", "aria-label": t("checklist.title", "Getting started"), hidden: true });
      this.node.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); drawer.close(true); }
      });
      document.body.appendChild(this.node);
    },
    render: function () {
      this.ensure();
      var root = this.node;
      var keepScroll = root.querySelector(".dt-drawer-body");
      var scroll = keepScroll ? keepScroll.scrollTop : 0;
      var hadFocus = root.contains(document.activeElement) ? document.activeElement.getAttribute("data-key") : null;
      root.textContent = "";
      var n = doneCount();
      var goal = TRACK_BY_ID[state.goal];
      var progress = t("checklist.progress", "{n} of {total}", { n: n, total: TOTAL });
      root.appendChild(el("div", { class: "dt-grabber", "aria-hidden": "true" }));
      var head = el("div", { class: "dt-drawer-head" }, [
        el("div", { class: "dt-drawer-top" }, [
          el("h2", { class: "dt-h dt-drawer-title", tabindex: "-1", text: t("checklist.title", "Getting started") }),
          closeX(t("checklist.close", "Close"), function () { drawer.close(true); })
        ]),
        el("div", { class: "dt-progress-row" }, [
          el("div", { class: "dt-progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(TOTAL),
                      "aria-valuenow": String(n), "aria-label": progress }, [
            el("span", { class: "dt-progress-fill", style: "width:" + Math.round(100 * n / TOTAL) + "%" })
          ]),
          el("span", { class: "dt-count", text: progress })
        ]),
        el("div", { class: "dt-drawer-meta" }, [
          el("span", { class: "dt-muted", text: t("checklist.yourTrack", t("checklist.track", "Your track")) }),
          goal ? el("span", { class: "dt-pill-soft", text: trackName(goal) }) : null,
          btn(t("checklist.change", "Change"), "dt-link", function () { drawer.pickTrack(this); },
              { "aria-expanded": "false", "data-key": "change" }),
          el("span", { class: "dt-spacer" }),
          btn(state.ui.hide_done ? t("checklist.showDone", "Show done") : t("checklist.hideDone", "Hide done"),
              "dt-link", function () { state.ui.hide_done = !state.ui.hide_done; save(); drawer.render(); },
              { "aria-pressed": state.ui.hide_done ? "true" : "false", "data-key": "hide" })
        ])
      ]);
      root.appendChild(head);
      var bodyEl = el("div", { class: "dt-drawer-body" });
      var upNext = nextStep(null);
      trackOrder().forEach(function (tid, ti) {
        var tr = TRACK_BY_ID[tid] || { id: tid, title: tid };
        var steps = trackSteps(tid);
        if (!steps.length) return;
        var dn = steps.filter(function (s) { return isDone(s.id); }).length;
        var isOpen = state.ui.open_tracks[tid];
        if (isOpen === undefined) isOpen = (upNext && upNext.track === tid) || (!upNext && ti === 0);
        var listId = nextId("sec");
        var section = el("section", { class: "dt-section" });
        var hbtn = el("button", { type: "button", class: "dt-section-head", "aria-expanded": isOpen ? "true" : "false",
                                  "aria-controls": listId, "data-key": "sec-" + tid }, [
          el("span", { class: "dt-section-title", text: trackName(tr) }),
          el("span", { class: "dt-section-count", text: dn + "/" + steps.length }),
          icon("chevron", "dt-chev")
        ]);
        hbtn.addEventListener("click", function () {
          state.ui.open_tracks[tid] = !isOpen;
          save();
          drawer.render();
        });
        section.appendChild(el("h3", { class: "dt-section-h" }, [hbtn]));
        var ul = el("ul", { class: "dt-steps", id: listId, hidden: !isOpen });
        steps.forEach(function (s) {
          if (state.ui.hide_done && isDone(s.id)) return;
          ul.appendChild(drawer.row(s, upNext));
        });
        section.appendChild(ul);
        bodyEl.appendChild(section);
      });
      root.appendChild(bodyEl);
      bodyEl.scrollTop = scroll;
      var fin = el("button", { type: "button", class: "dt-finish-btn", "data-key": "finish" }, [icon("rocket")]);
      fin.appendChild(el("span", {}, [rich(t("checklist.finishLine", "Finish line: run your own copy with")),
        COPY.checklist && COPY.checklist.finishCode ? " " : null,
        COPY.checklist && COPY.checklist.finishCode ? el("code", { class: "dt-code-inline", text: COPY.checklist.finishCode }) : null]));
      fin.addEventListener("click", function () { finish.open(fin); });
      root.appendChild(el("div", { class: "dt-drawer-foot" }, [fin]));
      if (hadFocus) {
        var again = root.querySelector('[data-key="' + hadFocus + '"]');
        if (again) again.focus();
      }
    },
    row: function (s, upNext) {
      var done = state.steps[s.id];
      var expanded = !done && (state.ui.expanded ? state.ui.expanded === s.id : !!(upNext && upNext.id === s.id));
      var li = el("li", { class: "dt-step" + (done ? " dt-done" : "") + (expanded ? " dt-expanded" : "") + (state.active && state.active.step === s.id ? " dt-current" : "") });
      var mark = el("span", { class: "dt-mark", "aria-hidden": "true" }, done ? [icon("check")] : []);
      var detailsId = nextId("row");
      var mins = stepMinutes(s);
      var title = el("button", { type: "button", class: "dt-step-title", "data-key": "step-" + s.id,
                                 "aria-expanded": done ? null : (expanded ? "true" : "false"),
                                 "aria-controls": done ? null : detailsId }, [
        mark,
        el("span", { class: "dt-step-name" }, [s.title, done ? el("span", { class: "dt-sr", text: ", " + viaLabel(done.via) }) : null]),
        done ? el("span", { class: "dt-via dt-via-" + (done.via === "ai" ? "ai" : "plain"), "aria-hidden": "true", text: viaLabel(done.via) })
             : (expanded && mins ? el("span", { class: "dt-mins", text: t("checklist.minutes", "{n} min", { n: mins }) }) : null)
      ]);
      title.addEventListener("click", function () {
        if (done) { startStep(s.id); return; }
        state.ui.expanded = expanded ? "__none__" : s.id;
        save();
        drawer.render();
      });
      li.appendChild(title);
      if (!done) {
        var box = el("div", { class: "dt-step-details", id: detailsId, hidden: !expanded });
        if (s.summary) box.appendChild(richEl("p", "dt-step-summary", s.summary));
        if (state.limited[s.id]) box.appendChild(el("p", { class: "dt-note dt-note-warn", text: limitText(state.limited[s.id]) }));
        if (state.ai && state.ai.step === s.id) box.appendChild(el("p", { class: "dt-note", text: state.ai.missed ? t("coach.aiMissed", "The assistant did not finish this step. You can do it by hand.") : t("coach.working", "The assistant is working on it.") }));
        var run = s.id === "B2" ? runningRun() : null;
        if (run) box.appendChild(runStrip(run));
        var need = missingFor(s);
        if (need) box.appendChild(el("p", { class: "dt-note", text: needsText(need) }));
        box.appendChild(el("div", { class: "dt-step-actions" }, need ? [
          btn(t("coach.needsGo", "Create an agent first"), "primary dt-btn", function () { startStep(need.id); }, { "data-key": "need-" + s.id })
        ] : [
          btn(t("checklist.showMe", "Show me"), "primary dt-btn", function () { startStep(s.id); }, { "data-key": "show-" + s.id }),
          aiAvailable(s) ? aiButton(s, "") : null
        ]));
        li.appendChild(box);
      }
      return li;
    },
    pickTrack: function (trigger) {
      var meta = this.node.querySelector(".dt-drawer-meta");
      var existing = this.node.querySelector(".dt-track-pick");
      if (existing) { existing.remove(); trigger.setAttribute("aria-expanded", "false"); return; }
      trigger.setAttribute("aria-expanded", "true");
      var pick = el("div", { class: "dt-track-pick", role: "group", "aria-label": t("checklist.change", "Change") });
      TRACKS.forEach(function (tr) {
        pick.appendChild(btn(trackName(tr), "dt-btn dt-btn-s" + (tr.id === state.goal ? " primary" : ""), function () {
          state.goal = tr.id;
          state.ui.open_tracks = {};
          state.ui.open_tracks[tr.id] = true;
          state.ui.expanded = null;
          save();
          drawer.render();
          var h = drawer.node.querySelector(".dt-section-head");
          if (h) h.focus();
        }, { "aria-pressed": tr.id === state.goal ? "true" : "false" }));
      });
      meta.parentNode.insertBefore(pick, meta.nextSibling);
      var first = pick.querySelector("button");
      if (first) first.focus();
    }
  };
  function runStrip(run) {
    return el("p", { class: "dt-yourturn dt-running" }, [
      el("span", { class: "dt-dot", "aria-hidden": "true" }),
      el("span", {}, [t("coach.running", "Running, about a minute"), " · ",
        el("a", { href: run.href, text: t("coach.openRun", "Open the run") })])
    ]);
  }

  // ---------------------------------------------------- sidebar entry/pill
  function ringSvg(frac, size) {
    size = size || 18;
    var stroke = size > 24 ? 4 : 2.5;
    var r = (size - stroke) / 2, c = 2 * Math.PI * r, mid = size / 2;
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "dt-ring");
    svg.setAttribute("width", String(size));
    svg.setAttribute("height", String(size));
    svg.setAttribute("viewBox", "0 0 " + size + " " + size);
    svg.setAttribute("aria-hidden", "true");
    [["dt-ring-bg", null], ["dt-ring-fg", frac]].forEach(function (p) {
      var circle = document.createElementNS(ns, "circle");
      circle.setAttribute("cx", String(mid)); circle.setAttribute("cy", String(mid)); circle.setAttribute("r", String(r));
      circle.setAttribute("class", p[0]);
      circle.setAttribute("stroke-width", String(stroke));
      if (p[1] !== null) {
        circle.setAttribute("stroke-dasharray", (c * p[1]).toFixed(2) + " " + c.toFixed(2));
        circle.setAttribute("transform", "rotate(-90 " + mid + " " + mid + ")");
      }
      svg.appendChild(circle);
    });
    return svg;
  }
  function syncEntry() {
    var n = doneCount();
    var label = t("entry.label", "Getting started");
    var count = n + "/" + TOTAL;
    var aria = label + ", " + t("checklist.progress", "{n} of {total}", { n: n, total: TOTAL });
    var entry = document.getElementById("dt-entry");
    if (entry) {
      var ring = entry.querySelector(".dt-ring-slot");
      if (ring) { ring.textContent = ""; ring.appendChild(ringSvg(n / TOTAL, 18)); }
      var lab = entry.querySelector(".dt-entry-label");
      if (lab) lab.textContent = label;
      var cnt = entry.querySelector(".dt-entry-count");
      if (cnt) cnt.textContent = count;
      entry.setAttribute("aria-expanded", drawer.isOpen() ? "true" : "false");
      entry.setAttribute("aria-label", aria);
      entry.title = label + " · " + count;
      entry.hidden = false;
    }
    var pill = document.getElementById("dt-pill");
    if (pill) {
      pill.textContent = "";
      pill.appendChild(ringSvg(n / TOTAL, 18));
      pill.appendChild(document.createTextNode(count));
      pill.setAttribute("aria-expanded", drawer.isOpen() ? "true" : "false");
      pill.setAttribute("aria-label", aria);
      pill.hidden = false;
    }
  }
  function onEntryClick(e) {
    e.preventDefault();
    if (state.status === "new") { openWelcome(e.currentTarget); return; }
    drawer.toggle();
  }

  // ---------------------------------------------------------- resume card
  function renderResume() {
    var mount = document.getElementById("demo-tour-resume");
    if (!mount) return;
    var n = doneCount();
    var show = state.status === "active" && n < TOTAL && !(state.hidden && state.hidden.overview_card);
    mount.textContent = "";
    mount.hidden = !show;
    if (!show) return;
    var nx = nextStep(null);
    var titleId = nextId("resume");
    var goal = TRACK_BY_ID[state.goal] || (nx && stepTrack(nx));
    var line = el("p", { class: "dt-resume-line" });
    if (nx) {
      line.appendChild(document.createTextNode(t("resume.next", "Next:") + " "));
      var tpl = t("resume.body", "{title} · {minutes} min. {short}");
      var mins = stepMinutes(nx);
      if (mins === null) tpl = tpl.replace(/\s*·\s*\{minutes\}[^.{]*\.?/, ".");
      var parts = tpl.split("{title}");
      if (parts[0]) line.appendChild(rich(fill(parts[0], { minutes: mins, short: nx.short || nx.summary || "" })));
      line.appendChild(el("strong", { text: nx.title }));
      if (parts[1]) line.appendChild(rich(fill(parts.slice(1).join(nx.title), { minutes: mins, short: nx.short || nx.summary || "" })));
    }
    mount.appendChild(el("section", { class: "dt-resume", "aria-labelledby": titleId }, [
      el("div", { class: "dt-resume-ring", "aria-hidden": "true" }, [ringSvg(n / TOTAL, 40)]),
      el("div", { class: "dt-resume-text" }, [
        el("div", { class: "dt-resume-title" }, [
          el("h2", { class: "dt-h", id: titleId, text: t("resume.title", "Pick up the tour") }),
          el("span", { class: "dt-pill-grey", text: t("resume.progress", "{n} of {total}", { n: n, total: TOTAL }) }),
          goal ? el("span", { class: "dt-pill-soft", text: trackName(goal) }) : null
        ]),
        line
      ]),
      el("div", { class: "dt-actions" }, [
        nx && aiAvailable(nx) ? aiButton(nx, "dt-btn-l", t("resume.letAi", "")) : null,
        nx ? btn(t("resume.continue", "Continue"), "primary dt-btn dt-btn-l", function () { startStep(nx.id); })
           : btn(t("resume.open", "Open checklist"), "dt-btn dt-btn-l", function () { drawer.open(true); }),
        el("button", { type: "button", class: "dt-x", "aria-label": t("resume.hide", "Hide this card"), onclick: function () {
          state.hidden.overview_card = true;
          save();
          renderResume();
        } }, ["×"])
      ])
    ]));
  }

  // ---------------------------------------------------------------- coach
  function currentCard() {
    if (!state.active) return null;
    var step = BY_ID[state.active.step];
    var cards = (step && Array.isArray(step.cards)) ? step.cards : [];
    return cards[state.active.card] || null;
  }

  var coach = {
    card: null, ring: null, anchor: null, waitTimer: null, observer: null, raf: 0, fallback: false, codeCard: false,
    clear: function () {
      clearTimeout(this.waitTimer);
      if (this.observer) { this.observer.disconnect(); this.observer = null; }
      if (this.card) { this.card.remove(); this.card = null; }
      if (this.ring) { this.ring.remove(); this.ring = null; }
      if (this.anchor) { this.anchor.classList.remove("dt-anchored"); }
      this.anchor = null;
      this.fallback = false;
      this.codeCard = false;
      this.needs = false;
      document.documentElement.classList.remove("dt-coach-sheet");
      document.documentElement.classList.remove("dt-hide-launcher");
    },
    // Show the active card on this page: wait for its anchor up to 4 s.
    show: function (focus) {
      this.clear();
      if (!state.active) return;
      var step = BY_ID[state.active.step];
      if (!step) { state.active = null; save(); return; }
      if (step.kind === "code") { this.renderCodeCard(step, focus); return; }
      var card = currentCard();
      // Cards no longer needed on this page (the agent is already open).
      while (card && typeof card.skip === "function" && safe(function () { return card.skip(location, document); }, false)) {
        state.active.card += 1;
        save();
        card = currentCard();
      }
      if (!card) { endWalkthrough(false); return; }
      // A page-advance card whose page is already shown moves on at once.
      if (card.advance && card.advance.on === "page" && typeof card.advance.match === "function" &&
          safe(function () { return card.advance.match(location, document); }, false)) {
        this.advance();
        return;
      }
      if (!card.anchor) { this.render(step, card, null, focus); return; }
      var found = cardAnchor(card);
      if (found) { this.render(step, card, found, focus); return; }
      var ahead = laterCardHere();
      if (ahead > 0) { state.active.card = ahead; save(); this.show(focus); return; }
      var self = this;
      var deadline = Date.now() + ANCHOR_WAIT_MS;
      this.observer = new MutationObserver(function () {
        var a = cardAnchor(card);
        if (a) { self.observer.disconnect(); self.observer = null; clearTimeout(self.waitTimer); self.render(step, card, a, focus); }
      });
      this.observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class", "style"] });
      var tick = function () {
        var a = cardAnchor(card);
        if (a) { if (self.observer) { self.observer.disconnect(); self.observer = null; } self.render(step, card, a, focus); return; }
        var later = laterCardHere();
        if (later > 0) {
          if (self.observer) { self.observer.disconnect(); self.observer = null; }
          state.active.card = later; save(); self.show(focus);
          return;
        }
        if (Date.now() >= deadline) {
          if (self.observer) { self.observer.disconnect(); self.observer = null; }
          startPath(step).then(function (path) {
            if (!state.active || currentCard() !== card) return;
            self.render(step, card, null, focus, (!path || samePlace(path)) ? "here" : "elsewhere");
          });
          return;
        }
        self.waitTimer = setTimeout(tick, 250);
      };
      this.waitTimer = setTimeout(tick, 250);
    },
    refresh: function () {
      if (this.needs) return;
      if (!state.active) { this.clear(); return; }
      if (!this.card) return;
      // Re-draw the same card for a new note (working, limit, running) without
      // moving focus out of it or announcing the step again.
      var old = this.card;
      var f = focusables(old);
      var at = f.indexOf(document.activeElement);
      var hadFocus = old.contains(document.activeElement);
      this.quiet = true;
      this.show(false);
      this.quiet = false;
      var now = this.card;
      if (hadFocus && now && now !== old) {
        var g = focusables(now);
        var target = (at >= 0 && g[Math.min(at, g.length - 1)]) || now;
        try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
      }
    },
    render: function (step, card, anchor, focus, missing) {
      this.clear();
      var self = this;
      var idx = state.active.card;
      var label = card.title || step.title;
      var sheet = isPhone();
      var list = trackSteps(step.track);
      var node = el("div", { class: "dt-coach" + (sheet ? " dt-sheet" : "") + (missing || !anchor ? " dt-coach-center" : ""),
                             role: "dialog", "aria-label": step.title, tabindex: "-1" });
      node.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endWalkthrough(true); }
      });
      if (sheet) node.appendChild(el("div", { class: "dt-grabber", "aria-hidden": "true" }));
      node.appendChild(el("div", { class: "dt-coach-head" }, [
        el("p", { class: "dt-eyebrow", text: stepLabel(step) }),
        closeX(t("coach.close", "Close"), function () { endWalkthrough(true); })
      ]));
      node.appendChild(el("h3", { class: "dt-coach-title", text: label }));
      if (sheet && anchor && !missing) {
        // Shown only while the sheet is folded (CSS): opens it again.
        node.appendChild(btn(t("coach.more", "Show the whole step"), "dt-link dt-mini-toggle", function () {
          node.setAttribute("data-user-expanded", "1");
          node.classList.remove("dt-sheet-mini", "dt-sheet-top");
          this.setAttribute("aria-expanded", "true");
        }, { "aria-expanded": "true" }));
      }
      if (missing === "here") node.appendChild(el("p", { class: "dt-body", text: t("coach.notHere", "This part of the page is not shown right now. You can skip this step and come back later.") }));
      else if (missing) node.appendChild(el("p", { class: "dt-body", text: t("coach.elsewhere", "This step starts on another page.") }));
      else if (card.body) node.appendChild(richEl("p", "dt-body", card.body));
      if (state.limited[step.id]) node.appendChild(el("p", { class: "dt-note dt-note-warn", text: limitText(state.limited[step.id]) }));
      if (state.ai && state.ai.step === step.id) node.appendChild(el("p", { class: "dt-note", text: state.ai.missed ? t("coach.aiMissed", "The assistant did not finish this step. You can do it by hand.") : t("coach.working", "The assistant is working on it.") }));
      var run = step.id === "B2" ? runningRun() : null;
      if (run) node.appendChild(runStrip(run));
      else if (card.yourTurn && !missing && !isDone(step.id)) {
        var turn = el("p", { class: "dt-yourturn" }, [el("span", { class: "dt-dot", "aria-hidden": "true" })]);
        var tt = el("span", {}, [label2(t("coach.yourTurn", "Your turn:")) + " "]);
        tt.appendChild(rich(card.yourTurn));
        turn.appendChild(tt);
        node.appendChild(turn);
      }
      var note = typeof card.note === "function" ? safe(function () { return card.note(location, document); }, null) : card.note;
      if (note) node.appendChild(richEl("p", "dt-small dt-muted dt-m0", note));
      var tabs = stepCodeTabs(step);
      if (tabs.length) {
        var inCode = t("coach.inCode", "Same step, in code");
        if (sheet) {
          node.appendChild(el("details", { class: "dt-incode" }, [el("summary", { text: inCode }),
            codeTabs(tabs, step.id, defaultLang(tabs), {})]));
        } else {
          node.appendChild(codeTabs(tabs, step.id, defaultLang(tabs), { label: inCode }));
        }
      }
      if (step.doneWhen && !sheet) {
        var dw = el("p", { class: "dt-donewhen" }, [icon("check"), el("span", {}, [label2(t("coach.doneWhen", "Done when:")) + " "])]);
        dw.lastChild.appendChild(rich(step.doneWhen));
        node.appendChild(dw);
      }
      var foot = el("div", { class: "dt-coach-foot" });
      if (!sheet && list.length > 1) {
        var dots = el("span", { class: "dt-dots", "aria-hidden": "true" });
        list.forEach(function (s2) { dots.appendChild(el("span", { class: "dt-dot-s" + (s2 === step ? " dt-on" : (isDone(s2.id) ? " dt-past" : "")) })); });
        foot.appendChild(dots);
        foot.appendChild(el("span", { class: "dt-spacer" }));
      }
      if (aiAvailable(step) && !isDone(step.id)) foot.appendChild(aiButton(step, sheet ? "dt-btn-l" : "ghost dt-btn-s dt-ai-link"));
      // Back goes to the previous card this page still shows (skipped "get
      // there" cards do not count), so it never bounces.
      var cardsAll = step.cards || [];
      var prev = -1;
      for (var pi = idx - 1; pi >= 0; pi--) {
        var pc2 = cardsAll[pi];
        if (typeof pc2.skip === "function" && safe(function () { return pc2.skip(location, document); }, false)) continue;
        if (pc2.anchor && !cardAnchor(pc2)) continue;
        prev = pi;
        break;
      }
      if (prev >= 0) foot.appendChild(btn(t("coach.back", "Back"), "ghost dt-btn " + (sheet ? "dt-btn-l" : "dt-btn-s"), function () { state.active.card = prev; save(); self.show(true); }));
      if (missing === "here") {
        foot.appendChild(btn(t("coach.skip", "Skip step"), "primary dt-btn " + (sheet ? "dt-btn-l" : "dt-btn-s"), function () { skipStep(step); }, { "data-autofocus": "1" }));
      } else if (missing) {
        foot.appendChild(btn(t("coach.takeMeThere", "Take me there"), "primary dt-btn " + (sheet ? "dt-btn-l" : "dt-btn-s"), function () {
          state.active.card = 0;
          save();
          startPath(step).then(function (path) {
            if (path && !samePlace(path)) goTo(path); else self.show(true);
          });
        }, { "data-autofocus": "1" }));
      } else if (!(card.advance && card.advance.on === "next") && !isDone(step.id)) {
        foot.appendChild(btn(t("coach.skip", "Skip step"), "primary dt-btn " + (sheet ? "dt-btn-l" : "dt-btn-s"), function () { skipStep(step); }));
      }
      if (!missing && card.advance && card.advance.on === "next") {
        foot.appendChild(btn(t("coach.next", "Next"), "primary dt-btn " + (sheet ? "dt-btn-l" : "dt-btn-s"), function () { self.advance(true); }, { "data-autofocus": "1" }));
      }
      if (sheet && foot.children.length === 1) foot.classList.add("dt-foot-one");
      if (foot.children.length) node.appendChild(foot);
      if (!sheet && anchor && !missing) node.appendChild(el("span", { class: "dt-arrow", "aria-hidden": "true" }));
      hostFor(anchor).appendChild(node);
      this.card = node;
      this.fallback = !!missing;
      if (anchor && !missing) {
        this.anchor = anchor;
        anchor.classList.add("dt-anchored");
        this.ring = el("div", { class: "dt-ring-spot" + (reducedMotion() ? "" : " dt-pulse"), "aria-hidden": "true" });
        hostFor(anchor).appendChild(this.ring);
        this.scrollAnchor(anchor, sheet);
      }
      if (sheet) document.documentElement.classList.add("dt-coach-sheet");
      this.position();
      if (!this.quiet) announce(t("coach.announce", "Step {n} of {total}: {title}", { n: list.indexOf(step) + 1, total: list.length, title: step.title }));
      if (focus !== false) {
        var f = node.querySelector("[data-autofocus]") || node;
        setTimeout(function () { try { f.focus({ preventScroll: true }); } catch (e) { f.focus(); } }, 30);
      }
    },
    scrollAnchor: function (anchor, sheet) {
      var r = anchor.getBoundingClientRect();
      var vh = window.innerHeight;
      var bottomLimit = sheet ? vh - Math.min(vh * 0.55, 420) : vh;
      if (r.top >= 64 && r.bottom <= bottomLimit - 8) return;
      var behavior = reducedMotion() ? "auto" : "smooth";
      if (anchor.closest("dialog")) {
        // Inside a dialog the dialog scrolls, not the page.
        try { anchor.scrollIntoView({ block: sheet ? "start" : "center", behavior: behavior }); } catch (e) { anchor.scrollIntoView(); }
        return;
      }
      var target = window.scrollY + r.top - Math.max(72, (bottomLimit - r.height) / 2);
      window.scrollTo({ top: Math.max(0, target), behavior: behavior });
    },
    // Keep the ring on the anchor and the card inside the viewport.
    position: function () {
      var self = coach;
      if (!self.card) return;
      if (self.anchor && !self.anchor.isConnected) {
        var again = cardAnchor(currentCard());
        if (again) { self.anchor = again; again.classList.add("dt-anchored"); }
        else { self.show(false); return; }
      }
      var vw = document.documentElement.clientWidth, vh = window.innerHeight;
      var m = 12;
      var leftBound = m, rightBound = vw - m;
      var d = document.getElementById("dt-drawer");
      if (d && !d.hidden && !isPhone()) leftBound = Math.max(leftBound, d.getBoundingClientRect().right + m);
      var ba = document.getElementById("ba-panel");
      if (ba && !ba.hidden && !isPhone()) rightBound = Math.min(rightBound, ba.getBoundingClientRect().left - m);
      if (rightBound - leftBound < Math.max(300, self.card ? self.card.offsetWidth : 0)) { leftBound = m; rightBound = vw - m; }
      var r = self.anchor ? self.anchor.getBoundingClientRect() : null;
      if (self.ring && r) {
        var pad = 6;
        self.ring.style.left = (r.left - pad) + "px";
        self.ring.style.top = (r.top - pad) + "px";
        self.ring.style.width = (r.width + pad * 2) + "px";
        self.ring.style.height = (r.height + pad * 2) + "px";
        var off = r.bottom < 0 || r.top > vh;
        self.ring.style.display = off ? "none" : "";
      }
      var c = self.card;
      // The Build with AI button floats over the page's bottom-right corner
      // (the Save bar's button): step it aside while it hides the anchor.
      var launcher = document.getElementById("ba-launcher");
      var hideL = false;
      if (launcher && r && !launcher.hidden) {
        var lr = launcher.getBoundingClientRect();
        hideL = lr.left < r.right && lr.right > r.left && lr.top < r.bottom && lr.bottom > r.top;
      }
      document.documentElement.classList.toggle("dt-hide-launcher", hideL);
      if (c.classList.contains("dt-sheet")) {
        c.style.left = ""; c.style.top = "";
        // The anchor still under the sheet (a button at the foot of a
        // dialog): fold the sheet to its title and "Your turn" line.
        var cur = currentCard();
        var foldable = r && r.height < vh * 0.4 && !(cur && cur.advance && cur.advance.on === "next");
        if (!foldable) c.classList.remove("dt-sheet-mini", "dt-sheet-top");
        if (foldable && !c.hasAttribute("data-user-expanded")) {
          var wasMini = c.classList.contains("dt-sheet-mini");
          c.classList.remove("dt-sheet-mini", "dt-sheet-top");
          var top = c.getBoundingClientRect().top;
          var covered = r.bottom > top - 4 && r.top < vh;
          c.classList.toggle("dt-sheet-mini", covered);
          // Still under it once folded: the folded sheet moves to the top edge.
          if (covered && r.bottom > c.getBoundingClientRect().top - 4) c.classList.add("dt-sheet-top");
          if (covered !== wasMini) { var mt = c.querySelector(".dt-mini-toggle"); if (mt) mt.setAttribute("aria-expanded", covered ? "false" : "true"); }
        }
        return;
      }
      // Open pop-ups (the Add toolset list, the More menu) are obstacles:
      // the card never lands on one, so the visitor can still pick from it.
      var obstacles = [];
      document.querySelectorAll(".combobox-panel, .menu-list, .menu-panel, [role=listbox], [role=menu]").forEach(function (pn) {
        if (!visible(pn) || c.contains(pn)) return;
        var q = pn.getBoundingClientRect();
        if (q.width > 0 && q.height > 0) obstacles.push(q);
      });
      if (!c.hasAttribute("data-side") && !self.fallback && !self.codeCard && r) c.setAttribute("data-side", "right");
      var cw = c.offsetWidth, ch = c.offsetHeight;
      // Too tall to show whole: let the card scroll inside and drop the arrow.
      if (ch > vh - 2 * m) { c.removeAttribute("data-side"); cw = c.offsetWidth; ch = c.offsetHeight; }
      var tall = ch >= vh - 2 * m - 1;
      var x, y, side = "";
      if (!r || self.fallback || self.codeCard) {
        x = leftBound + (rightBound - leftBound - cw) / 2;
        y = (vh - ch) / 2;
      } else {
        var gap = 16;
        var places = [
          { side: "right", x: r.right + gap, y: r.top + r.height / 2 - ch / 2, ok: r.right + gap + cw <= rightBound },
          { side: "left", x: r.left - gap - cw, y: r.top + r.height / 2 - ch / 2, ok: r.left - gap - cw >= leftBound },
          { side: "below", x: r.left + r.width / 2 - cw / 2, y: r.bottom + gap, ok: r.bottom + gap + ch <= vh - m },
          { side: "above", x: r.left + r.width / 2 - cw / 2, y: r.top - gap - ch, ok: r.top - gap - ch >= m }
        ];
        // Below or above first for wide anchors (a whole table, a form).
        if (r.width > (rightBound - leftBound) * 0.5) places = [places[2], places[3], places[0], places[1]];
        var clear = function (p) {
          return !obstacles.some(function (q) {
            return p.x < q.right && p.x + cw > q.left && p.y < q.bottom && p.y + ch > q.top;
          });
        };
        // Beside each open pop-up, level with the anchor, as extra choices.
        obstacles.forEach(function (q) {
          var yy = r.top + r.height / 2 - ch / 2;
          places.push({ side: "", x: q.left - gap - cw, y: yy, ok: q.left - gap - cw >= leftBound });
          places.push({ side: "", x: q.right + gap, y: yy, ok: q.right + gap + cw <= rightBound });
        });
        places.forEach(function (p) {
          // Clamp first, as the final placement will be.
          p.x = Math.min(Math.max(p.x, leftBound), Math.max(leftBound, rightBound - cw));
          p.y = Math.min(Math.max(p.y, m), Math.max(m, vh - ch - m));
        });
        var pick = places.filter(function (p) { return p.ok && clear(p); })[0] || places.filter(function (p) { return p.ok; })[0];
        if (!pick) {
          // Nothing fits beside a big anchor (a whole editor): sit inside its
          // right edge, near its top, where text is usually blank, and keep
          // the sidebar and the start of the anchor visible.
          pick = { side: "", x: rightBound - cw, y: Math.max(r.top, m) + 12 };
        }
        x = pick.x;
        y = pick.y;
        side = pick.side;
      }
      x = Math.min(Math.max(x, leftBound), Math.max(leftBound, rightBound - cw));
      y = Math.min(Math.max(y, m), Math.max(m, vh - ch - m));
      c.style.left = Math.round(x) + "px";
      c.style.top = Math.round(y) + "px";
      // The little arrow points at the anchor from the side the card sits on.
      if (side && r && !tall) {
        c.setAttribute("data-side", side);
        if (side === "left" || side === "right") c.style.setProperty("--dt-arrow", Math.round(Math.min(Math.max(r.top + r.height / 2 - y, 20), ch - 20)) + "px");
        else c.style.setProperty("--dt-arrow", Math.round(Math.min(Math.max(r.left + r.width / 2 - x, 20), cw - 20)) + "px");
      } else c.removeAttribute("data-side");
    },
    schedule: function () {
      if (coach.raf) return;
      coach.raf = requestAnimationFrame(function () { coach.raf = 0; coach.position(); });
    },
    advance: function (byNext) {
      if (!state.active) return;
      var step = BY_ID[state.active.step];
      var cards = (step && step.cards) || [];
      if (state.active.card + 1 < cards.length) {
        state.active.card += 1;
        save();
        this.show(true);
        return;
      }
      // Past the last card.
      if (byNext && !isDone(step.id)) {
        var reading = rules(step).every(function (r) { return r.on === "page"; });
        if (reading) {
          // A reading step: its page rule decides; Next still closes the card.
          checkPages();
        }
      }
      var id = step.id;
      endWalkthrough(false);
      if (isDone(id)) {
        if (byNext) toast.show(step, state.steps[id].via, nextStep(id));
      } else {
        drawer.open(false);
      }
    },
    onAnchorClick: function (name, target) {
      var card = currentCard();
      if (!card || !card.advance || card.advance.on !== "click") return;
      if (card.anchor !== name && card.advance.anchor !== name) return;
      var self = this;
      // A button that opens a pick list (+ Add toolset): move on once the
      // visitor has picked and the list has closed, not on the opening click.
      if (target && (target.hasAttribute("aria-haspopup") || target.hasAttribute("data-combobox-open"))) {
        var expectStep = state.active && state.active.step, expectCard = state.active && state.active.card;
        var waitClose = function () {
          if (!state.active || state.active.step !== expectStep || state.active.card !== expectCard) return;
          if (target.isConnected && target.getAttribute("aria-expanded") === "true") { setTimeout(waitClose, 300); return; }
          var st = BY_ID[state.active.step];
          if (state.active.card + 1 < ((st && st.cards) || []).length) { state.active.card += 1; save(); self.show(true); }
          else self.advance(false);
        };
        setTimeout(waitClose, 400);
        return;
      }
      // Save first: the click may navigate away.
      if (state.active) {
        var step = BY_ID[state.active.step];
        if (state.active.card + 1 < ((step && step.cards) || []).length) { state.active.card += 1; save(); setTimeout(function () { self.show(true); }, 60); }
        else setTimeout(function () { self.advance(false); }, 60);
      }
    },
    onEvent: function (name, detail) {
      var card = currentCard();
      var a = card && card.advance;
      if (!a || a.on !== "event" || a.name !== name) return;
      detail = detail || {};
      if (a.mine && !isMine(detail)) return;
      if (typeof a.match === "function" && !safe(function () { return a.match(detail, location, document); }, false)) return;
      if (!grew(a, detail)) return;
      this.advance(false);
    },
    checkPage: function () {
      var card = currentCard();
      if (card && card.advance && card.advance.on === "page" && typeof card.advance.match === "function" &&
          safe(function () { return card.advance.match(location, document); }, false)) this.advance(false);
    },
    // A step that needs another first (A4 before A3): say so, offer to go there.
    renderNeeds: function (step, need) {
      this.clear();
      var sheet = isPhone();
      var node = el("div", { class: "dt-coach dt-coach-center" + (sheet ? " dt-sheet" : ""), role: "dialog", "aria-label": step.title, tabindex: "-1" });
      node.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); coach.clear(); } });
      if (sheet) node.appendChild(el("div", { class: "dt-grabber", "aria-hidden": "true" }));
      node.appendChild(el("div", { class: "dt-coach-head" }, [
        el("p", { class: "dt-eyebrow", text: stepLabel(step) }),
        closeX(t("coach.close", "Close"), function () { coach.clear(); })
      ]));
      node.appendChild(el("h3", { class: "dt-coach-title", text: step.title }));
      node.appendChild(el("p", { class: "dt-body", text: needsText(need) }));
      var go = btn(t("coach.needsGo", "Create an agent first"), "primary dt-btn " + (sheet ? "dt-btn-l" : "dt-btn-s"), function () { startStep(need.id); }, { "data-autofocus": "1" });
      var foot = el("div", { class: "dt-coach-foot" + (sheet ? " dt-foot-one" : "") }, [sheet ? null : el("span", { class: "dt-spacer" }), go]);
      node.appendChild(foot);
      document.body.appendChild(node);
      this.card = node;
      this.fallback = true;
      this.needs = true;
      if (sheet) document.documentElement.classList.add("dt-coach-sheet");
      this.position();
      announce(step.title + ". " + needsText(need));
      setTimeout(function () { try { go.focus({ preventScroll: true }); } catch (e) { go.focus(); } }, 30);
    },
    // The Code step card (kind:"code"): tabs, the stream notes, copy counts.
    renderCodeCard: function (step, focus) {
      var cc = step.codeCard || {};
      var tabs = Array.isArray(cc.tabs) ? cc.tabs.filter(function (x) { return x && x.code; }) : [];
      if (!tabs.length) tabs = stepCodeTabs(step).filter(function (x) { return x.id !== "portal"; });
      var lang = defaultLang(tabs);
      var card0 = (step.cards && step.cards[0]) || {};
      var sheet = isPhone();
      var titleId = nextId("cc");
      var node = el("div", { class: "dt-coach dt-codecard dt-coach-center" + (sheet ? " dt-sheet" : ""), role: "dialog", "aria-labelledby": titleId, tabindex: "-1" });
      node.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endWalkthrough(true); }
      });
      if (sheet) node.appendChild(el("div", { class: "dt-grabber", "aria-hidden": "true" }));
      node.appendChild(el("div", { class: "dt-coach-head" }, [
        el("p", { class: "dt-eyebrow", text: stepLabel(step) }),
        closeX(t("coach.close", "Close"), function () { endWalkthrough(true); })
      ]));
      node.appendChild(el("h3", { class: "dt-coach-title dt-h-l", id: titleId, text: card0.title || step.title }));
      if (card0.body || step.summary) node.appendChild(richEl("p", "dt-body", card0.body || step.summary));
      var keysOff = t("codeCard.keysOff", "API keys are off in the demo. Run this against your own copy.");
      var events = Array.isArray(cc.events) ? cc.events.filter(function (x) { return x && x.name; }) : [];
      var names = events.map(function (x) { return x.name + ":"; });
      var notes = (Array.isArray(cc.notes) ? cc.notes : []).filter(function (x) {
        if (!x || x === keysOff) return false;
        return typeof x !== "string" || !names.some(function (nm) { return x.indexOf(nm) === 0; });
      });
      var notesTitle = cc.notesTitle || (typeof COPY.codeCard === "object" && typeof COPY.codeCard.events === "string" ? COPY.codeCard.events : "Good to know");
      var notesBox = el("div", { class: "dt-cc-notes" }, [el("p", { class: "dt-code-label", text: notesTitle })]);
      var dl = el("dl", { class: "dt-cc-list" });
      var ul = el("ul", { class: "dt-cc-ul" });
      events.forEach(function (x) { dl.appendChild(el("dt", { text: x.name })); dl.appendChild(richEl("dd", "", x.text || "")); });
      notes.forEach(function (x) {
        if (typeof x === "object" && (x.term || x.name)) {
          dl.appendChild(el("dt", { text: x.term || x.name }));
          dl.appendChild(richEl("dd", "", x.text || ""));
        } else {
          ul.appendChild(richEl("li", "", String(x)));
        }
      });
      if (dl.children.length) notesBox.appendChild(dl);
      if (ul.children.length) notesBox.appendChild(ul);
      var hasNotes = dl.children.length || ul.children.length;
      var aside = el("div", { class: "dt-cc-aside" }, [
        hasNotes ? notesBox : null,
        richEl("p", "dt-cc-warn", keysOff)
      ]);
      node.appendChild(el("div", { class: "dt-cc-grid" }, [codeTabs(tabs, step.id, lang, { copyInRow: true }), aside]));
      var nx = nextStep(step.id);
      var foot = el("div", { class: "dt-coach-foot dt-cc-foot" });
      if (step.doneWhen) foot.appendChild(richEl("p", "dt-donewhen dt-m0", label2(t("coach.doneWhen", "Done when:")) + " " + step.doneWhen));
      foot.appendChild(el("span", { class: "dt-spacer" }));
      if (cc.action && (cc.action.href || cc.action.resolve)) {
        foot.appendChild(btn(cc.action.label || t("codeCard.tryTestChat", "Try it in Test chat instead"), "dt-btn", function () {
          var go = typeof cc.action.resolve === "function" ? safe(function () { return cc.action.resolve(); }, null) : null;
          endWalkthrough(false);  // the card would cover the Test chat on the next page
          Promise.resolve(go).then(function (href) { goTo(subst(href || cc.action.href)); }, function () { goTo(subst(cc.action.href)); });
        }));
      }
      var own = btn("", "dt-btn", function () { finish.open(own); });
      own.appendChild(icon("terminal"));
      own.appendChild(document.createTextNode(t("codeCard.runOwn", "Run your own copy")));
      foot.appendChild(own);
      foot.appendChild(btn(t("codeCard.next", t("toast.next", "Next step")), "primary dt-btn", function () {
        if (nx) startStep(nx.id); else { endWalkthrough(false); drawer.open(true); }
      }, isDone(step.id) ? { "data-autofocus": "1" } : null));
      node.appendChild(foot);
      document.body.appendChild(node);
      this.card = node;
      this.codeCard = true;
      if (sheet) document.documentElement.classList.add("dt-coach-sheet");
      this.position();
      var list = trackSteps(step.track);
      announce(t("coach.announce", "Step {n} of {total}: {title}", { n: list.indexOf(step) + 1, total: list.length, title: step.title }));
      if (focus !== false) {
        var f = node.querySelector("[data-autofocus]") || node.querySelector('[role="tab"][aria-selected="true"]') || node;
        setTimeout(function () { try { f.focus({ preventScroll: true }); } catch (e) { f.focus(); } }, 30);
      }
    }
  };

  // "Done when:" and "Your turn:" carry their colon in the catalog; older copy did not.
  function label2(s) { s = String(s || ""); return /[:\uff1a]$/.test(s) ? s : s + ":"; }
  function skipStep(step) {
    var nx = nextStep(step.id);
    if (nx && nx.id !== step.id) startStep(nx.id);
    else { endWalkthrough(true); drawer.open(true); }
  }
  function startStep(id) {
    var step = BY_ID[id];
    if (!step) return;
    toast.hide();
    var need = missingFor(step);
    if (need) {
      if (drawer.isOpen()) drawer.close(false);
      if (state.active) { state.active = null; save(); }
      coach.renderNeeds(step, need);
      return;
    }
    if (state.status !== "done") state.status = "active";
    state.active = { step: id, card: 0 };
    state.ui.expanded = null;
    save();
    renderAll();
    // The coach card stands alone, as on the canvas: the checklist steps aside.
    if (drawer.isOpen()) drawer.close(false);
    if (step.kind === "code") { coach.show(true); return; }
    // The first card this page still needs (skip() drops "get there" cards).
    var cards = step.cards || [];
    var first = null;
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      if (typeof c.skip === "function" && safe(function () { return c.skip(location, document); }, false)) continue;
      first = c;
      break;
    }
    var here = first && first.anchor ? cardAnchor(first) : null;
    // Stay when that card's anchor is here, unless it is itself a "get there"
    // card (it has skip) and the step's own start page is elsewhere.
    if (here && typeof first.skip !== "function") { coach.show(true); return; }
    startPath(step).then(function (path) {
      if (!state.active || state.active.step !== id) return;
      if (path && !samePlace(path)) goTo(path);
      else coach.show(true);
    });
  }
  function endWalkthrough(returnFocus) {
    var had = state.active;
    state.active = null;
    save();
    coach.clear();
    renderAll();
    if (returnFocus && had) {
      var e = drawer.isOpen() ? drawer.node.querySelector(".dt-step-title") : (isPhone() ? document.getElementById("dt-pill") : document.getElementById("dt-entry"));
      if (e && visible(e)) e.focus();
    }
  }

  function renderAll() {
    syncEntry();
    if (drawer.node && !drawer.node.hidden) drawer.render();
    renderResume();
  }

  // ---------------------------------------------------------------- boot
  function boot() {
    document.body.appendChild(live);
    document.body.appendChild(toastRegion);
    drawer.ensure();

    var entry = document.getElementById("dt-entry");
    if (entry) entry.addEventListener("click", onEntryClick);
    var pill = document.getElementById("dt-pill");
    if (pill) pill.addEventListener("click", onEntryClick);

    document.addEventListener("click", onAnchorClick, true);
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || e.defaultPrevented || !coach.card) return;
      var a = document.activeElement;
      if (welcome.isOpen() || finish.modal.isOpen() || document.querySelector("dialog[open]")) return;
      var gs = document.getElementById("global-search-dialog");
      if (gs && !gs.hidden && gs.offsetParent !== null) return;
      if (a && a !== document.body && !coach.card.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
      if (coach.needs) coach.clear(); else endWalkthrough(true);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || !e.target || !e.target.matches || !e.target.matches('[data-tour][role="button"]')) return;
      onAnchorClick(e);
    }, true);
    document.addEventListener("toggle", onToggle, true);
    // A click can open a pop-up beside the anchor: place the card again.
    document.addEventListener("click", function () { setTimeout(coach.schedule, 60); }, false);
    // Each detail object is handled once: HOOKS also leaves the load-time
    // ones on window (__tourSaved, __tourRunStarted) for a script that
    // attached late, and the same object may then arrive as an event too.
    var seen = typeof WeakSet === "function" ? new WeakSet() : null;
    function once(detail) {
      if (!detail || typeof detail !== "object" || !seen) return true;
      if (seen.has(detail)) return false;
      seen.add(detail);
      return true;
    }
    ["tour:test-chat-sent", "tour:palette-used", "tour:saved", "tour:run-finished"].forEach(function (name) {
      window.addEventListener(name, function (e) { if (once(e.detail)) onEvent(name, e.detail || {}); });
    });
    var onRunStarted = function (d) { if (!once(d)) return; runStarted(d || {}); onEvent("tour:run-started", d || {}); };
    window.addEventListener("tour:run-started", function (e) { onRunStarted(e.detail); });
    window.addEventListener("tour:ai-done", function (e) { onAiDone(e.detail || {}); });
    window.addEventListener("tour:resume", function () {
      if (state.status === "new") { openWelcome(null); return; }
      drawer.open(!state.active);
      if (state.active) coach.show(true);
    });
    window.addEventListener("tour:restart", function () {
      coach.clear();
      toast.hide();
      drawer.close(false);
      var keep = { code_lang: state.code_lang, offer_ai: state.offer_ai };
      state = freshState();
      state.code_lang = keep.code_lang;
      state.offer_ai = keep.offer_ai;
      save(true);  // a restart drops the old progress on purpose
      renderAll();
      openWelcome(null);
    });
    // Progress made in another tab shows here too.
    window.addEventListener("storage", function (e) {
      if (e.key !== KEY || !e.newValue) return;
      var other = safe(function () { return JSON.parse(e.newValue); }, null);
      if (other && other.reset_id === state.reset_id && other.status === "new" && state.status !== "new" &&
          !Object.keys(other.steps || {}).length) return;  // a restart elsewhere: keep this tab as it is
      if (mergeFrom(other)) { renderAll(); coach.refresh(); }
    });
    window.addEventListener("resize", coach.schedule, { passive: true });
    window.addEventListener("scroll", coach.schedule, { passive: true, capture: true });
    var onSwap = function () { checkPages(); coach.checkPage(); if (state.active && !coach.card) coach.show(false); coach.schedule(); };
    document.body.addEventListener("htmx:afterSettle", onSwap);
    window.addEventListener("popstate", onSwap);
    window.addEventListener("hashchange", onSwap);
    try {
      window.matchMedia(PHONE).addEventListener("change", function () { if (coach.card) coach.show(false); });
    } catch (e) { /* old browser */ }

    // Overview tells which D2 applies; a walkthrough on the old one restarts.
    var switched = learnAttention();
    recordBaselines();
    // Completion first, with no UI, so a step done by hand on this page ticks.
    checkPages();
    safe(function () {
      if (window.__tourRunStarted) onRunStarted(window.__tourRunStarted);
      if (window.__tourSaved && once(window.__tourSaved)) onEvent("tour:saved", window.__tourSaved);
    });
    syncEntry();
    renderResume();
    schedulePoll(1000);

    if (doneCount() === TOTAL && !state.finish_shown) {
      showFinishOnce();
    } else if (state.status === "new" && !state.active) {
      openWelcome(null);
    } else {
      if (state.ui.drawer && !isPhone()) drawer.open(false);
      if (state.active && switched && BY_ID[state.active.step].alt) { startStep(state.active.step); return; }
      if (state.active) {
        coach.checkPage();
        if (state.active) coach.show(Date.now() - (state.active.at || 0) < 2 * 60 * 1000);
      }
    }
  }

  window.__demoTour = {
    state: function () { return JSON.parse(JSON.stringify(state)); },
    start: startStep,
    complete: function (id, via) { return complete(id, via || "hand"); },
    open: function () { drawer.open(true); },
    welcome: function () { openWelcome(null); },
    finish: function () { finish.open(null); },
    storage: function () {
      if (store === null) return "memory";
      return safe(function () { return store === window.localStorage ? "local" : "session"; }, "session");
    }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
