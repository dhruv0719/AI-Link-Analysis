"""Tiny in-memory TTL + LRU cache (single process). Swap for Redis if you scale out."""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int = 86_400, max_items: int = 256):
        self.ttl = ttl_seconds
        self.max_items = max_items
        self._data: "OrderedDict[str, tuple[float, T]]" = OrderedDict()

    def get(self, key: str) -> Optional[T]:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: T) -> None:
        if self.ttl <= 0 or self.max_items <= 0:
            return
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)
