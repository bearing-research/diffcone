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
| variable | module + name, for a top-level `NAME = <expr>` bound exactly once | `attr.ib` (an alias), `pkg.config.LIMIT` |

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
| variable | the right-hand side plus every module-level statement that mentions the name (it may mutate the value in place: `REGISTRY[k] = v`, `NAMES.append(x)`); those statements' lines are the variable's for coverage | (none) |
| module | top-level statements excluding definitions, imports and variable symbols | the set of import bindings (`import a as b`, `from m import n`), independent of grouping and order |

The docstring is excluded from every body hash and hashed on its own:
a docstring-only edit is reported as `docstring_changed` and carries no
impact (docstrings do not change behaviour; doctests, which would, are not
modelled). Consequences: adding or
removing a method is a class definition change; removing or redirecting an
import binding is a module definition change, while *adding* one is the
non-structural `imports_added`. Definitions nested inside `if`/`try`/`with`
blocks are still symbols and are excluded from their scope's body hash.

## Dependency edges

`Edge(source, target, kind, detail)` means *source depends on target*.

| Kind | Source → target | Produced by |
|---|---|---|
| `references` | function/class/module → symbol | resolved name or attribute chain; `detail` is `attribute:NAME` when the reference resolves to a module- or class-level variable (the module/class symbol stands in for it) or `module` when a module object itself is referenced |
| `references` (detail `mutated_by`) | variable → function/module | the target assigns into, augments, deletes from or calls a mutating method (`update`, `append`, ...) on the variable, so readers of the variable depend on its writers |
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
   `with ... as`, nested def name, function-local import) → **local**,
   except a parameter whose default is a module-level variable, which
   aliases that variable for reads and in-place mutations
   (`def build(registry=REGISTRY): registry[k] = v`). Decorators, defaults
   and annotations are resolved in the enclosing scope, where the
   function's own parameters do not exist yet;
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
6. A module-level variable → its `variable` symbol when it is a simple
   top-level assignment bound once (its right-hand side's references are
   the variable's edges, so an alias `ib = attrib` depends on `attrib`);
   otherwise (rebound, unpacked, assigned inside a block) the module symbol
   (`attribute:NAME`).
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
resolves `m` starting after the enclosing class in its MRO. A lookup on
`self`/`cls` dispatches at runtime, so besides the MRO hit it also records
`references` edges (detail `override`, or `override:attribute:NAME` for a
class-attribute rebinding) to whatever the attribute resolves to on each
in-scope descendant when that differs from the base hit: a method the
descendant defines, one it inherits from a mixin outside the base's
hierarchy, or a rebinding. A change to `Sub.step` (or `Mixin.step`) reaches
callers of `Base.run` that invoke `self.step()`. Override methods share the
dispatched call's site and escape status for literal propagation. Explicit
`Base.step` and `super().step` do not dispatch and get no override edges. Referencing a
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

`getattr(x, name)` and `importlib.import_module(name)` are expanded over
every string `name` may hold when that is bounded: a string literal, a
tuple/list/set of literals, a dict literal with string keys (iterated
directly, via `.keys()`, or as the first name of a `for key, value in
D.items()` target), a variable assigned
only such values (in the function or at module level), or a `for` variable
iterating over one
(`for attr in ("body", "orelse"): getattr(stmt, attr)`). Each candidate is
resolved like `x.<candidate>` or an import. A string built at runtime with a
literal prefix (`f"attr.{name}"`, `"attr." + name`, `"attr.%s" % name`,
`"attr.{}".format(name)`) bounds the candidates to what carries that
prefix: `import_module` expands to `imports` edges to every in-scope module
under it (a prefix matching no in-scope module is an external reference),
and `getattr` to every in-scope symbol name starting with it. When the name
is one of the enclosing function's parameters, the literal strings that every resolved
call site passes for it (positionally, by keyword, or via the parameter's
default) are used instead, so `def _attr(stream, attr): getattr(stream,
attr)` called as `_attr(s, "encoding")` and `_attr(s, "errors")` is bounded
to those two names; the function stays dynamic if it escapes (used as a
value, or its name occurs as an unresolved reference so callers may be
unknown), has no resolved call site, or any call site is unbounded
(`*args`, a non-literal). Only when the name is unbounded do `getattr`,
`eval`, `exec`, `__import__`, `globals()`, `vars()` and `import_module`
mark the enclosing symbol as having a **dynamic** reference.

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
| `dependencies_changed` | an outgoing non-containment edge was removed or redirected (a call was redirected, an alias now points elsewhere, an import stopped resolving) |
| `dependencies_added` | outgoing edges were only added (a new import binding, a new call); non-structural |
| `imports_added` | a module gained import bindings and lost none; non-structural |
| `docstring_changed` | only the docstring differs; non-structural, no impact |

A symbol whose only changes are `imports_added`, `dependencies_added`
and/or `docstring_changed` is reported but seeds no impact: nothing an existing dependent can observe
differs, and a member whose own resolution moved because of the addition
carries its own `dependencies_changed`. In practice this means adding a
name to a test module's import list no longer selects every test that lists
the module as a lifecycle dependency.

Symbols of a module that failed to parse in one revision are skipped rather
than reported as added/deleted; the analysis error fallback covers them.

The planner works on the **union** of both revisions' edges, each tagged with
the revisions it exists in. A dependency that only existed in base (a deleted
callee) still carries impact to its former consumers.

## Impact propagation

Each changed symbol seeds a backward search. A node carries impact in one of
two modes:

* **behaviour**: the symbol's runtime behaviour may differ;
* **structural**: the symbol was added/deleted, its definition changed,
  a dependency was removed or redirected, or (for classes) its body changed;
  this invalidates everything defined inside it. Pure additions (an import
  binding, a dependency edge) are not structural: they cannot break an
  existing member, and a member whose own resolution changed because of them
  carries its own `dependencies_changed`. Class bodies are structural because
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
* **Dynamic references.** A symbol containing a dynamic attribute access,
  `eval`, `exec`, `globals()` or `vars()` is treated as affected whenever an
  impact-carrying change lies in a module its own module can reach through
  imports (the module itself and its transitive import closure, over both
  revisions), since that is what its globals can name (rule
  `dynamic_reference`). A dynamic *import* (`__import__`,
  `importlib.import_module` with an unbounded name) can reach anything and
  stays always-on. A project-defined function named `vars` or `getattr` is
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

## Index cache

A committed snapshot's `SourceIndex` is a pure function of the commit, the
source roots and the index format, so `diffcone/cache.py` stores it as JSON
under `<repo>/.diffcone/cache/index/<sha256(format, commit, roots)>.json`
and serves it on later plans. `WORKTREE` and `INDEX` snapshots are never
cached whole (nothing identifies their content), and a head snapshot that
discovery needs files for is read rather than served. The cache is an
optimisation only: a hit must yield a byte-identical plan to a miss (tests
compare the reports and assert git is not read on a hit), writes are
atomic, write failures are silent, and `--no-cache` / `--cache-dir` control
it. `INDEX_FORMAT` is bumped whenever the indexer's output for the same
input can change; the fingerprint also covers the Python minor version,
since `ast.dump` output (hence every hash) may differ between versions.

### Module cache

Alongside the whole-index entries, `ModuleCache` keeps per-module results
in one SQLite file (`modules.sqlite`) for every snapshot kind, including
the working tree. Indexing is split into stages so each is a pure function
of something cacheable:

* **Facts, per file.** Pass 1 of a module (its symbols with hashes and line
  ranges, import table, bindings, literal names, class scopes with their
  base name chains, `defined_in` edges) depends only on the file's text,
  its module name and path, so it is stored under a key of those plus the
  index format and indexer fingerprint. A served module is not parsed at
  all. A module whose symbols collide with an earlier module's (an error
  either way) is indexed afresh so the errors come out exactly as without
  a cache, and a record is never stored for a module that collided.
