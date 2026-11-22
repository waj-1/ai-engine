// External MCP server "Discover tools" UI.
//
// Lives next to the connect-external-server form (Connect MCP / server detail
// screens). Lets the operator hit `Discover tools` and see the live
// `tools/list` result from the endpoint they just typed in — before saving
// the row, so they can catch a wrong URL or missing auth env var early.
//
// Wiring: any page that opts in drops a
//   <button type="button" data-mcp-discover>Discover tools</button>
// plus a
//   <div data-discover-result></div>
// near the connect form. We locate the form the same way tool-dry-run.js
// does (closest <form>, then walk previous siblings, then fall back to any
// form on the page with the fields we need), and locate the result
// container by walking up from the button looking for [data-discover-result]
// at each ancestor level.

(function () {
    "use strict";

    function findForm(btn) {
        const ancestor = btn.closest("form");
        if (ancestor) return ancestor;
        let el = btn.previousElementSibling;
        while (el) {
            if (el.tagName === "FORM") return el;
            const inside = el.querySelector && el.querySelector("form");
            if (inside) return inside;
            el = el.previousElementSibling;
        }
        const forms = document.querySelectorAll("form");
        for (const f of forms) {
            if (f.querySelector('[name="endpoint"]')) return f;
        }
        return null;
    }

    function findResultEl(btn) {
        let scope = btn.parentElement;
        while (scope) {
            const found = scope.querySelector("[data-discover-result]");
            if (found) return found;
            scope = scope.parentElement;
        }
        return document.querySelector("[data-discover-result]");
    }

    function readField(form, name) {
        if (!form) return "";
        const el = form.querySelector('[name="' + name + '"]');
        return el ? (el.value || "") : "";
    }

    function el(tag, attrs, children) {
        const node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (k) {
                if (k === "style") {
                    node.setAttribute("style", attrs[k]);
                } else if (k === "className") {
                    node.className = attrs[k];
                } else if (k.startsWith("on") && typeof attrs[k] === "function") {
                    node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
                } else {
                    node.setAttribute(k, attrs[k]);
                }
            });
        }
        (children || []).forEach(function (c) {
            if (c == null) return;
            node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
        });
        return node;
    }

    function chip(text, variant) {
        // Portal chip family (tokens, light + dark) instead of hardcoded colours.
        return el("span", { className: "chip chip-" + (variant || "warn") }, [text]);
    }

    // Plain-language headline for each failure class; the raw detail stays
    // underneath in small print for whoever has to fix it.
    function plainError(data) {
        const msg = (data.error || "").toLowerCase();
        if (msg.indexOf("loopback") !== -1 || msg.indexOf("link-local") !== -1 || msg.indexOf("metadata") !== -1) {
            return "That address is private or local, so the engine can't reach it. Use the server's public https:// address.";
        }
        if (msg.indexOf("absolute http") !== -1 || msg.indexOf("could not parse") !== -1) {
            return "That doesn't look like a web address. It should start with https://.";
        }
        if (msg.indexOf("401") !== -1 || msg.indexOf("403") !== -1 || msg.indexOf("unauthor") !== -1 || msg.indexOf("forbidden") !== -1) {
            return "The server refused the request. Check the access token setting.";
        }
        if (data.error_class === "TransportError" || msg.indexOf("timed out") !== -1 || msg.indexOf("connect") !== -1) {
            return "Couldn't reach the server. Check the address and that the server is running.";
        }
        return "The server didn't return a list of tools.";
    }

    function syncSubset(form, list) {
        const field = form && form.querySelector('[name="tool_subset"]');
        if (!field) return;
        const boxes = Array.prototype.slice.call(list.querySelectorAll("input[type=checkbox]"));
        const picked = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
        // All ticked = allow every tool (blank keeps future tools too).
        field.value = picked.length === boxes.length ? "" : picked.join(", ");
    }

    function renderResult(container, form, data) {
        container.innerHTML = "";
        if (!data) return;

        const isOk = data.ok === true;
        if (!isOk) {
            container.appendChild(el("div", { className: "row" }, [chip("Not connected", "err")]));
            container.appendChild(el("p", { style: "margin:var(--space-xs) 0 0;" }, [plainError(data)]));
            container.appendChild(el("p", { className: "technical", style: "margin:var(--space-xxs) 0 0;white-space:pre-wrap;" },
                [(data.error_class ? data.error_class + ": " : "") + (data.error || "")]));
            return;
        }

        const tools = data.tools || [];
        container.appendChild(el("div", { className: "row" }, [
            chip("Connected · " + tools.length + (tools.length === 1 ? " tool" : " tools"), "ok"),
            el("span", { className: "caption" }, ["answered in " + (data.latency_ms != null ? data.latency_ms : 0) + " ms"]),
        ]));
        if (tools.length === 0) {
            container.appendChild(el("p", { className: "caption", style: "margin-top:var(--space-s);" }, [
                "The server answered but offers no tools.",
            ]));
            return;
        }

        const field = form && form.querySelector('[name="tool_subset"]');
        const preset = field && field.value.trim()
            ? field.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean)
            : null;
        const list = el("div", { className: "check-grid", style: "margin-top:var(--space-s);grid-template-columns:1fr;" }, []);
        tools.forEach(function (t) {
            const box = el("input", { type: "checkbox", value: t.name || "" }, []);
            box.checked = !preset || preset.indexOf(t.name) !== -1;
            box.addEventListener("change", function () { syncSubset(form, list); });
            list.appendChild(el("label", { className: "field-check" }, [
                box,
                el("span", {}, [
                    el("strong", {}, [t.name || "(unnamed)"]),
                    t.description ? el("span", { className: "field-help" }, [t.description]) : null,
                ]),
            ]));
        });
        container.appendChild(el("p", { className: "caption", style: "margin:var(--space-s) 0 0;" }, [
            "Untick any tool your agents should not use.",
        ]));
        container.appendChild(list);
        syncSubset(form, list);
    }

    function init(btn) {
        if (btn.dataset.mcpDiscoverInit === "1") return;
        btn.dataset.mcpDiscoverInit = "1";

        const form = findForm(btn);
        const result = findResultEl(btn);

        btn.addEventListener("click", function () { runDiscover(); });

        async function runDiscover() {
            if (!result) return;
            if (!form) {
                renderResult(result, form, {
                    ok: false,
                    error_class: "ConfigError",
                    error: "Discover UI couldn't find the connect form on this page.",
                    latency_ms: 0,
                    tools: [],
                });
                return;
            }

            const body = {
                endpoint: readField(form, "endpoint"),
                transport: readField(form, "transport") || "http",
                auth_env_var: readField(form, "auth_env_var") || null,
            };

            const originalText = btn.textContent;
            btn.disabled = true;
            btn.textContent = "Discovering…";
            result.innerHTML = "";

            let resp, data;
            try {
                resp = await fetch("/admin/mcp/discover", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
            } catch (e) {
                renderResult(result, form, {
                    ok: false,
                    error_class: "TransportError",
                    error: "Couldn't reach the agent service: " + e.message,
                    latency_ms: 0,
                    tools: [],
                });
                btn.disabled = false;
                btn.textContent = originalText;
                return;
            }

            const text = await resp.text();
            try { data = JSON.parse(text); } catch (e) { data = null; }

            if (!data) {
                renderResult(result, form, {
                    ok: false,
                    error_class: resp.status >= 500 ? "TransportError" : "ConfigError",
                    error: "HTTP " + resp.status + " from /admin/mcp/discover:\n" + (text || "(empty body)").slice(0, 2000),
                    latency_ms: 0,
                    tools: [],
                });
            } else {
                renderResult(result, form, data);
            }

            btn.disabled = false;
            btn.textContent = originalText;
        }
    }

    function boot() {
        document.querySelectorAll("[data-mcp-discover]").forEach(init);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
