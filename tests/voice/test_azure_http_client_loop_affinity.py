"""The pooled Azure-speech HTTP client must not outlive its event loop.

`voice/providers/azure.py` caches one `httpx.AsyncClient` process-wide. An
AsyncClient binds its connection pool to the loop that created it, and
`is_closed` reports only whether *the client* was closed — it knows nothing
about its loop being torn down. So a second event loop reusing the cached
client hits `RuntimeError: Event loop is closed` deep in anyio.

That is not hypothetical: `admin/voice_eval_runner.py:98` calls `asyncio.run()`
PER SCENARIO, so every voice eval after the first scenario failed with
`VoiceProviderError: speech synthesis provider error` — the caller's speech
never synthesized, the scenario recorded `turns=0 / ws_closed`, and the judge
scored it 0 for never having run. Measured 2026-08-18: it corrupted every arm
of a duplex bake-off before anyone noticed the errors were ours, not Azure's.
"""
import asyncio

import voice.providers.azure as az


def _fresh_module_state():
    az._client = None
    az._client_loop = None


def test_a_second_event_loop_gets_a_new_client():
    """The regression. Two `asyncio.run` calls = two loops; the client cached
    on the first must not be handed to the second."""
    _fresh_module_state()
    first = asyncio.run(_grab())
    second = asyncio.run(_grab())
    assert first is not second, (
        "the client cached on a now-closed loop was reused — this is the "
        "RuntimeError('Event loop is closed') that zeroed every voice eval "
        "scenario after the first"
    )


async def _grab():
    return az._http()


def test_the_same_loop_still_reuses_one_client():
    """The fix must not become 'a new client per request'.

    The duplex voice path runs on one long-lived loop and depends on
    connection reuse — rebuilding per call would put a fresh TLS handshake in
    front of every utterance, on the exact path where latency is the failure
    mode.
    """
    _fresh_module_state()

    async def twice():
        return az._http(), az._http()

    a, b = asyncio.run(twice())
    assert a is b


def test_a_closed_client_is_still_rebuilt_on_the_same_loop():
    """The pre-existing `is_closed` guard has to survive the loop check."""
    _fresh_module_state()

    async def close_then_get():
        first = az._http()
        await first.aclose()
        return first, az._http()

    first, second = asyncio.run(close_then_get())
    assert first is not second


def test_no_running_loop_does_not_crash():
    """`_http()` is only ever awaited from async code, but it is a plain
    function — calling it outside a loop must fall back, not raise."""
    _fresh_module_state()
    client = az._http()
    assert isinstance(client, az.httpx.AsyncClient)
