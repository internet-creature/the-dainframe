"""the director seam (DESIGN.md §4.2): who speaks, with what obligations.

the director owns speaker selection, line caps, fallbacks, and dedup. the
engine's only guardrail is that unknown speakers become structured failed
LineResults - a typo in the only speaker must not resemble a successful quiet
activation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dainframe.core.events import EventReader
from dainframe.core.types import Script, ScriptLine, Stimulus


@runtime_checkable
class Director(Protocol):
    async def direct(self, stimulus: Stimulus, events: EventReader) -> Script: ...


class SingleSpeakerDirector:
    """the trivial default: every stimulus casts one speaker with one fixed
    obligation. single-agent apps need nothing more."""

    def __init__(
        self,
        speaker: str,
        response: str = "required",
        delivery: str = "direct",
    ):
        self.speaker = speaker
        self.response = response
        self.delivery = delivery

    async def direct(self, stimulus: Stimulus, events: EventReader) -> Script:
        return Script(
            lines=(
                ScriptLine(
                    speaker=self.speaker,
                    response=self.response,  # type: ignore[arg-type]
                    delivery=self.delivery,  # type: ignore[arg-type]
                ),
            )
        )
