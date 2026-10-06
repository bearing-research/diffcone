# Getting started

## Install

diffcone needs Python 3.11+ and git, and nothing else. Install the command
with any of:

```bash
uv tool install diffcone
pipx install diffcone
pip install diffcone
```

or the development version, `uv tool install
git+https://github.com/bearing-research/diffcone`. Check it with
`diffcone --version`.

diffcone does not need to be installed in your project's environment to
plan: it reads files and git objects, and never imports your code. The
commands that run your tests (`run`, `validate`, `collect`) take the
command that runs them, so your project keeps its own environment.

## A first plan

Take a small project with a `src` layout:

```text
src/calc/ops.py          tests/test_calc.py
-----------------        ------------------------------------
def add(a, b):           from calc.ops import add, mul, square
    return a + b
                         def test_add():
def mul(a, b):               assert add(1, 2) == 3
    return a * b
                         def test_mul():
def square(a):               assert mul(2, 3) == 6
    return mul(a, a)
                         def test_square():
                             assert square(3) == 9
```

Change `mul` to `return b * a`, commit, and ask diffcone what the commit
can affect:

```console
$ diffcone plan --base HEAD~1 --head HEAD --discover pytest \
    --source-root src --source-root . --format text
diffcone plan: HEAD~1 -> HEAD
source roots: src, .
base: commit 964b20a3a4dc (HEAD~1)
head: commit c32300921438 (HEAD)
scope: two committed snapshots; the working tree was not analyzed
status: complete

changed symbols (1):
  calc.ops.mul [function] body_changed

selected targets (2):
  pytest: tests/test_calc.py::test_mul
    - dependency: calc.ops.mul body_changed
      target:pytest:tests/test_calc.py::test_mul -[entry]-> tests.test_calc.test_mul -[references]-> calc.ops.mul
  pytest: tests/test_calc.py::test_square
    - dependency: calc.ops.mul body_changed
      target:pytest:tests/test_calc.py::test_square -[entry]-> tests.test_calc.test_square -[references]-> calc.ops.square -[references]-> calc.ops.mul

unselected targets (1):
  pytest: tests/test_calc.py::test_add

unresolved relationships: 0 (0 matching an affected symbol, 0 dynamic)

discovery (pytest): 3 target(s), 0 note(s)
```

What happened:

- `--discover pytest` found the three tests by reading pytest's
  configuration and collection rules, without importing anything.
- `--source-root src --source-root .` named the modules: `src/calc/ops.py`
  is `calc.ops`, `tests/test_calc.py` is `tests.test_calc`
  ([source roots](guides/planning.md#source-roots)).
- `calc.ops.mul` changed its body. `test_mul` calls it, and `test_square`
  reaches it through `square`; the paths show each step. `test_add` has no
  path to it.

Before committing, plan the working tree instead: `--base HEAD --head
WORKTREE`. The report says which kind of snapshot it read, so a plan of
uncommitted files never passes for a plan of a commit.

## Reading the report

Without `--format text`, `plan` prints JSON, the form tools read. The parts
you will use most:

```json
{
  "status": "complete",
  "changed_symbols": [
    {"id": "calc.ops.mul", "kind": "function", "changes": ["body_changed"], ...}
  ],
  "selected_targets": [
    {"runner": "pytest", "runner_id": "tests/test_calc.py::test_mul", "rules": ["dependency"], ...},
    {"runner": "pytest", "runner_id": "tests/test_calc.py::test_square", "rules": ["dependency"], ...}
  ],
  "unselected_targets": [
    {"runner": "pytest", "runner_id": "tests/test_calc.py::test_add",
     "reason": "no dependency path from this target to a changed symbol in either revision", ...}
  ],
  "fallback_decisions": []
}
```

- `status` is `complete`, or `degraded` when an analysis error (a file that
  does not parse, say) forced selecting everything.
- Each selected target lists the `rules` that selected it. `dependency`
  means a real path; others name a fallback, and `fallback_decisions` says
  where the analysis gave up bounding a change and why.
- `dependency_explanations` holds the edge paths shown above.

The [report reference](reference/report.md) lists every section.

The exit code matters in scripts: `0` a complete plan, `1` a degraded one
(selects everything), `2` no plan, `3` discovery may be short of what
pytest collects ([exit codes](reference/cli.md#exit-codes)).

## Running what it selected

```bash
diffcone run --base main --head WORKTREE --discover pytest \
    --source-root src --source-root . --command "uv run pytest" -- -x
```

`run` plans exactly like `plan`, then runs pytest on the selected tests
(arguments after `--` go to pytest). See
[running and checking](guides/running.md).

## Next

- Add `.diffcone/` to `.gitignore`: diffcone caches indexes there, so the
  next plan only re-reads the files that changed.
- If nearly every change selects nearly every test (a large library with a
  central module everything imports), try
  [execution evidence](guides/evidence.md).
