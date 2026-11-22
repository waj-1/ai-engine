// Portal-wide enhancements:
//
// 1. Convert every <table data-tabulator> into a Tabulator instance with
//    sort and header filters. The CSV / JSON / Clear-filters toolbar is
//    opt-in (`data-tabulator-export`, or automatic past 25 rows). A lone
//    `<td colspan>` status row becomes the empty-state placeholder; a
//    `<tr data-state="error">` one renders it as an error.
// 2. Walk every [data-md] element and render its text as markdown via
//    marked + DOMPurify so message bodies and prompt blocks aren't a
//    wall of escaped backticks.
//
// All progressive — if Tabulator or marked failed to load the page
// still renders, just without the bells and whistles.

(function () {
    "use strict";

    // ── Chart.js theme bridge ─────────────────────────────────────────
    // Resolve the semantic CSS tokens into a palette charts can use, and
    // keep Chart.js defaults in sync with the active light/dark theme.
    function wajChartTheme() {
        const css = getComputedStyle(document.documentElement);
        const v = (name) => css.getPropertyValue(name).trim();
        return {
            fg: v("--fg-2"), grid: v("--border"),
            accent: v("--accent"), accentSoft: v("--accent-soft"),
            success: v("--success"), warning: v("--warning"), error: v("--error"),
        };
    }
    window.wajChartTheme = wajChartTheme;

    function applyChartDefaults() {
        if (!window.Chart) return;
        const t = wajChartTheme();
        Chart.defaults.color = t.fg;
        Chart.defaults.borderColor = t.grid;
    }

    window.addEventListener("waj-theme-change", () => {
        if (!window.Chart) return;
        applyChartDefaults();
        for (const c of Object.values(Chart.instances || {})) c.update();
    });

    function readColumns(table) {
        // Build Tabulator column defs from the existing <th> headers.
        // Each th can carry data-tabulator-* attrs to tune the column.
        const headers = table.querySelectorAll("thead th");
        const cols = [];
        headers.forEach((th, idx) => {
            const text = th.textContent.trim();
            const formatter = th.dataset.tabulatorFormatter;
            const width = th.dataset.tabulatorWidth ? parseInt(th.dataset.tabulatorWidth, 10) : undefined;
            const col = {
                title: text,
                field: "c" + idx,
                headerFilter: text ? "input" : false,
                formatter: "html",
                resizable: true,
                widthGrow: text ? 1 : 0,
            };
            if (formatter === "rowSelection") {
                col.formatter = "rowSelection";
                col.titleFormatter = "rowSelection";
                col.hozAlign = "center";
                col.headerSort = false;
                col.headerFilter = false;
            }
            if (width) col.width = width;
            // Numeric columns: <th data-tabulator-align="right"> right-aligns
            // the cells and renders them with tabular figures (.num).
            const align = th.dataset.tabulatorAlign;
            if (align) {
                col.hozAlign = align;
                col.cssClass = "num";
            }
            cols.push(col);
        });
        return cols;
    }

    // A lone full-width cell (`<td colspan>`) is a status row — "Fetching…",
    // "No runs yet." or an error a poller wrote — not data. It becomes the
    // Tabulator placeholder; `<tr data-state="error">` marks it as an error.
    function placeholderRow(tr) {
        const cells = tr.querySelectorAll("td");
        return cells.length === 1 && cells[0].hasAttribute("colspan");
    }

    function readPlaceholder(table) {
        const tr = Array.from(table.querySelectorAll("tbody tr")).find(placeholderRow);
        if (!tr) return null;
        return {
            text: tr.textContent.trim() || "No rows.",
            error: tr.dataset.state === "error",
        };
    }

    function readData(table) {
        const rows = table.querySelectorAll("tbody tr");
        const out = [];
        rows.forEach((tr) => {
            if (placeholderRow(tr)) return;
            const cells = tr.querySelectorAll("td");
            const obj = {};
            cells.forEach((td, idx) => {
                obj["c" + idx] = td.innerHTML.trim();
            });
            // Carry through a click-target for the row. Templates set
            // this two ways:
            //   <tr onclick="window.location='/admin/sessions/{id}'">
            //   <tr data-href="/admin/sessions/{id}">
            // We extract whichever is present so the Tabulator-rendered
            // row can navigate when the user clicks it.
            const onclick = tr.getAttribute("onclick") || "";
            const m = onclick.match(/window\.location\s*=\s*['"]([^'"]+)['"]/);
            const href = tr.getAttribute("data-href") || (m ? m[1] : null);
            if (href) obj.__href = href;
            obj.__rowClass = tr.className || null;
            out.push(obj);
        });
        return out;
    }

    // Export toolbar (CSV / JSON / Clear filters) is opt-in: a table gets it
    // with `data-tabulator-export`, or automatically once it holds more than
    // EXPORT_AUTO_ROWS rows. `data-tabulator-export="off"` always suppresses
    // it. A 2-row table no longer carries a 3-button toolbar.
    const EXPORT_AUTO_ROWS = 25;
    function wantsToolbar(tableEl, rowCount) {
        const opt = tableEl.dataset.tabulatorExport;
        if (opt === "off" || opt === "false") return false;
        if (opt !== undefined) return true;
        return rowCount > EXPORT_AUTO_ROWS;
    }

    function buildToolbar(table, tableEl) {
        const wrap = document.createElement("div");
        wrap.className = "tabulator-toolbar";
        wrap.innerHTML = ""
            + "<button type='button' class='ghost' data-export='csv'>CSV</button>"
            + "<button type='button' class='ghost' data-export='json'>JSON</button>"
            + "<button type='button' class='ghost' data-clear-filters>Clear filters</button>";
        tableEl.parentNode.insertBefore(wrap, tableEl);
        wrap.querySelector("[data-export='csv']").addEventListener("click", () => table.download("csv", "export.csv"));
        wrap.querySelector("[data-export='json']").addEventListener("click", () => table.download("json", "export.json"));
        wrap.querySelector("[data-clear-filters]").addEventListener("click", () => table.clearHeaderFilter());
    }

    function enhanceTables() {
        if (typeof Tabulator === "undefined") return;
        const tables = document.querySelectorAll("table[data-tabulator]");
        tables.forEach((tableEl) => {
            if (tableEl.dataset.tabulatorReady) return;
            tableEl.dataset.tabulatorReady = "1";
            const pageSize = parseInt(tableEl.dataset.tabulatorPageSize || "25", 10);
            const cols = readColumns(tableEl);
            const data = readData(tableEl);
            // Replace the <table> with a host <div>; Tabulator builds its
            // own DOM. We keep the original markup hidden so forms inside
            // (checkboxes for the agent mapping editor) still POST values.
            const host = document.createElement("div");
            host.className = "tabulator-host";
            tableEl.style.display = "none";
            tableEl.parentNode.insertBefore(host, tableEl);
            const hasHref = data.some((r) => r.__href);
            // Layout mode: "fitColumns" squashes every column into the
            // viewport which makes wide tables unusable. Use
            // "fitDataStretch" by default — columns size to their
            // content, the last column flexes to fill any leftover
            // space, and overflow scrolls horizontally. Templates can
            // opt back into the old behaviour with
            // `<table data-tabulator data-tabulator-layout="fitColumns">`
            // when the table only has 3-4 short columns.
            const layout = tableEl.dataset.tabulatorLayout || "fitDataStretch";
            // Row rendering mode. Tabulator's default "virtual" DOM can fall into
            // an infinite tableResized -> adjustTableSize -> _virtualRenderFill ->
            // tableResized loop ("Maximum call stack size exceeded") for tables in
            // auto-height flex containers. Templates opt out with
            // `data-tabulator-render="basic"` (renders the whole current page, no
            // virtualization) — fine at our paginated sizes.
            const renderVertical = tableEl.dataset.tabulatorRender || "virtual";
            // Placeholder text follows the source table's status row, so a
            // poller that writes "Couldn't load…" into the hidden tbody shows
            // up as an error instead of a stale "Fetching…" (see watchSource).
            let placeholder = readPlaceholder(tableEl);
            const placeholderText = () => (placeholder ? placeholder.text : "No rows.");
            host.classList.toggle("is-error", !!(placeholder && placeholder.error));
            const tab = new Tabulator(host, {
                data: data,
                columns: cols,
                layout: layout,
                renderVertical: renderVertical,
                pagination: data.length > pageSize,
                paginationSize: pageSize,
                paginationSizeSelector: [10, 25, 50, 100, true],
                placeholder: placeholderText,
                movableColumns: true,
                resizableRows: false,
                // Keep the table within its host width and scroll the
                // inside; without this Tabulator can blow past the
                // .app-content max-width on very wide tables.
                maxHeight: tableEl.dataset.tabulatorMaxHeight || "70vh",
                rowFormatter: function (row) {
                    const cls = row.getData().__rowClass;
                    if (cls) cls.split(' ').forEach((c) => c && row.getElement().classList.add(c));
                },
            });
            // rowClick was removed from constructor options in Tabulator 6.x;
            // use the event subscription API instead.
            if (hasHref) {
                tab.on("rowClick", (e, row) => {
                    const href = row.getData().__href;
                    if (!href) return;
                    // Cmd/Ctrl/middle-click opens in a new tab.
                    if (e.metaKey || e.ctrlKey || e.button === 1) {
                        window.open(href, "_blank");
                    } else {
                        window.location = href;
                    }
                });
                host.style.cursor = "pointer";
            }
            let toolbar = wantsToolbar(tableEl, data.length);
            if (toolbar) buildToolbar(tab, host);
            // Keep the grid in sync when a script rewrites the source tbody
            // (deploy-poll.js and friends render into the original <table>).
            const tbody = tableEl.querySelector("tbody");
            if (tbody && typeof MutationObserver !== "undefined") {
                new MutationObserver(() => {
                    placeholder = readPlaceholder(tableEl);
                    host.classList.toggle("is-error", !!(placeholder && placeholder.error));
                    const rows = readData(tableEl);
                    // setData re-shows the placeholder on an empty set, which
                    // re-calls placeholderText() and picks up the new status.
                    tab.setData(rows).catch(() => {});
                    if (!toolbar && wantsToolbar(tableEl, rows.length)) {
                        buildToolbar(tab, host);
                        toolbar = true;
                    }
                }).observe(tbody, { childList: true, subtree: true, characterData: true });
            }
        });
    }

    function renderMarkdown() {
        if (typeof marked === "undefined" || typeof DOMPurify === "undefined") return;
        const nodes = document.querySelectorAll("[data-md]:not([data-md-rendered])");
        nodes.forEach((el) => {
            const raw = el.textContent;
            const html = DOMPurify.sanitize(marked.parse(raw, { breaks: true, gfm: true }));
            el.innerHTML = html;
            el.setAttribute("data-md-rendered", "1");
        });
    }

    function boot() {
        // Chart.js loads with `defer` like this file; by DOMContentLoaded it
        // is defined (deferred scripts run in document order), but guard
        // anyway so a CDN failure degrades gracefully.
        applyChartDefaults();
        enhanceTables();
        renderMarkdown();
    }

    // Attempt immediately too, in case this script runs after Chart.js
    // (it does — _base.html loads chart.umd first) so per-screen chart
    // scripts that fire on DOMContentLoaded already see themed defaults.
    applyChartDefaults();

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();

// ── Markdown edit/preview toggle ────────────────────────────────────────────
// Delegated handler for [data-md-preview-toggle] buttons. Each button must
// live inside a [data-md-editable] wrapper that also contains a <textarea>.
// Toggle state is per-wrapper and does not affect other fields on the page.
document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-md-preview-toggle]');
    if (!btn) return;
    const wrap = btn.closest('[data-md-editable]');
    if (!wrap) return;
    const ta = wrap.querySelector('textarea');
    if (!ta || typeof marked === 'undefined' || typeof DOMPurify === 'undefined') return;
    let prev = wrap.querySelector('.md-inline-preview');
    const showing = prev && !prev.hidden;
    if (showing) {
        prev.hidden = true;
        ta.hidden = false;
        btn.textContent = 'Preview';
    } else {
        if (!prev) {
            prev = document.createElement('div');
            prev.className = 'markdown-body md-inline-preview';
            ta.after(prev);
        }
        prev.innerHTML = DOMPurify.sanitize(marked.parse(ta.value || '*empty*'));
        prev.hidden = false;
        ta.hidden = true;
        btn.textContent = 'Edit';
    }
});
