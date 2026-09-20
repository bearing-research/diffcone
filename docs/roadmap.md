# Roadmap

Implemented today: `diffcone plan` over two committed revisions, with
targets from a manifest and/or static pytest and ASV discovery (see
[design.md](design.md)). Everything below is planned, in rough order.

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

## 2. Working-tree analysis

* Analyse the index/working tree as the head snapshot (`--head WORKTREE`),
  with the report stating exactly which uncommitted state was read.
* Incremental re-indexing keyed by blob hash.

## 3. Broader resolution

* Inheritance-aware method resolution (MRO within source roots) and
  `super()`.
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

## 4. Validated execution integration

* `diffcone run --runner pytest` that executes the selected set and
  optionally the full set, comparing outcomes to measure precision/recall of
  the plan on real repositories.
* Persistent cache of per-commit indexes once correctness is established.
* A corpus of real-world before/after commits as regression data.
