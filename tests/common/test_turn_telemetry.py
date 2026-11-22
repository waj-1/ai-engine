from unittest.mock import MagicMock, patch

import pytest

from common import turn_telemetry


def _mock_client():
    client = MagicMock()
    chain = MagicMock()
    chain.insert.return_value = chain
    chain.execute.return_value = MagicMock(data=[{}], error=None)
    client.table.return_value = chain
    client.schema.return_value = client
    return client, chain


@pytest.mark.asyncio
async def test_record_turn_telemetry_writes_row_with_cost():
    client, chain = _mock_client()
    model = {"model_string": "gemini-3-flash", "model_id": "m1"}
    with patch("common.turn_telemetry.get_service_client", return_value=client), \
         patch("core.pricing.cost_usd", return_value=0.0123) as mock_cost:
        await turn_telemetry.record_turn_telemetry(
            session_id="s1", agent_kind="sales", profile_id="p1", model=model,
            usage={"input_tokens": 100, "output_tokens": 50,
                   "cache_read_tokens": 20, "cache_creation_tokens": 0},
            latency_ms=1234, iteration_count=2, lmnr_trace_id="lmnr-1",
        )
    mock_cost.assert_called_once_with(model, 100, 50, cache_read_tokens=20, cache_creation_tokens=0)
    row = chain.insert.call_args[0][0]
    assert row["agent_kind"] == "sales"
    assert row["input_tokens"] == 100 and row["output_tokens"] == 50
    assert row["cache_read_tokens"] == 20
    assert row["latency_ms"] == 1234
    assert row["cost_usd"] == 0.0123
    assert row["model_string"] == "gemini-3-flash"
    assert row["profile_id"] == "p1"
    assert row["iteration_count"] == 2
    assert row["lmnr_trace_id"] == "lmnr-1"


@pytest.mark.asyncio
async def test_record_turn_telemetry_swallows_errors():
    client, chain = _mock_client()
    chain.execute.side_effect = RuntimeError("db down")
    with patch("common.turn_telemetry.get_service_client", return_value=client), \
         patch("core.pricing.cost_usd", return_value=None):
        # Must not raise.
        await turn_telemetry.record_turn_telemetry(
            session_id="s1", agent_kind="sales", profile_id=None, model=None,
            usage={"input_tokens": 1, "output_tokens": 1},
            latency_ms=5,
        )
