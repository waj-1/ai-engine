"""Internal record ids never reach a voice caller (live 2026-09-29).

    "You have one branch: Main Branch.
     Branch ID: ea22654d-00ca-4379-b83e-05c5e40ef45e — it's on your screen."

A prompt rule against ids is the kind that does not hold on this path, so the
delegate strips UUID-shaped ids from the spoken text, every streamed sentence
and the display, for agents that opt in (`voice_scrub_internal_ids`).
"""
from __future__ import annotations

import pytest

import voice.live.delegate as delegate_mod
from core.tool_provenance import record_tool_result
from voice.live.delegate import run_delegated_turn
from voice.live.speakable import scrub_internal_ids

LIVE = ("You have one branch: Main Branch.\n\n"
        "Branch ID: ea22654d-00ca-4379-b83e-05c5e40ef45e — it's on your screen.")


@pytest.mark.parametrize("text,want", [
    (LIVE, "You have one branch: Main Branch."),
    ("Sara (ID: cf378f57-f262-47f6-a01b-824aef923ffb) is free tomorrow.", "Sara is free tomorrow."),
    ("Use branch ea22654d-… for that. Revenue is AED 11,216.", "Use branch for that. Revenue is AED 11,216."),
    ("فرعك الرئيسي Main Branch، المعرف: ea22654d-00ca-4379-b83e-05c5e40ef45e موجود على شاشتك.",
     "فرعك الرئيسي Main Branch، موجود على شاشتك."),
])
def test_scrub_internal_ids(text, want):
    assert scrub_internal_ids(text) == want


@pytest.mark.parametrize("text", [
    "Revenue is AED 11,216 on 2026-09-29 — 172 services.",
    "Your IBAN is SA03 8000 0000 6080 1016 7519.",
    "Call 050-1234567 to confirm.",
])
def test_text_without_an_id_is_untouched(text):
    assert scrub_internal_ids(text) is text


class _Business:
    voice_scrub_internal_ids = True


class _Other:
    pass


def _gen(text):
    async def gen(**kwargs):
        record_tool_result('{"id": "ea22654d-00ca-4379-b83e-05c5e40ef45e"}', tool_name="list_branches")
        for event in ({"type": "answer", "content": text}, {"type": "usage"}, {"type": "done"}):
            yield event
    return gen


async def _run(monkeypatch, agent, **kwargs):
    monkeypatch.setattr(delegate_mod, "run_turn", _gen(LIVE))
    return await run_delegated_turn(agent=agent, profile=object(), message="List my branches",
                                    identity=object(), **kwargs)


async def test_the_live_answer_is_neither_spoken_nor_shown_with_its_id(monkeypatch):
    heard = []

    async def on_sentence(text):
        heard.append(text)

    result = await _run(monkeypatch, _Business(), on_sentence=on_sentence)
    for text in (result.spoken, result.display, " ".join(heard)):
        assert "ea22654d" not in text and "Branch ID" not in text, text
    assert "Main Branch" in result.spoken


async def test_agents_that_do_not_opt_in_are_unchanged(monkeypatch):
    result = await _run(monkeypatch, _Other())
    assert "ea22654d" in result.display


def test_reference_numbers_are_not_ids():
    text = "Invoice 20260929-0012 is paid."
    assert scrub_internal_ids(text) is text
