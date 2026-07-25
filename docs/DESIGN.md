# the dainframe

*the design doc for the dainframe: a stimulus-driven multi-agent orchestration
framework, extracted from chordial-mvp's orchestration layer. seeded in
chordial's `docs/DAINFRAME_DESIGN.md` on 2026-07-25; moved here the same day
when this repo was created.*

**status:** concept, revised 2026-07-25 — ambient scheduling (**the pulse**,
§6) promoted from "not in v0.1" to a defining feature.
**decision already made:** own repo, up-front complication accepted — the goal
is to unblock other projects and to find out *early* where the shared API is
too chordial-shaped. chordial itself is functionally done; everything else on
its roadmap is nice-to-have.

---

## 1. what the dainframe is

the stimulus-driven multi-agent activation engine that chordial's v2/v3
refactors already carved out, promoted to a first-class framework:

> something happens (**Stimulus**) → a **Director** casts who acts (**Script**)
> → each speaker gets a **Briefing** → the **Agent** acts (maybe with tools,
> maybe silently) → the engine **records** what happened into a shared,
> visibility-scoped **event log** → confirmed output goes back as a
> **Deliverable**.

and its second half, the part that makes dainframe apps *ambient* rather than
merely reactive:

> time passes (**the Pulse**) → abstract **Rhythms** (fixed, clock-based, or
> dynamically decided) come due → cheap **Gates** decide whether firing is
> welcome → a due rhythm is translated into a **Stimulus** and handed to the
> same engine as any user message.

reactive and ambient activations converge on ONE engine, one event log, one
recording discipline. that convergence — not the scheduler loop itself — is
the defining feature: an ambient check-in and a user reply are the same kind
of moment, distinguished only by what stimulated them.

three consumers define the API's pressure envelope:

1. **chordial** — companion ensemble. persona agents + silent curator, group/dm
   privacy scoping, ambient check-ins with backoff, multiple chat platforms.
2. **the creativity/education harness** — deliberately *limits* pure
   generation: tools and utility models do the grounded work, persona models do
   the framing. needs per-agent (and per-activation) model choice, a tool
   registry that constrains what an agent may do, silent evaluator agents,
   ambient pacing (session rhythm, spaced repetition).
3. **the interview platform** — an orchestrator runs the interview; separate
   agents score the candidate and evaluate thought process. needs a
   director-as-state-machine, silent scorer agents whose notes the candidate's
   transcript never shows, different models per role, and an ambient nudge
   when the candidate goes quiet.

the striking thing: chordial already proved every one of those shapes.
scorers and evaluators are curator-shaped agents (`text=None`, actions only).
the interview director is `_direct` with a state machine instead of routing
rules. "different models per agent" is `HelperAgent`'s per-agent config.
"candidate can't see scorer notes" is the privacy-scoped `visible_to` window.
the silence nudge is a scheduled tick with a different rhythm. the dainframe
is not speculative — it's generalizing machinery with three known consumers.

## 2. what's IN the library

roughly the bottom ~2,000 lines of chordial, de-chordial-ized:

```
dainframe/
  core/
    types.py         Stimulus, ScriptLine, Script, Deliverable, Event
    agent.py         Agent protocol, Briefing, AgentOutcome
    director.py      Director protocol + SingleSpeakerDirector default
    events.py        EventStore protocol + InMemoryEventStore reference impl
    hooks.py         TurnHooks + ContextProvider protocols (all optional)
    engine.py        Orchestrator: the recording/delivery state machine
  pulse/
    rhythm.py        Rhythm specs: Interval / Calendar / Dynamic (+ Decider protocol)
    gates.py         Gate protocol + shipped gates: BackoffGate, QuietHoursGate,
                     AllOf combinator
    pulse.py         the Pulse loop: due rhythms -> gates -> StimulusFactory
                     -> engine.handle -> deferred-delivery finalization
    state.py         PulseStore protocol + InMemoryPulseStore
  providers/
    types.py         AIRequest/AIResponse/ChatTurn/ToolCall/ToolDef/Usage,
                     typed errors (ProviderError/RateLimited/Unavailable)
    base.py          BaseAIProvider
    anthropic.py     AnthropicProvider (all config via constructor — no Config import)
    openai.py        OpenAIProvider
  tools/
    registry.py      Tool (terminal / record_event), ToolRegistry, view(),
                     acting_as context
  loop/
    agent_loop.py    the tool-call loop (chordial's AgentService): iteration cap,
                     concurrency semaphore, terminal-tool short-circuit
    usage.py         UsageSink protocol + NullUsageSink
```

