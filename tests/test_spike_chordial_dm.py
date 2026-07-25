"""PHASE-0 SPIKE #1: chordial's ordinary DM path (DESIGN.md §9 phase 0).

a user messages one helper in a private dm. the companion agent runs a REAL
AgentLoop (phase-1 machinery) over a scripted fake provider, replies while
saving a memory (terminal tool), and the engine records inbound → action →
confirmed reply in order. exercises the public API end to end: Stimulus →
Director → Briefing → Agent(AgentLoop) → EventStore → Deliverer →
ActivationResult.

also proves two invariants the ambient design leans on: a stale
ActivationPrecondition cancels before any model work, and a failing hook
never touches the result.
"""
from __future__ import annotations

import asyncio

from dainframe.core import (
    ActivationResult,
    DeliveryReceipt,
    DeliveryRequest,
    EventQuery,
    InMemoryEventStore,
    NewEvent,
    Orchestrator,
    Script,
    ScriptLine,
    Stimulus,
    DeliveryTarget,
)
from dainframe.core.agent import AgentOutcome, Briefing
from dainframe.loop.agent_loop import AgentLoop
from dainframe.providers.types import (
    AIRequest,
    AIResponse,
    ChatTurn,
    SystemBlock,
    ToolCall,
    ToolDef,
    Usage,
)
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool, ToolRegistry


def run(coro):
    return asyncio.run(coro)


# --- chordial-shaped wiring, app-side ----------------------------------------


def chordial_visibility(event, viewer):
    """a helper sees the group channel plus its OWN dms, never a sibling's."""
    if event.scope == "group" or event.audience is None:
        return True
    return event.audience == viewer


class DmDirector:
    """the dm rule: the addressed helper is the lone voice, chordial fields
    everything else."""

    async def direct(self, stimulus, events):
        # the engine hands directors a read-only projection (§4.1): only the
        # engine records
        assert not hasattr(events, "append")
        speaker = stimulus.addressed[0] if stimulus.addressed else "chordial"
        return Script(lines=(ScriptLine(speaker=speaker),))


class ScriptedProvider:
    model = "fake-chat-model"

    def __init__(self, responses):
        self._responses = list(responses)

    async def create_message(self, request: AIRequest) -> AIResponse:
        return self._responses.pop(0)


def _resp(text, tool_calls=None):
    return AIResponse(
        text=text,
        tool_calls=tool_calls or [],
        stop_reason="tool_use" if tool_calls else "end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
        assistant_turn=ChatTurn(role="assistant", content=text),
        model="fake-chat-model",
    )


class CompanionAgent:
    """chordial's companion shape: prompt from the briefing window, a real
    AgentLoop tool run, outcome carrying text + actions."""

    def __init__(self, name, loop):
        self.name = name
        self.loop = loop
        self.briefings = []

    async def act(self, briefing: Briefing) -> AgentOutcome:
        self.briefings.append(briefing)
        request = AIRequest(
            system=[SystemBlock(text=f"you are {self.name}, warm and grounded")],
            messages=[
                ChatTurn(role="user" if e.author_type == "user" else "assistant",
                         content=e.content)
                for e in briefing.events
                if e.kind == "message"
            ],
        )
        result = await self.loop.run(
            request,
            context=ToolContext(
                stream_id=briefing.stream_id,
                activation_id=briefing.activation_id,
                actor=self.name,
            ),
            platform=briefing.platform,
            turn_kind="conversation",
        )
        return AgentOutcome(
            text=result.text,
            actions=tuple(result.actions),
            refused=result.refused,
        )


class CapturingDeliverer:
    def __init__(self):
        self.requests: list[DeliveryRequest] = []

    async def deliver(self, request):
        self.requests.append(request)
        return DeliveryReceipt(provider_message_id=f"tg-{len(self.requests)}")


def _memory_registry():
    saved = []

    async def _save(tool_input, context):
        saved.append((context.actor, tool_input["instruction"]))
        return "saved"

    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDef(
                name="save_memory", description="save a memory",
                input_schema={"type": "object"},
            ),
            handler=_save,
            terminal=True,
        )
    )
    return registry, saved


def _wire(provider_responses):
    stores = {}
    registry, saved = _memory_registry()
    loop = AgentLoop(ScriptedProvider(provider_responses), registry, "fake")
    companion = CompanionAgent("chordial", loop)
    deliverer = CapturingDeliverer()
    engine = Orchestrator(
        agents={"chordial": companion},
        store_factory=lambda sid: stores.setdefault(
            sid, InMemoryEventStore(visibility=chordial_visibility)
        ),
        director=DmDirector(),
        deliverer=deliverer,
    )
    return engine, stores, companion, deliverer, saved


REPLY = "the parser is DONE?? that's huge, i'm so proud of you 💜"


