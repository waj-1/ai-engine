/* The public demo's usage analytics (demo_mode/analytics.py, docs/deploy/demo.md
 * "Usage analytics").
 *
 * Loaded only in demo mode with DEMO_POSTHOG_KEY set; the page embeds its config
 * in #demo-analytics-config. Sends named events to PostHog from this browser.
 *
 * What it sends: event names (all `engine_demo_*`, plus `$pageview` and
 * `$pageleave`), the page's section and its path with every id replaced by
 * `:id`, the tab, tool and action names of the Build with AI steps, counts,
 * durations rounded into buckets, which cap was hit, and which kind of link was
 * followed. Every event carries `app: "engine_demo"` and `env` ("public" on the
 * demo's own address, "local" anywhere else).
 *
 * What it never sends: anything a visitor typed (prompts, chat messages, names
 * of agents, knowledge bases or files), any text from the page (answers, step
 * labels, page titles), query strings other than a few fixed keys, or a run,
 * agent or conversation id. There is no session replay, no autocapture of
 * clicks or inputs, no heatmaps and no surveys.
 *
 * How it sees what happens without changing the portal's own scripts: it reads
 * a copy of the streams the Build with AI panel and the test chat already
 * receive (window.fetch, response.clone()), watches the voice sockets'
 * open and close (window.WebSocket), and listens for clicks and form
 * submissions on the document. It also forwards the guided tour's window
 * events (`tour:*`) when the tour is on the page.
 */
