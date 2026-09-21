# Roadmap

Implemented today: `diffcone plan` over two snapshots (commits, the staged
index or the working tree), with targets from a manifest and/or static
pytest and ASV discovery; `run`, `validate` (outcome and coverage based) and
`corpus`; evaluation on nine public repositories (see
[design.md](design.md) and [evaluation.md](evaluation.md)).

Everything below is planned, in the order it is worth doing. Each item
states the mechanism, the trade-off and what "done" means, so the
implementation can be checked against it and the corpus can measure it.
Any item that can narrow selection needs a regression scenario (AGENTS.md).

## 1. Discovery completeness

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

## 2. Resolution breadth

* **Instance-attribute tracking.** Resolve `self.<name>` to what the
  class's constructors bind it to, so `self.attr()` resolves and
  `getattr(x, self.attr)` is bounded. Measured on hatch (evaluation.md):
  every commit, including three version-bump releases, selects every test
  because `ClassRegister.collect` calls `getattr(cls, self.identifier)`
  and `identifier` is a constructor argument that is a string literal at
  every call site. *Mechanism:* in pass 2, for each class collect every
  write to `self.<name>` in every method of the class and of its in-scope
  subclasses (assignments, augmented and annotated assignments, `del`).
  An attribute is bounded only when its sole writes are in `__init__` and
  each binds a parameter, a resolvable name chain or a literal; a write
  anywhere else, a write through an unbounded target, or any
  `setattr(self, ...)`, `self.__dict__` use or `__setattr__` definition
  in the class or a subclass unbounds it (the last three unbound every
  attribute of the class; hatch's `PluginManager.__getattr__` does
  `setattr(self, name, ...)`, which is why this is needed). A chain
  `self.<name>` then resolves to the bound chain, and a parameter-bound
  attribute is treated like a parameter-dynamic use whose call sites are
  the constructor calls: calls of the class and of each in-scope subclass
  that does not define `__init__`, plus `super().__init__(...)` calls in
  subclasses that do, through the existing literal-propagation machinery.
  The class *escapes*, and the attribute stays unbounded, when the class
  is referenced other than as a callee, a base class or in a type
  position; type positions are annotations (including return
  annotations such as hatch's `-> ClassRegister`), the second argument of
  `isinstance`/`issubclass`, and `typing.cast`. Escape is tracked per
  class alongside today's per-function escapes and serialised in the
  module cache's resolution outputs. *Trade-off:* attributes written
  outside `__init__`, set reflectively, or on classes passed around as
  values stay unbounded, which is conservative; subclasses outside the
  source roots are invisible, as they are for dispatch today. *Done when:*
  on hatch's corpus the three release rows (which change only
  `hatchling.__about__.__version__`, so coverage recall is n/a there)
  each select fewer than a quarter of the suite with no outcome miss,
  the three non-release rows keep 100 % recall, and scenarios cover a
  constructor-argument `getattr`, a callable attribute, a rebinding in
  another method, a `setattr(self, ...)` and a class that escapes.
* Configurable treatment of module-init side effects (registries, plugin
  hooks) through explicit opt-in edges, and `diffcone.toml` ignore/force
  rules for known dynamic patterns.
* A `watch` loop for the developer inner loop once incremental analysis
  exists.

## 3. Planner cost on large trees

**Status.** With the module cache (design.md, "Module cache") a warm
working-tree plan on a synthetic 2 501-module tree spends 0.27 s indexing
and 0.4 to 0.55 s in `plan_from_indexes`: unioning the two indexes' edges
into one graph (0.16 s), dependency signatures for classification and
per-target explanation paths (about 0.1 s for 1 000 selected targets).

**Mechanism.** Profile first (`cProfile` around `plan_from_indexes` on
the synthetic tree); the candidates the current profile suggests are
building the union graph without re-adding every edge of both indexes,
and producing explanation paths without a sort per selected target.

**Trade-off.** Both touch determinism-sensitive code (sorted adjacency,
breadth-first order); the byte-identical-report tests and the recorded
corpora are the guard. Not worth doing before a real repository of that
size is in the evaluation set.

**Done when.** The synthetic tree plans warm in under 0.7 s wall with a
pending edit, and every recorded plan is byte-identical.
