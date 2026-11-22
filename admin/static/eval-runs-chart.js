/* eval-runs-chart.js — pass-rate / score trend for the Evaluation Runs
 * screen. Fetches /admin/eval-runs/report.json and renders a line+scatter
 * chart grouped by prompt_version so prompt-A/B comparisons are visual.
 *
 * Each prompt_version becomes its own coloured series. The suite's
 * pass_threshold (when the filtered set shares one) is drawn as a dashed
 * reference line. Fails soft when Chart.js isn't loaded.
 *
 * We use a category x-axis of short date-time labels rather than Chart.js's
 * "time" scale, because the date-fns adapter isn't vendored — this keeps
 * the chart dependency-free.
 *
 * Shared by both eval-tab trend cards (this script is a singleton per page
 * load — only one `[data-eval-trend]` section exists on a given screen).
 * The Production-monitoring tab (Task 2.5) reuses it unmodified for the
 * scheduled-runs trend by marking its section `data-scheduled-only="1"` (or
 * `data-schedule-id="<uuid>"` for one schedule) — `load()` below forwards
 * whichever is present to report.json. Tab 1's trend section carries
 * neither attribute, so its request is byte-for-byte what it was pre-2.5.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-eval-trend]");
  if (!root) return;
  var canvas = document.getElementById("eval-trend-chart");
  if (!canvas) return;

  // Series palette resolved from theme tokens where an analog exists
  // (accent / success / warning / error), with fixed categorical fillers
  // (indigo, rust, purple) that read fine on both light and dark surfaces.
  // Re-resolved on every render and on waj-theme-change so prompt-version
  // series follow the light/dark toggle.
  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }
  function palette() {
    var dark = document.documentElement.dataset.theme === "dark";
    var fillers = dark ? ["#8C9BE0", "#D97A4A", "#A07CC8"] : ["#5B6CB0", "#A4471F", "#6A4C93"];
    return [
      cssVar("--accent", "#006A6A"),
      cssVar("--error", "#8E4957"),
      cssVar("--warning", "#B59B00"),
      fillers[0],
      cssVar("--success", "#2E7D32"),
      fillers[1],
      fillers[2],
    ];
  }
  function colorFor(idx) {
    var p = palette();
    return p[idx % p.length];
  }

  var chart = null;

  function metricKey() {
    var sel = document.getElementById("eval-trend-metric");
    return sel ? sel.value : "score";
  }

  function shortLabel(iso) {
    if (!iso) return "";
    // 2026-05-21T14:03 → "05-21 14:03"
    var s = String(iso);
    return s.slice(5, 10) + " " + s.slice(11, 16);
  }

  function render(payload) {
    if (typeof Chart === "undefined") {
      if (window.console) console.warn("Chart.js not loaded; skipping eval trend chart.");
      return;
    }
    var points = (payload && payload.points) || [];
    var empty = document.getElementById("eval-trend-empty");
    if (!points.length) {
      if (empty) empty.style.display = "";
      canvas.style.display = "none";
      if (chart) { chart.destroy(); chart = null; }
      return;
    }
    if (empty) empty.style.display = "none";
    canvas.style.display = "";

    var metric = metricKey(); // "score" | "pass_rate"

    // Shared, sorted set of x labels (one per run, chronological).
    var labels = points.map(function (p) { return shortLabel(p.created_at); });

    // Group into one dataset per prompt_version. Each dataset aligns to the
    // shared label index so series share an x-axis.
    var groups = {};
    var order = [];
    points.forEach(function (p, i) {
      var key = p.prompt_version || "baseline";
      if (!groups[key]) { groups[key] = new Array(points.length).fill(null); order.push(key); }
      var y = metric === "pass_rate" ? p.pass_rate : p.score;
      groups[key][i] = (y === null || y === undefined) ? null : y;
    });

    var datasets = order.map(function (key, i) {
      return {
        label: key === "baseline" ? "Current prompt" : (key === "override" ? "Custom prompt" : "Prompt " + key),
        data: groups[key],
        borderColor: colorFor(i),
        backgroundColor: colorFor(i),
        pointRadius: 4,
        pointHoverRadius: 6,
        tension: 0.2,
        fill: false,
        spanGaps: true,
      };
    });

    var threshold = payload.pass_threshold;
    var thresholdPlugin = {
      id: "evalThreshold",
      afterDatasetsDraw: function (c) {
        if (threshold === null || threshold === undefined) return;
        var y = c.scales.y, x = c.scales.x;
        if (!y || !x) return;
        var yPx = y.getPixelForValue(threshold);
        var cx = c.ctx;
        // Resolved at draw time so the line follows theme toggles.
        var err = cssVar("--error", "#DC2626");
        cx.save();
        cx.strokeStyle = err;
        cx.setLineDash([5, 4]);
        cx.lineWidth = 1.5;
        cx.beginPath();
        cx.moveTo(x.left, yPx);
        cx.lineTo(x.right, yPx);
        cx.stroke();
        cx.fillStyle = err;
        cx.font = "10px Inter, sans-serif";
        cx.fillText("pass threshold " + threshold, x.left + 6, yPx - 4);
        cx.restore();
      },
    };

    var cfg = {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "bottom", labels: { font: { size: 11 } } },
          tooltip: {
            callbacks: {
              label: function (item) {
                var p = points[item.dataIndex] || {};
                var v = item.parsed.y == null ? "—" : Math.round(item.parsed.y * 100) + "%";
                return (item.dataset.label || "") + " · " + (p.suite_name || "") + " · " + v +
                  " · " + (p.status || "");
              },
            },
          },
        },
        scales: {
          y: {
            beginAtZero: true, max: 1,
            ticks: {
              font: { size: 10 },
              callback: function (v) { return Math.round(v * 100) + "%"; },
            },
            title: { display: true, text: metric === "pass_rate" ? "Pass rate" : "Score" },
          },
          x: { ticks: { font: { size: 9 }, maxRotation: 45, minRotation: 0 } },
        },
      },
      plugins: [thresholdPlugin],
    };

    if (chart) chart.destroy();
    chart = new Chart(canvas.getContext("2d"), cfg);
  }

  function load() {
    var suiteSel = document.getElementById("eval-trend-suite");
    var daysSel = document.getElementById("eval-trend-days");
    var suite = suiteSel ? suiteSel.value : "";
    var days = daysSel ? daysSel.value : "30";
    var url = "/admin/eval-runs/report.json?days=" + encodeURIComponent(days) +
      (suite ? "&suite_id=" + encodeURIComponent(suite) : "");
    // Task 2.5: narrow to scheduled runs when the trend section opts in via
    // a data attribute — see the file header comment above.
    if (root.dataset.scheduleId) {
      url += "&schedule_id=" + encodeURIComponent(root.dataset.scheduleId);
    } else if (root.dataset.scheduledOnly) {
      url += "&scheduled_only=1";
    }
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(render)
      .catch(function (err) { if (window.console) console.warn("eval-runs-chart: fetch failed", err); });
  }

  ["eval-trend-suite", "eval-trend-days", "eval-trend-metric"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("change", load);
  });

  // Re-colour the persisted chart when the light/dark toggle flips. The
  // threshold plugin reads --error at draw time, so .update() covers it.
  window.addEventListener("waj-theme-change", function () {
    if (!chart) return;
    chart.data.datasets.forEach(function (ds, i) {
      ds.borderColor = colorFor(i);
      ds.backgroundColor = colorFor(i);
    });
    chart.update();
  });

  load();
})();
