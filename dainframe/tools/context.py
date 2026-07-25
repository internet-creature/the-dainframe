"""the tool execution context: whose stream, which activation, which actor.

tools are shared Tool objects in one registry; a handler receives the
tool input plus a ToolContext - the model never sees or chooses identity.
there is deliberately NO default identity (DESIGN.md §11.5): streams are not
users, actors are not helpers, and a silent default misattributes mutations.
the context is mandatory and activation-scoped.

rather than thread the context through every call site inside a tool loop, it
also rides in a contextvar that AgentLoop binds around the loop. contextvars
are the async-safe mechanism here: asyncio.gather copies the current context
into each task at creation, so parallel tool calls in one turn all see the
context bound before the gather. handlers that don't care simply never read it.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass(frozen=True)
class ToolContext:
    """the execution context of one agent run within one activation."""

    stream_id: str        # opaque event-stream key (chordial: user_uuid)
    activation_id: str    # one engine activation; ties actions to their turn
    actor: str            # the named agent whose tool loop this is
    metadata: Mapping[str, object] = field(default_factory=dict)


_current: contextvars.ContextVar[Optional[ToolContext]] = contextvars.ContextVar(
    "tool_context", default=None
)


def current_tool_context() -> ToolContext:
    """the ToolContext of the running tool loop. raises LookupError outside
    one - identity is mandatory, never silently defaulted."""
    ctx = _current.get()
    if ctx is None:
        raise LookupError(
            "no ToolContext bound - tool handlers run inside AgentLoop.run "
            "(or an explicit tool_context(...) block)"
        )
    return ctx


@contextmanager
def tool_context(ctx: ToolContext):
    """bind the tool context for the duration of a tool loop. AgentLoop.run
    wraps its loop in this; the token reset makes it re-entrant and leak-free."""
    token = _current.set(ctx)
    try:
        yield
    finally:
        _current.reset(token)
