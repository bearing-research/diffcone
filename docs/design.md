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

Symlinks: git stores a symlink as a blob holding its target, and pytest
collects through linked directories (pydantic links
`tests/pydantic_core -> ../pydantic-core/tests`). All three kinds expand a
tracked symlink under the source roots whose relative target stays inside
the repository: a file link takes its target's content at the link's
path; a directory link adds every `.py` file under its target at the same
relative path under the link, so its modules and targets are named as
pytest names them. Links inside an expanded tree are not followed again,
and absolute links or links leaving the repository are ignored.

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
conditional definitions) are one symbol whose hashes cover all of them;
for a class defined more than once, the members of all its definitions
are indexed together, so a method defined in each `if`/`else` variant is
one symbol as well. A package whose `__init__.py` binds a name that is
also one of its submodules (`pkg/__init__.py` defining `retry` next to
`pkg/retry.py`) keeps `pkg.retry` for the module and gives the binding
the identity `pkg.__init__.retry` (manifests name it that way too). The
attribute `pkg.retry`, and `from pkg import retry`, resolve to both the
binding and the module (an edge to each, the one to the module with
detail `module`), since the binding normally wins at runtime but either
may be meant; `import pkg.retry` and `from pkg.retry import x` are the
module. The same holds when the package re-exports a name of the
submodule (`from .main import main` in `pkg/__init__.py`, a common
pattern): `pkg.main` is the re-exported function and the module. A
package whose `__init__` imports a submodule of itself that is not in the
snapshot (a compiled extension) resolves that name as unresolved.

Source roots are mapped to module names with the longest matching root
winning; `pkg/__init__.py` is module `pkg`. A root may carry a module
prefix, `DIR=PREFIX` (`--source-root api/tests=api_tests`): its modules
are named `PREFIX.<path>` and the directory itself is the package
`PREFIX`. The prefixed name is diffcone's identity only, never used to
resolve an import (pytest's `importlib` import mode, the case this
serves, gives such modules no importable name either). Two files that map to the same
module name are an analysis error (the second is skipped and the plan is
degraded), which is the condition under which one pytest session would
fail with an import file mismatch too.

**Monorepos.** One plan models one pytest session: pass the source roots
of every package that session imports plus the test tree it collects. A
repository whose packages each carry a `tests/` tree run as separate
sessions (opentelemetry-python: `tox -e test-opentelemetry-sdk` runs
`opentelemetry-sdk/tests` alone) is planned once per session, for example
`--source-root opentelemetry-api/src --source-root opentelemetry-sdk/src
--source-root tests/opentelemetry-test-utils/src --source-root
opentelemetry-sdk/tests`. Planning the API and SDK test trees together
collides on `trace.test_globals` and `context` and degrades exactly as a
default-mode pytest session would; when a session really collects both
(`--import-mode=importlib`), give each tree a prefix
(`opentelemetry-api/tests=api_tests`, `opentelemetry-sdk/tests=sdk_tests`)
and the trees keep distinct identities. A repository that runs one
session over several packages (hatch: `src`, `backend/src`, `tests`) is
one plan. Cross-package imports resolve like any other in-scope import
because module names are global across roots.

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
class (`Foo(...)`, subclassing) also adds edges to the `__init__` and
`__new__` found through its MRO, so constructor changes reach callers. A step that cannot
be taken yields an unresolved attribute reference bounded by the attribute
name. Once a chain reaches a value of unknown type (a local, a failed
step, a variable, a function, a class-level binding) it stops there, and
every attribute name after that point is recorded as a name-bounded
reference too: `client.session.send()` on an unknown `client` records
`session` and `send`, `DEFAULT.send()` on a module-level instance
records an edge to `DEFAULT` and the name `send`. An attribute whose base is not a name
chain (`Foo().run`, `items[0].run`, `make().run`) records an unresolved
attribute reference for `run` and the base expression is analysed on its
own, so `Foo` still gets a reference edge.

Creating a class runs code of its bases and metaclass, so a class
statement depends on the `__init_subclass__` that each in-scope base
finds through its MRO (their union covers the one the new class's MRO
picks) and on an in-scope `metaclass=`'s `__new__` and `__init__` (edge
details `__init_subclass__`, `__new__`, `__init__`). The dependency is
recorded for the class and for what runs the statement: the module for a
top-level class (it is created on import), the enclosing function for a
class defined in a function body (a test that subclasses a view).

