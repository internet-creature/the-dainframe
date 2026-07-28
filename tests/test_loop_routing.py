"""AgentLoop + ExecutionHints: one resolution per run, honor-or-fail-loudly
(§4.8/§11.6), and no mutation of the shared loop."""

from __future__ import annotations

import asyncio

import pytest

from dainframe.core.types import ExecutionHints
from dainframe.loop.agent_loop import AgentLoop
from dainframe.providers import (
    BaseAIProvider,
    HintResolutionError,
    ProviderTable,
)
from dainframe.providers.types import AIRequest, AIResponse, ChatTurn, Usage
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


class ScriptedProvider(BaseAIProvider):
    def __init__(self, model="fake-model", reply="hi", limiter=None):
        self.model = model
        self.reply = reply
        self.limiter = limiter
        self.requests: list[AIRequest] = []

    async def create_message(self, request):
        self.requests.append(request)
        return AIResponse(
            text=f"{self.reply} (from {self.model})",
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
            assistant_turn=ChatTurn(role="assistant", content=self.reply),
            model=self.model,
        )

    async def is_available(self):
        return True


class RecordingSink:
    def __init__(self):
        self.calls = []

    async def emit(self, event):
        self.calls.append(event)


def ctx(actor="aria"):
    return ToolContext(stream_id="s", activation_id="act-1", actor=actor)


def request(max_tokens=2048):
    return AIRequest(
        system=[],
        messages=[ChatTurn(role="user", content="hello")],
        tools=[],
        max_tokens=max_tokens,
    )


def table_with(providers):
    """a ProviderTable whose builders hand back pre-made ScriptedProviders,
    keyed '<name>/<model>'."""

    def builder_for(name):
        def build(model, limiter):
            return providers[f"{name}/{model}"]

        return build

    return ProviderTable(
        default_provider="anthropic",
        default_models={"anthropic": "sonnet", "openai": "gpt"},
        builders={
            "anthropic": builder_for("anthropic"),
            "openai": builder_for("openai"),
        },
        effort_support={"anthropic": True, "openai": False},
    )


def test_no_hints_means_the_constructor_provider_untouched():
    default = ScriptedProvider(model="sonnet")
    loop = AgentLoop(default, ToolRegistry(), "anthropic")
    result = run(loop.run(request(), context=ctx(), turn_kind="chat"))
    assert result.text == "hi (from sonnet)"
    assert len(default.requests) == 1


def test_hints_with_resolver_route_the_whole_run():
    default = ScriptedProvider(model="sonnet")
    fast = ScriptedProvider(model="haiku")
    sink = RecordingSink()
    loop = AgentLoop(
        default,
        ToolRegistry(),
        "anthropic",
        usage_sink=sink,
        resolver=table_with({"anthropic/sonnet": default, "anthropic/haiku": fast}),
    )
    result = run(
        loop.run(
            request(),
            context=ctx("scorer"),
            turn_kind="score",
            hints=ExecutionHints(model="haiku", effort="low", max_tokens=512),
        )
    )
    assert result.text == "hi (from haiku)"
    assert default.requests == []  # the default never saw the run
    # the validated dials were stamped onto the request
    assert fast.requests[0].effort == "low"
    assert fast.requests[0].max_tokens == 512
    # and usage carries the resolved identity
    assert sink.calls[0].provider == "anthropic"
    assert sink.calls[0].model == "haiku"


def test_empty_hints_with_resolver_use_the_default_route():
    default = ScriptedProvider(model="sonnet")
    loop = AgentLoop(
        default,
        ToolRegistry(),
        "anthropic",
        resolver=table_with({"anthropic/sonnet": default}),
    )
    result = run(
        loop.run(
            request(),
            context=ctx(),
            turn_kind="chat",
            hints=ExecutionHints(),
        )
    )
    assert result.text == "hi (from sonnet)"
    assert request().effort is None  # nothing stamped from nothing


def test_provider_hint_without_resolver_fails_before_any_call():
    default = ScriptedProvider(model="sonnet")
    loop = AgentLoop(default, ToolRegistry(), "anthropic")
    with pytest.raises(HintResolutionError, match="no ProviderResolver"):
        run(
            loop.run(
                request(),
                context=ctx(),
                turn_kind="chat",
                hints=ExecutionHints(model="haiku"),
            )
        )
    assert default.requests == []  # failed BEFORE the first call


def test_cheap_dials_without_resolver_apply_to_the_declared_provider():
    """effort/max_tokens need no routing: the agent's own wired provider is
    its declared policy, and the dials are honored on it."""
    default = ScriptedProvider(model="sonnet")
    loop = AgentLoop(default, ToolRegistry(), "anthropic")
    run(
        loop.run(
            request(),
            context=ctx(),
            turn_kind="chat",
            hints=ExecutionHints(effort="high", max_tokens=999),
        )
    )
    assert default.requests[0].effort == "high"
    assert default.requests[0].max_tokens == 999


def test_resolution_never_mutates_the_shared_loop():
    default = ScriptedProvider(model="sonnet")
    fast = ScriptedProvider(model="haiku")
    loop = AgentLoop(
        default,
        ToolRegistry(),
        "anthropic",
        resolver=table_with({"anthropic/sonnet": default, "anthropic/haiku": fast}),
    )
    run(
        loop.run(
            request(),
            context=ctx(),
            turn_kind="chat",
            hints=ExecutionHints(model="haiku"),
        )
    )
    # a later hint-less run still gets the constructor default
    result = run(loop.run(request(), context=ctx(), turn_kind="chat"))
    assert result.text == "hi (from sonnet)"
    assert loop.provider is default
