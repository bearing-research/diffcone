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

## 1. Evaluation at scale and breadth

Each of these is a corpus run first; code changes follow only from what
the run shows (this is how every improvement so far was found).

* **Third-party plugin fixtures.** None of the six repositories requested a
  fixture from an *installed* plugin. Measured on pipx (699 tests): 153
  tests fall back to select-all only because `mocker` (pytest-mock) and
  `fake_process` (pytest-subprocess) are not in the source roots.
  *Mechanism:* a curated table of well-known plugin fixture names, each
  mapped to the distribution that provides it, consulted after every
  in-scope level and before a name becomes `fixture:<name>`; every assumed
  name is reported in a discovery note (`external_fixture`, with the
  request count and the plugin) so the assumption is visible;
  `--assume-external-fixture` extends the table and
  `--no-well-known-fixtures` disables it. A name defined in scope always
  wins over the table. *Trade-off:* a project that requests `mocker`
  without installing pytest-mock would fail at collection anyway, so
  assuming the plugin never hides a real dependency; the table is a list
  to maintain. *Done when:* pipx plans without a fixture fallback, the
  assumed names are in the report, and a coverage corpus on pipx keeps
  recall at 100 %.
* **A monorepo** with several packages under one root and tests per
  package: exercises multiple source roots, cross-package imports and
  `conftest.py` layering across packages.

## 2. Discovery completeness

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

## 3. Resolution breadth

* Instance-attribute tracking: `self.attr = Callable` in `__init__` so
  `self.attr()` resolves; today it is name-bounded.
* Configurable treatment of module-init side effects (registries, plugin
  hooks) through explicit opt-in edges, and `diffcone.toml` ignore/force
  rules for known dynamic patterns.
* A `watch` loop for the developer inner loop once incremental analysis
  exists.

## 4. Planner cost on large trees

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
