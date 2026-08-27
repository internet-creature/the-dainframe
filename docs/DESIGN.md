# the dainframe

*the design doc for the dainframe: a stimulus-driven multi-agent orchestration
framework, extracted from chordial-mvp's orchestration layer. seeded in
chordial's `docs/DAINFRAME_DESIGN.md` on 2026-07-25; moved here the same day
when this repo was created.*

**status:** concept, revision 3, 2026-07-25 — ambient scheduling (**the
pulse**, §6) remains a defining feature; the execution contract is revised
around per-line response/delivery policy, structured activation results,
generic tool context, stream serialization, and durable pulse claims.
**decision already made:** own repo, up-front complication accepted — the goal
is to unblock other projects and to find out *early* where the shared API is
too chordial-shaped. chordial itself is functionally done; everything else on
its roadmap is nice-to-have.

revision 3 deliberately preserves rejected earlier shapes in §11. the old
ideas were useful stepping stones; recording why they were superseded is part
of the design, not editorial debris.

---

## 1. what the dainframe is

the stimulus-driven multi-agent activation engine that chordial's v2/v3
refactors already carved out, promoted to a first-class framework:

> something happens (**Stimulus**) → a **Director** casts who acts (**Script**)
> → each speaker gets a **Briefing** → the **Agent** acts (maybe with tools,
> maybe silently) → the engine **records** what happened into a shared,
> visibility-scoped **event log** → confirmed output goes back as a
> per-line result inside an **ActivationResult**.

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

the striking thing: chordial proved the constituent shapes, though not yet
every composition. the curator proves silent agents; ordinary helper turns
prove action-carrying outcomes; the separate utility provider proves
role-specific model selection; `_direct` is the place an interview state
machine fits; the privacy-scoped window is the mechanism scorer notes need;
and a silence nudge is a scheduled tick with a different anchor. the mixed
`[visible interviewer, silent scorers]` activation and line-level model
overrides remain hypotheses until the phase-0/phase-4 consumer spikes. the
dainframe generalizes proven machinery without pretending the shared API is
already proven.

## 2. what's IN the library

an extraction beginning with Chordial's orchestration/provider/tool-loop
machinery, plus the new contracts required to make it a framework:

```
dainframe/
  core/
    types.py         Stimulus, ScriptLine, Script, ActivationResult,
                     LineResult, PendingDelivery, Event
    agent.py         Agent protocol, Briefing, AgentOutcome
    director.py      Director protocol + SingleSpeakerDirector default
    events.py        EventStore protocol + InMemoryEventStore reference impl
    delivery.py      Deliverer, DeliveryLedger, in-memory ledger
    hooks.py         TurnHooks + ContextProvider protocols (all optional)
    engine.py        Orchestrator: the recording/delivery state machine
    coordination.py  StreamCoordinator + in-process keyed-lock default
  pulse/
    rhythm.py        Rhythm specs: Interval / Calendar / Dynamic (+ Decider protocol)
    gates.py         Gate protocol + shipped gates: BackoffGate, QuietHoursGate,
                     AllOf combinator
    pulse.py         the Pulse loop: claim -> decide -> plan -> gates
                     -> engine.handle -> complete
    state.py         PulseStore protocol + InMemoryPulseStore
  providers/
    types.py         AIRequest/AIResponse/ChatTurn/ToolCall/ToolDef/Usage,
                     typed errors (ProviderError/RateLimited/Unavailable)
    base.py          BaseAIProvider
    anthropic.py     AnthropicProvider (all config via constructor — no Config import)
    openai.py        OpenAIProvider
  tools/
    registry.py      Tool (terminal / record_event), ToolRegistry, view()
    context.py       ToolContext: stream/activation/actor/metadata
  loop/
    agent_loop.py    the tool-call loop (chordial's AgentService): iteration cap,
                     partial-action failures, terminal-tool short-circuit
    usage.py         UsageSink protocol + NullUsageSink
```

source mapping from chordial:

| dainframe module | from chordial | de-chordial-ization needed |
|---|---|---|
| `providers/*` | `src/providers/ai/*` | providers stop importing `config.Config`; model ids, concurrency limiter, thinking/cache flags become constructor params; model selection is resolved once per agent run |
| `tools/*` | `src/services/tools/base.py` + `context.py` | replace `user_uuid`, `acting_helper`, and the `"chordial"` default with `ToolContext`; copy registry/view semantics, not the product vocabulary |
| `loop/agent_loop.py` | `src/services/agent_service.py` | `UsageRecorder` becomes a `UsageSink`; partial actions survive later provider failure; the loop receives `ToolContext` and a resolved provider |
| `core/types.py` | `orchestration_types.py` + `Event` from `event_log.py` | see §5 — the big one |
| `core/agent.py` | `src/agents/base.py` | `Briefing.events` typed against library `Event`; add execution hints and make silent-vs-missing output explicit through `ScriptLine.response` |
| `core/engine.py` | `orchestrator.py` | chordial specifics move out through the seams in §4 |
| `pulse/*` | `scheduler_service.py` + `proactivity_gate.py` | see §6 — cadence, claims, backoff arithmetic, planning and outcome state generalize; chordial's onboarding/target-resolution/first-contact rules stay app-side |

## 3. what stays in chordial (and each app)

- **prompt construction** — `PromptService`, the cache-zone layout, persona
  cards. every app's prompts are its soul; the library never renders a prompt.
  (a later optional `dainframe.promptkit` could offer cache-breakpoint
  primitives, but NOT in v0.1 — the byte-exact-prefix discipline is easy to
  ruin by abstracting too early.)
- **the event store implementation** — chordial's SQLAlchemy `EventLog` stays
  app-side behind an async EventStore adapter. this is a real adapter, not a
  near-rename: EventQuery, visibility-before-windowing, opaque ids, and pending
  confirmation semantics must be implemented and contract-tested.
- **tools implementations, personas, platform adapters, onboarding, workspace,
  reconciler** — all product.
- **the director's rules** — `_direct`/`_group_lines`/`_finalize`, including
  the `"chordial"` fallback speaker, become a chordial-side `ChordialDirector`.
