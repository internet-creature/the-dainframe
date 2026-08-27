"""the shipped gates: chordial's non-interaction arithmetic, generalized."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from dainframe.core.events import EventQuery, InMemoryEventStore, NewEvent
from dainframe.pulse import (
    AllOf,
    BackoffGate,
    Cadence,
    CadenceGate,
    DeliveryHours,
    FiringPlan,
    GateDecision,
    NoNewerEvent,
    QuietHoursGate,
    RhythmKey,
    Rung,
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
    """an event store seeded with (author_type, author, message_type, at)
    rows; an optional fifth element names a non-message kind (an action)."""

    def __init__(self, rows):
        self._clock_value = None
        self.store = InMemoryEventStore(clock=lambda: self._clock_value)
        for row in rows:
            author_type, author, message_type, at = row[:4]
            kind = row[4] if len(row) > 4 else "message"
            self._clock_value = at
            run(
                self.store.append(
                    NewEvent(
                        author_type=author_type,
                        author=author,
                        kind=kind,
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


def banked(at):
    """a user-authored action event: showing up by doing, not saying."""
    return ("user", "user", None, at, "action")


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


# --- cadence ----------------------------------------------------------------


def test_cadence_parse_round_trips_the_canonical_spec():
    cadence = Cadence.parse("1d x3, 1w x3, 60d @ 8-11")
    assert cadence.rungs == (
        Rung(every=timedelta(days=1), tries=3),
        Rung(every=timedelta(weeks=1), tries=3),
        Rung(every=timedelta(days=60), tries=None),
    )
    assert cadence.during == DeliveryHours(start=8, end=11)
    assert str(cadence) == "1d x3, 1w x3, 60d @ 8-11"
    assert str(Cadence.parse("90m x2, 6h")) == "90m x2, 6h"


def test_cadence_parse_rejects_specs_that_could_misfire():
    for bad in (
        "",
        "1y x3",  # unknown unit
        "0d",  # zero wait
        "1d x0",  # zero tries
        "1d, 1w x3",  # uncounted rung before the end: 1w unreachable
        "1d @ 8",  # malformed hours
        "1d @ 8-8",  # empty hours
        "1d @ 8-24",  # hour out of range
        "999999999999d",  # would overflow timedelta - still ValueError
    ):
        with pytest.raises(ValueError):
            Cadence.parse(bad)


def test_cadence_wait_walks_the_ladder_and_reports_exhaustion():
    ladder = Cadence.parse("1d x3, 1w x3, 60d")
    assert [ladder.wait_for(n) for n in (1, 2, 3)] == [timedelta(days=1)] * 3
    assert [ladder.wait_for(n) for n in (4, 6)] == [timedelta(weeks=1)] * 2
    assert ladder.wait_for(7) == timedelta(days=60)
    assert ladder.wait_for(100) == timedelta(days=60)  # the eternal floor
    capped = Cadence.parse("1d x2")
    assert capped.wait_for(2) == timedelta(days=1)
    assert capped.wait_for(3) is None


def cadence_gate(spec="1d x3, 1w x3, 60d", **kwargs):
    # max_sleep=None so these tests assert the LADDER's exact horizons;
    # the bound has its own tests below
    kwargs.setdefault("max_sleep", None)
    return CadenceGate(Cadence.parse(spec), **kwargs)


def test_cadence_clear_when_the_user_spoke_last():
    events = SeededStore(
        [
            scheduled("aria", T0 - timedelta(hours=10)),
            user(T0 - timedelta(hours=1)),
        ]
    )
    assert run(cadence_gate().check(plan(), events, T0)).allowed


def test_cadence_first_rung_holds_a_day():
    newest = T0 - timedelta(hours=4)
    events = SeededStore([user(T0 - timedelta(days=1)), scheduled("aria", newest)])
    decision = run(cadence_gate().check(plan(), events, T0))
    assert not decision.allowed
    assert "cadence" in decision.reason
    assert decision.retry_at == newest + timedelta(days=1)
    later = newest + timedelta(days=1, minutes=1)
    assert run(cadence_gate().check(plan(), events, later)).allowed


def test_cadence_climbs_to_the_weekly_rung():
    newest = T0 - timedelta(days=2)
    events = SeededStore(
        [
            user(T0 - timedelta(days=30)),
            scheduled("aria", T0 - timedelta(days=10)),
            scheduled("aria", T0 - timedelta(days=8)),
            scheduled("aria", T0 - timedelta(days=5)),
            scheduled("aria", newest),
        ]
    )
    decision = run(cadence_gate().check(plan(), events, T0))
    assert not decision.allowed
    assert decision.retry_at == newest + timedelta(weeks=1)
    later = newest + timedelta(weeks=1, minutes=1)
    assert run(cadence_gate().check(plan(), events, later)).allowed


def test_cadence_floor_never_exhausts():
    """nine unanswered outreaches deep, the sixty-day floor still answers -
    presence never tapers to zero unless the ladder says so."""
    rows = [user(T0 - timedelta(days=300))]
    for days_ago in (200, 190, 180, 170, 160, 150, 140, 120):
        rows.append(scheduled("aria", T0 - timedelta(days=days_ago)))
    rows.append(scheduled("aria", T0 - timedelta(days=61)))
    events = SeededStore(rows)
    assert run(cadence_gate().check(plan(), events, T0)).allowed


def test_cadence_capped_ladder_goes_quiet_until_they_speak():
    rows = [
        user(T0 - timedelta(days=10)),
        scheduled("aria", T0 - timedelta(days=6)),
        scheduled("aria", T0 - timedelta(days=5)),
        scheduled("aria", T0 - timedelta(days=3)),
    ]
    decision = run(cadence_gate("1d x2").check(plan(), SeededStore(rows), T0))
    assert not decision.allowed
    assert "exhausted" in decision.reason
    assert decision.retry_at is None  # only the user speaking clears it
    replied = SeededStore(rows + [user(T0 - timedelta(hours=1))])
    assert run(cadence_gate("1d x2").check(plan(), replied, T0)).allowed


def test_cadence_delivery_hours_land_the_morning():
    gate = cadence_gate("1d x3 @ 8-11", tz_of=tz_utc)
    newest = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    events = SeededStore(
        [
            user(datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)),
            scheduled("aria", newest),
        ]
    )
    # the day is not up yet: the horizon is tomorrow 9:00, already a morning
    early = datetime(2026, 7, 2, 8, 30, tzinfo=timezone.utc)
    decision = run(gate.check(plan(), events, early))
    assert not decision.allowed
    assert decision.retry_at == datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    assert run(gate.check(plan(), events, decision.retry_at)).allowed

    # an afternoon outreach's horizon snaps forward to the NEXT morning
    afternoon = SeededStore(
        [user(datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)), scheduled("aria", T0)]
    )
    decision = run(
        gate.check(plan(), afternoon, datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc))
    )
    assert not decision.allowed
    assert decision.retry_at == datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc)


def test_cadence_delivery_hours_use_the_streams_own_timezone():
    async def tz_tokyo(stream_id):
        return "Asia/Tokyo"

    gate = cadence_gate("1d x3 @ 8-11", tz_of=tz_tokyo)
    # 23:30 UTC = 08:30 next day in tokyo, so tomorrow's horizon is a morning
    newest = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
    events = SeededStore(
        [
            user(datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)),
            scheduled("aria", newest),
        ]
    )
    decision = run(
        gate.check(plan(), events, datetime(2026, 7, 2, 23, 0, tzinfo=timezone.utc))
    )
    assert not decision.allowed
    assert decision.retry_at == datetime(2026, 7, 2, 23, 30, tzinfo=timezone.utc)
    later = datetime(2026, 7, 2, 23, 45, tzinfo=timezone.utc)
    assert run(gate.check(plan(), events, later)).allowed


def test_cadence_missed_window_waits_for_the_next_opening():
    """the horizon passed while the window was shut (a pulse outage, a long
    deny): the gate holds for the next opening instead of firing mid-day."""
    gate = cadence_gate("1d x3 @ 8-11", tz_of=tz_utc)
    newest = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    events = SeededStore(
        [
            user(datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)),
            scheduled("aria", newest),
        ]
    )
    decision = run(
        gate.check(plan(), events, datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc))
    )
    assert not decision.allowed
    assert decision.retry_at == datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc)


def test_cadence_per_stream_specs_resolve_through_the_callable():
    """the override seam: 'hold me at monthly' is a one-rung cadence the
    app hands back for this stream."""

    async def cadence_of(stream_id):
        assert stream_id == "s"
        return Cadence.parse("60d")

    newest = T0 - timedelta(days=2)
    events = SeededStore([user(T0 - timedelta(days=3)), scheduled("aria", newest)])
    decision = run(CadenceGate(cadence_of, max_sleep=None).check(plan(), events, T0))
    assert not decision.allowed
    assert decision.retry_at == newest + timedelta(days=60)


def test_cadence_delivery_hours_require_a_clock():
    events = SeededStore(
        [user(T0 - timedelta(days=2)), scheduled("aria", T0 - timedelta(days=1))]
    )
    with pytest.raises(ValueError):
        run(cadence_gate("1d @ 8-11").check(plan(), events, T0))


def test_cadence_denial_horizons_are_bounded_so_a_reply_can_land():
    """the pulse sleeps on a persisted denial horizon without consulting
    events: an unbounded sixty-day horizon would sleep through the reply
    that resets the ladder. the default bound turns it into six-hourly
    rechecks - one event read each, zero tokens."""
    newest = T0 - timedelta(days=2)
    events = SeededStore([user(T0 - timedelta(days=3)), scheduled("aria", newest)])
    decision = run(CadenceGate(Cadence.parse("60d")).check(plan(), events, T0))
    assert not decision.allowed
    assert decision.retry_at == T0 + timedelta(hours=6)
    tighter = CadenceGate(Cadence.parse("60d"), max_sleep=timedelta(hours=1))
    assert run(tighter.check(plan(), events, T0)).retry_at == T0 + timedelta(hours=1)


def test_cadence_saturated_window_exhausts_a_capped_ladder():
    """window full, no user message in it: the true unanswered count is only
    bounded below. a truncated count must never re-arm rungs the chain
    already spent - here two countable proactive sends hide behind two
    agent conversation rows, and the capped ladder reads as exhausted."""
    rows = [user(T0 - timedelta(days=30))]
    for days_ago in (20, 18, 16, 14, 12, 10):
        rows.append(scheduled("aria", T0 - timedelta(days=days_ago)))
    rows.append(("agent", "aria", "conversation", T0 - timedelta(days=9)))
    rows.append(("agent", "aria", "conversation", T0 - timedelta(days=8)))
    events = SeededStore(rows)
    gate = cadence_gate("1d x3", window=4)  # limit=max(4, 3+1): reads 4 rows
    decision = run(gate.check(plan(), events, T0))
    assert not decision.allowed
    assert "exhausted" in decision.reason


def test_cadence_saturated_window_floors_an_open_ladder():
    """same saturation, open ladder: the count reads as past every counted
    rung, so the chain waits the floor instead of re-running the dailies."""
    rows = [user(T0 - timedelta(days=30))]
    for days_ago in (20, 18, 16, 14, 12, 10):
        rows.append(scheduled("aria", T0 - timedelta(days=days_ago)))
    rows.append(("agent", "aria", "conversation", T0 - timedelta(days=9)))
    rows.append(("agent", "aria", "conversation", T0 - timedelta(days=8)))
    events = SeededStore(rows)
    gate = cadence_gate("1h x3, 60d", window=4)
    decision = run(gate.check(plan(), events, T0))
    assert not decision.allowed  # 1h passed long ago; the floor has not
    assert "cadence" in decision.reason


def test_cadence_default_reset_is_chat_shaped():
    """without opting in, only user MESSAGES reset: a user action after the
    unanswered outreach changes nothing, and the first rung still holds."""
    newest = T0 - timedelta(hours=4)
    events = SeededStore(
        [
            user(T0 - timedelta(days=1)),
            scheduled("aria", newest),
            banked(T0 - timedelta(hours=1)),
        ]
    )
    decision = run(cadence_gate().check(plan(), events, T0))
    assert not decision.allowed
    assert decision.retry_at == newest + timedelta(days=1)


def test_cadence_presence_kinds_let_showing_up_reset_the_ladder():
    """the desktop seam: with presence_kinds widened, banking a block counts
    as 'they showed up' - the ladder must not keep talking to someone who
    is already here."""
    events = SeededStore(
        [
            user(T0 - timedelta(days=1)),
            scheduled("aria", T0 - timedelta(hours=4)),
            banked(T0 - timedelta(hours=1)),
        ]
    )
    gate = cadence_gate(presence_kinds=frozenset({"message", "action"}))
    assert run(gate.check(plan(), events, T0)).allowed


def test_cadence_presence_older_than_the_outreach_does_not_reset():
    newest = T0 - timedelta(hours=4)
    events = SeededStore(
        [
            user(T0 - timedelta(days=1)),
            banked(T0 - timedelta(hours=6)),
            scheduled("aria", newest),
        ]
    )
    gate = cadence_gate(presence_kinds=frozenset({"message", "action"}))
    decision = run(gate.check(plan(), events, T0))
    assert not decision.allowed
    assert decision.retry_at == newest + timedelta(days=1)


def test_cadence_saturation_counts_messages_not_riding_actions():
    """widened kinds make AGENT action events ride along in the window
    (they never reset - presence is user-authored). they must not inflate
    the saturation arithmetic either: the window below is message-saturated
    with no user presence, so the capped ladder still reads as exhausted."""
    rows = [user(T0 - timedelta(days=30))]
    for days_ago in (20, 18, 16, 14, 12, 10):
        rows.append(scheduled("aria", T0 - timedelta(days=days_ago)))
    rows.append(("agent", "aria", "conversation", T0 - timedelta(days=9)))
    rows.append(("agent", "aria", None, T0 - timedelta(days=8, hours=12), "action"))
    rows.append(("agent", "aria", "conversation", T0 - timedelta(days=8)))
    events = SeededStore(rows)
    gate = cadence_gate(
        "1d x3", window=4, presence_kinds=frozenset({"message", "action"})
    )
    decision = run(gate.check(plan(), events, T0))
    assert not decision.allowed
    assert "exhausted" in decision.reason


def test_cadence_dst_fall_back_orders_by_instant_not_wall_clock():
    """the repeated hour: 08:30 utc on fall-back day is 1:30 MST, AFTER
    1:45 MDT (07:45 utc) by instant but BEFORE it by wall clock. same-zone
    aware comparison uses the wall clock (PEP 495), which denied this
    firing with a horizon already in the past."""

    async def tz_denver(stream_id):
        return "America/Denver"

    newest = datetime(2026, 10, 31, 7, 45, tzinfo=timezone.utc)
    events = SeededStore(
        [
            user(datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc)),
            scheduled("aria", newest),
        ]
    )
    gate = cadence_gate("1d @ 1-3", tz_of=tz_denver)
    # retry_at = newest + 1d = nov 1 07:45 utc = 1:45 MDT, inside 1-3 local;
    # now = 08:30 utc = 1:30 MST - a LATER instant in the same wall hour
    at = datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)
    assert run(gate.check(plan(), events, at)).allowed


def test_cadence_fails_closed_on_an_unresolvable_timezone(caplog):
    async def tz_broken(stream_id):
        return "Not/AZone"

    gate = cadence_gate("1d @ 8-11", tz_of=tz_broken)
    events = SeededStore(
        [user(T0 - timedelta(days=3)), scheduled("aria", T0 - timedelta(days=2))]
    )
    with caplog.at_level("WARNING", logger="dainframe.pulse.cadence"):
        decision = run(gate.check(plan(), events, T0))
    assert not decision.allowed
    assert "Not/AZone" in decision.reason
    assert decision.retry_at is None
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
