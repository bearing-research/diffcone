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

## 0. Dynamic references: measured, and left alone

The classification this item once asked for is done (evaluation.md, "What
the dynamic references actually are"), and it closes the item rather than
sharpening it. 71 % of dynamic-caused selections are a `getattr` on a
receiver with no known type and 22 % an `import_module` with no receiver
at all; both need type inference or the caller's configuration, which are
out of scope (AGENTS.md). The heaviest seeds are by-name import utilities
(`import_string`, `load_object`, `import_from_string`, `_load_plugin`)
and plugin dispatchers, called with names that come from a user's
configuration.

That leaves the 5.5 % whose receiver resolves to a module or a class,
where the candidates could be the receiver's members instead of the
select-all fallback. It is not worth building: the bound is sound only
while nothing may have attached an unseen attribute to that receiver, and
a `setattr` with a name the indexer cannot bound -- `monkeypatch.setattr`
in a test suite, almost always -- exists in 34 of the 44 measured
repositories, so the rule would stay switched off in three quarters of
them for a share of selections that is already small. Reopen this only
with a repository where the receiver-bounded shape is measured to
dominate, or after the pool of reflectively written values is tracked
(the values, not just the names, of unknown-receiver writes), which is
the piece that would make the guard cheap.

Instance-attribute tracking (implemented) bounds none of the census's
`self`-attribute cases (present in 11 repositories, causing selections
only in networkx); it is kept because it is sound and tested, with its
evidence limited to hatch.

## 1. Name matches: measured, and left alone

9 % of census selections alone, through a few attribute names on untyped
receivers (`app`, `callback`, anyio's task-group methods,
`load_cert_chain`, `get`, `headers`). Any bound needs receiver types,
which are out of scope, so this item asked for a measurement first. It is
done (evaluation.md, "Are name-match selections worth their cost?") and it
closes the item: in the six repositories measured the name-match bucket is
a handful of selections, the volume sits in `dependency` and in `dynamic
or name match` -- and the latter means the target is reachable both ways,
so bounding name matches would deselect none of it. Reopen only with a
repository where name matching alone is measured to cause selections that
coverage says were not needed.

## 2. Discovery completeness (rare in the census)

Unknown fixtures cause 1 % of census selections.

* pytest: names supplied
  by `pytest_generate_tests` (currently reported as unresolved, so
  conservative), `conftest.py` outside the source roots, and tests a
  runner plugin collects by its own rules (reported as
  `uncollected_test_class`; SQLAlchemy's plugin collects alembic's 2387
  tests, of which pytest's documented rules find 23).
* ASV: `params` expansion as parameter cases, benchmark
  directories outside the source roots, and `validate` for ASV (run `asv
  run --bench` at both snapshots and compare which benchmarks ran).
(The collection-based validator this item asked for is implemented:
`scripts/collection_check.py`, evaluation.md, "Static discovery against
real collection".)

## 3. Other resolution work

* **Declared bounds**, the other half, are the dangerous half: telling
  diffcone that a dynamic seed reaches *only* certain modules is the only
  lever that moves sphinx, pip, networkx or scrapy off select-all
  (evaluation.md, "What the dynamic references actually are"), and it lets
  a project narrow selection on its own authority. Not designed yet, and
  not to be built without deciding how a plan that trusted a bound says
  so.
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