- **the pulse's app half** — which streams are ambient (`PulseSource`), how a
  firing is cheaply planned and becomes a stimulus (`StimulusFactory`:
  candidate speaker, platform fallback, first-contact rule), onboarding gates,
  and any AI decider's actual prompt.

## 4. the engine seams (protocols the app implements)

these are the places chordial is currently baked into the engine, each
replaced by a protocol with a trivial default. protocols consumed by the
async engine are async even when chordial's first adapter delegates to
synchronous SQLAlchemy. the framework must not force a future async-postgres
consumer to block its event loop because its source application happened to.

### 4.1 EventStore

```python
class EventStore(Protocol):
    async def append(self, event: NewEvent) -> Event: ...
    async def read(self, query: EventQuery) -> list[Event]: ...
    async def latest(self, query: EventQuery) -> Event | None: ...

@dataclass(frozen=True)
class EventQuery:
    kinds: frozenset[str] | None = None
    author_types: frozenset[str] | None = None
    authors: frozenset[str] | None = None
    message_types: frozenset[str] | None = None
    viewer: str | None = None
    message_limit: int | None = None
```

- keyed by an opaque **stream id** the app chooses: chordial passes
  `user_uuid`, the interview platform passes an interview-session id, the
  harness passes whatever a "session" is there. the library never assumes
  stream == user.
- the `Event` dataclass (author_type/author/kind/content/platform/scope/
  audience/metadata/timestamp/event_id) moves INTO the library — `Briefing`
  depends on it, so it's shared vocabulary. (`audience` is today's
  `with_helper`, renamed: "which private channel this belongs to.") `event_id`
  is opaque/hashable — the shared type never assumes an integer database id.
- **visibility is an app policy, not library logic.** the store receives an
  optional `VisibilityPolicy: (event, agent_name) -> bool` at construction;
  default is everything-visible. chordial's rule (group + own dms, never a
  sibling's) and the interview rule (candidate stream never includes scorer
  actions) are both one small function. filtering happens BEFORE windowing, so
  `message_limit=30` means thirty messages the viewer can actually see.
- `message_limit` counts `kind="message"` only; intervening action/note events
  ride inside the selected id-ordered window. this is not an implementation
  accident — it is the exact semantic chordial's prompt history depends on.
- Directors, gates, and deciders receive an `EventReader` projection
  (`read`/`latest` only). only the engine owns append access. this keeps "one
  recording discipline" enforceable instead of merely conventional.
- notes receive the same scope/audience/provenance fields as every other
  event. an app may choose never to render them, but privacy is not waived
  because `kind="note"`.
- library ships `InMemoryEventStore` (tests, quick starts) and maybe a
  minimal sqlite one later. `EventStoreContract` is a reusable conformance
  suite that every implementation runs: ordering, visibility-before-windowing,
  message-count windows, immutable ids, and latest-query behavior.
- chordial's `format_action_line` (freezing a tool call into a promptable
  one-liner at write time) stays a chordial event-factory policy. the library
  stores the supplied immutable content plus structured metadata; it does not
  decide how an action should read in an app's future prompt.

### 4.2 Director

```python
class Director(Protocol):
    async def direct(self, stimulus: Stimulus, events: EventReader) -> Script: ...
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
  (cheap scorer, expensive evaluator). these live in a structured
  `ExecutionHints(provider, model, effort, max_tokens, extras)` carried by the
  Briefing. an agent declares whether it honors them; the engine never swaps a
  provider halfway through a tool loop.
- response and delivery policy also live per line, not on the stimulus:
  `response = required | optional | silent`; `delivery = direct | pending |
  none`. this is what lets one interview activation cast a visible
  interviewer plus silent scorers without treating the scorers as broken.
- line caps, fallback speakers, and dedup remain the director's business.
  unknown speakers are retained as structured failed `LineResult`s, not
  silently skipped — a typo in the only speaker must not resemble a successful
  quiet activation.

### 4.3 Deliverer

```python
class Deliverer(Protocol):
    async def deliver(self, request: DeliveryRequest) -> DeliveryReceipt | None: ...

class DeliveryLedger(Protocol):
    async def stage(self, pending: NewPendingDelivery) -> PendingDelivery: ...
    async def get(self, pending_id: str) -> PendingDelivery | None: ...
    async def confirm(self, pending_id: str, receipt: DeliveryReceipt,
                      events: EventStore) -> Event: ...
```

`get` is a phase-0 discovery: the engine must learn which stream a pending id
belongs to BEFORE it can hold the right StreamCoordinator lock around
`confirm` (§5.5). without it, confirmation would either skip the stream lock
or require the caller to remember the stream out-of-band.

`DeliveryRequest` carries the opaque target, speaker, text, stream and
activation/line ids; `DeliveryReceipt` is opaque platform evidence of success.
the confirmed-send-before-recording invariant (a reply enters shared history
only after the platform says yes) is engine behavior and one of the most
valuable things the library exports.

`delivery="direct"` without a target or Deliverer is a configuration failure.
tests and CLI applications can wire an accepting local Deliverer; the engine
never silently redefines "returned to the caller" as "confirmed delivered."
`delivery="pending"` returns an immutable `PendingDelivery` with an opaque id.
the caller later invokes `confirm_delivery(pending_id, receipt)`, which is
idempotent and records the exact frozen pending text once. arbitrary
`record_delivered_message(speaker, text)` is deliberately not exposed.

that idempotence is a storage property, not an id-format trick.
`InMemoryDeliveryLedger` is the quick-start default. an app that needs pending
outputs to survive restarts supplies a durable ledger whose `confirm`
atomically transitions pending→confirmed and appends (or returns) the one
message event in the same storage transaction. the delivery-ledger
conformance suite calls confirm concurrently and after simulated restart. if
an app cannot coordinate its ledger and EventStore transactionally, it must
document the remaining duplicate/loss window; Dainframe does not claim a
stronger guarantee than the adapter can provide.

### 4.4 ContextProvider (briefing enrichment)

```python
class ContextProvider(Protocol):
    async def enrich(self, stimulus: Stimulus, line: ScriptLine) -> BriefingContext: ...
    # BriefingContext: kind, ambient_context, extras
