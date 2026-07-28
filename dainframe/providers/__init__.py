"""provider layer: one neutral request/response vocabulary, N backends.

concrete providers live in their own modules (dainframe.providers.anthropic,
dainframe.providers.openai) so each sdk is only imported when used.
"""

from dainframe.providers.base import BaseAIProvider
from dainframe.providers.resolver import (
    HintResolutionError,
    ProviderResolver,
    ProviderTable,
    ResolvedProvider,
)
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

__all__ = [
    "AIRequest",
    "AIResponse",
    "BaseAIProvider",
    "ChatTurn",
    "HintResolutionError",
    "ProviderResolver",
    "ProviderTable",
    "ResolvedProvider",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "SystemBlock",
    "ToolCall",
    "ToolDef",
    "ToolResult",
    "Usage",
]
