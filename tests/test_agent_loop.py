"""agent-loop tests: terminal-tool behavior, budget retry, action truth.

the terminal-tool suite is ported from chordial, where the bug it locks down
was found: when the model writes a reply AND calls a terminal tool (e.g.
save_memory) in the same turn, the reply must survive - not get discarded and
replaced by a thin second-call closer. the action-truth tests lock down
DESIGN.md §5.4/§11.16: a provider failure after tools have run must carry the
partial actions out with it. uses a scripted fake provider (no network).
"""

import asyncio

import pytest

from dainframe.loop.agent_loop import AgentExecutionError, AgentLoop
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool, ToolRegistry
from dainframe.providers.types import (
    AIRequest,
    AIResponse,
    ChatTurn,
    ProviderError,
    SystemBlock,
    ToolCall,
    ToolDef,
    Usage,
)


def run(coro):
    return asyncio.run(coro)


CTX = ToolContext(stream_id="u", activation_id="act-1", actor="aria")


class ScriptedProvider:
    """returns a pre-scripted AIResponse (or raises a scripted error) per
    call, recording how many calls it got."""

    model = "fake-model"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def create_message(self, request: AIRequest) -> AIResponse:
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(text, tool_calls=None, stop_reason="end_turn"):
    return AIResponse(
        text=text,
        tool_calls=tool_calls or [],
        stop_reason=stop_reason,
        usage=Usage(input_tokens=1, output_tokens=1),
        assistant_turn=ChatTurn(role="assistant", content=text),
        model="fake-model-live",
    )


def _registry(*, save_terminal=True):
    reg = ToolRegistry()
    calls = []

    async def _save(tool_input, context):
        calls.append(("save_memory", tool_input))
        return "saved"

    async def _search(tool_input, context):
        calls.append(("search_memories", tool_input))
        return "found: user likes tea"

    reg.register(
        Tool(
            definition=ToolDef(
                name="save_memory", description="save", input_schema={"type": "object"}
            ),
            handler=_save,
            terminal=save_terminal,
        )
    )
    reg.register(
        Tool(
            definition=ToolDef(
                name="search_memories",
                description="search",
                input_schema={"type": "object"},
            ),
            handler=_search,
            terminal=False,
            record_event=False,  # a pure read: executed, reported, not persisted
        )
    )
    return reg, calls


def _request():
    return AIRequest(
        system=[SystemBlock(text="persona")],
        messages=[ChatTurn(role="user", content="hi")],
        tools=[],
    )


def _loop(provider, reg):
    # the default NullUsageSink keeps these tests storage-free
    return AgentLoop(provider, reg, "fake")


def test_reply_alongside_terminal_tool_is_kept_without_a_second_call():
    reg, calls = _registry()
    # one turn: a full reply PLUS a save_memory call
    provider = ScriptedProvider(
        [
            _resp(
                "that sounds like a rough night, i'm glad the morning felt better 💛",
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="save_memory",
                        input={"instruction": "slept better"},
                    )
                ],
                stop_reason="tool_use",
            ),
        ]
    )
    loop = _loop(provider, reg)

    result = run(loop.run(_request(), context=CTX, turn_kind="conversation"))

    # the memory was saved...
    assert calls == [("save_memory", {"instruction": "slept better"})]
    # ...the reply survived...
    assert (
        result.text
        == "that sounds like a rough night, i'm glad the morning felt better 💛"
    )
    # ...and we did NOT make a second api call to regenerate a reply
    assert provider.calls == 1


def test_silent_terminal_tool_still_round_trips_to_get_a_reply():
    """if the model saves with NO accompanying text, we must still round-trip so
    the caller isn't left with silence."""
    reg, calls = _registry()
    provider = ScriptedProvider(
        [
            _resp(
                None,
                tool_calls=[
                    ToolCall(id="t1", name="save_memory", input={"instruction": "x"})
                ],
                stop_reason="tool_use",
            ),
            _resp("noted! anything else on your mind?"),
        ]
    )
    loop = _loop(provider, reg)

    result = run(loop.run(_request(), context=CTX, turn_kind="conversation"))

    assert result.text == "noted! anything else on your mind?"
    assert provider.calls == 2  # had to round-trip for the reply


