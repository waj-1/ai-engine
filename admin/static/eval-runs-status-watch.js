/* eval-runs-status-watch.js — for screens whose recent-runs table is
 * virtualized (Tabulator) and can't be cell-patched reliably.
 *
 * Polls /admin/eval-runs/status.json and reloads the page ONCE when a run
 * that was in-flight at load time transitions to a terminal status. This
 * replaces the old every-5s setTimeout(location.reload) so the operator
 * isn't yanked back to the top of the page while reading — the page only
 * reloads when there's actually new settled data to show.
 *
 *   - Pauses while document.hidden; resumes on focus.
 *   - Stops polling once nothing is in-flight (any_inflight === false).
 */
(function () {
  "use strict";

  if (!document.querySelector("[data-eval-status-watch]")) return;

  var POLL_MS = 4000;
  var timer = null;
  var TERMINAL = { passed: 1, failed: 1, errored: 1, cancelled: 1 };

  // Snapshot of run_ids that were in-flight when the page loaded. We only
  // reload when one of THESE settles — avoids reloading for unrelated runs.
  var watching = {};
  var primed = false;

  function poll() {
    fetch("/admin/eval-runs/status.json", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        var runs = data.runs || [];
        if (!primed) {
          runs.forEach(function (run) {
            if (run.status === "queued" || run.status === "running") watching[run.run_id] = run.status;
          });
          primed = true;
        } else {
          var settled = runs.some(function (run) {
            return watching[run.run_id] && TERMINAL[run.status];
          });
          if (settled) { window.location.reload(); return; }
        }
        if (data.any_inflight) schedule();
      })
      .catch(function (err) {
        if (window.console) console.warn("eval-status-watch: fetch failed", err);
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

  poll();
})();
