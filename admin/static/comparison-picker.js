// Collect checked run ids from the recent-runs table and open the N-run
// comparison view. Enables the button only when >= 2 runs are selected, and
// only when they are all text runs or all voice checks — a voice check's
// call steps and a text suite's questions have nothing to line up against
// each other, so a mixed pick is disabled with the reason instead.
(function () {
  function picked() {
    return Array.prototype.slice.call(document.querySelectorAll(".cmp-pick:checked"));
  }
  function sync() {
    var els = picked();
    var n = els.length;
    var kinds = {};
    els.forEach(function (el) { kinds[el.getAttribute("data-kind") || "text"] = true; });
    var mixed = Object.keys(kinds).length > 1;
    var ok = n >= 2 && !mixed;
    var go = document.getElementById("cmp-go");
    var count = document.getElementById("cmp-count");
    if (count) {
      count.textContent = n === 0 ? "Tick two or more runs to compare them"
        : (n === 1 ? "1 selected — tick one more"
        : (mixed ? "A voice check can only be compared with other voice-check runs — untick the others"
        : n + " selected"));
      count.classList.toggle("text-warn", mixed);
    }
    if (go) {
      go.disabled = !ok;
      go.setAttribute("aria-disabled", ok ? "false" : "true");
      go.classList.toggle("primary", ok);
    }
  }
  document.addEventListener("change", function (e) {
    if (e.target && e.target.classList.contains("cmp-pick")) sync();
  });
  document.addEventListener("click", function (e) {
    if (e.target && e.target.id === "cmp-go" && !e.target.disabled) {
      // The table lists runs newest first, and the comparison reads its runs
      // as baseline -> latest. Sent in table order, ticking an old run and a
      // new one made the NEW run the baseline, so an improvement read as
      // "the change made things worse" and the reverse.
      var ids = picked().map(function (el) { return el.value; }).reverse();
      if (ids.length >= 2) {
        window.location.href = "/admin/evals/compare?runs=" + encodeURIComponent(ids.join(","));
      }
    }
  });
  sync();
})();
