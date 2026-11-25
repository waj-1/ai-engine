/* admin/static/mobile.js: small-screen behaviour for the portal shell.
 *
 * Loaded on every portal page (_base.html). It does nothing visible on a
 * desktop: everything it toggles is styled only under the phone breakpoint in
 * portal.css (search "Small screens").
 *
 * 1. The navigation drawer: the top bar's menu button (#nav-menu) opens the
 *    sidebar as an off-canvas drawer (html[data-nav-open]); the backdrop,
 *    Escape, following a link and the tour's "Getting started" close it.
 * 2. The visible viewport: --vvh and --vv-top on <html> follow
 *    window.visualViewport, so a full-screen sheet (the Build with AI panel,
 *    the Test chat, the search palette) can size itself to the part of the
 *    screen the on-screen keyboard leaves free. iOS Safari does not shrink the
 *    layout viewport for the keyboard, so 100dvh alone would put the composer
 *    behind it.
 * 3. Wide content: a table or code block wider than its column gets a
 *    horizontal scroll container (.table-wrap / .scroll-x) so it scrolls on
 *    its own instead of widening the whole page. Re-run after htmx swaps.
 * 4. The Build with AI sheet covers the whole screen on a phone, so following
 *    a link inside it (a step's "Open ...") closes it first, through the
 *    panel's own close button, so the page the visitor asked for is visible.
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var PHONE = window.matchMedia("(max-width: 720px)");

  // -- 1. navigation drawer --------------------------------------------------
  function setNav(open) {
    var btn = document.getElementById("nav-menu");
    if (open) root.dataset.navOpen = "1"; else delete root.dataset.navOpen;
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      var first = document.querySelector("#app-nav .nav-link");
      if (first) first.focus({ preventScroll: true });
    }
  }
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.closest("#nav-menu")) { setNav(!root.dataset.navOpen); return; }
    if (t.closest("#nav-backdrop")) { setNav(false); return; }
    // A link, or the guided tour's "Getting started" (it opens the checklist).
    if (root.dataset.navOpen && t.closest("#app-nav a, #app-nav #dt-entry")) setNav(false);
    // -- 4. a link inside the assistant sheet on a phone --------------------
    if (PHONE.matches) {
      var a = t.closest("#ba-panel a[href]");
      if (a && a.getAttribute("target") !== "_blank") {
        var close = document.getElementById("ba-close");
        if (close) close.click();
      }
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && root.dataset.navOpen) {
      setNav(false);
      var btn = document.getElementById("nav-menu");
      if (btn) btn.focus();
    }
  });
  PHONE.addEventListener("change", function (e) { if (!e.matches) setNav(false); });

  // -- 2. visible viewport ----------------------------------------------------
  var vv = window.visualViewport;
  function syncViewport() {
    var h = vv ? vv.height : window.innerHeight;
    var top = vv ? vv.offsetTop : 0;
    root.style.setProperty("--vvh", Math.round(h) + "px");
    root.style.setProperty("--vv-top", Math.round(top) + "px");
  }
  syncViewport();
  if (vv) {
    vv.addEventListener("resize", syncViewport);
    vv.addEventListener("scroll", syncViewport);
  } else {
    window.addEventListener("resize", syncViewport);
  }

  // -- 3. wide tables and code blocks ------------------------------------------
  var SCROLLERS = ".table-wrap, .table-scroll, .scroll-x, .tabulator-host, .tabulator-tableholder, .diff-block, .ba-panel, #waj-test-chat-root";
  function wrapWide(scope) {
    if (!PHONE.matches) return;
    var nodes = (scope || document).querySelectorAll(".app-content table, .app-content pre");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (el.closest(SCROLLERS) || el.closest(".tabulator")) continue;
      var parent = el.parentElement;
      if (!parent || el.scrollWidth <= parent.clientWidth + 1) continue;
      if (el.tagName === "PRE") { el.classList.add("scroll-x"); continue; }
      var wrap = document.createElement("div");
      wrap.className = "table-wrap";
      parent.insertBefore(wrap, el);
      wrap.appendChild(el);
    }
  }
  function onReady() { wrapWide(document); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", onReady);
  else onReady();
  window.addEventListener("load", onReady);
  document.addEventListener("htmx:afterSwap", function (e) { wrapWide(e.target); });
})();
