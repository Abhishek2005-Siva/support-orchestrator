"""Sliding-window rate limiter (in-process). Guardrail G-API-03: 100 req/min per authenticated user (configurable),
plus a stricter per-IP limit and failed-login lockout on /auth/token (brute-force protection, G-API-04).
Single-process correctness; for multi-worker deployments swap `_hits` for a Redis sorted-set (same interface)."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, float]:
        """returns (allowed, remaining, retry_after_seconds)"""
        now = now if now is not None else time.monotonic()
        q = self._hits[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False, 0, max(0.0, self.window - (now - q[0]))
        q.append(now)
        return True, self.limit - len(q), 0.0

    def prune(self, now: float | None = None):
        now = now if now is not None else time.monotonic()
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]:
            del self._hits[k]


class LoginThrottle:
    """Lock a client_id for `lock_s` after `max_fail` consecutive failures."""

    def __init__(self, max_fail: int = 5, lock_s: float = 60.0):
        self.max_fail, self.lock_s = max_fail, lock_s
        self.fails: dict[str, int] = defaultdict(int)
        self.locked_until: dict[str, float] = {}

    def locked(self, key: str) -> float:
        t = self.locked_until.get(key, 0.0)
        return max(0.0, t - time.monotonic())

    def failure(self, key: str):
        self.fails[key] += 1
        if self.fails[key] >= self.max_fail:
            self.locked_until[key] = time.monotonic() + self.lock_s
            self.fails[key] = 0

    def success(self, key: str):
        self.fails.pop(key, None)
