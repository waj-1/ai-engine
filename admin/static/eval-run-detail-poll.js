/* eval-run-detail-poll.js — in-place live status for a single eval run.
 *
 * Replaces the old setTimeout(location.reload, 5000) on the run-detail
 * page. While the run is in-flight it patches the status chip + score chip
 * in place (no scroll jump / flash). When the run settles (terminal
 * status) it reloads ONCE so the per-scenario cards and Chart.js figures —
 * which need the full results payload — render.
 *
 *   - Polls /admin/eval-runs/{run_id}/status.json every 4s.
 *   - Pauses while document.hidden; resumes on focus.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-run-detail]");
  if (!root) return;
  var runId = root.getAttribute("data-run-id");
  var TERMINAL = { passed: 1, failed: 1, errored: 1, cancelled: 1 };

  // The public demo's guided tour (demo-tour.js) listens for these; on any
  // other install nothing does. builder-assistant.js (demo only) marks a
  // "Start run" submit in this tab, so a run counts as the visitor's own only
  // when this page is where that submit landed. Never throws.
  function tourEmit(name, detail) {
    try { window.dispatchEvent(new CustomEvent(name, { detail: detail })); } catch (e) { /* optional */ }
  }
  function tourEmitAfterLoad(name, detail) {
    // While the page's deferred scripts run, demo-tour.js may not be
    // listening yet: wait for DOMContentLoaded, which fires after all of them.
    if (document.currentScript && document.readyState !== "complete") {
      document.addEventListener("DOMContentLoaded", function () { tourEmit(name, detail); });
    } else {
      tourEmit(name, detail);
    }
  }
  try {
    var mark = null;
    try { mark = JSON.parse(sessionStorage.getItem("demo-tour:run-submit") || "null"); } catch (e) { mark = null; }
    // The form's page redirected here: the referrer is that page.
    var fromRef = "";
    try { fromRef = document.referrer ? new URL(document.referrer).pathname : ""; } catch (e) { fromRef = ""; }
    if (mark) { try { sessionStorage.removeItem("demo-tour:run-submit"); } catch (e) { /* blocked */ } }
    if (mark && runId && Date.now() - (mark.at || 0) < 2 * 60 * 1000 && (!mark.from || mark.from === fromRef)) {
      var started = { run_id: runId, href: location.pathname };
      window.__tourRunStarted = started;
      tourEmitAfterLoad("tour:run-started", started);
      var loadedStatus = (root.querySelector("[data-detail-status]") || {}).textContent || "";
      if (root.getAttribute("data-inflight") !== "1") {
        // It finished before this page loaded.
        var st = /pass/i.test(loadedStatus) ? "passed" : /fail/i.test(loadedStatus) ? "failed"
          : /cancel/i.test(loadedStatus) ? "cancelled" : "errored";
        tourEmitAfterLoad("tour:run-finished", { run_id: runId, status: st });
      }
    }
  } catch (e) { /* the tour is optional */ }

  if (root.getAttribute("data-inflight") !== "1") return;
  if (!runId) return;

  var POLL_MS = 4000;
  var timer = null;

  var LABELS = { passed: ["Passed", "ok"], failed: ["Failed", "err"], errored: ["Error", "err"],
                 running: ["Running", "info"], queued: ["Queued", "neutral"], cancelled: ["Cancelled", "warn"] };

  function patch(data) {
    var statusEl = root.querySelector("[data-detail-status]");
    if (statusEl) {
      var l = LABELS[data.status] || [String(data.status || ""), "neutral"];
      statusEl.innerHTML = '<span class="chip chip-' + l[1] + '"><span class="chip-dot"></span>' + l[0] + "</span>";
    }
    var scoreEl = root.querySelector("[data-detail-score]");
    if (scoreEl) {
      scoreEl.textContent = (data.score === null || data.score === undefined)
        ? "—" : Math.round(data.score * 100) + "%";
    }
  }

  function poll() {
    fetch("/admin/eval-runs/" + encodeURIComponent(runId) + "/status.json",
          { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (r.status === 404) throw new Error("gone");
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        patch(data);
        if (TERMINAL[data.status]) {
          tourEmit("tour:run-finished", { run_id: runId, status: data.status });
          // Settled — reload once so scenario cards + charts render.
          window.location.reload();
          return;
        }
        schedule();
      })
      .catch(function (err) {
        if (window.console) console.warn("eval-run-detail-poll: fetch failed", err);
        schedule();
      });
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    if (document.hidden) return;
    timer = setTimeout(poll, POLL_MS);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (timer) { clearTimeout(timer); timer = null; }
    } else {
      poll();
    }
  });

  schedule();
})();
