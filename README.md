# diffcone

Diffcone is a static-first, function-level change-impact engine for Python.
It maps changes in application code to the tests and benchmarks that can
observe them, and explains every selection with a concrete dependency path or
an explicit fallback rule.

**Status: early prototype.** The first milestone (`diffcone plan`) is
implemented and covered by acceptance scenarios; it produces a selection
*plan* and does not run or deselect anything. See [Limitations](#limitations)
and [docs/roadmap.md](docs/roadmap.md) before relying on it.

## What it does

Given two **committed** git revisions and a manifest of runnable targets,
`diffcone plan`:

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
dependencies (fixtures, `setup` methods).

## Install and run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run diffcone plan \
  --repo . \
  --base main \
  --head HEAD \
  --targets targets.json \
  --source-root src --source-root tests \
  --format json          # or: text
```

Exit codes: `0` plan produced; `1` plan produced but analysis errors forced a
select-everything fallback; `2` no plan (bad revision, bad manifest).

### Target manifest

The manifest is a temporary integration boundary so the engine can be
validated before real pytest/ASV discovery exists. It is JSON:

```json
{
  "source_roots": ["src", "tests"],
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
  does not infer these from naming conventions.
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
* **Targets come from the manifest.** No pytest or ASV discovery yet.
* **Narrow, documented resolution subset** (see
  [docs/design.md](docs/design.md)): direct names and attribute chains rooted
  at module-level definitions, import aliases, star imports within source
  roots, or `self`/`cls`. No type inference, dynamic dispatch, inheritance
  lookup or instance attributes. Those references are reported as unresolved
  and matched conservatively by name against changed symbols.
* **Import-statement changes invalidate the whole importing module**, and
  adding or removing a method invalidates every method of its class. This is
  conservative by design for the first milestone.
* **Module-level side effects are only partially tracked.** A module's
  top-level statements are hashed, and functions that use module-level state
  get edges to the module, but a body change in a module's init code does not
  by itself invalidate every function defined in it or every importer.
* No caching; every run re-indexes both revisions.

## Development

```bash
uv sync
uv run pytest                                   # all tests
uv run pytest tests/test_scenarios.py -k alias  # one scenario
uv run ruff check src tests && uv run ruff format --check src tests
```

`tests/test_scenarios.py` holds the acceptance scenarios from the handoff
document; each builds a small git repository with before/after commits and
asserts exact target sets and reasons. See [AGENTS.md](AGENTS.md) for the
rules that apply when changing selection behaviour.

## License

MIT, see [LICENSE](LICENSE).