def test_non_terminal_tool_round_trips_and_keeps_all_text():
    """a read result matters, so we round-trip; preamble text is not lost."""
    reg, _ = _registry()
    provider = ScriptedProvider(
        [
            _resp(
                "let me check what i remember...",
                tool_calls=[
                    ToolCall(
                        id="t1", name="search_memories", input={"keywords": ["tea"]}
                    )
                ],
                stop_reason="tool_use",
            ),
            _resp("right - you're a tea person 🍵"),
        ]
    )
    loop = _loop(provider, reg)

    result = run(loop.run(_request(), context=CTX, turn_kind="conversation"))

    assert provider.calls == 2
    assert (
        result.text
        == "let me check what i remember...\n\nright - you're a tea person 🍵"
    )


def test_mixed_terminal_and_non_terminal_round_trips():
    """terminal + non-terminal in one turn must NOT short-circuit - the read
    result still needs a response."""
    reg, calls = _registry()
    provider = ScriptedProvider(
        [
            _resp(
                "one sec",
                tool_calls=[
                    ToolCall(id="t1", name="save_memory", input={"instruction": "x"}),
                    ToolCall(
                        id="t2", name="search_memories", input={"keywords": ["x"]}
                    ),
                ],
                stop_reason="tool_use",
            ),
            _resp("all set"),
        ]
    )
    loop = _loop(provider, reg)

    result = run(loop.run(_request(), context=CTX, turn_kind="conversation"))

    assert provider.calls == 2
    assert ("save_memory", {"instruction": "x"}) in calls
    assert result.text == "one sec\n\nall set"


def test_plain_reply_no_tools_unchanged():
    reg, _ = _registry()
    provider = ScriptedProvider([_resp("just a normal reply")])
    loop = _loop(provider, reg)

    result = run(loop.run(_request(), context=CTX, turn_kind="conversation"))

    assert result.text == "just a normal reply"
    assert provider.calls == 1
    assert result.actions == []  # nothing executed, nothing reported


def test_executed_actions_carry_policy_and_order():
    """the loop reports every executed call (reads, terminals, everything) with
    its result content AND its persistence policy - a recorder never needs to
    consult the registry that ran the tool."""
    reg, _ = _registry()
    provider = ScriptedProvider(
        [
            _resp(
                "one sec",
                tool_calls=[
                    ToolCall(id="t1", name="save_memory", input={"instruction": "x"}),
                    ToolCall(
                        id="t2", name="search_memories", input={"keywords": ["x"]}
                    ),
                ],
                stop_reason="tool_use",
            ),
            _resp("all set"),
        ]
    )
    loop = _loop(provider, reg)

    result = run(loop.run(_request(), context=CTX, turn_kind="conversation"))

    assert [(a.name, a.input) for a in result.actions] == [
        ("save_memory", {"instruction": "x"}),
        ("search_memories", {"keywords": ["x"]}),
    ]
    assert result.actions[0].result_content == "saved"
    assert result.actions[0].terminal is True
    assert result.actions[0].record_event is True  # a mutation: persisted
    assert result.actions[1].result_content == "found: user likes tea"
    assert result.actions[1].terminal is False
    assert result.actions[1].record_event is False  # a pure read: not persisted
    assert all(a.is_error is False for a in result.actions)


def test_terminal_short_circuit_still_reports_actions():
    reg, _ = _registry()
    provider = ScriptedProvider(
        [
            _resp(
                "saved it!",
                tool_calls=[
                    ToolCall(id="t1", name="save_memory", input={"instruction": "y"})
                ],
                stop_reason="tool_use",
            ),
        ]
    )
    loop = _loop(provider, reg)

    result = run(loop.run(_request(), context=CTX, turn_kind="conversation"))

    assert result.stop_reason == "terminal_tools"
    assert [(a.name, a.result_content) for a in result.actions] == [
        ("save_memory", "saved")
    ]


# --- action truth: partial actions survive provider failure (§5.4/§11.16) ----