* **Environment fingerprint.** After every class's bases are resolved
  (always run; it is cheap and its edges are part of the index), a digest
  is taken of everything a module's resolution can read from other
  modules: each module's observable facts (names, kinds, members, imports,
  bindings, star imports; digested once per file and stored with its
  facts) plus every class's resolved bases and completeness. A body edit
  leaves it unchanged; adding, removing or renaming a symbol changes it.
* **Resolution, per module.** Pass 2 writes (edges, unresolved and
  external references, call sites, escapes, parameter tables and deferred
  parameter-dynamic uses, whose function scopes are serialised) go to a
  per-module output, merged into the index and stored under
  `<facts key>-<fingerprint>`. Only a module whose file or environment
  changed is parsed and resolved again. Parameter-dynamic expansion needs
  every module's call sites, so it stays a final pass over the merged
  outputs.

Loads and stores are batched (one query per pass, one transaction per
build); with one file per record, opening thousands of files cost more
than resolving. The cache is invisible by construction and by test: every
indexer unit test builds its index plain, through a cold cache and through
a warm one and compares them; every scenario fixture plans uncached, cold
and warm (with the whole-index entries removed so the module cache is what
answers) and compares the reports; a test asserts that a one-line body
edit re-parses and re-resolves exactly one module. A malformed record is
treated as a miss (a facts record is fully built before it is installed).
Facts rows are kept for every distinct file content seen; resolution rows
only for the latest fingerprint each file was resolved against, so the
file is bounded by the number of distinct file contents. Reads open the
database read-only, so a cache directory that cannot be written still
serves hits.

