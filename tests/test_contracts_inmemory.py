"""the in-memory reference implementations run the same conformance suites
every durable adapter will (DESIGN.md §8) - the library keeps itself honest
with the exact tests it hands to apps."""

from dainframe.core.delivery import InMemoryDeliveryLedger
from dainframe.core.events import InMemoryEventStore
from dainframe.testing import DeliveryLedgerContract, EventStoreContract


class TestInMemoryEventStore(EventStoreContract):
    def make_store(self, visibility=None):
        return InMemoryEventStore(visibility=visibility)


class TestInMemoryDeliveryLedger(DeliveryLedgerContract):
    def make_ledger(self):
        return InMemoryDeliveryLedger()
