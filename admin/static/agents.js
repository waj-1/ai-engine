/* admin/static/agents.js — behaviour for the Agents area (B1 redesign).
 *
 *  - Agents list: search + segmented filters + nested sub-agent rows.
 *  - Agent detail: one Save per tab. Every <form data-tab-form> inside
 *    [data-tab-root] is snapshotted on load; the sticky save bar enables
 *    when any differs, and Save POSTs each CHANGED form to its own (unchanged)
 *    endpoint, in order, then reloads this tab. A refusal (HTTP 4xx JSON
 *    detail, or a redirect carrying ?error=) stops the run and is shown.
 *  - Pickers: "+ Add …" comboboxes over JSON catalogs embedded in the page,
 *    attached-row removal, and the per-toolset action-picker modal.
 *  - Action forms (pin / restore / checkpoint / run test / promote): posted
 *    in place and returned to the same tab.
 *  - Header: agent switcher search, dialogs, copy-ID, Test → test-chat drawer,
 *    legacy #anchors → the tab that now holds that section.
 */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent || "null"); } catch (e) { return null; }
  }

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "class") n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function hidden(name, value, extra) {
    var i = el("input", { type: "hidden", name: name });
    i.value = value;
    Object.keys(extra || {}).forEach(function (k) { i.setAttribute(k, extra[k]); });
    return i;
  }

  function currentTab() {
    var t = $("[data-agent-tab]");
    return t ? t.getAttribute("data-agent-tab") : "";
  }

  // Navigate back to this page/tab, carrying the markers a handler put on its
  // redirect (restore's dropped_* / edited_from, an ?error=).
  function returnTo(finalUrl, extra) {
    var target = new URL(window.location.href);
    ["saved", "error", "edited_from", "dropped_tools", "dropped_kb", "dropped_mcp",
     "dropped_model", "dropped_prompts", "test"].forEach(function (k) { target.searchParams.delete(k); });
    if (finalUrl) {
      try {
        var f = new URL(finalUrl, window.location.origin);
        f.searchParams.forEach(function (v, k) {
          if (k === "error" || k === "edited_from" || k.indexOf("dropped_") === 0) target.searchParams.set(k, v);
        });
      } catch (e) { /* ignore */ }
    }
    var tab = currentTab();
    if (tab && tab !== "overview" && target.pathname === "/admin/agents") target.searchParams.set("tab", tab);
    Object.keys(extra || {}).forEach(function (k) { target.searchParams.set(k, extra[k]); });
    target.hash = "";
    window.location.assign(target.toString());
  }

  // POST a form; resolve {ok, url, error}.
  function postForm(form) {
    return fetch(form.getAttribute("action"), {
      method: "POST", body: new FormData(form), credentials: "same-origin",
      headers: { "Accept": "application/json, text/html;q=0.9" },
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (txt) {
          var msg = txt;
          try {
            var j = JSON.parse(txt);
            msg = j.detail || (j.error && (j.error.message || j.error.detail)) || txt;
          } catch (e) { /* not JSON */ }
          if (typeof msg !== "string") msg = JSON.stringify(msg);
          if (msg.length > 600) msg = msg.slice(0, 600) + "…";
          return { ok: false, status: res.status, error: msg || ("Request failed (" + res.status + ")") };
        });
      }
      var err = null;
      try { err = new URL(res.url).searchParams.get("error"); } catch (e) { /* ignore */ }
      if (err) return { ok: false, status: res.status, error: err, url: res.url };
      return { ok: true, url: res.url };
    }).catch(function (e) {
      return { ok: false, error: "Network error — " + (e && e.message ? e.message : "request failed") };
    });
  }

  // ------------------------------------------------------------------ dialogs
  function openDialog(id, opener) {
    var d = document.getElementById(id);
    if (!d) return;
    if (opener && opener.getAttribute("data-compare-a")) {
      var s = $("[data-compare-a-select]", d);
      if (s) s.value = opener.getAttribute("data-compare-a");
    }
    $$("details.menu[open]").forEach(function (m) { m.removeAttribute("open"); });
    if (typeof d.showModal === "function") d.showModal(); else d.setAttribute("open", "");
  }
  document.addEventListener("click", function (e) {
    var o = e.target.closest("[data-open-dialog]");
    if (o) { e.preventDefault(); openDialog(o.getAttribute("data-open-dialog"), o); return; }
    var c = e.target.closest("[data-close-dialog]");
    if (c) { var d = c.closest("dialog"); if (d) d.close(); return; }
    var cp = e.target.closest("[data-copy]");
    if (cp) {
      var v = cp.getAttribute("data-copy");
      (navigator.clipboard ? navigator.clipboard.writeText(v) : Promise.reject()).then(function () {
        cp.textContent = "Copied"; setTimeout(function () { cp.textContent = "Copy agent ID"; }, 1400);
      }, function () { window.prompt("Agent ID", v); });
      return;
    }
    // Close open menus on outside click.
    $$("details.menu[open]").forEach(function (m) { if (!m.contains(e.target)) m.removeAttribute("open"); });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") $$("details.menu[open]").forEach(function (m) { m.removeAttribute("open"); });
  });

  // -------------------------------------------------------------- test drawer
  function openTestChat() {
    var l = document.querySelector(".waj-tc-launcher");
    if (l) { l.click(); return true; }
    return false;
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-test-chat-open]")) { e.preventDefault(); openTestChat(); }
  });
  if (new URLSearchParams(window.location.search).get("test") === "1") {
    var tries = 0;
    (function waitLauncher() {
      if (openTestChat() || ++tries > 40) return;
      setTimeout(waitLauncher, 100);
    })();
  }

  // ----------------------------------------------------------- legacy anchors
  (function () {
    var map = readJSON("agent-legacy-anchors");
    var h = (window.location.hash || "").replace(/^#/, "");
    if (!map || !h || !map[h]) return;
    if (document.getElementById(h)) return; // the section is on this tab
    var t = map[h];
    var u = new URL(window.location.href);
    if (t === "voice") { u.pathname = "/admin/agents/" + u.searchParams.get("profile_id") + "/voice"; u.searchParams.delete("profile_id"); }
    else u.searchParams.set("tab", t);
    u.hash = "";
    window.location.replace(u.toString());
  })();

  // --------------------------------------------------------------- switcher
  $$("[data-switcher]").forEach(function (sw) {
    var input = $("[data-switcher-search]", sw);
    sw.addEventListener("toggle", function () { if (sw.open && input) setTimeout(function () { input.focus(); }, 20); });
    if (input) input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      $$(".switcher-item", sw).forEach(function (a) {
        a.hidden = q && (a.getAttribute("data-search") || "").indexOf(q) === -1;
      });
    });
  });

  // ------------------------------------------------------------- agents list
  (function () {
    var table = $("[data-agents-table]");
    if (!table) return;
    var state = { q: "", type: "live", status: "", channel: "", category: "" };
    var rows = $$("[data-agent-row]", table);
    var collapsed = {};

    function rowMatches(r, viaFamily) {
      var type = r.getAttribute("data-type");
      if (state.type === "template" && type !== "template") return false;
      if (state.type !== "template" && type === "template") return false;
      if (state.type === "main" && type === "sub") return false;
      if (state.status && r.getAttribute("data-status") !== state.status) return false;
      if (state.channel && (" " + r.getAttribute("data-channels") + " ").indexOf(" " + state.channel + " ") === -1) return false;
      // Category: "__none__" is the Uncategorised choice.
      if (state.category) {
        var cat = r.getAttribute("data-category") || "";
        if (state.category === "__none__" ? cat !== "" : cat !== state.category) return false;
      }
      if (state.q && (r.getAttribute("data-search") || "").indexOf(state.q) === -1 && !viaFamily) return false;
      return true;
    }

    function apply() {
      var shown = 0;
      var parentsShown = {};
      rows.forEach(function (r) {
        if (r.getAttribute("data-parent")) return;
        var ok = rowMatches(r, false);
        r.hidden = !ok;
        if (ok) { parentsShown[r.getAttribute("data-id")] = true; shown++; }
      });
      var parentSelfMatch = {};
      rows.forEach(function (r) {
        if (r.getAttribute("data-parent")) return;
        parentSelfMatch[r.getAttribute("data-id")] = !state.q ||
          (r.getAttribute("data-self-search") || "").indexOf(state.q) !== -1;
      });
      rows.forEach(function (r) {
        var p = r.getAttribute("data-parent");
        if (!p) return;
        // A search that matched only a child shows that child, not its siblings.
        var ok = !!parentsShown[p] && !collapsed[p] && rowMatches(r, parentSelfMatch[p]);
        if (!parentsShown[p] && state.type !== "main" && rowMatches(r, false)) { ok = true; }
        r.hidden = !ok;
        if (ok) shown++;
      });
      var empty = $("[data-empty-row]", table);
      if (empty) empty.hidden = shown > 0;
      var counter = $("[data-agents-count]");
      if (counter) counter.textContent = shown + (shown === 1 ? " agent shown" : " agents shown");
    }

    var search = $("[data-filter-search]");
    if (search) search.addEventListener("input", function () { state.q = search.value.trim().toLowerCase(); apply(); });
    $$("[data-filter]").forEach(function (group) {
      var key = group.getAttribute("data-filter");
      group.addEventListener("click", function (e) {
        var b = e.target.closest("button[data-value]");
        if (!b) return;
        $$("button", group).forEach(function (x) { x.classList.remove("active"); x.setAttribute("aria-pressed", "false"); });
        b.classList.add("active"); b.setAttribute("aria-pressed", "true");
        state[key] = b.getAttribute("data-value");
        apply();
      });
    });
    $$("[data-filter-select]").forEach(function (s) {
      // A select can arrive preselected (?category=<slug> from the Categories screen).
      if (s.value) state[s.getAttribute("data-filter-select")] = s.value;
      s.addEventListener("change", function () { state[s.getAttribute("data-filter-select")] = s.value; apply(); });
    });
    document.addEventListener("click", function (e) {
      var t = e.target.closest("[data-toggle-children]");
      if (t) {
        var id = t.getAttribute("data-toggle-children");
        collapsed[id] = !collapsed[id];
        t.setAttribute("aria-expanded", collapsed[id] ? "false" : "true");
        t.textContent = collapsed[id] ? "▸" : "▾";
        apply();
      }
      if (e.target.closest("[data-clear-filters]")) {
        state = { q: "", type: "live", status: "", channel: "", category: "" };
        if (search) search.value = "";
        $$("[data-filter]").forEach(function (g) {
          $$("button", g).forEach(function (x, i) { var on = i === 0; x.classList.toggle("active", on); x.setAttribute("aria-pressed", on ? "true" : "false"); });
        });
        $$("[data-filter-select]").forEach(function (s) { s.value = ""; });
        apply();
      }
    });
    apply();
  })();

  // ------------------------------------------------------- tab dirty tracking
  var root = $("[data-tab-root]");
  var forms = root ? $$("form[data-tab-form]", root) : [];
  var savebar = root ? $("[data-savebar]", root) : null;

  function serialize(form) {
    var parts = [];
    new FormData(form).forEach(function (v, k) { parts.push(k + "=" + (typeof v === "string" ? v : "")); });
    parts.sort();
    return parts.join("&");
  }
  forms.forEach(function (f) { f.__snapshot = serialize(f); });
  function dirtyForms() { return forms.filter(function (f) { return serialize(f) !== f.__snapshot; }); }
  function setError(msg) {
    if (!savebar) return;
    var box = $("[data-savebar-error]", savebar);
    if (!box) return;
    box.textContent = msg || "";
    box.hidden = !msg;
  }
  function refreshDirty() {
    if (!savebar) return;
    var n = dirtyForms().length;
    savebar.classList.toggle("is-dirty", n > 0);
    $("[data-savebar-state]", savebar).textContent = n ? "Unsaved changes" : "No unsaved changes";
    $("[data-save]", savebar).disabled = n === 0;
    $("[data-discard]", savebar).disabled = n === 0;
  }
  if (root) {
    root.addEventListener("input", refreshDirty);
    root.addEventListener("change", refreshDirty);
    // Enter in a text field must not submit a single form natively.
    forms.forEach(function (f) {
      f.addEventListener("submit", function (e) { e.preventDefault(); if (dirtyForms().length) saveAll(); });
    });
  }
  window.addEventListener("beforeunload", function (e) {
    if (savebar && dirtyForms().length && !window.__agentsSaving) { e.preventDefault(); e.returnValue = ""; }
  });

  function saveAll() {
    var queue = dirtyForms();
    if (!queue.length) return;
    var btn = $("[data-save]", savebar);
    btn.disabled = true; btn.textContent = "Saving…";
    setError("");
    window.__agentsSaving = true;
    var lastUrl = null;
    (function next(i) {
      if (i >= queue.length) { returnTo(lastUrl, { saved: "1" }); return; }
      var form = queue[i];
      postForm(form).then(function (r) {
        if (!r.ok) {
          window.__agentsSaving = false;
          var label = form.getAttribute("data-form-label") || "This section";
          var done = i ? " (" + i + " other section" + (i === 1 ? " was" : "s were") + " saved)" : "";
          setError(label + " was not saved: " + r.error + done);
          // A safety-check refusal: offer the override on THAT form only.
          if (/validation/i.test(r.error || "")) {
            var box = $("[data-override]", form);
            if (box) { box.hidden = false; box.scrollIntoView({ block: "center", behavior: "smooth" }); }
          }
          btn.textContent = btn.getAttribute("data-label") || "Save";
          refreshDirty();
          if (i > 0) { // earlier forms were written — make that visible
            queue.slice(0, i).forEach(function (f) { f.__snapshot = serialize(f); });
            refreshDirty();
          }
          return;
        }
        lastUrl = r.url;
        next(i + 1);
      });
    })(0);
  }
  if (savebar && window.ResizeObserver) {
    // The floating "Test agent" launcher sits just above the bar, whatever
    // its height (an error line makes it taller).
    new ResizeObserver(function () {
      document.documentElement.style.setProperty("--savebar-h", savebar.offsetHeight + "px");
    }).observe(savebar);
  }
  if (savebar) {
    var sb = $("[data-save]", savebar);
    sb.setAttribute("data-label", sb.textContent);
    sb.addEventListener("click", saveAll);
    $("[data-discard]", savebar).addEventListener("click", function () {
      window.__agentsSaving = true; window.location.reload();
    });
    refreshDirty();
  }

  // ----------------------------------------------------------- action forms
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form[data-ajax-action]");
    if (!form) return;
    e.preventDefault();
    var msg = form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) return;
    var btn = form.querySelector("button[type=submit]");
    if (btn) { btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
    var errBox = form.querySelector("[data-form-error]");
    postForm(form).then(function (r) {
      if (!r.ok && errBox) {
        errBox.textContent = r.error; errBox.hidden = false;
        if (btn) { btn.disabled = false; btn.removeAttribute("aria-busy"); }
        return;
      }
      window.__agentsSaving = true;
      if (!r.ok) { returnTo(null, { error: r.error }); return; }
      returnTo(r.url, { saved: "1" });
    });
  });

  // -------------------------------------------------------------- comboboxes
  function attachedIds(kind) {
    var list = $("[data-attached-list='" + kind + "']");
    if (!list) return [];
    return $$("[data-row]", list).map(function (r) { return r.getAttribute("data-id") || r.getAttribute("data-mcp-id"); });
  }
  function updateEmpty(list) {
    var e = $("[data-empty]", list);
    if (e) e.hidden = $$("[data-row]", list).length > 0;
  }

  var catalogs = {};
  function catalog(id) { if (!(id in catalogs)) catalogs[id] = readJSON(id) || []; return catalogs[id]; }

  function removeButton(label) {
    return el("div", { class: "row attached-actions" }, [
      el("button", { type: "button", class: "btn ghost small", "data-remove-row": "", "aria-label": "Remove " + label, text: "Remove" }),
    ]);
  }

  var builders = {
    toolset: function (item) {
      var row = el("div", { class: "attached-row is-new", "data-row": "", "data-mcp-id": item.id, "data-mode": item.mode });
      row.appendChild(hidden("attach_mcp_ids", item.id));
      if (item.mode === "list") {
        row.appendChild(hidden("tool_subset_mode__" + item.id, "list"));
        row.appendChild(hidden("tool_subset_all__" + item.id, "1", { "data-subset-all": "" }));
      } else {
        row.appendChild(hidden("tool_subset__" + item.id, "", { "data-subset-text": "" }));
      }
      var chips = el("div", { class: "chip-row mt-xs", "data-chips": "" });
      (item.actions || []).forEach(function (a) { chips.appendChild(el("span", { class: "chip chip-neutral", title: a.name, text: a.label })); });
      var det = el("details", { class: "attached-main" }, [
        el("summary", {}, [
          el("span", { class: "attached-title", text: item.label }),
          el("span", { class: "chip chip-neutral", text: item.source }),
          el("span", { class: "caption", "data-count-label": "", text: item.count ? "All " + item.count + " actions" : "All actions (listed when it connects)" }),
          el("span", { class: "technical", text: item.tech || "" }),
        ]),
        el("p", { class: "caption mt-xs", text: item.description || "" }),
        chips,
      ]);
      row.appendChild(det);
      var actions = removeButton(item.label);
      actions.insertBefore(el("button", { type: "button", class: "btn small", "data-pick-actions": item.id, text: "Choose actions" }), actions.firstChild);
      row.appendChild(actions);
      return row;
    },
    knowledge: function (item) {
      var row = el("div", { class: "attached-row is-new", "data-row": "", "data-id": item.id });
      row.appendChild(hidden("data_source_ids", item.id));
      row.appendChild(el("div", { class: "attached-main" }, [
        el("span", { class: "attached-title", text: item.label }),
        el("span", { class: "caption", text: (item.kind || "") + " · refreshes " + (item.refresh || "manual") }),
        item.tech && item.tech !== item.label ? el("span", { class: "technical", text: item.tech }) : null,
      ]));
      row.appendChild(removeButton(item.label));
      return row;
    },
    skill: function (item) {
      var row = el("div", { class: "attached-row is-new", "data-row": "", "data-id": item.id });
      row.appendChild(hidden("attach_skill_ids", item.id));
      row.appendChild(el("div", { class: "attached-main" }, [
        el("span", { class: "attached-title", text: item.label }),
        el("span", { class: "chip " + (item.always_on ? "chip-info" : "chip-neutral"), text: item.always_on ? "Always on" : "When needed" }),
        el("span", { class: "caption", text: (item.description || "").slice(0, 120) }),
      ]));
      row.appendChild(removeButton(item.label));
      return row;
    },
    subagent: function (item) {
      var row = el("div", { class: "attached-row is-new", "data-row": "", "data-id": item.id });
      row.appendChild(hidden("downstream_profile_ids", item.id));
      var main = el("div", { class: "attached-main" }, [
        el("span", { class: "attached-title", text: item.label }),
        el("span", { class: "chip chip-" + ((item.status || {}).variant || "neutral"), text: (item.status || {}).label || "" }),
      ]);
      if (item.delegatable) main.appendChild(el("span", { class: "chip chip-info", text: "Specialist" }));
      main.appendChild(el("p", { class: "caption attached-desc", text: item.description || "No description." }));
      row.appendChild(main);
      row.appendChild(removeButton(item.label));
      return row;
    },
  };

  function optionMeta(kind, item) {
    if (kind === "toolset") return (item.count ? item.count + " actions · " : "Actions listed when it connects · ") + item.source + " · " + (item.tech || "");
    if (kind === "knowledge") return (item.kind || "") + (item.suggested ? " · suggested for this agent" : "") + (item.active ? "" : " · inactive");
    if (kind === "skill") return (item.always_on ? "Always on · " : "") + (item.description || "");
    if (kind === "subagent") return (item.delegatable ? "Specialist · " : "") + (item.description || "No description");
    return "";
  }

  $$("[data-combobox]").forEach(function (cb) {
    var kind = cb.getAttribute("data-combobox");
    var items = catalog(cb.getAttribute("data-catalog"));
    var openBtn = $("[data-combobox-open]", cb);
    var panel = $(".combobox-panel", cb);
    var input = $("[data-combobox-input]", cb);
    var list = $("[data-combobox-list]", cb);
    var active = -1;

    function render() {
      var q = (input.value || "").trim().toLowerCase();
      var taken = attachedIds(kind);
      list.innerHTML = "";
      var visible = items.filter(function (it) {
        if (taken.indexOf(it.id) !== -1) return false;
        if (!q) return true;
        return ((it.label || "") + " " + (it.tech || "") + " " + (it.description || "")).toLowerCase().indexOf(q) !== -1;
      });
      if (kind === "knowledge") visible.sort(function (a, b) { return (b.suggested ? 1 : 0) - (a.suggested ? 1 : 0); });
      if (!visible.length) {
        list.appendChild(el("div", { class: "combobox-empty caption", text: items.length ? "Nothing else to add." : "Nothing available." }));
      }
      visible.forEach(function (it, i) {
        var opt = el("button", { type: "button", class: "combobox-option", role: "option", "data-index": String(i) }, [
          el("span", { class: "combobox-option-title", text: it.label }),
          el("span", { class: "caption combobox-option-meta", text: optionMeta(kind, it) }),
        ]);
        opt.addEventListener("click", function () { pick(it); });
        list.appendChild(opt);
      });
      active = visible.length ? 0 : -1;
      highlight();
    }
    function highlight() {
      $$(".combobox-option", list).forEach(function (o, i) { o.classList.toggle("is-active", i === active); });
    }
    function open() { panel.hidden = false; openBtn.setAttribute("aria-expanded", "true"); input.value = ""; render(); input.focus(); }
    function close() { panel.hidden = true; openBtn.setAttribute("aria-expanded", "false"); }
    function pick(it) {
      var target = $("[data-attached-list='" + kind + "']");
      var row = builders[kind](it);
      var empty = $("[data-empty]", target);
      target.insertBefore(row, empty || null);
      updateEmpty(target);
      close();
      root && root.dispatchEvent(new Event("change", { bubbles: true }));
      if (kind === "toolset" && it.mode === "list" && it.count > 12) {
        // Big toolsets: start the operator on choosing actions.
        setTimeout(function () { openActionPicker(it.id); }, 50);
      }
    }
    openBtn.addEventListener("click", function () { if (panel.hidden) open(); else close(); });
    input.addEventListener("input", render);
    input.addEventListener("keydown", function (e) {
      var opts = $$(".combobox-option", list);
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(opts.length - 1, active + 1); highlight(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); highlight(); }
      else if (e.key === "Enter") { e.preventDefault(); if (opts[active]) opts[active].click(); }
      else if (e.key === "Escape") { close(); openBtn.focus(); }
    });
    document.addEventListener("click", function (e) { if (!cb.contains(e.target)) close(); });
  });

  document.addEventListener("click", function (e) {
    var rm = e.target.closest("[data-remove-row]");
    if (!rm) return;
    var row = rm.closest("[data-row]");
    var list = row && row.parentNode;
    if (!row) return;
    row.remove();
    if (list) updateEmpty(list);
    root && root.dispatchEvent(new Event("change", { bubbles: true }));
  });

  // ------------------------------------------------------- action picker modal
  var dlg = document.getElementById("dlg-actions");
  var pickerRow = null;
  var GROUPS = ["Read data", "Make changes", "Other"];

  function toolsetItem(id) {
    var cat = catalog("catalog-toolsets");
    for (var i = 0; i < cat.length; i++) if (cat[i].id === id) return cat[i];
    return null;
  }
  function rowSubset(row) {
    if (row.getAttribute("data-mode") === "list") {
      if ($("[data-subset-all]", row)) return null;
      var vals = $$("[data-subset-item]", row).map(function (i) { return i.value; });
      return vals.length ? vals : null;
    }
    var t = $("[data-subset-text]", row);
    var raw = t ? t.value.trim() : "";
    return raw ? raw.split(",").map(function (s) { return s.trim(); }).filter(Boolean) : null;
  }

  function openActionPicker(id) {
    if (!dlg) return;
    var row = $("[data-attached-list='toolset'] [data-mcp-id='" + id + "']");
    var item = toolsetItem(id);
    if (!row || !item) return;
    pickerRow = row;
    var subset = rowSubset(row);
    $("[data-actions-title]", dlg).textContent = "Choose actions — " + item.label;
    $("[data-actions-desc]", dlg).textContent = item.description || "";
    var allBox = $("[data-actions-all]", dlg);
    allBox.checked = !subset;
    var groups = $("[data-actions-groups]", dlg);
    groups.innerHTML = "";
    var hasList = (item.actions || []).length > 0;
    $("[data-actions-picker]", dlg).hidden = !hasList;
    $("[data-actions-text]", dlg).hidden = hasList;
    $("[data-actions-text-input]", dlg).value = subset ? subset.join(", ") : "";
    var chosen = subset || (item.actions || []).map(function (a) { return a.name; });
    var byGroup = {};
    (item.actions || []).forEach(function (a) { (byGroup[a.group] = byGroup[a.group] || []).push(a); });
    GROUPS.forEach(function (g) {
      var acts = byGroup[g];
      if (!acts || !acts.length) return;
      acts.sort(function (a, b) {
        var sa = chosen.indexOf(a.name) !== -1 ? 0 : 1, sb2 = chosen.indexOf(b.name) !== -1 ? 0 : 1;
        return sa - sb2 || a.label.localeCompare(b.label);
      });
      var box = el("fieldset", { class: "actions-group" }, [el("legend", { text: g + " (" + acts.length + ")" })]);
      var grid = el("div", { class: "actions-grid" });
      acts.forEach(function (a) {
        var cbx = el("input", { type: "checkbox", value: a.name });
        cbx.checked = chosen.indexOf(a.name) !== -1;
        grid.appendChild(el("label", { class: "action-item", "data-search": (a.label + " " + a.name + " " + (a.description || "")).toLowerCase(), title: a.description || a.name }, [
          cbx, el("span", {}, [el("span", { text: a.label }), el("span", { class: "technical", text: " " + a.name })]),
        ]));
      });
      box.appendChild(grid);
      groups.appendChild(box);
    });
    $("[data-actions-search]", dlg).value = "";
    syncAllState();
    dlg.showModal();
  }

  function syncAllState() {
    if (!dlg) return;
    var all = $("[data-actions-all]", dlg).checked;
    $("[data-actions-groups]", dlg).classList.toggle("is-disabled", all);
    $$("[data-actions-groups] input[type=checkbox]", dlg).forEach(function (c) { c.disabled = all; });
    var n = $$("[data-actions-groups] input:checked", dlg).length;
    var total = $$("[data-actions-groups] input", dlg).length;
    $("[data-actions-count]", dlg).textContent = all ? "All " + total + " included" : n + " of " + total + " selected";
  }

  if (dlg) {
    dlg.addEventListener("change", syncAllState);
    $("[data-actions-search]", dlg).addEventListener("input", function (e) {
      var q = e.target.value.trim().toLowerCase();
      $$(".action-item", dlg).forEach(function (l) { l.hidden = q && l.getAttribute("data-search").indexOf(q) === -1; });
    });
    $$("[data-actions-select]", dlg).forEach(function (b) {
      b.addEventListener("click", function () {
        var on = b.getAttribute("data-actions-select") === "all";
        $("[data-actions-all]", dlg).checked = false;
        $$(".action-item", dlg).forEach(function (l) { if (!l.hidden) $("input", l).checked = on; });
        syncAllState();
      });
    });
    $("[data-actions-apply]", dlg).addEventListener("click", function () {
      if (!pickerRow) return;
      var id = pickerRow.getAttribute("data-mcp-id");
      var item = toolsetItem(id);
      var all = $("[data-actions-all]", dlg).checked;
      var names = null;
      if (pickerRow.getAttribute("data-mode") === "list") {
        $$("[data-subset-all], [data-subset-item]", pickerRow).forEach(function (n) { n.remove(); });
        names = all ? null : $$("[data-actions-groups] input:checked", dlg).map(function (c) { return c.value; });
        if (!names || !names.length) { names = null; pickerRow.appendChild(hidden("tool_subset_all__" + id, "1", { "data-subset-all": "" })); }
        else names.forEach(function (n) { pickerRow.appendChild(hidden("tool_subset_cb__" + id, n, { "data-subset-item": "" })); });
      } else {
        var txt = $("[data-subset-text]", pickerRow);
        if ((item.actions || []).length) {
          names = all ? null : $$("[data-actions-groups] input:checked", dlg).map(function (c) { return c.value; });
          if (names && !names.length) names = null;
        } else {
          var raw = $("[data-actions-text-input]", dlg).value.trim();
          names = all || !raw ? null : raw.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        }
        if (txt) txt.value = names ? names.join(", ") : "";
      }
      // Reflect it on the row.
      var total = (item.actions || []).length;
      var lbl = $("[data-count-label]", pickerRow);
      if (lbl) lbl.textContent = names ? (total ? names.length + " of " + total + " actions" : names.length + " actions") : (total ? "All " + total + " actions" : "All actions");
      var chips = $("[data-chips]", pickerRow);
      if (chips) {
        chips.innerHTML = "";
        var labelOf = {};
        (item.actions || []).forEach(function (a) { labelOf[a.name] = a.label; });
        (names || (item.actions || []).map(function (a) { return a.name; })).forEach(function (n) {
          chips.appendChild(el("span", { class: "chip chip-neutral", title: n, text: labelOf[n] || n }));
        });
      }
      dlg.close();
      root && root.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-pick-actions]");
    if (b) openActionPicker(b.getAttribute("data-pick-actions"));
  });

  // ------------------------------------------------------ small table filter
  $$("[data-filter-rows]").forEach(function (inp) {
    var table = document.getElementById(inp.getAttribute("data-filter-rows"));
    if (!table) return;
    inp.addEventListener("input", function () {
      var q = inp.value.trim().toLowerCase();
      $$("[data-filter-row]", table).forEach(function (r) { r.hidden = q && (r.getAttribute("data-search") || "").indexOf(q) === -1; });
    });
  });

  // --------------------------------------------------------- checklist filter
  $$("[data-filter-checklist]").forEach(function (inp) {
    var list = document.getElementById(inp.getAttribute("data-filter-checklist"));
    inp.addEventListener("input", function () {
      var q = inp.value.trim().toLowerCase();
      $$("[data-check-item]", list).forEach(function (l) { l.hidden = q && (l.getAttribute("data-search") || "").indexOf(q) === -1; });
    });
  });

  // ------------------------------------------------------------ prompt editor
  function renderMd(raw) {
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") return null;
    return DOMPurify.sanitize(marked.parse(raw || "", { breaks: true, gfm: true }));
  }
  $$("[data-prompt-editor]").forEach(function (ed) {
    var input = $("[data-prompt-input]", ed);
    var preview = $("[data-prompt-preview]", ed);
    var tabs = $$(".prompt-tab", ed);
    var expandBtn = $("[data-prompt-expand]", ed);
    function setView(view) {
      var isPreview = view === "preview";
      if (isPreview) {
        var html = renderMd(input.value);
        if (html == null) return;
        preview.innerHTML = html;
      }
      input.hidden = isPreview;
      preview.hidden = !isPreview;
      tabs.forEach(function (t) { t.classList.toggle("is-active", t.getAttribute("data-prompt-view") === view); });
    }
    tabs.forEach(function (t) { t.addEventListener("click", function () { setView(t.getAttribute("data-prompt-view")); }); });
    if (expandBtn) {
      expandBtn.addEventListener("click", function () {
        var on = ed.classList.toggle("is-expanded");
        document.body.classList.toggle("prompt-editor-lock", on);
        expandBtn.textContent = on ? "⤡ Collapse" : "⤢ Expand";
        if (!input.hidden) input.focus();
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && ed.classList.contains("is-expanded")) {
          ed.classList.remove("is-expanded");
          document.body.classList.remove("prompt-editor-lock");
          expandBtn.textContent = "⤢ Expand";
        }
      });
    }
  });

  // Fade the "Saved." flash and drop ?saved from the URL.
  var flash = $("[data-flash]");
  // The demo's guided tour (demo-tour.js) listens for this; a no-op elsewhere.
  if (flash) {
    try {
      var tabEl = $("[data-agent-tab]");
      var count = function (sel) { return document.querySelectorAll(sel).length; };
      var saved = {
        kind: tabEl ? tabEl.getAttribute("data-agent-tab") : "",
        profile: tabEl ? (tabEl.getAttribute("data-agent-slug") || "") : "",
        profile_id: tabEl ? (tabEl.getAttribute("data-profile-id") || "") : "",
        toolsets: count('[data-attached-list="toolset"] [data-row]'),
        knowledge: count('[data-attached-list="knowledge"] [data-row]'),
        subagents: count('#form-subagents [data-row]')
      };
      window.__tourSaved = saved;
      var fireSaved = function () {
        try { window.dispatchEvent(new CustomEvent("tour:saved", { detail: saved })); } catch (e) { /* optional */ }
      };
      // Deferred scripts after this one (the tour) may not listen yet.
      if (document.currentScript) document.addEventListener("DOMContentLoaded", fireSaved);
      else fireSaved();
    } catch (e) { /* the tour is optional */ }
  }
  if (flash) {
    try { var u = new URL(window.location.href); u.searchParams.delete("saved"); history.replaceState(null, "", u.toString()); } catch (e) { /* ignore */ }
    setTimeout(function () { flash.classList.add("is-fading"); setTimeout(function () { flash.remove(); }, 400); }, 2500);
  }
})();
