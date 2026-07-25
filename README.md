# the dainframe

a stimulus-driven multi-agent orchestration framework with an ambient pulse.

> something happens (**Stimulus**) → a **Director** casts who acts (**Script**)
> → each speaker gets a **Briefing** → the **Agent** acts (maybe with tools,
> maybe silently) → the engine **records** what happened into a shared,
> visibility-scoped **event log** → confirmed output goes back as a
> **Deliverable**.
>
> and: time passes (**the Pulse**) → abstract **Rhythms** come due → cheap
> **Gates** decide whether firing is welcome → a due rhythm becomes an
> ordinary **Stimulus**, handled by the same engine as any user message.

reactive and ambient activations converge on one engine, one event log, one
recording discipline. that convergence is the point: an ambient check-in and a
user reply are the same kind of moment, distinguished only by what stimulated
them.

extracted from [chordial](https://github.com/DainDeer/chordial-mvp), where the
machinery earned its invariants first. the full architecture — the engine, the
seams, the pulse, and the migration plan — lives in
[docs/DESIGN.md](docs/DESIGN.md).

## status

**pre-v0.1, extraction in progress.** phases (see the design doc, §9):

- [ ] phase 1 — providers, tool registry, agent loop
- [ ] phase 2 — core vocabulary (Stimulus, Event, EventStore, Agent)
- [ ] phase 3 — the engine (Orchestrator, Director, hooks)
- [ ] phase 4 — the pulse (rhythms, gates, ambient loop)
- [ ] phase 5 — chordial fully migrated, `v0.1.0` tagged

expect honest breakage until a second consumer exists — finding where the API
is too chordial-shaped is the whole reason this repo exists this early.

## using it (during extraction)

```toml
# pyproject.toml of a consumer, while the api is still settling
[project]
dependencies = ["dainframe"]

[tool.poetry.dependencies]
dainframe = { path = "../the-dainframe", develop = true }
```

## development

```bash
poetry install
poetry run pytest
```
