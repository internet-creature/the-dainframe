"""the dainframe: a stimulus-driven multi-agent orchestration framework.

phase 1 exports: provider vocabulary, tool registry, and the agent loop.
provider implementations (AnthropicProvider, OpenAIProvider) are imported
from their own modules so their sdks load only when actually used:

    from dainframe.providers.anthropic import AnthropicProvider
"""

from dainframe.providers.base import BaseAIProvider
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
from dainframe.tools.context import acting_as, current_actor
from dainframe.loop.agent_loop import AgentLoop, AgentResult, ExecutedAction
from dainframe.loop.usage import NullUsageSink, UsageSink

__all__ = [
    "AIRequest",
    "AIResponse",
    "AgentLoop",
    "AgentResult",
    "BaseAIProvider",
    "ChatTurn",
    "ExecutedAction",
    "NullUsageSink",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "SystemBlock",
    "Tool",
    "ToolCall",
    "ToolDef",
    "ToolRegistry",
    "ToolResult",
    "Usage",
    "UsageSink",
    "acting_as",
    "current_actor",
]
