"""
Rate limiting for authentication endpoints.

A tiny in-memory sliding-window limiter.  For a single-instance personal app
this is sufficient; if you scale out, swap the store for Redis with the same
interface (``allow(key, limit, window)``).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

_LOCK = threading.Lock()
_HITS: Dict[str, Deque[float]] = defaultdict(deque)


def allow(key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
    """
    Register a hit for ``key``.

    Returns (allowed, retry_after_seconds).
    """
    now = time.monotonic()
    with _LOCK:
        bucket = _HITS[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = int(window_seconds - (now - bucket[0])) + 1
            return False, max(1, retry_after)
        bucket.append(now)
        return True, 0


def reset(key: str | None = None) -> None:
    """Clear counters (used by tests and after a successful login)."""
    with _LOCK:
        if key is None:
            _HITS.clear()
        else:
            _HITS.pop(key, None)


def remaining(key: str, limit: int, window_seconds: int) -> int:
    now = time.monotonic()
    with _LOCK:
        bucket = _HITS.get(key)
        if not bucket:
            return limit
        live = [t for t in bucket if now - t <= window_seconds]
        return max(0, limit - len(live))
