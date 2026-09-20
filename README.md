# diffcone

Diffcone is a static-first, function-level change-impact engine for Python.
It maps changes in application code to the tests and benchmarks that can
observe them, and explains every selection with a concrete dependency path or
an explicit fallback rule.

**Status: early prototype.** `diffcone plan` (milestone 1) and static
pytest/ASV target discovery (milestone 2) are implemented and covered by
acceptance scenarios. The tool produces a selection *plan* and does not run
or deselect anything. See [Limitations](#limitations) and
[docs/roadmap.md](docs/roadmap.md) before relying on it.

## What it does

Given two **committed** git revisions and a set of runnable targets (from a
manifest, from static discovery, or both), `diffcone plan`:

1. reads both source snapshots straight from git (no checkout, no execution);
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

`--discover RUNNER` statically discovers targets in the head revision.
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
select-everything fallback; `2` no plan (bad revision, bad manifest).

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
outward > `pytest_plugins` modules in the source roots), autouse fixtures,
xunit setup functions, its module, every `conftest.py` on its path and their
`pytest_*` hooks. A fixture that is neither found nor a pytest builtin
becomes the dependency `fixture:<name>`, which the planner cannot resolve, so
the test is selected conservatively; pass `--assume-external-fixture NAME`
for fixtures that installed plugins provide (`mocker`, `httpx_mock`, ...).

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

The JSON report (`schema_version: 1`) contains:

| Section | Contents |
|---|---|
| `analysis` | revisions/commits, source roots, `working_tree_analyzed: false`, the supported scope, counts |
| `changed_symbols` | every symbol that differs, with its change kinds |
| `selected_targets` | targets to run, the rules that selected them, whether any rule is conservative |
| `unselected_targets` | targets with no path to a change |
| `dependency_explanations` | per selected target, the edge path from target to changed symbol |
| `unresolved_relationships` | references the resolver could not bound, and which changed symbols they may match |
| `fallback_decisions` | every place the planner broadened selection instead of guessing |
| `analysis_errors` | parse failures etc.; any error selects all targets and sets `status: degraded` |

`--format text` prints the same information as a readable summary.

## Limitations

* **Committed snapshots only.** Uncommitted working-tree changes are never
  read; the report says so explicitly.
* **Discovery is static and partial.** `request.getfixturevalue`, fixtures
  from installed plugins, test base classes defined in other modules,
  inherited ASV benchmark methods and `params` expansion into cases are not
  modelled; see `docs/design.md` for the exact subset. Unknown fixtures are
  reported and selected conservatively.
* **Narrow, documented resolution subset** (see
  [docs/design.md](docs/design.md)): direct names and attribute chains rooted
  at module-level definitions, import aliases, star imports within source
  roots, or `self`/`cls`. No type inference, dynamic dispatch, inheritance
  lookup or instance attributes. Those references are reported as unresolved
  and matched conservatively by name against changed symbols.
* **Import-statement changes invalidate the whole importing module**, and
  any change to a class body (attributes, member list, bases, decorators)
  invalidates every method of that class. This is conservative by design.
* **Module-level side effects are only partially tracked.** A module's
  top-level statements are hashed, and functions that use module-level state
  get edges to the module, but a body change in a module's init code does not
  by itself invalidate every function defined in it or every importer.
  Declare the module as a lifecycle dependency when it should.
* **Dynamic reflection is always-on.** A function using `eval`, `exec`,
  `globals()`, `vars()`, `__import__`, or `getattr`/`import_module` with a
  non-literal name is treated as affected by *any* change in the repository.
* No caching; every run re-indexes both revisions.

## Development

```bash
uv sync
uv run pytest                                   # all tests
uv run pytest tests/test_scenarios.py -k alias  # one scenario
uv run ruff check src tests && uv run ruff format --check src tests
```

`tests/test_scenarios.py` holds the acceptance scenarios from the handoff
document and `tests/test_discovery.py` the discovery rules; each builds a
small git repository with before/after commits and asserts exact target sets
and reasons. See [AGENTS.md](AGENTS.md) for the rules that apply when
changing selection behaviour.

## License

MIT, see [LICENSE](LICENSE).
