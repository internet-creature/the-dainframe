"""the acting-actor context: which named agent is running the current tool loop.

tools are shared Tool objects in one registry; a handler's signature is
(tool_input, stream_id) and deliberately doesn't carry agent identity - the
model never chooses who it is. but some tools DO need to know which actor is
acting (e.g. attributing a saved memory to the persona that saved it, so a
sibling's note can later render as "(from aria) ...").

rather than thread an extra arg through every handler and every call site, the
acting actor rides in a contextvar that AgentLoop sets around the tool loop.
contextvars are the async-safe mechanism here: asyncio.gather copies the
current context into each task at creation, so parallel tool calls in one turn
all see the actor that was set before the gather. handlers that don't care
simply never read it.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

_acting_actor: contextvars.ContextVar[str] = contextvars.ContextVar(
    "acting_actor", default="agent"
)


def current_actor() -> str:
    """the actor id running the current tool loop (default 'agent')."""
    return _acting_actor.get()


@contextmanager
def acting_as(actor_id: str):
    """bind the acting actor for the duration of a tool loop. AgentLoop.run
    wraps its loop in this; the token reset makes it re-entrant and leak-free."""
    token = _acting_actor.set(actor_id)
    try:
        yield
    finally:
        _acting_actor.reset(token)
