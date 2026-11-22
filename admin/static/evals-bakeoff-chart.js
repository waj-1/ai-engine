/* evals-bakeoff-chart.js — grouped-bar (mean score / pass rate) + cost-vs-
 * quality scatter for a bake-off detail page, fed by
 * GET /admin/evals/bakeoffs/{id}/data.json?suite_id=.
 *
 * Mirrors eval-runs-chart.js for Chart.js setup/theming (same `Chart`
 * global loaded deferred from a CDN in _base.html, same cssVar()/palette()
 * token resolution, same "fails soft when Chart.js isn't loaded" guard) and
 * eval-runs-poll.js for the poll-until-settled loop (fetch, patch, stop —
 * here stopping on status 'complete' OR 'errored' rather than
 * `any_inflight`, and re-rendering the charts on every tick instead of
 * patching table cells).
 *
 * The bake-off id + the suite focus currently applied come from data
 * attributes on the `#bakeoff-charts-mount` section (set server-side by
 * evals_bakeoff_detail.html from `bakeoff.bakeoff_id` / `suite_focus`).
 * Changing the suite-focus filter is a full page reload with `?suite_id=`
 * (Task 3.3), so this script only ever needs to read the attrs once at
 * load — no client-side re-filtering to wire up.
 */
(function () {
  "use strict";

  var mount = document.getElementById("bakeoff-charts-mount");
  if (!mount) return;

  var bakeoffId = mount.getAttribute("data-bakeoff-id");
  if (!bakeoffId) return;
  var suiteFocus = mount.getAttribute("data-suite-focus") || "";

  var barCanvas = document.getElementById("bakeoff-chart-bars");
  var scatterCanvas = document.getElementById("bakeoff-chart-scatter");
  var emptyEl = document.getElementById("bakeoff-charts-empty");
  var gridEl = document.getElementById("bakeoff-charts-grid");

  var POLL_MS = 4000;
  var TERMINAL = { complete: 1, errored: 1 };
  var timer = null;
  var barChart = null;
  var scatterChart = null;

  // --- theming (mirrors eval-runs-chart.js's cssVar/palette) --------------
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

  // --- data shaping --------------------------------------------------------
  // `matrix` is either the aggregated shape ({rows, by_variant}) or the
  // suite-focused shape ({rows: [...]}) — see admin/services/bakeoffs.py's
  // get_bakeoff(). Reduce both to one { variantLabel: metrics } map: prefer
  // by_variant when present (aggregated view), else fall back to rows
  // (each row is already one (suite, variant) pair — with a suite focus
  // applied there's exactly one row per variant).
  function variantMap(matrix) {
    var map = {};
    if (matrix && matrix.by_variant && Object.keys(matrix.by_variant).length) {
      return matrix.by_variant;
    }
    var rows = (matrix && matrix.rows) || [];
    rows.forEach(function (r) {
      if (r && r.variant_label) map[r.variant_label] = r;
    });
    return map;
  }

  function destroyCharts() {
    if (barChart) { barChart.destroy(); barChart = null; }
    if (scatterChart) { scatterChart.destroy(); scatterChart = null; }
  }

  function showEmpty(msg) {
    destroyCharts();
    if (barCanvas) barCanvas.style.display = "none";
    if (scatterCanvas) scatterCanvas.style.display = "none";
    if (gridEl) gridEl.style.display = "none";
    if (emptyEl) {
      emptyEl.style.display = "";
      emptyEl.textContent = msg || "No scored results yet — charts will render once scenarios report scores.";
    }
  }

  function showCharts() {
    if (barCanvas) barCanvas.style.display = "";
    if (scatterCanvas) scatterCanvas.style.display = "";
    if (gridEl) gridEl.style.display = "";
    if (emptyEl) emptyEl.style.display = "none";
  }

  function fmtPct(v) {
    return Math.round((v || 0) * 100) + "%";
  }

  function render(payload) {
    if (typeof Chart === "undefined") {
      if (window.console) console.warn("Chart.js not loaded; skipping bake-off charts.");
      return;
    }
    var matrix = (payload && payload.matrix) || {};
    var byVariant = variantMap(matrix);
    var labels = Object.keys(byVariant);

    if (!labels.length) {
      showEmpty();
      return;
    }
    showCharts();

    // --- grouped bar: mean_score + pass_rate per variant ---
    var scores = labels.map(function (l) { return byVariant[l].mean_score || 0; });
    var passRates = labels.map(function (l) { return byVariant[l].pass_rate || 0; });

    if (barCanvas) {
      var barCfg = {
        type: "bar",
        data: {
          labels: labels,
          datasets: [
            {
              label: "Score",
              data: scores,
              backgroundColor: colorFor(0),
            },
            {
              label: "Cases passed",
              data: passRates,
              backgroundColor: colorFor(4),
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: "bottom", labels: { font: { size: 11 } } },
            tooltip: {
              callbacks: {
                label: function (item) {
                  return (item.dataset.label || "") + ": " + fmtPct(item.parsed.y);
                },
              },
            },
          },
          scales: {
            y: {
              beginAtZero: true, max: 1,
              ticks: { font: { size: 10 }, callback: function (v) { return fmtPct(v); } },
            },
            x: { ticks: { font: { size: 11 } } },
          },
        },
      };
      if (barChart) barChart.destroy();
      barChart = new Chart(barCanvas.getContext("2d"), barCfg);
    }

    // --- cost-vs-quality scatter: one dataset (point) per variant so the
    // legend + tooltip label each point by variant name. ---
    if (scatterCanvas) {
      var datasets = labels.map(function (l, i) {
        var m = byVariant[l];
        var cost = m.mean_cost_usd;
        var score = m.mean_score;
        // An unrecorded cost is not $0 — leave that variant off the plot
        // (its legend entry says so) instead of pinning it to the y-axis.
        var unknown = (cost === null || cost === undefined || !m.priced_scenarios);
        return {
          label: unknown ? l + " (cost not recorded)" : l,
          data: unknown ? [] : [{ x: cost, y: score || 0 }],
          backgroundColor: colorFor(i),
          borderColor: colorFor(i),
          pointRadius: 6,
          pointHoverRadius: 8,
        };
      });
      var scatterCfg = {
        type: "scatter",
        data: { datasets: datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: "bottom", labels: { font: { size: 11 } } },
            tooltip: {
              callbacks: {
                label: function (item) {
                  var p = item.raw || {};
                  return (item.dataset.label || "") + ": $" + Number(p.x || 0).toFixed(4) +
                    " · " + fmtPct(p.y);
                },
              },
            },
          },
          scales: {
            x: {
              title: { display: true, text: "Cost per case (USD)" },
              ticks: { font: { size: 10 }, maxTicksLimit: 6, callback: function (v) { return "$" + Number(v).toPrecision(2); } },
            },
            y: {
              beginAtZero: true, max: 1,
              title: { display: true, text: "Score" },
              ticks: { font: { size: 10 }, callback: function (v) { return fmtPct(v); } },
            },
          },
        },
      };
      if (scatterChart) scatterChart.destroy();
      scatterChart = new Chart(scatterCanvas.getContext("2d"), scatterCfg);
    }
  }

  function load() {
    var url = "/admin/evals/bakeoffs/" + encodeURIComponent(bakeoffId) + "/data.json";
    if (suiteFocus) url += "?suite_id=" + encodeURIComponent(suiteFocus);
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        // The page (verdict, matrix, runs) is server-rendered: once a running
        // bake-off settles, reload once so all of it shows the final numbers.
        if (TERMINAL[data.status] && !TERMINAL[mount.getAttribute("data-status") || ""]) {
          window.location.reload();
          return;
        }
        render(data);
        if (!TERMINAL[data.status]) schedule();
      })
      .catch(function (err) {
        if (window.console) console.warn("evals-bakeoff-chart: fetch failed", err);
        schedule();
      });
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    if (document.hidden) return;
    timer = setTimeout(load, POLL_MS);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (timer) { clearTimeout(timer); timer = null; }
    } else {
      load();
    }
  });

  // Re-colour persisted charts when the light/dark toggle flips (mirrors
  // eval-runs-chart.js — cheap since the next poll tick will fully
  // re-render anyway, but this keeps the current frame in sync too).
  window.addEventListener("waj-theme-change", function () {
    if (barChart) {
      barChart.data.datasets.forEach(function (ds, i) {
        ds.backgroundColor = colorFor(i === 0 ? 0 : 4);
      });
      barChart.update();
    }
    if (scatterChart) {
      scatterChart.data.datasets.forEach(function (ds, i) {
        ds.backgroundColor = colorFor(i);
        ds.borderColor = colorFor(i);
      });
      scatterChart.update();
    }
  });

  // Chart.js is loaded with `defer` in _base.html, so it may not be defined
  // yet while this deferred script runs (mirrors eval_run_detail.html's
  // whenChartReady). Deferred scripts execute in document order before
  // DOMContentLoaded, so a short poll is enough belt-and-braces.
  function whenChartReady(cb) {
    if (typeof Chart !== "undefined") { cb(); return; }
    var tries = 0, done = false;
    function go() { if (!done) { done = true; cb(); } }
    var t = setInterval(function () {
      if (typeof Chart !== "undefined") { clearInterval(t); go(); }
      else if (tries++ > 100) {
        clearInterval(t);
        if (window.console) console.warn("Chart.js not loaded; skipping bake-off charts.");
      }
    }, 50);
  }

  whenChartReady(load);
})();
