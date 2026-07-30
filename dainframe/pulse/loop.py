"""the pulse loop (DESIGN.md §6.3): one cycle, per stream, per due rhythm.

due+claim -> decide -> plan -> gates -> build+act -> complete. every step
before build+act is cheap; a firing that dies at any of them spends zero
tokens. the engine's own invariants (precondition revalidation under the
stream lock, confirmed-delivery-before-recording, action truth) apply to the
activation unchanged - the pulse produces ordinary stimuli, nothing more.

ambient user-facing lines SHOULD be direct: the engine holds the stream
across delivery and recording (§11.15). a pulse configured with a
`pending_dispatcher` may accept pending lines and confirms them through the
engine; without one, a pending line is a configuration error recorded as a
failed firing, never an undelivered success.

failed sends stay out of the event log and do not consume the stream's
outreach allowance; they DO advance last_attempt and persist a bounded
retry horizon, so the next poll neither regenerates nor redelivers in a
tight loop. the library guarantees idempotent event-log confirmation and
at-most-one active claim; if a platform accepted a send and the process died
before confirmation, that external at-least-once edge is documented, not
magicked away (§6.3).

the app owns the lifecycle (`await pulse.run()` / `pulse.stop()`); the
library owns the loop's correctness.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Sequence

from dainframe.core.delivery import DeliveryReceipt
from dainframe.core.events import EventReader
from dainframe.core.types import PendingDelivery, Stimulus
from dainframe.pulse.gates import AllOf
from dainframe.pulse.rhythms import TaggedRhythm, evaluate
from dainframe.pulse.store import PulseClaim, PulseStore
from dainframe.pulse.types import (
    FiringPlan,
    PulseOutcome,
    PulseSource,
    RhythmKey,
    StimulusFactory,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Pulse:
    def __init__(
        self,
        *,
        source: PulseSource,
        factory: StimulusFactory,
        engine,
        store: PulseStore,
        events_for: Callable[[str], EventReader],
        gates: Sequence[object] = (),
        pending_dispatcher: Optional[
            Callable[[PendingDelivery], Awaitable[bool]]
        ] = None,
        now: Callable[[], datetime] = _utc_now,
        cycle: timedelta = timedelta(minutes=5),
        lease: timedelta = timedelta(minutes=5),
        denial_recheck: timedelta = timedelta(minutes=15),
        delivery_retry: timedelta = timedelta(minutes=30),
    ):
        self.source = source
        self.factory = factory
        self.engine = engine
        self.store = store
        self.events_for = events_for
        self.gate = AllOf(tuple(gates))
        self.pending_dispatcher = pending_dispatcher
        self._now = now
        self.cycle = cycle
        self.lease = lease
        self.denial_recheck = denial_recheck
        self.delivery_retry = delivery_retry
        self._running = False
        self._stopping: Optional[asyncio.Event] = None

    # --- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        self._stopping = asyncio.Event()
        while self._running:
            await self.tick()
            if not self._running:
                break
            # sleep until the next cycle - or until stop() interrupts it, so
            # shutdown is prompt instead of waiting out the full cycle
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.cycle.total_seconds()
                )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._running = False
        if self._stopping is not None:
            self._stopping.set()

    # --- one cycle -----------------------------------------------------------

    async def tick(self) -> None:
        try:
            streams = await self.source.streams()
        except Exception:
            logger.exception("pulse source failed; skipping cycle")
            return
        for stream_id, rhythms in streams:
            for tagged in rhythms:
                try:
                    await self._process(stream_id, tagged)
                except Exception:
                    # one broken rhythm must not silence the rest of the crew
                    logger.exception(
                        "pulse rhythm %s/%s failed", stream_id, tagged.rhythm_id
                    )

    async def _process(self, stream_id: str, tagged: TaggedRhythm) -> None:
        now = self._now()
        key = RhythmKey(stream_id=stream_id, rhythm_id=tagged.rhythm_id)

        # 1. due + claim: no claim means not due, or another worker owns it
        claim = await self.store.claim_due(key, now, now + self.lease)
        if claim is None:
            return

        events = self.events_for(stream_id)
        try:
            # 2. decide: rhythm arithmetic (and, for Dynamic, one decider call)
            evaluation = await evaluate(tagged, stream_id, claim.state, events, now)
            if evaluation.decision is None:
                await self.store.complete(
                    claim,
                    PulseOutcome(status="skipped", at=now, detail="not due"),
                    next_check=evaluation.next_check,
                    # calendar bookkeeping a quiet answer may still carry: a
                    # fresh rhythm's start boundary, or a misfire-skipped
                    # backlog being consumed
                    occurrence_key=evaluation.occurrence_key,
                )
                return
            decision = evaluation.decision

            # 3. plan: resolve WHO and WHERE before spending anything on WHAT
            # (the occurrence is NOT consumed: nothing happened yet, and a
            # transient no-plan must not eat a calendar firing)
            plan = await self.factory.plan(stream_id, tagged, decision)
            if plan is None:
                await self.store.complete(
                    claim,
                    PulseOutcome(status="skipped", at=now, detail="no plan"),
                    next_check=now + self.denial_recheck,
                )
                return

            # 4. gates: zero-token veto, with a persisted horizon when computable
            # (occurrence likewise not consumed - the grace window and misfire
            # policy decide what happens if the denial outlives it)
            verdict = await self.gate.check(plan, events, now)
            if not verdict.allowed:
                # denials are the loop's most-asked forensic question ("why
                # didn't it fire?" / "why DID it fire?") - answer it in logs,
                # not just in the store
                logger.info(
                    "pulse denied %s/%s: %s (retry %s)",
                    stream_id,
                    tagged.rhythm_id,
                    verdict.reason,
                    verdict.retry_at or f"in {self.denial_recheck}",
                )
                await self.store.complete(
                    claim,
                    PulseOutcome(status="denied", at=now, detail=verdict.reason),
                    next_check=verdict.retry_at or (now + self.denial_recheck),
                )
                return

            # 5. build + act: an ordinary stimulus through the ordinary engine
            stimulus = await self.factory.build(plan)
            result = await self.engine.handle(stimulus)
        except Exception as e:
            # unexpected failure: release the claim with a bounded retry so
            # a transient error neither loses the rhythm nor spins the loop
            await self.store.abandon(
                claim, retry_at=now + self.delivery_retry, reason=str(e)
            )
            raise

        # 6. complete: record what actually happened + the next horizon. the
        # occurrence is consumed only when the activation genuinely concluded
        # (ran, or was cancelled as stale) - a FAILED delivery keeps the
        # occurrence open so the retry re-fires the same morning brief
        # instead of silently skipping to tomorrow's
        outcome, next_check = await self._conclude(result, evaluation, now)
        consumed = outcome.status in ("activated", "cancelled")
        await self.store.complete(
            claim,
            outcome,
            next_check=next_check,
            occurrence_key=decision.occurrence_key if consumed else None,
        )

    async def _conclude(self, result, evaluation, now: datetime):
        if result.status == "cancelled":
            # the plan went stale under the stream lock (a user spoke);
            # cadence resumes from the firing horizon, nothing was spent
            return (
                PulseOutcome(status="cancelled", at=now, detail=result.status_reason),
                evaluation.next_after_fire,
            )
        if result.status == "noop":
            return (
                PulseOutcome(
                    status="skipped",
                    at=now,
                    detail=f"director noop: {result.status_reason}",
                ),
                evaluation.next_after_fire,
            )

        delivered = result.any_delivered
        generated = any(
            line.status in ("delivered", "pending") for line in result.lines
        )
        failed_lines = [l for l in result.lines if l.status == "errored"]

        # pending lines: confirm through the dispatcher, or fail loudly
        for line in result.lines:
            if line.status != "pending":
                continue
            if self.pending_dispatcher is None:
                logger.error(
                    "pulse activation produced a pending line but no "
                    "pending_dispatcher is configured (line %s)",
                    line.line_id,
                )
                return (
                    # `delivered` survives into the failed outcome: an
                    # earlier line's confirmed send is real, and losing it
                    # would let a rhythm-anchored retry duplicate outreach
                    PulseOutcome(
                        status="failed",
                        at=now,
                        generated=True,
                        delivered=delivered,
                        detail="pending line without a pending_dispatcher",
                    ),
                    now + self.delivery_retry,
                )
            sent = await self.pending_dispatcher(line.pending)
            if sent:
                await self.engine.confirm_delivery(
                    line.pending.pending_id, DeliveryReceipt()
                )
                delivered = True
            else:
                return (
                    PulseOutcome(
                        status="failed",
                        at=now,
                        generated=True,
                        delivered=delivered,
                        detail="platform did not confirm the send",
                    ),
                    now + self.delivery_retry,
                )

        if failed_lines and not delivered:
            return (
                PulseOutcome(
                    status="failed",
                    at=now,
                    generated=generated,
                    detail=(
                        failed_lines[0].error.message
                        if failed_lines[0].error
                        else "line errored"
                    ),
                ),
                now + self.delivery_retry,
            )

        return (
            PulseOutcome(
                status="activated",
                at=now,
                generated=generated,
                delivered=delivered,
            ),
            evaluation.next_after_fire,
        )
