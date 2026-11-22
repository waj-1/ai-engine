/* jobs-poll.js — replaces the <meta refresh content="5"> on /admin/jobs.
 *
 * The jobs table is Tabulator (virtualized), so instead of cell-patching
 * we poll each job that was in-flight at load time via /admin/jobs/{id}.json
 * and reload the page ONCE when any of them reaches a terminal status. This
 * keeps the operator from being yanked back to the top of the page every
 * 5s while reading, but still surfaces fresh results promptly.
 *
 *   - Polls every 4s while at least one watched job is still in-flight.
 *   - Pauses while document.hidden; resumes on focus.
 *   - A watched job that 404s (row gone) is dropped from the watch set.
 */
(function () {
  "use strict";

  var POLL_MS = 4000;
  var IN_FLIGHT = { queued: 1, running: 1 };

  var watch = [];
  document.querySelectorAll("tr[data-job-id]").forEach(function (tr) {
    if (IN_FLIGHT[tr.getAttribute("data-job-status")]) {
      watch.push(tr.getAttribute("data-job-id"));
    }
  });
  if (!watch.length) return;

  var timer = null;

  function tick() {
    Promise.all(watch.map(function (id) {
      return fetch("/admin/jobs/" + encodeURIComponent(id) + ".json",
                   { credentials: "same-origin", cache: "no-store" })
        .then(function (r) {
          if (r.status === 404) return { _gone: true, job_id: id };
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .catch(function () { return null; }); // transient — keep watching
    })).then(function (rows) {
      var settled = false;
      var stillWatching = [];
      rows.forEach(function (row, i) {
        var id = watch[i];
        if (!row) { stillWatching.push(id); return; }
        if (row._gone) return; // drop
        if (!IN_FLIGHT[row.status]) { settled = true; }
        else { stillWatching.push(id); }
      });
      if (settled) { window.location.reload(); return; }
      watch = stillWatching;
      if (watch.length) schedule();
    });
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    if (document.hidden) return;
    timer = setTimeout(tick, POLL_MS);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (timer) { clearTimeout(timer); timer = null; }
    } else {
      tick();
    }
  });

  schedule();
})();