```

everything chordial-specific in today's `_brief` moves here: the user-profile
lookup, the agenda digest, the "introduction gets no ambient" rule, the
stimulus-kind → briefing-kind mapping. the engine itself only fills the
mechanical fields: the visible event window, cue, style, scope, reason, and
the line's execution hints.

### 4.5 TurnHooks

```python
class TurnHooks(Protocol):
    async def after_inbound_recorded(self, stimulus, store, prev_user_event) -> None: ...
    async def after_turn(self, stimulus, store, result: ActivationResult) -> None: ...
```

- chordial's platform-switch courtesy → `after_inbound_recorded` (it's a
  product nicety, not engine logic — the 💜 goes home).
- chordial's completion reconciler → `after_turn` (already fully guarded;
  the engine keeps the guarantee that a hook failure never affects the reply).
- the legacy compression path → chordial-side, or deleted (it's off by
  default and pre-event-log vintage).
- shipped `CompositeTurnHooks` provides ordered composition. every hook call is
  isolated: its exception is logged/observed and never changes the activation
  result or prevents later hooks.

### 4.6 UsageSink

```python
class UsageSink(Protocol):
    async def emit(self, event: UsageEvent) -> None: ...
```

the loop emits a `ProviderCallUsage` for every model call and an
`AgentRunTrace` when the run ends; the app persists. chordial's two existing
recorder methods adapt mechanically. `NullUsageSink` is the default, and sink
failure is guarded — accounting can never break an agent run.

### 4.7 StreamCoordinator

```python
class StreamCoordinator(Protocol):
    def hold(self, stream_id: str) -> AsyncContextManager[None]: ...
```

one activation holds its stream from the inbound read/append through
`after_turn`. this prevents a user message and an ambient firing — or two
platform messages — from interleaving their scripts and event windows.
`InProcessStreamCoordinator` uses keyed asyncio locks and is the default.
multi-process apps provide a distributed lease or an optimistic versioned
implementation. serialization is per stream, never global.

### 4.8 ProviderResolver and shared concurrency

```python
class ProviderResolver(Protocol):
    def resolve(self, hints: ExecutionHints, *, agent: str) -> ResolvedProvider: ...
```

`ResolvedProvider` fixes provider, model, provider-compatible effort/options,
and one shared `ConcurrencyLimiter` for the whole Agent run. resolving a new
provider object per model must not accidentally create a new "global"
semaphore per object. provider-native continuation blocks stay on the
resolved provider/model until the run ends.

unsupported hint combinations fail before the first model call (or are
explicitly ignored by an Agent whose declared policy permits that); they do
not silently degrade based on whichever SDK happens to be active. usage
events record the model returned by each actual `AIResponse`, not merely a
provider object's configured default.

## 5. de-chordial-izing an activation (the key API change)

today the engine branches on chordial's stimulus taxonomy
(`user_message`/`scheduled_tick`/`introduction`/`curation_due`). the library
must not know that vocabulary. **`Stimulus.kind` is therefore an app-defined
string interpreted by the Director and ContextProvider.** the engine reads
only mechanical inbound-recording fields; response and delivery obligations
belong to the script lines the Director creates.

### 5.1 input: Stimulus

```python
@dataclass(frozen=True)
class Stimulus:
    kind: str
    stream_id: str
    content: str | None = None

    # inbound recording facts
    record_inbound: bool = True
    inbound_author: str = "user"
    inbound_author_type: str = "user"
    inbound_message_type: str = "conversation"

    # routing, provenance, and privacy
    platform: str | None = None
    scope: str = "dm"
    audience: str | None = None
    addressed: tuple[str, ...] = ()
    target: DeliveryTarget | None = None

    # ambient/director/context information
    reason: str | None = None
    precondition: ActivationPrecondition | None = None
    extras: Mapping[str, object] = field(default_factory=dict)
```

the object is immutable because it becomes the activation's source record.
there is one target field; `"group"` is a scope, not a second kind of
destination. platform-specific routing details belong inside the app's opaque
`DeliveryTarget`.

`ActivationPrecondition` is a read-only predicate evaluated only after the
engine holds the stream. ordinary reactive stimuli omit it. Pulse-built
stimuli use it to say, for example, "the candidate still has not spoken since
the event observed while planning." a stale ambient decision cancels before
direction or generation rather than delivering an exquisitely serialized but
obsolete nudge.

chordial's mapping is mechanical:

- `user_message`: records inbound; the Director casts required/direct lines.
- `introduction`: records inbound when content exists; the Director selects
  the introducing helper and a required/direct line.
- `scheduled_tick`: no inbound content; the Director casts a
  required/direct line with `outbound_message_type="scheduled"`.
- `curation_due`: no inbound content; the Director casts a silent/none line.

the interview platform invents `candidate_answer`, `stage_transition`, and
whatever else its state machine needs. no Chordial kind appears in library
control flow.

### 5.2 script: per-line obligations

```python
@dataclass(frozen=True)
class ExecutionHints:
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    max_tokens: int | None = None
    extras: Mapping[str, object] = field(default_factory=dict)

@dataclass(frozen=True)
class ScriptLine:
    speaker: str
    cue: str | None = None
    style: str = "full"
    response: Literal["required", "optional", "silent"] = "required"
    delivery: Literal["direct", "pending", "none"] = "direct"
    execution: ExecutionHints = field(default_factory=ExecutionHints)
    target: DeliveryTarget | None = None
    event_context: EventContext | None = None

@dataclass(frozen=True)
class Script:
    lines: tuple[ScriptLine, ...] = ()
    noop_reason: str | None = None
