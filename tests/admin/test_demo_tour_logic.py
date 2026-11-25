"""The guided tour's own rules, run under node (no packages needed).

`tests/admin/js/demo_tour_logic.js` loads `admin/static/demo-tour-steps.js`
and the pure half of `admin/static/demo-tour.js` (exported as
`window.__demoTourLogic`) and prints the result of fixed cases: AI steps tick
by action, not by a read with the same tool name; A4 needs both attachments;
"mine" keeps the shared agents and other visitors' runs out; A5 needs a
sub-agent added, not a re-save; two tabs merge instead of overwriting; a new
reset id drops old progress; the page rules of A1, A3 and D2. Skipped where
node is not installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "js" / "demo_tour_logic.js"


@pytest.fixture(scope="module")
def result():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, str(SCRIPT)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def test_ai_ticks_by_action(result):
    assert result["a3_list_only"] is False and result["a3_use"] is True
    assert result["a4_mcp_only"] is False and result["a4_list"] is False and result["a4_both"] is True
    assert result["a5_downstream"] is True and result["a5_template_sub"] is True
    assert result["a5_template_only"] is False
    assert result["b4_list"] is False and result["b4_compare"] is True
    assert result["c1_promote"] is True


def test_mine_and_grew(result):
    assert result["mine_own_agent"] is True and result["mine_shared_agent"] is False
    assert result["mine_own_run"] is True and result["mine_other_run"] is False
    assert result["a5_resave"] is False and result["a5_added"] is True
    assert result["a5_rule_mine"] is True and result["c1_requires_a3"] is True


def test_tabs_merge_and_reset_clears(result):
    assert result["merge_changed"] is True
    assert result["merge_steps"] == ["A1", "B1"]
    assert result["merge_made"] == ["x"] and result["merge_runs"] == ["r9"]
    assert result["merge_other_reset"] is False
    assert result["same_reset"] is True and result["new_reset"] is False


def test_page_rules(result):
    assert result["a1_instructions"] is True and result["a1_overview"] is False
    assert result["a3_from_templates"] is True and result["a3_from_list"] is False
    assert result["d2_alt_open"] is True and result["d2_alt_list"] is False
    assert result["d2_alt_note_empty"] and result["d2_alt_note_rows"] is None
    assert result["d2_flagged"] is True and result["d2_unflagged_reload"] is False
    # D2 with Needs attention: Open latest, confirmed by the next page (no referrer).
    assert result["d2_click_anchor"] == "needs-attention"
    assert result["d2_open_latest"] is True and result["d2_open_list"] is False
    assert json.loads(result["b3_open_rule"]) == {"name": "data-outcome", "values": ["failed", "flaky"]}
    assert result["steps"] == 17


def test_a_clock_fallback_page_keeps_progress(result):
    """The server falls back to the clock's date only when it cannot read the
    data's generation; one such page must not wipe a visitor's progress and
    bring the Welcome back. A real new generation (a reset) still does."""
    assert result["keep_same"] is True
    assert result["keep_on_date_fallback"] is True
    assert result["keep_new_generation"] is False
    assert result["keep_new_date"] is False
    assert result["keep_nothing"] is False


def test_agent_links_are_read_from_the_assistants_turn(result):
    assert result["agent_link"] == "df493d13-0c07-4f64-9e28-7522515797fb"
    assert result["agent_link_env_first"] == "df493d13-0c07-4f64-9e28-7522515797fb"
    assert result["agent_link_list"] is None
