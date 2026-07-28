"""the pulse's durable state: a claim/complete/abandon state machine per
stable (stream, rhythm) key - never a read/update bag of timestamps
(DESIGN.md §11.10: concurrent loops double-fire, crashes leave ambiguous
state, failed sends regenerate every poll).

`claim_due` is atomic compare-and-set: two loops or processes cannot both own
one firing. the claim carries a state snapshot, so anchors like
"last_delivered" need no second read. completion with a stale claim (lease
expired, someone else claimed) raises instead of clobbering the new owner's
state.

`complete` takes `occurrence_key` as a phase-5 addition over the §6.2
signature: calendar occurrences must persist their dedup key atomically with
the horizon, or a crash between two writes re-fires the same morning brief.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from dainframe.pulse.types import PulseOutcome, RhythmKey


class StaleClaimError(RuntimeError):
    """the claim being completed/abandoned is no longer the active one."""


@dataclass(frozen=True)
class PulseState:
    """what the store remembers per rhythm key."""

    next_check: Optional[datetime] = None
    last_attempt: Optional[datetime] = None
    last_generated: Optional[datetime] = None
    last_delivered: Optional[datetime] = None
    occurrence_key: Optional[str] = None


@dataclass(frozen=True)
class PulseClaim:
    """exclusive ownership of one firing, with the state snapshot the
    evaluation will read."""

    key: RhythmKey
    claim_id: str
    lease_until: datetime
    state: PulseState


@runtime_checkable
class PulseStore(Protocol):
    async def claim_due(
        self, key: RhythmKey, now: datetime, lease_until: datetime
    ) -> Optional[PulseClaim]: ...

    async def complete(
        self,
        claim: PulseClaim,
        outcome: PulseOutcome,
        next_check: datetime,
        occurrence_key: Optional[str] = None,
    ) -> None: ...

    async def abandon(
        self, claim: PulseClaim, *, retry_at: datetime, reason: str
    ) -> None: ...


class InMemoryPulseStore:
    """single-process reference implementation. apps where duplicate ambient
    sends across restarts matter supply a durable one and run the same
    conformance suite (dainframe.testing.PulseStoreContract)."""

    def __init__(self):
        self._states: dict[RhythmKey, PulseState] = {}
        self._leases: dict[RhythmKey, tuple[str, datetime]] = {}
        self._lock = asyncio.Lock()

    async def claim_due(
        self, key: RhythmKey, now: datetime, lease_until: datetime
    ) -> Optional[PulseClaim]:
        async with self._lock:
            lease = self._leases.get(key)
            if lease is not None and lease[1] > now:
                return None  # someone else owns this firing
            state = self._states.get(key, PulseState())
            if state.next_check is not None and state.next_check > now:
                return None  # not due yet
            claim_id = f"pc-{uuid.uuid4().hex[:12]}"
            self._leases[key] = (claim_id, lease_until)
            return PulseClaim(
                key=key, claim_id=claim_id, lease_until=lease_until, state=state
            )

    async def complete(
        self,
        claim: PulseClaim,
        outcome: PulseOutcome,
        next_check: datetime,
        occurrence_key: Optional[str] = None,
    ) -> None:
        async with self._lock:
            self._require_active(claim)
            state = self._states.get(claim.key, PulseState())
            updates: dict = {"next_check": next_check}
            if occurrence_key is not None:
                updates["occurrence_key"] = occurrence_key
            if outcome.status in ("activated", "failed"):
                updates["last_attempt"] = outcome.at
            if outcome.generated:
                updates["last_generated"] = outcome.at
            if outcome.delivered:
                updates["last_delivered"] = outcome.at
            self._states[claim.key] = replace(state, **updates)
            del self._leases[claim.key]

    async def abandon(
        self, claim: PulseClaim, *, retry_at: datetime, reason: str
    ) -> None:
        async with self._lock:
            self._require_active(claim)
            state = self._states.get(claim.key, PulseState())
            self._states[claim.key] = replace(state, next_check=retry_at)
            del self._leases[claim.key]

    def _require_active(self, claim: PulseClaim) -> None:
        lease = self._leases.get(claim.key)
        if lease is None or lease[0] != claim.claim_id:
            raise StaleClaimError(
                f"claim {claim.claim_id} for {claim.key} is no longer active"
            )
