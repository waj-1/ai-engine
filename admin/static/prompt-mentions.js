// System-prompt @-mention dropdown.
//
// Adds an @-mention autocomplete to the admin portal's system-prompt editor
// (agents.html). When an operator types `@` at a word boundary inside the
// prompt textarea, a small dropdown lists the tools, MCPs, and KBs available
// to the active profile. Picking one inserts a stable token —
//   @[tool:get_clients]   @[mcp:Product Docs]   @[kb:Getting Started Guide]
// which the server-side prompt resolver later expands to `` `name` ``. This
// ships dark: it only ever *inserts* a token the operator chose, so there's
// nothing to gate — the resolver already understands the token.
//
// Wiring: the editor div carries [data-prompt-editor][data-mentions-profile]
// and contains the [data-prompt-input] textarea. We fetch the profile's
// mentionables lazily (once, on the first `@`) from
//   GET /admin/api/profiles/<pid>/mentionables
// and cache the JSON on the editor's state. Everything renders via
// textContent / createTextNode — MCP and KB names are operator-influenceable,
// so we never feed server strings into innerHTML. On any fetch failure we
// simply don't show a dropdown (fail silent — never throw into the page).

(function () {
    "use strict";

    // Response key → token type + dropdown header. Order = display order.
    var GROUPS = [
        { key: "tools", type: "tool", header: "Tools" },
        { key: "mcps", type: "mcp", header: "MCPs" },
        { key: "kbs", type: "kb", header: "KBs" },
    ];

    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (k) {
                if (k === "style") {
                    node.setAttribute("style", attrs[k]);
                } else if (k === "className") {
                    node.className = attrs[k];
                } else {
                    node.setAttribute(k, attrs[k]);
                }
            });
        }
        // Server strings arrive here as plain strings and become text nodes —
        // never parsed as HTML. This is the XSS guard.
        (children || []).forEach(function (c) {
            if (c == null) return;
            node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
        });
        return node;
    }

    // Locate an active @-mention ending at the caret. Returns
    //   { start, fragment }
    // when the nearest `@` before the caret sits at a word boundary
    // (start-of-string or whitespace/newline before it) and no newline falls
    // between it and the caret. Returns null otherwise, so `email@x` — where
    // the char before `@` is `l` — never triggers.
    function activeMention(value, caret) {
        var at = -1;
        for (var i = caret - 1; i >= 0; i--) {
            var ch = value.charAt(i);
            if (ch === "\n") return null; // fragment stops at a newline
            if (ch === "@") { at = i; break; }
        }
        if (at < 0) return null;
        if (at > 0 && !/\s/.test(value.charAt(at - 1))) return null; // word boundary
        return { start: at, fragment: value.slice(at + 1, caret) };
    }

    function filterItems(items, fragment) {
        var q = (fragment || "").toLowerCase();
        return (items || []).filter(function (it) {
            return it && it.name && String(it.name).toLowerCase().indexOf(q) >= 0;
        });
    }

    function ensureData(state, cb) {
        if (state.fetched) { if (cb) cb(); return; }
        if (state.loading) return;
        state.loading = true;
        var url = "/admin/api/profiles/" + encodeURIComponent(state.pid) + "/mentionables";
        fetch(url, { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) {
                state.loading = false;
                state.fetched = true;
                state.data = (data && typeof data === "object")
                    ? data
                    : { tools: [], mcps: [], kbs: [] };
                if (cb) cb();
            })
            .catch(function () {
                // Fail silent: mark fetched so we don't hammer on every `@`,
                // and leave data empty so no dropdown is shown.
                state.loading = false;
                state.fetched = true;
                state.data = { tools: [], mcps: [], kbs: [] };
            });
    }

    function closeDropdownEl(state) {
        if (state.dropdown && state.dropdown.parentNode) {
            state.dropdown.parentNode.removeChild(state.dropdown);
        }
        state.dropdown = null;
    }

    function closeDropdown(state) {
        closeDropdownEl(state);
        state.options = [];
        state.highlight = -1;
        state.start = -1;
    }

    function setHighlight(state, idx) {
        if (idx < 0 || idx >= state.options.length) return;
        state.options.forEach(function (o, i) {
            o.el.style.background = (i === idx) ? "var(--accent-soft)" : "transparent";
        });
        state.highlight = idx;
        var cur = state.options[idx];
        if (cur && cur.el.scrollIntoView) cur.el.scrollIntoView({ block: "nearest" });
    }

    function renderDropdown(state, groups) {
        closeDropdownEl(state);
        var dd = el("div", {
            "data-mention-dropdown": "",
            style: "position:absolute;z-index:60;min-width:240px;max-width:380px;max-height:260px;" +
                   "overflow:auto;background:var(--surface-raised);border:1px solid var(--border);" +
                   "border-radius:8px;box-shadow:0 12px 32px rgba(0,0,0,0.28);padding:4px 0;font-size:13px;",
        }, []);

        state.options = [];
        groups.forEach(function (g) {
            dd.appendChild(el("div", {
                className: "eyebrow",
                style: "padding:6px 12px 2px;opacity:0.7;",
            }, [g.header]));
            g.items.forEach(function (it) {
                var idx = state.options.length;
                var row = el("div", {
                    "data-mention-option": "",
                    style: "padding:4px 12px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;",
                }, [it.label || it.name]);
                // mousedown (not click) + preventDefault beats the textarea's
                // blur-close so the selection registers with focus intact.
                row.addEventListener("mousedown", function (e) {
                    e.preventDefault();
                    selectOption(state, g.type, it.name);
                });
                row.addEventListener("mouseenter", function () { setHighlight(state, idx); });
                state.options.push({ type: g.type, name: it.name, el: row });
                dd.appendChild(row);
            });
        });

        // Simple, predictable placement: a box just below the textarea inside
        // the (position:relative) editor. No caret pixel math.
        var ta = state.input;
        dd.style.left = ta.offsetLeft + "px";
        dd.style.top = (ta.offsetTop + ta.offsetHeight + 4) + "px";

        state.editor.appendChild(dd);
        state.dropdown = dd;
        setHighlight(state, 0);
    }

    function showFor(state, fragment) {
        var data = state.data || {};
        var groups = GROUPS.map(function (g) {
            return { type: g.type, header: g.header, items: filterItems(data[g.key], fragment) };
        }).filter(function (g) { return g.items.length > 0; });

        var total = groups.reduce(function (n, g) { return n + g.items.length; }, 0);
        if (total === 0) { closeDropdown(state); return; }
        renderDropdown(state, groups);
    }

    function selectOption(state, type, name) {
        var ta = state.input;
        var value = ta.value;
        var caret = ta.selectionStart;
        var men = activeMention(value, caret);
        var start = men ? men.start : state.start;
        if (start < 0) { closeDropdown(state); return; }

        var token = "@[" + type + ":" + name + "]";
        ta.value = value.slice(0, start) + token + value.slice(caret);
        var pos = start + token.length;
        ta.focus();
        ta.setSelectionRange(pos, pos);
        closeDropdown(state);

        // Keep any other listeners consistent, but don't let our own input
        // handler reopen the dropdown for the token we just inserted.
        state.ignoreInput = true;
        ta.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function onInput(state) {
        if (state.ignoreInput) { state.ignoreInput = false; return; }
        var men = activeMention(state.input.value, state.input.selectionStart);
        if (!men) { closeDropdown(state); return; }
        state.start = men.start;
        if (!state.fetched) {
            ensureData(state, function () {
                // Re-check on load — the caret may have moved while fetching.
                var m2 = activeMention(state.input.value, state.input.selectionStart);
                if (m2) { state.start = m2.start; showFor(state, m2.fragment); }
            });
            return;
        }
        showFor(state, men.fragment);
    }

    function onKeydown(state, e) {
        if (!state.dropdown || state.options.length === 0) return;
        if (e.key === "ArrowDown") {
            e.preventDefault();
            setHighlight(state, (state.highlight + 1) % state.options.length);
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlight(state, (state.highlight - 1 + state.options.length) % state.options.length);
        } else if (e.key === "Enter") {
            e.preventDefault(); // don't submit the form or add a newline
            var cur = state.options[state.highlight];
            if (cur) selectOption(state, cur.type, cur.name);
        } else if (e.key === "Escape") {
            e.preventDefault();
            closeDropdown(state);
        }
    }

    function init(editor) {
        if (editor.dataset.mentionsInit === "1") return;
        var input = editor.querySelector("[data-prompt-input]");
        var pid = editor.getAttribute("data-mentions-profile");
        if (!input || !pid) return;
        editor.dataset.mentionsInit = "1";

        // Anchor the absolutely-positioned dropdown to the editor.
        if (window.getComputedStyle(editor).position === "static") {
            editor.style.position = "relative";
        }

        var state = {
            editor: editor,
            input: input,
            pid: pid,
            data: null,
            fetched: false,
            loading: false,
            dropdown: null,
            options: [],
            highlight: -1,
            start: -1,
            ignoreInput: false,
        };

        input.addEventListener("input", function () { onInput(state); });
        input.addEventListener("keydown", function (e) { onKeydown(state, e); });
        // Clicking away / blurring closes it. The delay lets an option's
        // mousedown fire first (belt-and-braces with the preventDefault above).
        input.addEventListener("blur", function () {
            setTimeout(function () { closeDropdown(state); }, 150);
        });
    }

    function boot() {
        document.querySelectorAll("[data-prompt-editor][data-mentions-profile]").forEach(init);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
