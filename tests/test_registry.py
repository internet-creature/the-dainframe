"""tool registry + ToolContext tests: the view() wiring-time guard, the
record/terminal policy defaults, mandatory identity (no silent default), and
contextvar propagation into gathered parallel tool calls."""

import asyncio

import pytest

from dainframe.tools.context import ToolContext, current_tool_context, tool_context
from dainframe.tools.registry import Tool, ToolRegistry
from dainframe.providers.types import ToolCall, ToolDef


CTX = ToolContext(stream_id="s1", activation_id="act-1", actor="aria")


def _tool(name, *, terminal=False, record_event=True):
    async def _handler(tool_input, context):
        return f"{name} ran for {context.stream_id} as {context.actor}"

    return Tool(
        definition=ToolDef(
            name=name, description=name, input_schema={"type": "object"}
        ),
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

    async def _boom(tool_input, context):
        raise RuntimeError("kaput")

    reg.register(
        Tool(
            definition=ToolDef(name="boom", description="d", input_schema={}),
            handler=_boom,
        )
    )

    result = asyncio.run(reg.execute(ToolCall(id="t1", name="boom", input={}), CTX))
    assert result.is_error is True
    assert "kaput" in result.content

    unknown = asyncio.run(reg.execute(ToolCall(id="t2", name="ghost", input={}), CTX))
    assert unknown.is_error is True


def test_handlers_receive_the_full_context():
    reg = ToolRegistry()
    reg.register(_tool("x"))
    result = asyncio.run(reg.execute(ToolCall(id="1", name="x", input={}), CTX))
    assert result.content == "x ran for s1 as aria"


def test_context_is_mandatory_never_defaulted():
    """there is no silent identity (DESIGN.md §11.5): reading the context
    outside a bound tool loop is an error, not 'agent' or 'chordial'."""
    with pytest.raises(LookupError):
        current_tool_context()


def test_bound_context_reaches_parallel_tool_calls_via_contextvar():
    """asyncio.gather copies the current context into each task, so parallel
    calls in one turn all see the ToolContext bound before the gather."""
    reg = ToolRegistry()

    async def _peek(tool_input, context):
        # a handler may also read the ambient context instead of the argument
        ambient = current_tool_context()
        assert ambient is context
        return f"seen: {ambient.activation_id}/{ambient.actor}"

    reg.register(
        Tool(
            definition=ToolDef(name="peek", description="d", input_schema={}),
            handler=_peek,
        )
    )

    async def _go():
        with tool_context(CTX):
            return await asyncio.gather(
                reg.execute(ToolCall(id="1", name="peek", input={}), CTX),
                reg.execute(ToolCall(id="2", name="peek", input={}), CTX),
            )

    results = asyncio.run(_go())
    assert [r.content for r in results] == [
        "seen: act-1/aria",
        "seen: act-1/aria",
    ]
    # the context manager resets cleanly: outside, identity is absent again
    with pytest.raises(LookupError):
        current_tool_context()
