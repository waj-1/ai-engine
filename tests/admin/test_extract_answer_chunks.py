"""Regression tests for ``_extract_answer_chunks`` SSE event-type handling.

The helper pulls ``content`` out of ``data: {...}`` SSE events. Some agent
endpoints emit the human-facing answer under ``"type": "final_response"`` or
``"type": "output"`` instead of ``"answer"``/``"final"``/``"result"``. Those
must be extracted into the assembled answer too; otherwise the helper returns
"" and the caller falls back to the raw body.
"""

from __future__ import annotations

import sys
from pathlib import Path

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def test_answer_event_is_extracted():
    """Baseline: the classic ``answer`` event still works (no regression)."""
    from admin.evals import _extract_answer_chunks

    body = 'data: {"type": "answer", "content": "hello world"}\n\n'
    assert _extract_answer_chunks(body) == "hello world"


def test_final_response_event_is_extracted():
    """An endpoint that carries the answer under ``final_response``."""
    from admin.evals import _extract_answer_chunks

    body = 'data: {"type": "final_response", "content": "the final answer"}\n\n'
    assert _extract_answer_chunks(body) == "the final answer"


def test_output_event_is_extracted():
    """An endpoint that carries the answer under ``output``."""
    from admin.evals import _extract_answer_chunks

    body = 'data: {"type": "output", "content": "the output text"}\n\n'
    assert _extract_answer_chunks(body) == "the output text"


def test_mixed_answer_and_final_response_chunks_are_joined():
    """Multiple matched event types all contribute to the assembled answer."""
    from admin.evals import _extract_answer_chunks

    body = (
        'data: {"type": "answer", "content": "part one "}\n\n'
        'data: {"type": "final_response", "content": "part two"}\n\n'
    )
    assert _extract_answer_chunks(body) == "part one part two"
