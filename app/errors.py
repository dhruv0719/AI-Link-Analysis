"""Application-level errors that map 1:1 to the JSON error envelope."""
from __future__ import annotations


class AppError(Exception):
    """An expected, user-facing failure (bad URL, blocked site, LLM outage ...)."""

    def __init__(self, status_code: int, code: str, message: str, *, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}