source mapping from chordial:

| dainframe module | from chordial | de-chordial-ization needed |
|---|---|---|
| `providers/*` | `src/providers/ai/*` | anthropic provider stops importing `config.Config`; model ids, retry counts, cache flags become constructor params |
| `tools/registry.py` | `src/services/tools/base.py` + `context.py` | none — already generic |
| `loop/agent_loop.py` | `src/services/agent_service.py` | `UsageRecorder` becomes a `UsageSink` protocol; chordial's DB-backed recorder implements it app-side |
| `core/types.py` | `orchestration_types.py` + `Event` from `event_log.py` | see §5 — the big one |
| `core/agent.py` | `src/agents/base.py` | `Briefing.events` typed against library `Event`; otherwise unchanged |
| `core/engine.py` | `orchestrator.py` | chordial specifics move out through the seams in §4 |
| `pulse/*` | `scheduler_service.py` + `proactivity_gate.py` | see §6 — the loop, the backoff arithmetic, and the deferred-delivery dance generalize; chordial's onboarding/target-resolution/first-contact rules stay app-side |

## 3. what stays in chordial (and each app)

- **prompt construction** — `PromptService`, the cache-zone layout, persona
  cards. every app's prompts are its soul; the library never renders a prompt.
  (a later optional `dainframe.promptkit` could offer cache-breakpoint
  primitives, but NOT in v0.1 — the byte-exact-prefix discipline is easy to
  ruin by abstracting too early.)
- **the event store implementation** — chordial's SQLAlchemy `EventLog` stays,
  adapted to the `EventStore` protocol (it already has all the methods).
- **tools implementations, personas, platform adapters, onboarding, workspace,
  reconciler** — all product.
- **the director's rules** — `_direct`/`_group_lines`/`_finalize`, including
  the `"chordial"` fallback speaker, become a chordial-side `ChordialDirector`.
- **the pulse's app half** — which streams are ambient (`PulseSource`), how a
  firing becomes a deliverable stimulus (`StimulusFactory`: platform fallback,
  onboarding gate, first-contact rule), and any AI decider's actual prompt.

## 4. the engine seams (protocols the app implements)

these are the six places chordial is currently baked into the engine, each
replaced by a protocol with a trivial default:

### 4.1 EventStore

```python
class EventStore(Protocol):
    def append_message(self, author_type, author, content, *, message_type,
                       platform, scope, audience) -> Event: ...
    def append_action(self, author, tool, tool_input, result, *,
                      platform, scope, audience) -> Event: ...
    def append_note(self, content, *, platform, metadata) -> Event: ...
    def recent(self, limit: int, visible_to: str | None = None) -> list[Event]: ...
    def last_user_message(self) -> Event | None: ...
    def last_message(self) -> Event | None: ...
```

- keyed by an opaque **stream id** the app chooses: chordial passes
  `user_uuid`, the interview platform passes an interview-session id, the
  harness passes whatever a "session" is there. the library never assumes
  stream == user.
- the `Event` dataclass (author_type/author/kind/content/platform/scope/
  audience/metadata/timestamp/db_id) moves INTO the library — `Briefing`
  depends on it, so it's shared vocabulary. (`audience` is today's
  `with_helper`, renamed: "which private channel this belongs to.")
- **visibility is an app policy, not library logic.** the store receives an
  optional `VisibilityPolicy: (event, agent_name) -> bool` at construction;
  default is everything-visible. chordial's rule (group + own dms, never a
  sibling's) and the interview rule (candidate stream never includes scorer
  actions) are both one small function.
- library ships `InMemoryEventStore` (tests, quick starts) and maybe a
  minimal sqlite one later. chordial's `format_action_line` (freezing a tool
  call into a promptable one-liner at write time) is a *pattern* the library
  documents and defaults, with the formatter overridable per-app.

### 4.2 Director

```python
class Director(Protocol):
    async def direct(self, stimulus: Stimulus, store: EventStore) -> Script: ...
```

- chordial: today's rules (+ the phase-3 AI director when it comes — the
  utility-model call fits behind the same protocol).
- interview: a state machine over interview stages; every candidate answer
  casts `[interviewer(cue="probe deeper on X"), scorer(silent), evaluator(silent)]`.
- harness: probably near-static, but the cue/style stage directions carry the
  "constrain the generative register" instruction per activation.
