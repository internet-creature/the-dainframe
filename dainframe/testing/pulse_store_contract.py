"""the PulseStore conformance suite (DESIGN.md §6.2/§11.10).

subclass and implement `make_store()`; the suite verifies the semantics every
implementation must honor: atomic claim ownership, lease expiry, horizon
gating, stale-claim rejection, and the state bookkeeping that anchors and
occurrence dedup depend on.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from dainframe.pulse.store import StaleClaimError
from dainframe.pulse.types import PulseOutcome, RhythmKey

KEY = RhythmKey(stream_id="stream-1", rhythm_id="checkin")
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class PulseStoreContract:
    def make_store(self):
        raise NotImplementedError

    # --- claims are exclusive ------------------------------------------------

    def test_fresh_key_is_claimable_once(self):
        store = self.make_store()

        async def scenario():
            lease = T0 + timedelta(minutes=5)
            first = await store.claim_due(KEY, T0, lease)
            second = await store.claim_due(KEY, T0, lease)
            return first, second

        first, second = run(scenario())
        assert first is not None
        assert second is None  # the lease excludes a second owner

    def test_concurrent_claims_yield_exactly_one_owner(self):
        store = self.make_store()

        async def scenario():
            lease = T0 + timedelta(minutes=5)
            return await asyncio.gather(
                *[store.claim_due(KEY, T0, lease) for _ in range(8)]
            )

        claims = [c for c in run(scenario()) if c is not None]
        assert len(claims) == 1

    def test_expired_lease_is_reclaimable(self):
        store = self.make_store()

        async def scenario():
            first = await store.claim_due(KEY, T0, T0 + timedelta(minutes=5))
            later = T0 + timedelta(minutes=6)
            second = await store.claim_due(KEY, later, later + timedelta(minutes=5))
            return first, second

        first, second = run(scenario())
        assert first is not None and second is not None
        assert first.claim_id != second.claim_id

    # --- the horizon gates claims -------------------------------------------

    def test_completed_next_check_gates_future_claims(self):
        store = self.make_store()

        async def scenario():
            claim = await store.claim_due(KEY, T0, T0 + timedelta(minutes=5))
            await store.complete(
                claim,
                PulseOutcome(status="skipped", at=T0, detail="not due"),
                next_check=T0 + timedelta(hours=1),
            )
            too_early = await store.claim_due(
                KEY,
                T0 + timedelta(minutes=30),
                T0 + timedelta(minutes=35),
            )
            on_time = await store.claim_due(
                KEY, T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=5)
            )
            return too_early, on_time

        too_early, on_time = run(scenario())
        assert too_early is None
        assert on_time is not None

    def test_abandon_releases_with_a_retry_horizon(self):
        store = self.make_store()

        async def scenario():
            claim = await store.claim_due(KEY, T0, T0 + timedelta(minutes=5))
            retry = T0 + timedelta(minutes=30)
            await store.abandon(claim, retry_at=retry, reason="boom")
            before = await store.claim_due(
                KEY, T0 + timedelta(minutes=10), T0 + timedelta(minutes=15)
            )
            after = await store.claim_due(KEY, retry, retry + timedelta(minutes=5))
            return before, after

        before, after = run(scenario())
        assert before is None  # the retry horizon holds even after release
        assert after is not None

    # --- state bookkeeping ---------------------------------------------------

    def test_outcome_timestamps_and_occurrence_persist_into_snapshots(self):
        store = self.make_store()

        async def scenario():
            claim = await store.claim_due(KEY, T0, T0 + timedelta(minutes=5))
            await store.complete(
                claim,
                PulseOutcome(status="activated", at=T0, generated=True, delivered=True),
                next_check=T0 + timedelta(hours=1),
                occurrence_key="2026-07-01T09:00:00+00:00",
            )
            later = T0 + timedelta(hours=1)
            return await store.claim_due(KEY, later, later + timedelta(minutes=5))

        claim = run(scenario())
        assert claim.state.last_attempt == T0
        assert claim.state.last_generated == T0
        assert claim.state.last_delivered == T0
        assert claim.state.occurrence_key == "2026-07-01T09:00:00+00:00"

    def test_failed_outcome_advances_attempt_but_not_delivery(self):
        store = self.make_store()

        async def scenario():
            claim = await store.claim_due(KEY, T0, T0 + timedelta(minutes=5))
            await store.complete(
                claim,
                PulseOutcome(status="failed", at=T0, generated=True, delivered=False),
                next_check=T0 + timedelta(minutes=30),
            )
            later = T0 + timedelta(minutes=30)
            return await store.claim_due(KEY, later, later + timedelta(minutes=5))

        claim = run(scenario())
        assert claim.state.last_attempt == T0
        assert claim.state.last_generated == T0
        assert claim.state.last_delivered is None

    # --- stale claims must not clobber ---------------------------------------

    def test_stale_claim_completion_raises(self):
        store = self.make_store()

        async def scenario():
            first = await store.claim_due(KEY, T0, T0 + timedelta(minutes=5))
            later = T0 + timedelta(minutes=6)  # lease expired
            second = await store.claim_due(KEY, later, later + timedelta(minutes=5))
            await store.complete(
                second,
                PulseOutcome(status="skipped", at=later, detail="not due"),
                next_check=later + timedelta(hours=1),
            )
            try:
                await store.complete(
                    first,
                    PulseOutcome(status="activated", at=later, delivered=True),
                    next_check=later + timedelta(minutes=1),
                )
            except StaleClaimError:
                return True
            return False

        assert run(scenario()) is True

    def test_keys_are_independent(self):
        store = self.make_store()
        other = RhythmKey(stream_id="stream-1", rhythm_id="curation")

        async def scenario():
            a = await store.claim_due(KEY, T0, T0 + timedelta(minutes=5))
            b = await store.claim_due(other, T0, T0 + timedelta(minutes=5))
            return a, b

        a, b = run(scenario())
        assert a is not None and b is not None
