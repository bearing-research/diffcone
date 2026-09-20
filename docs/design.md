# Diffcone design (milestone 1)

This document describes what the current implementation does. Planned work
lives in [roadmap.md](roadmap.md).

## Pipeline

```
Git snapshot reader        diffcone/snapshot.py
  -> Source index          diffcone/indexer.py   (symbols, hashes, edges, unresolved refs)
  -> Change classifier     diffcone/classify.py
  -> Impact planner        diffcone/planner.py
  -> Reports               diffcone/report.py    (JSON and text)
```

Each stage exchanges plain dataclasses (`diffcone/model.py`). The planner
does not import pytest or ASV; targets are opaque manifest records.

### Snapshot reader

`read_snapshot(repo, revision, source_roots)` resolves the revision to a
commit, lists `.py` files under the source roots with `git ls-tree`, and
reads their contents with `git cat-file --batch`. Nothing is checked out and
the working tree is never consulted. Unknown revisions are fatal (exit 2).

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
   class; `self.m` resolves to `Class.m` if the class itself defines it.
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
variable. A step that cannot be taken yields an unresolved attribute
reference bounded by the attribute name. Once a chain reaches a function or
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
  attribute `NAME` in a symbol, and every changed symbol whose short name is
  `NAME`, the planner adds an `unresolved_name_match` edge. `obj.save()` is
  therefore affected when *any* `save` changes. The report lists each
  unresolved reference with its matches.
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

## Known gaps (by design, this milestone)

* Inheritance: `self.m` where `m` is inherited is unresolved (name-bounded).
* Module init side effects: a body change in module init does not invalidate
  the module's own members or importers that do not reference its state.
  Use a module lifecycle dependency where that matters.
* Walrus assignments inside comprehensions bind in the enclosing scope in
  Python but are treated as comprehension-local here.
* Decorators that rewrite the decorated function are treated as ordinary
  references.
* Parameterised targets are selected as a whole.
