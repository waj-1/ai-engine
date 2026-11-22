import pytest
from fastapi import HTTPException
from voice.rate_limit import SlidingWindowLimiter


def test_allows_up_to_limit_then_429():
    t = [1000.0]
    lim = SlidingWindowLimiter(max_requests=2, window_sec=60, now=lambda: t[0])
    lim.check("user1")
    lim.check("user1")
    with pytest.raises(HTTPException) as ei:
        lim.check("user1")
    assert ei.value.status_code == 429
    assert ei.value.headers["Retry-After"] == "60"


def test_window_slides():
    t = [1000.0]
    lim = SlidingWindowLimiter(max_requests=1, window_sec=60, now=lambda: t[0])
    lim.check("u")
    t[0] = 1061.0  # past the window
    lim.check("u")  # no raise


def test_keys_are_isolated():
    t = [0.0]
    lim = SlidingWindowLimiter(max_requests=1, window_sec=60, now=lambda: t[0])
    lim.check("a")
    lim.check("b")  # different key, no raise


def test_empty_key_is_a_noop():
    lim = SlidingWindowLimiter(max_requests=1, window_sec=60)
    lim.check("")
    lim.check("")  # never raises for anonymous/no-id callers
