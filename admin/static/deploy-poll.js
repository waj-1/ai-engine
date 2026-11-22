/* deploy-poll.js — populates the "Recent deploys" table on
 * /admin/deployments and polls /admin/deploy-runs.json every 5s.
 *
 * Behavior:
 *   - On load: fetch once, render rows.
 *   - Then poll every 5s while the tab is visible. Pause on
 *     document.visibilitychange when hidden; resume on visible.
 *   - When any in-flight run transitions to a terminal conclusion
 *     (success / failure / cancelled), trigger location.reload() so
 *     the registered-targets card and audit links pick up fresh data.
 *   - On HTTP error: back off to 30s for the next attempt, then return
 *     to the normal 5s cadence once a poll succeeds. Keeps a flaky
 *     GitHub API from hammering the agent service.
 *   - No external dependencies. Tested against vanilla Tabulator-free
 *     rendering — the table is plain <tr>s and we let portal-table.js
 *     pick it up via the data-tabulator attribute.
 */
(function () {
  "use strict";

  var POLL_MS = 5000;
  var BACKOFF_MS = 30000;

  var root = document.querySelector("[data-deploy-monitor]");
  if (!root) return;
  var env = root.getAttribute("data-deploy-env") || "";
  var tbody = root.querySelector("[data-deploy-runs]");
  var fetchedEl = root.querySelector("[data-deploy-fetched]");
  var emptyHint = document.getElementById("deploy-empty-hint");
  var inflightChip = document.querySelector("[data-deploy-inflight]");

  var lastRunStates = {}; // run_id -> conclusion
  var timer = null;
  var inErrorBackoff = false;

  function chip(label, variant, dot) {
    var dotEl = dot ? '<span class="chip-dot"></span>' : "";
    return (
      '<span class="chip chip-' + variant + '">' + dotEl + label + "</span>"
    );
  }

  function statusChip(run) {
    if (run.status === "queued") return chip("queued", "ghost", true);
    if (run.status === "in_progress") return chip("running", "warn", true);
    if (run.conclusion === "success") return chip("success", "ok", true);
    if (run.conclusion === "failure") return chip("failure", "err", true);
    if (run.conclusion === "cancelled") return chip("cancelled", "ghost", false);
    return chip(run.conclusion || run.status || "unknown", "ghost", false);
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      return d.toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  function fmtDuration(s) {
    if (s === null || s === undefined) return "—";
    if (s < 60) return s + "s";
    var m = Math.floor(s / 60);
    var r = s % 60;
    return m + "m " + r + "s";
  }

  function commitHref(sha) {
    if (!sha) return null;
    return "https://github.com/example-org/ai-engine/commit/" + sha;
  }

  function rowHtml(r) {
    var ref = r.head_branch || "—";
    var sha = r.head_sha_short || "";
    var refCell = sha
      ? '<code>' + ref + '</code> · <a href="' + commitHref(r.head_sha) + '" target="_blank" rel="noopener">' + sha + "</a>"
      : "<code>" + ref + "</code>";
    var logs = r.html_url
      ? '<a href="' + r.html_url + '" target="_blank" rel="noopener">View run →</a>'
      : "—";
    return (
      "<tr>" +
      "<td>" + fmtTime(r.created_at) + "</td>" +
      "<td>" + (r.target_env || "—") + "</td>" +
      "<td><code>" + (r.workflow_label || "deploy") + "</code></td>" +
      "<td>" + refCell + "</td>" +
      "<td>" + (r.actor || "—") + "</td>" +
      "<td>" + statusChip(r) + "</td>" +
      "<td>" + fmtDuration(r.duration_s) + "</td>" +
      "<td>" + logs + "</td>" +
      "</tr>"
    );
  }

  function detectTransitions(runs) {
    var transitioned = false;
    runs.forEach(function (r) {
      var key = String(r.run_id);
      var prev = lastRunStates[key];
      // A row that previously was queued/in_progress and is now terminal
      // means a deploy finished while the tab was open — refresh.
      if (
        prev !== undefined &&
        (prev === "in_progress" || prev === "queued") &&
        r.status === "completed"
      ) {
        transitioned = true;
      }
      lastRunStates[key] = r.status === "completed" ? r.conclusion : r.status;
    });
    return transitioned;
  }

  function updateInflightChip(runs) {
    if (!inflightChip) return;
    var live = runs.filter(function (r) {
      return r.status === "in_progress" || r.status === "queued";
    });
    if (!live.length) {
      inflightChip.style.display = "none";
      inflightChip.textContent = "";
      return;
    }
    var r = live[0];
    var started = r.created_at ? new Date(r.created_at) : null;
    var ago = started ? Math.max(0, Math.round((Date.now() - started.getTime()) / 60000)) : null;
    var agoStr = ago === null ? "" : " · started " + ago + "m ago";
    inflightChip.style.display = "";
    inflightChip.innerHTML =
      'Deploy in progress' + agoStr +
      ' · <a href="' + (r.html_url || "#") + '" target="_blank" rel="noopener">view live logs →</a>';
  }

  function showHint(text) {
    if (!emptyHint) return;
    emptyHint.textContent = text || "";
    emptyHint.hidden = !text;
  }

  function render(payload) {
    var runs = (payload && payload.runs) || [];
    if (!runs.length && payload && payload.error) {
      // The endpoint answered but could not list runs (e.g. no GitHub token):
      // say so instead of an empty "no runs" that reads like a clean history.
      var msg = String(payload.error).replace(/[<>&]/g, "");
      tbody.innerHTML =
        '<tr data-state="error"><td colspan="8">Deploy history is unavailable.</td></tr>';
      showHint(msg);
    } else if (!runs.length) {
      tbody.innerHTML =
        '<tr data-state="empty"><td colspan="8" class="caption">No deploys or rollbacks yet.</td></tr>';
      showHint("");
    } else {
      showHint("");
      tbody.innerHTML = runs.map(rowHtml).join("");
    }
    if (fetchedEl) {
      fetchedEl.textContent = "Updated " + new Date().toLocaleTimeString();
    }
    updateInflightChip(runs);
    var transitioned = detectTransitions(runs);
    if (transitioned) {
      // A run finished while we watched — reload so any side effects
      // (new commit on production, fresh audit row) appear.
      window.location.reload();
    }
  }

  function poll() {
    var url = "/admin/deploy-runs.json" + (env ? "?env=" + encodeURIComponent(env) : "");
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        inErrorBackoff = false;
        render(data);
        schedule(POLL_MS);
      })
      .catch(function (err) {
        if (fetchedEl) fetchedEl.textContent = "Fetch failed — retrying";
        // Replace the "Fetching recent runs…" row so the table shows an
        // error state rather than loading forever.
        if (!Object.keys(lastRunStates).length) {
          tbody.innerHTML =
            '<tr data-state="error"><td colspan="8">Couldn\'t load deploy history. Retrying in 30 seconds.</td></tr>';
        }
        // Back off to 30s so transient GH API hiccups don't spam.
        inErrorBackoff = true;
        schedule(BACKOFF_MS);
        // eslint-disable-next-line no-console
        console.warn("deploy-poll: fetch failed", err);
      });
  }

  function schedule(ms) {
    if (timer) clearTimeout(timer);
    if (document.hidden) return; // resume on visibilitychange
    timer = setTimeout(poll, ms);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (timer) clearTimeout(timer);
      timer = null;
    } else {
      // Refresh immediately on regain focus so the operator doesn't
      // stare at stale "running" rows that already finished.
      poll();
    }
  });

  // Kick off.
  poll();
})();
