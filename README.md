# diffcone

Diffcone is a static-first, function-level change-impact engine for Python.
It maps changes in application code to the tests and benchmarks that can
observe them, and explains every selection with a concrete dependency path or
an explicit fallback rule.

**Status: early prototype.** `diffcone plan` (milestone 1), static
pytest/ASV target discovery (milestone 2), working-tree analysis
(milestone 3), inheritance-aware resolution (milestone 4) and execution /
validation commands (milestone 5) are implemented and covered by acceptance
scenarios. Analysis never runs project code; `run` and `validate` execute
the runner only after a plan exists. See [Limitations](#limitations) and
[docs/roadmap.md](docs/roadmap.md) before relying on it.

## What it does

Given two snapshots (git revisions, the staged `INDEX`, or the `WORKTREE`)
and a set of runnable targets (from a manifest, from static discovery, or
both), `diffcone plan`:

1. reads both snapshots without checking anything out or executing code;
2. indexes modules, classes, functions and methods with stable identities and
   hashes of their bodies and definitions;
3. resolves the statically resolvable subset of references into dependency
   edges, and records everything else as *unresolved*;
4. classifies each symbol as added, deleted, body-changed, definition-changed
   or dependencies-changed, using **both** revisions' graphs;
5. walks dependencies backward to the targets and emits a deterministic plan.

Runner-independence is built in: pytest tests and ASV benchmarks are just
targets with a runner label, an entry symbol and declared lifecycle
dependencies (fixtures, `setup` methods). Runner knowledge lives only in the
discovery modules that produce those targets.

## Install and run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run diffcone plan \
  --repo . \
  --base main \
  --head HEAD \
  --discover pytest --discover asv \
  --source-root src --source-root . \
  --format json          # or: text
```

Committed snapshots are indexed once and cached under `.diffcone/cache/`
(add it to `.gitignore`; `--no-cache` and `--cache-dir` control it), and
every module's index is cached by file content, so the developer loop
`--base main --head WORKTREE` re-parses and re-resolves only the files
that changed since the last plan.

`--base` and `--head` accept any git revision, `INDEX` (staged content) or
`WORKTREE` (files on disk, tracked or untracked, ignored files excluded).
The report names the kind of each snapshot and flags uncommitted analysis,
so `--head WORKTREE` is the everyday developer loop and `--head HEAD` is the
CI form. `--discover RUNNER` statically discovers targets in the head
snapshot.
`--targets manifest.json` supplies them explicitly; both can be combined, and
a manifest entry overrides a discovered target with the same id. To inspect
or edit what discovery finds, emit a manifest first:

```bash
uv run diffcone discover --repo . --rev HEAD --discover pytest --discover asv \
  --source-root src --source-root . -o targets.json
```

Source roots decide module names: a file is named relative to the **longest**
root that contains it. With `--source-root src --source-root .`, the file
`src/calc/ops.py` is module `calc.ops` and `tests/test_calc.py` is
`tests.test_calc`, which is how the manifest below refers to them. With only
`--source-root tests`, that test module would be named `test_calc` instead.

Exit codes: `0` plan produced; `1` plan produced but analysis errors forced a
select-everything fallback; `2` no plan (bad revision, bad manifest); `3` plan
produced but discovery may be short of what the runner collects (a class a
plugin collects by its own rules, a base class or an imported test outside the
source roots). `1` and `3` are opposite failures -- `1` selects too much, `3`
means the target list itself may be incomplete -- and `3` wins when both apply.
`run` refuses to execute such a plan unless given `--allow-incomplete-discovery`.

### Running and validating

Planning never executes project code. Two commands run things *after* a
plan exists, with a command line you control:

```bash
# execute only the selected pytest targets (arguments after -- go to pytest)
uv run diffcone run --base main --head WORKTREE --discover pytest --command "uv run pytest" -- -x
uv run diffcone run --base main --head HEAD --discover asv --runner asv --dry-run

# outcome-based validation of the plan
uv run diffcone validate --base main --head HEAD --discover pytest --command "uv run pytest"
```

`corpus --range A..B` replays history: it plans and validates every
parent-to-commit pair in the range (first-parent order, commits without
`.py` changes skipped by default) and aggregates outcome misses, coverage
recall/precision and mean selection savings. Each commit's suite runs once.

Pass the source roots that hold the package (for a `src` layout,
`--source-root src --source-root tests`): validation puts them first on
`PYTHONPATH` so the checkout's code, not an installed copy, is what runs.
A relative interpreter path in `--command` (`.venv/bin/python -m pytest`)
is resolved against the current directory and then the repository, since
the suites run in temporary worktrees.
In a monorepo, one plan is one pytest session: pass every package root
that session imports plus the test tree it collects, and plan once per
session when packages carry their own `tests/` trees (two files mapping to
the same module name are reported as an analysis error, as pytest would
report an import mismatch). When one session collects several such trees
(`--import-mode=importlib`), give each its own namespace with
`--source-root DIR=PREFIX`.

`validate` runs the full pytest suite at both snapshots (commits are checked
out into temporary `git worktree`s, `WORKTREE` runs in place) and reports
every test whose pass/fail outcome changed but was not selected. With
`--coverage` it also runs the head suite under pytest-cov with per-test
contexts (pytest-cov must be installed in the environment that runs the
suite) and requires every test that *executed* a changed symbol to have been
selected, reporting recall and precision against that dynamic ground truth.
It exits 1 on any miss.

### Static discovery

Discovery never imports or runs project code; it reproduces a documented
subset of each runner's collection rules from the AST and reports what it
cannot resolve.

**pytest** ([details](docs/design.md#pytest)): `python_files`,
`python_classes`, `python_functions` and `testpaths` from `pytest.ini`,
`pyproject.toml`, `tox.ini` or `setup.cfg`; test functions, `Test*` classes
(without `__init__`), nested classes and `unittest.TestCase` methods. Each
test's lifecycle dependencies are its fixtures (by parameter, by
`usefixtures`, transitively, resolved class > module > nearest `conftest.py`
outward > `pytest_plugins` modules and the project's own `pytest11`
entry-point plugins in the source roots), autouse fixtures,
xunit setup functions, its module, every `conftest.py` on its path and their
`pytest_*` hooks. A fixture that is not found is assumed to come from an
installed plugin when a well-known plugin provides it (`mocker`,
`httpx_mock`, `freezer`, `anyio_backend`, `benchmark`, ...; the report
lists every such assumption; `--no-well-known-fixtures` turns this off) or
when it is passed with `--assume-external-fixture NAME`; any other unknown
fixture becomes the dependency `fixture:<name>`, which the planner cannot
resolve, so the test is selected conservatively. Doctests are discovered as
pytest collects them (`--doctest-modules`, `--doctest-glob`): a docstring
doctest depends on everything its module's globals can reach, and a
text-file doctest is always selected.

**ASV** ([details](docs/design.md#asv)): `benchmark_dir` from
`asv.conf.json`; `time_`/`timeraw_`/`mem_`/`peakmem_`/`track_` functions and
methods; lifecycle dependencies are the class and module `setup`,
`setup_cache` and `teardown` plus the module itself. Class attributes such
as `params` reach benchmarks through the class body.

### Target manifest

The manifest is the interchange format between discovery and the planner,
and the way to hand-author targets for runners without discovery. It is
JSON:

```json
{
  "source_roots": ["src", "."],
  "targets": [
    {
      "runner": "pytest",
      "runner_id": "tests/test_calc.py::test_add",
      "entry_symbol": "tests.test_calc.test_add",
      "lifecycle_dependencies": ["tests.conftest.db"]
    },
    {
      "runner": "asv",
      "runner_id": "bench_calc.TimeCalc.time_add",
      "entry_symbol": "benchmarks.bench_calc.TimeCalc.time_add",
      "lifecycle_dependencies": ["benchmarks.bench_calc.TimeCalc.setup"]
    }
  ]
}
```

* `entry_symbol` is the dotted identity of the test/benchmark function
  (module path relative to a source root, then class and function names).
* `lifecycle_dependencies` are symbols the runner executes for this target
  outside its body: pytest fixtures, ASV `setup`/`setup_cache`, etc. Diffcone
  does not infer these from naming conventions. A module symbol (for example
  `tests.test_calc`) can be listed too; that makes module-level state such as
  `pytestmark` or `pytest.importorskip` count for the target.
* A target's parameter cases are not modelled; a target is selected as a
  whole.
* `--source-root` on the command line overrides `source_roots`.

### Report

The JSON report (`schema_version: 2`) contains:

| Section | Contents |
|---|---|
| `analysis` | both snapshots (`revision`, `commit`, `kind`, `uncommitted`, `description`), source roots, `working_tree_analyzed` / `uncommitted_analyzed`, the supported scope with a one-sentence `analyzed` statement, counts |
| `changed_symbols` | every symbol that differs, with its change kinds |
| `selected_targets` | targets to run, the rules that selected them, whether any rule is conservative |
| `unselected_targets` | targets with no path to a change |
| `dependency_explanations` | per selected target, the edge path from target to changed symbol |
| `unresolved_relationships` | references the resolver could not bound, and which changed symbols they may match |
| `fallback_decisions` | every place the planner broadened selection instead of guessing |
| `analysis_errors` | parse failures etc.; any error selects all targets and sets `status: degraded` |

`--format text` prints the same information as a readable summary.

Measured results on real repositories, with reproduction steps, are in
[docs/evaluation.md](docs/evaluation.md), including a planning-only census
of 42 projects (`scripts/census.py`) that attributes every selection to
its cause.

## Limitations

* **Uncommitted analysis is explicit.** A `WORKTREE` or `INDEX` snapshot is
  named as such in the report (`analysis.head.kind`, `uncommitted_analyzed`).
  Results for a working tree are only as stable as the working tree.
* **Discovery is static and partial.** `request.getfixturevalue`, fixtures
  from installed plugins, test base classes defined in other modules,
  inherited ASV benchmark methods and `params` expansion into cases are not
  modelled; see `docs/design.md` for the exact subset. Unknown fixtures are
  reported and selected conservatively.
* **Narrow, documented resolution subset** (see
  [docs/design.md](docs/design.md)): direct names and attribute chains rooted
  at module-level definitions, import aliases, star imports within source
  roots, or `self`/`cls`, with class attributes looked up through the
  in-scope MRO (including `super()`) and `self`/`cls` calls dispatching to
  in-scope overrides. No type inference and no dispatch on receivers of
  unknown type; an instance attribute is resolved only when every write is
  a plain `__init__` assignment of a literal, a function or class, or a
  constructor argument that every construction passes as a literal
  (`getattr(hooks, self.identifier)`). Other references are
  reported as unresolved and matched conservatively by name against every
  known function, method or class of that name, so impact reaches them
  whenever any such symbol is affected.
* **Removing or redirecting an import invalidates the whole importing
  module** (adding one does not), and any change to a class body
  (attributes, member list, bases, decorators) invalidates every method of
  that class. This is conservative by design.
* **Import-time changes select every importer.** A change that runs when
  a module is imported (a top-level statement, a module-level constant, a
  class body, a decorator, a function that import-time code calls) selects
  every target whose module imports that module, directly or through
  others. This is deliberately broad: a plan may select more tests than
  needed, never fewer.
* **A file that does not parse forces select-all.** Any file under a
  source root with invalid syntax (including Python 2 code, which is not
  supported) or that is not UTF-8 is an analysis error, even a test data
  file nothing imports. Choose source roots that leave such files out.
* **Dynamic reflection is bounded by imports, dynamic imports are not.** A
  function using `eval`, `exec`, `globals()`, `vars()` or `getattr` with an
  unbounded name is treated as affected by any change in a module its own
  module imports (transitively); `__import__` or `import_module` with an
  unbounded name is affected by any change anywhere.

## Development

```bash
uv sync
uv run pytest                                   # all tests
uv run pytest tests/test_scenarios.py -k alias  # one scenario
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
```

`tests/test_scenarios.py` holds the acceptance scenarios from the handoff
document and `tests/test_discovery.py` the discovery rules; each builds a
small git repository with before/after commits (via `diffcone.testing`,
which is public so integrators can write the same kind of scenarios) and
asserts exact target sets and reasons. See [AGENTS.md](AGENTS.md) for the rules that apply when
changing selection behaviour.

## License

MIT, see [LICENSE](LICENSE).
