/* eval-runs-poll.js — in-place live updates for the Evaluation Runs screen.
 *
 * Replaces the old `setTimeout(location.reload, 5000)` (which scroll-jumped
 * and flashed) with a fetch-poll that patches only the status chip, score,
 * and pass-rate cells of in-flight rows.
 *
 * Behavior:
 *   - Poll /admin/eval-runs/status.json every 4s while something is
 *     queued/running. Stop once `any_inflight` is false.
 *   - Pause while document.hidden; resume (and poll immediately) on focus.
 *   - When a row flips from in-flight to terminal we patch the cells in
 *     place — no reload, no scroll jump.
 */
(function () {
  "use strict";

  var POLL_MS = 4000;
  var tableBody = document.querySelector("[data-eval-runs-body]");
  if (!tableBody) return;

  var timer = null;

  // Same vocabulary as admin/testing_view.py (Passed · Failed · Error ·
  // Running · Queued · Cancelled).
  var LABELS = { passed: ["Passed", "ok"], failed: ["Failed", "err"], errored: ["Error", "err"],
                 running: ["Running", "info"], queued: ["Queued", "neutral"], cancelled: ["Cancelled", "warn"] };

  function chipHtml(status) {
    var l = LABELS[status] || [String(status || ""), "neutral"];
    return '<span class="chip chip-' + l[1] + '"><span class="chip-dot"></span>' + l[0] + "</span>";
  }

  function fmtPct(v) {
    if (v === null || v === undefined) return "—";
    return Math.round(v * 100) + "%";
  }

  function patchRow(run) {
    var row = tableBody.querySelector('tr[data-run-id="' + run.run_id + '"]');
    if (!row) return;
    var statusCell = row.querySelector("[data-cell-status]");
    var scoreCell = row.querySelector("[data-cell-score]");
    if (statusCell) statusCell.innerHTML = chipHtml(run.status);
    if (scoreCell && run.score !== null && run.score !== undefined) scoreCell.textContent = fmtPct(run.score);
    // Settled: the cases column / change vs previous need the full row —
    // mark it so a refresh is offered rather than showing stale numbers.
    if (run.status !== "queued" && run.status !== "running") {
      if (row.getAttribute("data-inflight") === "1") {
        // The demo's guided tour listens for this; a no-op elsewhere.
        try {
          window.dispatchEvent(new CustomEvent("tour:run-finished", { detail: { run_id: run.run_id, status: run.status } }));
        } catch (e) { /* optional */ }
      }
      row.setAttribute("data-inflight", "0");
    }
  }

  function poll() {
    fetch("/admin/eval-runs/status.json", { credentials: "same-origin", cache: "no-store" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        (data.runs || []).forEach(patchRow);
        if (data.any_inflight) {
          schedule();
        }
      })
      .catch(function (err) {
        // eslint-disable-next-line no-console
        if (window.console) console.warn("eval-runs-poll: fetch failed", err);
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

  // Only start if at least one row is in-flight at load time.
  var inflight = tableBody.querySelector('tr[data-inflight="1"]');
  if (inflight) poll();
})();
