"""the event contract (DESIGN.md §4.1): shared, visibility-scoped, append-only.

the Event dataclass is shared vocabulary (Briefing depends on it). event ids
are opaque and hashable - the shared type never assumes an integer database
id. visibility is an app policy applied BEFORE windowing, so a message limit
means "messages the viewer can actually see". `message_limit` counts
kind='message' only; intervening action/note events ride inside the selected
id-ordered window - the exact semantic chordial's prompt history depends on.

only the engine holds append access; directors, gates, and deciders receive
an EventReader projection. that keeps "one recording discipline" enforceable
instead of merely conventional.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Protocol, runtime_checkable


# (event, viewer) -> may this viewer see this event? default: everything.
VisibilityPolicy = Callable[["Event", str], bool]


@dataclass(frozen=True)
class Event:
    """one recorded moment in a stream. stream-scoped: the store it came from
    names the stream, so the event itself doesn't repeat it."""

    event_id: str  # opaque, hashable, unique within the stream
    author_type: str  # 'user' | 'agent' | 'system'
    author: str  # 'user', an agent name, ...
    kind: str  # 'message' | 'action' | 'note'
    content: str
    created_at: datetime
    message_type: Optional[str] = (
        None  # messages only ('conversation', 'scheduled', ...)
    )
    platform: Optional[str] = None  # provenance - never a filter key by default
    scope: Optional[str] = None
    audience: Optional[str] = None  # which private channel this belongs to
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NewEvent:
    """what a writer supplies; the store assigns id and timestamp."""

    author_type: str
    author: str
    kind: str
    content: str
    message_type: Optional[str] = None
    platform: Optional[str] = None
    scope: Optional[str] = None
    audience: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class EventQuery:
    """explicit, testable read semantics (§11.7): no convenient method names
    with unspecified windows. `viewer` applies the store's visibility policy
    before any windowing; `message_limit` counts message-kind events only."""

    kinds: Optional[frozenset[str]] = None
    author_types: Optional[frozenset[str]] = None
    authors: Optional[frozenset[str]] = None
    message_types: Optional[frozenset[str]] = None
    viewer: Optional[str] = None
    message_limit: Optional[int] = None


@runtime_checkable
class EventReader(Protocol):
    """the read-only projection handed to directors, gates, and deciders."""

    async def read(self, query: EventQuery) -> list[Event]: ...
    async def latest(self, query: EventQuery) -> Optional[Event]: ...


@runtime_checkable
class EventStore(Protocol):
    """the full contract; only the engine (and delivery confirmation) appends.
    async even when an adapter delegates to synchronous storage - the
    framework must not force a future async consumer to block (§11.8)."""

    async def append(self, event: NewEvent) -> Event: ...
    async def read(self, query: EventQuery) -> list[Event]: ...
    async def latest(self, query: EventQuery) -> Optional[Event]: ...


class ReadOnlyEventReader:
    """the projection the engine hands to directors, gates, deciders, and
    preconditions (§4.1): read/latest only, structurally incapable of
    appending. this is what makes 'only the engine records' enforceable
    instead of merely conventional."""

    def __init__(self, store: EventStore):
        self._store = store

    async def read(self, query: EventQuery) -> list[Event]:
        return await self._store.read(query)

    async def latest(self, query: EventQuery) -> Optional[Event]:
        return await self._store.latest(query)


def _visible_to_all(event: Event, viewer: str) -> bool:
    return True


class InMemoryEventStore:
    """the reference implementation: one stream, id-ordered, visibility-aware.
    quick starts and tests; durable adapters implement the same contract and
    run the same conformance suite."""

    def __init__(
        self,
        visibility: Optional[VisibilityPolicy] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self._events: list[Event] = []
        self._visibility = visibility or _visible_to_all
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ids = itertools.count(1)

    async def append(self, event: NewEvent) -> Event:
        stored = Event(
            event_id=f"ev-{next(self._ids)}",
            author_type=event.author_type,
            author=event.author,
            kind=event.kind,
            content=event.content,
            created_at=self._clock(),
            message_type=event.message_type,
            platform=event.platform,
            scope=event.scope,
            audience=event.audience,
            metadata=event.metadata,
        )
        self._events.append(stored)
        return stored

    async def read(self, query: EventQuery) -> list[Event]:
        filtered = self._filtered(query)
        if query.message_limit is None:
            return filtered
        # window on the last N MESSAGE events; non-message events inside that
        # id-ordered window ride along.
        message_positions = [i for i, e in enumerate(filtered) if e.kind == "message"]
        if not message_positions:
            return []
        window = message_positions[-query.message_limit :]
        return filtered[window[0] :]

    async def latest(self, query: EventQuery) -> Optional[Event]:
        filtered = self._filtered(query)
        return filtered[-1] if filtered else None

    def _filtered(self, query: EventQuery) -> list[Event]:
        # visibility filters BEFORE windowing (and before attribute filters,
        # though those commute) - a limit means visible things, full stop.
        events = self._events
        if query.viewer is not None:
            events = [e for e in events if self._visibility(e, query.viewer)]
        return [
            e
            for e in events
            if (query.kinds is None or e.kind in query.kinds)
            and (query.author_types is None or e.author_type in query.author_types)
            and (query.authors is None or e.author in query.authors)
            and (query.message_types is None or e.message_type in query.message_types)
        ]
