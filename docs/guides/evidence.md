# Execution evidence

Static planning cannot tell which tests of a large, tightly connected
library reach a change: in pandas nearly every change selects nearly every
test, because nearly everything imports the module that changed. Evidence
adds the one fact reading code cannot supply: which functions each test
actually executed, in a recorded run. On pandas, evidence plans select a
median of a few percent of the suite where static plans select all of it
([evaluation](../evaluation.md)).

Evidence is opt-in. It needs Python 3.12+ in the project's environment
(the recorder uses `sys.monitoring`); diffcone itself may run on another
interpreter.

## Record

```bash
# at a clean checkout; arguments after -- go to pytest
diffcone collect --command "uv run pytest" -- -n 8
diffcone evidence          # list the recordings (stores)
```

`collect` runs the whole suite once under a recorder and writes a store,
`.diffcone/evidence/<commit>-<environment>.sqlite`: the functions each test
executed and the repository files it touched, at that commit. It records
the checkout as it is, so the checkout must be clean (or pass `--rev REV`
to record a commit in a temporary worktree). `--reverse-check` runs the
suite a second time in reverse order and marks tests whose records differ
as unstable; those are always selected.

## Plan with it

```bash
diffcone plan --base main --head WORKTREE --discover pytest --evidence auto
```

`--evidence auto` picks the store at the nearest ancestor of the head
(`--evidence PATH` names one). A test is selected when its record meets the
change: it executed a changed function, a reader of a changed definition
or value, or a lookup that could see a changed name, or it touched a
changed file. A recording from an older commit still works: the plan
covers the changes between the recorded commit and both snapshots.

What evidence cannot bound is planned statically or selects everything,
and the report names the rule each time:

- changes that run at import, which reach every importer as in a static
  plan;
- tests with no record, an unstable record, or that started a subprocess;
- compiled sources (C, build files) and configuration.

ASV targets keep static selection.

## Run with it

```bash
diffcone run --base main --head HEAD --discover pytest \
    --command "uv run pytest" --evidence auto -- -n 8
```

The records assume the environment they were made in, so before any test
runs, `run` checks inside the test process that the interpreter, the
installed distributions and a few variables match the recording:
`PYTHONHASHSEED` (which `run` sets as recorded), `TZ`, `LANG`, `LC_ALL`, and
any variable the project names with `collect --env-var NAME` (pandas'
`PANDAS_FUTURE`, say). If they differ, the evidence says nothing about this
environment: `run` runs the static plan instead, or the whole suite if that
plan's discovery may be incomplete, and says why.

The recording also settles a doubt static discovery cannot: whether a
plugin collects a class pytest's own rules skip. A plan that would exit `3`
for such a class does not when the recorded collection shows nothing
beyond the targets and the class's file has not changed since.

## Keep it current

```bash
diffcone run --base main --head HEAD --discover pytest \
    --command "uv run pytest" --evidence auto --collect -- -n 8
```

With `--collect`, `run` also records the tests it runs and writes a store
for the head: their new records, and the old ones for every test the plan
did not select (they run identically at the head). The next plan starts
from there, without a full suite run per commit. It needs a clean checkout
of the head and the pytest arguments the store was collected with, and
writes nothing if the environment differs or pytest stops early.

In CI, a nightly full recording on the default branch serves the day's
pull requests: see [CI](../ci.md).

## Cython

Compiled Cython code (`.pyx`, `.pxd`, `.pxi`) is covered when the evidence
was recorded against a build with Cython's `profile=True` directive, on
Python 3.13+ (where a profiled build reports to `sys.monitoring`); the
test runs themselves use the ordinary build.

- An edit to a function's body selects the tests that executed it; for a
  `nogil` or `cpdef` function, which a profiled build does not always
  report, also the tests that executed a Cython function naming it.
- An edit outside function bodies (an import, a declaration, a constant, a
  class attribute, a function added) selects the tests that executed a
  Cython function that can see and names what changed, and for a name
  Python can see, the tests that ran Python code reading it.
- A module without Cython records, code outside functions that binds
  nothing by name (a bare call, a docstring, a compiler directive,
  `include`), a deleted name Python can see, and C sources or build files
  still select everything.

diffcone does not build your project; building with `profile=True` for the
recording is up to you.

## Shallow clones

Planning needs the recorded commit's objects. In a shallow clone, fetch it
(`git fetch --depth=1 origin <commit>`); diffcone names the commit when it
is missing. `--evidence auto` cannot tell ancestry in a shallow clone, so
pass the store's path there.

The assumptions evidence rests on (the same environment, deterministic
tests, test isolation) and what each rule covers are in the
[evidence design](../evidence_design.md).
