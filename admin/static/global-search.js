/**
 * global-search.js — the portal's Ctrl-K search palette.
 *
 * Markup: _base.html (#global-search-trigger in the top bar, the
 * #global-search-dialog overlay at the end of <body>). Server: POST
 * /admin/search.json (admin/search.py) — the query rides in the body so a
 * phone number never lands in a URL or access log.
 *
 *   - Ctrl/Cmd+K (or the top-bar button) opens; Esc or a click outside closes.
 *   - Empty: recent items (this browser only) + quick actions.
 *   - Typing: debounced fetch, grouped rows (icon · title · one-line context ·
 *     type once, right-aligned), matched text highlighted.
 *   - ↑/↓ move, Enter opens, Ctrl/Cmd+Enter opens in a new tab.
 *   - Scope prefixes: ">" actions, "@" people & conversations, "?" help/docs.
 *   - Sources that failed or ran late come back as quiet notes.
 */
(function () {
  "use strict";

  var DEBOUNCE_MS = 160;
  var RECENT_KEY = "waj-admin-search-recent";
  var RECENT_MAX = 6;
  var SCOPE_LABEL = { ">": "Actions", "@": "People", "?": "Help & docs" };
  var IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || "");

  // 16×16 stroke icons (same drawing style as _components/icon.html).
  var P = {
    screen: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><line x1="2" y1="5.5" x2="14" y2="5.5"/>',
    overview: '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>',
    agents: '<rect x="3" y="5" width="10" height="8" rx="2"/><line x1="8" y1="2.5" x2="8" y2="5"/><circle cx="8" cy="1.8" r="0.7"/><line x1="6" y1="8.2" x2="6" y2="9.8"/><line x1="10" y1="8.2" x2="10" y2="9.8"/>',
    conversations: '<path d="M2.5 4a1.5 1.5 0 011.5-1.5h8A1.5 1.5 0 0113.5 4v5a1.5 1.5 0 01-1.5 1.5H7l-3 2.5v-2.5H4A1.5 1.5 0 012.5 9z"/>',
    knowledge: '<path d="M3 12.5V3.5A1.5 1.5 0 014.5 2H13v9H4.5A1.5 1.5 0 003 12.5a1.5 1.5 0 001.5 1.5H13"/>',
    connections: '<path d="M6 1.5v3M10 1.5v3"/><path d="M4 4.5h8v2.5a4 4 0 01-8 0z"/><path d="M8 11v3.5"/>',
    tools: '<path d="M9.8 4.2a.67.67 0 000 .93l1.07 1.07a.67.67 0 00.93 0l2.51-2.51a4 4 0 01-5.29 5.29l-4.61 4.61a1.41 1.41 0 01-2-2l4.61-4.61a4 4 0 015.29-5.29L9.8 4.2z"/>',
    channels: '<circle cx="8" cy="8" r="1"/><path d="M5.2 10.8a4 4 0 010-5.6M10.8 5.2a4 4 0 010 5.6M3.2 12.8a6.8 6.8 0 010-9.6M12.8 3.2a6.8 6.8 0 010 9.6"/>',
    testing: '<path d="M6 1.5h4M7 1.5v4.2L3.3 12a1.4 1.4 0 001.2 2.2h7a1.4 1.4 0 001.2-2.2L9 5.7V1.5"/><polyline points="6,10.3 7.5,11.8 10,9"/>',
    activity: '<circle cx="8" cy="8" r="6"/><polyline points="8,4.5 8,8 10.5,9.5"/>',
    help: '<circle cx="8" cy="8" r="6"/><path d="M6.2 6.2a1.9 1.9 0 013.6.7c0 1.3-1.8 1.6-1.8 2.8"/><circle cx="8" cy="11.6" r="0.4"/>',
    docs: '<path d="M9 1.5H4.5a1 1 0 00-1 1v11a1 1 0 001 1h7a1 1 0 001-1V5z"/><polyline points="9,1.5 9,5 12.5,5"/><line x1="6" y1="8.5" x2="10" y2="8.5"/><line x1="6" y1="11" x2="10" y2="11"/>',
    settings: '<circle cx="8" cy="8" r="2"/><path d="M8 1.5v1.8M8 12.7v1.8M14.5 8h-1.8M3.3 8H1.5M12.6 3.4l-1.3 1.3M4.7 11.3l-1.3 1.3M12.6 12.6l-1.3-1.3M4.7 4.7L3.4 3.4"/>',
    users: '<circle cx="6" cy="5.5" r="2.5"/><path d="M2 14c0-2.2 1.8-4 4-4s4 1.8 4 4"/><path d="M11 3.5a2.5 2.5 0 010 4M14 14c0-2-1.2-3.4-3-3.9"/>',
    person: '<circle cx="8" cy="5.5" r="2.8"/><path d="M2.8 14c0-2.9 2.3-4.8 5.2-4.8s5.2 1.9 5.2 4.8"/>',
    patient: '<circle cx="8" cy="5" r="2.5"/><path d="M3 14c0-2.7 2.2-4.5 5-4.5s5 1.8 5 4.5"/><path d="M11.5 1.8v3M10 3.3h3"/>',
    deploy: '<path d="M9 2.8c2-1.3 4.5-1.3 4.5-.3s0 2.5-1.3 4.5S9 10.5 7.5 11L5 8.5C5.5 7 7 4 9 2.8z"/><path d="M5 8.5l-2.3.8L4.5 11M7.5 11l.8 2.3L10 11.5"/>',
    variables: '<circle cx="5.5" cy="10.5" r="3"/><path d="M7.6 8.4L13.5 2.5M11 5l1.8 1.8M12.5 3.5l1.3 1.3"/>',
    diagnostics: '<polyline points="1.5,8.5 4.5,8.5 6,4.5 8.5,12 10,7 11,8.5 14.5,8.5"/>',
    voice: '<rect x="6" y="1.5" width="4" height="7" rx="2"/><path d="M3.5 7a4.5 4.5 0 009 0"/><line x1="8" y1="11.5" x2="8" y2="14"/>',
    model: '<rect x="4" y="4" width="8" height="8" rx="1.5"/><rect x="6.5" y="6.5" width="3" height="3" rx="0.5"/><path d="M6 1.5v2.5M10 1.5v2.5M6 12v2.5M10 12v2.5M1.5 6h2.5M1.5 10h2.5M12 6h2.5M12 10h2.5"/>',
    action: '<polyline points="4,8 12,8"/><polyline points="8.5,4.5 12,8 8.5,11.5"/>',
    recent: '<circle cx="8" cy="8" r="6"/><polyline points="8,4.5 8,8 10.5,9.5"/>'
  };
  function icon(name) {
    return '<svg class="icon" aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" ' +
      'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
      (P[name] || P.screen) + "</svg>";
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // Wrap every occurrence of every query word in <mark>, escaping the rest.
  function highlight(text, words) {
    text = String(text == null ? "" : text);
    if (!words.length) return esc(text);
    var low = text.toLowerCase(), ranges = [];
    words.forEach(function (w) {
      if (!w) return;
      var i = low.indexOf(w);
      while (i !== -1) {
        // Only mark matches at the start of a word: "ai" must not light up
        // inside "against" or "plain".
        if (i === 0 || !/[a-z0-9؀-ۿ]/i.test(low.charAt(i - 1))) ranges.push([i, i + w.length]);
        i = low.indexOf(w, i + w.length);
      }
    });
    if (!ranges.length) return esc(text);
    ranges.sort(function (a, b) { return a[0] - b[0]; });
    var merged = [ranges[0]];
    for (var k = 1; k < ranges.length; k++) {
      var last = merged[merged.length - 1];
      if (ranges[k][0] <= last[1]) last[1] = Math.max(last[1], ranges[k][1]);
      else merged.push(ranges[k]);
    }
    var out = "", pos = 0;
    merged.forEach(function (r) {
      out += esc(text.slice(pos, r[0])) + "<mark>" + esc(text.slice(r[0], r[1])) + "</mark>";
      pos = r[1];
    });
    return out + esc(text.slice(pos));
  }

  function loadRecent() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]") || []; } catch (e) { return []; }
  }
  function saveRecent(item) {
    // People never go into browser storage; only screens, records, docs.
    if (!item || item.person || !item.href) return;
    try {
      var list = loadRecent().filter(function (r) { return r.href !== item.href; });
      list.unshift({ label: item.label, type: item.type, href: item.href, icon: item.icon, context: item.context || "" });
      localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
    } catch (e) { /* private window / storage blocked: recents are a convenience */ }
  }

  // The public demo's guided tour (demo-tour.js): "Resume tour" / "Restart
  // tour" quick actions carry a "#tour:<name>" href (admin/nav.py
  // search_quick_actions adds them only when the tour is on), and opening any
  // result tells the tour the palette was used. Every event is wrapped so a
  // tour problem can never break search.
  function tourOn() { return !!document.getElementById("demo-tour-config"); }
  function isTourAction(r) { return !!(r && typeof r.href === "string" && r.href.indexOf("#tour:") === 0); }
  function tourEmit(name, detail) {
    try { window.dispatchEvent(new CustomEvent(name, { detail: detail || {} })); } catch (e) { /* optional */ }
  }
  function paletteUsed(item) {
    tourEmit("tour:palette-used", { kind: (item && (item.type || item.group)) || "", href: (item && item.href) || "" });
  }

  function init() {
    var overlay = document.getElementById("global-search-dialog");
    var input = document.getElementById("global-search");
    var panel = document.getElementById("global-search-panel");
    var trigger = document.getElementById("global-search-trigger");
    var spinner = document.getElementById("global-search-spinner");
    var status = document.getElementById("global-search-status");
    var scopeChip = document.getElementById("global-search-scope");
    var closeBtn = document.getElementById("global-search-close");
    if (!overlay || !input || !panel) return;

    var env = input.getAttribute("data-env") || "staging";
    var quick = [];
    try { quick = JSON.parse((document.getElementById("global-search-quick") || {}).textContent || "[]"); } catch (e) {}
    if (!tourOn()) quick = quick.filter(function (r) { return !isTourAction(r); });
    var tourActions = quick.filter(isTourAction);
    var timer = null, reqId = 0, items = [], rowEls = [], active = -1, lastFocus = null;
    var openOnLoad = null;  // Enter pressed before results arrived: {newTab}

    if (!IS_MAC) {
      document.querySelectorAll(".gs-mod").forEach(function (k) { k.textContent = "Ctrl"; });
    } else {
      document.querySelectorAll(".gs-mod").forEach(function (k) { k.textContent = "⌘"; });
      var tk = document.querySelector(".gs-trigger .gs-kbd");
      if (tk) tk.textContent = "⌘K";
    }

    function open(prefill) {
      if (!overlay.hidden) { input.focus(); return; }
      lastFocus = document.activeElement;
      overlay.hidden = false;
      document.documentElement.classList.add("gs-open");
      if (typeof prefill === "string") input.value = prefill;
      input.focus();
      input.select();
      onInput();
    }
    function close() {
      overlay.hidden = true;
      document.documentElement.classList.remove("gs-open");
      if (timer) clearTimeout(timer);
      reqId++;
      setLoading(false);
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }
    function setLoading(on) {
      if (spinner) spinner.hidden = !on;
      panel.setAttribute("aria-busy", on ? "true" : "false");
      panel.classList.toggle("is-loading", !!on);
    }
    function setActive(i, scroll) {
      if (rowEls[active]) rowEls[active].classList.remove("active");
      active = i;
      var el = rowEls[active];
      if (el) {
        el.classList.add("active");
        input.setAttribute("aria-activedescendant", el.id);
        if (scroll !== false) el.scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }
    function go(i, newTab) {
      var item = items[i];
      if (!item) return;
      paletteUsed(item);
      if (isTourAction(item)) {
        close();
        tourEmit("tour:" + item.href.slice("#tour:".length), {});
        return;
      }
      saveRecent(item);
      if (newTab) { window.open(item.href, "_blank", "noopener"); return; }
      close();
      window.location.href = item.href;
    }

    function parse(raw) {
      var q = raw.trim(), scope = null;
      if (q && SCOPE_LABEL[q.charAt(0)]) { scope = q.charAt(0); q = q.slice(1).trim(); }
      return { scope: scope, q: q, words: q.toLowerCase().split(/\s+/).filter(Boolean) };
    }

    function rowHtml(r, idx, words) {
      var ctx = r.context || "";
      return '<span class="gs-ico">' + icon(r.icon) + "</span>" +
        '<span class="gs-main"><span class="gs-title">' + highlight(r.label, words) + "</span>" +
        (ctx || r.tech ? '<span class="gs-ctx">' + highlight(ctx, words) +
          (r.tech ? (ctx ? " " : "") + '<code class="gs-tech">' + highlight(r.tech, words) + "</code>" : "") +
          "</span>" : "") +
        "</span>" +
        '<span class="gs-type">' + esc(r.type) + "</span>";
    }

    // groups: [{name, rows:[…]}]
    function render(groups, words, extraHtml) {
      panel.innerHTML = "";
      items = []; rowEls = []; active = -1;
      groups.forEach(function (g) {
        if (!g.rows.length) return;
        var head = document.createElement("div");
        head.className = "gs-group";
        head.setAttribute("role", "presentation");
        head.textContent = g.name;
        panel.appendChild(head);
        g.rows.forEach(function (r) {
          var idx = items.length;
          var a = document.createElement("a");
          a.className = "gs-row";
          a.id = "gs-opt-" + idx;
          a.href = r.href;
          a.setAttribute("role", "option");
          a.innerHTML = rowHtml(r, idx, words);
          a.addEventListener("mousemove", function () { if (active !== idx) setActive(idx, false); });
          a.addEventListener("click", function (e) {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) {
              if (isTourAction(r)) { e.preventDefault(); go(idx, false); return; }
              paletteUsed(r); saveRecent(r); return;
            }
            e.preventDefault();
            go(idx, false);
          });
          panel.appendChild(a);
          items.push(r); rowEls.push(a);
        });
      });
      if (extraHtml) {
        var x = document.createElement("div");
        x.innerHTML = extraHtml;
        while (x.firstChild) panel.appendChild(x.firstChild);
      }
      if (rowEls.length) setActive(0, false);
      panel.scrollTop = 0;
    }

    function renderHome() {
      var recent = loadRecent().map(function (r) {
        r.href = String(r.href || "").replace(/([?&]env=)(staging|prod)\b/, "$1" + env);
        r.icon = r.icon || "recent";
        return r;
      });
      render([{ name: "Recent", rows: recent }, { name: "Quick actions", rows: quick }], [],
        recent.length ? "" : '<p class="gs-hint">Pages you open from search will show up here.</p>');
      status.textContent = "";
    }

    function emptyHtml(p) {
      var s = '<div class="gs-empty"><p class="gs-empty-title">No matches for “' + esc(p.q) + '”</p>';
      if (p.scope === "@") {
        s += '<p>Try a name, a phone number with its country code, an email, or a conversation id.</p>';
      } else if (p.scope === "?") {
        s += '<p>Try a single word, like “publish” or “voice”.</p>';
      } else {
        s += '<p>' + (p.words.length > 1 ? "Try fewer words, or " : "Try ") + 'searching only:</p>' +
          '<p class="gs-empty-actions">' +
          '<button type="button" data-rescope="@">People &amp; conversations</button>' +
          '<button type="button" data-rescope="?">Help &amp; docs</button></p>';
      }
      return s + "</div>";
    }

    function fetchResults(raw) {
      var p = parse(raw);
      var my = ++reqId;
      setLoading(true);
      if (!rowEls.length) {
        panel.innerHTML = '<div class="gs-loading"><span></span><span></span><span></span></div>';
      }
      fetch("/admin/search.json", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ q: raw, env: env })
      })
        .then(function (resp) { if (!resp.ok) throw new Error("HTTP " + resp.status); return resp.json(); })
        .then(function (data) {
          if (my !== reqId) return;
          setLoading(false);
          var groups = [], byName = {};
          // The tour's own actions answer "> tour" (and a plain "tour") too.
          var tourHits = (p.scope === ">" || !p.scope) ? tourActions.filter(function (r) {
            var hay = (r.label + " " + (r.context || "")).toLowerCase();
            return p.words.length ? p.words.every(function (w) { return hay.indexOf(w) !== -1; }) : p.scope === ">";
          }) : [];
          if (tourHits.length) { byName.Tour = { name: "Tour", rows: tourHits.slice() }; groups.push(byName.Tour); }
          (data.results || []).forEach(function (r) {
            var g = r.group || "Results";
            if (!byName[g]) { byName[g] = { name: g, rows: [] }; groups.push(byName[g]); }
            byName[g].rows.push(r);
          });
          var notes = (data.notes || []).map(function (n) { return n.message; });
          var notesHtml = notes.length
            ? '<p class="gs-note">' + esc("Not searched this time: " + notes.join(" ")) + "</p>" : "";
          if (!groups.length) {
            if (p.scope && !p.q) {
              render([], [], '<p class="gs-hint">' + (p.scope === "@"
                ? "Type a name, phone number, email or conversation id."
                : "Type to search.") + "</p>");
            } else {
              render([], [], emptyHtml(p) + notesHtml);
            }
            status.textContent = p.q ? "No results" : "";
            return;
          }
          render(groups, p.words, notesHtml);
          status.textContent = items.length + " result" + (items.length === 1 ? "" : "s");
          if (openOnLoad) { var nt = openOnLoad.newTab; openOnLoad = null; go(0, nt); }
        })
        .catch(function () {
          if (my !== reqId) return;
          setLoading(false);
          render([], [], '<div class="gs-empty"><p class="gs-empty-title">Search isn’t available right now</p>' +
            "<p>Check your connection and try again.</p></div>");
          status.textContent = "Search failed";
        });
    }

    function onInput() {
      openOnLoad = null;
      var raw = input.value;
      var p = parse(raw);
      if (scopeChip) {
        scopeChip.hidden = !p.scope;
        scopeChip.textContent = p.scope ? SCOPE_LABEL[p.scope] : "";
      }
      if (timer) clearTimeout(timer);
      if (!raw.trim()) { reqId++; setLoading(false); renderHome(); return; }
      timer = setTimeout(function () { fetchResults(raw.trim()); }, DEBOUNCE_MS);
    }

    input.addEventListener("input", onInput);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); if (rowEls.length) setActive((active + 1) % rowEls.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); if (rowEls.length) setActive((active - 1 + rowEls.length) % rowEls.length); return; }
      if (e.key === "Home" && rowEls.length && e.ctrlKey) { e.preventDefault(); setActive(0); return; }
      if (e.key === "End" && rowEls.length && e.ctrlKey) { e.preventDefault(); setActive(rowEls.length - 1); return; }
      if (e.key === "Enter") {
        e.preventDefault();
        if (timer && input.value.trim()) {
          // A query still debouncing: fetch now, open the top hit when it lands.
          clearTimeout(timer); timer = null;
          openOnLoad = { newTab: e.metaKey || e.ctrlKey };
          fetchResults(input.value.trim());
          return;
        }
        if (active >= 0) go(active, e.metaKey || e.ctrlKey);
        return;
      }
      if (e.key === "Tab") { e.preventDefault(); }  // focus stays in the palette
    });

    panel.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest("[data-rescope]");
      if (!b) return;
      var p = parse(input.value);
      input.value = b.getAttribute("data-rescope") + p.q;
      input.focus();
      onInput();
    });
    overlay.addEventListener("mousedown", function (e) { if (e.target === overlay) close(); });
    if (closeBtn) closeBtn.addEventListener("click", close);
    overlay.querySelectorAll("[data-scope]").forEach(function (b) {
      b.addEventListener("click", function () {
        var p = parse(input.value);
        input.value = b.getAttribute("data-scope") + p.q;
        input.focus();
        onInput();
      });
    });
    if (trigger) trigger.addEventListener("click", function () { open(); });

    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && (e.key || "").toLowerCase() === "k") {
        e.preventDefault();
        if (overlay.hidden) open(); else close();
      }
    });
    window.WajSearch = { open: open, close: close };
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
