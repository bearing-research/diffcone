"""What pytest runs for the whole session beyond conftest hooks.

Two kinds of code reach every test without a dependency edge leading there:

* Hooks of a plugin *object*: a class registered with
  ``config.pluginmanager.register(Obj())`` is a plugin like a module, and
  pytest calls its ``pytest_*`` methods for every node (only conftests are
  scoped to a directory). Which class a registration hands over is not
  traced (``register(self)``, ``register(session)``, an object kept in a
  list), so every ``pytest_*`` method of an in-scope class counts
  (``plugin_object_hooks``), except a test class's own
  ``pytest_generate_tests``, which pytest calls for that class's tests. A
  module registered by name (``import_plugin("pkg.plug")``) is a plugin
  module like one in ``pytest_plugins`` (pytest_static).
* Code that runs while pytest collects, before any test, and writes
  process-global state outside the source roots (``os.environ``,
  ``sys.path``, warning filters: indexer/process.py): the import-time code
  of every conftest and test module and of every module they import, and
  the collection hooks of conftests and test modules
  (``pytest_generate_tests`` in ``tests/sub/conftest.py`` runs only for the
  tests under ``sub``, but the environment variable it sets stays set for
  every later test). Whoever reads that state cannot be bounded (any
  library may read an environment variable), so such code is a dependency
  of every test (``state_writing_roots``). It counts when it writes, or can
  run code that writes, through references, declared edges, name matches
  of its unresolved calls, and dynamic references (an unbounded import, a
  lookup on an external module in-scope code writes to, or one whose
  module's import closure holds a writer, as the planner bounds them).
  What a module does only under ``if __name__ == "__main__":`` does not run
  when pytest imports it (``SourceIndex.main_guarded``).

Not modelled: a hook implementation under another name
(``@pytest.hookimpl(specname=...)``), a module object registered at run
time (``pluginmanager.register(module)``, ``import_plugin`` with a name
computed at run time), and another library's configuration call missing
from indexer/process.py.
State written into in-scope variables needs none of this: those writes are
``mutated_by`` edges, and readers reach the variable. Code that runs inside
a test (a fixture, ``pytest_runtest_setup``) and leaves state behind for
later tests is outside the model: tests are assumed not to depend on what
earlier tests left.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from diffcone.model import (
    CLASS,
    DECLARED,
    EXTERNAL_WRITTEN,
    IMPORTS,
    INSTALLED,
    INSTALLED_ANYWHERE,
    METHOD,
    MODULE,
    REFERENCES,
    UNRESOLVED_DYNAMIC,
    SourceIndex,
)

# Path-scoped hooks pytest calls while collecting, before any test runs.
COLLECTION_HOOKS = frozenset(
    {
        "pytest_generate_tests",
        "pytest_make_parametrize_id",
        "pytest_collect_file",
        "pytest_collect_directory",
        "pytest_pycollect_makemodule",
        "pytest_pycollect_makeitem",
        "pytest_ignore_collect",
        "pytest_collectstart",
        "pytest_make_collect_report",
        "pytest_itemcollected",
        "pytest_collectreport",
    }
)

# Edges that do not mean the source's code can run the target's (a module
# does not run what installs an object under its name).
_NOT_RUN = frozenset({"mutated_by", "registers", INSTALLED, INSTALLED_ANYWHERE})
# Edges from a class to a member that whoever uses an instance runs (a
# special method, a method an external base calls); the class's other
# edges are its body's code, which runs when its module is imported.
_INSTANCE_EDGES = frozenset({"special_method", "external_base"})


def plugin_object_hooks(index: SourceIndex, test_modules: set[str]) -> list[str]:
    """The ``pytest_*`` methods of in-scope classes: hooks of a plugin
    object pytest may register, called for every test."""
    out: list[str] = []
    for symbol_id, symbol in sorted(index.symbols.items()):
        if symbol.kind != METHOD or not symbol.name.startswith("pytest_"):
            continue
        if symbol.name == "pytest_generate_tests" and symbol.module in test_modules:
            continue  # a test class parametrizing its own tests
        out.append(symbol_id)
    return out


def state_writing_roots(
    index: SourceIndex, modules: Iterable[str], hooks: Iterable[str]
) -> list[str]:
    """Of the modules pytest imports while collecting (``modules`` and what
    they import at module level, transitively) and the collection ``hooks``,
    those whose code writes process-global state or can run code that
    does."""
    writers = {s for s, _ in index.process_writes if s in index.symbols}
    if not writers:
        return []
    callers: dict[str, set[tuple[str, str]]] = defaultdict(set)
    imported: dict[str, set[str]] = defaultdict(set)
    importers: dict[str, set[str]] = defaultdict(set)
    main_only = index.main_guarded
    for edge in index.edges:
        if (edge.source, "edge", edge.target) in main_only:
            continue  # only under ``if __name__ == "__main__":``
        if edge.kind in (REFERENCES, DECLARED) and edge.detail not in _NOT_RUN:
            callers[edge.target].add((edge.source, edge.detail))
        elif edge.kind == IMPORTS:
            imported[edge.source].add(edge.target)
            importers[edge.target].add(edge.source)
    # A dynamic reference reaches what its module's import closure holds (as
    # the planner bounds it), anything when it imports by a name only known
    # at run time or looks something up on an external module in-scope code
    # writes to.
    writing_closures: set[str] = set()
    stack = sorted({index.symbols[s].module for s in writers})
    while stack:
        module = stack.pop()
        if module not in writing_closures:
            writing_closures.add(module)
            stack.extend(importers.get(module, ()))
    by_name: dict[str, set[str]] = defaultdict(set)
    dynamic: set[str] = set()
    for ref in index.unresolved:
        if (ref.symbol, ref.kind, ref.name or ref.detail) in main_only:
            continue
        if ref.kind == UNRESOLVED_DYNAMIC:
            symbol = index.symbols.get(ref.symbol)
            if symbol is not None and (
                "import" in ref.detail
                or ref.detail.endswith(EXTERNAL_WRITTEN)
                or symbol.module in writing_closures
            ):
                dynamic.add(ref.symbol)
        elif ref.name:
            by_name[ref.name].add(ref.symbol)
    # Backwards from the writers: whatever can run one. A module's own code
    # runs when it is imported, not when something names it, so a module is
    # not followed further back; nor is a class whose body runs a writer (it
    # counts for its module). A class reached through one of its special
    # methods runs it for whoever uses the class.
    reach: set[str] = set()
    queue: deque[str] = deque()

    def add(symbol_id: str) -> None:
        if symbol_id in reach or symbol_id not in index.symbols:
            return
        reach.add(symbol_id)
        queue.append(symbol_id)

    for symbol_id in sorted(writers | dynamic):
        add(symbol_id)
    while queue:
        current = queue.popleft()
        symbol = index.symbols[current]
        if symbol.kind == MODULE:
            continue
        if symbol.kind == CLASS and current in writers | dynamic:
            add(symbol.module)  # its body writes, when the module is imported
            continue
        for caller, detail in sorted(callers.get(current, ())):
            caller_symbol = index.symbols.get(caller)
            if caller_symbol is None:
                continue
            if caller_symbol.kind == CLASS and detail not in _INSTANCE_EDGES:
                add(caller_symbol.module)  # class-body code
            else:
                add(caller)
        if not (symbol.name.startswith("__") and symbol.name.endswith("__")):
            for caller in sorted(by_name.get(symbol.name, ())):
                caller_symbol = index.symbols.get(caller)
                if caller_symbol is not None and caller_symbol.kind == CLASS:
                    add(caller_symbol.module)
                else:
                    add(caller)
    # The modules imported while collecting.
    closure: set[str] = set()
    stack = [m for m in modules if m in index.symbols]
    while stack:
        module = stack.pop()
        if module in closure:
            continue
        closure.add(module)
        stack.extend(t for t in imported.get(module, ()) if t not in closure)
    return sorted((closure | set(hooks)) & reach)
