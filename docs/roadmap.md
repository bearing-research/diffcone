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

* **Instance-attribute tracking.** `self.attr = <expr>` in `__init__`,
  with the values that construction sites pass for the parameter it comes
  from, so `self.attr()` resolves and `getattr(x, self.attr)` is bounded.
  Measured on hatch (evaluation.md): every commit, including three
  version-bump releases, selects all 2 105 tests because
  `ClassRegister.collect` calls `getattr(cls, self.identifier)` and
  `identifier` is a constructor argument that is always a string literal
  at the call sites. *Mechanism:* in pass 2, for each class record
  `self.<name> = <expr>` assignments in `__init__` (a parameter, a name
  chain or a literal); resolve a chain `self.<name>` to the bound chain,
  and treat a parameter-bound attribute like a parameter-dynamic use whose
  call sites are the class's constructor calls (the existing literal
  propagation machinery). *Trade-off:* attributes assigned elsewhere or
  rebound stay unbounded; only `__init__` is read, which is where the
  pattern lives. *Done when:* hatch's release commits select fewer than
  a quarter of the suite at 100 % recall, and a scenario covers a
  constructor-argument getattr and a callable attribute.
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
