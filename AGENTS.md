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

Out of scope until the roadmap says otherwise: type inference, dispatch on
receivers of unknown type (`self`/`cls` dispatch to in-scope overrides is in
scope), branch- or argument-sensitive analysis, machine learning ranking,
and persistent caching beyond the index, module and evidence stores
described in CLAUDE.md.

Runtime tracing is in scope for one thing only: the opt-in evidence
recorder (`diffcone collect`, `src/diffcone/collect.py`, roadmap item 5).
It runs inside the project's own test process, started from
`execution.py`, and never during planning. `plan --evidence` reads the
store it wrote as data, the way it reads a manifest. Static planning stays
the default and the reference.

## Invariants

* **The governing rule: a plan may select more targets than needed, but it
  must never miss one that the change can affect.** Soundness fixes come
  before precision work; a narrowing rule is acceptable only when it is
  provably conservative.
* An attribute read off an object by a name nothing resolves
  (`getattr(obj, name)`) is bounded by the classes that reach it: those a
  call site names, and those a factory it calls returns. This was an
  accepted exception while factories were untyped; it is closed. Widening
  the fallback instead -- such a read reaches any module -- costs five
  corpus repositories every saving they have (evaluation.md, "What the
  caller-object rule cost"), so close any remaining case the same way, by
  typing more receivers.
* A file the index does not read is unknown, not unaffected: a change to
  any non-Python file under a source root selects every target
  (`unanalysed_file_changed`). Only diffcone's own files are exempt, and
  a narrower rule needs a measured reason, like any other narrowing.
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
* Evidence narrows only pytest targets that have a record. Anything a record
  cannot vouch for falls back to static planning or selects the target:
  - a test with no record, an unstable record, or a subprocess;
  - a change that ran at import, or a module-level change;
  - a file no test's record sees.
  The recorder must never change a test's outcome: it prints nothing, and
  its own errors invalidate the store rather than reach the suite.

## Changing selection behaviour

Any change that can **narrow** selection (a new resolution rule, a relaxed
propagation rule, a new hash exclusion) must ship with a regression scenario
in `tests/test_scenarios.py` that:

* builds a fixture repository with before/after commits;
* includes both pytest- and ASV-labelled targets;
* asserts the exact selected and unselected sets and the rules/paths behind
  them, not just that the command succeeds.

Update `docs/design.md` when resolution or propagation rules change, and keep
`README.md` honest about what is implemented versus planned. New work starts
as a design sketch in `docs/roadmap.md` (mechanism, trade-off, done-when);
re-run the corpora in `docs/evaluation.md` when selection rules change.

## Commands

```bash
uv sync
uv run pytest
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
```
