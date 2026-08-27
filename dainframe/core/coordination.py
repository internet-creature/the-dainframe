"""stream coordination (DESIGN.md §4.7): one activation at a time per stream.

one activation holds its stream from the inbound read/append through
after_turn, so a user message and an ambient firing - or two platform
messages - cannot interleave their scripts and event windows. serialization
is per stream, never global; different streams proceed concurrently.

the in-process default makes the safe local behavior the easy behavior.
multi-process apps supply a distributed lease or optimistic versioned
implementation behind the same protocol.
"""

from __future__ import annotations

import asyncio
from typing import AsyncContextManager, Protocol, runtime_checkable


@runtime_checkable
class StreamCoordinator(Protocol):
    def hold(self, stream_id: str) -> AsyncContextManager[None]: ...


class InProcessStreamCoordinator:
    """keyed asyncio locks: correct within one event loop, which is exactly
    the chordial deployment shape today."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def hold(self, stream_id: str) -> AsyncContextManager[None]:
        lock = self._locks.get(stream_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[stream_id] = lock
        return lock
