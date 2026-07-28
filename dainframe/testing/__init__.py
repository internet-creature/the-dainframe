"""reusable conformance suites (DESIGN.md §8): app adapters run these in
addition to their product tests. subclass a contract in your test suite and
override its factory method; every test method comes along."""

from dainframe.testing.delivery_ledger_contract import DeliveryLedgerContract
from dainframe.testing.event_store_contract import EventStoreContract
from dainframe.testing.pulse_store_contract import PulseStoreContract

__all__ = ["DeliveryLedgerContract", "EventStoreContract", "PulseStoreContract"]
