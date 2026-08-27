"""the naive-UTC boundary: the protocol says aware-UTC, but real adapters
surface datetime.utcnow() rows (chordial's SQL store). the pulse treats naive
timestamps as UTC instead of corrupting a conversion or raising mid-cycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from dainframe.core.events import EventQuery, InMemoryEventStore, NewEvent
from dainframe.pulse import (
    BackoffGate,
    Cadence,
    CadenceGate,
    FiringPlan,
    Interval,
    NoNewerEvent,
    PulseState,
    RhythmKey,
    TaggedRhythm,
    evaluate,
)

AWARE_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
NAIVE_PAST = datetime(2026, 7, 1, 8, 0)  # same clock, no tzinfo


def run(coro):
    return asyncio.run(coro)


def naive_store(
    created_at=NAIVE_PAST,
    message_type="conversation",
    author_type="user",
    author="user",
):
    store = InMemoryEventStore(clock=lambda: created_at)
    run(
        store.append(
            NewEvent(
                author_type=author_type,
                author=author,
                kind="message",
                content="x",
                message_type=message_type,
            )
        )
    )
    return store


def test_interval_anchor_treats_naive_rows_as_utc():
    tagged = TaggedRhythm(
        rhythm_id="r",
        kind="tick",
        rhythm=Interval(
            every=timedelta(hours=1),
            anchor=EventQuery(kinds=frozenset({"message"})),
        ),
    )
    ev = run(evaluate(tagged, "s", PulseState(), naive_store(), AWARE_NOW))
    assert ev.decision is not None  # 4h since the naive anchor: due
    assert ev.decision.due_at == datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


def test_backoff_gate_compares_naive_rows_against_aware_now():
    store = naive_store(
        created_at=datetime(2026, 7, 1, 11, 0),  # 1h ago, naive
        message_type="scheduled",
        author_type="agent",
        author="aria",
    )
    gate = BackoffGate(base_interval=timedelta(hours=3), per_author_cap=0)
    plan = FiringPlan(
        key=RhythmKey(stream_id="s", rhythm_id="r"),
        kind="tick",
        due_at=AWARE_NOW,
        actor="aria",
    )
    decision = run(gate.check(plan, store, AWARE_NOW))
    assert not decision.allowed
    assert decision.retry_at == datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)


def test_cadence_gate_compares_naive_rows_against_aware_now():
    store = naive_store(
        created_at=datetime(2026, 7, 1, 11, 0),  # 1h ago, naive
        message_type="scheduled",
        author_type="agent",
        author="aria",
    )
    gate = CadenceGate(Cadence.parse("1d"), max_sleep=None)
    plan = FiringPlan(
        key=RhythmKey(stream_id="s", rhythm_id="r"),
        kind="tick",
        due_at=AWARE_NOW,
        actor="aria",
    )
    decision = run(gate.check(plan, store, AWARE_NOW))
    assert not decision.allowed
    assert decision.retry_at == datetime(2026, 7, 2, 11, 0, tzinfo=timezone.utc)


def test_no_newer_event_mixes_naive_rows_and_aware_horizons():
    store = naive_store()
    query = EventQuery(kinds=frozenset({"message"}))
    assert run(NoNewerEvent(query, than=AWARE_NOW).holds(store))
    earlier = datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)
    assert not run(NoNewerEvent(query, than=earlier).holds(store))
