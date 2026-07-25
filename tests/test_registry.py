"""tool registry + acting-context tests: the view() wiring-time guard, the
record/terminal policy defaults, and contextvar propagation into gathered
parallel tool calls."""

import asyncio

import pytest

from dainframe.tools.context import acting_as, current_actor
from dainframe.tools.registry import Tool, ToolRegistry
from dainframe.providers.types import ToolCall, ToolDef


def _tool(name, *, terminal=False, record_event=True):
    async def _handler(tool_input, stream_id):
        return f"{name} ran for {stream_id} as {current_actor()}"

    return Tool(
        definition=ToolDef(name=name, description=name, input_schema={"type": "object"}),
        handler=_handler,
        terminal=terminal,
        record_event=record_event,
    )


def test_view_shares_tools_and_rejects_unknown_names():
    reg = ToolRegistry()
    reg.register(_tool("a"))
    reg.register(_tool("b"))

    view = reg.view(["a"])
    assert [d.name for d in view.definitions()] == ["a"]

    # a typo in an agent's tool list fails at wiring time, not mid-chat
    with pytest.raises(KeyError):
        reg.view(["a", "nope"])


def test_policy_defaults_are_the_safe_direction():
    reg = ToolRegistry()
    reg.register(_tool("write", terminal=True, record_event=True))
    reg.register(_tool("read", terminal=False, record_event=False))

    assert reg.is_terminal("write") is True
    assert reg.is_terminal("read") is False
    assert reg.should_record("write") is True
    assert reg.should_record("read") is False
    # unknown tools: non-terminal (gets the round-trip) and recorded (over-record)
    assert reg.is_terminal("unknown") is False
    assert reg.should_record("unknown") is True


def test_execute_returns_errors_instead_of_raising():
    reg = ToolRegistry()

    async def _boom(tool_input, stream_id):
        raise RuntimeError("kaput")

    reg.register(
        Tool(
            definition=ToolDef(name="boom", description="d", input_schema={}),
            handler=_boom,
        )
    )

    result = asyncio.run(reg.execute(ToolCall(id="t1", name="boom", input={}), "s1"))
    assert result.is_error is True
    assert "kaput" in result.content

    unknown = asyncio.run(reg.execute(ToolCall(id="t2", name="ghost", input={}), "s1"))
    assert unknown.is_error is True


def test_acting_context_reaches_parallel_tool_calls():
    """asyncio.gather copies the current context into each task, so parallel
    calls in one turn all see the actor bound before the gather."""
    reg = ToolRegistry()
    reg.register(_tool("x"))

    async def _go():
        with acting_as("aria"):
            return await asyncio.gather(
                reg.execute(ToolCall(id="1", name="x", input={}), "s1"),
                reg.execute(ToolCall(id="2", name="x", input={}), "s1"),
            )

    results = asyncio.run(_go())
    assert [r.content for r in results] == [
        "x ran for s1 as aria",
        "x ran for s1 as aria",
    ]
    # the context manager resets cleanly
    assert current_actor() == "agent"
