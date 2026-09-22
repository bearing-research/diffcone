# Roadmap

Implemented today: `diffcone plan` over two snapshots (commits, the staged
index or the working tree), with targets from a manifest and/or static
pytest and ASV discovery; `run`, `validate` (outcome and coverage based) and
`corpus`; recall validated on nineteen public repositories and a
planning-only census of 42 (see
[design.md](design.md) and [evaluation.md](evaluation.md)).

Everything below is planned, in the order it is worth doing. The order
comes from the selection census (evaluation.md, "Selection census"):
where selections come from across 42 repositories, not the worst case in
one corpus. Each item states the mechanism, the trade-off and what "done"
means, so the implementation can be checked against it and the census
and corpora can measure it. Any item that can narrow selection needs a
regression scenario (AGENTS.md).

## The governing rule

A plan may run more tests than needed; it must never miss one. Recall
work comes before precision work: nineteen repositories are validated
(evaluation.md, the corpora and "Recall validation beyond the
corpora"), and a selection-rule change is re-planned on all of them and
re-validated where selections move. Validating further repositories that
differ from these (other layouts, heavy metaprogramming, frameworks with
their own runners) is the standing way to find the next miss.

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
