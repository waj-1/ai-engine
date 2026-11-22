"""The portal's static UI: RTL-safe CSS, no HTML parsing of data, and a
template that carries every element the controller looks up."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/agents/portal.css").read_text(encoding="utf-8")
JS = (ROOT / "static/agents/portal.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates/agents/portal.html").read_text(encoding="utf-8")


def test_css_uses_logical_properties_only():
    for physical in ("margin-left:", "margin-right:", "padding-left:", "padding-right:",
                     "text-align: left", "text-align: right", "float: left", "float: right",
                     "border-left", "border-right"):
        assert physical not in CSS, physical
    assert not re.search(r"(^|[\s;{])(left|right)\s*:", CSS), "physical inset"


def test_js_never_parses_data_as_html():
    assert "innerHTML" not in JS and "insertAdjacentHTML" not in JS and "outerHTML" not in JS
    assert "textContent" in JS


def test_template_has_every_id_the_controller_uses():
    wanted = set(re.findall(r'\$\("([\w-]+)"\)', JS)) - {"ap-pickup"}  # created at runtime
    present = set(re.findall(r'id="([\w-]+)"', HTML))
    assert wanted <= present, sorted(wanted - present)


def test_both_languages_cover_the_same_keys():
    def keys(block):
        return set(re.findall(r"^\s{6}(\w+):", block, re.MULTILINE)) | set(re.findall(r",\s(\w+):", block))

    en = JS[JS.index("    en: {"):JS.index("    ar: {")]
    ar = JS[JS.index("    ar: {"):JS.index("  var AGENT_COPY")]
    # `<key>_f` entries are Arabic-only feminine verb forms (see tg()).
    assert keys(en) == {k for k in keys(ar) if not k.endswith("_f")}


def test_desktop_workspace_is_one_viewport_row():
    assert "block-size: 100dvh; overflow: hidden;" in CSS
    assert "grid-template-columns: minmax(0, 1.08fr) minmax(0, 1fr)" in CSS


def test_portal_ui_has_no_emoji_and_no_persona_names():
    """Agents are labelled by role, stories by their summary; no emoji."""
    from common.emoji_names import emoji_in

    for name, text in (("portal.js", JS), ("portal.html", HTML)):
        assert emoji_in(text) == [], name
    assert "persona_en" not in JS and "persona_ar" not in JS
    assert '"summary")' in JS
