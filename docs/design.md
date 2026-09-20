# Diffcone design

This document describes what the current implementation does. Planned work
lives in [roadmap.md](roadmap.md).

## Pipeline

```
Git snapshot reader        diffcone/snapshot.py
  -> Source index          diffcone/indexer.py   (symbols, hashes, edges, unresolved refs)
  -> Change classifier     diffcone/classify.py
  -> Impact planner        diffcone/planner.py
  -> Reports               diffcone/report.py    (JSON and text)

Static discovery           diffcone/discovery/   (head snapshot -> manifest targets)
```

Each stage exchanges plain dataclasses (`diffcone/model.py`). The planner
does not import pytest or ASV; targets are opaque records produced by the
manifest, by discovery, or both (a manifest entry overrides a discovered
target with the same runner and id).

### Snapshot reader

`read_snapshot(repo, revision, source_roots)` returns one of three snapshot
kinds, and the kind travels with the index into the report:

| `revision` | kind | contents |
|---|---|---|
| any git revision | `commit` | `.py` files under the source roots from the object store (`git ls-tree` + `git cat-file --batch`); nothing is checked out |
| `INDEX` | `index` | staged content of tracked files (`git ls-files --cached`, blobs read as `:path`) |
| `WORKTREE` | `worktree` | files on disk: tracked and untracked (`git ls-files --cached --others --exclude-standard`), ignored files excluded, tracked files deleted on disk absent |

`INDEX` and `WORKTREE` record `HEAD` as their commit and carry a description
that says "uncommitted". A frozen `SnapshotInfo` (revision, commit, kind,
description) travels unchanged from the reader through the index to the
report, which exposes it as `analysis.<side>`, plus `working_tree_analyzed`,
`uncommitted_analyzed` and a derived `scope.analyzed` sentence; the text
report prints both descriptions and the same sentence. Runner configuration
for discovery is read from the same snapshot (staged or on-disk files
respectively). Unknown revisions are fatal (exit 2).

Edge cases: skip-worktree entries (sparse checkouts) are not on disk by
design, so `WORKTREE` reads them from the index instead of reporting
deletions; paths that are unmerged during a merge have no staged content, so
`INDEX` records an analysis error for each (the plan degrades and selects
everything), while `WORKTREE` sees the conflict markers as a parse error
with the same effect.

## Symbol identity

A symbol's identity is its dotted qualified name:

| Kind | Identity | Example |
|---|---|---|
| module | module path relative to a source root | `pkg.service` |
| class | module + class name (nested classes chain) | `pkg.models.Model` |
| function | module + name | `pkg.service.run` |
| method | module + class + name | `pkg.models.Model.save` |

Line numbers and file paths are metadata. Inserting blank lines or comments
does not change identity or any hash. Functions defined inside functions are
part of the enclosing function's body, not separate symbols. Several
definitions with the same name in one scope (overloads, property setters,
conditional definitions) are one symbol whose hashes cover all of them.

Source roots are mapped to module names with the longest matching root
winning; `pkg/__init__.py` is module `pkg`.

## Hashes

Hashes are SHA-1 prefixes of `ast.dump(node, include_attributes=False)`, so
whitespace, comments and positions never matter.

| Kind | `body_hash` | `definition_hash` |
|---|---|---|
| function/method | body statements | arguments (names, defaults, annotations), decorators, return annotation, sync/async |
| class | class-level statements excluding member definitions | bases, keywords, decorators, **sorted member names** |
| module | top-level statements excluding definitions and imports | import statements |

Consequences: a docstring edit is a body change (conservative); adding or
removing a method is a class definition change; changing any import statement
is a module definition change. Definitions nested inside `if`/`try`/`with`
blocks are still symbols and are excluded from their scope's body hash.

## Dependency edges

`Edge(source, target, kind, detail)` means *source depends on target*.

| Kind | Source → target | Produced by |
|---|---|---|
| `references` | function/class/module → symbol | resolved name or attribute chain; `detail` is `attribute:NAME` when the reference resolves to a module- or class-level variable (the module/class symbol stands in for it) or `module` when a module object itself is referenced |
| `defined_in` | member → container | every class, function and method |
| `imports` | module or function → module | `import m`, `from m import sub`, `importlib.import_module("m")` |
| `imports_name` | module → symbol | module-level `from m import name` |
| `entry` | target → symbol | manifest |
| `lifecycle` | target → symbol | manifest |
| `unresolved_name_match` | function → changed symbol | synthesised by the planner, see below |

### Resolution rules

A name or dotted chain `a.b.c` is resolved from its base:

