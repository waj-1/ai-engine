"""The Build with AI panel's start-up and call-time link behaviour
(admin/static/builder-assistant.js), run in Node against a minimal document.
Skipped where Node is not installed.

What these pin:

1. No auto-open on a phone. The panel opens by itself on a desktop's first
   visit; under 640px (where it is a full-screen sheet) it stays closed and a
   small hint shows by the launcher instead. A phone that left the panel open
   gets it back open.
2. Links during a voice call. While a call is open, a link in the panel gets
   target=_blank before mobile.js reads it (capture phase), so following it
   neither ends the call nor closes the sheet. With no call, links are left
   as they are. A tab opened that way starts closed on a phone, without
   changing how the calling tab left the panel.
3. The breakpoint the script tests is the one the stylesheet makes the sheet
   at, and mobile.js still leaves target=_blank links alone.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "admin" / "static"
JS = STATIC / "builder-assistant.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const cfg = JSON.parse(fs.readFileSync(0, "utf8"));
function El(id, tag) {
  this.id = id; this.tag = tag || "div"; this.hidden = false; this.attrs = {}; this.children = [];
  this.listeners = []; this.cls = new Set(); this.disabled = false; this.style = {}; this.innerHTML = "";
  const self = this;
  this.classList = {
    toggle(c, on) { (on === undefined ? !self.cls.has(c) : on) ? self.cls.add(c) : self.cls.delete(c); },
    add(c) { self.cls.add(c); }, remove(c) { self.cls.delete(c); }, contains(c) { return self.cls.has(c); } };
}
El.prototype = {
  addEventListener(type, fn, capture) { this.listeners.push({ type, fn, capture: !!capture }); },
  setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
  appendChild(c) { this.children.push(c); return c; },
  set textContent(v) { this.children = []; this.text = v; }, get textContent() { return ""; },
  querySelector(s) { return s === "[data-hint-close]" ? els["hint-close"] : null; },
  contains(x) { return x === this || x.inside === this; }, closest() { return null; }, focus() {}, requestSubmit() {},
};
const ids = ["ba-panel", "ba-launcher", "ba-log", "ba-form", "ba-input", "ba-send", "ba-chips", "ba-close", "ba-new",
             "ba-hint", "hint-close", "ba-voice", "ba-voice-status", "ba-voice-log", "ba-mic"];
const els = {}; ids.forEach((i) => { els[i] = new El(i); });
els["ba-panel"].hidden = true; els["ba-launcher"].hidden = true; els["ba-hint"].hidden = true;
els["ba-mic"].attrs = { "data-voice-src": "/v.js", "data-call-src": "/c.js" };
const store = Object.assign({}, cfg.storage);
if (cfg.tour) els["demo-tour-config"] = new El("demo-tour-config", "script");
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; } };
const made = [];
const doc = { getElementById: (i) => els[i] || null, createElement: (t) => { const e = new El(null, t); made.push(e); return e; },
  createTextNode: () => new El(null, "#text"), head: new El("head"), body: new El("body"), addEventListener() {} };
let lastCall = null;
const win = {
  matchMedia: (q) => ({ matches: q === "(max-width: 640px)" ? cfg.phone : false, media: q, addEventListener() {} }),
  addEventListener() {}, location: { protocol: "http:", host: "x" },
  performance: { getEntriesByType: () => [{ type: cfg.reload ? "reload" : "navigate" }] },
  VoiceLiveClient: function (opts) { this.opts = opts; lastCall = this; this.connect = () => opts.onState("listening");
    this.disconnect = () => opts.onState("closed"); },
  VoiceCallTranscript: () => ({ handle() {} }),
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), {
  window: win, document: doc, localStorage, sessionStorage: localStorage,
  location: { pathname: "/admin/agents", search: "" },
  setTimeout, clearTimeout, fetch: () => Promise.reject(new Error("no network")), TextDecoder, console,
});
function clickLink() {
  const a = new El(null, "a"); a.attrs.href = "/admin/agents?profile_id=p1"; a.inside = els["ba-panel"];
  a.closest = (s) => (s === "a[href]" ? a : null);
  const ev = { target: a, preventDefault() {} };
  els["ba-panel"].listeners.filter((l) => l.type === "click" && l.capture).forEach((l) => l.fn(ev));
  return a.getAttribute("target");
}
const out = { buttons: made.filter((e) => e.tag === "button").map((e) => e.text),
  captions: made.filter((e) => e.cls.has("caption")).map((e) => e.text),
  panelOpen: !els["ba-panel"].hidden, launcherShown: !els["ba-launcher"].hidden,
  hintShown: !els["ba-hint"].hidden, nudging: els["ba-launcher"].cls.has("ba-launcher-nudge") };
out.linkTargetNoCall = clickLink();
els["ba-mic"].listeners.filter((l) => l.type === "click").forEach((l) => l.fn({}));
out.micPressed = els["ba-mic"].getAttribute("aria-pressed");
out.linkTargetInCall = clickLink();
out.fromCallMarked = Date.now() - Number(store["demo-assistant:from-call"] || 0) < 5000;
lastCall.disconnect();
out.linkTargetAfterCall = clickLink();
out.storage = store;
process.stdout.write(JSON.stringify(out));
"""


