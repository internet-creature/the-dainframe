"""the DeliveryLedger conformance suite (DESIGN.md §4.3/§8).

subclass in your tests and override `make_ledger` (and `make_store` if the
default in-memory event store won't do). locks down: staged text is frozen,
confirmation records the exact text once, double confirmation is idempotent —
including CONCURRENT confirmation, because idempotence is a storage property,
not an id-format trick.
"""

from __future__ import annotations

import asyncio

from dainframe.core.delivery import DeliveryLedger, DeliveryReceipt, NewPendingDelivery
from dainframe.core.events import EventQuery, EventStore, InMemoryEventStore
from dainframe.core.types import DeliveryTarget, EventContext


class DeliveryLedgerContract:
    """override make_ledger() to test your implementation."""

    def make_ledger(self) -> DeliveryLedger:
        raise NotImplementedError

    def make_store(self) -> EventStore:
        return InMemoryEventStore()

    def _pending(self, text="hello from the pulse"):
        return NewPendingDelivery(
            stream_id="s1",
            activation_id="act-1",
            line_id="act-1:0",
            speaker="aria",
            target=DeliveryTarget(platform="telegram", target_id="chat-9"),
            text=text,
            event_context=EventContext(
                platform="telegram",
                scope="dm",
                audience="aria",
                outbound_message_type="scheduled",
            ),
        )

    def test_stage_freezes_the_text_behind_an_opaque_id(self):
        async def _run():
            ledger = self.make_ledger()
            pending = await ledger.stage(self._pending())
            assert pending.pending_id
            assert pending.text == "hello from the pulse"
            fetched = await ledger.get(pending.pending_id)
            assert fetched == pending

        asyncio.run(_run())

    def test_confirm_records_the_exact_frozen_text_with_its_context(self):
        async def _run():
            ledger = self.make_ledger()
            store = self.make_store()
            pending = await ledger.stage(self._pending())
            event = await ledger.confirm(
                pending.pending_id, DeliveryReceipt(provider_message_id="m-1"), store
            )
            assert event.content == "hello from the pulse"
            assert event.author == "aria"
            assert event.message_type == "scheduled"
            assert event.audience == "aria"
            recorded = await store.read(EventQuery())
            assert [e.event_id for e in recorded] == [event.event_id]

        asyncio.run(_run())

    def test_double_confirm_is_idempotent(self):
        async def _run():
            ledger = self.make_ledger()
            store = self.make_store()
            pending = await ledger.stage(self._pending())
            first = await ledger.confirm(pending.pending_id, DeliveryReceipt(), store)
            second = await ledger.confirm(pending.pending_id, DeliveryReceipt(), store)
            assert first.event_id == second.event_id
            assert len(await store.read(EventQuery())) == 1

        asyncio.run(_run())

    def test_concurrent_confirm_records_once(self):
        async def _run():
            ledger = self.make_ledger()
            store = self.make_store()
            pending = await ledger.stage(self._pending())
            results = await asyncio.gather(
                *[
                    ledger.confirm(pending.pending_id, DeliveryReceipt(), store)
                    for _ in range(5)
                ]
            )
            assert len({e.event_id for e in results}) == 1
            assert len(await store.read(EventQuery())) == 1

        asyncio.run(_run())

    def test_confirming_the_unknown_raises(self):
        async def _run():
            ledger = self.make_ledger()
            store = self.make_store()
            try:
                await ledger.confirm("pd-nope", DeliveryReceipt(), store)
            except KeyError:
                return
            raise AssertionError("confirming an unknown pending id must raise")

        asyncio.run(_run())