- `ScriptLine` grows optional **`model` and `effort` hints** — this is where
  chordial's parked dynamic-model-routing lands, and it's load-bearing for the
  harness ("different models" per activation) and the interview platform
  (cheap scorer, expensive evaluator). `HelperAgent` already takes per-agent
  config; a line-level hint just overrides it for one activation.
- line caps, fallback speakers, dedup: all the director's own business. the
  engine's only guardrail is "skip speakers that aren't registered agents."

### 4.3 Deliverer

```python
Deliverer = Callable[..., Awaitable[bool]]   # (platform, target_id, text, *, speaker) -> bool
```

unchanged from chordial's `deliver` hook — it was already generic. the
confirmed-send-before-recording invariant (a reply enters shared history only
after the platform says yes) is engine behavior and one of the most valuable
things the library exports.

### 4.4 ContextProvider (briefing enrichment)

```python
class ContextProvider(Protocol):
    async def enrich(self, stimulus: Stimulus, line: ScriptLine) -> BriefingContext: ...
    # BriefingContext: ambient_context, briefing_kind override, extras dict
```

everything chordial-specific in today's `_brief` moves here: the user-profile
lookup, the agenda digest, the "introduction gets no ambient" rule, the
stimulus-kind → briefing-kind mapping. the engine itself only fills the
mechanical fields: the visible event window, cue, style, scope.

### 4.5 TurnHooks

```python
class TurnHooks(Protocol):
    async def after_inbound_recorded(self, stimulus, store, prev_user_event) -> None: ...
    async def after_turn(self, stimulus, store, deliverable) -> None: ...
```

- chordial's platform-switch courtesy → `after_inbound_recorded` (it's a
  product nicety, not engine logic — the 💜 goes home).
- chordial's completion reconciler → `after_turn` (already fully guarded;
  the engine keeps the guarantee that a hook failure never affects the reply).
- the legacy compression path → chordial-side, or deleted (it's off by
  default and pre-event-log vintage).

### 4.6 UsageSink

```python
class UsageSink(Protocol):
    async def record(self, *, stream_id, agent, model, usage, trace) -> None: ...
```

the loop reports; the app persists. chordial's `UsageRecorder` implements it.

## 5. de-chordial-izing Stimulus (the key API change)

today the engine branches on chordial's stimulus taxonomy
(`user_message`/`scheduled_tick`/`introduction`/`curation_due`). the library
must not know that vocabulary. the fix: **`kind` becomes an app-defined string
the *director* interprets; the *engine* reads explicit policy flags.**

```python
@dataclass
class Stimulus:
    kind: str                        # app vocabulary; only the app's Director branches on it
    stream_id: str                   # event-store key (chordial: user_uuid)
    content: str | None = None       # inbound text, if any
    platform: str | None = None      # provenance tag for recorded events

    # engine policy (what today's kind-branches actually decide)
    record_inbound: bool = True      # write content to the log before acting
    expects_reply: bool = True       # empty text from a speaker => errored (the
                                     #  silent-failure guard, kept from chordial)
    delivery: str = "direct"         # 'direct' | 'deferred' | 'none'
                                     #  deferred = generate now, caller confirms
                                     #  delivery later via record_delivered_message
                                     #  (the pulse's pattern, generalized)

    # routing/recording context
    scope: str = "dm"                # app-meaningful channel tag ('dm'/'group'/...)
    audience: str | None = None      # which private channel (today's dm_helper)
    addressed: list[str] = field(default_factory=list)   # today's `mentioned`
    target: DeliveryTarget | None = None  # (platform, target_id) for direct delivery
    group_target: DeliveryTarget | None = None

    reason: str | None = None        # the "why now" for ambient stimuli — set by a
                                     #  pulse decider/gate, woven into Briefing.reason
    extras: dict = field(default_factory=dict)  # user_name, timezone, intro_helper —
                                                # anything only the app's Director /
                                                # ContextProvider understands
```

chordial's mapping is mechanical: `scheduled_tick` → `delivery='deferred'`,
`curation_due` → `record_inbound=False, expects_reply=False, delivery='none'`,
introductions carry `intro_helper` in extras. the interview platform never
touches chordial's vocabulary; it invents `candidate_answer`,
`stage_transition`, whatever.

engine algorithm (v0.1), now app-agnostic:

1. if `record_inbound` and content: append inbound message → fire
   `after_inbound_recorded`