```

`target` and `event_context` are complete per-line overrides of the Stimulus
defaults. `EventContext` carries provenance, scope, audience, and outbound
message type. this matters when a visible interviewer reply and a private
scorer action share one activation: delivery policy alone is not a privacy
boundary. a complete override also avoids the ambiguity between "inherit the
audience" and "explicitly no audience."

an empty Script is valid only with an explicit `noop_reason`; otherwise
validation fails. this preserves app-defined unknown/no-op stimulus handling
without letting a broken Director silently drop an ordinary user turn.

the legal combinations are validated at script creation:

- `required/direct`: user-facing synchronous reply.
- `required/pending`: generate now; an external caller confirms later.
- `optional/direct|pending`: absence is accepted; text, if produced, follows
  the delivery policy.
- `silent/none`: text is neither required nor delivered; actions are the
  intended product.

`silent/direct` and `silent/pending` are invalid because they encode
contradictory intent. `required/none` is also invalid: if text is required,
the design must say where it goes. a silent agent that unexpectedly returns
text produces a line error rather than leaking private evaluator prose.

### 5.3 output: every line survives

```python
@dataclass(frozen=True)
class PendingDelivery:
    pending_id: str
    stream_id: str
    activation_id: str
    line_id: str
    speaker: str
    target: DeliveryTarget
    text: str
    # phase-0 discovery: the frozen pending must carry the line's effective
    # EventContext - confirmation otherwise cannot record the message event
    # with the right scope/audience/outbound message type (§5.2's per-line
    # EventContext override would be lost exactly where it matters most).
    event_context: EventContext

@dataclass(frozen=True)
class LineResult:
    line_id: str
    speaker: str
    status: Literal[
        "delivered", "pending", "silent", "refused", "errored"
    ]
    actions: tuple[ExecutedAction, ...] = ()
    pending: PendingDelivery | None = None
    error: ExecutionErrorInfo | None = None

@dataclass(frozen=True)
class ActivationResult:
    activation_id: str
    stream_id: str
    inbound_event_id: EventId | None
    status: Literal["completed", "cancelled", "noop"]
    status_reason: str | None
    lines: tuple[LineResult, ...]
```

`ActivationResult` intentionally has no aggregate `refused`, `errored`, or
`handled` booleans: in a multi-line activation they are lossy. convenience
properties such as `any_delivered` may be derived, never stored as a second
source of truth. activation-level `cancelled`/`noop` are not aggregate line
outcomes: they mean no script ran because a precondition became stale or the
Director explicitly chose no action.

### 5.4 tool execution context and action truth

the extracted registry is generic only after its handler context is:

```python
@dataclass(frozen=True)
class ToolContext:
    stream_id: str
    activation_id: str
    actor: str
    metadata: Mapping[str, object] = field(default_factory=dict)

ToolHandler = Callable[[Mapping[str, object], ToolContext], Awaitable[str]]
```

there is no `user_uuid`, `acting_helper`, or default `"chordial"` in library
code. a contextvar may still carry `ToolContext` safely through parallel tool
calls, but the value is mandatory and activation-scoped.

the loop attaches persistence policy to the action it returns:

```python
@dataclass(frozen=True)
class ExecutedAction:
    name: str
    input: Mapping[str, object]
    result_content: str
    is_error: bool
    terminal: bool
    record_event: bool
```

the engine therefore records arbitrary Agent outcomes without importing or
consulting that agent's ToolRegistry. successful side effects with
`record_event=True` are recorded even if a later provider call, required
reply, or delivery fails.

provider failure after tools have run is represented by an errored
`AgentOutcome` carrying its partial actions (or a typed `AgentExecutionError`
that carries the same data). raising a bare `ProviderError` and losing the
already-executed mutations is not allowed.

### 5.5 engine algorithm

inside `StreamCoordinator.hold(stream_id)`:

1. create an activation id and acquire the stream's EventStore.
2. evaluate the optional precondition against the locked stream; return a
   cancelled ActivationResult without recording or acting if it fails.
3. if `record_inbound` and content exists, append the inbound event and run
   `after_inbound_recorded`.
4. `script = await director.direct(stimulus, event_reader)`, then validate it;
   an explicit no-op returns a noop ActivationResult.
5. per line, in order:
   - build the viewer-filtered event window after all prior lines;
   - combine the mechanical fields, ContextProvider output, and execution
     hints into a Briefing;
   - invoke the Agent;
   - record every successful `record_event=True` action;
   - enforce the line's response policy;
   - deliver directly, freeze a pending delivery, or remain silent;
   - append prose only after a direct receipt or later pending confirmation;
   - retain success/failure as a `LineResult`.
6. run isolated `after_turn` hooks with the complete `ActivationResult`.
7. release the stream and return the result.

later speakers are briefed only after earlier lines have been recorded. a
pending line is not yet shared reality, so it does not appear in a later
speaker's event window unless it is confirmed before that speaker runs.
Direct multi-speaker scripts preserve Chordial's genuine-reaction behavior.
action and prose events use the line's effective EventContext, so private
evaluator actions do not accidentally inherit a candidate-facing channel.

`confirm_delivery` acquires the same StreamCoordinator before appending. a
pending integration still has an unavoidable external-send/database race;
direct delivery is preferred whenever the engine can own the send, because it
holds the stream across delivery and recording.

### 5.6 invariants

these are the hard-won product of Chordial's prior versions:

- inbound events are recorded before direction/acting when requested.
- once a side effect executes, every successful recordable action survives
  later refusal, provider failure, missing prose, or delivery failure.
- refusals and errors add no fictional conversational prose.
- a reply becomes shared history only after confirmed delivery.
- confirming the same pending delivery twice records it once.
- empty output on a `required` line is an error; empty output on `optional` or
  `silent` is intentional.
- text from a `silent` line is never delivered or recorded as conversation.
- provider errors are typed execution failures, never apology strings.
- activations for one stream are serialized; different streams may proceed
  concurrently.
- stale ambient preconditions cancel before model/tool work and record no
  fictional activation.

## 6. the pulse — ambient as a first-class feature

**the design question that created this section:** can a Stimulus be "a
schedule, or a dynamic schedule"? answer: no — and that's the design. a
`Stimulus` stays the atomic "something happened *now*". what lives in the
library is the thing that *produces* stimuli over time: the **Pulse**, which
takes abstract descriptions of *when* (**Rhythms**) and translates each firing
into an ordinary `Stimulus` handed to the same engine as any user message.
"when" and "what happens" stay orthogonal, so every engine invariant —
confirmed delivery, gate-before-generate, action truth, and no fictional error
prose — applies to ambient activations for free.

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
    anchor: EventQuery | Literal["last_delivered", "last_attempt"]
    jitter: timedelta | None = None

@dataclass
class Calendar:
    """clock-anchored: fire at local times/cron in the stream's timezone
    (tz resolved per-stream via an app callable). the morning-brief shape."""
    cron: str
    tz_of: Callable[[str], Awaitable[str]]
    misfire: Literal["skip", "fire_once"] = "skip"

@dataclass
class Dynamic:
    """decided cadence: a Decider is asked 'fire now? and when should I next
    even check?'. THE seam for an AI check-in gate — a utility model reading
    recent events and answering 'reach out now, about X' or 'ask me again
    this evening'."""
    decider: Decider
    max_sleep: timedelta = timedelta(hours=6)   # never trust a decider forever

class Decider(Protocol):
    async def decide(self, stream_id: str, events: EventReader,
                     now: datetime) -> Decision: ...
    # Decision: fire (bool), next_check (datetime), reason/cue (str | None)
```

