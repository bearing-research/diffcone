# Limitations

diffcone is built to select too much rather than too little. These are the
places where that shows, and the edges of what it analyses.

- **Uncommitted analysis is explicit.** A `WORKTREE` or `INDEX` snapshot is
  named as such in the report. Results for a working tree are only as
  stable as the working tree.
- **Discovery is static, and says where it stops.** It cannot reproduce
  what a runner plugin collects by its own rules (SQLAlchemy's
  `<Name>Test`, a Sybil doctest in a `.rst` file), and it does not expand
  parameters into separate cases. What it cannot see it reports, and a plan
  whose target list may be short exits `3` rather than looking complete
  ([discovery](reference/discovery.md)).
- **A narrow, documented resolution subset.** Direct names and attribute
  chains rooted at module-level definitions, import aliases, star imports
  within the source roots, or `self`/`cls`, with class attributes looked up
  through the in-scope MRO (`super()` included). No type inference, and no
  dispatch on receivers of unknown type: an instance attribute is resolved
  only when every write is a plain `__init__` assignment of a literal, a
  function or class, or a constructor argument every construction passes
  as a literal. Other references are reported as unresolved and matched by
  name against every known function, method or class of that name, so a
  change reaches them whenever any such symbol is affected.
- **Removing or redirecting an import invalidates the whole importing
  module** (adding one does not), and any change to a class body
  invalidates every method of the class.
- **Import-time changes select every importer.** A change that runs when a
  module is imported (a top-level statement, a module-level constant, a
  class body, a decorator, a function that import-time code calls) selects
  every target whose module imports that module, directly or not. In a
  library where everything imports a central module, that is nearly
  everything; [execution evidence](guides/evidence.md) is the remedy.
- **A file that does not parse forces select-all.** Any file under a source
  root with invalid syntax (Python 2 code included) or that is not UTF-8 is
  an analysis error, even a test data file nothing imports. Choose source
  roots that leave such files out.
- **Dynamic reflection is bounded by imports; dynamic imports are not.** A
  function using `eval`, `exec`, `globals()`, `vars()` or `getattr` with an
  unbounded name is affected by any change in a module its own module
  imports, transitively; `__import__` or `import_module` with an unbounded
  name is affected by any change anywhere.

How each rule works, and why, is in the [design](design.md); how often
these cost precision on real repositories is in the
[evaluation](evaluation.md).
