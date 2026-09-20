# Roadmap

Implemented today: `diffcone plan` over two committed revisions with a
manifest of targets (see [design.md](design.md)). Everything below is planned,
in rough order.

## 1. Real runner discovery

* **pytest**: discover test functions, methods and their fixture graph
  (including `conftest.py` layering, autouse and parametrised fixtures) and
  emit manifest targets with lifecycle dependencies automatically. Likely as
  a pytest plugin that runs collection only.
* **ASV**: read benchmark suites, `setup`/`setup_cache`/`teardown` and
  `params`, and emit targets.
* Keep the manifest as the interchange format so the engine stays
  runner-independent and discovery can be tested in isolation.

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
* Symbol-level ignore/force rules for known dynamic patterns.

## 4. Validated execution integration

* `diffcone run --runner pytest` that executes the selected set and
  optionally the full set, comparing outcomes to measure precision/recall of
  the plan on real repositories.
* Persistent cache of per-commit indexes once correctness is established.
* A corpus of real-world before/after commits as regression data.