a stream can carry several rhythms at once, each tagged with an app-chosen
stable, unique **rhythm id** plus an app-chosen `kind` that flows into the
stimulus — chordial: a `checkin` Interval + a
`curation` Interval; the harness: a `session_pace` Interval plus deterministic
review due dates (or a `review_due` Dynamic only when the policy is genuinely
adaptive); the interview: a `silence_nudge`
Interval anchored on an explicit query for the candidate's latest message.
the vague string `"last_message"` is rejected: candidate silence must not be
reset by a scorer action or interviewer message, and Chordial may want human
recency while another rhythm wants last successful outreach.

jitter is sampled once per occurrence and persisted; re-sampling it on every
poll would move the due time indefinitely. datetimes are timezone-aware UTC at
the protocol boundary — but the pulse normalizes defensively (`as_utc`): a
naive timestamp is treated as UTC rather than corrupting a conversion or
raising mid-cycle, because the first real adapter (chordial's SQL store)
already surfaces `datetime.utcnow()` rows. Calendar implementations specify
DST behavior: nonexistent local times simply never occur (spring-forward);
the fall-back fold is deduplicated by LOCAL wall clock, so a repeated local
time fires at most once per local occurrence — not once per UTC instant.

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
    async def check(self, firing: FiringPlan, events: EventReader,
                    now: datetime) -> GateDecision: ...
    # GateDecision: allowed, reason, retry_at

class StimulusFactory(Protocol):
    """cheaply resolve a due rhythm into the candidate actor/destination and,
    after gates clear, build its Stimulus."""
    async def plan(self, stream_id: str, rhythm: TaggedRhythm,
                   decision: RhythmDecision) -> FiringPlan | None: ...
    async def build(self, plan: FiringPlan) -> Stimulus: ...

class PulseStore(Protocol):
    async def claim_due(self, key: RhythmKey, now: datetime,
                        lease_until: datetime) -> PulseClaim | None: ...
    async def complete(self, claim: PulseClaim, outcome: PulseOutcome,
                       next_check: datetime,
                       occurrence_key: str | None = None) -> None: ...
    async def abandon(self, claim: PulseClaim, *, retry_at: datetime,
                      reason: str) -> None: ...
```

two phase-5 discoveries over the original field lists (the phase-0
doc-follows-code pattern):

- `complete()` takes `occurrence_key`: calendar dedup must persist
  atomically with the horizon, or a crash between two writes re-fires the
  same morning brief. the loop passes it **only when the activation
  genuinely concluded** (`activated`/`cancelled`) — a failed delivery keeps
  the occurrence open so the retry re-fires *today's* firing instead of
  silently skipping to tomorrow's.
- rhythm evaluation's quiet answer can carry an `occurrence_key` too
  (`Evaluation.occurrence_key`): a fresh Calendar rhythm persists its START
  BOUNDARY on its first poll — without that, evaluation re-anchors to `now`
  every cycle and the rhythm never fires — and a misfire-skip persists the
  consumed backlog.

`FiringPlan` is cheap and contains the rhythm key/kind, candidate actor (when
known), target/destination eligibility, the Dynamic decider's reason, and the
event observation needed to build an ActivationPrecondition. this resolves
WHERE and WHO before spending tokens on WHAT. it also fixes the original
ordering ambiguity: a per-author backoff gate cannot run before anyone has
selected the author.

`PulseStore` is durable state per stable `(stream, rhythm id)`. a state record
contains at least `last_attempt`, `last_generated`, `last_delivered`,
`next_check`, persisted jitter/occurrence key, decider scratch, and the active
claim/lease. `claim_due` is atomic compare-and-set: two loops or processes
cannot both own the same firing. `InMemoryPulseStore` provides these semantics
for tests and one-process quick starts; production apps must use a durable
implementation if duplicate ambient sends across restarts matter.

### 6.3 the loop

one cycle, per stream, per due rhythm:

1. **due + claim** — atomically claim a due interval/calendar occurrence or a
   Dynamic `next_check`. no claim means another worker owns it.
2. **decide** — when Dynamic is due, invoke its decider once; validate/clamp
   timezone-aware `next_check` to configured minimum/maximum bounds. `fire=False`
   completes the claim without an activation.
3. **plan** — cheaply resolve actor/destination eligibility and preserve the
   decider's reason. `None` completes or abandons according to the factory's
   explicit retry policy; it never spends generation tokens.
4. **gates** — run the gate stack with the complete FiringPlan. denial records
   its reason and persists `retry_at`; it does not hot-poll every cycle.
5. **build + act** — build the Stimulus and call `engine.handle`. ambient
   user-facing lines SHOULD be direct: the engine holds the stream across
   delivery and recording. a Pulse configured to accept pending results needs
   an explicit `PendingDispatcher`; otherwise a pending line is a
   configuration error rather than an undelivered success. the engine
   revalidates the FiringPlan-derived precondition after acquiring the stream;
   intervening user activity cancels without generation.
6. **complete** — atomically record attempt/generation/delivery outcome and
   the next occurrence/check, then release the claim.

failed sends stay out of the event log and do not consume the stream's
outreach allowance. they DO advance `last_attempt` and persist a bounded
delivery retry time, so the next five-minute poll neither regenerates nor
redelivers in a tight loop. if a process crashes after an external platform
accepted a send but before confirmation, exactly-once delivery is impossible
without a platform idempotency key/outbox. the library guarantees idempotent
event-log confirmation and at-most-one active claim; it documents the
remaining external at-least-once edge instead of claiming magic.

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
- **`CadenceGate(cadence, tz_of=...)`** — the long-tail alternative to
  `BackoffGate`: unanswered outreach climbs an explicit ladder of waits
  instead of doubling toward a cap. the ladder is a declarative spec with a
  compact string form — `Cadence.parse("1d x3, 1w x3, 60d @ 8-11")` reads
  "daily for three tries, weekly for three, then every sixty days forever,
  landing between 8 and 11 local" — so it can live in an env var, a settings
  row, or a chat tool's argument, and every consumer app speaks the same
  language. a final rung without a try count is the eternal floor (presence
  never tapers to zero); with one, the ladder exhausts into
  silence-until-they-speak, the old cap shape. `cadence` may be a per-stream
  async callable, which is the seam for user overrides ("hold me at monthly"
  is just a stored one-rung spec). everything derives from the event log:
  any user-authored event whose kind is in `presence_kinds` resets the
  ladder — by default only messages, but an app whose users show up by
  doing things rather than saying things (a desktop companion) should
  widen it to the kinds it records for those actions, or the ladder keeps
  escalating at someone who is already here. and one boundary the gate
  never crosses: it licenses **initiated moments** — "may something fire
  at this stream now?" — while what fires, and what form it takes (a
  generated message, an authored line, a pose, nothing at all), belongs
  to the stimulus factory downstream. ambient presence — a companion
  simply being there — is a state, not a firing, and is never gated.
  two honesty rules keep the arithmetic
  safe at the edges: denial horizons are bounded by `max_sleep` (default six
  hours), because the pulse sleeps on a persisted horizon without consulting
  events — the long floor becomes cheap periodic rechecks so a reply or a
  changed spec takes hold within one bound; and the event read is never
  narrower than the ladder's counted rungs, with a saturated no-user window
  reading as "past every counted rung" so a truncated count can't re-arm
  rungs the chain already spent.
- **`AllOf(gates)`** — first denial wins, reasons preserved.

`BackoffGate` requires `FiringPlan.actor` when `per_author_cap` is enabled;
configuration fails loudly if it is absent. app-flavored guards (chordial's
onboarding check, its platform-liveness rule) are just more `Gate`
implementations composed into the same stack.

### 6.5 what this dissolves in chordial

`SchedulerService` stops existing as a bespoke loop. its parts land as:
`get_scheduled_users` → `PulseSource`; onboarding + quiet hours + proactivity
gate → a gate stack (two of the three now library-shipped);
`resolve_delivery_identity` + speaker candidate + the first-contact rule →
`StimulusFactory.plan`; scheduled delivery moves into the engine's ordinary
direct-delivery path instead of remaining a scheduler-only two-phase dance;
the piggybacked curation pass → a second rhythm (`curation` Interval, whose
Director line is `silent/none`) —
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
    deliverer=message_router,
    coordinator=InProcessStreamCoordinator(),
)
pulse = Pulse(
    engine,
    source=ChordialPulseSource(user_manager),      # scheduled users -> rhythms
    gates=AllOf([OnboardingGate(user_manager),
                 QuietHoursGate(21, 8, tz_of=user_manager.get_user_timezone),
                 BackoffGate(crew_cap=4, per_author_cap=2, base_interval=hours(3))]),
    factory=ChordialStimulusFactory(user_manager),  # plan: actor + active target
    store=SqlPulseStore(...),
)
```

**interview platform**:

```python
engine = Orchestrator(
    agents={"interviewer": persona_agent, "scorer": silent_agent,
            "rubric": silent_agent},
    store_factory=lambda sid: PgEventStore(sid, visibility=candidate_never_sees_scores),
    director=InterviewDirector(stage_machine),      # required/direct interviewer,
                                                    # silent/none scorers
    context=InterviewContext(candidate_profile, role_rubric),
    deliverer=WebsocketDeliverer(...),
    coordinator=PgAdvisoryStreamCoordinator(...),
)
pulse = Pulse(engine,
    source=ActiveInterviews(),   # each session: silence_nudge Interval(3 min,
    gates=AllOf([]),             #   anchor=candidate_messages), stage_timeout Calendar
    factory=NudgeFactory())
# scorers return AgentOutcome(text=None, actions=[score_write, ...]) — the
# explicit silent/none shape. their action events are the evaluation record.
```

**creativity/education harness**: single or few agents, a near-static
director whose ScriptLines carry model/effort hints and constraining cues; the
"limit pure generation" contract is enforced by giving the persona agent a
narrow `ToolRegistry.view()` and pushing grounded work into terminal tools
backed by utility models. ambient pacing via a `session_pace` Interval;
deterministic spaced-repetition due dates use a Calendar/explicit event-query
anchor, while a genuinely adaptive review policy can use `review_due`
Dynamic. (this also
inherits chordial's v4 lesson for free: groundedness has to be in the framing,
and the dainframe makes the framing a first-class, per-activation cue.)

## 8. repo & workflow mechanics

- **repo:** `the-dainframe` (github.com/DainDeer/the-dainframe), poetry,
  python **>=3.10** initially, matching chordial's declared runtime contract.
  raising the floor later is an explicit coordinated decision, not an
  accidental consequence of extracting from a 3.13 development environment.
  same test-first culture: pytest from day one — port the generic
  halves of chordial's `test_orchestrator`, `test_agent_loop`,
  `test_anthropic_kwargs`, `test_event_log`, `test_proactivity_gate`, and
  `test_delivery_eligibility` suites.
- **contract suites:** the library ships reusable EventStore, DeliveryLedger,
  PulseStore, and provider-adapter conformance tests. app adapters run them in
  addition to product tests. engine tests include multi-line mixed policies,
  partial actions followed by provider failure, duplicate/concurrent pending confirmation,
  same-stream races, stale ambient cancellation, and different-stream
  concurrency. Pulse tests use an injected clock and cover claims,
  crashes/restarts, denial retry times,
  persisted jitter, DST/misfires, and failed delivery.
- **dependencies:** core/loop/tools have no provider SDK dependency.
  Anthropic and OpenAI adapters are optional extras (`dainframe[anthropic]`,
  `dainframe[openai]`); a combined extra is convenient but not mandatory.
  cron support likewise remains isolated to the Pulse dependency surface.
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

each extraction phase: implement the library contract → run its conformance
tests → adapt chordial imports → delete the chordial copy → run the full
chordial suite. no phase is complete merely because copied tests pass inside
the library; the installed package and the app adapter must also pass.

0. **phase 0 — contract freeze:** encode revision 3's types/protocols and
   conformance-test scaffolds before copying implementation. build two tiny
   executable spikes: Chordial's ordinary DM path and the interview shape
   `[required/direct interviewer, silent/none scorer]`. they may use fake
   providers/stores, but must exercise the public API.
1. **phase 1 — leaves, genuinely generalized:** providers (constructor-only
   config and injected shared limiter), `ToolContext` + registry views, the
   agent loop, partial-action failures, and `UsageSink`. point chordial at
   them. this phase is not a byte-for-byte copy: removing `user_uuid`,
   `"chordial"`, and the concrete UsageRecorder is its acceptance criterion.
2. **phase 2 — vocabulary and storage contract:** immutable Event/Stimulus/
   Script/ActivationResult vocabulary, Agent/Briefing/AgentOutcome,
   EventStore/EventReader and DeliveryLedger protocols, in-memory
   implementations and conformance suites. adapt chordial's SQL EventLog;
   preserve prompt bytes exactly.
3. **phase 3 — engine and coordination:** Orchestrator, per-line policies,
   pending confirmation, hooks, and keyed stream coordinator. chordial's
   `orchestrator.py` shrinks to `ChordialDirector` + `ChordialContext` +
   `ChordialHooks` + wiring. switch notice, reconciler, compression, and
   profile lookup all leave through seams.
4. **phase 4 — consumer pressure test:** implement one real thin vertical
   slice of project #2 before freezing v0.1 — preferably the interview path
   because it exercises mixed visible/silent lines and visibility-scoped
   evaluation. discoveries may still break the 0.x API; that is the purpose.
5. **phase 5 — the Pulse:** rhythms, FiringPlan, gates, atomic PulseStore
   claims, loop, failure retry and pending confirmation. chordial's
   SchedulerService dissolves per §6.5; curation becomes a rhythm.
6. **phase 6 — proof and tag:** chordial and the second-consumer slice are
   green; conformance suites pass against real adapters; installed-wheel tests
   pass; **prompt-byte tests remain unchanged** so warm caches survive. tag
   `v0.1.0`.

**risk 1 — activation/delivery truth:** a multi-agent activation is not one
optional string. mixed silent/visible roles and external delivery
confirmation are where a convenient result type can lie. mitigations:
per-line policy, lossless results, frozen pending ids, idempotent confirmation,
and no unconfirmed direct fallback.

**risk 2 — EventStore semantics:** chordial's log carries hard-won semantics:
action freezing, note kinds that never render, platform provenance vs.
filtering, visibility-before-windowing, and message-count windows. the library
takes ordering/query/visibility invariants and leaves renderings/action-line
format to apps. contract tests keep the in-memory and SQL versions honest.

**risk 3 — Pulse clocks and distributed ownership:** rhythms + restarts +
timezones breed double-fire/never-fire bugs; delivery adds a crash window.
mitigations: query-defined anchors, injected timezone-aware `now`, atomic
claims/leases, persisted jitter/occurrence ids, explicit misfire policy,
failure retry state, and honest documentation of the external delivery edge.

**risk 4 — concurrency:** without stream serialization, reactive and ambient
activations can interleave precisely where the framework promises sequential
shared reality. the coordinator default makes the safe local behavior the
easy behavior; distributed apps must consciously supply the stronger seam.
Pulse preconditions close the plan→lock race. v0.1 does not preempt an ambient
model call already running: a reactive activation arriving afterward waits
for the current holder. this is an explicit latency tradeoff for simple
ordering; apps needing preemption can provide a cancellation-aware coordinator
and Agent, but must still preserve action truth for tools already executed.

## 10. explicitly NOT in v0.1

- promptkit / cache-zone helpers (pattern documented, code stays app-side)
- any AI decider *implementation* — the `Dynamic`/`Decider` seam is library;
  an actual utility-model decider needs prompts, and prompts are app soul.
  chordial's haiku check-in gate, when built, is chordial code plugged into
  the seam (and the proof the seam is right).
- any storage implementation beyond in-memory (EventStore, DeliveryLedger,
  and PulseStore). apps supply durable adapters behind the contracts.
- account/user identity and stream-lifecycle management. Pulse can enumerate
  opaque stream ids, but what creates, owns, authorizes, or retires a stream is
  app business.
- the AI director (phase-3 chordial feature — same story as the AI decider:
  the `Director` seam is the library's part)
- an exactly-once external messaging claim. Dainframe can serialize a stream,
  lease a firing, freeze a pending output, and idempotently confirm its event;
  it cannot make an arbitrary chat platform and the app's database one atomic
  transaction. apps needing stronger delivery semantics supply a durable
  outbox and platform idempotency keys.
- distributed StreamCoordinator or durable PulseStore implementations. the
  protocols and conformance tests ship; infrastructure choices stay app-side.

## 11. dismissed alternatives and resolved ambiguities

this section is the decision trail. these shapes appeared in earlier
revisions or in Chordial's current implementation and are intentionally NOT
the revision-3 API.

### 11.1 activation-wide `expects_reply` and `delivery`

**dismissed:** put `expects_reply` and `direct|deferred|none` on Stimulus.

**why it looked good:** Chordial's current stimulus kinds mostly imply one
uniform kind of speaker, so the mapping was mechanical.

**why dismissed:** an interview answer casts a required visible interviewer
and silent private scorers in the same activation. one boolean either marks
correct scorers as errors or permits a missing interviewer reply. response and
delivery are per-line obligations (§5.2).

### 11.2 singular `Deliverable`

**dismissed:** return one `text`, `speaker`, `refused`, `errored`, `handled`
object from a multi-line script.

**why it looked good:** scheduled Chordial currently casts one speaker; group
lines are delivered internally and collapse to handled/not-handled.

**why dismissed:** multiple pending outputs overwrite each other, mixed
success disappears, and aggregate booleans cannot say which role failed.
`ActivationResult.lines` is lossless (§5.3).

### 11.3 direct-without-deliverer fallback

**dismissed:** when no delivery hook/target is wired, return the text and
record it as conversation anyway.

**why it looked good:** it kept old isolated adapters and tests convenient.

**why dismissed:** it violates confirmed-send-before-history while claiming
to preserve it. a test/CLI wires a local accepting Deliverer; external callers
use pending delivery.

### 11.4 free-form delivery finalization

**dismissed:** `record_delivered_message(stream, speaker, text)`.

**why dismissed:** callers can change text, spoof a speaker, confirm twice, or
record a message that was never generated. confirmation now consumes an
engine-issued immutable pending id plus receipt and is idempotent.

### 11.5 copy the tool registry unchanged

**dismissed:** treat `(tool_input, user_uuid)` and an `acting_helper`
contextvar defaulting to `"chordial"` as generic.

**why dismissed:** streams are not users, actors are not helpers, and a silent
default misattributes mutations. `ToolContext` is explicit and mandatory;
action persistence disposition travels with the executed action (§5.4).

### 11.6 model/effort strings as passive decoration

**dismissed:** add fields to ScriptLine without defining how they reach or are
honored by an Agent.

**why dismissed:** current HelperAgent owns one fixed loop/provider, and
provider-native continuation blocks cannot safely switch provider halfway
through a run. structured ExecutionHints reach the Briefing; the agent resolves
one provider/model for the whole run, using an injected shared limiter.

### 11.7 `recent(limit)` as a sufficient store abstraction

**dismissed:** expose Chordial's convenient method name without specifying its
window semantics, plus hard-coded `last_user_message`/`last_message`.

**why dismissed:** gates and rhythms need different anchors, and privacy must
filter before message-count windowing. EventQuery makes kinds/authors/viewer/
message limit explicit and testable (§4.1).

### 11.8 sync EventStore protocol

**dismissed:** preserve Chordial's synchronous SQLAlchemy surface in the
framework protocol.

**why dismissed:** it forces async Postgres consumers to block or invent a
parallel API. the shared engine is async; Chordial adapts its synchronous
store behind the async boundary during migration.

### 11.9 gate before actor/destination resolution

**dismissed:** `due → gates → StimulusFactory`, while BackoffGate includes a
per-author cap.

**why dismissed:** the gate cannot enforce an author budget before an author
exists. `StimulusFactory.plan` cheaply resolves actor/destination first; gates
still run before generation, preserving the zero-token denial invariant.

### 11.10 PulseStore as `last_fired` plus `next_check`

**dismissed:** a read/update bag of timestamps with no atomic ownership.

**why dismissed:** concurrent loops double-fire; crashes leave ambiguous
state; failed sends may regenerate every poll; jitter moves if resampled.
PulseStore is a claim/complete/abandon state machine with attempt, generation,
delivery, retry, occurrence and lease state (§6.2–6.3).

### 11.11 implicit concurrent activation behavior

**dismissed:** rely on event-loop scheduling and database id order.

**why dismissed:** ordered rows do not prevent two activations from briefing
against interleaved partial history. StreamCoordinator makes the activation
boundary real.

### 11.12 tag v0.1 before a second consumer

**dismissed:** finish Chordial extraction, tag v0.1, then start project #2.

**why dismissed:** the second consumer is the evidence that the API is not
merely Chordial renamed. a thin real slice now precedes the tag (§9).

### 11.13 Python 3.13-only extraction

**dismissed:** set the library floor from the developer's current interpreter.

**why dismissed:** Chordial declares Python >=3.10. revision 3 preserves that
contract until a deliberate coordinated runtime-floor decision is made.

### 11.14 activation-wide scope/audience for every line

**dismissed:** use only Stimulus scope/audience/provenance when recording all
agents in its script.

**why dismissed:** a visible interviewer and private scorer may share one
stimulus while producing events for different audiences. ScriptLine carries a
complete EventContext override; actions and prose use that effective context.

### 11.15 Pulse-specific deferred delivery as the normal path

**dismissed:** make the Pulse generate pending text, return it to a scheduler
callback, send it, then call a free-form recording method.

**why it looked good:** this is Chordial's current SchedulerService seam.

**why dismissed:** the engine already has the Deliverer, target, confirmation
invariant, and stream lock. ambient outreach should use the ordinary
required/direct line so generation→delivery→recording stays one serialized
activation. pending delivery remains available for integrations that truly
cannot let the engine own the send, behind an explicit ledger/dispatcher.

### 11.16 “errors record nothing” as an absolute invariant

**dismissed:** discard every post-inbound event whenever an Agent eventually
refuses or errors.

**why dismissed:** a tool mutation may already have succeeded before a later
provider call fails. erasing its action trail makes the shared record false.
the precise invariant is asymmetric: executed recordable side effects always
survive; failed/refused conversational prose never enters history.

### 11.17 plan/gate once, then activate regardless of intervening events

**dismissed:** let a Pulse plan clear outside the stream lock and assume that
decision remains valid until `engine.handle` eventually runs.

**why dismissed:** a user may speak while the firing waits for the stream,
making a silence nudge or check-in obsolete. FiringPlan captures the relevant
observation; Stimulus carries an ActivationPrecondition revalidated under the
lock. stale ambient work returns an explicit cancelled ActivationResult before
tokens or tools are spent.
