"""Tests for the FastAPI endpoint → Pydantic schema extractor.

Verifies:
  - Known endpoints (a chat route) produce the expected fields.
  - Missing endpoints return None.
  - Field shape matches what the agents.html template iterates over.
"""
from __future__ import annotations

import sys
from pathlib import Path

from typing import Optional

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


@pytest.fixture(autouse=True)
def _reset_schema_cache():
    """Drop the schema cache before and after each test so monkey-patched
    routes / Pydantic models don't leak across cases."""
    from admin import _endpoint_schemas

    _endpoint_schemas.reset_cache()
    yield
    _endpoint_schemas.reset_cache()


# ---------------------------------------------------------------------------
# Happy path — a real FastAPI app with a real Pydantic body model, standing
# in for main.app (the walk imports `main.app` at call time).
# ---------------------------------------------------------------------------


class SupportChatRequest(BaseModel):
    message: str = Field(..., description="The visitor's message")
    session_id: str
    business_id: str
    order_id: Optional[str] = None
    screen_context: Optional[dict] = None


@pytest.fixture
def sample_app(monkeypatch):
    import main

    app = FastAPI()

    @app.post("/support/chat/stream")
    async def support_chat(body: SupportChatRequest):  # pragma: no cover - never called
        return {}

    @app.get("/health")
    async def health():  # pragma: no cover - never called
        return {}

    monkeypatch.setattr(main, "app", app)
    return app


def test_chat_endpoint_extracts_pydantic_schema(sample_app):
    """`/support/chat/stream` is gated by `SupportChatRequest` — the
    extractor must surface its required fields."""
    from admin._endpoint_schemas import get_input_schema_for_endpoint

    entry = get_input_schema_for_endpoint("/support/chat/stream")
    assert entry is not None, "expected /support/chat/stream to be a registered FastAPI route"
    assert entry["model_name"] == "SupportChatRequest"
    assert "POST" in entry["methods"]
    # The request requires message + session_id + business_id.
    required_names = {f["name"] for f in entry["fields"] if f["required"]}
    for expected in ("message", "session_id", "business_id"):
        assert expected in required_names, (
            f"expected {expected} marked required; got required={required_names}"
        )
    # Optional fields exist too.
    all_names = {f["name"] for f in entry["fields"]}
    assert "order_id" in all_names
    assert "screen_context" in all_names


def test_field_shape_matches_template_contract(sample_app):
    """Each field row must expose the keys the agents.html template reads."""
    from admin._endpoint_schemas import get_input_schema_for_endpoint

    entry = get_input_schema_for_endpoint("/support/chat/stream")
    assert entry is not None
    for f in entry["fields"]:
        for required_key in ("name", "type", "required", "description"):
            assert required_key in f, f"missing {required_key} in field row {f}"
        # `required` is a bool; rendering depends on it.
        assert isinstance(f["required"], bool)


def test_required_fields_sort_before_optional(sample_app):
    """The flattening helper sorts required fields first, alpha within
    each bucket — the template relies on this ordering."""
    from admin._endpoint_schemas import get_input_schema_for_endpoint

    entry = get_input_schema_for_endpoint("/support/chat/stream")
    assert entry is not None
    assert any(not f["required"] for f in entry["fields"])  # both buckets present
    seen_optional = False
    for f in entry["fields"]:
        if not f["required"]:
            seen_optional = True
        elif seen_optional:
            pytest.fail(
                "required field appeared after an optional one — "
                f"order is broken: {[(r['name'], r['required']) for r in entry['fields']]}"
            )


# ---------------------------------------------------------------------------
# Missing endpoints
# ---------------------------------------------------------------------------


def test_unknown_endpoint_returns_none():
    from admin._endpoint_schemas import get_input_schema_for_endpoint

    assert get_input_schema_for_endpoint("/nope/does-not-exist") is None


def test_none_endpoint_returns_none():
    from admin._endpoint_schemas import get_input_schema_for_endpoint

    assert get_input_schema_for_endpoint(None) is None
    assert get_input_schema_for_endpoint("") is None


def test_endpoint_without_body_has_empty_fields():
    """Routes without a request body (e.g. /health, /) must show up in
    the cache but with `model_name=None` so the template can render a
    "no body" message instead of crashing."""
    from admin._endpoint_schemas import get_input_schema_for_endpoint

    # `/health` is a GET endpoint with no request body in this repo.
    entry = get_input_schema_for_endpoint("/health")
    if entry is None:
        pytest.skip("/health is not registered; nothing to assert about empty-body case")
    assert entry["model_name"] is None
    assert entry["fields"] == []


# ---------------------------------------------------------------------------
# list_endpoint_summaries — debug aid, also worth a smoke check.
# ---------------------------------------------------------------------------


def test_list_endpoint_summaries_contains_the_chat_route(sample_app):
    from admin._endpoint_schemas import list_endpoint_summaries

    summaries = list_endpoint_summaries()
    paths = {s["path"] for s in summaries}
    assert "/support/chat/stream" in paths