Special methods run without being named: `==` runs `__eq__`, `len()`
runs `__len__`, calling an instance runs `__call__`, `with` runs
`__enter__`/`__exit__`, a missing attribute runs `__getattr__`. A class
therefore depends on each special method it defines (every
`__dunder__` method except `__init__`, `__new__` and `__init_subclass__`,
which are reached explicitly): a `references` edge with detail
`special_method`. Whatever references the class (constructs it,
subclasses it, a factory returning instances) is reached by a change to
one of them, inherited ones through the subclass-to-base edge; the edge
does not invalidate the class's other methods (`defined_in` carries only
structural impact). Inside `__getattr__`/`__getattribute__`,
`getattr(x, name)` with `name` the method's own name parameter is not a
dynamic reference: the method runs only for an access `obj.<name>`, and
every such access records `<name>` itself.

A class with a base outside the source roots (directly or anywhere in
its in-scope MRO, or the subscripted base of `Base[T]`) may have any of
its methods called by that code: httpx calls a transport's
`handle_request`, `logging` calls a handler's `emit`, `json` calls an
encoder's `default`. Such a class depends on every method it defines
(edge detail `external_base`), so whatever references the class is
reached. Bases that only add structure do not count: builtins (`object`,
`dict`, `Exception`), `abc.ABC`, and `Generic`, `Protocol`, `NamedTuple`
and `TypedDict` from `typing` or `typing_extensions`.

Module and class bodies are analysed without entering the definitions they
contain; those are symbols with their own edges.

