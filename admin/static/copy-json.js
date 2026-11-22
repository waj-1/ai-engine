/**
 * copy-json.js — dependency-free copy-to-clipboard for JSON <pre> blocks.
 *
 * Scans for <pre> elements with class "ox-pre" or "copyable" and injects a
 * small "Copy" button positioned at the top-right of each block.
 *
 * window.attachCopyButtons(root) can be called any time after dynamic content
 * is injected (e.g. after fetch-driven renders) to wire up newly-added blocks.
 * Already-wired nodes are skipped (idempotent via data-copy-wired attribute).
 */
(function () {
  "use strict";

  var SELECTOR = "pre.ox-pre, pre.copyable";

  function createBtn() {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy to clipboard");
    btn.style.cssText = [
      "position:absolute",
      "top:6px",
      "right:6px",
      "padding:2px 8px",
      "font-size:10px",
      "line-height:1.6",
      "font-family:inherit",
      "background:var(--surface,#fff)",
      "color:var(--fg-2,#555)",
      "border:1px solid var(--border,#d1d5db)",
      "border-radius:4px",
      "cursor:pointer",
      "opacity:0",
      "transition:opacity 0.15s ease",
      "z-index:2",
    ].join(";");
    return btn;
  }

  function wire(pre) {
    if (pre.dataset.copyWired) return;
    pre.dataset.copyWired = "1";

    // Ensure the pre has relative positioning so the button anchors correctly.
    var pos = window.getComputedStyle(pre).position;
    if (pos === "static") pre.style.position = "relative";

    var btn = createBtn();

    // Show/hide button on hover of the container.
    pre.addEventListener("mouseenter", function () { btn.style.opacity = "1"; });
    pre.addEventListener("mouseleave", function () { btn.style.opacity = "0"; });

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var text = pre.textContent || "";
      if (!navigator.clipboard) {
        // Fallback for non-HTTPS / older browsers.
        try {
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.style.cssText = "position:fixed;opacity:0;";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
          flash(btn);
        } catch (_) { /* silent */ }
        return;
      }
      navigator.clipboard.writeText(text).then(function () {
        flash(btn);
      }).catch(function () { /* silent — clipboard denied */ });
    });

    pre.appendChild(btn);
  }

  function flash(btn) {
    var prev = btn.textContent;
    btn.textContent = "Copied!";
    btn.style.opacity = "1";
    setTimeout(function () {
      btn.textContent = prev;
      // Let hover state re-govern visibility.
    }, 1500);
  }

  /** Public API: (re)scan a subtree and wire any unwired blocks. */
  window.attachCopyButtons = function (root) {
    var scope = root || document;
    scope.querySelectorAll(SELECTOR).forEach(wire);
  };

  // Auto-wire on initial load.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      window.attachCopyButtons();
    });
  } else {
    window.attachCopyButtons();
  }
})();
