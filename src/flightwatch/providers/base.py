from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from ..models import Availability, SearchQuery


class ProviderError(RuntimeError):
    """Raised for configuration problems and unrecoverable upstream failures."""


@runtime_checkable
class Provider(Protocol):
    """Source of award availability.

    Implementations should be safe to call repeatedly and must not raise on an
    empty result set -- no award space is the normal case, not an error.
    """

    name: str

    def search(self, query: SearchQuery) -> list[Availability]: ...

    def close(self) -> None: ...


class RateLimiter:
    """Minimum-interval throttle, so we stay under the plan's documented rate limit."""

    def __init__(self, requests_per_minute: int) -> None:
        self._min_interval = 60.0 / max(requests_per_minute, 1)
        self._last_call: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self._last_call is not None:
            remaining = self._min_interval - (now - self._last_call)
            if remaining > 0:
                time.sleep(remaining)
                now = time.monotonic()
        self._last_call = now
