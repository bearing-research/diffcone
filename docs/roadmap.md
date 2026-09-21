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

## 1. Analysis errors that force select-all

Seven of 42 census repositories select every test on every commit;
analysis errors are 33 % of all census selections, more than any
selection rule.

* **Repeated class definitions** (anyio, black). A class defined more
  than once in a scope (`if`/`else`, a data file) is merged into one
  symbol, but each definition's body is indexed separately, so a method
  defined in both collides. *Mechanism:* index the members of all
  definitions of a class together, the way repeated functions already
  share one symbol whose hash covers every definition. *Trade-off:*
  none beyond what repeated functions accept (one symbol per name,
  hashed over every definition). *Done when:* a scenario with an
  `if`/`else` class defining the same method in both branches plans
  without errors, and anyio and black no longer degrade in the census.
* **A package binding that shadows a submodule** (tenacity, pip,
  poetry, scrapy): `pkg/__init__.py` binds `retry` and `pkg/retry.py`
  exists. Legal Python; at runtime `pkg.retry` is whichever binding ran
  last (normally the `__init__` one, since importing the submodule sets
  the attribute first). *Mechanism:* keep the module's identity
  (`pkg.retry` stays the module) and give the shadowing binding a
  distinct identity for the index (a documented suffix), resolving the
  attribute `pkg.retry` to *both* (an edge to each), so either reading
  is covered. *Trade-off:* a reference to `pkg.retry` depends on both
  the function and the module, which is conservative. *Done when:* a
  scenario covers a function shadowing a submodule with changes to each
  side, and the four repositories plan without errors.
* **One unparseable file** (pygments' Python 2 example under
  `tests/examplefiles`). *Mechanism:* a module that fails to parse is
  still reported as an analysis error, but the fallback is scoped:
  targets whose import closure (over both revisions) can reach the
  module, or that the module would define, are selected; others are
  planned normally. A file no in-scope module imports and pytest does
  not collect then costs nothing. *Trade-off:* an unparseable module
  that is imported dynamically is invisible to the closure; dynamic
  imports are already unbounded, so they still reach it. *Done when:* a
  scenario with an unparseable data file keeps other selections
  precise, the report still lists the error, and pygments stops
  degrading.

## 2. Soundness: class creation hooks

Subclassing runs the base's `__init_subclass__` (and a metaclass's
`__init__`/`__new__`); diffcone does not model it (flask's
`MethodView.__init_subclass__` is reached only through a dynamic
fallback in the census). *Mechanism:* a class, or a class statement in a
function body, whose in-scope bases define `__init_subclass__` gets an
edge to it (the first definition in its MRO after itself); a
`metaclass=` keyword referencing an in-scope class gets edges to its
`__new__`/`__init__`. *Trade-off:* widens selection only. *Done when:*
a scenario where a test subclasses a base whose `__init_subclass__`
reads a changed variable selects the test without any dynamic
reference.

## 3. Dynamic references: the widespread constructs

The census attributes 23 % of selections to dynamic references alone
(14 % more to dynamic references or name matches). The constructs that
matter in many repositories are `getattr` with a loop variable over a
non-literal iterable (selections in 16 repositories, present in 30) and
`getattr` with a parameter whose call sites are unbounded or escape (16
and 31); `import_module` of a local variable and `__import__` of a
parameter follow (6 and 4). Everything else is a single seed in a single
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

## 4. Name matches

7 % of census selections alone, dominant in five repositories through a
few attribute names on untyped receivers (`app`, `callback`,
`load_cert_chain`, `get`, `headers`). Any bound needs receiver types,
which are out of scope; the useful next step is measurement (how many
of these selections the corpora's coverage confirms), before any rule.

## 5. Discovery completeness (rare in the census)

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

## 6. Other resolution work

* Configurable treatment of module-init side effects (registries, plugin
  hooks) through explicit opt-in edges, and `diffcone.toml` ignore/force
  rules for known dynamic patterns.
* A `watch` loop for the developer inner loop once incremental analysis
  exists.

## 7. Planner cost on large trees

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
