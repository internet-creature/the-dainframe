"""the shared concurrency ceiling for in-flight provider calls.

create ONE limiter and inject it into every provider that should share the
ceiling (DESIGN.md §4.8): resolving a new provider object per model must not
accidentally create a new "global" semaphore per object. a provider
constructed without a limiter gets a private one — correct for single-provider
apps, and explicit sharing is one argument away.
"""

from __future__ import annotations

import asyncio


class ConcurrencyLimiter:
    """an async context manager capping concurrent in-flight calls. invisible
    at one user; a guardrail against burst fan-out (e.g. ambient activations)
    at scale."""

    def __init__(self, max_concurrent: int = 6):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def __aenter__(self) -> "ConcurrencyLimiter":
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._semaphore.release()
