"""the cadence: a declarative re-engagement ladder (DESIGN.md §6.4).

the backoff gate answers "how long until we may try again" with doubling
arithmetic that ends in a cap: a few tries over a day or so, then silence
until the user speaks. that shape fits a crew of nudging specialists; it
does not fit a companion whose presence must never taper to zero. the
cadence is the long-tail alternative: an explicit ladder of waits - say
once each morning for a few days, then weekly for a few weeks, then every
couple of months forever - written as a spec the app hands over, not
arithmetic baked into the gate.

the spec is a value with a compact string form, so it can live in an env
var, a settings row, or a chat tool's argument:

    Cadence.parse("1d x3, 1w x3, 60d @ 8-11")

reads: three daily waits, then three weekly waits, then a sixty-day wait
forever, each landing between 8am and 11am in the stream's local time. a
final rung WITH a count expresses the old cap ("after these, silence until
they speak"); a final rung without one is the eternal floor - the sentinel
keeps its post.

everything is derived from the event log, exactly like the backoff gate:
the unanswered count picks the rung, and any user message anywhere resets
it. per-stream specs arrive through a callable, so "hold me at monthly" is
just a one-rung cadence the app stores: a reply still resets the count,
but a one-rung ladder waits the same either way - the pin holds by
construction, not by special case.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Union
from zoneinfo import ZoneInfo

from dainframe.core.events import EventQuery, EventReader
from dainframe.pulse.gates import _unanswered_proactive
from dainframe.pulse.rhythms import as_utc
from dainframe.pulse.types import FiringPlan, GateDecision

logger = logging.getLogger(__name__)

_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_RUNG_RE = re.compile(r"^(\d+)\s*([mhdw])(?:\s*x\s*(\d+))?$", re.IGNORECASE)
_HOURS_RE = re.compile(r"^(\d{1,2})\s*-\s*(\d{1,2})$")


@dataclass(frozen=True)
class Rung:
    """one step of the ladder: the required quiet after each unanswered
    outreach, and how many waits it covers. `tries=None` never exhausts -
    only the last rung may carry it (anything after would be unreachable)."""

    every: timedelta
    tries: Optional[int] = None


@dataclass(frozen=True)
class DeliveryHours:
    """the local hours a cadence outreach may land in ("each morning").
    `start`/`end` are local hours, end exclusive; start > end wraps
    midnight, same convention as the quiet-hours gate."""

    start: int
    end: int

    def contains(self, hour: int) -> bool:
        if self.start > self.end:  # wraps midnight
            return hour >= self.start or hour < self.end
        return self.start <= hour < self.end

    def next_start(self, local: datetime) -> datetime:
        """the first window-opening at or after `local` (local clock in,
        local clock out)."""
        opening = local.replace(hour=self.start, minute=0, second=0, microsecond=0)
        if opening < local:
            opening += timedelta(days=1)
        return opening

    def snap(self, local: datetime) -> datetime:
        """`local` if it already falls inside the hours, else the next
        opening - the moment a computed horizon may actually deliver."""
        return local if self.contains(local.hour) else self.next_start(local)


@dataclass(frozen=True)
class Cadence:
    """the ladder plus where in the day it lands. build directly or via
    `parse`; `str()` renders the canonical spec back."""

    rungs: tuple[Rung, ...]
    during: Optional[DeliveryHours] = None

    @staticmethod
    def parse(spec: str) -> "Cadence":
        """the compact form: comma-separated rungs `<N><m|h|d|w>[ xK]`,
        optionally `@ H-H` for local delivery hours. raises ValueError with
        the offending piece named - a stored spec that no longer parses
        must fail loudly at the seam that reads it, not silently misfire."""
        body, _, hours_part = spec.partition("@")
        during = _parse_hours(hours_part.strip()) if hours_part.strip() else None

        rungs: list[Rung] = []
        pieces = [p.strip() for p in body.split(",")]
        if pieces == [""]:
            raise ValueError("cadence spec has no rungs")
        for piece in pieces:
            match = _RUNG_RE.match(piece)
            if not match:
                raise ValueError(f"unparseable cadence rung: {piece!r}")
            amount, unit, tries = match.groups()
            if int(amount) < 1:
                raise ValueError(
                    f"cadence rung must wait at least one {unit}: {piece!r}"
                )
            if tries is not None and int(tries) < 1:
                raise ValueError(f"cadence rung needs at least one try: {piece!r}")
            rungs.append(
                Rung(
                    every=timedelta(seconds=int(amount) * _UNIT[unit.lower()]),
                    tries=int(tries) if tries is not None else None,
                )
            )
        for rung in rungs[:-1]:
            if rung.tries is None:
                raise ValueError(
                    "only the last cadence rung may omit its try count - "
                    "rungs after an uncounted one could never be reached"
                )
        return Cadence(rungs=tuple(rungs), during=during)

    def wait_for(self, unanswered: int) -> Optional[timedelta]:
        """the required quiet after the nth unanswered outreach (n >= 1).
        None means the ladder is exhausted: silence until they speak."""
        consumed = 0
        for rung in self.rungs:
            if rung.tries is None or unanswered <= consumed + rung.tries:
                return rung.every
            consumed += rung.tries
        return None

    def __str__(self) -> str:
        parts = []
        for rung in self.rungs:
            piece = _render_duration(rung.every)
            if rung.tries is not None:
                piece += f" x{rung.tries}"
            parts.append(piece)
        rendered = ", ".join(parts)
        if self.during is not None:
            rendered += f" @ {self.during.start}-{self.during.end}"
        return rendered


def _parse_hours(text: str) -> DeliveryHours:
    match = _HOURS_RE.match(text)
    if not match:
        raise ValueError(f"unparseable delivery hours: {text!r} (expected H-H)")
    start, end = int(match.group(1)), int(match.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise ValueError(f"delivery hours out of range: {text!r}")
    if start == end:
        raise ValueError(f"delivery hours cannot be empty: {text!r}")
    return DeliveryHours(start=start, end=end)


def _render_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for unit in ("w", "d", "h", "m"):
        if seconds % _UNIT[unit] == 0:
            return f"{seconds // _UNIT[unit]}{unit}"
    return f"{seconds // 60}m"


CadenceOf = Union[Cadence, Callable[[str], Awaitable[Cadence]]]


class CadenceGate:
    """the re-engagement guard: unanswered outreach climbs the ladder
    instead of doubling toward a cap.

    `cadence` is a Cadence for the whole fleet, or an async callable
    `(stream_id) -> Cadence` when specs are per-stream (a user override
    stored by the app). the unanswered count is the same event-log
    arithmetic as the backoff gate, so any user message anywhere resets
    the ladder; delivery hours need `tz_of`, and an unresolvable timezone
    fails closed exactly like the quiet-hours gate - not knowing what time
    it is for the stream is a reason to stay silent, never to guess."""

    def __init__(
        self,
        cadence: CadenceOf,
        *,
        proactive_message_type: str = "scheduled",
        window: int = 100,
        tz_of: Optional[Callable[[str], Awaitable[str]]] = None,
    ):
        self.cadence = cadence
        self.proactive_message_type = proactive_message_type
        self.window = window
        self.tz_of = tz_of

    async def _resolve(self, stream_id: str) -> Cadence:
        if isinstance(self.cadence, Cadence):
            return self.cadence
        return await self.cadence(stream_id)

    async def check(
        self, firing: FiringPlan, events: EventReader, now: datetime
    ) -> GateDecision:
        window = await events.read(
            EventQuery(kinds=frozenset({"message"}), message_limit=self.window)
        )
        unanswered = _unanswered_proactive(window, self.proactive_message_type)
        if not unanswered:
            return GateDecision(True, "clear")

        spec = await self._resolve(firing.key.stream_id)
        wait = spec.wait_for(len(unanswered))
        if wait is None:
            return GateDecision(False, "cadence exhausted: quiet until they speak")

        newest = as_utc(unanswered[-1].created_at)
        retry_at = newest + wait
        if spec.during is None:
            if as_utc(now) < retry_at:
                elapsed = as_utc(now) - newest
                return GateDecision(
                    False,
                    f"cadence: {wait} required, {elapsed} elapsed",
                    retry_at=retry_at,
                )
            return GateDecision(True, "clear")

        if self.tz_of is None:
            raise ValueError(
                "a cadence with delivery hours requires tz_of; the gate "
                "cannot place a local morning without a clock"
            )
        tz = await self.tz_of(firing.key.stream_id)
        try:
            zone = ZoneInfo(tz)
        except Exception:
            logger.warning(
                "cadence cannot resolve timezone %r for stream %s; failing "
                "closed (denying the firing). fix the stored timezone or "
                "install tzdata",
                tz,
                firing.key.stream_id,
            )
            return GateDecision(
                False, f"cadence: unresolvable timezone '{tz}' (fail closed)"
            )

        local_now = as_utc(now).astimezone(zone)
        target = spec.during.snap(retry_at.astimezone(zone))
        if local_now < target:
            return GateDecision(
                False,
                f"cadence: {wait} required, landing "
                f"{spec.during.start}-{spec.during.end} local",
                retry_at=target.astimezone(timezone.utc),
            )
        if not spec.during.contains(local_now.hour):
            # the horizon passed while the window was shut (a long pulse
            # outage, a stretched deny) - wait for the next opening
            return GateDecision(
                False,
                f"cadence: outside delivery hours "
                f"({spec.during.start}-{spec.during.end} local)",
                retry_at=spec.during.next_start(local_now).astimezone(timezone.utc),
            )
        return GateDecision(True, "clear")
