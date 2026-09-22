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

## 0. `getattr` bounded by a receiver whose type is known (precision)

The classification the previous version of this item asked for is done
(evaluation.md, "What the dynamic references actually are"). It did not
find a widespread construct a rule can bound: 71 % of dynamic-caused
selections are a `getattr` on a receiver with no known type and 22 % an
`import_module` with no receiver at all, both of which need type
inference or the caller's configuration. What is left is small but real:
5 % of those selections are a `getattr` on a name imported from a module,
and 0.5 % on `self`/`cls`. *Mechanism:* when the receiver of a dynamic
`getattr` resolves to a module or to a class (`self`/`cls` in a method,
an import alias, a module-level name bound once), expand the name over
that receiver's members -- the module's symbols, or the in-scope MRO's --
instead of seeding the select-all fallback; when the receiver resolves
outside the source roots (`getattr(sys, name)`), it reaches nothing in
scope and seeds nothing. *Trade-off:* sound only while the receiver's
attribute set is bounded, so a class or module the analysis has seen a
reflective write into (`setattr` with an unbounded name, `__dict__`
updates -- already tracked for instance attributes) must keep the
fallback; the candidate set also has to include inherited members, which
`lookup_in_class` already computes. *Done when:* flake8's
`getattr(sys, filename)` and isort's `getattr(settings, name)` stop
seeding, the census's dynamic share drops by the measured 5.5 %, no
recorded corpus commit loses a selected target, and a regression scenario
covers a reflective write defeating the bound.

Instance-attribute tracking (implemented) bounds none of the census's
`self`-attribute cases (present in 11 repositories, causing selections
only in networkx); it is kept because it is sound and tested, with its
evidence limited to hatch.

## 1. Name matches

9 % of census selections alone, dominant in six repositories through a
few attribute names on untyped receivers (`app`, `callback`, anyio's
task-group methods, `load_cert_chain`, `get`, `headers`). Any bound needs receiver types,
which are out of scope; the useful next step is measurement (how many
of these selections the corpora's coverage confirms), before any rule.

## 2. Discovery completeness (rare in the census)

Unknown fixtures cause 1 % of census selections.

* pytest: names supplied
  by `pytest_generate_tests` (currently reported as unresolved, so
  conservative), base classes defined in other modules, `conftest.py`
  outside the source roots.
* ASV: benchmark methods inherited from base classes, `params` expansion as
  parameter cases, benchmark directories outside the source roots, and
  `validate` for ASV (run `asv run --bench` at both snapshots and compare
  which benchmarks ran).
* An optional collection-based validator (`pytest --collect-only` through a
  plugin) to measure static discovery against real collection; it executes
  project code, so it stays opt-in and outside planning.

## 3. Other resolution work

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