def test_provider_failure_after_tools_carries_partial_actions():
    """tools ran, then the next provider call died. the mutations are real -
    the error must carry their trail, not erase it."""
    reg, calls = _registry()
    provider = ScriptedProvider(
        [
            _resp(
                None,
                tool_calls=[
                    ToolCall(id="t1", name="save_memory", input={"instruction": "x"})
                ],
                stop_reason="tool_use",
            ),
            ProviderError("boom", retryable=True),
        ]
    )
    loop = _loop(provider, reg)

    with pytest.raises(AgentExecutionError) as exc_info:
        run(loop.run(_request(), context=CTX, turn_kind="conversation"))

    err = exc_info.value
    assert calls == [("save_memory", {"instruction": "x"})]  # it really ran
    assert [(a.name, a.record_event) for a in err.actions] == [("save_memory", True)]
    assert err.retryable is True
    # still a ProviderError, so existing catch blocks keep working
    assert isinstance(err, ProviderError)


def test_provider_failure_before_any_tools_stays_a_plain_provider_error():
    """nothing executed yet, nothing to protect - the typed error passes
    through unwrapped."""
    reg, _ = _registry()
    provider = ScriptedProvider([ProviderError("down", retryable=False)])
    loop = _loop(provider, reg)

    with pytest.raises(ProviderError) as exc_info:
        run(loop.run(_request(), context=CTX, turn_kind="conversation"))
    assert not isinstance(exc_info.value, AgentExecutionError)


# --- usage events -------------------------------------------------------------


def test_usage_events_carry_actor_and_response_model():
    reg, _ = _registry()
    provider = ScriptedProvider([_resp("on it")])

    class CapturingSink:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    sink = CapturingSink()
    loop = AgentLoop(provider, reg, "fake", usage_sink=sink)

    ctx = ToolContext(stream_id="u", activation_id="act-9", actor="tempo")
    run(
        loop.run(_request(), context=ctx, platform="telegram", turn_kind="conversation")
    )

    call_event, trace_event = sink.events
    assert call_event.actor == "tempo"
    assert call_event.platform == "telegram"
    # the model the RESPONSE reported, not the provider object's default
    assert call_event.model == "fake-model-live"
    assert trace_event.actor == "tempo"
    assert trace_event.iterations == 1


def test_usage_sink_failure_never_breaks_the_run():
    reg, _ = _registry()
    provider = ScriptedProvider([_resp("still fine")])

    class ExplodingSink:
        async def emit(self, event):
            raise RuntimeError("ledger on fire")

    loop = AgentLoop(provider, reg, "fake", usage_sink=ExplodingSink())
    result = run(loop.run(_request(), context=CTX, turn_kind="conversation"))
    assert result.text == "still fine"


# --- empty max_tokens responses (thinking ate the budget) ---------------------


def test_empty_max_tokens_response_retries_once_with_doubled_budget():
    """adaptive thinking can consume the whole max_tokens budget and emit zero
    text. the loop must retry once with the ceiling doubled instead of handing
    the caller a silent turn."""
    reg = ToolRegistry()
    provider = ScriptedProvider(
        [
            _resp(None, stop_reason="max_tokens"),
            _resp("okay here's my actual reply!"),
        ]
    )
    request = _request()
    before = request.max_tokens
    result = run(
        _loop(provider, reg).run(request, context=CTX, turn_kind="conversation")
    )

    assert provider.calls == 2
    assert request.max_tokens == before * 2
    assert result.text == "okay here's my actual reply!"


def test_empty_max_tokens_retries_exactly_once():
    reg = ToolRegistry()
    provider = ScriptedProvider(
        [
            _resp(None, stop_reason="max_tokens"),
            _resp(None, stop_reason="max_tokens"),
        ]
    )
    result = run(
        _loop(provider, reg).run(_request(), context=CTX, turn_kind="conversation")
    )

    assert provider.calls == 2  # one retry, never a loop
    assert not result.text  # still empty - the caller's job now


def test_empty_end_turn_response_is_not_retried():
    """an empty response that ISN'T a budget failure (end_turn) stays a single
    call - the retry is targeted, not a blanket second chance."""
    reg = ToolRegistry()
    provider = ScriptedProvider([_resp(None, stop_reason="end_turn")])
    result = run(
        _loop(provider, reg).run(_request(), context=CTX, turn_kind="conversation")
    )
    assert provider.calls == 1
    assert not result.text
