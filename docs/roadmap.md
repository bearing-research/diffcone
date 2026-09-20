# Roadmap

Implemented today: `diffcone plan` over two snapshots (commits, the staged
index or the working tree), with targets from a manifest and/or static
pytest and ASV discovery; `run`, `validate` (outcome and coverage based) and
`corpus`; evaluation on six public repositories (see
[design.md](design.md) and [evaluation.md](evaluation.md)).

Everything below is planned, in the order it is worth doing. Each item
states the mechanism, the trade-off and what "done" means, so the
implementation can be checked against it and the corpus can measure it.
Any item that can narrow selection needs a regression scenario (AGENTS.md).

## 1. Per-module resolution cache

**Status.** The per-commit index cache shipped (design.md, "Index cache"):
a warm `plan --head WORKTREE` on click is 0.75 s, of which about 0.5 s is
indexing the working tree from scratch. That is under the one-second goal
for click; a monorepo will not be.

**Mechanism.** Split indexing into cacheable per-module stages:

* *pass 1 per file*, keyed by `(content hash, index format)`: symbols with
  hashes and line ranges, import table, bindings, literal names, class
  scopes. A pure function of the file.
* *global fingerprint*: the sorted module names plus every symbol id and
  class member list, computed from the pass-1 outputs (mostly cache hits).
  A body edit does not change it; adding or renaming a symbol does.
* *pass 2 per module*, keyed by `(content hash, fingerprint)`: edges,
  unresolved and external references, call sites, escapes and parameter
  dynamics contributed by that module. Only modules whose file or whose
  fingerprint changed are re-parsed and re-resolved; the rest load their
  pass-2 output without an AST. Parameter-dynamic expansion and override
  edges need cross-module state (call sites, descendants), so they stay a
  final full pass over the merged outputs.

**Trade-off.** Serialising pass-2 state that today lives on `Scope` objects
(local imports, literal names, parameters) for deferred parameter-dynamic
expansion is the intricate part; if it proves fragile, cache pass 1 only
and keep pass 2 full, which still removes parsing and hashing.

**Done when.** A second `plan --head WORKTREE` after a one-line body edit
re-resolves exactly one module (observable through cache counters), plans
are byte-identical with and without the cache on every scenario fixture,
and a synthetic 2 000-file tree plans warm in under one second.

## 2. Evaluation at scale and breadth

Each of these is a corpus run first; code changes follow only from what
the run shows (this is how every improvement so far was found).

* **A multi-minute suite, as a corpus (`corpus --jobs N`).** One pytest
  pair is measured: 8 min for two coverage runs of a 2 min 18 s suite, so
  a six-pair corpus is about 50 min serially.
  *Mechanism:* run pairs in parallel, each in its own pair of temporary
  worktrees, sharing the outcome cache through a lock; the per-commit
  suite-once property holds only within a job, so `--jobs` trades some
  repeated base runs for wall time.
  *Trade-off:* N worktrees of disk and N concurrent coverage databases;
  suites that write to shared locations (a global `.pytest_cache`, a
  hard-coded temp dir) can interfere, so `--jobs` defaults to 1.
  *Done when:* a six-pair pytest corpus with `--jobs 4` finishes in under
  20 min with results identical to the serial run.
* **Third-party plugin fixtures.** None of the six repositories requested a
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
