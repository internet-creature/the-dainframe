"""PHASE-0 SPIKE #2: the interview shape (DESIGN.md §9 phase 0).

one candidate answer casts `[required/direct interviewer, silent/none
scorer]` - the mixed visible/silent activation that revision 3's per-line
policy exists for (§11.1). proves:

- the interviewer's reply is delivered and recorded on the candidate channel
- the scorer acts silently; its action records under a PRIVATE EventContext
- the candidate's event window never shows scorer evidence (visibility)
- the scorer is briefed AFTER the interviewer's line is recorded (genuine
  reaction ordering)
- a silent line that unexpectedly produces text is a line error: private
  evaluator prose is never delivered or recorded (§5.2)
"""

from __future__ import annotations

import asyncio

from dainframe.core import (
    DeliveryReceipt,
    DeliveryTarget,
    EventContext,
    EventQuery,
    InMemoryEventStore,
    Orchestrator,
    Script,
    ScriptLine,
    Stimulus,
)
from dainframe.core.agent import AgentOutcome
from dainframe.loop.agent_loop import ExecutedAction


def run(coro):
    return asyncio.run(coro)


# --- interview-shaped wiring, app-side ---------------------------------------


def candidate_never_sees_scores(event, viewer):
    """scorer evidence lives on the 'panel' audience; the candidate's view
    (and anything rendered to them) excludes it."""
    if event.audience == "panel":
        return viewer in ("scorer", "interviewer")
    return True


class InterviewDirector:
    """a state-machine stand-in: every candidate answer casts the visible
    interviewer plus a silent scorer with a private event context."""

    async def direct(self, stimulus, events):
        if stimulus.kind != "candidate_answer":
            return Script(noop_reason=f"no rule for kind '{stimulus.kind}'")
        return Script(
            lines=(
                ScriptLine(
                    speaker="interviewer",
                    cue="probe deeper on the tradeoff they just mentioned",
                    response="required",
                    delivery="direct",
                ),
                ScriptLine(
                    speaker="scorer",
                    response="silent",
                    delivery="none",
                    event_context=EventContext(
                        platform="web",
                        scope="panel",
                        audience="panel",
                    ),
                ),
            )
        )


class FakeInterviewer:
    name = "interviewer"

    def __init__(self):
        self.briefings = []

    async def act(self, briefing):
        self.briefings.append(briefing)
        return AgentOutcome(text="interesting - why a queue over a stack there?")


def _score_action(note):
    return ExecutedAction(
        name="record_score",
        input={"dimension": "reasoning", "note": note},
        result_content="scored",
        is_error=False,
        terminal=True,
        record_event=True,
    )


class FakeScorer:
    name = "scorer"

    def __init__(self, leak_text=None):
        self.briefings = []
        self._leak_text = leak_text

    async def act(self, briefing):
        self.briefings.append(briefing)
        return AgentOutcome(
            text=self._leak_text,  # None in the correct shape
            actions=(_score_action("clear tradeoff articulation"),),
        )


class AcceptingDeliverer:
    def __init__(self):
        self.requests = []

    async def deliver(self, request):
        self.requests.append(request)
        return DeliveryReceipt(provider_message_id=f"ws-{len(self.requests)}")


def _wire(scorer):
    stores = {}
    interviewer = FakeInterviewer()
    deliverer = AcceptingDeliverer()
    engine = Orchestrator(
        agents={"interviewer": interviewer, "scorer": scorer},
        store_factory=lambda sid: stores.setdefault(
            sid, InMemoryEventStore(visibility=candidate_never_sees_scores)
        ),
        director=InterviewDirector(),
        deliverer=deliverer,
    )
    return engine, stores, interviewer, deliverer


def _answer():
    return Stimulus(
        kind="candidate_answer",
        stream_id="interview-42",
        content="i'd use a queue because ordering matters under load",
        inbound_author="candidate",
        platform="web",
        scope="interview",
        target=DeliveryTarget(platform="web", target_id="socket-42"),
    )


def test_mixed_visible_and_silent_lines_share_one_activation():
    scorer = FakeScorer()
    engine, stores, interviewer, deliverer = _wire(scorer)

    result = run(engine.handle(_answer()))

    # lossless per-line truth: the interviewer delivered, the scorer is a
    # CORRECT quiet line, not a broken one (§11.1)
    assert result.status == "completed"
    assert [(l.speaker, l.status) for l in result.lines] == [
        ("interviewer", "delivered"),
        ("scorer", "silent"),
    ]
    assert [a.name for a in result.lines[1].actions] == ["record_score"]

    # the score is real evidence, recorded on the PRIVATE panel channel
    events = run(stores["interview-42"].read(EventQuery()))
    assert [(e.kind, e.author) for e in events] == [
        ("message", "candidate"),
        ("message", "interviewer"),
        ("action", "scorer"),
    ]
    score_event = events[2]
    assert score_event.audience == "panel"
    assert score_event.metadata["input"]["dimension"] == "reasoning"

    # the candidate-facing view NEVER includes scorer evidence
    candidate_view = run(stores["interview-42"].read(EventQuery(viewer="candidate")))
    assert [e.author for e in candidate_view] == ["candidate", "interviewer"]

    # the scorer was briefed AFTER the interviewer's reply became history -
    # it evaluates the full exchange, not half of it
    assert [e.author for e in scorer.briefings[0].events] == [
        "candidate",
        "interviewer",
    ]
    # and the interviewer's own briefing ended on the candidate's answer,
    # carrying the director's cue
    assert interviewer.briefings[0].events[-1].author == "candidate"
    assert "probe deeper" in interviewer.briefings[0].cue


def test_silent_line_text_is_an_error_and_never_leaks():
    """a scorer that unexpectedly writes prose must not have it delivered or
    recorded - the guard that keeps private evaluation private."""
    scorer = FakeScorer(leak_text="score: 3/10, seemed nervous")
    engine, stores, _, deliverer = _wire(scorer)

    result = run(engine.handle(_answer()))

    line = result.lines[1]
    assert line.status == "errored"
    assert line.error.kind == "unexpected_text_from_silent_line"
    # the score ACTION still recorded (it's real evidence)...
    assert [a.name for a in line.actions] == ["record_score"]
    # ...but the prose reached neither the platform nor the event log
    assert len(deliverer.requests) == 1  # only the interviewer's send
    all_content = " ".join(
        e.content for e in run(stores["interview-42"].read(EventQuery()))
    )
    assert "nervous" not in all_content


def test_unknown_stimulus_kind_is_an_explicit_noop():
    engine, stores, interviewer, _ = _wire(FakeScorer())
    result = run(
        engine.handle(
            Stimulus(
                kind="hallway_smalltalk",
                stream_id="interview-42",
                content="nice weather",
                record_inbound=False,
            )
        )
    )
    assert result.status == "noop"
    assert "no rule" in result.status_reason
    assert interviewer.briefings == []