(function () {
  "use strict";
  var cfgEl = document.getElementById("demo-analytics-config");
  if (!cfgEl || window.__demoAnalytics) return;
  var cfg;
  try { cfg = JSON.parse(cfgEl.textContent || "{}"); } catch (e) { return; }
  if (!cfg || !cfg.key || !cfg.host) return;
  window.__demoAnalytics = true;

  var UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
  var KEEP_QUERY = { tab: 1, env: 1, show: 1, utm_source: 1, utm_medium: 1, utm_campaign: 1,
                     utm_content: 1, utm_term: 1, gclid: 1, fbclid: 1, ref: 1 };
  var MSG = cfg.messages || {};
  var SEEDED = {};
  (cfg.seeded || []).forEach(function (s) { SEEDED[s] = 1; });

  function isLocalHost(h) {
    h = (h || "").toLowerCase();
    return h === "localhost" || h === "::1" || h === "[::1]" || /^127\./.test(h) ||
      /^10\./.test(h) || /^192\.168\./.test(h) || /^172\.(1[6-9]|2\d|3[01])\./.test(h) ||
      /\.(local|localhost|internal|test)$/.test(h);
  }
  var ENV = (!isLocalHost(location.hostname) && cfg.public_host &&
             location.hostname.toLowerCase() === cfg.public_host) ? "public" : "local";

  // A path with its ids replaced (/admin/agents/:id/voice), and a number-only
  // segment too (a run or row number).
  function template(path) {
    return String(path || "/").replace(UUID, ":id").replace(/\/\d+(?=\/|$)/g, "/:n")
      .replace(/\/[0-9a-f]{24,}(?=\/|$)/gi, "/:id");
  }
  function cleanUrl(value) {
    try {
      var u = new URL(value, location.href);
      var own = u.host === location.host;
      if (!own) return u.origin + u.pathname;  // another site: no query, no fragment
      var keep = [];
      u.searchParams.forEach(function (v, k) {
        if (KEEP_QUERY[k]) keep.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v).replace(UUID, ":id").slice(0, 60)));
      });
      return u.origin + template(u.pathname) + (keep.length ? "?" + keep.join("&") : "");
    } catch (e) { return ""; }
  }
  var URL_KEY = /(url|referrer)$/i;
  var PATH_KEY = /pathname$/i;
  // The library adds the page title to a $pageview as `title` (and `$title`):
  // an agent page's title carries the agent's name, which a visitor may have typed.
  var DROP_KEY = /^(\$?title|\$el_text|\$elements|\$elements_chain)$/;
  function scrub(props) {
    if (!props || typeof props !== "object") return;
    Object.keys(props).forEach(function (k) {
      var v = props[k];
      if (DROP_KEY.test(k)) { delete props[k]; return; }
      if (typeof v !== "string") return;
      if (k === "$referrer" || k === "$initial_referrer") {
        if (v && v !== "$direct") props[k] = cleanUrl(v);
      } else if (URL_KEY.test(k)) {
        if (/^https?:/i.test(v) || v.charAt(0) === "/") props[k] = cleanUrl(v);
      } else if (PATH_KEY.test(k)) {
        props[k] = template(v);
      }
    });
  }
  function beforeSend(event) {
    try {
      if (!event) return event;
      scrub(event.properties);
      if (event.properties) { scrub(event.properties.$set); scrub(event.properties.$set_once); }
      scrub(event.$set);
      scrub(event.$set_once);
    } catch (e) { /* send it as built */ }
    return event;
  }

  // The PostHog loader (posthog-js snippet): a stub that queues calls until
  // the SDK has loaded from the ingestion host.
  /* eslint-disable */
  !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],Object.defineProperty(u,"toString",{configurable:!0,enumerable:!0,writable:!0,value:function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e}}),Object.defineProperty(u.people,"toString",{configurable:!0,enumerable:!0,writable:!0,value:function(){return u.toString(1)+".people (stub)"}}),o="init capture register register_once register_for_session unregister unregister_for_session reset get_distinct_id get_session_id set_config opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing debug".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
  /* eslint-enable */
  // The SDK replaces the stub on window.posthog once it has loaded: always
  // call through window.posthog, never a reference kept from before.
  function ph() { return window.posthog; }
  ph().init(cfg.key, {
    api_host: cfg.host,
    defaults: "2026-08-30",
    // Only the explicit events below: no autocapture, no automatic pageview
    // (it carries the raw address; ours carries the section), no replay,
    // heatmaps, dead clicks, rage clicks, surveys, exceptions or web vitals,
    // whatever the project's settings say.
    autocapture: false,
    capture_pageview: false,
    capture_pageleave: true,
    disable_session_recording: true,
    capture_heatmaps: false,
    capture_dead_clicks: false,
    rageclick: false,
    disable_surveys: true,
    capture_exceptions: false,
    capture_performance: false,
    advanced_disable_flags: true,
    mask_all_text: true,
    mask_all_element_attributes: true,
    // The project id stays the browser's own; the same project key on a
    // sibling subdomain (the product's site) shares it through the cookie
    // PostHog sets on the parent domain.
    persistence: "localStorage+cookie",
    cross_subdomain_cookie: true,
    person_profiles: "identified_only",
    before_send: beforeSend
  });

  // ---------------------------------------------------------------------
  // Tour state (localStorage demo-tour:v1), read on each page: short ids only.
  // ---------------------------------------------------------------------
  function shortId(v) { return typeof v === "string" && /^[a-z0-9_:-]{1,40}$/i.test(v) ? v : undefined; }
  function tourState() {
    try {
      var raw = JSON.parse(localStorage.getItem("demo-tour:v1") || "null");
      if (!raw || typeof raw !== "object") return {};
      return { tour_goal: shortId(raw.goal), tour_code_lang: shortId(raw.code_lang), tour_status: shortId(raw.status) };
    } catch (e) { return {}; }
  }

  var supers = { app: "engine_demo", env: ENV, demo_page: cfg.page || "portal" };
  var ts = tourState();
  Object.keys(ts).forEach(function (k) { if (ts[k]) supers[k] = ts[k]; });
  ph().register(supers);

  function send(name, props) {
    try {
      var p = {};
      Object.keys(props || {}).forEach(function (k) { if (props[k] !== undefined) p[k] = props[k]; });
      ph().capture(name, p);
    } catch (e) { /* analytics never breaks the page */ }
  }

  // sessionStorage notes carried to the next page (a form's result is the page
  // it redirects to). Never sent.
  var ss = (function () { try { sessionStorage.setItem("da:probe", "1"); return sessionStorage; } catch (e) { return null; } })();
  function note(key, value) { try { if (ss) ss.setItem("da:" + key, JSON.stringify(value)); } catch (e) {} }
  function take(key) {
    try {
      if (!ss) return null;
      var raw = ss.getItem("da:" + key);
      ss.removeItem("da:" + key);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }
  function peek(key) { try { return ss ? JSON.parse(ss.getItem("da:" + key) || "null") : null; } catch (e) { return null; } }
  function fresh(n, ms) { return n && typeof n.t === "number" && Date.now() - n.t < (ms || 120000); }

  // ---------------------------------------------------------------------
  // The page itself
  // ---------------------------------------------------------------------
  var params = new URLSearchParams(location.search);
  var path = location.pathname;
  var section = cfg.section || "";
  var tab = shortId(params.get("tab") || "") || undefined;
  if (/^\/admin\/agents\/?$/.test(path) && params.get("profile_id")) {
    section = "agent_detail";
    tab = tab || "overview";
  } else if (/^\/admin\/agents\/[0-9a-f-]{36}\/([a-z-]+)/i.test(path)) {
    section = "agent_detail";
    tab = path.match(/^\/admin\/agents\/[0-9a-f-]{36}\/([a-z-]+)/i)[1];
  } else if (/^\/admin\/templates/.test(path)) {
    section = "agent_new";
  }
  var page = cfg.page || "portal";
  if (page === "entry") section = "entry";
  if (page === "locked") section = "locked";
  if (page === "rate" || page === "daily" || page === "upload_size") section = "limit";
  if (!section) section = "other";

  send("$pageview", { section: section, tab: tab, path_template: template(path),
                      $current_url: cleanUrl(location.href) });

  function limitHit(limit, where) { send("engine_demo_limit_hit", { limit: limit, where: where }); }

  if (page === "entry") {
    send("engine_demo_entry_viewed", {});
  } else if (page === "locked") {
    send("engine_demo_locked_hit", { path_template: template(path) });
  } else if (page === "rate" || page === "daily" || page === "upload_size") {
    limitHit(page, "page");
  } else {
    // A demo session: the first portal page after "Try the demo", or the first
    // portal page of a tab that came back with its sign-in still valid.
    var entered = take("entered");
    if (!peek("started")) {
      note("started", { t: Date.now() });
      send("engine_demo_session_started", { via: fresh(entered, 10 * 60 * 1000) ? "try_button" : "returning" });
    }
    // A form submitted on the previous page, confirmed by where it led.
    var pending = take("pending");
    if (fresh(pending)) {
      if (pending.kind === "agent" && params.get("profile_id")) {
        send("engine_demo_agent_created", { via: pending.via });
      } else if (pending.kind === "run") {
        var m = path.match(/^\/admin\/evals\/runs\/([0-9a-f-]{36})/i);
        if (m) {
          send("engine_demo_test_run_started", { via: pending.via });
          watchRun(m[1], pending.via, Date.now());
        }
      }
    }
  }

  // ---------------------------------------------------------------------
  // Clicks and forms
  // ---------------------------------------------------------------------
  function placement(el) {
    if (el.closest("[data-demo-banner]")) return "banner";
    if (el.closest("#ba-panel")) return "assistant";
    if (el.closest("[class*='tour'],[id*='tour'],[data-tour]")) return "tour";
    if (page === "entry") return "entry";
    if (page !== "portal") return page;
    return "page";
  }
  function linkTarget(a) {
    var href = a.getAttribute("href") || "";
    var u;
    try { u = new URL(href, location.href); } catch (e) { return null; }
    if (u.host === location.host) {
      if (/^\/admin\/docs/.test(u.pathname)) return { target: "docs", host: "self" };
      if (/^\/admin\/demo\/call/.test(u.pathname)) return { target: "voice_call_link", host: "self" };
      return null;
    }
    var host = u.hostname.toLowerCase();
    var repo = (cfg.repo_url || "").replace(/\/+$/, "");
    var full = u.href.replace(/\/+$/, "");
    var target = "other";
    if (cfg.showcase_url && full.indexOf(cfg.showcase_url.replace(/\/+$/, "")) === 0) target = "showcase";
    else if (repo && full.indexOf(repo) === 0) target = /\/(blob|tree)\/[^/]+\/docs\/|\/docs\b|#/.test(full.slice(repo.length)) ? "docs" : "github";
    else if (host === "github.com") target = "github";
    else if (/docs|readme/i.test(u.pathname) || /^docs\./.test(host)) target = "docs";
    // Only hosts a visitor cannot write into the page; anything else is "other".
    var knownHost = (host === "github.com" || (repo && full.indexOf(repo) === 0) ||
                     target === "showcase") ? host : "other";
    return { target: target, host: knownHost };
  }
  document.addEventListener("click", function (e) {
    try {
      var el = e.target && e.target.closest ? e.target : null;
      if (!el) return;
      var a = el.closest("a[href]");
      if (a) {
        var t = linkTarget(a);
        if (t) send("engine_demo_outbound_click", { target: t.target, host: t.host, placement: placement(a) });
      }
      if (el.closest("#ba-launcher")) send("engine_demo_assistant_opened", { via: "launcher" });
      var chip = el.closest("#ba-chips [data-prompt]");
      if (chip) {
        var chips = Array.prototype.slice.call(document.querySelectorAll("#ba-chips [data-prompt]"));
        nextAsk = { source: "chip", chip: chips.indexOf(chip) + 1 };
      }
    } catch (err) { /* never break a click */ }
  }, true);

  function formAction(form) {
    try { return new URL(form.getAttribute("action") || location.href, location.href).pathname; }
    catch (e) { return ""; }
  }
  document.addEventListener("submit", function (e) {
    try {
      var form = e.target;
      if (!form || !form.getAttribute) return;
      var action = formAction(form);
      var method = (form.getAttribute("method") || "get").toLowerCase();
      if (action === "/admin/demo/enter") {
        note("entered", { t: Date.now() });
        send("engine_demo_try_clicked", {});
        return;
      }
      if (method !== "post" || action === "/admin/logout") return;
      if (action === "/admin/agents/from-template") note("pending", { kind: "agent", via: "template", t: Date.now() });
      else if (/^\/admin\/agents\/[0-9a-f-]{36}\/fork$/i.test(action)) note("pending", { kind: "agent", via: "copy", t: Date.now() });
      else if (action === "/admin/eval-runs/trigger" || action === "/admin/evals/run") note("pending", { kind: "run", via: "portal", t: Date.now() });
      else if (/^\/admin\/eval-runs\/schedules\/[0-9a-f-]{36}\/fire$/i.test(action)) note("pending", { kind: "run", via: "schedule", t: Date.now() });
      send("engine_demo_form_submitted", { form: template(action), section: section });
    } catch (err) { /* never block a form */ }
  }, true);

  // The Build with AI panel opens by itself on a browser's first visit
  // (builder-assistant.js runs after this file and sets the "seen" flag),
  // except on a phone (a full-screen sheet there) and with the guided tour on
  // (its Welcome comes first).
  try {
    if (document.getElementById("ba-panel") && !document.getElementById("demo-tour-config") &&
        !(window.matchMedia && window.matchMedia("(max-width: 640px)").matches) &&
        !localStorage.getItem("demo-assistant:seen")) {
      send("engine_demo_assistant_opened", { via: "first_visit" });
    }
  } catch (e) {}

  // ---------------------------------------------------------------------
  // Streams: a copy of what the page already reads
  // ---------------------------------------------------------------------
  var nextAsk = null;
  function limitOf(text) {
    text = String(text || "");
    if (MSG.daily && text.indexOf(MSG.daily) === 0) return "daily_turns";
    if (MSG.rate && text.indexOf(MSG.rate) === 0) return "rate";
    return null;
  }
  // Reads an SSE body to its end, calling onEvent for each JSON event.
  function readSse(resp, onEvent) {
    if (!resp.body || !resp.body.getReader) return resp.text().then(function (t) { parseBlocks(t + "\n\n", onEvent); });
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) { parseBlocks(buffer + "\n\n", onEvent); return; }
        buffer += decoder.decode(chunk.value, { stream: true });
        var cut = buffer.lastIndexOf("\n\n");
        if (cut !== -1) { parseBlocks(buffer.slice(0, cut + 2), onEvent); buffer = buffer.slice(cut + 2); }
        return pump();
      });
    }
    return pump();
  }
  function parseBlocks(text, onEvent) {
    text.split("\n\n").forEach(function (block) {
      block.split("\n").forEach(function (line) {
        if (line.indexOf("data:") !== 0) return;
        var raw = line.slice(5).trim();
        if (!raw || raw === "[DONE]") return;
        try { onEvent(JSON.parse(raw)); } catch (e) { /* keep-alive */ }
      });
    });
  }
  function bucketSeconds(s) {
    if (s < 5) return "0-5s";
    if (s < 15) return "5-15s";
    if (s < 30) return "15-30s";
    if (s < 60) return "30-60s";
    if (s < 120) return "1-2m";
    if (s < 300) return "2-5m";
    return "5m+";
  }

  function observeAssistant(resp, started) {
    var ask = nextAsk || { source: "typed" };
    nextAsk = null;
    send("engine_demo_assistant_turn_sent", { source: ask.source, chip: ask.chip });
    if (!resp.ok) {
      var limit = resp.status === 429 ? "rate" : null;
      if (limit) limitHit(limit, "assistant");
      send("engine_demo_assistant_turn_finished", { ok: false, status: resp.status, limited: !!limit, limit: limit || undefined,
                                                    tool_count: 0, tools: [], duration: bucketSeconds((Date.now() - started) / 1000) });
      return;
    }
    var tools = {}, actions = {}, count = 0, failed = 0, first = "", error = false, changed = false;
    readSse(resp, function (ev) {
      if (ev.type === "step") {
        count += 1;
        var tool = shortId(ev.tool);
        var act = shortId(ev.action || "") ? ev.action : "";
        if (!tool) return;
        if (ev.ok === false) { failed += 1; return; }
        tools[tool] = 1;
        actions[tool + (act ? ":" + act : "")] = 1;
        if ((tool === "templates" && act === "use") || tool === "fork_profile") send("engine_demo_agent_created", { via: "assistant" });
        if (tool === "trigger_eval_run") send("engine_demo_test_run_started", { via: "assistant" });
      } else if (ev.type === "answer" && typeof ev.content === "string" && first.length < 400) {
        first += ev.content;
      } else if (ev.type === "error") {
        error = true;
      } else if (ev.type === "changed") {
        changed = true;
      }
    }).catch(function () { return "dropped"; }).then(function (dropped) {
      var limit = limitOf(first);
      if (limit) limitHit(limit, "assistant");
      send("engine_demo_assistant_turn_finished", {
        ok: !error && !limit, limited: !!limit, limit: limit || undefined,
        tools: Object.keys(tools), actions: Object.keys(actions), tool_count: count, steps_failed: failed,
        changed: changed, dropped: dropped === "dropped" || undefined,
        duration: bucketSeconds((Date.now() - started) / 1000)
      });
    });
  }

  function observeTestChat(url, resp, started) {
    var m = url.pathname.match(/^\/v1\/agents\/([^/]+)\/chat$/);
    var agent = m && SEEDED[decodeURIComponent(m[1])] ? "seeded" : "visitor";
    send("engine_demo_test_chat_sent", { agent: agent });
    if (!resp.ok) {
      var lim = resp.status === 429 ? "rate" : null;
      if (lim) limitHit(lim, "test_chat");
      send("engine_demo_test_chat_finished", { agent: agent, ok: false, status: resp.status, limited: !!lim, limit: lim || undefined });
      return;
    }
    var first = "", error = false;
    readSse(resp, function (ev) {
      if (ev.type === "answer" && typeof ev.content === "string" && first.length < 400) first += ev.content;
      else if (ev.type === "error") error = true;
    }).catch(function () {}).then(function () {
      var limit = limitOf(first);
      if (limit) limitHit(limit, "test_chat");
      send("engine_demo_test_chat_finished", { agent: agent, ok: !error && !limit, limited: !!limit,
                                               limit: limit || undefined, duration: bucketSeconds((Date.now() - started) / 1000) });
    });
  }

  if (window.fetch && window.Response && Response.prototype.clone) {
    var nativeFetch = window.fetch;
    window.fetch = function (input, init) {
      var result = nativeFetch.apply(this, arguments);
      try {
        var raw = typeof input === "string" ? input : (input && input.url) || "";
        var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
        var url = new URL(raw, location.href);
        if (method === "POST" && url.host === location.host) {
          var started = Date.now();
          if (url.pathname === "/admin/demo/assistant/chat") {
            result.then(function (r) { try { observeAssistant(r.clone(), started); } catch (e) {} }, function () {});
          } else if (/^\/v1\/agents\/[^/]+\/chat$/.test(url.pathname)) {
            result.then(function (r) { try { observeTestChat(url, r.clone(), started); } catch (e) {} }, function () {});
          }
        }
      } catch (e) { /* the page's own request goes on unchanged */ }
      return result;
    };
  }

  // ---------------------------------------------------------------------
  // Voice calls: the sockets' open and close
  // ---------------------------------------------------------------------
  function voiceReason(code, reason) {
    reason = String(reason || "");
    var keys = ["voice_daily", "voice_busy", "voice_rate", "voice_off", "voice_length"];
    for (var i = 0; i < keys.length; i++) {
      if (MSG[keys[i]] && reason.indexOf(MSG[keys[i]].slice(0, 40)) === 0) return keys[i];
    }
    if (/^Calls in the public demo end after/.test(reason)) return "voice_length";
    if (code === 1000 || code === 1005) return null;
    return code >= 4000 ? "refused" : (code === 1006 ? "dropped" : null);
  }
  if (window.WebSocket) {
    var NativeWS = window.WebSocket;
    var Wrapped = function (url, protocols) {
      var ws = protocols === undefined ? new NativeWS(url) : new NativeWS(url, protocols);
      try {
        var u = new URL(url, location.href);
        var target = /^\/admin\/demo\/assistant\/voice$/.test(u.pathname) ? "assistant"
          : /^\/v1\/agents\/[^/]+\/voice$/.test(u.pathname) ? "agent" : null;
        if (target) {
          var opened = 0;
          ws.addEventListener("open", function () {
            opened = Date.now();
            send("engine_demo_voice_call_started", { target: target });
          });
          ws.addEventListener("close", function (ev) {
            var why = voiceReason(ev.code, ev.reason);
            if (why && why.indexOf("voice_") === 0) limitHit(why, "voice");
            var secs = opened ? (Date.now() - opened) / 1000 : 0;
            send("engine_demo_voice_call_ended", { target: target, connected: !!opened, code: ev.code,
                                                   reason: why || undefined, duration: bucketSeconds(secs),
                                                   seconds: Math.round(secs / 5) * 5 });
          });
        }
      } catch (e) { /* the call goes on */ }
      return ws;
    };
    Wrapped.prototype = NativeWS.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (k) { Wrapped[k] = NativeWS[k]; });
    window.WebSocket = Wrapped;
  }

  // ---------------------------------------------------------------------
  // A test run this tab started: its final state (read from the run's own
  // status, never its id or name).
  // ---------------------------------------------------------------------
  var FINAL = { passed: 1, failed: 1, errored: 1, cancelled: 1 };
  var reportedRuns = {};
  function runFinished(runId, status, via, started) {
    if (reportedRuns[runId]) return;
    reportedRuns[runId] = 1;
    var watched = peek("runs") || {};
    delete watched[runId];
    note("runs", watched);
    send("engine_demo_test_run_finished", { status: shortId(status) || "unknown", via: via,
                                            duration: started ? bucketSeconds((Date.now() - started) / 1000) : undefined });
  }
  function watchRun(runId, via, started) {
    var watched = peek("runs") || {};
    watched[runId] = { via: via, t: started };
    note("runs", watched);
  }
  function pollRuns() {
    var watched = peek("runs") || {};
    Object.keys(watched).forEach(function (id) {
      var w = watched[id];
      if (!w || Date.now() - w.t > 15 * 60 * 1000) { delete watched[id]; note("runs", watched); return; }
      nativeFetchGet("/admin/eval-runs/" + encodeURIComponent(id) + "/status.json").then(function (data) {
        if (data && FINAL[data.status]) runFinished(id, data.status, w.via, w.t);
      }, function () {});
    });
  }
  function nativeFetchGet(u) {
    var f = nativeFetch || window.fetch;
    return f(u, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (r.status === 404) return { status: "gone" }; return r.ok ? r.json() : null; });
  }
  if (page === "portal") {
    setTimeout(pollRuns, 3000);
    setInterval(function () { if (Object.keys(peek("runs") || {}).length) pollRuns(); }, 8000);
  }

  // ---------------------------------------------------------------------
  // The guided tour's events (feat/demo-tour), when it is on the page
  // ---------------------------------------------------------------------
  function list(v, max) {
    return Array.isArray(v) ? v.map(shortId).filter(Boolean).slice(0, max || 30) : undefined;
  }
  var TOUR = {
    "tour:step-done": function (d) { send("engine_demo_tour_step_done", { step: shortId(d.step), via: shortId(d.via) }); },
    "tour:ai-done": function (d) {
      send("engine_demo_tour_ai_done", { tools: list(d.tools), actions: list(d.actions), limited: !!d.limited,
                                         limit: shortId(d.limit || ""), tour_step: shortId(d.tourStep || "") });
    },
    "tour:test-chat-sent": function (d) {
      send("engine_demo_tour_test_chat_sent", { agent: d.profile && SEEDED[d.profile] ? "seeded" : "visitor", limited: !!d.limited });
    },
    "tour:run-started": function () { send("engine_demo_tour_run_started", {}); },
    // The run pages' pollers announce every run they watch (the demo's own
    // runs too) and may announce one twice; the tour re-announces, once, only
    // the runs this browser started (source "tour"). That one is reported.
    "tour:run-finished": function (d) {
      if (d.source !== "tour") return;
      send("engine_demo_tour_run_finished", { status: shortId(d.status) });
    },
    "tour:palette-used": function (d) { send("engine_demo_tour_palette_used", { kind: shortId(String(d.kind || "").toLowerCase()) }); },
    "tour:saved": function (d) { send("engine_demo_tour_saved", { kind: shortId(d.kind) }); },
    "tour:resume": function () { send("engine_demo_tour_resumed", {}); },
    "tour:restart": function () { send("engine_demo_tour_restarted", {}); }
  };
  Object.keys(TOUR).forEach(function (name) {
    window.addEventListener(name, function (e) {
      try {
        TOUR[name]((e && e.detail) || {});
        var now = tourState();
        Object.keys(now).forEach(function (k) { if (now[k] && now[k] !== supers[k]) { supers[k] = now[k]; var one = {}; one[k] = now[k]; ph().register(one); } });
      } catch (err) {}
    });
  });
})();