`importlib.import_module` and `importlib.__import__` are recognised under
any spelling the module's or function's imports give them (`from
importlib import import_module`, `import importlib as il`). A relative
name (`import_module(f"..templates.{name}", __name__)`) is resolved as
`importlib.util.resolve_name` does when the `package` argument is a
literal, `__name__` (the module's name) or `__package__` (its package);
under a `DIR=PREFIX` root the indexed name is not the runtime one, so
only a literal `package` resolves there. Any other relative name is
unbounded.

`getattr(x, name)` and `importlib.import_module(name)` are expanded over
every string `name` may hold when that is bounded: a string literal, a
tuple/list/set of literals, a dict literal with string keys (iterated
directly, via `.keys()`, or as the first name of a `for key, value in
D.items()` target), a dict literal's string values (`D[key]`, `D.values()`,
or the second name of a `for key, value in D.items()` target) or a sequence
literal's elements (`L[i]`, a slice excepted), a variable assigned
only such values (in the function or at module level) and never mutated in
place (a `REGISTRY = {}` that any module fills with `REGISTRY[k] = v`, an
`append`, an `update` or a `del` is not the literal it was assigned, in
the scope that mutates it and everywhere the variable is visible), or a
`for` variable iterating over one
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
to those two names. The call sites are plain calls, `obj.m(...)` and
`self.m(...)` (shared with dispatched overrides), `super().m(...)`, and
constructions: `Foo(...)` or `cls(...)` is a call site of the `__init__`
its MRO resolves to (any subclass's for `cls`), with `self` implicit. The
function stays dynamic if its body rebinds the parameter; if it escapes
(used as a value: a bare reference, `getattr(mod, "f")` returning it, or
`super().m` not called), or its name occurs as an unresolved reference so
callers may be unknown (an unresolved `super().m` in class K counts only
for an `m` that follows K in the MRO of K or of one of its subclasses);
if it is an `__init__` and a class that inherits it escapes; or if it has
no resolved call site or any call site is unbounded (`*args`, a
non-literal). A class escapes when it is referenced other than as a
callee, a base class, the second argument of the builtin
`isinstance`/`issubclass` or the first of `typing.cast`. Annotations
count as escapes: injector, FastAPI's `Depends()` and pydantic build
instances from them with arguments no call shows; `cls` as a
value in a classmethod, `type(self)` and `self.__class__` make the class
and every in-scope subclass escape. Expanding a `getattr` can make a
function escape, or record a name-bounded reference (a candidate that
does not resolve on its receiver, or a name after a chain stops), either
of which can unbound another expansion, so expansion is repeated until
both the escape set and the unresolved names are stable.

The name can also be an instance attribute: `getattr(hooks,
self.identifier)` in a method of class C is expanded over what
`self.identifier` may hold. That is bounded only when every class in the
MRO of C or of an in-scope subclass of C is plain (no class decorators or
keywords such as `metaclass=`, every base in scope or `object`), none of
them defines the attribute at class level or defines `__setattr__`,
`__delattr__`, `__getattr__` or `__getattribute__`, none uses
`setattr(self, ...)`, `delattr(self, ...)`, `self.__dict__` or `vars(self)`,
and every write of `self.<attr>` in their methods is a single-target
assignment in `__init__` of a parameter the body does not rebind, a
literal, or a name chain that resolves to a symbol. A write through any
other receiver (`obj.identifier = ...`), `setattr(obj, "identifier",
...)` / `monkeypatch.setattr` / `patch.object` naming it, or one of those
with an unbounded name, or a write into another object's `__dict__`,
leaves the attribute (or every attribute) unbounded everywhere, since the
receiver's type is unknown; so does any use of another object's
`__dict__` (aliased, reassigned, `|=`). A class in that hierarchy that
escapes, or whose name occurs as an unresolved reference, may have
subclasses the index cannot see (`class S(Base)` with `Base = Foo if X
else Bar`), so it leaves the attribute unbounded too. A parameter binding takes the literals of the
`__init__`'s call sites as above. When every write binds a symbol
(`self.handler = process`), `self.handler` and `self.handler.x` also get
edges (detail `self.handler`) to that symbol, alongside the name-bounded
unresolved reference an unknown attribute always records. Only when the
name is unbounded do `getattr`,
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
| `annotations_changed` | only a function's annotations differ and they are never evaluated at import (see Import time); behaviour-level, non-structural |

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
  bodies are not structural (a constant edit does not invalidate every
  function of the module), but they run at import (below).

Import time. Importing a module runs its top-level statements, variable
initialisers, class bodies, decorators and defaults, and whatever those
call; tests import at collection, and some re-import a package inside a
test (httpx, trio). So a change that runs at import (a module body change,
a variable, a class, a function's definition, an added or deleted
definition) seeds its module, and an `imports` edge carries
behaviour-level impact from the imported module to the importer: every
module and function that imports it, transitively, is affected, and
through each test's dependency on its own module, every test that
imports it. A function body change does not run at import; when
import-time code calls the function, the module's edge to it carries the
change. Import-time code includes more than top-level statements:
decorators (`@app.get("/")` builds a route handler), default values,
annotations when evaluated eagerly, and class statements (bases, class
decorators, class bodies, nested classes) all run when the module is
imported, so their references are recorded for the module as well as
for the function or class they belong to. `runpy.run_module` imports by
name like `importlib.import_module`; `runpy.run_path` is an unbounded
dynamic import. A module-level variable bound to a literal (constants,
and tuples, lists, sets or dicts of literals) with no module-level
mutation runs no code when the module is imported, so it does not seed
its module and only its readers are reached; `__all__` is never inert,
since it decides what `from m import *` binds. A `def` statement runs code at import only through its
decorators, its defaults and its annotations when they are evaluated
eagerly: a function whose decorators are inert (`overload`, `override`
or `final` imported from `typing` or `typing_extensions`; a project
decorator of the same name is not), whose defaults are literals, whose annotations are
absent or deferred (`from __future__ import annotations`), and which is
module-level or in a plain class, only binds its name, so adding,
deleting or redefining it does not seed its module (deleting or rebinding
the name reaches its users through deletion and resolution edges). An
annotation-only change to such a function is `annotations_changed`:
behaviour-level for its callers and referrers (typer, FastAPI and
pydantic read annotations when called), neither structural nor
import-time. Measured on the validated repositories: mean selection
70.3 % to 69.7 % over 171 commits; it also keeps a new test in a test
module from re-selecting the module's other tests. Measured before adoption (evaluation.md, roadmap history): mean
selection rose from 58 % to 70 % over 171 validated commits and from 63 %
to 71 % over the census; the rule follows the governing rule that a plan
may select more than needed but must not miss.

Edge rules (impact flows from the edge's target back to its source):

| Edge kind | Propagates when | Resulting mode |
|---|---|---|
| `references`, `entry`, `lifecycle`, `unresolved_name_match` | always | behaviour |
| `defined_in` | container is structurally affected | structural |
| `imports` | imported module affected (import-time code) | behaviour (structural if deleted) |
| `imports_name` | imported name was **deleted** | structural |

So: changing a function body reaches its callers and their callers; adding a
method or editing a class attribute invalidates all methods of the class;
deleting a symbol that a test module imports at module level invalidates
every test in that module; editing a module-level constant reaches the
functions that reference it and every target whose module imports the
module (it runs at import).

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
  stays always-on; the reason says which of the two fired, since "any
  module in scope may be the one" and "reachable from its module's
  imports" are different claims. A project-defined function named `vars`
  or `getattr` is not mistaken for the builtin.
* **Entry symbol or lifecycle dependency not found in either revision**: the
  target is selected (`entry_symbol_unresolved`,
  `lifecycle_dependency_unresolved`).
* **Code the runner itself runs** (`runner_dependency`). pytest's own
  process imports `_pytest`, `pytest`, `pluggy`, `iniconfig`,
  `exceptiongroup`, `tomli`, `colorama`, and `packaging.version` /
  `packaging.requirements` (discovery's `RUNNER_MODULES`); pytest calls
  pluggy's hooks for every test. When those modules, anything under them
  or their import closure are in the source roots, a changed symbol there
  selects every target of that runner (discovered or from a manifest),
  since no project code references what the runner calls. Other runners'
  targets are unaffected.
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
  facts) plus every class's resolved bases and completeness, and the
  source-root specs (whether a module's `__name__` is its indexed name
  depends on them). A body edit
  leaves it unchanged; adding, removing or renaming a symbol changes it.
* **Resolution, per module.** Pass 2 writes (edges, unresolved and
  external references, call sites, escapes, parameter tables, deferred
  parameter- and attribute-dynamic uses, whose function scopes are
  serialised, instance-attribute writes and reads, and attribute
  unbounding) go to a
  per-module output, merged into the index and stored under
  `<facts key>-<fingerprint>`. Only a module whose file or environment
  changed is parsed and resolved again. Parameter- and attribute-dynamic
  expansion needs every module's call sites and writes, so it stays a
  final pass over the merged outputs.

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
without `/` matches the basename, one with `/` the path, as pytest's
`fnmatch_ex` does -- pytest matches absolute paths, so such a pattern is
effectively prefixed with `*/` and scrapy's `test_*/__init__.py` collects
`tests/test_settings/__init__.py`; restricted to `testpaths` if set); module-level functions matching `python_functions`;
methods of classes matching `python_classes` that have no `__init__`,
including methods inherited from base classes (own definitions win): a
base defined in the same module, or imported from a module in the source
roots (networkx's `TestDiGraph(BaseGraphTester)`), whose own bases then
resolve in the module that defines it; a base that resolves to neither
yields an `unknown_base_class` note; nested test classes; methods whose
name starts with `test` in classes that reach a base ending in `TestCase`
anywhere in that chain, whatever the class itself is called (pytest's
unittest plugin collects it: django-rest-framework's
`XffSpoofingTests(XffTestingBase)`). Node ids follow
pytest (`path::Class::method`); the entry symbol of an inherited test is the
method where it is defined, and the collecting class is a lifecycle
dependency. Parameter cases are not enumerated.

A test class imported into a module is collected there with everything it
inherits, its bases resolved in the module that defines it (urllib3's
`test_pyopenssl.py` imports `TestHTTPS_TLSv1`, whose tests are nearly all
on its bases).

A test module may also re-run another's tests with `from <module> import
*`: the star binds what that module's `__all__` lists, or every name it
defines that does not start with an underscore (names it imported itself
are not followed), minus the names the importing module defines, and each
one becomes a target whose entry is where it is defined. poetry's `sync`
tests are the `install` tests imported this way.

Four notes mean the target list may be short of what the runner collects
-- `uncollected_test_class`, `unknown_base_class`,
`imported_test_out_of_scope`, `unparsed_file` and `plugin_collects_files` (a conftest or
plugin that binds `pytest_collect_file`, `pytest_collect_directory` or
`pytest_pycollect_makeitem`, as a function or a value: scrapy's
`docs/conftest.py` binds a Sybil instance and its `.rst` files become
doctests) -- as opposed to the ones that only widen a
target's dependencies. The report carries `discovery_incomplete`, `plan`
exits 3, and `run` refuses without `--allow-incomplete-discovery`: a
degraded plan runs too much, an incomplete one would run too little, and
only the second can miss.

`unparsed_file` covers two cases. A file that does not parse is also an
analysis error, so the plan degrades and selects everything. A file that
cannot be named from any source root -- a directory component that is not
a Python identifier, such as pytest-asyncio's `docs/how-to-guides` -- is
dropped instead: pytest imports a test file by its basename and collects
it regardless, so its tests are not targets. Naming that directory with a
`DIR=PREFIX` source root makes them targets (197 of pytest-asyncio's
tests become 208).

A plugin may collect what these rules do not: SQLAlchemy's testing plugin
collects `<Name>Test`, so alembic's suite is 2387 tests of which these
rules find 23. Discovery does not guess, and does not stay silent either:
a class that defines test methods but does not match `python_classes` is
reported (`uncollected_test_class`), so a plan over such a project shows
that its target list is not the suite. Targets a plugin creates have to
come from a manifest.

Fixtures are functions decorated with a dotted name ending in `fixture` or
`yield_fixture`, or module-level bindings of the form
`name = pytest.fixture(...)(function)`; `name=` and `autouse=True` keyword
arguments are honoured.
Requests follow pytest's `getfuncargnames`: parameter names minus `self`,
`request`, parameters with defaults, arguments injected by `mock.patch` /
`patch.object` decorators (unless `new` is given) on the function or, for
`test*` methods, on its class and in-module bases (each such class
decorator injects one more argument, as `unittest.mock` patches every
`test*` attribute at class definition), arguments supplied by
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

Imported tests: pytest collects every module attribute that matches the
naming rules, so a function or class imported into a test module is a
test of that module (fastapi's tutorial tests import `test_read_main` from
`docs_src`). It is named by the bound name, its entry is the defining
symbol, and its fixtures resolve from the importing module. A matching
name imported from outside the source roots is reported
(`imported_test_out_of_scope`) rather than made a target: whether pytest
collects anything from it is unknown (`unittest.TestCase` yields
nothing), and what it would run is outside the index.

Doctests, as pytest collects them (injector: `--doctest-modules
--doctest-glob=*.md`). With `--doctest-modules` in `addopts`, every
docstring with examples in a collected module (outside `norecursedirs`,
not `setup.py` or `__main__.py`) is a target named as pytest names it,
`path::module.Qualified.name`, with the owning symbol as entry. Examples
run with the module's globals, so each doctest has the lifecycle
dependency `dynamic:<module>` (and one per in-scope module an example
imports); the planner treats `dynamic:<module>` like a dynamic reference,
affected by any impact-carrying change in the module's import closure. A
docstring-only change of a target's entry symbol selects the target
(`entry_docstring_changed`; for a doctest the docstring is the test). Text
files matching `--doctest-glob` (default `test*.txt`) that contain
examples are targets whose entry is not a symbol, so they are always
selected: the index does not read them, and a change to them is invisible.

Not modelled: `request.getfixturevalue` with a name that is not a literal, fixture visibility rules of
`pytest_plugins` declared outside the root conftest (accepted anyway),
fixtures of plugins outside the well-known table, doctests of objects
added through `__test__` or assigned rather than defined, base classes
defined in other modules,
and `conftest.py` files outside the source roots.

### ASV

Collected: `.py` files under `benchmark_dir` (default `benchmarks`; read
from `asv.conf.json`, whose `//` and `/* */` comments are stripped, with an
`unparsable_config` note if it still fails to parse). ASV resolves
`benchmark_dir` against the directory holding that file, which is usually
not the repository root -- numpy and networkx keep both under `benchmarks/`,
pandas under `asv_bench/` -- so the snapshot reads `asv.conf.json` up to
three levels down and the shallowest one wins; benchmark ids stay relative
to the real benchmark directory, as `asv run --bench` expects. Files whose
path components
do not start with an underscore; functions and methods of
non-underscore classes named with `time_`, `timeraw_`, `mem_`, `peakmem_` or
`track_`. Benchmark ids are `<module relative to benchmark_dir>.<Class>.<method>`.

ASV reads a benchmark class's attributes, inherited ones included, so base
classes count: one defined in the same module, or imported from any module
in the source roots (a shared base often lives in the package under test,
not under `benchmark_dir`), is followed with the subclass's own definitions
winning. The benchmark is named after the subclass and entered at the
method that defines it. A base that resolves to neither is an
`unknown_base_class` note, which counts as incomplete discovery.

Lifecycle dependencies: the module, module-level `setup`/`setup_cache`/
`teardown`, and the class's `setup`/`setup_cache`/`teardown`, inherited ones
included. Class attributes (`params`, `timeout`, ...) reach every method
through the structural class-body rule; module attributes through the module
dependency.

Not modelled: `params` expansion, a `benchmark_dir` outside the source roots
(targets get `missing_symbol` notes).

### Declared dependencies

A registry filled at import time, a plugin resolved through entry points, a
handler named in a data file: the dependency is real and no static rule can
find it, so the project states it in `diffcone.toml` at the repository root.
It is read from *both* revisions, as every other edge is, so a commit that
deletes a declaration while changing what it pointed at still selects what
depended on it.

```toml
[[edges]]
from = "pkg.registry.dispatch"   # a symbol or a module, in either revision
to = "pkg.handlers.json_handler"
why = "handlers register themselves through entry points"
```

An endpoint may name a module or a class, which stands for everything in
it: the edge is added between every member of each side, since a change to
one member is what such a declaration is about and the container node alone
would never see it. Each edge joins the union graph with its own kind, and a
selection whose path crosses one is explained by the `declared_dependency`
rule with the project's `why` on the step, never as an ordinary dependency
(a name match anywhere on the path outranks it: that one is diffcone's
guess, this one is the project's statement).

Declarations only *add* edges, so they can only widen selection: a wrong one
costs a test that runs anyway, and no declaration can make the plan miss.
These are analysis errors, so the plan degrades and selects everything,
because a declaration that silently does nothing is the outcome worth
failing on: a file that does not parse, an unknown key at any level (a
singular `[[edge]]` declares nothing), a file that exists but cannot be
read, an endpoint that is in neither revision, and a pair of containers
whose members would join more than 5 000 pairs.

Telling diffcone that a dynamic reference reaches *only* certain modules is
the opposite trade (it narrows on the project's authority) and is not
implemented; see the roadmap.

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
  the installed revision. A relative executable in `--command`
  (`.venv/bin/python`) is resolved against the current directory and then
  the repository before the suites run in their temporary worktrees, and a
  missing executable is a plain error. With `--coverage` this is verified: a measured
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
  The base run's database is kept as well (in a corpus, per commit
  alongside its cached outcomes) and read with the line ranges of the
  changed symbols *as they were at base*, so a test that executed a symbol
  deleted or moved in head is attributed too; a test that no longer exists
  at head is `removed`, not affected, since nothing could select it.
  A test is *dynamically affected* when it executed a line owned by a
  changed symbol, at either side, whose change carries impact
  (additive-only changes are not ground truth: nothing executed behaves
  differently). Ownership
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
* Import-time effects on the module's own members: a module body change
  reaches importers, but does not invalidate every function defined in
  the same module (functions that read module state have their own edges).
* Walrus assignments inside comprehensions bind in the enclosing scope in
  Python but are treated as comprehension-local here.
* Decorators that rewrite the decorated function are treated as ordinary
  references.
* Parameterised targets are selected as a whole.
* Argument-insensitive: a CLI entry point that registers every subcommand
  handler (argparse `set_defaults(func=...)`, a dict of callables)
  references all of them, so every test that drives the CLI is selected
  on any command change regardless of the arguments it passes (pipx in
  evaluation.md). Which handler a test reaches is a property of its
  argument strings, which are not analysed.
* Instance attributes assume `__init__` ran: an object built without it
  (`object.__new__`, a subclass whose `__init__` neither writes the
  attribute nor calls `super().__init__`) has no instance value, and a
  construction through a path the index cannot see (a class taken from a
  registry by unresolved name, code outside the source roots) passes
  arguments that are not counted.
* Any file under a source root that does not parse (invalid syntax,
  Python 2 code) is an analysis error and forces select-all, even a data
  file that no module imports and pytest never collects (pygments and
  black in the census). Python 2 is not supported. Choose source roots
  that leave such files out. A file is read in the encoding it declares:
  a PEP 263 coding cookie or a BOM, UTF-8 otherwise, as Python itself
  reads it (`tokenize.detect_encoding`), so a legitimately latin-1 file
  is analysed rather than failing to decode.
* Special methods are reached through their class: an instance obtained
  without any reference to its class in the source roots (from external
  code, `pickle`, `copy`) does not connect its user to the class's
  special methods.
* External code calling methods by protocol without inheritance: an
  object with no base outside the source roots but handed to external
  code (a file-like object passed to `json.load`, a callback object)
  has the methods that code calls reached only through name matches.
