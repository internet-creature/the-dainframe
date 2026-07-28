"""rhythms: abstract descriptions of WHEN (DESIGN.md §6.1), plus the
evaluation that turns one into a concrete due/not-due answer.

- Interval: recency-anchored - fire when `every` has passed since the anchor.
  the anchor is an explicit EventQuery or a pulse-state timestamp; the vague
  string "last_message" is deliberately rejected (§6.1) - candidate silence
  must not be reset by a scorer action, and one rhythm may want human recency
  while another wants last successful outreach.
- Calendar: clock-anchored - a five-field cron evaluated in the stream's own
  timezone. occurrences are keyed and fire at most once; downtime follows the
  misfire policy.
- Dynamic: decided cadence - a Decider is asked "fire now? and when should I
  next even check?". THE seam for an AI check-in gate; the decider's reason
  rides Stimulus.reason. deciders are never trusted forever: next_check is
  clamped into [min_sleep, max_sleep].

evaluation is pure with respect to the store: it reads the claim's state
snapshot and the stream's events, and returns both the firing decision and
the next horizon, so the loop owns no per-rhythm arithmetic.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Literal,
    Optional,
    Protocol,
    Union,
    runtime_checkable,
)
from zoneinfo import ZoneInfo

from dainframe.core.events import EventQuery, EventReader

if TYPE_CHECKING:  # pragma: no cover
    from dainframe.pulse.store import PulseState


@dataclass(frozen=True)
class Decision:
    """a Decider's answer: fire now (with an optional reason that becomes
    Stimulus.reason), and when to next even check."""

    fire: bool
    next_check: datetime
    reason: Optional[str] = None


@runtime_checkable
class Decider(Protocol):
    async def decide(
        self, stream_id: str, events: EventReader, now: datetime
    ) -> Decision: ...


@dataclass(frozen=True)
class Interval:
    """recency-anchored cadence: fire when `every` has passed since the
    anchor. anchor absent entirely (no matching event, no state) means due
    now - the first-contact shape. jitter is sampled once per occurrence and
    baked into the persisted horizon, never resampled per poll (§6.1)."""

    every: timedelta
    anchor: Union[EventQuery, Literal["last_delivered", "last_attempt"]]
    jitter: Optional[timedelta] = None


@dataclass(frozen=True)
class Calendar:
    """clock-anchored: fire at local cron times in the stream's timezone
    (resolved per-stream via `tz_of`). the morning-brief shape."""

    cron: str
    tz_of: Callable[[str], Awaitable[str]]
    misfire: Literal["skip", "fire_once"] = "skip"


@dataclass(frozen=True)
class Dynamic:
    """decided cadence. max_sleep: never trust a decider forever; min_sleep:
    never let one spin the loop."""

    decider: Decider
    max_sleep: timedelta = timedelta(hours=6)
    min_sleep: timedelta = timedelta(minutes=1)


Rhythm = Union[Interval, Calendar, Dynamic]


@dataclass(frozen=True)
class TaggedRhythm:
    """one rhythm on one stream: an app-chosen stable `rhythm_id` (unique
    within the stream) and an app-chosen `kind` that flows into the
    stimulus."""

    rhythm_id: str
    kind: str
    rhythm: Rhythm


@dataclass(frozen=True)
class Evaluation:
    """the loop's whole per-rhythm answer. `decision` present = due now;
    absent = quiet until `next_check`. `next_after_fire` is the horizon to
    persist if this firing goes ahead."""

    decision: Optional["RhythmDecision"]
    next_check: datetime
    next_after_fire: datetime


# --- calendar: a small five-field cron -----------------------------------
#
# fields: minute hour day-of-month month day-of-week (0-6, 0=sunday, 7=0).
# supports "*", numbers, comma lists, ranges, and steps ("*/15", "9-17/2").
# standard quirk honored: when BOTH day-of-month and day-of-week are
# restricted, a time matches if EITHER does. names ("mon", "jan") are not
# supported - numbers only.
#
# occurrences are found by scanning UTC minutes and converting each to the
# stream's local zone, which makes DST behavior fall out naturally:
# nonexistent local times simply never appear, and repeated local times are
# deduplicated by occurrence key.

_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

# an occurrence older than this is a misfire (downtime), not a normal firing
# caught a few minutes late by the polling cycle.
CALENDAR_GRACE = timedelta(hours=1)

# a cron that never matches must fail loudly, not scan forever.
_MAX_SCAN = timedelta(days=366)


def _parse_field(spec: str, lo: int, hi: int) -> Optional[frozenset[int]]:
    """None means unrestricted ('*')."""
    if spec == "*":
        return None
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"cron step must be >= 1: {spec!r}")
        if part == "*":
            lo_p, hi_p = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            lo_p, hi_p = int(a), int(b)
        else:
            lo_p = hi_p = int(part)
        if not (lo <= lo_p <= hi and lo <= hi_p <= hi and lo_p <= hi_p):
            raise ValueError(f"cron field out of range: {spec!r}")
        values.update(range(lo_p, hi_p + 1, step))
    return frozenset(values)


def parse_cron(cron: str) -> tuple[Optional[frozenset[int]], ...]:
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(parts)}: {cron!r}")
    fields = tuple(
        _parse_field(spec, lo, hi) for spec, (lo, hi) in zip(parts, _FIELD_BOUNDS)
    )
    dow = fields[4]
    if dow is not None and 7 in dow:  # cron tradition: sunday is 0 and 7
        fields = fields[:4] + (frozenset((dow - {7}) | {0}),)
    return fields


def _matches(fields, local: datetime) -> bool:
    minute, hour, dom, month, dow = fields
    if minute is not None and local.minute not in minute:
        return False
    if hour is not None and local.hour not in hour:
        return False
    if month is not None and local.month not in month:
        return False
    cron_dow = (local.weekday() + 1) % 7  # python monday=0 -> cron sunday=0
    dom_ok = dom is None or local.day in dom
    dow_ok = dow is None or cron_dow in dow
    if dom is not None and dow is not None:
        return dom_ok or dow_ok  # the standard either-matches quirk
    return dom_ok and dow_ok


def next_cron_occurrence(cron: str, tz: str, after: datetime) -> datetime:
    """the first occurrence strictly after `after` (tz-aware UTC in, UTC
    out). raises if the cron never fires within a year."""
    fields = parse_cron(cron)
    zone = ZoneInfo(tz)
    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = after + _MAX_SCAN
    while candidate <= limit:
        if _matches(fields, candidate.astimezone(zone)):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"cron {cron!r} has no occurrence within a year")


# --- evaluation ------------------------------------------------------------


def _with_jitter(due_at: datetime, jitter: Optional[timedelta]) -> datetime:
    if jitter is None:
        return due_at
    return due_at + timedelta(seconds=random.uniform(0, jitter.total_seconds()))


async def _interval_anchor(
    interval: Interval, state: "PulseState", events: EventReader
) -> Optional[datetime]:
    if isinstance(interval.anchor, EventQuery):
        latest = await events.latest(interval.anchor)
        return latest.created_at if latest else None
    if interval.anchor == "last_delivered":
        return state.last_delivered
    return state.last_attempt


async def evaluate(
    tagged: TaggedRhythm,
    stream_id: str,
    state: "PulseState",
    events: EventReader,
    now: datetime,
) -> Evaluation:
    from dainframe.pulse.types import RhythmDecision

    rhythm = tagged.rhythm

    if isinstance(rhythm, Interval):
        anchor = await _interval_anchor(rhythm, state, events)
        if anchor is None:
            # nothing to be recent to: due now (first contact)
            return Evaluation(
                decision=RhythmDecision(due_at=now),
                next_check=now,
                next_after_fire=_with_jitter(now + rhythm.every, rhythm.jitter),
            )
        due_at = anchor + rhythm.every
        if now < due_at:
            return Evaluation(
                decision=None,
                next_check=_with_jitter(due_at, rhythm.jitter),
                next_after_fire=_with_jitter(now + rhythm.every, rhythm.jitter),
            )
        return Evaluation(
            decision=RhythmDecision(due_at=due_at),
            next_check=now,
            # anchored on the FIRING, not the stale anchor - a query anchor
            # that our own outreach doesn't move (a user-silence nudge) must
            # not hot-refire; cadence resumes `every` from now
            next_after_fire=_with_jitter(now + rhythm.every, rhythm.jitter),
        )

    if isinstance(rhythm, Calendar):
        tz = await rhythm.tz_of(stream_id)
        # fresh keys start their clock now: enabling a 9am brief at 4pm does
        # not owe a retroactive 9am firing
        last = (
            datetime.fromisoformat(state.occurrence_key)
            if state.occurrence_key
            else now
        )
        occurrence = next_cron_occurrence(rhythm.cron, tz, last)
        pending = []
        while occurrence <= now:
            pending.append(occurrence)
            occurrence = next_cron_occurrence(rhythm.cron, tz, occurrence)
        if not pending:
            return Evaluation(
                decision=None, next_check=occurrence, next_after_fire=occurrence
            )
        newest = pending[-1]
        if now - newest > CALENDAR_GRACE and rhythm.misfire == "skip":
            # missed while down; skip to the future rather than firing stale
            return Evaluation(
                decision=None, next_check=occurrence, next_after_fire=occurrence
            )
        # fire_once collapses any backlog into one firing (the newest);
        # a within-grace occurrence is just normal polling latency
        return Evaluation(
            decision=RhythmDecision(due_at=newest, occurrence_key=newest.isoformat()),
            next_check=now,
            next_after_fire=occurrence,
        )

    if isinstance(rhythm, Dynamic):
        decision = await rhythm.decider.decide(stream_id, events, now)
        next_check = min(
            max(decision.next_check, now + rhythm.min_sleep),
            now + rhythm.max_sleep,
        )
        if not decision.fire:
            return Evaluation(
                decision=None, next_check=next_check, next_after_fire=next_check
            )
        return Evaluation(
            decision=RhythmDecision(due_at=now, reason=decision.reason),
            next_check=now,
            next_after_fire=next_check,
        )

    raise TypeError(f"unknown rhythm type: {type(rhythm).__name__}")