Cost, warm `plan --base HEAD --head WORKTREE --discover pytest` wall time
through the installed entry point (about 0.1 s of it is interpreter
start-up): click (75 files, 555 tests) 0.27 s, was 0.75 s; pytest (3 486
discovered tests) 1.2 s; a synthetic tree of 2 501 modules 0.90 s with a
clean tree and 1.0 s with a pending one-line edit that selects 1 000
tests. On the synthetic tree, indexing the working tree fell from 1.1 s to
0.27 s in-process; the planner (0.4 to 0.55 s) is now the largest phase.

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
exists (even empty), otherwise `pyproject.toml` (`[tool.pytest.ini_options]`,
or pytest 9's native `[tool.pytest]` table), `tox.ini`, `setup.cfg` in that
order. `testpaths` entries may be directories, files or globs
(`tests/integ*`); a leading `./` is ignored.

Collected: files matching `python_files` under the source roots (a pattern
without `/` matches the basename, one with `/` the repo-relative path, as
pytest's `fnmatch_ex` does; restricted to `testpaths` if set); module-level functions matching `python_functions`;
methods of classes matching `python_classes` that have no `__init__`,
including methods inherited from base classes defined in the same module
(own definitions win; a base defined elsewhere yields an
`unknown_base_class` note); nested test classes; methods whose name starts
with `test` in classes with a base ending in `TestCase`. Node ids follow
pytest (`path::Class::method`); the entry symbol of an inherited test is the
method where it is defined, and the collecting class is a lifecycle
dependency. Parameter cases are not enumerated.

Fixtures are functions decorated with a dotted name ending in `fixture` or
`yield_fixture`, or module-level bindings of the form
`name = pytest.fixture(...)(function)`; `name=` and `autouse=True` keyword
arguments are honoured.
Requests follow pytest's `getfuncargnames`: parameter names minus `self`,
`request`, parameters with defaults, arguments injected by `mock.patch` /
`patch.object` decorators (unless `new` is given), arguments supplied by
hypothesis `@given` (keyword strategies by name, positional strategies
filling the last parameters), and names supplied by
`@pytest.mark.parametrize` on the function, class, enclosing classes or
module (`indirect` names stay requests). `@pytest.mark.usefixtures(...)`
on the function, class, enclosing classes or module adds requests.
Resolution order is class fixtures (the class, then its in-module bases,
then enclosing classes), module fixtures, `conftest.py` from the test's
directory outward, then plugin modules: those named in `pytest_plugins`
(conftest declarations are global) and the project's own `pytest11` entry
points from `pyproject.toml` (`[project.entry-points]` or
`[tool.poetry.plugins]`) or `setup.cfg`, and modules loaded with `-p name`
in `addopts` (a pytest-internal name such as `pytester` resolves to
`_pytest.<name>` when that module is in scope, as in pytest's own
repository), following one level of
`from ... import` re-exports so a plugin package's `__init__` exposes the
fixtures and hooks it imports (matched on the original name; a fixture keeps
its own name under an alias). Hooks defined in or imported into those
plugin modules are lifecycle dependencies of every test. Nearest scope wins; a fixture that requests its
own name (`def db(db)`) resolves to the next definition outward; requests
are resolved transitively along the same chain; autouse fixtures anywhere
on the chain apply.

Lifecycle dependencies of a test: the resolved fixture symbols, the test
module and its own `pytest_*` hooks, the module-level `pytestmark`,
`pytest_plugins`, `collect_ignore` and `collect_ignore_glob` variables of
the module and its conftests when they are variable symbols (`pytest_generate_tests` applies to
every test in the module), every conftest module on the chain and its
`pytest_*` hook functions, `setup_module`/`teardown_module`/
`setup_function`/`teardown_function` if present, and the class's (or its
in-module bases') xunit/unittest setup/teardown methods if present. A
fixture found at no in-scope level is a pytest builtin (ignored), a name a
well-known plugin provides (`WELL_KNOWN_PLUGIN_FIXTURES` in
`pytest_static.py`: `mocker` from pytest-mock, `fp` from pytest-subprocess,
`httpx_mock`, `freezer`, `anyio_backend`, `benchmark`, `db`, ...) or one
declared with `--assume-external-fixture`, in which case it is assumed to
come from the installed plugin and reported in an `external_fixture` note
with its request count and origin (`--no-well-known-fixtures` turns the
table off), or otherwise unknown and becomes `fixture:<name>`; a name
supplied only by `pytest_generate_tests` is reported the same way. A name
defined in scope always wins over the table, and a project requesting one
of these names without the plugin would fail at collection, so the
assumption never hides a real dependency. Measured on pipx (699 tests):
before the table, 153 tests were selected on every change because of
`mocker` and `fake_process` alone.

Not modelled: dynamic `request.getfixturevalue`, fixture visibility rules of
`pytest_plugins` declared outside the root conftest (accepted anyway),
fixtures of plugins outside the well-known table, doctests, base classes
defined in other modules,
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
  in the repository; `INDEX` is not supported. `--setup-command` runs a
  shell command inside each checkout before its suite, for build-generated
  git-ignored files a fresh checkout lacks (pytest's own `_version.py`). Each run gets the checkout's
  source roots first on `PYTHONPATH`, so the checkout's code wins over an
  installed (editable) copy of the project; with a `src` layout the working
  directory alone would not achieve that and the suite would silently test
  the installed revision. With `--coverage` this is verified: a measured
  file outside the checkout that shadows a checkout file is an error. Every test whose outcome
  differs between the snapshots must be selected; otherwise it is reported
  as missed and the command exits 1. New tests count as outcome changes;
  tests that ran at base and no longer exist at head are reported as
  removed, not missed (there is nothing to select). This is a necessary, not sufficient, check: behaviour changes
  that keep the same outcome are not visible to it.
* `validate --coverage` runs *both* suites under the coverage tracer so
  their outcomes are comparable (a recursion-depth or timing-sensitive test
  can flip under `sys.settrace`; comparing an uninstrumented base with an
  instrumented head produced false misses on structlog), and makes the head
  run also record
  `--cov=. --cov-context=test` into a temporary coverage database (the
  project's own `addopts` stay in force, so the same tests are collected)
  with a minimal coverage config of its own (a project's `source`/`omit`
  usually exclude the tests, which would make added tests unattributable;
  `branch`/`parallel` change the database layout) and reads it directly:
  both the `line_bits` and `arc` tables, paths resolved against the
  checkout, contexts folded to the test function.
  A test is *dynamically affected* when it executed a line owned by a
  changed head symbol whose change carries impact (additive-only changes
  are not ground truth: nothing executed behaves differently). Ownership
  mirrors the planner's rules: a function or method owns its lines
  including its decorators; a module or class owns only the lines outside
  its members unless its change is structural, in which case all its lines
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

* `corpus` drives `validate` over a commit range with a shared outcome
  cache (each committed snapshot's suite runs once when serial; coverage
  runs are per pair), records per-commit selection, savings, outcome and
  coverage results, and reports micro-averaged recall and precision plus
  mean savings. Commits that touch no `.py` file are skipped unless
  requested, since the planner cannot see them and their outcome changes
  would only ever be misses of the static approach itself.
* `corpus --jobs N` validates pairs in parallel threads, each in its own
  temporary worktrees and coverage database. The outcome cache is shared
  with a no-wait policy: a snapshot another job has already produced is
  reused, otherwise the job runs it itself and offers the result. Nobody
  waits on another job, because in a linear history each pair's base is
  the previous pair's head and waiting would serialise the corpus; the
  price is at most one extra suite run per pair. The report is identical
  to the serial one, and any exception (Ctrl-C included) cancels the pairs
  that have not started. Suites that write to shared locations (a
  hard-coded temp path) can interfere, so the default is 1.

## Known gaps (by design)

* Dynamic dispatch: `self.m()` resolves to the MRO definition plus in-scope
  overrides; overrides in classes outside the source roots cannot be seen,
  and a call through an unknown receiver stays name-bounded. The MRO is an approximation of C3
  and ignores metaclasses and `__getattr__`.
* Module init side effects: a body change in module init does not invalidate
  the module's own members or importers that do not reference its state.
  Use a module lifecycle dependency where that matters.
* Walrus assignments inside comprehensions bind in the enclosing scope in
  Python but are treated as comprehension-local here.
* Decorators that rewrite the decorated function are treated as ordinary
  references.
* Parameterised targets are selected as a whole.