def _dm_stimulus():
    return Stimulus(
        kind="user_message",
        stream_id="user-dain",
        content="i finished the parser!!",
        platform="telegram",
        scope="dm",
        audience="chordial",
        addressed=("chordial",),
        target=DeliveryTarget(platform="telegram", target_id="chat-1"),
    )


def test_dm_turn_records_inbound_action_and_confirmed_reply_in_order():
    engine, stores, companion, deliverer, saved = _wire([
        _resp(
            REPLY,
            tool_calls=[ToolCall(
                id="t1", name="save_memory",
                input={"instruction": "finished the parser"},
            )],
        ),
    ])

    result: ActivationResult = run(engine.handle(_dm_stimulus()))

    # the result is lossless: one delivered line, its action attached
    assert result.status == "completed"
    (line,) = result.lines
    assert line.status == "delivered"
    assert [a.name for a in line.actions] == ["save_memory"]

    # the platform confirmed BEFORE the reply became shared history
    (request,) = deliverer.requests
    assert request.text == REPLY
    assert request.target.target_id == "chat-1"

    # the event log reads: inbound first, then the action, then the reply
    events = run(stores["user-dain"].read(EventQuery()))
    assert [(e.kind, e.author) for e in events] == [
        ("message", "user"),
        ("action", "chordial"),
        ("message", "chordial"),
    ]
    assert events[0].event_id == result.inbound_event_id
    assert events[2].content == REPLY
    assert events[2].audience == "chordial"     # the dm stays a private channel

    # the tool really ran, attributed to the acting persona
    assert saved == [("chordial", "finished the parser")]

    # the companion was briefed with the inbound message as the last event
    briefing = companion.briefings[0]
    assert briefing.events[-1].content == "i finished the parser!!"
    assert briefing.kind == "user_message"


def test_failed_send_keeps_actions_but_not_the_prose():
    """the mutation is real; the undelivered reply never enters history."""

    class RefusingDeliverer:
        async def deliver(self, request):
            return None

    stores = {}
    registry, saved = _memory_registry()
    loop = AgentLoop(
        ScriptedProvider([
            _resp(REPLY, tool_calls=[ToolCall(
                id="t1", name="save_memory", input={"instruction": "x"})]),
        ]),
        registry, "fake",
    )
    engine = Orchestrator(
        agents={"chordial": CompanionAgent("chordial", loop)},
        store_factory=lambda sid: stores.setdefault(sid, InMemoryEventStore()),
        director=DmDirector(),
        deliverer=RefusingDeliverer(),
    )

    result = run(engine.handle(_dm_stimulus()))

    (line,) = result.lines
    assert line.status == "errored"
    assert line.error.kind == "delivery_failed"
    events = run(stores["user-dain"].read(EventQuery()))
    assert [e.kind for e in events] == ["message", "action"]   # no reply event
    assert saved  # the save still happened and its trail survived


def test_stale_ambient_precondition_cancels_before_any_model_work():
    """the pulse shape: a scheduled check-in whose reason evaporated (the user
    already spoke) cancels under the stream lock - zero generation."""

    class UserStillSilent:
        """holds only if the user has NOT spoken since planning observed."""

        async def holds(self, events):
            latest = await events.latest(
                EventQuery(kinds=frozenset({"message"}),
                           author_types=frozenset({"user"}))
            )
            return latest is None

    engine, stores, companion, deliverer, _ = _wire([])  # NO scripted responses

    # the user spoke while the firing waited for the stream
    store = stores.setdefault(
        "user-dain", InMemoryEventStore(visibility=chordial_visibility)
    )
    run(store.append(NewEvent(
        author_type="user", author="user", kind="message",
        content="hey!", message_type="conversation",
    )))

    result = run(engine.handle(Stimulus(
        kind="scheduled_tick",
        stream_id="user-dain",
        record_inbound=False,
        platform="telegram",
        audience="chordial",
        addressed=("chordial",),
        reason="quiet for a while",
        precondition=UserStillSilent(),
        target=DeliveryTarget(platform="telegram", target_id="chat-1"),
    )))

    assert result.status == "cancelled"
    assert result.lines == ()
    assert companion.briefings == []        # no briefing, no tokens
    assert deliverer.requests == []


def test_a_failing_hook_never_touches_the_result():
    class ExplodingHooks:
        async def after_inbound_recorded(self, *a):
            raise RuntimeError("hook kaput")

        async def after_turn(self, *a):
            raise RuntimeError("hook kaput")

    engine, stores, *_ = _wire([_resp(REPLY)])
    engine.hooks = ExplodingHooks()

    result = run(engine.handle(_dm_stimulus()))
    assert result.status == "completed"
    assert result.lines[0].status == "delivered"
