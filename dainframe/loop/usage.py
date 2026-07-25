"""the usage seam: the loop emits, the app persists.

cost accounting is app business (where the ledger lives, what a row looks
like), but WHEN to account is loop business — one ProviderCallUsage per model
call, one AgentRunTrace when the run ends. the sink is a single async `emit`
(DESIGN.md §4.6); chordial's two existing recorder methods adapt mechanically.

sink failure is guarded at the call site: accounting can never break an agent
run. `model` on ProviderCallUsage is the model the actual AIResponse reported,
not merely a provider object's configured default (§4.8).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Union, runtime_checkable

from dainframe.providers.types import Usage


@dataclass(frozen=True)
class ProviderCallUsage:
    """one model call's token accounting."""

    stream_id: Optional[str]
    provider: str
    model: str
    turn_kind: str
    usage: Usage
    actor: str
    platform: Optional[str] = None


@dataclass(frozen=True)
class AgentRunTrace:
    """one completed agent run: iterations, tool trail, outcome shape."""

    stream_id: Optional[str]
    turn_kind: str
    iterations: int
    hit_iteration_cap: bool
    tool_trace: tuple
    final_text_length: int
    stop_reason: Optional[str]
    total_usage: Usage
    actor: str
    platform: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)


UsageEvent = Union[ProviderCallUsage, AgentRunTrace]


@runtime_checkable
class UsageSink(Protocol):
    async def emit(self, event: UsageEvent) -> None: ...


class NullUsageSink:
    """the default: no accounting. apps that care pass a real sink."""

    async def emit(self, event: UsageEvent) -> None:
        pass
