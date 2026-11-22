"""Tests for the N-run compare route helpers in `admin.eval_handlers`.

TDD for Task 3.2: `_compare_run_ids` parser that powers the generalised
`/admin/evals/compare?runs=id1,id2,...` route with `a`/`b` back-compat.
"""
from __future__ import annotations

import sys
from pathlib import Path

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def test_parse_run_ids_from_query():
    from admin import eval_handlers as eh

    assert eh._compare_run_ids(runs="a,b,c", a=None, b=None) == ["a", "b", "c"]
    assert eh._compare_run_ids(runs="", a="x", b="y") == ["x", "y"]   # back-compat
    assert eh._compare_run_ids(runs=" a , b ", a=None, b=None) == ["a", "b"]
