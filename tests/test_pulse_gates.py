"""the shipped gates: chordial's non-interaction arithmetic, generalized."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from dainframe.core.events import EventQuery, InMemoryEventStore, NewEvent
from dainframe.pulse import (
    AllOf,
    BackoffGate,
    FiringPlan,
    GateDecision,
    NoNewerEvent,
    QuietHoursGate,
    RhythmKey,
)

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def plan(actor="aria"):
    return FiringPlan(
        key=RhythmKey(stream_id="s", rhythm_id="checkin"),
        kind="scheduled_tick",
        due_at=T0,
        actor=actor,
    )


class SeededStore:
    """an event store seeded with (author_type, author, message_type, at)."""

    def __init__(self, rows):
        self._clock_value = None
        self.store = InMemoryEventStore(clock=lambda: self._clock_value)
        for author_type, author, message_type, at in rows:
            self._clock_value = at
            run(
                self.store.append(
                    NewEvent(
                        author_type=author_type,
                        author=author,
                        kind="message",
                        content="x",
                        message_type=message_type,
                    )
                )
            )

    def __getattr__(self, name):
        return getattr(self.store, name)


def scheduled(author, at):
    return ("agent", author, "scheduled", at)


def user(at):
    return ("user", "user", "conversation", at)


# --- backoff ---------------------------------------------------------------


def gate(**kwargs):
    defaults = dict(crew_cap=4, per_author_cap=3, base_interval=timedelta(hours=3))
    defaults.update(kwargs)
    return BackoffGate(**defaults)


def test_clear_stream_allows():
    events = SeededStore([user(T0 - timedelta(hours=5))])
    decision = run(gate().check(plan(), events, T0))
    assert decision.allowed


def test_user_message_resets_the_chain():
    events = SeededStore(
        [
            scheduled("aria", T0 - timedelta(hours=10)),
            scheduled("aria", T0 - timedelta(hours=8)),
            user(T0 - timedelta(hours=1)),
        ]
    )
    assert run(gate().check(plan(), events, T0)).allowed


def test_crew_cap_silences_everyone():
    events = SeededStore(
        [
            user(T0 - timedelta(days=3)),
            scheduled("aria", T0 - timedelta(hours=60)),
            scheduled("tempo", T0 - timedelta(hours=48)),
            scheduled("aria", T0 - timedelta(hours=30)),
            scheduled("tempo", T0 - timedelta(hours=20)),
        ]
    )
    decision = run(gate().check(plan("cadence"), events, T0))
    assert not decision.allowed
    assert "crew cap" in decision.reason
    assert decision.retry_at is None  # only the user speaking clears it


def test_per_author_cap_silences_just_that_author():
    events = SeededStore(
        [
            user(T0 - timedelta(days=3)),
            scheduled("aria", T0 - timedelta(hours=60)),
            scheduled("aria", T0 - timedelta(hours=40)),
            scheduled("aria", T0 - timedelta(hours=20)),
        ]
    )
    denied = run(gate().check(plan("aria"), events, T0))
    assert not denied.allowed and "aria cap" in denied.reason
    other = run(gate().check(plan("tempo"), events, T0))
    assert other.allowed


def test_per_author_cap_without_an_actor_fails_loudly():
    events = SeededStore([user(T0 - timedelta(days=1))])
    with pytest.raises(ValueError):
        run(gate().check(plan(actor=None), events, T0))


def test_backoff_doubles_and_carries_its_horizon():
    """two unanswered -> 6h required since the newest."""
    newest = T0 - timedelta(hours=4)
    events = SeededStore(
        [
            user(T0 - timedelta(days=1)),
            scheduled("aria", T0 - timedelta(hours=9)),
            scheduled("aria", newest),
        ]
    )
    decision = run(gate(per_author_cap=3).check(plan("tempo"), events, T0))
    assert not decision.allowed
    assert "backoff" in decision.reason
    assert decision.retry_at == newest + timedelta(hours=6)
    # ...and once the horizon passes, the gate clears
    later = newest + timedelta(hours=6, minutes=1)
    assert run(gate().check(plan("tempo"), events, later)).allowed


def test_actions_and_notes_never_count_on_either_side():
    events = SeededStore([user(T0 - timedelta(days=2))])
    run(
        events.store.append(
            NewEvent(
                author_type="agent",
                author="aria",
                kind="action",
                content="[aria used save_memory]",
            )
        )
    )
    run(
        events.store.append(
            NewEvent(
                author_type="agent",
                author="aria",
                kind="note",
                content="notice",
            )
        )
    )
    assert run(gate().check(plan(), events, T0)).allowed


# --- quiet hours ------------------------------------------------------------


async def tz_utc(stream_id):
    return "UTC"


def test_quiet_hours_denies_the_local_night_and_knows_when_it_ends():
    quiet = QuietHoursGate(start=21, end=8, tz_of=tz_utc)
    night = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
    decision = run(quiet.check(plan(), InMemoryEventStore(), night))
    assert not decision.allowed
    assert decision.retry_at == datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc)

    early = datetime(2026, 7, 2, 3, 0, tzinfo=timezone.utc)
    decision = run(quiet.check(plan(), InMemoryEventStore(), early))
    assert not decision.allowed
    assert decision.retry_at == datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc)

    day = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert run(quiet.check(plan(), InMemoryEventStore(), day)).allowed


def test_quiet_hours_uses_the_streams_own_timezone():
    async def tz_tokyo(stream_id):
        return "Asia/Tokyo"

    quiet = QuietHoursGate(start=21, end=8, tz_of=tz_tokyo)
    # 14:00 UTC = 23:00 in tokyo: night there, midday in utc
    at = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
    assert not run(quiet.check(plan(), InMemoryEventStore(), at)).allowed


def test_quiet_hours_resolves_legacy_link_names():
    """US/Pacific is a backward-compat link, present in pytz but dropped by
    minimal system tz databases - the exact name that made the gate silently
    fall back to UTC in production. the tzdata dependency guarantees it."""

    async def tz_legacy(stream_id):
        return "US/Pacific"

    quiet = QuietHoursGate(start=21, end=8, tz_of=tz_legacy)
    # 10:00 UTC = 03:00 pacific (PDT): deep in the local night. the old
    # fallback saw 10:00 utc and ALLOWED this firing
    at = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert not run(quiet.check(plan(), InMemoryEventStore(), at)).allowed


def test_quiet_hours_fails_closed_on_an_unresolvable_timezone(caplog):
    """not knowing what time it is for the stream denies the firing - loudly.
    the old behavior guessed UTC, which read as 'daytime' during the user's
    actual night and let the bot message them at 3am."""

    async def tz_broken(stream_id):
        return "Not/AZone"

    quiet = QuietHoursGate(start=21, end=8, tz_of=tz_broken)
    at = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)  # would pass in UTC
    with caplog.at_level("WARNING", logger="dainframe.pulse.gates"):
        decision = run(quiet.check(plan(), InMemoryEventStore(), at))
    assert not decision.allowed
    assert "Not/AZone" in decision.reason
    assert decision.retry_at is None  # no horizon computable without a clock
    assert any("failing closed" in r.message for r in caplog.records)


# --- composition ------------------------------------------------------------


class Allow:
    async def check(self, firing, events, now):
        return GateDecision(True, "clear")


class Deny:
    def __init__(self, reason):
        self.reason = reason

    async def check(self, firing, events, now):
        return GateDecision(False, self.reason)


def test_all_of_first_denial_wins():
    stack = AllOf((Allow(), Deny("first"), Deny("second")))
    decision = run(stack.check(plan(), InMemoryEventStore(), T0))
    assert not decision.allowed
    assert decision.reason == "first"
    assert run(AllOf(()).check(plan(), InMemoryEventStore(), T0)).allowed


# --- the plan-staleness precondition ----------------------------------------


def test_no_newer_event_goes_stale_when_the_user_speaks():
    events = SeededStore([user(T0 - timedelta(hours=2))])
    query = EventQuery(kinds=frozenset({"message"}), author_types=frozenset({"user"}))
    precondition = NoNewerEvent(query, than=T0)
    assert run(precondition.holds(events))
    events._clock_value = T0 + timedelta(minutes=1)
    run(
        events.store.append(
            NewEvent(
                author_type="user",
                author="user",
                kind="message",
                content="hey!",
                message_type="conversation",
            )
        )
    )
    assert not run(precondition.holds(events))
