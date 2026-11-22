"""Per-key sliding-window limiter, extracted from main.py's transcribe gate.

Voice endpoints call paid providers on every request, so the coarse global
IP middleware isn't enough — each voice route gets its own per-user (or
per-IP, for anonymous callers) limiter. An empty key is a no-op so
anonymous callers without a stable id aren't blocked by a shared bucket.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict

from fastapi import HTTPException


class SlidingWindowLimiter:
    def __init__(self, *, max_requests: int, window_sec: int, now: Callable[[], float] = time.time) -> None:
        self._max = max_requests
        self._window = window_sec
        self._now = now
        self._store: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        """Record a hit for `key`; raise 429 if it exceeds the window budget."""
        if not key:
            return
        now = self._now()
        bucket = self._store[key]
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            raise HTTPException(
                status_code=429,
                detail=f"Voice rate limit: {self._max} requests per {self._window}s. Try again shortly.",
                headers={"Retry-After": str(self._window)},
            )
        bucket.append(now)
