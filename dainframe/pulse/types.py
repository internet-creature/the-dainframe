"""the pulse vocabulary (DESIGN.md §6): how ambient time becomes stimuli.

a Stimulus stays "something happened NOW"; the pulse owns the thing that
produces stimuli over time. rhythms describe WHEN abstractly; the FiringPlan
resolves WHERE and WHO cheaply before any tokens are spent on WHAT; gates can
then veto with zero generation cost (§11.9: an author budget cannot be
enforced before an author exists).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping, Optional, Protocol, runtime_checkable

from dainframe.core.events import EventReader
from dainframe.core.types import ActivationPrecondition, DeliveryTarget, Stimulus


@dataclass(frozen=True)
class RhythmKey:
    """the durable identity of one rhythm on one stream. rhythm ids are
    app-chosen, stable, and unique within the stream (§6.1)."""

    stream_id: str
    rhythm_id: str


@dataclass(frozen=True)
class RhythmDecision:
    """why this rhythm is firing now: the due moment, the occurrence that
    owns it (calendar dedup), and - for Dynamic - the decider's reason,
    which flows into Stimulus.reason -> Briefing.reason."""

    due_at: datetime
    occurrence_key: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class FiringPlan:
    """the cheap resolution of a due rhythm: who would speak, where it would
    deliver, why now, and the observation to revalidate under the stream lock
    (§11.17). built by the app's StimulusFactory BEFORE gates run."""

    key: RhythmKey
    kind: str
    due_at: datetime
    actor: Optional[str] = None
    target: Optional[DeliveryTarget] = None
    reason: Optional[str] = None
    precondition: Optional[ActivationPrecondition] = None
    extras: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GateDecision:
    """a gate's verdict. `retry_at` lets a denial persist its own horizon
    (backoff expiry, end of quiet hours) instead of hot-polling every cycle;
    None means 'retry on the loop's ordinary recheck cadence'."""

    allowed: bool
    reason: str = ""
    retry_at: Optional[datetime] = None


@dataclass(frozen=True)
class PulseOutcome:
    """what one claimed firing amounted to, recorded by the store:

    - `skipped`   - not actually due / decider declined / factory had no plan
    - `denied`    - a gate said no (reason preserved; zero tokens spent)
    - `cancelled` - the engine's precondition found the plan stale
    - `activated` - the engine ran the script (delivered/generated as flagged)
    - `failed`    - generation or delivery failed; retry state persisted
    """

    status: Literal["skipped", "denied", "cancelled", "activated", "failed"]
    at: datetime
    detail: Optional[str] = None
    generated: bool = False
    delivered: bool = False


@runtime_checkable
class PulseSource(Protocol):
    """which streams are ambient, and what rhythms each carries. re-read
    every cycle, so activation/deactivation needs no registration dance."""

    async def streams(self) -> list[tuple[str, list["TaggedRhythm"]]]: ...


@runtime_checkable
class Gate(Protocol):
    """cheap pre-generation guard. a denied tick costs a store read and ZERO
    tokens - never generate a proactive message just to throw it away."""

    async def check(
        self, firing: FiringPlan, events: EventReader, now: datetime
    ) -> GateDecision: ...


@runtime_checkable
class StimulusFactory(Protocol):
    """the app's translation seam: cheaply resolve a due rhythm into the
    candidate actor/destination (`plan`), and - only after gates clear -
    build the ordinary Stimulus the engine will handle (`build`).
    `plan` returning None means 'nothing to fire' (no deliverable platform,
    stream retired, ...): the firing completes as skipped without tokens."""

    async def plan(
        self, stream_id: str, rhythm: "TaggedRhythm", decision: RhythmDecision
    ) -> Optional[FiringPlan]: ...

    async def build(self, plan: FiringPlan) -> Stimulus: ...


# TaggedRhythm lives in rhythms.py (it carries the rhythm union); re-exported
# here for the protocol annotations above via postponed evaluation.
from dainframe.pulse.rhythms import TaggedRhythm  # noqa: E402
