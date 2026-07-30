"""shipped gates (DESIGN.md §6.4).

the surprise of reading chordial's ProactivityGate with library eyes: it is
pure arithmetic over event-log semantics the library already owns
(kind='message', author_type, a proactive message_type). so the "don't nag"
ethic of the ambient design ships with the dainframe, and app-flavored
guards (an onboarding check, a platform-liveness rule) are just more Gate
implementations composed into the same stack.

denials carry `retry_at` where the horizon is computable (backoff expiry,
end of quiet hours) so a denied rhythm persists its wait instead of
hot-polling; cap denials clear only when the user speaks, so they leave
retry_at unset and rely on the loop's ordinary recheck cadence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from dainframe.core.events import Event, EventQuery, EventReader
from dainframe.pulse.rhythms import as_utc
from dainframe.pulse.types import FiringPlan, GateDecision

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AllOf:
    """a gate stack: first denial wins, reasons preserved. an empty stack
    allows everything."""

    gates: Sequence[object]

    async def check(
        self, firing: FiringPlan, events: EventReader, now: datetime
    ) -> GateDecision:
        for gate in self.gates:
            decision = await gate.check(firing, events, now)
            if not decision.allowed:
                return decision
        return GateDecision(allowed=True, reason="clear")


def _unanswered_proactive(
    events: list[Event], proactive_message_type: str
) -> list[Event]:
    """agent proactive messages after the newest user message in the window,
    oldest -> newest. no user message in the window means the whole window
    counts (nothing has reset the chain)."""
    last_user_idx = -1
    for i, event in enumerate(events):
        if event.kind == "message" and event.author_type == "user":
            last_user_idx = i
    return [
        event
        for event in events[last_user_idx + 1 :]
        if event.kind == "message"
        and event.author_type == "agent"
        and event.message_type == proactive_message_type
    ]


class BackoffGate:
    """the non-interaction guard: three stacked rules, checked in order.

    1. crew cap - `crew_cap` unanswered proactive messages, from anyone,
       silences the whole crew. being ignored is a signal to everyone, not a
       budget each author spends separately.
    2. per-author cap - `per_author_cap` unanswered proactive messages from
       THIS author silences just them (others may still be clear). requires
       `FiringPlan.actor`; a plan without one fails loudly (§11.9) - an
       author budget cannot be enforced before an author exists.
    3. exponential backoff - each unanswered message doubles the required
       quiet period since the newest one: base * 2**(n-1).

    any user message, anywhere in the stream, resets all three."""

    def __init__(
        self,
        crew_cap: int = 4,
        per_author_cap: int = 3,
        base_interval: timedelta = timedelta(hours=3),
        *,
        proactive_message_type: str = "scheduled",
        window: int = 100,
    ):
        self.crew_cap = crew_cap
        self.per_author_cap = per_author_cap
        self.base_interval = base_interval
        self.proactive_message_type = proactive_message_type
        self.window = window

    async def check(
        self, firing: FiringPlan, events: EventReader, now: datetime
    ) -> GateDecision:
        window = await events.read(
            EventQuery(kinds=frozenset({"message"}), message_limit=self.window)
        )
        unanswered = _unanswered_proactive(window, self.proactive_message_type)

        if len(unanswered) >= self.crew_cap:
            return GateDecision(False, "crew cap: quiet until they speak")

        if self.per_author_cap:
            if firing.actor is None:
                raise ValueError(
                    "BackoffGate.per_author_cap requires FiringPlan.actor; "
                    "the factory must resolve an author before this gate runs"
                )
            by_author = sum(1 for e in unanswered if e.author == firing.actor)
            if by_author >= self.per_author_cap:
                return GateDecision(False, f"{firing.actor} cap: their turn is over")

        if unanswered:
            required = self.base_interval * (2 ** (len(unanswered) - 1))
            newest = as_utc(unanswered[-1].created_at)
            retry_at = newest + required
            if as_utc(now) < retry_at:
                elapsed = as_utc(now) - newest
                return GateDecision(
                    False,
                    f"backoff: {required} required, {elapsed} elapsed",
                    retry_at=retry_at,
                )

        return GateDecision(True, "clear")


class QuietHoursGate:
    """no proactive send in a stream's local night; applies to every rhythm,
    including backoff chains - being ignored is not license to nag at 3am.
    `start`/`end` are local hours; start > end wraps midnight (21 -> 8).

    an unresolvable timezone FAILS CLOSED: not knowing what time it is for
    the stream is a reason to stay silent, never to guess a zone and nag at
    night. the denial is loud (a warning per recheck) so a broken tz row or
    a host missing tzdata surfaces in logs instead of in someone's sleep."""

    def __init__(
        self,
        start: int,
        end: int,
        tz_of: Callable[[str], Awaitable[str]],
    ):
        self.start = start
        self.end = end
        self.tz_of = tz_of

    async def check(
        self, firing: FiringPlan, events: EventReader, now: datetime
    ) -> GateDecision:
        tz = await self.tz_of(firing.key.stream_id)
        try:
            zone = ZoneInfo(tz)
        except Exception:
            logger.warning(
                "quiet hours cannot resolve timezone %r for stream %s; "
                "failing closed (denying the firing). fix the stored "
                "timezone or install tzdata",
                tz,
                firing.key.stream_id,
            )
            return GateDecision(
                False, f"quiet hours: unresolvable timezone '{tz}' (fail closed)"
            )
        local = now.astimezone(zone)
        if not self._in_quiet(local.hour):
            return GateDecision(True, "clear")
        wake = local.replace(hour=self.end, minute=0, second=0, microsecond=0)
        if wake <= local:
            wake += timedelta(days=1)
        return GateDecision(
            False,
            f"quiet hours ({self.start}-{self.end} local)",
            retry_at=wake.astimezone(now.tzinfo),
        )

    def _in_quiet(self, hour: int) -> bool:
        if self.start > self.end:  # wraps midnight
            return hour >= self.start or hour < self.end
        return self.start <= hour < self.end


class NoNewerEvent:
    """an ActivationPrecondition for firing plans (§11.17): holds while no
    event matching `query` is newer than the plan. a user speaking between
    plan and stream-lock makes a silence nudge or check-in stale - the
    engine cancels before tokens are spent."""

    def __init__(self, query: EventQuery, than: datetime):
        self.query = query
        self.than = than

    async def holds(self, events: EventReader) -> bool:
        latest = await events.latest(self.query)
        return latest is None or as_utc(latest.created_at) <= as_utc(self.than)
