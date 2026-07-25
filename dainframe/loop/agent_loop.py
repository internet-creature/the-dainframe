"""the agentic loop: sits between an Agent implementation and the provider.

runs the tool-call loop provider-agnostically. the iteration cap is a hard
cost guard against runaway loops - on the final iteration tools are removed so
the caller always gets a text answer. all tool results from one response go
back in a single user turn (splitting them degrades parallel tool use).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from dainframe.providers.base import BaseAIProvider
from dainframe.providers.types import AIRequest, ChatTurn, Usage
from dainframe.tools.registry import ToolRegistry
from dainframe.tools.context import acting_as
from dainframe.loop.usage import NullUsageSink, UsageSink

logger = logging.getLogger(__name__)


@dataclass
class ExecutedAction:
    """one tool call the loop actually ran, surfaced to the caller so it can
    be recorded into the conversation event log. the loop captures ALL
    executed calls (reads, errors, everything) - which ones get persisted is
    the recorder's policy, not the loop's."""

    name: str
    input: dict
    result_content: str
    is_error: bool
    terminal: bool


@dataclass
class AgentResult:
    text: Optional[str]
    refused: bool = False
    stop_reason: Optional[str] = None
    usage: Usage = None
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
    ):
        self.provider = provider
        self.registry = registry
        self.provider_name = provider_name
        self.usage = usage_sink or NullUsageSink()
        self.max_iterations = max_iterations

    async def run(
        self,
        request: AIRequest,
        *,
        stream_id: Optional[str],
        platform: Optional[str],
        turn_kind: str,
        actor: str = "agent",
    ) -> AgentResult:
        """`actor` is the named agent whose tool loop this is - it rides in a
        contextvar so tools (e.g. a memory save) attribute the right identity
        without threading it through every handler."""
        with acting_as(actor):
            return await self._run(
                request,
                stream_id=stream_id,
                platform=platform,
                turn_kind=turn_kind,
                actor=actor,
            )

    async def _run(
        self,
        request: AIRequest,
        *,
        stream_id: Optional[str],
        platform: Optional[str],
        turn_kind: str,
        actor: str,
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
            response = await self.provider.create_message(request)
            total = total + response.usage
            self._record_call(stream_id, platform, turn_kind, response.usage, actor)
            stop_reason = response.stop_reason

            if response.stop_reason == "refusal":
                self._save_trace(
                    stream_id,
                    platform,
                    turn_kind,
                    i,
                    False,
                    tool_trace,
                    0,
                    stop_reason,
                    total,
                    actor,
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
                        stream_id, request.max_tokens,
                    )
                    continue
                self._save_trace(
                    stream_id,
                    platform,
                    turn_kind,
                    i + 1,
                    False,
                    tool_trace,
                    len(final_text or ""),
                    stop_reason,
                    total,
                    actor,
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
                *[
                    self.registry.execute(call, stream_id)
                    for call in response.tool_calls
                ]
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
                self._save_trace(
                    stream_id,
                    platform,
                    turn_kind,
                    i + 1,
                    False,
                    tool_trace,
                    len(final_text or ""),
                    "terminal_tools",
                    total,
                    actor,
                )
                return AgentResult(
                    text=final_text,
                    stop_reason="terminal_tools",
                    usage=total,
                    actions=executed,
                )

        # iteration cap reached: force a final answer with tools disabled
        logger.warning(
            "agent hit iteration cap (%s) for stream %s", self.max_iterations, stream_id
        )
        request.tools = []
        final = await self.provider.create_message(request)
        total = total + final.usage
        self._record_call(stream_id, platform, turn_kind, final.usage, actor)
        if (
            not final.text
            and final.stop_reason == "max_tokens"
            and not budget_retried
        ):
            # same thinking-ate-the-budget failure on the forced final answer
            budget_retried = True
            request.max_tokens = request.max_tokens * 2
            logger.warning(
                "empty max_tokens response on forced final answer for stream %s "
                "- retrying once with max_tokens=%s", stream_id, request.max_tokens,
            )
            final = await self.provider.create_message(request)
            total = total + final.usage
            self._record_call(stream_id, platform, turn_kind, final.usage, actor)
        stop_reason = final.stop_reason
        refused = final.stop_reason == "refusal"
        if final.text:
            collected_text.append(final.text)
        final_text = self._join(collected_text)
        self._save_trace(
            stream_id,
            platform,
            turn_kind,
            self.max_iterations,
            True,
            tool_trace,
            len(final_text or ""),
            stop_reason,
            total,
            actor,
        )
        return AgentResult(
            text=None if refused else final_text,
            refused=refused,
            stop_reason=stop_reason,
            usage=total,
            hit_iteration_cap=True,
            actions=executed,
        )

    @staticmethod
    def _join(parts: list[str]) -> Optional[str]:
        """join the assistant's text fragments into one reply. None if empty, so
        callers keep treating 'no text' as an error/non-answer."""
        cleaned = [p.strip() for p in parts if p and p.strip()]
        return "\n\n".join(cleaned) if cleaned else None

    def _record_call(self, stream_id, platform, turn_kind, usage, actor) -> None:
        self.usage.record_call(
            stream_id=stream_id,
            platform=platform,
            provider=self.provider_name,
            model=self.provider.model,
            role=turn_kind,
            usage=usage,
            actor=actor,
        )

    def _save_trace(
        self,
        stream_id,
        platform,
        turn_kind,
        iterations,
        hit_cap,
        tool_trace,
        text_len,
        stop_reason,
        total,
        actor,
    ) -> None:
        self.usage.record_trace(
            stream_id=stream_id,
            platform=platform,
            turn_kind=turn_kind,
            iterations=iterations,
            hit_iteration_cap=hit_cap,
            tool_trace=tool_trace,
            final_text_length=text_len,
            stop_reason=stop_reason,
            total_usage=total,
            actor=actor,
        )
