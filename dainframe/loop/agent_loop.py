"""the agentic loop: sits between an Agent implementation and the provider.

runs the tool-call loop provider-agnostically. the iteration cap is a hard
cost guard against runaway loops - on the final iteration tools are removed so
the caller always gets a text answer. all tool results from one response go
back in a single user turn (splitting them degrades parallel tool use).

action truth (DESIGN.md §5.4/§11.16): once a side effect executes, its record
must survive whatever happens next. a provider failure AFTER tools have run
raises AgentExecutionError carrying the partial actions - raising a bare
ProviderError and losing already-executed mutations is not allowed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Optional

from dainframe.providers.base import BaseAIProvider
from dainframe.providers.resolver import HintResolutionError, ProviderResolver
from dainframe.providers.types import AIRequest, ChatTurn, ProviderError, Usage
from dainframe.tools.registry import ToolRegistry
from dainframe.tools.context import ToolContext, tool_context
from dainframe.loop.usage import (
    AgentRunTrace,
    NullUsageSink,
    ProviderCallUsage,
    UsageSink,
)

if TYPE_CHECKING:  # pragma: no cover - core imports this module at load time
    from dainframe.core.types import ExecutionHints

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutedAction:
    """one tool call the loop actually ran, surfaced to the caller so it can
    be recorded into the conversation event log. the loop captures ALL
    executed calls (reads, errors, everything) and attaches the registry's
    persistence policy (`record_event`), so a recorder never needs to consult
    the registry that ran the tool."""

    name: str
    input: Mapping[str, object]
    result_content: str
    is_error: bool
    terminal: bool
    record_event: bool


class AgentExecutionError(ProviderError):
    """a provider failure mid-run, after tools already executed. carries the
    partial actions so their mutations stay recordable - the failure loses the
    prose, never the trail."""

    def __init__(
        self,
        message: str,
        *,
        actions: tuple[ExecutedAction, ...],
        retryable: bool = False,
    ):
        super().__init__(message, retryable=retryable)
        self.actions = actions


@dataclass
class AgentResult:
    text: Optional[str]
    refused: bool = False
    stop_reason: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    hit_iteration_cap: bool = False
    actions: list[ExecutedAction] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        provider: BaseAIProvider,
        registry: ToolRegistry,
        provider_name: str,
        usage_sink: Optional[UsageSink] = None,
        max_iterations: int = 5,
        resolver: Optional["ProviderResolver"] = None,
    ):
        self.provider = provider
        self.registry = registry
        self.provider_name = provider_name
        self.usage = usage_sink or NullUsageSink()
        self.max_iterations = max_iterations
        # the §4.8 routing seam: when set, ExecutionHints passed to run()
        # resolve to a (provider, model, effort) for the whole run. the
        # constructor's provider stays the default route for hint-less runs.
        self.resolver = resolver

    async def run(
        self,
        request: AIRequest,
        *,
        context: ToolContext,
        platform: Optional[str] = None,
        turn_kind: str,
        hints: Optional["ExecutionHints"] = None,
    ) -> AgentResult:
        """`context` is mandatory and activation-scoped: it names the stream,
        the activation, and the actor whose loop this is. it rides in a
        contextvar so tools (e.g. a memory save) attribute the right identity
        without threading it through every handler.

        `hints` is the director's per-line ExecutionHints, resolved ONCE for
        the whole run (§4.8) - provider-native continuation blocks cannot
        switch provider halfway through. resolution never mutates the loop:
        one AgentLoop instance is safely shared across concurrent runs."""
        provider, provider_name = self._route(request, hints, context)
        with tool_context(context):
            return await self._run(
                request,
                context=context,
                platform=platform,
                turn_kind=turn_kind,
                provider=provider,
                provider_name=provider_name,
            )

    def _route(
        self,
        request: AIRequest,
        hints: Optional["ExecutionHints"],
        context: ToolContext,
    ) -> tuple[BaseAIProvider, str]:
        """turn hints into this run's fixed (provider, name), applying the
        validated per-run dials to the request. the honor-or-declare rule
        (§11.6), enforced:

        - no hints: the constructor's provider, untouched.
        - hints + resolver: full routing - provider/model/effort validated
          by the resolver, effort/max_tokens stamped onto the request.
        - hints without a resolver: effort/max_tokens are honored on the
          agent's own declared provider (the cheap dials need no routing);
          a provider/model hint CANNOT be honored and fails before the
          first model call - never silently ignored.
        """
        if hints is None:
            return self.provider, self.provider_name
        if self.resolver is not None:
            resolved = self.resolver.resolve(hints, agent=context.actor)
            if resolved.effort is not None:
                request.effort = resolved.effort
            if resolved.max_tokens is not None:
                request.max_tokens = resolved.max_tokens
            return resolved.provider, resolved.provider_name
        if hints.provider or hints.model:
            raise HintResolutionError(
                f"agent '{context.actor}' received a provider/model hint "
                f"(provider={hints.provider!r}, model={hints.model!r}) but "
                "its loop has no ProviderResolver configured"
            )
        if hints.effort is not None:
            request.effort = hints.effort
        if hints.max_tokens is not None:
            request.max_tokens = hints.max_tokens
        return self.provider, self.provider_name

    async def _run(
        self,
        request: AIRequest,
        *,
        context: ToolContext,
        platform: Optional[str],
        turn_kind: str,
        provider: BaseAIProvider,
        provider_name: str,
    ) -> AgentResult:
        total = Usage()
        tool_trace: list = []
        stop_reason: Optional[str] = None
        # one-shot budget retry: with adaptive thinking, max_tokens covers
        # thinking + reply TOGETHER, so an instruction-heavy turn can burn the
        # whole budget thinking and emit zero text (stop_reason=max_tokens,
        # empty response). when that happens we double the ceiling and retry
        # once - the ceiling only costs when tokens are actually generated.
        budget_retried = False
        # user-facing text the model wrote, in order, across every iteration.
        # a turn can carry both a reply and tool calls; we keep the reply instead
        # of letting a later iteration's text replace it.
        collected_text: list[str] = []
        # every tool call actually executed this turn, in order - returned to
        # the caller so it can persist them as conversation events
        executed: list[ExecutedAction] = []

        for i in range(self.max_iterations):
            response = await self._create_message(request, executed, provider)
            total = total + response.usage
            await self._emit_call(
                context, platform, turn_kind, response, provider, provider_name
            )
            stop_reason = response.stop_reason

            if response.stop_reason == "refusal":
                await self._emit_trace(
                    context,
                    platform,
                    turn_kind,
                    i,
                    False,
                    tool_trace,
                    0,
                    stop_reason,
                    total,
                )
                return AgentResult(
                    text=None,
                    refused=True,
                    stop_reason=stop_reason,
                    usage=total,
                    actions=executed,
                )

            if response.text:
                collected_text.append(response.text)

            if not response.tool_calls:
                final_text = self._join(collected_text)
                if (
                    not final_text
                    and response.stop_reason == "max_tokens"
                    and not budget_retried
                ):
                    budget_retried = True
                    request.max_tokens = request.max_tokens * 2
                    logger.warning(
                        "empty max_tokens response (thinking consumed the "
                        "budget) for stream %s - retrying once with max_tokens=%s",
                        context.stream_id,
                        request.max_tokens,
                    )
                    continue
                await self._emit_trace(
                    context,
                    platform,
                    turn_kind,
                    i + 1,
                    False,
                    tool_trace,
                    len(final_text or ""),
                    stop_reason,
                    total,
                )
                return AgentResult(
                    text=final_text,
                    stop_reason=stop_reason,
                    usage=total,
                    actions=executed,
                )

            # append the assistant turn (with its raw blocks) then run tools
            request.messages.append(response.assistant_turn)
            results = await asyncio.gather(
                *[self.registry.execute(call, context) for call in response.tool_calls]
            )
            request.messages.append(ChatTurn(role="user", tool_results=list(results)))

            tool_trace.append(
                {
                    "iteration": i,
                    "calls": [
                        {"name": c.name, "input": c.input, "is_error": r.is_error}
                        for c, r in zip(response.tool_calls, results)
                    ],
                }
            )
            executed.extend(
                ExecutedAction(
                    name=c.name,
                    input=c.input,
                    result_content=r.content,
                    is_error=r.is_error,
                    terminal=self.registry.is_terminal(c.name),
                    record_event=self.registry.should_record(c.name),
                )
                for c, r in zip(response.tool_calls, results)
            )

            # terminal short-circuit: the model already wrote a reply this turn
            # and every tool it called is a fire-and-forget side effect (now run).
            # keep that reply instead of spending another call to regenerate one -
            # this is what stops a side-effect save from replacing the response.
            all_terminal = all(
                self.registry.is_terminal(c.name) for c in response.tool_calls
            )
            if collected_text and all_terminal:
                final_text = self._join(collected_text)
                await self._emit_trace(
                    context,
                    platform,
                    turn_kind,
                    i + 1,
                    False,
                    tool_trace,
                    len(final_text or ""),
                    "terminal_tools",
                    total,
                )
                return AgentResult(
                    text=final_text,
                    stop_reason="terminal_tools",
                    usage=total,
                    actions=executed,
                )

        # iteration cap reached: force a final answer with tools disabled
        logger.warning(
            "agent hit iteration cap (%s) for stream %s",
            self.max_iterations,
            context.stream_id,
        )
        request.tools = []
        final = await self._create_message(request, executed, provider)
        total = total + final.usage
        await self._emit_call(
            context, platform, turn_kind, final, provider, provider_name
        )
        if not final.text and final.stop_reason == "max_tokens" and not budget_retried:
            # same thinking-ate-the-budget failure on the forced final answer
            budget_retried = True
            request.max_tokens = request.max_tokens * 2
            logger.warning(
                "empty max_tokens response on forced final answer for stream %s "
                "- retrying once with max_tokens=%s",
                context.stream_id,
                request.max_tokens,
            )
            final = await self._create_message(request, executed, provider)
            total = total + final.usage
            await self._emit_call(
                context, platform, turn_kind, final, provider, provider_name
            )
        stop_reason = final.stop_reason
        refused = final.stop_reason == "refusal"
        if final.text:
            collected_text.append(final.text)
        final_text = self._join(collected_text)
        await self._emit_trace(
            context,
            platform,
            turn_kind,
            self.max_iterations,
            True,
            tool_trace,
            len(final_text or ""),
            stop_reason,
            total,
        )
        return AgentResult(
            text=None if refused else final_text,
            refused=refused,
            stop_reason=stop_reason,
            usage=total,
            hit_iteration_cap=True,
            actions=executed,
        )

    async def _create_message(self, request, executed: list, provider):
        """one provider call, wrapped for action truth: if tools already ran
        this turn, a provider failure must carry their trail out with it."""
        try:
            return await provider.create_message(request)
        except AgentExecutionError:
            raise
        except ProviderError as e:
            if executed:
                raise AgentExecutionError(
                    str(e), actions=tuple(executed), retryable=e.retryable
                ) from e
            raise

    @staticmethod
    def _join(parts: list[str]) -> Optional[str]:
        """join the assistant's text fragments into one reply. None if empty, so
        callers keep treating 'no text' as an error/non-answer."""
        cleaned = [p.strip() for p in parts if p and p.strip()]
        return "\n\n".join(cleaned) if cleaned else None

    async def _emit_call(
        self, context, platform, turn_kind, response, provider, provider_name
    ) -> None:
        # the model each response actually reported, not the provider default
        await self._emit(
            ProviderCallUsage(
                stream_id=context.stream_id,
                provider=provider_name,
                model=response.model or provider.model,
                turn_kind=turn_kind,
                usage=response.usage,
                actor=context.actor,
                platform=platform,
            )
        )

    async def _emit_trace(
        self,
        context,
        platform,
        turn_kind,
        iterations,
        hit_cap,
        tool_trace,
        text_len,
        stop_reason,
        total,
    ) -> None:
        await self._emit(
            AgentRunTrace(
                stream_id=context.stream_id,
                turn_kind=turn_kind,
                iterations=iterations,
                hit_iteration_cap=hit_cap,
                tool_trace=tuple(tool_trace),
                final_text_length=text_len,
                stop_reason=stop_reason,
                total_usage=total,
                actor=context.actor,
                platform=platform,
            )
        )

    async def _emit(self, event) -> None:
        # accounting must never break an agent run
        try:
            await self.usage.emit(event)
        except Exception as e:
            logger.error("usage sink failed (continuing): %s", e)