2. `script = await director.direct(stimulus, store)`
3. per line, in order (later speakers are briefed after earlier lines are
   recorded — genuine reaction, kept): build briefing (visible window +
   ContextProvider) → `agent.act` → record surviving actions (via the
   registry's `record_event` policy) → deliver per `delivery` mode → record
   the message only on confirmed delivery
4. fire `after_turn`
5. return Deliverable

invariants the engine keeps, verbatim from chordial (these ARE the product of
two versions of hard-won lessons — they're why the library is worth extracting):

- refusals/errors record nothing after the inbound message
- action events persist even when the reply can't be delivered (the mutations
  already happened; the trail must stay true)
- a reply becomes shared history only after confirmed delivery
- an empty reply on a turn that owes one is an error, never a silent shrug
- provider errors are typed and raised, never apology strings

## 6. the pulse — ambient as a first-class feature

**the design question that created this section:** can a Stimulus be "a
schedule, or a dynamic schedule"? answer: no — and that's the design. a
`Stimulus` stays the atomic "something happened *now*". what lives in the
library is the thing that *produces* stimuli over time: the **Pulse**, which
takes abstract descriptions of *when* (**Rhythms**) and translates each firing
into an ordinary `Stimulus` handed to the same engine as any user message.
"when" and "what happens" stay orthogonal, so every engine invariant —
confirmed delivery, gate-before-generate, refusals record nothing — applies to
ambient activations for free.

this section replaces the original design's "scheduling stays app-side"
position. the reversal is deliberate: the ambient nature is chordial's most
distinctive property, all three consumers want it, and chordial's scheduler +
proactivity gate turn out to be mostly generic arithmetic wearing a
chordial costume.

### 6.1 Rhythm — the abstract schedule description

```python
@dataclass
class Interval:
    """recency-anchored cadence: fire when `every` has passed since the anchor."""
    every: timedelta
    anchor: str = "last_message"     # 'last_message' | 'last_fire'
    jitter: timedelta | None = None

@dataclass
class Calendar:
    """clock-anchored: fire at local times/cron in the stream's timezone
    (tz resolved per-stream via an app callable). the morning-brief shape."""
    cron: str
    tz_of: Callable[[str], Awaitable[str]] | None = None   # stream_id -> tz

@dataclass
class Dynamic:
    """decided cadence: a Decider is asked 'fire now? and when should I next
    even check?'. THE seam for an AI check-in gate — a utility model reading
    recent events and answering 'reach out now, about X' or 'ask me again
    this evening'."""
    decider: Decider
    max_sleep: timedelta = timedelta(hours=6)   # never trust a decider forever

class Decider(Protocol):
    async def decide(self, stream_id: str, store: EventStore,
                     now: datetime) -> Decision: ...
    # Decision: fire (bool), next_check (datetime), reason/cue (str | None)
```

a stream can carry several rhythms at once, each tagged with an app-chosen
`kind` that flows into the stimulus — chordial: a `checkin` Interval + a
`curation` Interval; the harness: a `session_pace` Interval + a `review_due`
Dynamic (spaced repetition IS a decider); the interview: a `silence_nudge`
Interval anchored on last_message.

`Dynamic` is chordial's designed-but-never-built "haiku check-in gate"
(replace the fixed interval with an AI decide-when-to-reach-out call),
realized as a framework seam. the decider's `reason` rides
`Stimulus.reason` → `Briefing.reason` — a field chordial's Briefing already
reserved for exactly this.

### 6.2 the pulse's seams

```python
class PulseSource(Protocol):
    """which streams are ambient, and what rhythms each carries. re-read every
    cycle, so activation/deactivation needs no registration dance."""
    async def streams(self) -> list[tuple[str, list[TaggedRhythm]]]: ...

class Gate(Protocol):
    """cheap pre-generation guard. a denied tick costs a db read and ZERO
    tokens (chordial's invariant: never generate a proactive message just to
    throw it away)."""
    async def check(self, stream_id: str, rhythm: TaggedRhythm,
                    store: EventStore, now: datetime) -> GateDecision: ...
    # GateDecision: allowed (bool), reason (str)

class StimulusFactory(Protocol):
    """translate a cleared firing into a Stimulus — or None to skip (e.g. no
    deliverable target: resolve WHERE before spending tokens on WHAT)."""
    async def build(self, stream_id: str, rhythm: TaggedRhythm,
                    decision: GateDecision) -> Stimulus | None: ...

class PulseStore(Protocol):
    """durable pulse state per (stream, rhythm tag): last_fired, next_check,
    decider scratch. in-memory default; an app that must survive restarts
    without double-firing persists it (calendar/dynamic rhythms care most —
    interval rhythms self-heal because their anchor lives in the event log)."""
```

### 6.3 the loop

one cycle, per stream, per due rhythm:

1. **due?** — interval/calendar arithmetic, or the Dynamic decider's stored
   `next_check` (the decider is only *invoked* when its own next_check
   arrives — a lazy decider costs nothing between checks)
2. **gates** — run the app's gate stack; denial is logged with its reason and
   costs no tokens
3. **build** — `StimulusFactory` resolves the concrete stimulus (target,
   platform, extras); `None` skips quietly
4. **act** — `engine.handle(stimulus)`; for `delivery='deferred'`, the pulse
   itself runs the finalization dance: deliver via the Deliverer, and only on
   a confirmed send call `engine.record_delivered_message`. failed sends stay
   out of the event log — they neither pollute future context nor consume the
   stream's outreach allowance (chordial invariant, now library behavior)
5. **mark** — update PulseStore

the pulse is started/stopped by the app (`await pulse.run()` /
`pulse.stop()`) — the library owns the loop's correctness, the app owns its
lifecycle.

### 6.4 shipped gates

the surprise of reading chordial's `ProactivityGate` with library eyes: it's
pure arithmetic over event-log semantics the library already owns
(`kind='message'`, `author_type`, `message_type='scheduled'`). so it ships
with the dainframe:

- **`BackoffGate(crew_cap, per_author_cap, base_interval)`** — chordial's
  three stacked rules, verbatim: N unanswered proactive messages from anyone
  silences everyone; M from one author silences them; each unanswered message
  doubles the required quiet period. any user message anywhere resets all
  three. this is the "don't nag" ethic of the ambient design, exported.
- **`QuietHoursGate(start, end, tz_of)`** — no proactive send in a stream's
  local night; applies to every rhythm, including backoff chains ("being
  ignored is not license to nag at 3am").
- **`AllOf(gates)`** — first denial wins, reasons preserved.

app-flavored guards (chordial's onboarding check, its platform-liveness rule)
are just more `Gate` implementations composed into the same stack.

### 6.5 what this dissolves in chordial

`SchedulerService` stops existing as a bespoke loop. its parts land as:
`get_scheduled_users` → `PulseSource`; onboarding + quiet hours + proactivity
gate → a gate stack (two of the three now library-shipped);
`resolve_delivery_identity` + the first-contact rule → `StimulusFactory`; the
deferred-delivery finalization → the pulse loop itself; the piggybacked
curation pass → a second rhythm (`curation` Interval, `delivery='none'`) —
no longer a special case, just another beat.

## 7. what each consumer looks like (wiring sketches)

**chordial** (after extraction — `main.py` scale):

```python
engine = Orchestrator(
    agents={"chordial": ..., "aria": ..., "curator": ...},   # unchanged agents
    store_factory=lambda sid: SqlEventLog(sid, visibility=chordial_visibility),
    director=ChordialDirector(helper_state_manager, fallback="chordial"),
    context=ChordialContext(user_manager, agenda_service),
    hooks=ChordialHooks(reconciler, switch_notifier),
    deliver=message_router.deliver_as,
    usage=UsageRecorder(),
)
pulse = Pulse(
    engine,
    source=ChordialPulseSource(user_manager),      # scheduled users -> rhythms
    gates=AllOf([OnboardingGate(user_manager),
                 QuietHoursGate(21, 8, tz_of=user_manager.get_user_timezone),
                 BackoffGate(crew_cap=4, per_author_cap=2, base_interval=hours(3))]),
    factory=ChordialStimulusFactory(user_manager),  # active_platform + fallback
)
```

**interview platform**:

```python
engine = Orchestrator(
    agents={"interviewer": persona_agent, "scorer": silent_agent,
            "rubric": silent_agent},
    store_factory=lambda sid: PgEventStore(sid, visibility=candidate_never_sees_scores),
    director=InterviewDirector(stage_machine),      # casts interviewer + silent scorers
    context=InterviewContext(candidate_profile, role_rubric),
    deliver=websocket_send,
)
pulse = Pulse(engine,
    source=ActiveInterviews(),   # each session: silence_nudge Interval(3 min,
    gates=AllOf([]),             #   anchor='last_message'), stage_timeout Calendar
    factory=NudgeFactory())
# scorers return AgentOutcome(text=None, actions=[score_write, ...]) — the
# curator shape. their action events are the interview's evaluation record.
```

**creativity/education harness**: single or few agents, a near-static
director whose ScriptLines carry model/effort hints and constraining cues; the
"limit pure generation" contract is enforced by giving the persona agent a
narrow `ToolRegistry.view()` and pushing grounded work into terminal tools
backed by utility models. ambient pacing via a `session_pace` Interval and a
`review_due` Dynamic whose decider implements spaced repetition. (this also
inherits chordial's v4 lesson for free: groundedness has to be in the framing,
and the dainframe makes the framing a first-class, per-activation cue.)

## 8. repo & workflow mechanics

- **repo:** `the-dainframe` (github.com/DainDeer/the-dainframe), poetry,
  python 3.13, same test-first culture. pytest from day one — port the generic
  halves of chordial's `test_orchestrator`, `test_agent_loop`,
  `test_anthropic_kwargs`, `test_event_log`, `test_proactivity_gate`, and
  `test_delivery_eligibility` suites.
- **dependency during development:**
  `dainframe = {path = "../the-dainframe", develop = true}` in chordial's
  pyproject — instant cross-repo iteration, no publish step. once stable, pin
  a git tag (`dainframe = {git = ..., tag = "v0.1.0"}`). PyPI only if it ever
  wants a public life.
- **versioning:** 0.x with honest breakage until the second consumer exists.
  the whole point of the early split is to *find* the breakage; a CHANGELOG
  and loud commit messages beat a stability promise.
- **the two-PR tax is real and accepted:** every seam change during extraction
  touches both repos. the path-dependency keeps it cheap locally; chordial's
  test suite is the integration harness that keeps it honest.

## 9. migration plan (bottom-up, chordial green at every step)

each phase: copy into dainframe + de-chordial-ize → point chordial's imports
at the library → delete the chordial copy → full chordial test suite passes.

1. **phase 1 — leaves:** `providers/ai/*` (constructor-param config),
   `tools/base.py` + `context.py`, `agent_service.py` (+ `UsageSink`).
   lowest risk, immediately useful: the other two projects can start building
   on providers + tool loop before the engine even lands.
2. **phase 2 — vocabulary:** `Event`, generalized `Stimulus`/`Script`/
   `Deliverable`, `Agent`/`Briefing`/`AgentOutcome`, `EventStore` protocol.
   chordial's `EventLog` gains the protocol's method names (near-rename).
3. **phase 3 — the engine:** `core/engine.py` lands; chordial's
   `orchestrator.py` shrinks to `ChordialDirector` + `ChordialContext` +
   `ChordialHooks` + wiring. the switch notice, reconciler call, compression,
   and profile lookup all leave the engine through their seams.
4. **phase 4 — the pulse:** `pulse/*` lands (rhythms, gates, loop, state);
   chordial's `SchedulerService` dissolves per §6.5 and
   `proactivity_gate.py` becomes a thin re-export of the library's
   `BackoffGate` (or is deleted outright). curation becomes a rhythm.
5. **phase 5 — proof:** chordial fully green, **prompt-byte tests pass
   unchanged** (the extraction must not alter a single prompt byte — warm
   caches survive, same as every migration before it). tag `v0.1.0`. start
   project #2 against it.

**biggest design risk, named:** the `EventStore` boundary. chordial's event
log carries a lot of hard-won semantics (action freezing, note kinds that
never render, platform provenance vs. filtering, `active_platform`). the
library should take the *invariants* (three kinds, author attribution,
visibility hook, provenance-not-filter) and leave the *renderings* (action
line format, note handling in prompts) to each app. when in doubt, keep it
app-side — a library can absorb a proven pattern later far more easily than it
can disown a wrong one.

**second risk, new with the pulse:** clock code. rhythms + restarts + tz
arithmetic breed subtle double-fire/never-fire bugs. mitigations: interval
rhythms anchor on the event log (self-healing, no stored clock); everything
takes `now` as a parameter (deterministic tests); `Dynamic.max_sleep` bounds
decider trust; the PulseStore contract is written down before phase 4 starts.

## 10. explicitly NOT in v0.1

- promptkit / cache-zone helpers (pattern documented, code stays app-side)
- any AI decider *implementation* — the `Dynamic`/`Decider` seam is library;
  an actual utility-model decider needs prompts, and prompts are app soul.
  chordial's haiku check-in gate, when built, is chordial code plugged into
  the seam (and the proof the seam is right).
- any storage implementation beyond in-memory (EventStore and PulseStore both)
- multi-stream / multi-user management (streams are opaque ids; user identity
  is app business)
- the AI director (phase-3 chordial feature — same story as the AI decider:
  the `Director` seam is the library's part)
