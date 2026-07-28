"""BoundedDeliveryLedger: the same contract as the unbounded reference,
plus the retention bounds that make it safe in a long-running daemon."""

from __future__ import annotations

import asyncio

from dainframe.core import (
    BoundedDeliveryLedger,
    DeliveryReceipt,
    DeliveryTarget,
    EventContext,
    InMemoryEventStore,
    NewPendingDelivery,
)
from dainframe.testing import DeliveryLedgerContract


def run(coro):
    return asyncio.run(coro)


class TestBoundedDeliveryLedgerContract(DeliveryLedgerContract):
    def make_ledger(self):
        return BoundedDeliveryLedger()


def _new(i):
    return NewPendingDelivery(
        stream_id="s",
        activation_id=f"a{i}",
        line_id=f"a{i}:0",
        speaker="aria",
        target=DeliveryTarget(platform="telegram", target_id="t"),
        text=f"msg {i}",
        event_context=EventContext(),
    )


def test_stale_unconfirmed_pendings_fall_off():
    ledger = BoundedDeliveryLedger(max_unconfirmed=3)
    staged = [run(ledger.stage(_new(i))) for i in range(5)]
    assert run(ledger.get(staged[0].pending_id)) is None
    assert run(ledger.get(staged[1].pending_id)) is None
    assert all(run(ledger.get(p.pending_id)) is not None for p in staged[2:])


def test_confirmed_history_is_capped_but_idempotent_inside_the_window():
    ledger = BoundedDeliveryLedger(max_confirmed=2)
    store = InMemoryEventStore()
    pendings = [run(ledger.stage(_new(i))) for i in range(3)]
    events = [
        run(ledger.confirm(p.pending_id, DeliveryReceipt(), store)) for p in pendings
    ]

    # the newest two stay idempotent...
    again = run(ledger.confirm(pendings[2].pending_id, DeliveryReceipt(), store))
    assert again.event_id == events[2].event_id
    # ...the evicted oldest raises instead of double-recording
    try:
        run(ledger.confirm(pendings[0].pending_id, DeliveryReceipt(), store))
    except KeyError:
        pass
    else:
        raise AssertionError("evicted confirmation must raise, not re-record")
