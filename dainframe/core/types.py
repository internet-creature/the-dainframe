"""the frozen activation vocabulary (DESIGN.md §5).

what comes IN (a Stimulus), what the Director produces (a Script of per-line
obligations), and what comes back OUT (an ActivationResult whose LineResults
are lossless - no aggregate booleans that could lie about a multi-line
activation, §11.2).

everything here is immutable: a Stimulus becomes the activation's source
record, a Script is the Director's committed decision, and results are shared
history's receipt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional, Protocol, runtime_checkable

from dainframe.loop.agent_loop import ExecutedAction


class ScriptValidationError(ValueError):
    """a Script that encodes contradictory intent, caught at creation."""


@dataclass(frozen=True)
class DeliveryTarget:
    """an opaque destination the app's Deliverer understands. the library
    never interprets it - platform-specific routing lives inside."""

    platform: str
    target_id: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionHints:
    """the director's per-line model routing (§4.2/§4.8): structured, carried
    by the Briefing, honored or explicitly ignored by the Agent - never a
    passive decoration (§11.6). resolution to one provider for the whole run
    is the ProviderResolver's job (phase 3)."""

    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    max_tokens: Optional[int] = None
    extras: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class EventContext:
    """where a line's events belong: provenance, privacy channel, and the
    outbound message type. a ScriptLine's event_context is a COMPLETE override
    of the stimulus defaults (§11.14) - a visible interviewer and a private
    scorer may share one stimulus while recording for different audiences."""

    platform: Optional[str] = None
    scope: str = "dm"
    audience: Optional[str] = None
    outbound_message_type: str = "conversation"


@runtime_checkable
class ActivationPrecondition(Protocol):
    """a read-only predicate evaluated only after the engine holds the stream
    (§5.1/§11.17). pulse-built stimuli use it to cancel stale ambient work -
    'the candidate still has not spoken' - before tokens or tools are spent."""

    async def holds(self, events: "EventReader") -> bool: ...  # noqa: F821


@dataclass(frozen=True)
class Stimulus:
    """something happened, NOW. the engine reads only the mechanical
    inbound-recording fields; `kind` is app vocabulary interpreted by the
    app's Director and ContextProvider - no chordial kind appears in library
    control flow (§5.1)."""

    kind: str
    stream_id: str
    content: Optional[str] = None

    # inbound recording facts
    record_inbound: bool = True
    inbound_author: str = "user"
    inbound_author_type: str = "user"
    inbound_message_type: str = "conversation"

    # routing, provenance, and privacy
    platform: Optional[str] = None
    scope: str = "dm"
    audience: Optional[str] = None
    addressed: tuple[str, ...] = ()
    target: Optional[DeliveryTarget] = None

    # ambient/director/context information
    reason: Optional[str] = None
    precondition: Optional[ActivationPrecondition] = None
    extras: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptLine:
    """one turn in the director's script: who speaks, with what obligations.

    response and delivery are per-LINE policy (§5.2/§11.1) - this is what lets
    one activation cast a visible interviewer plus silent scorers without
    treating the scorers as broken."""

    speaker: str
    cue: Optional[str] = None
    style: str = "full"
    response: Literal["required", "optional", "silent"] = "required"
    delivery: Literal["direct", "pending", "none"] = "direct"
    execution: ExecutionHints = field(default_factory=ExecutionHints)
    target: Optional[DeliveryTarget] = None
    event_context: Optional[EventContext] = None


_LEGAL_COMBOS = {
    ("required", "direct"),
    ("required", "pending"),
    ("optional", "direct"),
    ("optional", "pending"),
    ("silent", "none"),
}


@dataclass(frozen=True)
class Script:
    """the ordered lines of one activation. validated at creation: illegal
    response/delivery combos encode contradictory intent (§5.2), and an empty
    script is valid only with an explicit noop_reason - a broken Director must
    not silently drop an ordinary user turn."""

    lines: tuple[ScriptLine, ...] = ()
    noop_reason: Optional[str] = None

    def __post_init__(self):
        if not self.lines and not self.noop_reason:
            raise ScriptValidationError(
                "an empty Script needs an explicit noop_reason"
            )
        for line in self.lines:
            if (line.response, line.delivery) not in _LEGAL_COMBOS:
                raise ScriptValidationError(
                    f"line for '{line.speaker}': response={line.response!r} with "
                    f"delivery={line.delivery!r} encodes contradictory intent"
                )


@dataclass(frozen=True)
class ExecutionErrorInfo:
    """a structured line failure: what kind, what happened, is retry sane."""

    kind: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class PendingDelivery:
    """a generated-but-unconfirmed outbound line, frozen by the ledger. the
    caller later confirms with the opaque pending_id + a receipt; free-form
    recording of arbitrary text is deliberately not exposed (§11.4).

    `event_context` is a phase-0 addition over the §5.3 field list: without
    it, confirmation could not record the message event with the line's
    effective scope/audience/message type."""

    pending_id: str
    stream_id: str
    activation_id: str
    line_id: str
    speaker: str
    target: DeliveryTarget
    text: str
    event_context: EventContext = field(default_factory=EventContext)


@dataclass(frozen=True)
class LineResult:
    """what one line actually did. every line survives into the result -
    including structured failures for unknown speakers (§4.2)."""

    line_id: str
    speaker: str
    status: Literal["delivered", "pending", "silent", "refused", "errored"]
    actions: tuple[ExecutedAction, ...] = ()
    pending: Optional[PendingDelivery] = None
    error: Optional[ExecutionErrorInfo] = None


@dataclass(frozen=True)
class ActivationResult:
    """the lossless outcome of one activation. deliberately NO aggregate
    refused/errored/handled booleans (§11.2); derive conveniences, never store
    a second source of truth. cancelled/noop mean no script ran at all."""

    activation_id: str
    stream_id: str
    inbound_event_id: Optional[str]
    status: Literal["completed", "cancelled", "noop"]
    status_reason: Optional[str]
    lines: tuple[LineResult, ...]

    @property
    def any_delivered(self) -> bool:
        return any(line.status == "delivered" for line in self.lines)
