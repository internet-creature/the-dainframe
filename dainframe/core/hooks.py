"""turn hooks (DESIGN.md §4.5): app reactions to engine milestones.

every hook call is isolated: its exception is logged and never changes the
activation result or prevents later hooks. chordial's platform-switch
courtesy and completion reconciler live behind these.
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol, runtime_checkable

from dainframe.core.events import Event, EventStore
from dainframe.core.types import ActivationResult, Stimulus

logger = logging.getLogger(__name__)


@runtime_checkable
class TurnHooks(Protocol):
    async def after_inbound_recorded(
        self,
        stimulus: Stimulus,
        store: EventStore,
        prev_user_event: Optional[Event],
    ) -> None: ...

    async def after_turn(
        self, stimulus: Stimulus, store: EventStore, result: ActivationResult
    ) -> None: ...


class NullTurnHooks:
    async def after_inbound_recorded(self, stimulus, store, prev_user_event) -> None:
        pass

    async def after_turn(self, stimulus, store, result) -> None:
        pass


class CompositeTurnHooks:
    """ordered composition with per-hook isolation: one failing hook never
    silences the ones after it."""

    def __init__(self, hooks: list[TurnHooks]):
        self._hooks = list(hooks)

    async def after_inbound_recorded(self, stimulus, store, prev_user_event) -> None:
        for hook in self._hooks:
            try:
                await hook.after_inbound_recorded(stimulus, store, prev_user_event)
            except Exception:
                logger.exception("after_inbound_recorded hook failed (isolated)")

    async def after_turn(self, stimulus, store, result) -> None:
        for hook in self._hooks:
            try:
                await hook.after_turn(stimulus, store, result)
            except Exception:
                logger.exception("after_turn hook failed (isolated)")
