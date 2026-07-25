"""the usage seam: the loop reports, the app persists.

cost accounting is app business (where the ledger lives, what a row looks
like), but WHEN to account is loop business — one record_call per api call,
one record_trace per completed turn. the protocol keeps that split honest.

implementations must never raise into the chat path: swallow and log your own
storage failures (accounting must never break a reply).
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from dainframe.providers.types import Usage


@runtime_checkable
class UsageSink(Protocol):
    def record_call(
        self,
        *,
        stream_id: Optional[str],
        platform: Optional[str],
        provider: str,
        model: str,
        role: str,
        usage: Usage,
        actor: Optional[str] = None,
    ) -> None: ...

    def record_trace(
        self,
        *,
        stream_id: Optional[str],
        platform: Optional[str],
        turn_kind: str,
        iterations: int,
        hit_iteration_cap: bool,
        tool_trace: list,
        final_text_length: int,
        stop_reason: Optional[str],
        total_usage: Usage,
        actor: Optional[str] = None,
    ) -> None: ...


class NullUsageSink:
    """the default: no accounting. apps that care pass a real sink."""

    def record_call(self, **kwargs) -> None:
        pass

    def record_trace(self, **kwargs) -> None:
        pass
