"""the pulse loop end-to-end over fakes: claim -> decide -> plan -> gate ->
act -> complete, with every off-ramp taken."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from dainframe.core.events import InMemoryEventStore
from dainframe.core.types import (
    ActivationResult,
    DeliveryTarget,
    EventContext,
    ExecutionErrorInfo,
    LineResult,
    PendingDelivery,
    Stimulus,
)
from dainframe.pulse import (
    FiringPlan,
    GateDecision,
    InMemoryPulseStore,
    Interval,
    Pulse,
    RhythmKey,
    TaggedRhythm,
)

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, delta):
        self.at += delta


def line(status, *, speaker="aria", pending=None, error=None):
    return LineResult(
        line_id="act:0",
        speaker=speaker,
        status=status,
        pending=pending,
        error=error,
    )


def activation(*lines, status="completed", reason=None):
    return ActivationResult(
        activation_id="act",
        stream_id="s1",
        inbound_event_id=None,
        status=status,
        status_reason=reason,
        lines=tuple(lines),
    )


class FakeEngine:
    def __init__(self, results):
        self.results = list(results)
        self.handled: list[Stimulus] = []
        self.confirmed: list[str] = []

    async def handle(self, stimulus):
        self.handled.append(stimulus)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def confirm_delivery(self, pending_id, receipt):
        self.confirmed.append(pending_id)


class Factory:
    def __init__(self, plans=True):
        self.plans = plans
        self.planned = []
        self.built = []

    async def plan(self, stream_id, rhythm, decision):
        self.planned.append(decision)
        if not self.plans:
            return None
        return FiringPlan(
            key=RhythmKey(stream_id=stream_id, rhythm_id=rhythm.rhythm_id),
            kind=rhythm.kind,
            due_at=decision.due_at,
            actor="aria",
            target=DeliveryTarget(platform="telegram", target_id="t1"),
            reason=decision.reason,
        )

    async def build(self, plan):
        self.built.append(plan)
        return Stimulus(
            kind=plan.kind,
            stream_id=plan.key.stream_id,
            record_inbound=False,
            target=plan.target,
            reason=plan.reason,
        )


class Source:
    def __init__(self, *streams):
        self._streams = list(streams)

    async def streams(self):
        return self._streams


def checkin(every=timedelta(hours=1)):
    return TaggedRhythm(
        rhythm_id="checkin",
        kind="scheduled_tick",
        rhythm=Interval(every=every, anchor="last_delivered"),
    )


def wire(engine_results, *, gates=(), plans=True, dispatcher=None, clock=None):
    clock = clock or Clock()
    engine = FakeEngine(engine_results)
    factory = Factory(plans=plans)
    store = InMemoryPulseStore()
    pulse = Pulse(
        source=Source(("s1", [checkin()])),
        factory=factory,
        engine=engine,
        store=store,
        events_for=lambda sid: InMemoryEventStore(),
        gates=gates,
        pending_dispatcher=dispatcher,
        now=clock,
    )
    return pulse, engine, factory, store, clock


def test_due_rhythm_activates_and_schedules_the_next_beat():
    pulse, engine, factory, store, clock = wire([activation(line("delivered"))])
    run(pulse.tick())

    assert [s.kind for s in engine.handled] == ["scheduled_tick"]
    assert engine.handled[0].target.platform == "telegram"

    # immediately after: not due again (the horizon holds)
    run(pulse.tick())
    assert len(engine.handled) == 1

    # after `every`: due again, and the state remembers the delivery
    clock.advance(timedelta(hours=1, minutes=1))
    engine.results.append(activation(line("delivered")))
    run(pulse.tick())
    assert len(engine.handled) == 2


def test_gate_denial_spends_no_tokens_and_persists_its_horizon():
    class DenyUntil:
        def __init__(self, retry_at):
            self.retry_at = retry_at
            self.checks = 0

        async def check(self, firing, events, now):
            self.checks += 1
            if now < self.retry_at:
                return GateDecision(False, "backoff", retry_at=self.retry_at)
            return GateDecision(True, "clear")

    gate = DenyUntil(T0 + timedelta(hours=2))
    pulse, engine, factory, store, clock = wire(
        [activation(line("delivered"))], gates=(gate,)
    )
    run(pulse.tick())
    assert engine.handled == []  # zero-token denial
    assert gate.checks == 1

    clock.advance(timedelta(hours=1))
    run(pulse.tick())
    assert gate.checks == 1  # horizon persisted: no hot-polling

    clock.advance(timedelta(hours=1, minutes=1))
    run(pulse.tick())
    assert engine.handled != []  # past the horizon: fires


def test_no_plan_completes_without_generation():
    pulse, engine, factory, store, clock = wire([], plans=False)
    run(pulse.tick())
    assert factory.planned != []
    assert engine.handled == []


def test_cancelled_activation_is_recorded_not_retried_hot():
    pulse, engine, factory, store, clock = wire(
        [activation(status="cancelled", reason="precondition no longer holds")]
    )
    run(pulse.tick())
    assert len(engine.handled) == 1
    run(pulse.tick())
    assert len(engine.handled) == 1  # cadence resumes at the next beat


def test_failed_delivery_persists_a_bounded_retry():
    failed = activation(
        line(
            "errored",
            error=ExecutionErrorInfo(
                kind="delivery_failed", message="send failed", retryable=True
            ),
        )
    )
    pulse, engine, factory, store, clock = wire([failed, activation(line("delivered"))])
    run(pulse.tick())
    assert len(engine.handled) == 1

    # within the retry window: no regeneration
    clock.advance(timedelta(minutes=5))
    run(pulse.tick())
    assert len(engine.handled) == 1

    # past it: tries again
    clock.advance(timedelta(minutes=26))
    run(pulse.tick())
    assert len(engine.handled) == 2


def test_pending_line_without_dispatcher_is_a_failed_firing():
    pending = PendingDelivery(
        pending_id="pd-1",
        stream_id="s1",
        activation_id="act",
        line_id="act:0",
        speaker="aria",
        target=DeliveryTarget(platform="telegram", target_id="t1"),
        text="hello!",
        event_context=EventContext(),
    )
    pulse, engine, factory, store, clock = wire(
        [activation(line("pending", pending=pending))]
    )
    run(pulse.tick())
    assert engine.confirmed == []  # nothing confirmed
    run(pulse.tick())
    assert len(engine.handled) == 1  # and the retry horizon holds


def test_pending_line_with_dispatcher_is_sent_and_confirmed():
    pending = PendingDelivery(
        pending_id="pd-1",
        stream_id="s1",
        activation_id="act",
        line_id="act:0",
        speaker="aria",
        target=DeliveryTarget(platform="telegram", target_id="t1"),
        text="hello!",
        event_context=EventContext(),
    )
    sent = []

    async def dispatcher(p):
        sent.append(p.pending_id)
        return True

    pulse, engine, factory, store, clock = wire(
        [activation(line("pending", pending=pending))], dispatcher=dispatcher
    )
    run(pulse.tick())
    assert sent == ["pd-1"]
    assert engine.confirmed == ["pd-1"]  # confirmed through the engine


def test_engine_exception_abandons_with_retry_and_recovers():
    pulse, engine, factory, store, clock = wire(
        [RuntimeError("provider down"), activation(line("delivered"))]
    )
    run(pulse.tick())  # exception is contained by tick()
    assert len(engine.handled) == 1

    clock.advance(timedelta(minutes=31))
    run(pulse.tick())
    assert len(engine.handled) == 2  # recovered after the retry horizon


def test_one_broken_rhythm_does_not_silence_the_rest():
    class ExplodingFactory(Factory):
        async def plan(self, stream_id, rhythm, decision):
            if rhythm.rhythm_id == "broken":
                raise RuntimeError("boom")
            return await super().plan(stream_id, rhythm, decision)

    clock = Clock()
    engine = FakeEngine([activation(line("delivered"))])
    pulse = Pulse(
        source=Source(
            (
                "s1",
                [
                    TaggedRhythm(
                        rhythm_id="broken",
                        kind="tick",
                        rhythm=Interval(
                            every=timedelta(hours=1), anchor="last_delivered"
                        ),
                    ),
                    checkin(),
                ],
            ),
        ),
        factory=ExplodingFactory(),
        engine=engine,
        store=InMemoryPulseStore(),
        events_for=lambda sid: InMemoryEventStore(),
        now=clock,
    )
    run(pulse.tick())
    assert len(engine.handled) == 1  # the healthy rhythm still fired
