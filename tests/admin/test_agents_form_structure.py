"""Guard the agent editor's form structure (B1 redesign: one partial per tab).

History: the model <select> and the tool/source/MCP checkboxes once sat inside
an OUTER <form> that also (illegally) wrapped the System Prompt <form>.
Browsers drop nested <form>s, so the model select ended up outside any form
and "the model dropdown doesn't save". The tabbed editor keeps that lesson:
no tab partial nests a <form>, every control sits inside the form that owns
it, and — new with the redesign — a form that posts to /admin/profiles/{id}
carries ONLY its own tab's fields, so saving one tab cannot blank another
tab's data (profile_update writes every array it is sent).
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "admin" / "templates"
PARTIALS = TEMPLATES / "agents"


def _src(name: str) -> str:
    return (PARTIALS / name).read_text()


def _forms(src: str):
    """[(open_index, close_index, body)] for every <form> in a template."""
    out = []
    for m in re.finditer(r"<form\b", src):
        close = src.index("</form>", m.start())
        out.append((m.start(), close, src[m.start():close]))
    return out


def _form_containing(src: str, needle: str) -> str:
    pos = src.index(needle)
    for start, close, body in _forms(src):
        if start < pos < close:
            return body
    raise AssertionError(f"{needle!r} is not inside any <form>")


def test_no_tab_partial_nests_a_form():
    for path in PARTIALS.glob("_tab_*.html"):
        for _start, _close, body in _forms(path.read_text()):
            assert "<form" not in body[5:], f"nested <form> in {path.name}"


def test_model_select_is_inside_the_settings_form():
    body = _form_containing(_src("_tab_settings.html"), 'name="model_main_id"')
    assert 'action="/admin/profiles/{{ agent.id }}"' in body
    # Settings owns scalars only — it must not post any array field.
    for arr in ("tool_ids", "data_source_ids", "mcp_server_ids", "downstream_profile_ids"):
        assert f'name="{arr}"' not in body, arr
    assert 'name="_mapping_form"' not in body


def test_delegation_controls_are_inside_the_subagents_form():
    src = _src("_tab_subagents.html")
    body = _form_containing(src, 'name="delegatable"')
    assert 'action="/admin/profiles/{{ agent.id }}"' in body
    assert 'name="downstream_profile_ids" value=""' in body  # "removed the last one"
    for arr in ("tool_ids", "data_source_ids", "mcp_server_ids"):
        assert f'name="{arr}"' not in body, arr


def test_profile_forms_preserve_delegatable():
    """profile_update writes `delegatable` unconditionally, so every other
    form posting there re-sends the current value."""
    for name, needle in (("_tab_settings.html", 'name="model_main_id"'),
                         ("_tab_tools.html", 'name="data_source_ids" value=""'),
                         ("_tab_tools.html", 'name="tool_ids" value=""')):
        body = _form_containing(_src(name), needle)
        assert ('name="delegatable" value="on"' in body
                or "keep_delegatable()" in body), (name, needle)


def test_tools_tab_forms_each_carry_only_their_own_arrays():
    src = _src("_tab_tools.html")
    knowledge = _form_containing(src, 'name="data_source_ids" value=""')
    assert 'name="tool_ids"' not in knowledge and 'name="downstream_profile_ids"' not in knowledge
    legacy = _form_containing(src, 'name="tool_ids" value=""')
    assert 'name="data_source_ids"' not in legacy and 'name="downstream_profile_ids"' not in legacy
    toolsets = _form_containing(src, 'name="attach_mcp_ids"')
    assert 'action="/admin/profiles/{{ agent.id }}/mcp-attachments"' in toolsets
    skills = _form_containing(src, 'name="attach_skill_ids"')
    assert 'action="/admin/profiles/{{ agent.id }}/skill-attachments"' in skills


def test_prompt_editor_has_preview_and_expand_controls():
    src = _src("_tab_instructions.html")
    assert "data-prompt-preview" in src
    assert "data-prompt-expand" in src
    assert 'data-prompt-view="preview"' in src


def test_prompt_version_selector_removed():
    """The per-prompt serve dropdown stays gone (versions live on the
    Versions tab); the editor seeds from the latest content."""
    src = _src("_tab_instructions.html")
    assert "data-prompt-version-select" not in src
    assert "Serve this version" not in src
    assert "{{ system_prompt.content or '' }}" in src


def test_model_select_handles_empty_and_cross_env_lists():
    src = _src("_tab_settings.html")
    assert "No models registered yet" in src
    assert "/admin/agents?tab=models" in src
    assert "no model assigned" in src
    assert "m.environment != current_env" in src


def test_every_editing_tab_has_one_save_bar():
    for name in ("_tab_instructions.html", "_tab_tools.html",
                 "_tab_subagents.html", "_tab_settings.html"):
        src = _src(name)
        assert src.count("savebar(") == 1, name
        assert "data-tab-root" in src, name
        # No per-form submit buttons: the tab's single Save posts the forms.
        assert 'type="submit"' not in src, name
