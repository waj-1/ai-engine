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
  if (root.getAttribute("data-inflight") !== "1") return;

  var runId = root.getAttribute("data-run-id");
  if (!runId) return;

  var POLL_MS = 4000;
  var timer = null;
  var TERMINAL = { passed: 1, failed: 1, errored: 1, cancelled: 1 };

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
