# Roadmap

Implemented today: `diffcone plan` over two snapshots (commits, the staged
index or the working tree), with targets from a manifest and/or static
pytest and ASV discovery; `run`, `validate` (outcome and coverage based) and
`corpus`; evaluation on four public repositories (see
[design.md](design.md) and [evaluation.md](evaluation.md)).

Everything below is planned, in the order it is worth doing. Each item
states the mechanism, the trade-off and what "done" means, so the
implementation can be checked against it and the corpus can measure it.
Any item that can narrow selection needs a regression scenario (AGENTS.md).

## 1. Incremental re-indexing

**Problem.** Every plan re-parses and re-resolves both snapshots. On click
(~90 files) a plan takes a few seconds; on a monorepo it will take minutes,
and the inner loop (`--head WORKTREE` after each edit) needs sub-second.

**Mechanism.** Two layers, both keyed by content so they can never be stale:

* *Per-file parse cache*: `(blob sha or file content hash, indexer
  version) -> per-module facts` (symbols with hashes and line ranges,
  import table, literal names, unresolved references *before* cross-module
  resolution). Resolution is cross-module and stays a full pass, but it is
  cheap compared to parsing.
* *Per-snapshot index cache*: `(commit sha, source roots, indexer version)
  -> SourceIndex`, stored as JSON under `.diffcone/cache/`; a WORKTREE
  snapshot is never cached whole (its key would have to be the hash of
  every file), only its files hit the parse cache.

The cache is an optimisation only: a cache miss must produce a
byte-identical plan to a cache hit, and `--no-cache` must exist.

**Trade-off.** Cross-module resolution must be re-run whenever any file
changes, because a new symbol can change how an unchanged file's names
resolve. Caching resolution per module keyed on the *set of all module
names and their member names* is the next step if resolution itself becomes
the bottleneck.

**Done when.** A second `plan --head WORKTREE` on click after a one-line
edit runs in under one second, and a test proves that a plan produced from
cache equals the uncached plan on every scenario fixture.

## 2. Evaluation at scale and breadth

Each of these is a corpus run first; code changes follow only from what
the run shows (this is how every improvement so far was found).

* **A multi-minute suite** (candidates: `attrs` with hypothesis, `rich`,
  `pandas`-sized is out of scope for now). Questions: cost of the per-test
  coverage run relative to the plain run, memory of the coverage database,
  and whether `corpus` needs `--jobs` to run pairs in parallel worktrees.
* **Third-party plugin fixtures.** None of the four repositories requested a
  fixture from an *installed* plugin. Find one that uses `mocker`,
  `httpx_mock`, `freezer` or `anyio_backend` and measure how much
  `--assume-external-fixture` is needed; then ship a curated list of
  well-known plugin fixture names as the default, still overridable.
* **A monorepo** with several packages under one root and tests per
  package: exercises multiple source roots, cross-package imports and
  `conftest.py` layering across packages.
* **Coverage validation at base** as well as head: a test that executed a
  symbol *deleted* in head has no head lines to attribute; running the base
  suite under coverage (already done for outcome symmetry) and reading its
  database closes that gap.

## 3. Discovery completeness

* pytest: `request.getfixturevalue("name")` with a literal, names supplied
  by `pytest_generate_tests` (currently reported as unresolved, so
  conservative), base classes defined in other modules, `conftest.py`
  outside the source roots, doctests.
* ASV: benchmark methods inherited from base classes, `params` expansion as
  parameter cases, benchmark directories outside the source roots, and
  `validate` for ASV (run `asv run --bench` at both snapshots and compare
  which benchmarks ran).
* An optional collection-based validator (`pytest --collect-only` through a
  plugin) to measure static discovery against real collection; it executes
  project code, so it stays opt-in and outside planning.

## 4. Resolution breadth

* Instance-attribute tracking: `self.attr = Callable` in `__init__` so
  `self.attr()` resolves; today it is name-bounded.
* Configurable treatment of module-init side effects (registries, plugin
  hooks) through explicit opt-in edges, and `diffcone.toml` ignore/force
  rules for known dynamic patterns.
* A `watch` loop for the developer inner loop once incremental analysis
  exists.
