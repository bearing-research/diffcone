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
* **Monorepos with same-named test modules in one session.** Two files
  mapping to one module name are an analysis error today, which matches
  pytest's default import mode but not `--import-mode=importlib`, where
  `opentelemetry-api/tests/trace/test_globals.py` and
  `opentelemetry-sdk/tests/trace/test_globals.py` collect side by side
  (evaluation.md plans that repository per session instead). *Mechanism:*
  a per-root module prefix, `--source-root DIR=PREFIX`, so each test tree
  gets its own namespace (`api_tests.trace.test_globals`) while package
  roots keep theirs; discovery emits node ids from the path, unaffected.
  *Trade-off:* a prefixed name is diffcone's, not Python's, so it must
  never be used to resolve an import (imports of a prefixed module are
  impossible in importlib mode anyway). *Done when:* the opentelemetry
  API and SDK trees plan together without an analysis error and a hatch
  style one-session monorepo corpus (`src`, `backend/src`, `tests`, 2 106
  tests) is recorded.
* ASV: benchmark methods inherited from base classes, `params` expansion as
  parameter cases, benchmark directories outside the source roots, and
  `validate` for ASV (run `asv run --bench` at both snapshots and compare
  which benchmarks ran).
* An optional collection-based validator (`pytest --collect-only` through a
  plugin) to measure static discovery against real collection; it executes
  project code, so it stays opt-in and outside planning.

## 2. Resolution breadth

* Instance-attribute tracking: `self.attr = Callable` in `__init__` so
  `self.attr()` resolves; today it is name-bounded.
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
