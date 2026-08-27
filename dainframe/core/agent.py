"""the agent contract: what the engine briefs, and what comes back.

an Agent is a named actor. the engine decides WHO acts and hands them a
Briefing (the necessary info for this activation); the agent owns HOW - its
persona, its prompt construction, its model, its tools. silent evaluators and
chatty personas fit the same interface; whether silence is correct is the
ScriptLine's response policy, not the agent's problem (§5.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, runtime_checkable

from dainframe.core.events import Event
from dainframe.core.types import ExecutionHints, ScriptLine, Stimulus
from dainframe.loop.agent_loop import ExecutedAction


@dataclass(frozen=True)
class Briefing:
    """everything the engine hands an agent for one activation line. the
    engine fills the mechanical fields (window, cue, style, scope, reason,
    execution hints); the app's ContextProvider fills the rest (§4.4)."""

    kind: str
    stream_id: str
    activation_id: str
    platform: Optional[str] = None
    # viewer-filtered event window; for a recorded inbound, the last message
    # is the just-received one. later lines are briefed after earlier lines
    # recorded, so a second speaker genuinely reacts to the first.
    events: tuple[Event, ...] = ()
    ambient_context: Optional[str] = None
    cue: Optional[str] = None
    style: str = "full"
    scope: str = "dm"
    # the "why now" of an ambient stimulus (a pulse decider/gate wrote it)
    reason: Optional[str] = None
    # the director's model routing for this line; an agent honors these or
    # declares that it ignores them - never silently half-applies (§4.8)
    execution: ExecutionHints = field(default_factory=ExecutionHints)
    extras: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentOutcome:
    """what one activation line produced. text=None is not an error shape -
    whether it's correct is the line's response policy. a provider failure
    AFTER tools ran must still surface its partial actions (§5.4): raise
    AgentExecutionError (or return errored=True with the actions attached),
    never a bare error that loses the trail."""

    text: Optional[str] = None
    actions: tuple[ExecutedAction, ...] = ()
    refused: bool = False
    errored: bool = False


@runtime_checkable
class Agent(Protocol):
    name: str

    async def act(self, briefing: Briefing) -> AgentOutcome: ...


@dataclass(frozen=True)
class BriefingContext:
    """the app's enrichment of one line's briefing: an optional briefing-kind
    override, ambient context, and app extras (user profile, agenda, ...)."""

    kind: Optional[str] = None
    ambient_context: Optional[str] = None
    extras: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class ContextProvider(Protocol):
    async def enrich(self, stimulus: Stimulus, line: ScriptLine) -> BriefingContext: ...


class NullContextProvider:
    """the default: no enrichment beyond the mechanical fields."""

    async def enrich(self, stimulus: Stimulus, line: ScriptLine) -> BriefingContext:
        return BriefingContext()
