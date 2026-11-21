"""Per-client sliding-window rate limiter. Protects LLM credits on a public demo."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request

from app.errors import AppError


class RateLimiter:
    def __init__(self, per_minute: int, trust_proxy_headers: bool = False):
        self.per_minute = per_minute
        self.trust_proxy_headers = trust_proxy_headers
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _client_id(self, request: Request) -> str:
        if self.trust_proxy_headers:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check(self, request: Request) -> None:
        if self.per_minute <= 0:
            return
        now = time.monotonic()
        hits = self._hits[self._client_id(request)]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            retry_after = max(1, int(60 - (now - hits[0])))
            raise AppError(
                429, "rate_limited",
                f"Too many requests. Limit is {self.per_minute} per minute.",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)