1. `self`/`cls` (the first parameter of a non-static method) → the enclosing
   class; `self.m` resolves through the class's MRO (below).
2. A function-local import alias.
3. A binding of the current scope (parameter, assignment, `except ... as`,
   `with ... as`, nested def name, function-local import) → **local**;
   `local.attr` becomes an unresolved attribute reference bounded by `attr`.
   Nested functions, lambdas and comprehensions are separate scopes: a
   comprehension variable or an inner function's parameter never shadows a
   reference made outside it, while an inner scope still sees the enclosing
   function's locals.
4. A module-level definition in the same module.
5. A module-level import alias (`import a.b as x`, `from a import b`,
   relative forms). Absolute module names are resolved against the snapshot;
   modules outside the source roots are *external* (recorded, not edges).
   A missing submodule of an analysed package is *unresolved*, not external.
6. A module-level variable → the module symbol (`attribute:NAME`).
7. Star imports: every analysed star-imported module is consulted (in order,
   recursively) before an out-of-scope star import is blamed, so an in-scope
   symbol is never misattributed to a third-party package.
8. Builtins and dunder names are ignored, unless the program binds the same
   name itself.
9. Anything else → unresolved bare name.

Attribute steps walk from module to submodule, module member, module
variable or star-imported name; from class to method, nested class or class
variable, searching the class and then its bases in MRO order. Base names
are resolved where the class statement executes: the enclosing class body
for a nested class, then the module. Bases are resolved on demand (a dotted
base such as `Zed.Inner` may need another class's MRO first) and MROs are
memoised only once every class's bases are known. The linearisation is the
class followed by its bases depth-first left-to-right keeping the last
occurrence of a repeated base, which matches C3 for ordinary hierarchies;
inheritance cycles stop at the repeated class. A base that is external,
dynamic (`Generic[T]`, `namedtuple(...)`) or unknown marks the class
incomplete: a lookup that passes such a class before finding a hit records
the edge *and* a name-bounded unresolved reference, since an override in
the unknown part of the hierarchy could win. `super().m` inside a method
resolves `m` starting after the enclosing class in its MRO. Referencing a
class (`Foo(...)`, subclassing) also adds an edge to the `__init__` found
through its MRO, so constructor changes reach callers. A step that cannot
be taken yields an unresolved attribute reference bounded by the attribute
name. Once a chain reaches a function or
an opaque variable it stops there. An attribute whose base is not a name
chain (`Foo().run`, `items[0].run`, `make().run`) records an unresolved
attribute reference for `run` and the base expression is analysed on its
own, so `Foo` still gets a reference edge.

Module and class bodies are analysed without entering the definitions they
contain; those are symbols with their own edges.

`getattr(x, "lit")` is resolved like `x.lit`; `getattr` with a non-literal
name, `eval`, `exec`, `__import__`, `globals()`, `vars()` and
`importlib.import_module(<non-literal>)` mark the enclosing symbol as having
a **dynamic** reference.

Method identity and method-call resolution are separate: `Model.save` is a
symbol, but `obj.save()` on an untyped `obj` is an unresolved attribute
reference, never a resolved call.

## Before/after analysis

Both revisions are indexed independently. The classifier compares symbols by
identity and reports:

| Change | Meaning |
|---|---|
| `added` / `deleted` | present in only one revision |
| `body_changed` | `body_hash` differs |
| `definition_changed` | kind or `definition_hash` differs |
| `dependencies_changed` | the symbol's outgoing non-containment edges differ (a call was redirected, an alias now points elsewhere, an import stopped resolving) |

Symbols of a module that failed to parse in one revision are skipped rather
than reported as added/deleted; the analysis error fallback covers them.

The planner works on the **union** of both revisions' edges, each tagged with
the revisions it exists in. A dependency that only existed in base (a deleted
callee) still carries impact to its former consumers.

## Impact propagation

Each changed symbol seeds a backward search. A node carries impact in one of
two modes:

* **behaviour**: the symbol's runtime behaviour may differ;
* **structural**: the symbol was added/deleted, its definition or
  dependencies changed, or (for classes) its body changed; this invalidates
  everything defined inside it. Class bodies are structural because
  class-level attributes such as ASV `params`, pytest marks, or registries
  shape how every method runs without being referenced textually. Module
  bodies are not: making every constant edit invalidate a whole module and
  all its importers would make the plan useless; members that use module
  state carry their own edges, and a runner may declare the module as a
  lifecycle dependency of a target when module-level state governs it.

Edge rules (impact flows from the edge's target back to its source):

| Edge kind | Propagates when | Resulting mode |
|---|---|---|
| `references`, `entry`, `lifecycle`, `unresolved_name_match` | always | behaviour |
| `defined_in` | container is structurally affected | structural |
| `imports`, `imports_name` | imported module/name was **deleted** | structural |

So: changing a function body reaches its callers and their callers; adding a
method or editing a class attribute invalidates all methods of the class;
deleting a symbol that a test module imports at module level invalidates
every test in that module; editing a module-level constant reaches only the
functions that reference it (or targets that declare the module as a
lifecycle dependency).

## Uncertainty and fallbacks

Unknown is never treated as unaffected:

* **Name-bounded unresolved references.** For every unresolved bare name or
  attribute `NAME` in a symbol, and every known function, method or class
  in either revision whose short name is `NAME`, the planner adds an
  `unresolved_name_match` edge through a per-name pseudo-node (so the edge
  count is linear, and the pseudo-node is collapsed out of reported paths).
  Impact then flows through those edges like any other: `obj.save()` is
  affected when *any* `save` changes, and also when any `save` calls
  something that changed. Dunder names are excluded from matching: they
  exist on nearly every class and would bound nothing (constructors are
  reached through class references instead). The report lists each
  unresolved reference with the matches that actually carry impact
  (`matched_affected_symbols`).
* **Dynamic references.** A symbol containing a dynamic reference is treated
  as affected whenever anything at all changed (rule `dynamic_reference`).
  This is deliberately always-on: a helper using `vars(o)` is selected on
  every non-empty change set. Narrowing it (per-package seeds, ignore rules)
  is roadmap work; a project-defined function named `vars` or `getattr` is
  not mistaken for the builtin.
* **Entry symbol or lifecycle dependency not found in either revision**: the
  target is selected (`entry_symbol_unresolved`,
  `lifecycle_dependency_unresolved`).
* **Analysis errors** (unparsable file, identity collision): every target is
  selected (`analysis_error`, scope `all_targets`), the report status is
  `degraded` and the CLI exits 1. An error can never produce an empty plan.

Every reason in the report carries either a full edge path ending at a
changed symbol (with its change kinds) or the name of the fallback rule.
Reasons produced by any of the above rules are flagged `conservative`.

## Determinism

Files, symbols, edges and adjacency lists are sorted; the search is a
breadth-first traversal over sorted neighbours. Two runs on the same inputs
produce byte-identical reports.

## Static discovery

Discovery runs on the head snapshot only (targets that exist to run), reads
runner configuration files from the repository root (`pytest.ini`,
`pyproject.toml`, `tox.ini`, `setup.cfg`, `asv.conf.json`) and never imports
project code. Each runner module reproduces a documented subset of its
collection rules and emits notes for what it cannot handle. Every produced
target is validated against the index; a target whose entry symbol is not
indexed carries a `missing_symbol` note and falls into the planner's
`entry_symbol_unresolved` fallback.

### pytest

Configuration: a `pytest.ini` at the root is the config file whenever it
exists (even empty), otherwise `pyproject.toml`, `tox.ini`, `setup.cfg` in
that order. `testpaths` entries may be directories, files or globs
(`tests/integ*`); a leading `./` is ignored.

Collected: files matching `python_files` under the source roots (restricted
to `testpaths` if set); module-level functions matching `python_functions`;
methods of classes matching `python_classes` that have no `__init__`,
including methods inherited from base classes defined in the same module
(own definitions win; a base defined elsewhere yields an
`unknown_base_class` note); nested test classes; methods whose name starts
with `test` in classes with a base ending in `TestCase`. Node ids follow
pytest (`path::Class::method`); the entry symbol of an inherited test is the
method where it is defined, and the collecting class is a lifecycle
dependency. Parameter cases are not enumerated.

Fixtures are functions decorated with a dotted name ending in `fixture` or
`yield_fixture`; `name=` and `autouse=True` keyword arguments are honoured.
Requests follow pytest's `getfuncargnames`: parameter names minus `self`,
`request`, parameters with defaults, arguments injected by `mock.patch` /
`patch.object` decorators (unless `new` is given), and names supplied by
`@pytest.mark.parametrize` on the function, class, enclosing classes or
module (`indirect` names stay requests). `@pytest.mark.usefixtures(...)`
on the function, class, enclosing classes or module adds requests.
Resolution order is class fixtures (the class, then its in-module bases,
then enclosing classes), module fixtures, `conftest.py` from the test's
directory outward, then modules named in `pytest_plugins` (conftest
declarations are global). Nearest scope wins; a fixture that requests its
own name (`def db(db)`) resolves to the next definition outward; requests
are resolved transitively along the same chain; autouse fixtures anywhere
on the chain apply.

Lifecycle dependencies of a test: the resolved fixture symbols, the test
module and its own `pytest_*` hooks (`pytest_generate_tests` applies to
every test in the module), every conftest module on the chain and its
`pytest_*` hook functions, `setup_module`/`teardown_module`/
`setup_function`/`teardown_function` if present, and the class's (or its
in-module bases') xunit/unittest setup/teardown methods if present. Unknown
fixtures become `fixture:<name>` unless declared external
(`--assume-external-fixture`) or a pytest builtin; a name supplied only by
`pytest_generate_tests` is reported the same way.

Not modelled: dynamic `request.getfixturevalue`, fixture visibility rules of
`pytest_plugins` declared outside the root conftest (accepted anyway),
plugin-provided fixtures, doctests, base classes defined in other modules,
and `conftest.py` files outside the source roots.

### ASV

Collected: `.py` files under `benchmark_dir` (default `benchmarks`; read
from `asv.conf.json`, whose `//` and `/* */` comments are stripped, with an
`unparsable_config` note if it still fails to parse) whose path components
do not start with an underscore; functions and methods of
non-underscore classes named with `time_`, `timeraw_`, `mem_`, `peakmem_` or
`track_`. Benchmark ids are `<module relative to benchmark_dir>.<Class>.<method>`.

Lifecycle dependencies: the module, module-level `setup`/`setup_cache`/
`teardown`, and the class's `setup`/`setup_cache`/`teardown`. Class
attributes (`params`, `timeout`, ...) reach every method through the
structural class-body rule; module attributes through the module dependency.

Not modelled: `params` expansion, benchmark methods inherited from base
classes, a `benchmark_dir` outside the source roots (targets get
`missing_symbol` notes).

## Execution and validation

`diffcone/execution.py` is the only module that runs project code, and only
from the `run` and `validate` commands after a plan exists.

* `run` builds the runner command for the selected targets: pytest node ids
  appended to the command, or an anchored `--bench` regex for ASV. It exits
  with the runner's exit code, 0 when nothing was selected.
* `validate` (pytest only) runs the full suite at base and head with `-v`
  and parses the per-test outcome lines, folding parameter cases into their
  function and keeping the worst outcome. Commits are checked out into
  temporary detached worktrees that are removed afterwards; `WORKTREE` runs
  in the repository; `INDEX` is not supported. Every test whose outcome
  differs between the snapshots must be selected; otherwise it is reported
  as missed and the command exits 1. New or deleted tests count as outcome
  changes. This is a necessary, not sufficient, check: behaviour changes
  that keep the same outcome are not visible to it.
* `validate --coverage` makes the single head run also record
  `--cov=. --cov-context=test` into a temporary coverage database (the
  project's own `addopts` stay in force, so the same tests are collected)
  and reads it directly: both the `line_bits` and `arc` tables (branch
  coverage stores arcs instead of lines), paths resolved against the
  checkout so `relative_files` works, contexts folded to the test function.
  A test is *dynamically affected* when it executed a line owned by a
  changed head symbol. Ownership mirrors the planner's rules: a function or
  method owns its lines; a module or class owns only the lines outside its
  members unless its change is structural, in which case all its lines
  count (a constant edit does not make every function in the module
  "executed changed code", but adding a method does for the whole class).
  Recall is caught / dynamically affected and must be 100 % for the command
  to succeed; precision (dynamically affected among selected) is reported,
  not enforced, because conservative selection is by design. Lines executed
  at import/collection time carry no test context and are ignored; a run
  that records no contexts at all is an error, never an OK. The run sets
  `COVERAGE_CORE=ctrace` unless already set: coverage.py's default
  `sys.monitoring` core disables a line after its first execution, which
  silently credits only the first test to run each line and makes
  per-test contexts unusable.

## Known gaps (by design)

* Dynamic dispatch: `self.m()` resolves to the definition found in the
  enclosing class's MRO, not to overrides in subclasses; a call through an
  unknown receiver stays name-bounded. The MRO is an approximation of C3
  and ignores metaclasses and `__getattr__`.
* Module init side effects: a body change in module init does not invalidate
  the module's own members or importers that do not reference its state.
  Use a module lifecycle dependency where that matters.
* Walrus assignments inside comprehensions bind in the enclosing scope in
  Python but are treated as comprehension-local here.
* Decorators that rewrite the decorated function are treated as ordinary
  references.
* Parameterised targets are selected as a whole.