def _run(*, phone: bool, storage: dict | None = None, tour: bool = False, reload: bool = False) -> dict:
    done = subprocess.run(
        ["node", "-e", _HARNESS, str(JS)],
        input=json.dumps({"phone": phone, "storage": storage or {}, "tour": tour, "reload": reload}),
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_a_desktop_first_visit_opens_the_panel():
    out = _run(phone=False)
    assert out["panelOpen"] and not out["launcherShown"]
    assert not out["hintShown"]
    assert out["storage"]["demo-assistant:open"] == "1"


@needs_node
def test_a_phone_first_visit_keeps_the_sheet_closed_and_shows_the_hint():
    out = _run(phone=True)
    assert not out["panelOpen"] and out["launcherShown"]
    assert out["hintShown"] and out["nudging"]
    assert out["storage"]["demo-assistant:seen"] == "1"
    # Not saved as "closed": the visitor did not close it.
    assert "demo-assistant:open" not in out["storage"]


@needs_node
@pytest.mark.parametrize("phone", [True, False])
def test_with_the_tour_on_a_first_visit_stays_closed_without_a_hint(phone):
    """The guided tour's Welcome comes first, on a desktop and on a phone."""
    out = _run(phone=phone, tour=True)
    assert not out["panelOpen"] and out["launcherShown"]
    assert not out["hintShown"] and not out["nudging"]
    # Later pages: as the visitor (or the tour) left it.
    out = _run(phone=phone, tour=True, storage={"demo-assistant:seen": "1", "demo-assistant:open": "1"})
    assert out["panelOpen"]


@needs_node
def test_with_the_tour_on_a_tab_opened_from_a_call_link_starts_closed_on_a_phone():
    import time
    out = _run(phone=True, tour=True, storage={"demo-assistant:seen": "1", "demo-assistant:open": "1",
                                               "demo-assistant:from-call": str(int(time.time() * 1000))})
    assert not out["panelOpen"]
    assert out["storage"]["demo-assistant:open"] == "1"


@needs_node
def test_a_phone_that_left_the_panel_open_gets_it_back():
    out = _run(phone=True, storage={"demo-assistant:seen": "1", "demo-assistant:open": "1"})
    assert out["panelOpen"] and not out["hintShown"]


@needs_node
@pytest.mark.parametrize("phone", [True, False])
def test_links_open_in_a_new_tab_only_while_a_call_is_open(phone):
    out = _run(phone=phone, storage={"demo-assistant:seen": "1", "demo-assistant:open": "1"})
    assert out["linkTargetNoCall"] is None
    assert out["micPressed"] == "true"
    assert out["linkTargetInCall"] == "_blank"
    assert out["fromCallMarked"]


@needs_node
def test_a_tab_opened_from_a_call_link_starts_closed_on_a_phone():
    import time
    fresh = str(int(time.time() * 1000))
    out = _run(phone=True, storage={"demo-assistant:seen": "1", "demo-assistant:open": "1",
                                    "demo-assistant:from-call": fresh})
    assert not out["panelOpen"] and not out["hintShown"]
    # The calling tab's choice is kept for its next page.
    assert out["storage"]["demo-assistant:open"] == "1"
    # An old mark does not count.
    out = _run(phone=True, storage={"demo-assistant:seen": "1", "demo-assistant:open": "1",
                                    "demo-assistant:from-call": "1000"})
    assert out["panelOpen"]


def test_the_sheet_breakpoint_matches_the_stylesheet():
    src = JS.read_text()
    query = re.search(r'var SHEET = window\.matchMedia\("([^"]+)"\)', src).group(1)
    css = (STATIC / "portal.css").read_text()
    # The rule that turns the panel into a full-screen sheet.
    assert re.search(r"@media " + re.escape(query) + r" \{\s*\.ba-panel \{[^}]*height: var\(--vvh", css)


def test_mobile_js_leaves_new_tab_links_open_in_the_sheet():
    src = (STATIC / "mobile.js").read_text()
    assert 'a.getAttribute("target") !== "_blank"' in src
    # and it reads the target in the bubble phase, after the panel's capture listener
    assert 'document.addEventListener("click", function (e) {' in src


def _top_level_rules(css: str) -> list[str]:
    """Selectors of the rules outside any @media block."""
    out, depth, buf = [], 0, ""
    for ch in re.sub(r"/\*.*?\*/", "", css, flags=re.S):
        if ch == "{":
            if depth == 0:
                out.append(buf.strip())
            depth += 1
            buf = ""
        elif ch == "}":
            depth -= 1
            buf = ""
        elif depth == 0:
            buf += ch
    return out


def test_the_launchers_stack_on_a_desktop_too():
    """Follow-up 3: the Build with AI launcher sits above the Test chat's at
    every width, not only in the phone block, and above a save bar."""
    rules = _top_level_rules((STATIC / "portal.css").read_text())
    assert "body:has(#waj-test-chat-root) .ba-launcher" in rules
    assert "body:has([data-savebar]):has(#waj-test-chat-root) .ba-launcher" in rules
    assert "body:has([data-savebar]) .ba-launcher" in rules


def test_the_tour_stylesheet_leaves_the_launchers_where_portal_css_puts_them():
    """demo-tour.css loads after portal.css: a launcher position of its own
    would make the two launchers overlap on an agent tab with a save bar."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "demo-tour.css").read_text(), flags=re.S)
    for rule in re.findall(r"([^{}]*\{[^{}]*\})", css):
        sel, body = rule.split("{", 1)
        if ".ba-launcher" in sel or "#waj-test-chat-root" in sel:
            assert not re.search(r"\b(bottom|right|top|left)\s*:", body), rule.strip()


# --------------------------------------------------------------------------
# A finished turn is not replayed: a reload brings the page back, not the
# panel, and the turn's "Refresh this page" offer is shown once.
# --------------------------------------------------------------------------

_AGENT = "/admin/agents?profile_id=df493d13-0c07-4f64-9e28-7522515797fb&env=staging"


def _conversation(*, pending: bool = False, changed: bool = True) -> dict:
    reply = {"role": "assistant", "pending": pending, "changed": changed,
             "text": f"Built **Outage Support Agent**.\n\n[Open Outage Support Agent]({_AGENT})",
             "steps": [{"tool": "templates", "action": "use", "ok": True, "label": "Created it"},
                       {"tool": "update_profile", "ok": True, "label": "Set it to active",
                        "links": [{"href": _AGENT, "text": "Open the agent"}]}]}
    return {"demo-assistant:seen": "1", "demo-assistant:open": "1",
            "demo-assistant:v2": json.dumps({"session": None, "items": [
                {"role": "user", "text": "Build me a support agent that checks outages"}, reply]})}


@needs_node
@pytest.mark.parametrize("phone", [True, False])
@pytest.mark.parametrize("tour", [True, False])
def test_a_reload_after_a_finished_turn_keeps_the_panel_closed(phone, tour):
    out = _run(phone=phone, tour=tour, reload=True, storage=_conversation())
    assert not out["panelOpen"] and out["launcherShown"]
    assert out["storage"]["demo-assistant:open"] == "0"
    # Moving to another page (not a reload) still keeps it as it was left.
    out = _run(phone=False, tour=tour, storage=_conversation())
    assert out["panelOpen"]


@needs_node
def test_a_reload_while_a_turn_runs_reopens_the_panel():
    out = _run(phone=False, tour=True, reload=True, storage=_conversation(pending=True, changed=False))
    assert out["panelOpen"]


@needs_node
def test_the_changed_offer_is_not_shown_again_on_a_later_page():
    out = _run(phone=False, tour=True, storage=_conversation())
    saved = json.loads(out["storage"]["demo-assistant:v2"])["items"][-1]
    assert saved["changed"] is False and saved["reported"] is True
    assert "Refresh this page" not in out["buttons"]
    assert "Open Outage Support Agent" not in out["buttons"]


def test_a_turn_that_made_an_agent_offers_to_open_it():
    """In the turn itself, the offer is the new agent, not a reload of this page."""
    src = JS.read_text()
    assert 'label.textContent = "Your new agent is ready.";' in src
    assert "window.location.href = made.href" in src
    # Reported to the tour once per turn: a turn read back later is not.
    assert "if (!reply || reply.reported) return;" in src
