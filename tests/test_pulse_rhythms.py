"""rhythm evaluation: interval anchors, calendar crons in local time,
dynamic deciders with clamped horizons."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from dainframe.core.events import EventQuery, InMemoryEventStore, NewEvent
from dainframe.pulse import (
    Calendar,
    Decision,
    Dynamic,
    Interval,
    PulseState,
    TaggedRhythm,
    evaluate,
    next_cron_occurrence,
    parse_cron,
)

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def tagged(rhythm):
    return TaggedRhythm(rhythm_id="r", kind="tick", rhythm=rhythm)


def store_with_message(created_at):
    store = InMemoryEventStore(clock=lambda: created_at)
    run(
        store.append(
            NewEvent(
                author_type="user",
                author="user",
                kind="message",
                content="hi",
                message_type="conversation",
            )
        )
    )
    return store


USER_MESSAGES = EventQuery(
    kinds=frozenset({"message"}), author_types=frozenset({"user"})
)


# --- interval ---------------------------------------------------------------


def test_interval_fires_when_every_has_passed_since_query_anchor():
    store = store_with_message(T0 - timedelta(hours=2))
    ev = run(
        evaluate(
            tagged(Interval(every=timedelta(hours=1), anchor=USER_MESSAGES)),
            "s",
            PulseState(),
            store,
            T0,
        )
    )
    assert ev.decision is not None
    assert ev.decision.due_at == T0 - timedelta(hours=1)
    # cadence resumes from the firing, not the stale anchor - a nudge whose
    # anchor our own outreach can't move must not hot-refire
    assert ev.next_after_fire == T0 + timedelta(hours=1)


def test_interval_waits_and_reports_the_exact_horizon():
    store = store_with_message(T0 - timedelta(minutes=10))
    ev = run(
        evaluate(
            tagged(Interval(every=timedelta(hours=1), anchor=USER_MESSAGES)),
            "s",
            PulseState(),
            store,
            T0,
        )
    )
    assert ev.decision is None
    assert ev.next_check == T0 + timedelta(minutes=50)


def test_interval_with_no_anchor_at_all_is_due_now():
    """first contact: nothing to be recent to."""
    ev = run(
        evaluate(
            tagged(Interval(every=timedelta(hours=1), anchor=USER_MESSAGES)),
            "s",
            PulseState(),
            InMemoryEventStore(),
            T0,
        )
    )
    assert ev.decision is not None
    assert ev.decision.due_at == T0


def test_interval_state_anchors_read_the_pulse_state():
    delivered = T0 - timedelta(minutes=30)
    ev = run(
        evaluate(
            tagged(Interval(every=timedelta(hours=1), anchor="last_delivered")),
            "s",
            PulseState(last_delivered=delivered),
            InMemoryEventStore(),
            T0,
        )
    )
    assert ev.decision is None
    assert ev.next_check == delivered + timedelta(hours=1)


def test_interval_jitter_is_bounded_and_only_pushes_later():
    store = store_with_message(T0 - timedelta(minutes=10))
    for _ in range(20):
        ev = run(
            evaluate(
                tagged(
                    Interval(
                        every=timedelta(hours=1),
                        anchor=USER_MESSAGES,
                        jitter=timedelta(minutes=10),
                    )
                ),
                "s",
                PulseState(),
                store,
                T0,
            )
        )
        base = T0 + timedelta(minutes=50)
        assert base <= ev.next_check <= base + timedelta(minutes=10)


# --- calendar ---------------------------------------------------------------


async def _tz_utc(stream_id):
    return "UTC"


async def _tz_ny(stream_id):
    return "America/New_York"


def test_cron_parses_and_finds_next_occurrence():
    occurrence = next_cron_occurrence("0 9 * * *", "UTC", T0)
    assert occurrence == datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)


def test_cron_respects_local_timezone():
    # 9am in new york on 2026-07-01 is 13:00 UTC (EDT)
    occurrence = next_cron_occurrence(
        "0 9 * * *",
        "America/New_York",
        datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert occurrence == datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)


def test_cron_rejects_malformed_specs():
    for bad in ("0 9 * *", "61 * * * *", "* * * * 9", "*/0 * * * *"):
        with pytest.raises(ValueError):
            parse_cron(bad)


def test_calendar_fresh_key_owes_no_retroactive_firing():
    ev = run(
        evaluate(
            tagged(Calendar(cron="0 9 * * *", tz_of=_tz_utc)),
            "s",
            PulseState(),
            InMemoryEventStore(),
            T0,  # noon: 9am already past
        )
    )
    assert ev.decision is None
    assert ev.next_check == datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)


def test_calendar_fires_a_due_occurrence_once():
    state = PulseState(occurrence_key="2026-07-01T09:00:00+00:00")
    now = datetime(2026, 7, 2, 9, 2, tzinfo=timezone.utc)  # polling latency
    ev = run(
        evaluate(
            tagged(Calendar(cron="0 9 * * *", tz_of=_tz_utc)),
            "s",
            state,
            InMemoryEventStore(),
            now,
        )
    )
    assert ev.decision is not None
    assert ev.decision.occurrence_key == "2026-07-02T09:00:00+00:00"
    assert ev.next_after_fire == datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)


def test_calendar_misfire_skip_drops_stale_occurrences():
    """down for a day: skip jumps to the future instead of firing stale."""
    state = PulseState(occurrence_key="2026-07-01T09:00:00+00:00")
    now = datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc)  # missed two 9ams
    ev = run(
        evaluate(
            tagged(Calendar(cron="0 9 * * *", tz_of=_tz_utc, misfire="skip")),
            "s",
            state,
            InMemoryEventStore(),
            now,
        )
    )
    assert ev.decision is None
    assert ev.next_check == datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)


def test_calendar_misfire_fire_once_collapses_the_backlog():
    state = PulseState(occurrence_key="2026-07-01T09:00:00+00:00")
    now = datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc)
    ev = run(
        evaluate(
            tagged(Calendar(cron="0 9 * * *", tz_of=_tz_utc, misfire="fire_once")),
            "s",
            state,
            InMemoryEventStore(),
            now,
        )
    )
    assert ev.decision is not None  # one firing...
    assert ev.decision.occurrence_key == "2026-07-03T09:00:00+00:00"
    assert ev.next_after_fire == datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)


# --- dynamic ----------------------------------------------------------------


class ScriptedDecider:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def decide(self, stream_id, events, now):
        self.calls.append((stream_id, now))
        return self.decision


def test_dynamic_fire_carries_the_deciders_reason():
    decider = ScriptedDecider(
        Decision(
            fire=True,
            next_check=T0 + timedelta(hours=2),
            reason="they seem low",
        )
    )
    ev = run(
        evaluate(
            tagged(Dynamic(decider=decider)),
            "s",
            PulseState(),
            InMemoryEventStore(),
            T0,
        )
    )
    assert ev.decision is not None
    assert ev.decision.reason == "they seem low"
    assert ev.next_after_fire == T0 + timedelta(hours=2)
    assert decider.calls == [("s", T0)]


def test_dynamic_decline_sleeps_until_the_decider_says():
    decider = ScriptedDecider(
        Decision(
            fire=False,
            next_check=T0 + timedelta(hours=3),
        )
    )
    ev = run(
        evaluate(
            tagged(Dynamic(decider=decider)),
            "s",
            PulseState(),
            InMemoryEventStore(),
            T0,
        )
    )
    assert ev.decision is None
    assert ev.next_check == T0 + timedelta(hours=3)


def test_dynamic_never_trusts_a_decider_beyond_the_clamp():
    too_far = ScriptedDecider(Decision(fire=False, next_check=T0 + timedelta(days=30)))
    too_soon = ScriptedDecider(Decision(fire=False, next_check=T0 - timedelta(hours=1)))
    ev_far = run(
        evaluate(
            tagged(Dynamic(decider=too_far, max_sleep=timedelta(hours=6))),
            "s",
            PulseState(),
            InMemoryEventStore(),
            T0,
        )
    )
    ev_soon = run(
        evaluate(
            tagged(Dynamic(decider=too_soon, min_sleep=timedelta(minutes=5))),
            "s",
            PulseState(),
            InMemoryEventStore(),
            T0,
        )
    )
    assert ev_far.next_check == T0 + timedelta(hours=6)
    assert ev_soon.next_check == T0 + timedelta(minutes=5)
