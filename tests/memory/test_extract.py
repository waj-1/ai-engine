# ai_agent/tests/memory/test_extract.py
import asyncio
import json


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, _messages):
        return _FakeMsg(self._content)


def test_extract_parses_facts_and_applies_threshold():
    from core.memory.extract import extract_facts

    payload = json.dumps({"facts": [
        {"content": "Prefers AED currency", "importance": 0.9},
        {"content": "Asked about July revenue once", "importance": 0.2},
    ]})
    cfg = {"importance_threshold": 0.5, "max_per_turn": 5}
    facts = asyncio.run(extract_facts(_FakeLLM(payload), "msg", "resp", cfg))
    assert [f["content"] for f in facts] == ["Prefers AED currency"]


def test_extract_caps_max_per_turn():
    from core.memory.extract import extract_facts

    payload = json.dumps({"facts": [
        {"content": f"fact {i}", "importance": 0.9} for i in range(10)
    ]})
    cfg = {"importance_threshold": 0.5, "max_per_turn": 3}
    facts = asyncio.run(extract_facts(_FakeLLM(payload), "msg", "resp", cfg))
    assert len(facts) == 3


def test_extract_tolerates_garbage_output():
    from core.memory.extract import extract_facts

    cfg = {"importance_threshold": 0.5, "max_per_turn": 5}
    facts = asyncio.run(extract_facts(_FakeLLM("not json at all"), "m", "r", cfg))
    assert facts == []


def test_content_to_text_handles_gemini_list_blocks():
    """Gemini 3 returns content as a list of blocks, not a string. The old
    str(content) stringified the Python repr and broke JSON parsing — dropping
    every extracted memory. content_to_text must flatten the text blocks."""
    from core.memory.extract import _parse_facts, content_to_text

    assert content_to_text("plain string") == "plain string"
    # The exact shape Gemini 3 returns (with a thinking signature in extras).
    blocks = [{"type": "text", "text": '{"facts": [{"content": "x", "importance": 0.9}]}',
               "extras": {"signature": "abc"}}]
    flat = content_to_text(blocks)
    assert flat == '{"facts": [{"content": "x", "importance": 0.9}]}'
    # And it must now parse to a real fact (the bug was 0 facts here).
    assert _parse_facts(flat) == [{"content": "x", "importance": 0.9}]
    # multiple text blocks concatenate; non-text/None degrade safely
    assert content_to_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert content_to_text(None) == ""
