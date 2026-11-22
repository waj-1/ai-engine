// ai_agent/admin/static/live-charts.js
// Live Monitor analytics — fetches /admin/live/analytics.json and renders the
// charts with uPlot (vendored uplot.min.js, v1.6.30). Re-fetches + redraws when
// a filter changes; no full page reload.
(function () {
  "use strict";
  // Resolve a semantic CSS token to its computed color. Re-read on every
  // render so the charts pick up light/dark palette swaps (waj-theme-change).
  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }
  // Theme-dependent chart palette, resolved lazily (not at script load) so a
  // theme toggle before/after init always reads the active token values.
  function theme() {
    return {
      stroke: cssVar("--accent", "#006A6A"),
      grid: cssVar("--border", "#E1E5E9"),
      axis: cssVar("--fg-3", "#9AA4AE"),
      warn: cssVar("--warning", "#D97706"),
      err: cssVar("--error", "#DC2626")
    };
  }
  // Escape category labels (tool names, agent kinds) before they hit innerHTML.
  // Data is server-controlled, but this is cheap defense-in-depth.
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // Per-agent series colours: the server's AGENT_PALETTE
  // (admin/screens/observability.py), rendered into the page by live.html.
  // A kind without one takes the chart's fallback stroke.
  var AGENT_COLORS = window.WAJ_AGENT_COLORS || {};
  var charts = [];

  function clearCharts() {
    charts.forEach(function (c) { try { c.destroy(); } catch (e) {} });
    charts = [];
  }

  function mount(id) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = "";
    return el;
  }

  function emptyNote(el, msg) {
    if (el) el.innerHTML = '<p class="caption">' + (msg || "No data in this window.") + "</p>";
  }

  function xVals(buckets) { return buckets.map(function (b) { return Date.parse(b) / 1000; }); }

  // Average across a numeric series, ignoring null/undefined gaps.
  function avgOf(values) {
    var sum = 0, n = 0;
    (values || []).forEach(function (v) {
      if (v != null && !isNaN(v)) { sum += +v; n++; }
    });
    return n ? sum / n : null;
  }

  // uPlot draw hook: render a subtle dashed horizontal reference line at `avg`
  // on the first data scale. Keeps the line muted and out of the legend.
  function avgLinePlugin(avg, label) {
    var muted = theme().axis;
    return {
      hooks: {
        draw: function (u) {
          if (avg == null) return;
          var scaleKey = "y";
          // First non-x series defines the value scale; default uPlot key is "y".
          for (var i = 1; i < u.series.length; i++) {
            if (u.series[i].scale) { scaleKey = u.series[i].scale; break; }
          }
          var yPos = u.valToPos(avg, scaleKey, true);
          var ctx = u.ctx;
          var left = u.bbox.left, right = u.bbox.left + u.bbox.width;
          ctx.save();
          ctx.beginPath();
          ctx.setLineDash([4, 4]);
          ctx.strokeStyle = muted;
          ctx.lineWidth = 1;
          ctx.moveTo(left, yPos);
          ctx.lineTo(right, yPos);
          ctx.stroke();
          if (label) {
            ctx.setLineDash([]);
            ctx.fillStyle = muted;
            ctx.font = "10px system-ui, sans-serif";
            ctx.textAlign = "right";
            ctx.textBaseline = "bottom";
            ctx.fillText(label, right - 2, yPos - 2);
          }
          ctx.restore();
        }
      }
    };
  }

  // The figcaption in live.html is now the single source of each figure's
  // title, so charts no longer render their own (no uPlot opts.title here).
  function lineChart(elId, buckets, seriesDefs, opts2) {
    var el = mount(elId);
    if (!el || !buckets.length) { emptyNote(el); return; }
    opts2 = opts2 || {};
    var t = theme();
    var data = [xVals(buckets)].concat(seriesDefs.map(function (s) { return s.values; }));
    var opts = {
      width: el.clientWidth || 480, height: 200,
      scales: { x: { time: true } },
      axes: [{ stroke: t.axis, grid: { stroke: t.grid } },
             { stroke: t.axis, grid: { stroke: t.grid } }],
      series: [{}].concat(seriesDefs.map(function (s) {
        return { label: s.label, stroke: s.stroke || t.stroke, width: 1.6,
                 fill: s.fill || null };
      }))
    };
    // Optional dashed average reference line over the primary series.
    if (opts2.avgLine) {
      var avg = avgOf(seriesDefs[0].values);
      if (avg != null) {
        opts.plugins = [avgLinePlugin(avg, "avg " + (opts2.avgFmt ? opts2.avgFmt(avg) : Math.round(avg)))];
      }
    }
    charts.push(new uPlot(opts, data, el));
  }

  function stackedAgentChart(elId, buckets, volByAgent) {
    var el = mount(elId);
    var kinds = Object.keys(volByAgent || {});
    if (!el || !buckets.length || !kinds.length) { emptyNote(el); return; }
    var fallback = theme().axis;
    var defs = kinds.map(function (k) {
      return { label: k, values: volByAgent[k],
               stroke: AGENT_COLORS[k] || fallback,
               fill: (AGENT_COLORS[k] || fallback) + "33" };
    });
    lineChart(elId, buckets, defs);
  }

  function barChart(elId, labels, values, colorFn) {
    // Minimal horizontal bars in plain DOM (uPlot is line-oriented); keeps the
    // dependency surface small for the categorical charts. The figcaption in
    // live.html is the figure title, so no chart-internal title is rendered.
    var el = mount(elId);
    if (!el || !values.length) { emptyNote(el); return; }
    var max = Math.max.apply(null, values) || 1;
    var stroke = theme().stroke;
    var html = '<div class="mini-bars">';
    labels.forEach(function (lab, i) {
      var pct = Math.round((values[i] / max) * 100);
      var color = colorFn ? colorFn(lab) : stroke;
      html += '<div class="mini-bar-row"><span class="mini-bar-label">' + esc(lab) +
        '</span><span class="mini-bar-track"><span class="mini-bar-fill" style="width:' +
        pct + '%;background:' + color + '"></span></span><span class="mini-bar-val num">' +
        esc(values[i]) + "</span></div>";
    });
    el.innerHTML = html + "</div>";
  }

  // Vertical bar histogram in plain DOM: bars grow upward, count above each bar,
  // bucket label below. The trailing outlier bucket (when present) is tinted
  // distinctly so it reads as "everything slower than the cutoff".
  function verticalHistogram(elId, labels, values, hasOutlier) {
    var el = mount(elId);
    if (!el || !values.length) { emptyNote(el); return; }
    var max = Math.max.apply(null, values) || 1;
    var html = '<div class="vbars">';
    labels.forEach(function (lab, i) {
      var v = values[i] || 0;
      var pct = Math.round((v / max) * 100);
      var isOut = hasOutlier && i === labels.length - 1;
      var cls = "vbar-fill" + (isOut ? " vbar-fill--outlier" : "");
      html += '<div class="vbar-col">' +
        '<span class="vbar-count num">' + esc(v) + '</span>' +
        '<span class="vbar-track"><span class="' + cls +
        '" style="height:' + pct + '%"></span></span>' +
        '<span class="vbar-label">' + esc(lab) + '</span>' +
        '</div>';
    });
    el.innerHTML = html + "</div>";
  }

  // Keep a reference to the last loaded analytics payload so the CSV export
  // button can serialize it without making a second network request.
  var lastData = null;

  // -------------------------------------------------------------------
  // CSV export for the analytics dataset currently shown in the charts.
  // Exports two tables: (1) the time-series buckets, (2) the top_tools list.
  // -------------------------------------------------------------------
  function exportAnalyticsCsv() {
    var d = lastData;
    if (!d) { alert("No analytics data loaded yet. Try clicking Refresh first."); return; }
    var s = d.series || {};
    var buckets = d.buckets || [];
    // -- Time-series table --
    var lines = ["bucket,volume,tokens_in,tokens_out,cost_usd,p50_ms,p95_ms,error_rate,avg_iters,ttft_p50,ttft_p95,ttlt_p50,ttlt_p95"];
    var n = buckets.length;
    function safeVal(arr, i) {
      if (!arr || arr[i] == null) return "";
      return String(arr[i]);
    }
    for (var i = 0; i < n; i++) {
      lines.push([
        buckets[i],
        safeVal(s.volume, i),
        safeVal(s.tokens_in, i),
        safeVal(s.tokens_out, i),
        safeVal(s.cost_usd, i),
        safeVal(s.p50_ms, i),
        safeVal(s.p95_ms, i),
        safeVal(s.error_rate, i),
        safeVal(s.avg_iters, i),
        safeVal(s.ttft_p50, i),
        safeVal(s.ttft_p95, i),
        safeVal(s.ttlt_p50, i),
        safeVal(s.ttlt_p95, i)
      ].join(","));
    }
    lines.push("");  // blank separator between tables
    // -- Top tools table --
    lines.push("tool_name,calls");
    (d.top_tools || []).forEach(function (t) {
      var name = /[",\n]/.test(String(t.name)) ? '"' + String(t.name).replace(/"/g, '""') + '"' : String(t.name);
      lines.push(name + "," + (t.calls == null ? "" : String(t.calls)));
    });
    // -- Cost share table --
    lines.push("");
    lines.push("agent_kind,cost_usd");
    (d.cost_share || []).forEach(function (r) {
      lines.push(esc(r.agent_kind) + "," + (r.cost_usd == null ? "" : String(r.cost_usd)));
    });
    var csv = lines.join("\n");
    var blob = new Blob([csv], { type: "text/csv" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "live_analytics.csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
  // Expose so the inline onclick in live.html can reach it.
  window.exportAnalyticsCsv = exportAnalyticsCsv;

  // One message for the whole chart area instead of 14 "No data" boxes: an
  // unavailable source or an empty window hides the grid and shows a single
  // empty state (the old dashboard stacked ~1,250px of empty frames).
  function setChartsState(d) {
    var grid = document.getElementById("chart-grid");
    var empty = document.getElementById("charts-empty");
    var notice = document.getElementById("analytics-notice");
    var hasData = !!(d && d.ok && d.buckets && d.buckets.length);
    if (grid) grid.hidden = !hasData;
    if (empty) {
      empty.hidden = hasData;
      var p = empty.querySelector("p");
      if (p) p.textContent = (d && d.ok)
        ? "No traces for these filters — nothing to chart."
        : "Charts are unavailable right now.";
    }
    // Truncation notices etc. only matter when there is something to read.
    if (notice) {
      var msg = (d && d.ok) ? (d.notice || "") : (d && d.notice) || "";
      notice.textContent = msg;
      notice.hidden = !msg;
    }
    return hasData;
  }

  function render(d) {
    lastData = d;
    clearCharts();
    var t = theme();
    if (!setChartsState(d)) return;
    var s = d.series || {};
    lineChart("chart-volume", d.buckets, [{ label: "traces", values: s.volume, fill: t.stroke + "22" }]);
    lineChart("chart-tokens", d.buckets, [
      { label: "in", values: s.tokens_in },
      { label: "out", values: s.tokens_out, stroke: t.warn }]);
    lineChart("chart-cost", d.buckets, [{ label: "USD", values: s.cost_usd, fill: t.stroke + "22" }],
      { avgLine: true, avgFmt: function (v) { return "$" + v.toFixed(2); } });
    lineChart("chart-latency", d.buckets, [
      { label: "p50", values: s.p50_ms },
      { label: "p95", values: s.p95_ms, stroke: t.err }],
      { avgLine: true, avgFmt: fmtMs });
    lineChart("chart-errors", d.buckets, [{ label: "error %", values: s.error_rate, stroke: t.err, fill: t.err + "22" }]);
    lineChart("chart-iters", d.buckets, [{ label: "avg iters", values: s.avg_iters }]);
    stackedAgentChart("chart-by-agent", d.buckets, d.volume_by_agent);
    barChart("chart-cost-share",
      (d.cost_share || []).map(function (r) { return r.agent_kind; }),
      (d.cost_share || []).map(function (r) { return r.cost_usd; }),
      function (k) { return AGENT_COLORS[k] || t.stroke; });
    // Data-driven vertical histogram; the trailing outlier bucket (if any) is
    // tinted distinctly by verticalHistogram.
    var h = d.latency_hist || { labels: [], counts: [], outlier: false };
    verticalHistogram("chart-latency-hist", h.labels || [], h.counts || [], !!h.outlier);
    renderLatencyStats(d.totals || {});
    barChart("chart-top-tools",
      (d.top_tools || []).map(function (t) { return t.name; }),
      (d.top_tools || []).map(function (t) { return t.calls; }));
    // TTFT and TTLT are plotted SEPARATELY — they sit on very different scales
    // (first token ~seconds, last token ~10s+), so a shared y-axis flattens TTFT.
    lineChart("chart-ttft", d.buckets, [
      { label: "TTFT p50", values: s.ttft_p50 },
      { label: "TTFT p95", values: s.ttft_p95, stroke: t.warn }],
      { avgLine: true, avgFmt: fmtMs });
    lineChart("chart-ttlt", d.buckets, [
      { label: "TTLT p50", values: s.ttlt_p50, stroke: t.err },
      { label: "TTLT p95", values: s.ttlt_p95, stroke: t.err + "88" }],
      { avgLine: true, avgFmt: fmtMs });
    lineChart("chart-cache-tokens", d.buckets, [
      { label: "cached", values: s.tokens_cached, fill: t.stroke + "22" },
      { label: "uncached", values: s.tokens_uncached, stroke: t.warn }]);
    barChart("chart-cache-hit-by-agent",
      (d.per_agent || []).map(function (r) { return r.agent_kind; }),
      (d.per_agent || []).map(function (r) { return r.cache_hit_rate; }),
      function (k) { return AGENT_COLORS[k] || t.stroke; });
  }

  // Format milliseconds as a compact "320ms" / "1.8s" string for stat tiles
  // and the latency avg-line label (mirrors live_analytics._format_ms).
  function fmtMs(ms) {
    if (ms == null || isNaN(ms)) return "—";
    if (ms >= 1000) {
      var secs = ms / 1000;
      return (secs === Math.round(secs) ? secs.toFixed(0) : secs.toFixed(1)) + "s";
    }
    return Math.round(ms) + "ms";
  }

  // Fill the p50/p95/p99 latency stat tiles next to the latency charts.
  function renderLatencyStats(totals) {
    var el = document.getElementById("latency-stats");
    if (!el) return;
    var tiles = [
      ["p50", totals.p50_ms], ["p95", totals.p95_ms], ["p99", totals.p99_ms]
    ];
    el.innerHTML = tiles.map(function (t) {
      return '<span class="lat-stat"><span class="lat-stat-k">' + t[0] +
        '</span> <span class="lat-stat-v num">' + esc(fmtMs(t[1])) + '</span></span>';
    }).join('');
  }

  function currentQuery() {
    var p = new URLSearchParams();
    ["hours", "agent_kind", "channel", "status"].forEach(function (k) {
      var el = document.getElementById("filter-" + k);
      if (el && el.value) p.set(k, el.value);
    });
    // Scope analytics to the portal's current env so each env shows its own
    // numbers. Env is fixed per portal deployment, surfaced via data-env on the
    // analytics section (set from current_env in live.html).
    var section = document.getElementById("analytics-section");
    var env = section && section.getAttribute("data-env");
    if (env) p.set("environment", env);
    return p.toString();
  }

  function load() {
    fetch("/admin/live/analytics.json?" + currentQuery(), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) {
        setChartsState(null);
        var notice = document.getElementById("analytics-notice");
        if (notice) { notice.textContent = "Failed to load analytics: " + e; notice.hidden = false; }
      });
  }

  function init() {
    ["hours", "agent_kind", "channel", "status"].forEach(function (k) {
      var el = document.getElementById("filter-" + k);
      if (el) el.addEventListener("change", load);
    });
    var refresh = document.getElementById("analytics-refresh");
    if (refresh) refresh.addEventListener("click", load);
    // Re-render from the cached payload when the light/dark theme flips so
    // strokes, grids, and axis labels pick up the new token values.
    window.addEventListener("waj-theme-change", function () {
      if (lastData) render(lastData);
    });
    window.addEventListener("resize", function () {
      charts.forEach(function (c) {
        var el = c.root.parentElement;
        if (el) c.setSize({ width: el.clientWidth || 480, height: 200 });
      });
    });
    // The charts live in the collapsed "Technical details" section: fetch on
    // first open, not on every Overview load.
    var adv = document.getElementById("advanced");
    if (adv && !adv.open) {
      var onToggle = function () {
        if (!adv.open) return;
        adv.removeEventListener("toggle", onToggle);
        load();
      };
      adv.addEventListener("toggle", onToggle);
    } else {
      load();
    }
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
