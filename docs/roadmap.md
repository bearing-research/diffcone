# Roadmap

Implemented today: `diffcone plan` over two snapshots (commits, the staged
index or the working tree), with targets from a manifest and/or static
pytest and ASV discovery (see [design.md](design.md)). Everything below is planned, in rough order.

## 1. Discovery completeness

Static discovery covers the common layout. Remaining gaps, roughly by value:

* pytest: `request.getfixturevalue` with literal names, names supplied by
  `pytest_generate_tests` (currently reported as unresolved), base classes
  defined in other modules, `conftest.py` outside the source roots,
  doctests, a curated list of well-known plugin fixtures so
  `--assume-external-fixture` is rarely needed.
* ASV: benchmark methods inherited from base classes, `params` expansion as
  parameter cases, benchmark directories outside the source roots.
* An optional collection-based validator (`pytest --collect-only` via a
  plugin) to measure static discovery against real collection on a corpus;
  it would execute project code, so it stays opt-in and separate from
  planning.

## 2. Incremental analysis

* Incremental re-indexing keyed by blob hash, so repeated `--head WORKTREE`
  runs only re-parse changed files.
* A `diffcone watch` loop for the developer inner loop once caching exists.

## 3. Broader resolution

* Override-aware dispatch: when `Base.m()` is called through `self`, also
  consider in-scope subclasses that override `m` (conservative widening).
* Class-attribute and instance-attribute assignment tracking for
  `self.attr = Callable`.
* Finer import-change classification so adding an import does not invalidate
  a whole module.
* Configurable treatment of module-init side effects (registries, plugin
  hooks) with explicit opt-in edges.
* Symbol-level ignore/force rules for known dynamic patterns, and narrowing
  the always-on `dynamic_reference` seed (for example to the changed
  packages).
* Module-init side effects: let runner discovery declare module lifecycle
  dependencies automatically (pytest marks, `importorskip`, ASV module
  attributes), and evaluate making module body changes structural for test
  modules only.

## 4. Validation depth

* `validate` for ASV.
* Coverage validation at base as well as head (a test that executed a
  deleted symbol at base).
* Run `corpus` on public repositories and publish the numbers.
* Persistent cache of per-commit indexes once correctness is established.
* A corpus of real-world before/after commits as regression data.
