"""the delivery contract (DESIGN.md §4.3): confirmed-send-before-recording.

a reply enters shared history only after the platform says yes. direct
delivery lets the engine own that (it holds the stream across send and
record); pending delivery freezes the exact text behind an opaque id, and
confirmation is idempotent - free-form `record_delivered_message(text)` is
deliberately not exposed (§11.4).

idempotence is a storage property, not an id-format trick: a durable ledger's
confirm must atomically transition pending→confirmed and append (or return)
the one message event. the in-memory ledger provides those semantics for one
process; the conformance suite calls confirm concurrently to keep every
implementation honest.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, runtime_checkable

from dainframe.core.events import Event, EventStore, NewEvent
from dainframe.core.types import DeliveryTarget, EventContext, PendingDelivery


@dataclass(frozen=True)
class DeliveryRequest:
    """what a Deliverer is asked to send. the target is opaque app routing."""

    stream_id: str
    activation_id: str
    line_id: str
    speaker: str
    target: DeliveryTarget
    text: str


@dataclass(frozen=True)
class DeliveryReceipt:
    """opaque platform evidence of a successful send."""

    provider_message_id: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class Deliverer(Protocol):
    """returns a receipt on confirmed success, None on failure. never raises
    apology strings into history - a None simply keeps the line errored."""

    async def deliver(self, request: DeliveryRequest) -> Optional[DeliveryReceipt]: ...


@dataclass(frozen=True)
class NewPendingDelivery:
    """what the engine stages; the ledger assigns the opaque pending id."""

    stream_id: str
    activation_id: str
    line_id: str
    speaker: str
    target: DeliveryTarget
    text: str
    event_context: EventContext


@runtime_checkable
class DeliveryLedger(Protocol):
    async def stage(self, pending: NewPendingDelivery) -> PendingDelivery: ...

    async def get(self, pending_id: str) -> Optional[PendingDelivery]: ...

    async def confirm(
        self, pending_id: str, receipt: DeliveryReceipt, events: EventStore
    ) -> Event: ...


class InMemoryDeliveryLedger:
    """single-process reference ledger. an app that needs pending outputs to
    survive restarts supplies a durable one whose confirm is transactional
    with its EventStore; anything weaker must document the remaining
    duplicate/loss window (§4.3)."""

    def __init__(self):
        self._pending: dict[str, PendingDelivery] = {}
        self._confirmed: dict[str, Event] = {}
        self._lock = asyncio.Lock()

    async def stage(self, pending: NewPendingDelivery) -> PendingDelivery:
        frozen = PendingDelivery(
            pending_id=f"pd-{uuid.uuid4().hex[:12]}",
            stream_id=pending.stream_id,
            activation_id=pending.activation_id,
            line_id=pending.line_id,
            speaker=pending.speaker,
            target=pending.target,
            text=pending.text,
            event_context=pending.event_context,
        )
        self._pending[frozen.pending_id] = frozen
        return frozen

    async def get(self, pending_id: str) -> Optional[PendingDelivery]:
        return self._pending.get(pending_id)

    async def confirm(
        self, pending_id: str, receipt: DeliveryReceipt, events: EventStore
    ) -> Event:
        async with self._lock:
            already = self._confirmed.get(pending_id)
            if already is not None:
                return already
            pending = self._pending.get(pending_id)
            if pending is None:
                raise KeyError(f"unknown pending delivery '{pending_id}'")
            ec = pending.event_context
            event = await events.append(
                NewEvent(
                    author_type="agent",
                    author=pending.speaker,
                    kind="message",
                    content=pending.text,  # the exact frozen text, nothing else
                    message_type=ec.outbound_message_type,
                    platform=ec.platform,
                    scope=ec.scope,
                    audience=ec.audience,
                    metadata={"pending_id": pending_id},
                )
            )
            self._confirmed[pending_id] = event
            return event
