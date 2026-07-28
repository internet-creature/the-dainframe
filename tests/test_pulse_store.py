"""the in-memory reference PulseStore runs its own conformance suite - the
same one durable app adapters subclass."""

from __future__ import annotations

from dainframe.pulse import InMemoryPulseStore
from dainframe.testing import PulseStoreContract


class TestInMemoryPulseStore(PulseStoreContract):
    def make_store(self):
        return InMemoryPulseStore()
