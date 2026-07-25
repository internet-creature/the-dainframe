"""the dainframe: a stimulus-driven multi-agent orchestration framework.

top-level exports cover the leaf layer: provider vocabulary, tool registry +
context, and the agent loop. the activation contracts (Stimulus, Script,
Orchestrator, EventStore, ...) live in `dainframe.core`, and the reusable
conformance suites in `dainframe.testing`. provider implementations
(AnthropicProvider, OpenAIProvider) are optional extras imported from their
own modules so their sdks load only when actually installed and used:

    from dainframe.providers.anthropic import AnthropicProvider
"""

from dainframe.providers.base import BaseAIProvider
from dainframe.providers.limits import ConcurrencyLimiter
from dainframe.providers.types import (
    AIRequest,
    AIResponse,
    ChatTurn,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    SystemBlock,
    ToolCall,
    ToolDef,
    ToolResult,
    Usage,
)
from dainframe.tools.registry import Tool, ToolRegistry
from dainframe.tools.context import ToolContext, current_tool_context, tool_context
from dainframe.loop.agent_loop import (
    AgentExecutionError,
    AgentLoop,
    AgentResult,
    ExecutedAction,
)
from dainframe.loop.usage import (
    AgentRunTrace,
    NullUsageSink,
    ProviderCallUsage,
    UsageEvent,
    UsageSink,
)

__all__ = [
    "AIRequest",
    "AIResponse",
    "AgentExecutionError",
    "AgentLoop",
    "AgentResult",
    "AgentRunTrace",
    "BaseAIProvider",
    "ChatTurn",
    "ConcurrencyLimiter",
    "ExecutedAction",
    "NullUsageSink",
    "ProviderCallUsage",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "SystemBlock",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolDef",
    "ToolRegistry",
    "ToolResult",
    "Usage",
    "UsageEvent",
    "UsageSink",
    "current_tool_context",
    "tool_context",
]
