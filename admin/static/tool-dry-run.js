// HTTP-tool dry-run UI.
//
// Lives next to the tool form on the in-MCP add-tool screen
// (/admin/agent-mcp-servers/{id}/add-tool) and the HTTP-tool edit
// section on /admin/tools/{id}. Lets the operator hit `Test tool` with a
// sample args payload and see the endpoint's actual response inline —
// before saving, before attaching to a profile, before running an eval.
//
// Wiring: every page that opts in drops a
//   <section data-tool-dry-run>...</section>
// somewhere inside (or alongside) the form it cares about. We locate the
// form by walking up from the section to the nearest <form>, then we
// pluck endpoint_url / http_method / auth_env_var / timeout_seconds /
// argument_schema out of it by `name="..."`. That way the same JS works
// on both the create form and the edit form without per-page wiring.

(function () {
    "use strict";

    function findForm(section) {
        // The section is rendered AFTER the form on /tools/new, so it isn't
        // a descendant. Search nearby: ancestor first, then the closest
        // previous form sibling, then any form in the page.
        const ancestor = section.closest("form");
        if (ancestor) return ancestor;
        let el = section.previousElementSibling;
        while (el) {
            if (el.tagName === "FORM") return el;
            const inside = el.querySelector && el.querySelector("form");
            if (inside) return inside;
            el = el.previousElementSibling;
        }
        // Fallback: any form on the page that has the fields we need.
        const forms = document.querySelectorAll("form");
        for (const f of forms) {
            if (f.querySelector('[name="endpoint_url"]') && f.querySelector('[name="argument_schema"]')) {
                return f;
            }
        }
        return null;
    }

    function readField(form, name) {
        if (!form) return "";
        const el = form.querySelector('[name="' + name + '"]');
        return el ? (el.value || "") : "";
    }

    function generateSampleArgs(schemaText) {
        // Produce a kwargs object the operator can edit. Strategy:
        //   - Parse the JSON schema. If it's broken, return {} so the UI
        //     still gives them an editable starting point.
        //   - Include every required field; for optionals, include them
        //     too (operators can delete what they don't want, but the
        //     visible structure is more useful than a stub `{}`).
        //   - Use the schema's `default` when present, otherwise a typed
        //     placeholder per `type` (string → "sample", number → 0,
        //     boolean → false, array → [], object → {}).
        let schema;
        try {
            schema = JSON.parse(schemaText);
        } catch (e) {
            return {};
        }
        if (!schema || typeof schema !== "object") return {};
        const props = schema.properties || {};
        const out = {};
        Object.keys(props).forEach(function (k) {
            const p = props[k] || {};
            if (Object.prototype.hasOwnProperty.call(p, "default")) {
                out[k] = p.default;
                return;
            }
            let t = p.type;
            if (Array.isArray(t)) {
                const nn = t.filter(function (x) { return x !== "null"; });
                t = nn[0] || "string";
            }
            switch (t) {
                case "integer":
                case "number":
                    out[k] = 0; break;
                case "boolean":
                    out[k] = false; break;
                case "array":
                    out[k] = []; break;
                case "object":
                    out[k] = {}; break;
                default:
                    out[k] = "sample";
            }
        });
        return out;
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
        const colors = {
            ok:   "background:rgba(40,160,80,0.18);color:#5fd49b;border:1px solid rgba(40,160,80,0.35);",
            warn: "background:rgba(200,160,40,0.18);color:#e8c46d;border:1px solid rgba(200,160,40,0.35);",
            err:  "background:rgba(200,60,60,0.18);color:#ef8a8a;border:1px solid rgba(200,60,60,0.35);",
        };
        return el("span", {
            style: "display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:0.04em;" + (colors[variant] || colors.warn),
        }, [text]);
    }

    function renderResult(container, data) {
        container.innerHTML = "";
        if (!data) return;

        const isOk = data.ok === true;
        const isValidation = !isOk && data.error_class === "ValidationError";
        const variant = isOk ? "ok" : isValidation ? "warn" : "err";
        const label = isOk
            ? "OK · HTTP " + (data.http_status != null ? data.http_status : "?")
            : (data.error_class || "Error");

        const header = el("div", {
            style: "display:flex;gap:var(--space-s);align-items:center;flex-wrap:wrap;",
        }, [
            chip(label, variant),
            el("span", { className: "caption" }, ["latency " + (data.latency_ms != null ? data.latency_ms : 0) + " ms"]),
        ]);
        container.appendChild(header);

        if (!isOk && data.error) {
            container.appendChild(el("pre", {
                className: "num",
                style: "white-space:pre-wrap;font-size:12px;background:rgba(200,60,60,0.06);border:1px solid rgba(200,60,60,0.25);padding:var(--space-s);border-radius:6px;margin-top:var(--space-xs);",
            }, [data.error]));
        }

        if (data.request) {
            const req = data.request;
            const reqBlock = el("details", { style: "margin-top:var(--space-s);" }, [
                el("summary", { className: "caption", style: "cursor:pointer;" }, [
                    "Request · " + (req.method || "") + " " + (req.url || ""),
                ]),
                el("pre", {
                    className: "num",
                    style: "font-size:11px;overflow:auto;max-height:200px;margin-top:var(--space-xs);",
                }, [
                    "Headers:\n" + JSON.stringify(req.headers || {}, null, 2) +
                    "\n\nBody:\n" + JSON.stringify(req.body || {}, null, 2),
                ]),
            ]);
            container.appendChild(reqBlock);
        }

        if (data.response) {
            const resp = data.response;
            const meta = "Status " + (data.http_status != null ? data.http_status : "?") +
                " · " + (resp.content_type || "text/plain") +
                " · " + (resp.size_bytes != null ? resp.size_bytes : 0) + " bytes" +
                (resp.truncated ? " (truncated to 4KB)" : "");
            container.appendChild(el("div", {
                className: "caption", style: "margin-top:var(--space-s);",
            }, [meta]));
            let pretty = resp.body || "";
            if ((resp.content_type || "").indexOf("json") >= 0) {
                try { pretty = JSON.stringify(JSON.parse(resp.body), null, 2); } catch (e) { /* keep raw */ }
            }
            container.appendChild(el("pre", {
                className: "num",
                style: "font-size:12px;overflow:auto;max-height:400px;margin-top:var(--space-xs);white-space:pre-wrap;word-break:break-word;",
            }, [pretty]));
        }
    }

    function init(section) {
        if (section.dataset.toolDryRunInit === "1") return;
        section.dataset.toolDryRunInit = "1";

        const form = findForm(section);
        if (!form) {
            section.appendChild(el("p", { className: "caption" }, [
                "Dry-run UI couldn't find the tool form on this page.",
            ]));
            return;
        }

        // Build the UI. We don't trust whatever was already inside the
        // section — overwrite it so callers don't have to author HTML.
        section.innerHTML = "";
        section.appendChild(el("h2", { style: "margin-top:0;display:flex;justify-content:space-between;align-items:center;gap:var(--space-s);" }, [
            "Test tool",
            chip("dry-run", "warn"),
        ]));
        section.appendChild(el("p", { className: "caption" }, [
            "Invoke the endpoint with the kwargs below. Nothing is persisted — this is the same code path the agent uses, against the values currently in the form (no need to save first).",
        ]));

        const argsTa = el("textarea", {
            id: "dry-run-args",
            name: "dry_run_args",
            rows: "10",
            className: "num",
            style: "font-size:12px;width:100%;",
            spellcheck: "false",
        });
        argsTa.value = JSON.stringify(generateSampleArgs(readField(form, "argument_schema")), null, 2);

        const genBtn = el("button", {
            type: "button",
            className: "ghost",
            onclick: function () {
                argsTa.value = JSON.stringify(generateSampleArgs(readField(form, "argument_schema")), null, 2);
            },
        }, ["Generate sample args"]);

        const testBtn = el("button", {
            type: "button",
            onclick: function () { runTest(); },
        }, ["Test tool"]);

        const result = el("div", { "data-dry-run-result": "", style: "margin-top:var(--space-s);" }, []);

        section.appendChild(el("label", {}, [
            el("span", { className: "eyebrow" }, ["Sample kwargs (JSON)"]),
            argsTa,
            el("span", { className: "caption" }, [
                "Pre-filled from the argument schema. Edit freely — required fields with no default use a typed placeholder.",
            ]),
        ]));
        section.appendChild(el("div", {
            style: "display:flex;gap:var(--space-s);margin-top:var(--space-s);flex-wrap:wrap;",
        }, [genBtn, testBtn]));
        section.appendChild(result);

        async function runTest() {
            let args;
            try {
                args = argsTa.value.trim() ? JSON.parse(argsTa.value) : {};
            } catch (e) {
                renderResult(result, {
                    ok: false,
                    error_class: "ValidationError",
                    error: "Sample kwargs is not valid JSON: " + e.message,
                    latency_ms: 0,
                });
                return;
            }
            let schema;
            try {
                schema = JSON.parse(readField(form, "argument_schema") || "{}");
            } catch (e) {
                renderResult(result, {
                    ok: false,
                    error_class: "ValidationError",
                    error: "argument_schema field is not valid JSON: " + e.message,
                    latency_ms: 0,
                });
                return;
            }
            const body = {
                endpoint_url: readField(form, "endpoint_url"),
                http_method: readField(form, "http_method") || "POST",
                auth_env_var: readField(form, "auth_env_var") || null,
                timeout_seconds: parseInt(readField(form, "timeout_seconds") || "30", 10),
                argument_schema: schema,
                args: args,
            };

            testBtn.disabled = true;
            testBtn.textContent = "Testing…";
            result.innerHTML = "";

            let resp, data;
            try {
                resp = await fetch("/admin/tools/dry-run", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
            } catch (e) {
                renderResult(result, {
                    ok: false,
                    error_class: "TransportError",
                    error: "Couldn't reach the agent service: " + e.message,
                    latency_ms: 0,
                });
                testBtn.disabled = false;
                testBtn.textContent = "Test tool";
                return;
            }

            const text = await resp.text();
            try { data = JSON.parse(text); } catch (e) { data = null; }

            if (!data) {
                renderResult(result, {
                    ok: false,
                    error_class: resp.status >= 500 ? "TransportError" : "ConfigError",
                    error: "HTTP " + resp.status + " from /admin/tools/dry-run:\n" + (text || "(empty body)").slice(0, 2000),
                    latency_ms: 0,
                });
            } else if (!resp.ok) {
                // Backend 4xx with JSON body — surface cleanly.
                renderResult(result, Object.assign({
                    ok: false,
                    error_class: data.error_class || "ConfigError",
                    error: data.error || ("HTTP " + resp.status),
                    latency_ms: data.latency_ms || 0,
                }, data));
            } else {
                renderResult(result, data);
            }

            testBtn.disabled = false;
            testBtn.textContent = "Test tool";
        }
    }

    function boot() {
        document.querySelectorAll("section[data-tool-dry-run]").forEach(init);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
