"""the orchestrator: the recording/delivery state machine (DESIGN.md §5.5).

PHASE-0 SKELETON: a faithful, working implementation of the §5.5 algorithm
over the frozen contracts - real enough for the consumer spikes and contract
tests, deliberately not yet the whole of phase 3 (no ProviderResolver
integration, no distributed coordination concerns, no pulse dispatcher).
what it already enforces are the §5.6 invariants:

- inbound recorded before direction/acting, when requested
- executed recordable side effects survive refusal, provider failure,
  missing prose, or delivery failure
- refusals and errors add no fictional conversational prose
- a reply becomes shared history only after confirmed delivery
- confirming the same pending delivery twice records it once
- empty output on a required line is an error; on optional/silent it's intent
- text from a silent line is never delivered or recorded as conversation
- activations for one stream are serialized
- stale ambient preconditions cancel before model/tool work
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable, Mapping, Optional

from dainframe.core.agent import (
    Agent,
    Briefing,
    BriefingContext,
    ContextProvider,
    NullContextProvider,
)
from dainframe.core.coordination import InProcessStreamCoordinator, StreamCoordinator
from dainframe.core.delivery import (
    Deliverer,
    DeliveryLedger,
    DeliveryReceipt,
    DeliveryRequest,
    NewPendingDelivery,
)
from dainframe.core.director import Director
from dainframe.core.events import (
    Event,
    EventQuery,
    EventStore,
    NewEvent,
    ReadOnlyEventReader,
)
from dainframe.core.hooks import NullTurnHooks, TurnHooks
from dainframe.core.types import (
    ActivationResult,
    EventContext,
    ExecutionErrorInfo,
    LineResult,
    ScriptLine,
    Stimulus,
)
from dainframe.loop.agent_loop import AgentExecutionError, ExecutedAction
from dainframe.providers.types import ProviderError

logger = logging.getLogger(__name__)


def _default_action_line(speaker: str, action: ExecutedAction) -> str:
    """the library's plain default rendering of an action event. how an action
    should read in an app's prompt is app policy (§4.1) - chordial overrides
    this with its own format_action_line."""
    return f"[{speaker} used {action.name}]"


class Orchestrator:
    def __init__(
        self,
        agents: Mapping[str, Agent],
        store_factory: Callable[[str], EventStore],
        director: Director,
        *,
        context_provider: Optional[ContextProvider] = None,
        hooks: Optional[TurnHooks] = None,
        deliverer: Optional[Deliverer] = None,
        ledger: Optional[DeliveryLedger] = None,
        coordinator: Optional[StreamCoordinator] = None,
        message_window: int = 30,
        action_formatter: Callable[[str, ExecutedAction], str] = _default_action_line,
    ):
        self.agents = dict(agents)
        self.store_factory = store_factory
        self.director = director
        self.context_provider = context_provider or NullContextProvider()
        self.hooks = hooks or NullTurnHooks()
        self.deliverer = deliverer
        self.ledger = ledger
        self.coordinator = coordinator or InProcessStreamCoordinator()
        self.message_window = message_window
        self.action_formatter = action_formatter

    # --- entry point ---------------------------------------------------------

    async def handle(self, stimulus: Stimulus) -> ActivationResult:
        activation_id = f"act-{uuid.uuid4().hex[:12]}"
        async with self.coordinator.hold(stimulus.stream_id):
            store = self.store_factory(stimulus.stream_id)
            # directors and preconditions get the read-only projection (§4.1):
            # only the engine records
            reader = ReadOnlyEventReader(store)

            # stale ambient work cancels before recording, direction, or
            # generation - under the lock, so the check can't race a reply
            if stimulus.precondition is not None:
                if not await stimulus.precondition.holds(reader):
                    return ActivationResult(
                        activation_id=activation_id,
                        stream_id=stimulus.stream_id,
                        inbound_event_id=None,
                        status="cancelled",
                        status_reason="precondition no longer holds",
                        lines=(),
                    )

            inbound_event_id: Optional[str] = None
            if stimulus.record_inbound and stimulus.content:
                prev_user = await store.latest(
                    EventQuery(
                        kinds=frozenset({"message"}),
                        author_types=frozenset({"user"}),
                    )
                )
                inbound = await store.append(
                    NewEvent(
                        author_type=stimulus.inbound_author_type,
                        author=stimulus.inbound_author,
                        kind="message",
                        content=stimulus.content,
                        message_type=stimulus.inbound_message_type,
                        platform=stimulus.platform,
                        scope=stimulus.scope,
                        audience=stimulus.audience,
                    )
                )
                inbound_event_id = inbound.event_id
                await self._safe(
                    self.hooks.after_inbound_recorded, stimulus, store, prev_user
                )

            script = await self.director.direct(stimulus, reader)
            if not script.lines:
                result = ActivationResult(
                    activation_id=activation_id,
                    stream_id=stimulus.stream_id,
                    inbound_event_id=inbound_event_id,
                    status="noop",
                    status_reason=script.noop_reason,
                    lines=(),
                )
                await self._safe(self.hooks.after_turn, stimulus, store, result)
                return result

            line_results = []
            for idx, line in enumerate(script.lines):
                line_results.append(
                    await self._run_line(
                        stimulus, store, activation_id, f"{activation_id}:{idx}", line
                    )
                )

            result = ActivationResult(
                activation_id=activation_id,
                stream_id=stimulus.stream_id,
                inbound_event_id=inbound_event_id,
                status="completed",
                status_reason=None,
                lines=tuple(line_results),
            )
            await self._safe(self.hooks.after_turn, stimulus, store, result)
            return result

    async def confirm_delivery(
        self, pending_id: str, receipt: DeliveryReceipt
    ) -> Event:
        """finalize a pending line: idempotent, exact frozen text, under the
        same stream lock the activation used."""
        if self.ledger is None:
            raise RuntimeError("no DeliveryLedger configured")
        pending = await self.ledger.get(pending_id)
        if pending is None:
            raise KeyError(f"unknown pending delivery '{pending_id}'")
        async with self.coordinator.hold(pending.stream_id):
            store = self.store_factory(pending.stream_id)
            return await self.ledger.confirm(pending_id, receipt, store)

    # --- one line ------------------------------------------------------------

    async def _run_line(
        self,
        stimulus: Stimulus,
        store: EventStore,
        activation_id: str,
        line_id: str,
        line: ScriptLine,
    ) -> LineResult:
        agent = self.agents.get(line.speaker)
        if agent is None:
            # retained as a structured failure, never silently skipped (§4.2)
            logger.error("script cast unknown agent '%s'", line.speaker)
            return LineResult(
                line_id=line_id,
                speaker=line.speaker,
                status="errored",
                error=ExecutionErrorInfo(
                    kind="unknown_speaker",
                    message=f"no agent registered as '{line.speaker}'",
                ),
            )

        ec = line.event_context or EventContext(
            platform=stimulus.platform,
            scope=stimulus.scope,
            audience=stimulus.audience,
        )

        # the viewer-filtered window, built after all prior lines recorded
        events = await store.read(
            EventQuery(viewer=line.speaker, message_limit=self.message_window)
        )
        try:
            ctx = await self.context_provider.enrich(stimulus, line)
        except Exception:
            logger.exception("context provider failed; briefing without enrichment")
            ctx = BriefingContext()

        briefing = Briefing(
            kind=ctx.kind or stimulus.kind,
            stream_id=stimulus.stream_id,
            activation_id=activation_id,
            platform=stimulus.platform,
            events=tuple(events),
            ambient_context=ctx.ambient_context,
            cue=line.cue,
            style=line.style,
            scope=stimulus.scope,
            reason=stimulus.reason,
            execution=line.execution,
            extras={**dict(stimulus.extras), **dict(ctx.extras)},
        )

        try:
            outcome = await agent.act(briefing)
        except AgentExecutionError as e:
            # action truth: the mutations already happened - record their
            # trail even though the run died (§5.4/§11.16)
            await self._record_actions(store, line.speaker, ec, e.actions)
            return LineResult(
                line_id=line_id,
                speaker=line.speaker,
                status="errored",
                actions=e.actions,
                error=ExecutionErrorInfo(
                    kind="provider_failure_after_tools",
                    message=str(e),
                    retryable=e.retryable,
                ),
            )
        except ProviderError as e:
            return LineResult(
                line_id=line_id,
                speaker=line.speaker,
                status="errored",
                error=ExecutionErrorInfo(
                    kind="provider_failure", message=str(e), retryable=e.retryable
                ),
            )

        # successful recordable side effects persist regardless of what the
        # response/delivery policy decides below
        await self._record_actions(store, line.speaker, ec, outcome.actions)

        if outcome.refused:
            return LineResult(
                line_id=line_id,
                speaker=line.speaker,
                status="refused",
                actions=outcome.actions,
            )
        if outcome.errored:
            return LineResult(
                line_id=line_id,
                speaker=line.speaker,
                status="errored",
                actions=outcome.actions,
                error=ExecutionErrorInfo(
                    kind="agent_error", message="agent reported an errored outcome"
                ),
            )

        if line.response == "silent":
            if outcome.text:
                # never leak private evaluator prose (§5.2): a silent agent
                # that unexpectedly returns text is a line error
                return LineResult(
                    line_id=line_id,
                    speaker=line.speaker,
                    status="errored",
                    actions=outcome.actions,
                    error=ExecutionErrorInfo(
                        kind="unexpected_text_from_silent_line",
                        message="silent line produced text; not delivered, not recorded",
                    ),
                )
            return LineResult(
                line_id=line_id,
                speaker=line.speaker,
                status="silent",
                actions=outcome.actions,
            )

        if not outcome.text:
            if line.response == "required":
                # an empty reply on a turn that owes one is an error, never a
                # silent shrug
                return LineResult(
                    line_id=line_id,
                    speaker=line.speaker,
                    status="errored",
                    actions=outcome.actions,
                    error=ExecutionErrorInfo(
                        kind="empty_required_reply",
                        message="required line produced no deliverable text",
                    ),
                )
            return LineResult(  # optional: absence is accepted
                line_id=line_id,
                speaker=line.speaker,
                status="silent",
                actions=outcome.actions,
            )

        return await self._deliver_text(
            stimulus, store, activation_id, line_id, line, ec, outcome
        )

    async def _deliver_text(
        self, stimulus, store, activation_id, line_id, line, ec, outcome
    ) -> LineResult:
        target = line.target or stimulus.target

        if line.delivery == "direct":
            # no unconfirmed fallback (§11.3): a missing deliverer/target is a
            # configuration failure, not a quiet redefinition of "delivered"
            if self.deliverer is None or target is None:
                return LineResult(
                    line_id=line_id,
                    speaker=line.speaker,
                    status="errored",
                    actions=outcome.actions,
                    error=ExecutionErrorInfo(
                        kind="delivery_configuration",
                        message="delivery='direct' needs a Deliverer and a target",
                    ),
                )
            receipt = await self.deliverer.deliver(
                DeliveryRequest(
                    stream_id=stimulus.stream_id,
                    activation_id=activation_id,
                    line_id=line_id,
                    speaker=line.speaker,
                    target=target,
                    text=outcome.text,
                )
            )
            if receipt is None:
                # actions stay recorded; the undelivered prose stays out
                return LineResult(
                    line_id=line_id,
                    speaker=line.speaker,
                    status="errored",
                    actions=outcome.actions,
                    error=ExecutionErrorInfo(
                        kind="delivery_failed",
                        message="platform did not confirm the send",
                        retryable=True,
                    ),
                )
            await store.append(
                NewEvent(
                    author_type="agent",
                    author=line.speaker,
                    kind="message",
                    content=outcome.text,
                    message_type=ec.outbound_message_type,
                    platform=ec.platform,
                    scope=ec.scope,
                    audience=ec.audience,
                )
            )
            return LineResult(
                line_id=line_id,
                speaker=line.speaker,
                status="delivered",
                actions=outcome.actions,
            )

        # delivery == "pending" (script validation permits nothing else here)
        if self.ledger is None or target is None:
            return LineResult(
                line_id=line_id,
                speaker=line.speaker,
                status="errored",
                actions=outcome.actions,
                error=ExecutionErrorInfo(
                    kind="delivery_configuration",
                    message="delivery='pending' needs a DeliveryLedger and a target",
                ),
            )
        pending = await self.ledger.stage(
            NewPendingDelivery(
                stream_id=stimulus.stream_id,
                activation_id=activation_id,
                line_id=line_id,
                speaker=line.speaker,
                target=target,
                text=outcome.text,
                event_context=ec,
            )
        )
        return LineResult(
            line_id=line_id,
            speaker=line.speaker,
            status="pending",
            actions=outcome.actions,
            pending=pending,
        )

    # --- recording -----------------------------------------------------------

    async def _record_actions(
        self,
        store: EventStore,
        speaker: str,
        ec: EventContext,
        actions: tuple[ExecutedAction, ...],
    ) -> None:
        """persist successful recordable actions with the line's effective
        event context - private evaluator actions must not inherit a
        candidate-facing channel (§5.5). the policy travels ON the action, so
        no registry lookup happens here (§5.4)."""
        for action in actions:
            if action.is_error or not action.record_event:
                continue
            await store.append(
                NewEvent(
                    author_type="agent",
                    author=speaker,
                    kind="action",
                    content=self.action_formatter(speaker, action),
                    platform=ec.platform,
                    scope=ec.scope,
                    audience=ec.audience,
                    metadata={
                        "tool": action.name,
                        "input": dict(action.input),
                        "result": action.result_content,
                    },
                )
            )

    @staticmethod
    async def _safe(hook, *args) -> None:
        """hooks are isolated: their failure never changes the activation."""
        try:
            await hook(*args)
        except Exception:
            logger.exception("turn hook failed (isolated)")
