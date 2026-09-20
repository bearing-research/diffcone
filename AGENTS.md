# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` mirrors
it for Claude Code.

## Scope boundaries (do not widen silently)

Diffcone is a static-first, function-level change-impact engine. The current
milestone compares two snapshots (git revisions, `INDEX`, `WORKTREE`) and produces an
explainable selection **plan** from manifest and/or discovered targets. It
must:

* never execute project code or modify the working tree during analysis;
* always state which snapshot kind was analysed (commit, index or working
  tree); never present uncommitted analysis as committed;
* keep the planner free of pytest/ASV imports; runner integrations feed the
  manifest;
* keep the layers separate: snapshot reader → indexer/resolver → classifier
  → planner → reports, exchanging explicit data structures.

Out of scope until the roadmap says otherwise: type inference, dynamic
dispatch, branch- or argument-sensitive analysis, runtime tracing, machine
learning ranking, persistent caching.

## Invariants

* Symbol identity is the dotted qualified name; source locations are
  metadata. Blank-line or comment changes must produce no changed symbols.
* Both revisions are analysed; edges that exist only in the base revision
  still carry impact.
* Body changes and definition-time changes are classified separately.
* Unresolved is not unaffected. Every unresolved reference is recorded and
  matched conservatively; when impact cannot be bounded the plan broadens
  and says why.
* Every selection reason is backed by a real edge path or a named fallback
  rule. Never fabricate a call path.
* An analysis error selects every target and marks the plan degraded. It can
  never yield an empty selection.
* New or changed targets are always selected.
* Reports are deterministic.

## Changing selection behaviour

Any change that can **narrow** selection (a new resolution rule, a relaxed
propagation rule, a new hash exclusion) must ship with a regression scenario
in `tests/test_scenarios.py` that:

* builds a fixture repository with before/after commits;
* includes both pytest- and ASV-labelled targets;
* asserts the exact selected and unselected sets and the rules/paths behind
  them, not just that the command succeeds.

Update `docs/design.md` when resolution or propagation rules change, and keep
`README.md` honest about what is implemented versus planned.

## Commands

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
```
