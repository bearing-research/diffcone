# Roadmap

Implemented today: `diffcone plan` over two snapshots (commits, the staged
index or the working tree), with targets from a manifest and/or static
pytest and ASV discovery; `run`, `validate` (outcome and coverage based) and
`corpus`; evaluation on nine public repositories and a planning-only
census of 42 (see
[design.md](design.md) and [evaluation.md](evaluation.md)).

Everything below is planned, in the order it is worth doing. The order
comes from the selection census (evaluation.md, "Selection census"):
where selections come from across 42 repositories, not the worst case in
one corpus. Each item states the mechanism, the trade-off and what "done"
means, so the implementation can be checked against it and the census
and corpora can measure it. Any item that can narrow selection needs a
regression scenario (AGENTS.md).

## 0. Recall validation beyond the corpora (the governing rule first)

The rule every other item serves: running more tests than needed is
acceptable, missing an affected test is not. Recall is measured on nine
repositories only; the census plans 42 but never checks a selection, and
the flask `__init_subclass__` gap surfaced by accident. Finding misses
comes before any precision work.

*Mechanism:* run `diffcone corpus --coverage` (outcome changes and
per-test coverage at both snapshots) on about ten census repositories
that differ from the recorded corpora and install cleanly (a venv per
project with the project editable and its test dependencies), over their
recent Python-touching commits, with the census's root heuristic. Every
miss is triaged to a scenario plus fix, or to a documented known gap.
*Trade-off:* hours of machine time and per-project setup; coverage
cannot see import-time effects, so recall stays a lower bound on what
could be missed (outcome changes are the second check). *Done when:* at
least eight new repositories have recorded rows in evaluation.md with
their reproduction commands, and every miss found is fixed or recorded.

### Misses found so far (each gets a scenario and a fix)

Fixed: special methods (tenacity, 135 missed; design.md, "Special
methods run without being named"), every name after the point where
a chain stops resolving (found while tracing that fix), and symlinked
directories (pydantic; design.md, "Snapshot reader").


* **The runner runs project code** (pluggy: pytest calls pluggy's hook
  machinery during every test; 49 missed). *Mechanism:* a documented set
  of packages the pytest runner itself imports (`pluggy`, `_pytest`,
  `pytest`, `iniconfig`, `packaging`, `exceptiongroup`, `tomli`,
  `colorama`, `pygments`, plus `coverage` and `execnet` which the usual
  plugins load); a change in a module of such a package that lies in the
  source roots selects every pytest target through a new fallback rule
  `runner_dependency`, listed in the report. *Trade-off:* developing
  those packages means running their whole suite, which is what their
  own maintainers do. *Done when:* a scenario where a source-root
  package named `pluggy` changes selects every pytest target (and no ASV
  target), and pluggy re-validates at 100 %.

## 1. Dynamic references: the widespread constructs

The census attributes 27 % of selections to dynamic references alone
(18 % more to dynamic references or name matches). The constructs that
matter in many repositories are `getattr` with a loop variable over a
non-literal iterable (selections in 18 repositories, present in 30) and
`getattr` with a parameter whose call sites are unbounded or escape (17
and 31); `getattr` and `import_module` of a local variable and
`__import__` of a parameter follow (11, 8 and 4). Everything else is a single seed in a single
repository (networkx's backend dispatcher, rich's `repr.auto`).
*Mechanism:* not yet chosen. First classify a sample of the loop and
parameter cases from the census (what the iterable is: `dir(obj)`,
`vars(obj)`, `__dict__`, a class attribute tuple, a function result;
why the parameter is unbounded: escape, `*args`, a non-literal
argument), then sketch the one rule that covers the most. *Done when:*
that classification is in evaluation.md and this item carries a
concrete mechanism.

Instance-attribute tracking (implemented) bounds none of the census's
`self`-attribute cases (present in 11 repositories, causing selections
only in networkx); it is kept because it is sound and tested, with its
evidence limited to hatch.

## 2. Name matches

9 % of census selections alone, dominant in six repositories through a
few attribute names on untyped receivers (`app`, `callback`, anyio's
task-group methods, `load_cert_chain`, `get`, `headers`). Any bound needs receiver types,
which are out of scope; the useful next step is measurement (how many
of these selections the corpora's coverage confirms), before any rule.

## 3. Discovery completeness (rare in the census)

Unknown fixtures cause 1 % of census selections.

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

## 4. Other resolution work

* Configurable treatment of module-init side effects (registries, plugin
  hooks) through explicit opt-in edges, and `diffcone.toml` ignore/force
  rules for known dynamic patterns.
* A `watch` loop for the developer inner loop once incremental analysis
  exists.

## 5. Planner cost on large trees

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
