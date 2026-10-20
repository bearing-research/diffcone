"""Evidence-mode planning: select the tests whose recorded run meets a change.

Evidence recorded at commit C says which symbols each test executed and
which repository paths it touched (evidence.py). A test that runs
identically at C and at a snapshot cannot differ there, so a plan base ->
head is the union of two plans from C: C -> base and C -> head (one when C
is the base). For each, every change is turned into E, the symbols whose
execution would *notice* it (internal/evidence_design.md, "What a change is
observed by"), and a test is selected when its record meets E:

* a function body: the function itself; if it ran during an import, the
  importing module's import-time state may differ, so that module is
  escalated (below). If it ran while pytest collected a test module
  (``Evidence.collect_by``: parameter ids, ``ids=`` callables,
  ``pytest_generate_tests``), the tests collected from that module's file
  are selected (``_collected``, rule ``collection_code``). Code that ran
  outside every test, import and such collection (hooks) selects
  everything (below). Each may also have written into another module's
  state, which tests read without running it: the variables written in
  place by what it can call are changed values (``_import_writes``), and
  so are those of a changed variable statement, class body or ``def``
  header that runs code;
* a function's signature, defaults, decorators or annotations: also its
  readers (resolved references and name matches, one hop), the lookup and
  reflection sites that can see its namespace, and escalation when the
  ``def`` runs code at import (``_runs_at_import``: decorators, defaults,
  and annotations that call something, which before Python 3.14 are
  evaluated with the ``def``). A name match on a member of a class in test
  code, and a library lookup by a name nothing bounds, is guarded: it
  selects a test only if the test also ran code that can hand it an
  instance (``_Observers._holders``), which includes code finding classes
  without naming them (``__subclasses__``, ``gc``). An attribute read off a
  value of unknown type matches a module-level name only of a module such
  a value may be (``SourceIndex.attribute_modules``, W20); of a module the
  test runner holds, it is guarded by code that can obtain the module
  (``_module_guard``);
* a variable: its readers and the lookup and reflection sites that can see
  its namespace (a lookup by a name nothing bounds reads the value too); a
  reader that is itself a variable captured the value and is followed in
  turn; a module's or class's top-level reader escalates its module. An
  initialiser that runs code, or an annotation evaluated at import, may
  write elsewhere (``_import_writes``);
* a class body: the attributes whose statements changed, through every
  reader of those names and the lookup and reflection sites; an opaque
  body statement, a dunder attribute or a changed class statement (bases,
  decorators, keywords) escalates, and so does any attribute change of a
  class whose creation may read its body (``SourceIndex.open_classes``:
  decorators, keywords, a base the index cannot see, such as a dataclass
  field default or an ``Enum`` member; or an ancestor with
  ``__init_subclass__``), and a reorder of the body's statements
  (``Symbol.statement_layout``; R6). A special method (``__eq__``) is used without
  being named, so a change to one reaches every member of the class's
  hierarchy; a module's ``__getattr__``/``__dir__`` escalates its module;
* an added or deleted name: its readers, the unbounded lookup and
  reflection sites that can see the namespace, and for a deletion the
  modules importing it (their import now fails); an added or deleted
  definition that runs code at import (a decorated function, one whose
  defaults or annotations call code, a class whose creation runs code)
  escalates too;
* module-level code: escalates the module, and so does a change to
  ``__all__`` (what star imports bind);
* a docstring only: the code reading docstrings (``reads_docstrings``)
  that refers to the symbol or sits in its module, or reads a docstring
  off a value it does not name (``reads_any_docstring``, not a decorator:
  R7), as in static planning;
  such a reader running at import escalates its module. A docstring its
  decorator reads at import (``doc_decorated``: pandas' ``@doc``)
  escalates the change;
* a non-Python file, or any file outside the source roots (read from a
  git diff, since the index holds only the roots): the tests that touched
  it or a directory above it (one listed, for an added or deleted file);
  touched while a module was imported, that module is escalated; while
  pytest collected a test module, that module's tests are selected and the
  code that ran then is followed as if it had changed. Everything when it
  was touched outside every test, import and test module (a hook), or is
  compiled source, build, dependency or pytest
  configuration (``build_input``), or a ``conftest.py`` outside the
  roots, or a ``.py`` file the index does not read (outside the roots, or
  ``[index] exclude``d) whose code ran inside a test
  (``Evidence.test_window_code``; for an older store, any ``.py`` file a
  test touched: ``unindexed_import``), since later tests use the module
  from ``sys.modules`` without running it. A Python source file is data
  to the tests that opened it as a file (``inspect.getsource``) too; a
  module's file selects everything only when it is a build script as
  static planning judges it
  (planner.is_build_script: ``setup.py``, a ``build.py`` at the root or
  beside the project's metadata, not ``pkg/build.py``);
* a Cython source (``.pyx``, ``.pxd``, ``.pxi``), at function level: a
  changed body reaches the tests that ran the function in a profiled
  build, and for one such a build does not always report (``nogil``,
  ``cpdef``, ``profile(False)``) those that ran a Cython function naming
  it; a changed name bound outside the functions reaches the code that
  can see it and names it; anything else selects everything
  (``_cython``);
* test code other than a function body: the tests that list the symbol as
  a lifecycle dependency (a fixture) or are collected from its class or a
  subclass, and for a test module's variable (``pytestmark``) every test
  of the module, since pytest reads marks, fixtures and parameters without
  a static reader. ``pytest_plugins`` in any module selects everything
  (the plugins it names serve the whole session), and so do a
  ``pytest_*`` function anywhere (a hook) and a conftest's
  ``collect_ignore`` or ``collect_ignore_glob`` (what pytest collects).

Code that ran at C in a hook, changed or reading a change, selects
everything: what a hook leaves behind (an environment variable) reaches
tests no static path leads to; a test's or fixture's own ``ids=`` callable
is the exception (``_collects_itself``). Code that ran only while pytest
collected test modules (and in no hook) selects those modules' tests, and
what it writes is followed as for code run at import; it counts as a hook's
when the code that ran it then, or what that code can run, writes
process-global state or can reach pytest's session-wide objects (a
``pytest_*`` hook handed the config, code reading ``.config``,
``.session``, ``.stash`` or ``.pluginmanager``: ``_collection_reach``). A
lookup site in E is a reader too, followed into the import, collection or
hook that ran it. Once E is
built, the variables written in place by code that can run a member of E,
and by the code that code calls, transitively (``_handed_to``), whatever
they store (a cache an earlier test filled), are changed values for the code reading
them inside tests (``_stored_results``); code in that chain storing
through a receiver, a closure or a function's or class's attribute
(``SourceIndex.stores``) is read by every test that executed it, and
code whose decorator keeps its result (``SourceIndex.memoized``:
``cached_property``) by its readers. A target whose lifecycle
dependencies differ between C and either snapshot is selected
(``lifecycle_changed``).

Neither rule counts a lazily initialised constant's own writer as a reader
of its fill (``SourceIndex.lazy_constants``: ``global V; if V is None: V =
<pure value>`` in the one function writing ``V``; ``_lazy_writer``): it
computes the same value whoever runs it first, so code running it at
import, in a hook or in an earlier test leaves nothing it reads. Other code
reading ``V`` is followed as before; a change to the writer is planned as
any change.

A module a change newly imports, or no longer imports (an import a
module's top-level code or a function's body gains or loses, with the
imported module's import closure, less what the importing module's
closure ran already: ``planner._newly_run_modules``, audit round 5, R24)
ran at C for no test, or will run for none: what its
import-time code writes into is a changed value for its readers, and code
reading the module or its members that its own imports never reach now
finds, or no longer finds, it (``_newly_run``).

The modules imported before the recorder started (``Evidence.preloaded``)
ran their import-time code where no record shows it: any change in one
is escalated, with the module (``_preloaded``).

Something that ran outside every test and that no record shows (a process
nothing followed, text code: ``Evidence.import_flagged``) makes any change
escalate the module whose import ran it, or, outside every import, select
everything. A test that ran text code (a doctest) is always selected, as
one that started a process no record follows is.

"Escalated" means planned by the static planner from exactly those seeds,
without dynamic-reference pseudo-seeds: a test that executed a dynamic
site is in the record through the code it reached, so the lookup sites
that can see an escalated module join E instead. Every symbol of an
escalated module joins E too. A lookup on an external module that only
code running inside tests writes to joins E only for a change to code that
runs at import (which may now write there): what it finds of the
project's, a test's record holds through the writer
(``_Observers._only_in_tests``, ``import_sites``). Any such lookup joins
E for an import-time change only when the changed code can run one of its
writers (``_forward``).

A test module's names are seen by code that can obtain the module without
importing it: an import of a module named at run time whose module is
kept (an unpickler, a module loaded from a path nothing bounds), a graph
walk (``gc`` functions that hand objects out), and an import whose name a
literal table bounds to no test module only once code that may change the
table ran (``table_getters``, guarded by those writers).

Targets of other runners, which have no evidence, keep their static
decision. A pytest target with no record (new, or never run) is selected.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path

from diffcone.child import SCANNED
from diffcone.classify import (
    ChangeKind,
    SymbolChange,
    _import_closures,
    classify,
    reordered,
)
from diffcone.cython import ASSIGNMENT as CYTHON_ASSIGNMENT
from diffcone.cython import CLASS as CYTHON_CLASS
from diffcone.cython import DECLARATION as CYTHON_DECLARATION
from diffcone.cython import IMPORT as CYTHON_IMPORT
from diffcone.cython import (
    CythonFunction,
    CythonName,
    cython_changes,
    is_cython,
    names_module,
    pxd_stem,
    symbol_id,
)
from diffcone.declarations import Declaration
from diffcone.discovery import DiscoveryResult
from diffcone.evidence import (
    COLLECTED,
    FLAG_FAILED,
    FLAG_SUBPROCESS,
    FLAG_TEXT,
    FLAG_UNSTABLE,
    UNINDEXED_MODULE,
    Evidence,
    python_version,
)
from diffcone.manifest import Manifest, Target
from diffcone.model import (
    ATTRIBUTE_KINDS,
    CLASS_STATEMENT,
    DERIVED_ANY,
    DERIVED_NAME,
    DISCARDED_IMPORT,
    EXTERNAL_WRITTEN,
    GRAPH_HANDLE,
    INSTALLED,
    INSTALLED_ANYWHERE,
    MUTATED_BY,
    OPAQUE_ATTRIBUTE,
    REBINDS,
    REGISTERS,
    RUN_PATH,
    RUNS_PROGRAM,
    RUNS_SCRIPT,
    RUNS_TEXT,
    STORE_CLOSURE,
    STORE_PARAM,
    STORE_SELF,
    WRITES,
    EdgeKind,
    SourceIndex,
    Symbol,
    SymbolKind,
    UnresolvedKind,
    attribute_reachable,
    edge_mark,
    imports_anything,
)
from diffcone.planner import (
    ANY_DOCSTRING,
    OWN_DIRS,
    OWN_FILES,
    Decision,
    Fallback,
    LifecycleChanges,
    Plan,
    Reason,
    Rule,
    Seeds,
    StepKind,
    _changed_unanalysed_files,
    _decision,
    _ImportReach,
    _inside,
    _is_dunder,
    _members_by_container,
    _newly_run_modules,
    _on_unknown_value,
    _project_dirs,
    _runner_dependency_fallbacks,
    build_input,
    docstring_readers,
    is_build_script,
    isolated_test_code,
    lifecycle_reason,
    merge_targets,
    plan_from_indexes,
)
from diffcone.snapshot import changed_paths, split_root

# Dynamic references that stand for a child process (diffcone.indexer.scripts).
CHILD_PROGRAMS = (RUNS_PROGRAM, RUNS_SCRIPT)

# Module-level names in a conftest that pytest reads to decide what to load
# or collect at all.
PYTEST_COLLECTION_NAMES = frozenset({"pytest_plugins", "collect_ignore", "collect_ignore_glob"})

# Attributes through which code reaches pytest's session-wide objects (the
# config, its stash and cache, the plugin manager): read off a node, a
# ``metafunc`` or a request, they let code run while one module is collected
# leave state for hooks that act on every test.
SESSION_ATTRIBUTES = frozenset({"config", "session", "stash", "pluginmanager"})

# Attributes that hand out objects without naming them: a class's
# subclasses, a function's or frame's globals, a closure cell, the objects
# the collector tracks (read off a ``gc`` module handed on: the module itself
# is model.GRAPH_MODULES, and its members that hand out nothing are
# model.GRAPH_INERT).
GRAPH_ATTRIBUTES = frozenset(
    {
        "__subclasses__",
        "__globals__",
        "f_globals",
        "f_locals",
        "cell_contents",
        "get_objects",
        "get_referrers",
        "get_referents",
        "garbage",
    }
)

# What calling or creating a class runs.
_CONSTRUCTORS = frozenset(
    {"__new__", "__init__", "__post_init__", "__call__", "__init_subclass__", "__set_name__"}
)


# The kinds of dynamic lookup (UnresolvedKind.DYNAMIC details), by what the site can see.
class SiteKind(StrEnum):
    """What a lookup site can see."""

    CLOSURE = "closure"  # a name looked up on a module global: its import closure
    ELSEWHERE = "elsewhere"  # on an object from anywhere
    IMPORT = "import"  # a module named at run time
    ANY = "any"  # eval/exec/run_path


def _site_kind(detail: str) -> SiteKind:
    if detail.endswith(EXTERNAL_WRITTEN):
        # On an external module: in-scope code anywhere may have stored there,
        # not only the site's own import closure.
        return SiteKind.ANY
    if detail.startswith(RUN_PATH):
        # Runs a file whose code may look anything up, not only import.
        return SiteKind.ANY
    if detail.startswith(RUNS_TEXT):
        # Text built at run time: what it imported and ran is in the record
        # (or the test is flagged for running text no file holds), as for
        # ``exec()`` of text written out; no module getter.
        return SiteKind.ANY
    if imports_anything(detail):
        return SiteKind.IMPORT
    if detail.startswith(("getattr(<non-literal>) on a receiver from elsewhere", "vars(")):
        return SiteKind.ELSEWHERE
    if detail.startswith(("getattr(<non-literal>)", "globals(")):
        return SiteKind.CLOSURE
    return SiteKind.ANY  # eval, exec, runpy.run_path, anything not recognised


def _definition_runs_code(symbol: Symbol) -> bool:
    """Whether executing ``symbol``'s definition at import runs code beyond
    binding the name. A ``def`` runs its decorators and defaults
    (``inert_header``) and, before Python 3.14, its annotations, unless they
    are deferred (``from __future__ import annotations``) or absent
    (``inert_definition``) or call nothing (``quiet_header``):
    ``def f(x: set_mode("fast"))`` calls project code at import, ``def f(x:
    Item)`` only looks a name up (audit round 2, F3; round 4, Q3). A
    variable runs its initialiser (``inert_definition``: a literal runs
    nothing) and an annotation evaluated at import
    (``X: set_mode("fast") = 1``), which its ``inert_definition`` does not
    cover."""
    if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
        return not (symbol.inert_header and (symbol.inert_definition or symbol.quiet_header))
    if symbol.kind == SymbolKind.VARIABLE:
        eager = bool(symbol.annotation_hash) and not symbol.deferred_annotations
        return not symbol.inert_definition or eager
    return True


def _runs_at_import(change: SymbolChange) -> bool:
    """planner._runs_at_import, as evidence mode bounds a ``def``
    (``_definition_runs_code``): module bodies and class bodies run at
    import; a function's change does when it adds, deletes or changes a
    ``def`` whose header runs code on either side (a body edit, or a
    deferred annotation's, does not); a variable's when its definition
    changed (an annotation evaluated at import), or its statement runs code
    on either side. An unchanged annotation runs the same code again: a
    variable's evaluated annotation matters only to its addition or
    deletion."""
    symbol = change.symbol
    kinds = set(change.changes)
    if symbol.kind in (SymbolKind.MODULE, SymbolKind.CLASS):
        return True
    sides = [s for s in (change.base, change.head) if s is not None]
    if symbol.kind == SymbolKind.VARIABLE:
        if ChangeKind.DEFINITION_CHANGED in kinds:
            return True
        if not all(s.inert_definition for s in sides):
            return True
        return bool(kinds & {ChangeKind.ADDED, ChangeKind.DELETED}) and any(
            _definition_runs_code(s) for s in sides
        )
    if not kinds & {ChangeKind.ADDED, ChangeKind.DELETED, ChangeKind.DEFINITION_CHANGED}:
        return False
    return any(_definition_runs_code(s) for s in sides)


# ``_Observers._related``: which way to follow the class graph.
BASES = "bases"
SUBCLASSES = "subclasses"


def _ancestors(path: str) -> list[str]:
    """The path and every directory above it, the checkout root as ""."""
    out = [path]
    while "/" in path:
        path = path.rsplit("/", 1)[0]
        out.append(path)
    out.append("")
    return out


def _scanned(path: str) -> str:
    """The listing record of the directory holding ``path`` scanned with
    ``os.scandir`` (``child.SCANNED``): its entries' metadata may have been
    read without an event, so an edit to ``path`` was seen there."""
    return (path.rsplit("/", 1)[0] if "/" in path else "") + SCANNED


class _TestCode:
    """Which modules are test code: those holding a pytest target (by file or
    entry symbol) and every conftest, and which tests each one scopes."""

    def __init__(self, targets: list[Target], indexes: Iterable[SourceIndex]) -> None:
        self.module_of_path: dict[str, str] = {}
        self.path_of_module: dict[str, str] = {}
        for index in indexes:
            for symbol in index.symbols.values():
                if symbol.kind == SymbolKind.MODULE:
                    self.module_of_path[symbol.path] = symbol.id
                    self.path_of_module[symbol.id] = symbol.path
        self.tests_by_file: dict[str, list[str]] = defaultdict(list)
        self.entries: set[str] = set()
        self.modules: set[str] = set()
        # Symbol -> the tests listing it as a lifecycle dependency (fixtures,
        # setup functions, conftest hooks: discovery's fixture chain).
        self.users: dict[str, list[str]] = defaultdict(list)
        for target in targets:
            file = target.runner_id.split("::", 1)[0]
            self.tests_by_file[file].append(target.runner_id)
            self.entries.add(target.entry_symbol)
            for dep in target.lifecycle_dependencies:
                self.users[dep].append(target.runner_id)
            if file in self.module_of_path:
                self.modules.add(self.module_of_path[file])
        self.conftests = {m for p, m in self.module_of_path.items() if _is_conftest(p)}
        # Code that runs only inside one test (planner.isolated_test_code):
        # under the isolation assumption, what it stores is seen by that
        # test alone.
        self.isolated: frozenset[str] = frozenset()

    def entry_modules(self, symbols: dict[str, Symbol]) -> None:
        for entry in self.entries:
            symbol = symbols.get(entry)
            if symbol is not None:
                self.modules.add(symbol.module)

    def is_test_code(self, module: str) -> bool:
        return module in self.modules or module in self.conftests

    def scope(self, symbol: Symbol, classes: set[str], *, whole_module: bool) -> list[str]:
        """The tests a change to ``symbol`` in test code reaches without a
        static reader:
        - every test listing the symbol among its lifecycle dependencies
          (a fixture, as discovery resolves it at head, autouse included);
        - every test collected from ``classes``, the class holding the
          symbol and its subclasses (a fixture or mark on a base test class
          in another file reaches every subclass's tests);
        - with ``whole_module``, every test of the module (``pytestmark``).
        Tests that ran a fixture at C have it in their record, which covers
        one that was deleted or stopped applying to them."""
        tests = set(self.users.get(symbol.id, ()))
        for cls in classes:
            tests.update(self.users.get(cls, ()))
        if whole_module:
            tests.update(self.tests_by_file.get(self.path_of_module.get(symbol.module, ""), ()))
        return sorted(tests)


def _class_graph(
    indexes: Iterable[SourceIndex],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Bases and subclasses of every class, over both revisions."""
    bases: dict[str, set[str]] = defaultdict(set)
    subclasses: dict[str, set[str]] = defaultdict(set)
    for index in indexes:
        for cls, names in index.class_bases.items():
            for base in names:
                bases[cls].add(base)
                subclasses[base].add(cls)
    return bases, subclasses


def _closure(starts: Iterable[str], graph: Mapping[str, Iterable[str]]) -> set[str]:
    """``starts`` and every class reachable from them in ``graph`` (class ->
    its bases, or -> its subclasses)."""
    found = set(starts)
    stack = list(found)
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in found:
                found.add(nxt)
                stack.append(nxt)
    return found


def _runner_only(c_index: SourceIndex, other: SourceIndex, targets: list[Target]) -> set[str]:
    """Test classes (with the bases they inherit from) whose instances only
    pytest ever holds: planner._runner_only_classes, with three differences.
    A class that is only ever a base of test classes (pandas' ExtensionTests
    combines a dozen Base*Tests) counts too, since its instances are the
    test classes' instances. A read of ``.instance`` does not turn the rule
    off: the tests that executed one are selected instead
    (``_Observers._seeing_sites``, ``hands_on``). And the runner's classes
    are those of every pytest target, a manifest's as well as discovery's
    (the static rule reads discovery's only)."""
    symbols = {**c_index.symbols, **other.symbols}
    bases, _ = _class_graph((c_index, other))
    runner: set[str] = set()
    for target in targets:
        entry = symbols.get(target.entry_symbol)
        if entry is not None and entry.kind == SymbolKind.METHOD and entry.container:
            runner.add(entry.container)
        for dep in target.lifecycle_dependencies:
            owner = symbols.get(dep)
            if owner is not None and owner.kind == SymbolKind.CLASS:
                runner.add(dep)
    runner = _closure(runner, bases)
    if not runner:
        return set()
    held = (c_index.escaped_classes | other.escaped_classes) & runner
    for index in (c_index, other):
        for edge in index.edges:
            if (
                edge.kind == EdgeKind.REFERENCES
                and edge.target in runner
                and not _inside(edge.source, runner, c_index, other)
            ):
                held.add(edge.target)
    # A held subclass holds its bases: its instances carry their methods.
    return runner - _closure(held, bases)


def _is_conftest(path: str) -> bool:
    return path == "conftest.py" or path.endswith("/conftest.py")


class _Observers:
    """E for one pair of snapshots (C -> other), and what else it selects."""

    def __init__(
        self,
        c_index: SourceIndex,
        other: SourceIndex,
        evidence: Evidence,
        test_code: _TestCode,
        declarations: list[Declaration],
        runner_only: set[str],
    ) -> None:
        self.c, self.other = c_index, other
        self.runner_only = runner_only
        self.evidence = evidence
        self.test_code = test_code
        self.symbols: dict[str, Symbol] = {**c_index.symbols, **other.symbols}
        self.changes = classify(c_index, other)
        self.readers_of: dict[str, set[str]] = defaultdict(set)
        self.attribute_readers: dict[str, set[str]] = defaultdict(set)  # "attribute:n" edges
        self.importers_of: dict[str, set[str]] = defaultdict(set)
        self.by_name: dict[str, set[str]] = defaultdict(set)
        self.sites: list[tuple[str, SiteKind]] = []
        # Sites on an external module the project writes to -> the writers.
        # When only test-window code writes there (``_only_in_tests``), the
        # site is seen only by a change to code that runs at import and can
        # run a writer (``import_sites``); the others are lookup sites as
        # well (``sites``).
        site_writers: dict[tuple[str, str], set[str]] = defaultdict(set)
        external: dict[tuple[str, str], set[str]] = defaultdict(set)
        for index in (c_index, other):
            for key, writers in index.external_sites.items():
                external[key].update(writers)
        # Code that can obtain a module without importing it by name in its
        # source (``import_module(name)``), and code that walks the
        # object graph (``__subclasses__``, ``gc``, frames' globals): both
        # can hand on what nothing names.
        self.module_getters: set[str] = set()
        self.graph_readers: set[str] = set()
        # Getters whose module name a literal table bounds to no test module,
        # but which other code may change -> that code (``table_getters``).
        table_getters: dict[str, set[str]] = defaultdict(set)
        # Code a symbol's code can run (references, forward), and the
        # variables each function writes into in place (``mutated_by``).
        self.calls: dict[str, set[str]] = defaultdict(set)
        self.writes: dict[str, set[str]] = defaultdict(set)
        self.holds: dict[str, set[str]] = defaultdict(set)
        self.imported_by: dict[str, set[str]] = defaultdict(set)  # importer -> imported
        self.names_used: dict[str, set[str]] = defaultdict(set)  # unresolved names
        # An attribute read off a value of unknown type finds a module-level
        # name only on a module such a value may be (``attribute_modules``;
        # audit round 3, W20). The readers by a bare name or off a module
        # (``global_names``) may find any symbol of the name.
        self.attribute_modules = c_index.attribute_modules | other.attribute_modules
        self._any_module_readers = c_index.any_module_readers | other.any_module_readers
        self.global_names: dict[str, set[str]] = defaultdict(set)
        self.global_used: dict[str, set[str]] = defaultdict(set)
        # Per variable, the code naming it only to empty or add to it
        # (``writes`` edges) or to rebind it (``rebinds``), and the code
        # naming it any other way (a read): see ``_readers``.
        written_only: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for index in (c_index, other):
            for edge in index.edges:
                mark = edge_mark(edge)
                if mark in (INSTALLED, INSTALLED_ANYWHERE):
                    continue  # static planning's: the record holds what ran
                if edge.kind in (EdgeKind.REFERENCES, EdgeKind.DECLARED):
                    self.readers_of[edge.target].add(edge.source)
                    written_only[edge.target][edge.source].add(
                        mark if mark in (WRITES, REBINDS) else ""
                    )
                    if mark.startswith("attribute:"):
                        self.attribute_readers[mark[len("attribute:") :]].add(edge.source)
                    if mark == MUTATED_BY:
                        self.writes[edge.target].add(edge.source)
                    elif mark != REGISTERS:
                        self.calls[edge.source].add(edge.target)
                    if mark in (MUTATED_BY, REGISTERS):
                        # A registry and what is put in it (``_forward``).
                        self.holds[edge.source].add(edge.target)
                elif edge.kind in (EdgeKind.IMPORTS, EdgeKind.IMPORTS_NAME):
                    self.importers_of[edge.target].add(edge.source)
                    self.imported_by[edge.source].add(edge.target)
            for ref in index.unresolved:
                if ref.kind == UnresolvedKind.DYNAMIC and ref.detail.startswith(CHILD_PROGRAMS):
                    # A Python program the symbol starts: the record follows
                    # the child (or flags the test), so no lookup site here.
                    continue
                if ref.kind == UnresolvedKind.DYNAMIC:
                    site = (ref.symbol, _site_kind(ref.detail))
                    if site[1] == SiteKind.IMPORT and not ref.detail.endswith(DISCARDED_IMPORT):
                        # Imports a module named at run time (or runs code
                        # read at run time): it can hold any module. Code
                        # generated in the program reaches what its module
                        # can, as static planning bounds it. An import
                        # whose module is not kept hands it to nothing.
                        table = index.table_imports.get((ref.symbol, ref.detail))
                        if table is not None and not any(
                            self._names_test_module(n) for n in table[0]
                        ):
                            # The lazy-export table: only its writers can
                            # make it name a test module.
                            table_getters[ref.symbol].update(table[1])
                        else:
                            self.module_getters.add(ref.symbol)
                    writers = external.get((ref.symbol, ref.detail))
                    if writers:
                        site_writers[site].update(writers)
                    if not (writers and self._only_in_tests(writers)):
                        self.sites.append(site)
                elif ref.name:
                    self.by_name[ref.name].add(ref.symbol)
                    self.names_used[ref.symbol].add(ref.name)
                    if not _on_unknown_value(ref, self._any_module_readers, c_index, other):
                        self.global_names[ref.name].add(ref.symbol)
                        self.global_used[ref.symbol].add(ref.name)
                    if ref.kind in ATTRIBUTE_KINDS and ref.name in GRAPH_ATTRIBUTES:
                        self.graph_readers.add(ref.symbol)
            for symbol, detail in index.reflection:
                # GRAPH_HANDLE: a ``gc`` function that hands out objects
                # (not ``gc.collect()`` or ``gc.get_stats()``), or the module
                # handed on or looked up by a name nothing bounds.
                if detail == GRAPH_HANDLE or detail[1:] in GRAPH_ATTRIBUTES:
                    # Hands objects out (``type.__subclasses__(c)``); looks no
                    # name up.
                    self.graph_readers.add(symbol)
                    continue
                writers = external.get((symbol, detail))
                if writers:
                    site_writers[(symbol, SiteKind.ANY)].update(writers)
                if not (writers and self._only_in_tests(writers)):
                    self.sites.append((symbol, SiteKind.ANY))
        # A table getter whose writers all run only inside test windows
        # obtains a test module only in a test that ran one of them (and
        # never outside every test); otherwise it is any getter.
        self.table_getters: dict[str, frozenset[str]] = {}
        for getter, writers in sorted(table_getters.items()):
            if getter in self.module_getters or not self._only_in_tests(writers):
                self.module_getters.add(getter)
            else:
                self.table_getters[getter] = frozenset(writers)
        for decl in declarations:
            self.readers_of[decl.target].add(decl.source)
        self.write_sites: dict[str, set[str]] = defaultdict(set)
        self.rebind_sites: dict[str, set[str]] = defaultdict(set)
        for target, sources in written_only.items():
            for source, details in sources.items():
                if source in self.readers_of[target] and "" not in details:
                    self.write_sites[target].add(source)
                    if details == {REBINDS}:
                        self.rebind_sites[target].add(source)
        self.hands_on = {
            ref.symbol
            for index in (c_index, other)
            for ref in index.unresolved
            if ref.kind in ATTRIBUTE_KINDS and ref.name in ("instance", "cls")
        }
        self.module_hands_on = {
            ref.symbol
            for index in (c_index, other)
            for ref in index.unresolved
            if ref.kind in ATTRIBUTE_KINDS and ref.name == "module"
        } | {
            symbol
            for index in (c_index, other)
            for symbol, detail in index.reflection
            if detail == ".modules"
        }
        # Code that may obtain a module the test runner holds and hand it on
        # (``_module_holders``), in test code or not.
        self._module_handers = {
            ref.symbol
            for index in (c_index, other)
            for ref in index.unresolved
            if ref.kind in ATTRIBUTE_KINDS and ref.name in ("module", "obj")
        } | {
            symbol
            for index in (c_index, other)
            for symbol, detail in index.reflection
            if detail == ".modules"
        }
        self._module_holders_of: dict[str, frozenset[str] | None] = {}
        self._runner_held: set[str] | None = None
        self.module_hands_on = {s for s in self.module_hands_on if self._in_test_code(s)}
        self.sites = sorted(set(self.sites))
        self.members = _members_by_container(set(self.symbols))
        # Code that puts objects on a module only inside tests is seen by a
        # lookup there only in a test that ran it (W24).
        self.reach = _ImportReach(c_index, other, lambda w: not self._only_in_tests({w}))
        self.bases, self.subclasses = _class_graph((c_index, other))
        self._related_of: dict[tuple[str, str], frozenset[str]] = {}
        self._by_module: dict[str, list[Symbol]] | None = None

        # Whether a module that uses pytest added, deleted or redefined a
        # function or class: a fixture (or a class holding them) could have
        # changed where discovery does not see it.
        pytest_modules = {
            self.symbols[x.symbol].module
            for index in (c_index, other)
            for x in index.external
            if x.symbol in self.symbols and x.module.split(".")[0] in ("pytest", "_pytest")
        }
        self.fixture_sources_changed = any(
            c.carries_impact
            and c.symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS)
            and c.symbol.module in pytest_modules
            and {ChangeKind.ADDED, ChangeKind.DELETED, ChangeKind.DEFINITION_CHANGED}
            & set(c.changes)
            for c in self.changes
        )
        self.E: dict[str, Reason] = {}
        # Reader -> (builders, members, why): name-matched readers of a test
        # fake's member, which meet the change only in a test that also ran
        # code able to hand them an instance (``_holders``).
        self.guarded: dict[str, list[tuple[frozenset[str], frozenset[str], Reason]]] = defaultdict(
            list
        )
        self._holders_of: dict[str, tuple[frozenset[str], frozenset[str]] | None] = {}
        self.direct: dict[str, Reason] = {}
        self.fallbacks: list[Fallback] = []
        self.files: dict[str, tuple[str, bool]] = {}  # changed path -> (what, names too)
        # Changed files under the roots the index does not read.
        self.unanalysed = _changed_unanalysed_files(c_index, other)
        # Where a ``build.py`` is a build script (planner.is_build_script).
        self.project_dirs = _project_dirs(c_index, other)
        # Changed paths outside the source roots (set by plan_with_evidence):
        # the index reads none of them, but the recorder saw what tests ran
        # and opened there.
        self.outside: dict[str, str] = {}
        self.seed_changes: set[str] = set()
        self.seed_nodes: dict[str, str] = {}
        self.escalated_modules: set[str] = set()
        self._followed: set[str] = set()
        self._ran_at_import: set[str] = set()  # what ``_import_writes`` followed
        self._variables_by_name: dict[str, set[str]] | None = None
        self._import_code_followed: set[str] = set()
        # Lookup and reflection sites in E, to be followed as readers.
        self._sites_followed: set[tuple[str, bool]] = set()
        self._pending_sites: deque[tuple[str, SymbolChange | None, str, bool]] = deque()
        # ``_stored_results``: code known to be able to run a member of E ->
        # that member; who calls what, backwards; lookup sites by namespace.
        self._runs_e: dict[str, str] = {}
        self._called_by: dict[str, set[str]] | None = None
        # The code the chain calls, transitively, outside it -> (the chain
        # code calling it, what that can run): ``_handed_to``, kept across
        # rounds, as the chain only grows.
        self._handed: dict[str, tuple[str, str]] = {}
        # Caches outside module variables (audit round 5, R22): per function,
        # where it stores computed values and what they derive from
        # (``SourceIndex.stores``); per callee, the attributes handed to it
        # in a parameter it writes (``handed``); the functions whose
        # decorator keeps a result (``memoized``); and the decorator
        # functions whose wrapper may run what they decorate.
        self.stores: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.handed_to: dict[str, set[str]] = defaultdict(set)
        for index in (c_index, other):
            for function, where, derived in index.stores:
                self.stores[function].add((where, derived))
            for _caller, callee, attr in index.handed:
                self.handed_to[callee].add(attr)
        self.memoized = c_index.memoized | other.memoized
        self._decorated_by: dict[str, set[str]] = defaultdict(set)
        for index in (c_index, other):
            for decorated, decorator in index.decorations:
                self._decorated_by[decorated].add(decorator)
        self._stored_seen: set[str] = set()
        self._change_by_id = {c.id: c for c in self.changes}
        # Lazily initialised constants (diffcone.indexer.lazy) at both
        # revisions: variable -> the one function that fills it.
        self.lazy_constants = {
            var: writer
            for var, writer in sorted(c_index.lazy_constants.items())
            if var not in other.symbols or other.lazy_constants.get(var) == writer
        }
        self.declarations = declarations
        # Cython name -> the Cython functions at C that mention it (lazily).
        self._cython_mentions: dict[str, list[tuple[str, CythonFunction]]] | None = None
        # The lookups on an external module that a change to code running at
        # import may make write there, for every later test (``_seeing_sites``):
        # site -> its writers.
        self.import_sites: dict[tuple[str, str], frozenset[str]] = {
            site: frozenset(writers) for site, writers in sorted(site_writers.items())
        }
        # Code that can call what no edge shows: a lookup by a name nothing
        # bounds, an import of a module named at run time, a graph walk.
        self._opaque_callers = (
            {site for site, _ in self.sites}
            | self.module_getters
            | set(self.table_getters)
            | self.graph_readers
        )
        self._forward_of: dict[frozenset[str], frozenset[str]] = {}
        self._symbols_named: dict[str, set[str]] | None = None
        # Code that writes process-global state (indexer/process.py), and
        # for code run while pytest collected a test module (``_collected``):
        # per symbol and the modules it ran for, what the code running it
        # then can run and why that is a hook's doing (``_collection_reach``);
        # the test modules' code that ran then, by module; and the code that
        # can reach pytest's session-wide objects (``_session_code``).
        self._process_writers = {s for index in (c_index, other) for s, _ in index.process_writes}
        self._collection_reach_of: dict[
            tuple[str, tuple[str, ...]], tuple[frozenset[str], str | None]
        ] = {}
        self._collected_code_of: dict[str, list[str]] | None = None
        self._session_code_found: set[str] | None = None
        # Every symbol the recording ran, in any phase or process; a module's
        # top-level code counts as its module symbol (``_never_imported``).
        self._ran_at_c: frozenset[str] | None = None
        # The same, for an attribute read off a value of unknown type (W20).
        self._attribute_named: dict[str, set[str]] = {}

    # -- recording --------------------------------------------------------------

    def _runner_modules(self) -> set[str]:
        """The modules the test runner imports and can hand to other code
        (``request.module``, a collector's ``obj``): test modules,
        conftests, and those defining a test's lifecycle dependencies."""
        if self._runner_held is None:
            held = self.test_code.modules | self.test_code.conftests
            for dep in self.test_code.users:
                symbol = self.symbols.get(dep)
                if symbol is not None:
                    held.add(symbol.module)
            self._runner_held = held
        return self._runner_held

    def _matched(self, symbol: Symbol | None, name: str) -> tuple[set[str], set[str]]:
        """The code reading ``name`` by name that may find ``symbol``: (the
        readers, and those that find it only on a module the test runner
        hands out, which ``_module_guard`` guards). A module-level name is
        found by an attribute read off a value of unknown type only on a
        module such a value may be (audit round 3, W20)."""
        matched = self.by_name.get(name, set())
        if symbol is None or attribute_reachable(symbol, self.attribute_modules):
            return set(matched), set()
        plain = set(self.global_names.get(name, set()))
        if symbol.module in self._runner_modules():
            return plain, matched - plain
        return plain, set()

    def _module_guard(
        self, matched: set[str], module: str, change: SymbolChange | None, label: str
    ) -> set[str]:
        """Readers that find a module-level name of ``module``, a module the
        test runner holds, only on a value of unknown type: those that are
        functions or methods meet the change only in a test that also ran
        code that can obtain the module and hand it on
        (``_module_holders``); the rest are returned, to be read as now."""
        if not matched:
            return matched
        builders = self._module_holders(module)
        if builders is None:
            return matched
        rest: set[str] = set()
        for reader in sorted(matched):
            symbol = self.symbols.get(reader)
            if symbol is None or symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                rest.add(reader)
                continue
            why = Reason(
                Rule.EXECUTED_READER,
                f"{reader} reads {label} by name, on a value only code obtaining {module} "
                "can hand it",
                (),
                change.id if change is not None else None,
                change.changes if change is not None else (),
            )
            self.guarded[reader].append((builders, frozenset(), why))
        return rest

    def _module_holders(self, module: str) -> frozenset[str] | None:
        """The code that can obtain ``module``, held by the test runner, and
        hand it to other code: a read of ``.module`` or ``.obj`` (pytest's
        request, a node, a collector), of ``sys.modules``, a module named at
        run time, a graph walk, a reference to the module as a value. None
        unless each is a function or method that never ran during an import
        or outside every test: otherwise what it obtained may serve any
        later test."""
        if module in self._module_holders_of:
            return self._module_holders_of[module]
        builders = set(self._module_handers) | self.module_getters | self.graph_readers
        builders |= set(self.table_getters)
        builders |= self.readers_of.get(module, set())
        found = frozenset(builders) if self._only_in_tests(builders) else None
        self._module_holders_of[module] = found
        return found

    def _names_test_module(self, name: str) -> bool:
        """Whether importing ``name`` may yield a test module or conftest, or
        a package holding one (whatever name the runner gives it at run
        time: the indexed name or a tail of it)."""
        for module in self.test_code.modules | self.test_code.conftests:
            dotted, wanted = "." + module, "." + name
            if dotted.endswith(wanted) or wanted.endswith(dotted) or module.startswith(name + "."):
                return True
        return False

    def _in_test_code(self, symbol_id: str) -> bool:
        symbol = self.symbols.get(symbol_id)
        return symbol is not None and self.test_code.is_test_code(symbol.module)

    def _symbols_of(self, module: str) -> list[Symbol]:
        """The symbols of ``module`` over both revisions, by identity."""
        if self._by_module is None:
            self._by_module = defaultdict(list)
            for symbol in self.symbols.values():
                self._by_module[symbol.module].append(symbol)
        return self._by_module.get(module, [])

    def _related(self, cls: str, direction: str) -> frozenset[str]:
        """``cls`` with its ancestors (``BASES``) or its descendants
        (``SUBCLASSES``) over both revisions, computed once."""
        key = (cls, direction)
        found = self._related_of.get(key)
        if found is None:
            graph = self.bases if direction == BASES else self.subclasses
            found = self._related_of[key] = frozenset(_closure([cls], graph))
        return found

    def _never_imported(self, module: str) -> bool:
        """Whether ``module`` is a Python module of the index, not test code,
        whose top-level code ran nowhere in the recording: not during an
        import, collection or a hook, not inside a test (a lazy import), not
        in a process a test started (its records fold into the test's), and
        not before the recorder started (``preloaded``). No object of it
        existed at C. Not a Cython module, a module outside the index, or
        test code, which have their own rules."""
        symbol = self.c.symbols.get(module)
        if (
            symbol is None
            or symbol.kind != SymbolKind.MODULE
            or not symbol.path.endswith(".py")
            or self.test_code.is_test_code(module)
        ):
            return False
        if self._ran_at_c is None:
            ev = self.evidence
            self._ran_at_c = (
                frozenset(ev.symbols)
                | ev.import_phase
                | ev.import_by.keys()
                | ev.collect_by.keys()
                | ev.hook_phase
                | ev.preloaded
            )
        return module not in self._ran_at_c

    def _ran_outside_tests(self, code: str) -> bool:
        """Whether ``code`` (a symbol) ran at C outside every test: during
        an import, in a hook or during collection. Evaluating its
        annotations there (``__annotate__``, Python 3.14) is not running it:
        that writes, obtains and hands on nothing of its own."""
        ev = self.evidence
        if code in ev.annotations_only:
            return False
        return (
            code in ev.import_phase
            or code in ev.import_by
            or code in ev.collect_by
            or code in ev.hook_phase
        )

    def _only_in_tests(self, code: Iterable[str]) -> bool:
        """Whether what ``code`` does (writes, obtains, hands on) serves only
        the test running it: each is a function or method, and none ran at C
        outside every test. A lookup on an external module written by such
        writers finds the project's code only in a test that ran one of them
        (roadmap item 14); code run at import (module or class top-level
        code, a decorator) leaves what it did for every later test."""
        for sid in code:
            symbol = self.symbols.get(sid)
            if symbol is None or symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                return False
            if self._ran_outside_tests(sid):
                return False
        return True

    def _forward(self, sources: Iterable[str], *, imports: bool = False) -> frozenset[str]:
        """What code running at import (``sources``) can run: the code it
        references, transitively (a call, or a value it may call later), with
        the code an unresolved name it uses may be (``obj.install()``), the
        constructors of the classes it uses, and what a variable it reads
        holds (initialiser, decorated functions, what code puts in it). With
        ``imports`` (a module whose imports changed), the import-time code of
        what a module of ``sources`` imports, transitively, as well."""
        key = frozenset(sources)
        if imports:
            stack = sorted(
                s for s in key if s in self.symbols and self.symbols[s].kind == SymbolKind.MODULE
            )
            modules: set[str] = set()
            while stack:
                module = stack.pop()
                if module in modules:
                    continue
                modules.add(module)
                stack.extend(self.imported_by.get(module, ()))
            key = key | frozenset(modules)
        if key not in self._forward_of:
            if self._symbols_named is None:
                self._symbols_named = defaultdict(set)
                self._attribute_named = defaultdict(set)
                runner = self._runner_modules()
                for symbol in self.symbols.values():
                    if symbol.kind in (
                        SymbolKind.FUNCTION,
                        SymbolKind.METHOD,
                        SymbolKind.CLASS,
                    ) and not _is_dunder(symbol.name):
                        self._symbols_named[symbol.name].add(symbol.id)
                        if symbol.module in runner or attribute_reachable(
                            symbol, self.attribute_modules
                        ):
                            self._attribute_named[symbol.name].add(symbol.id)
            seen: set[str] = set()
            stack = sorted(key)
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                symbol = self.symbols.get(current)
                if symbol is None:
                    continue
                if symbol.kind == SymbolKind.CLASS:
                    stack.extend(self._constructors(current))
                if symbol.kind == SymbolKind.MODULE and current not in key:
                    continue  # its import ran already: it runs nothing new
                stack.extend(self.calls.get(current, ()))
                for holder in self.holds.get(current, ()):
                    held = self.symbols.get(holder)
                    if held is not None and held.kind == SymbolKind.MODULE:
                        # Top-level code put something in it: whatever it names.
                        stack.extend(self.calls.get(holder, ()))
                    else:
                        stack.append(holder)
                for name in self.names_used.get(current, ()):
                    if name in self.global_used.get(current, ()):
                        stack.extend(self._symbols_named.get(name, ()))
                    else:
                        stack.extend(self._attribute_named.get(name, ()))
            self._forward_of[key] = frozenset(seen)
        return self._forward_of[key]

    def _observe(self, symbol: str, rule: Rule, detail: str, change: SymbolChange | None) -> None:
        if symbol not in self.E:
            self.E[symbol] = Reason(
                rule,
                detail,
                (),
                change.id if change is not None else None,
                change.changes if change is not None else (),
            )

    def _select_all(self, rule: Rule, detail: str) -> None:
        self.fallbacks.append(Fallback(rule, "all_targets", detail))

    def _scope(self, symbol: Symbol, change: SymbolChange) -> None:
        classes: set[str] = set()
        current: str | None = symbol.id if symbol.kind == SymbolKind.CLASS else symbol.container
        while current:
            holder = self.symbols.get(current)
            if holder is None:
                break
            if holder.kind == SymbolKind.CLASS:
                classes |= self._related(current, SUBCLASSES)
            current = holder.container
        whole_module = (
            symbol.kind == SymbolKind.VARIABLE
            and symbol.container == symbol.module
            and symbol.module not in self.test_code.conftests
        )
        for test in self.test_code.scope(symbol, classes, whole_module=whole_module):
            self.direct.setdefault(
                test,
                Reason(
                    Rule.TEST_SCOPE,
                    f"{change.id} {'/'.join(change.changes)} in test code; pytest reads marks, "
                    "fixtures and parameters without a static reader, so every test in its "
                    "scope is selected",
                    (),
                    change.id,
                    change.changes,
                ),
            )

    # -- rules -------------------------------------------------------------------

    def run(self) -> None:
        if self.changes or self.unanalysed or self.outside:
            self._unrecorded_phases()
            self._preloaded()
        decorated = self.c.doc_decorated | self.other.doc_decorated
        for change in self.changes:
            if is_build_script(change.symbol.path, self.project_dirs):
                self._file(change.symbol.path, "edited", names=False, source=True)
                continue
            if change.carries_impact:
                self._change(change)
            elif ChangeKind.DOCSTRING_CHANGED in change.changes and change.id in decorated:
                # Its decorator reads the docstring when the module is
                # imported: no record shows that, the import-time state does.
                self._escalate_change(
                    change, f"{change.id} docstring_changed: its decorator reads it at import"
                )
        self._newly_run()
        self._docstring_readers()
        cython = self._cython()
        for path in self.unanalysed:
            if path in cython:
                continue
            before, after = path in self.c.other_files, path in self.other.other_files
            what = "edited" if before and after else ("added" if after else "deleted")
            self._file(path, what, names=not (before and after))
        for change in self.changes:
            symbol = change.symbol
            if symbol.kind == SymbolKind.MODULE and {ChangeKind.ADDED, ChangeKind.DELETED} & set(
                change.changes
            ):
                what = f"{change.id} {'/'.join(change.changes)}"
                self._file(symbol.path, what, names=True, source=True)
        # A source file is also data to whoever reads it as a file
        # (``exec(open("version.py").read())``, ``inspect.getsource``): its
        # edit reaches the tests that opened it.
        for path in sorted({c.symbol.path for c in self.changes} - set(self.files)):
            if path.endswith(".py"):
                self._file(path, "edited", names=False, source=True)
        for path, what in sorted(self.outside.items()):
            if _is_conftest(path):
                # pytest loads it for tests that never ran a line of it (a new
                # autouse fixture, a hook): no record can vouch for them.
                self._select_all(
                    Rule.UNOBSERVED_FILE,
                    f"{path} {what}: a conftest.py outside the source roots, which the "
                    "index does not read",
                )
            else:
                self._file(path, what, names=what != "edited")
        self._settle()

    def _settle(self) -> None:
        """Follow what the rules left pending until E stops growing: lookup
        and reflection sites as readers, and the module state that code able
        to run a member of E may hold a result of (``_stored_results``)."""
        while True:
            while self._pending_sites:
                site, change, label, in_tests = self._pending_sites.popleft()
                self._reader(site, change, label, in_tests=in_tests)
            if not self._stored_results():
                break

    def _stored_results(self) -> bool:
        """A cache an earlier test filled hides what filled it from a later
        test's record (audit round 3, W7): ``get`` stores ``compute``'s result
        in ``_CACHE`` during the first test that asks, and every later test
        reads it there without running ``compute``, in every order. So for
        each function or method in E, the code that can run it (``_callers_of``,
        transitively) may have stored what it returned into the variables it
        writes in place (``mutated_by``); each of those holds a changed value
        for the tests after, so its readers, and the sites that can see it,
        join E. A test stores there only while tests run, so only code running
        inside a test can have read it: no import or hook is followed
        (``in_tests``). Every write counts, whatever it stores: what a
        value derives from, statically, misses too many ways a chain
        reaches a store (the changed function's own literal, a changed
        constant, a call through a variable or a table, ``heapq.heappush``,
        a container stored first and filled through a local; audit T1).
        The code the chain calls, transitively, is followed the same way
        (``_handed_to``, audit E36): ``get`` handing ``compute(k)`` to
        ``_put(k, v)``, which writes ``_CACHE``. ``functools`` caches need
        none of this: the recorder clears them before every test. True when
        a variable was found, since its readers may in turn be stored.

        A cache held elsewhere than a module variable (audit round 5, R22) is
        found the same way, by what the code stores rather than where: code
        in the chain that stores a value derived from code that can run a
        member of E (``SourceIndex.stores``: ``self._cache[k] =
        compute(k)``, ``cls._cache[k] = ...``, a memoising decorator's
        ``cache[k] = f(k)``) holds the result on an object or in a closure
        that a later test reads without reaching the change. What reads it
        is whoever reads that state: the code reading the attribute by name
        (for a store through ``self``, ``cls``, a parameter, a function or a
        class), every member of the class for a method writing its receiver
        through another method (``self.put(k, compute(k))``), the storing
        code itself for a closure, and the readers of the attribute a caller
        handed for a parameter container (``fill(self._cache, k)``:
        ``handed``); each is observed, and the storing code too, so every
        test that executed one of them joins. Code whose decorator keeps a
        result without running it again (``SourceIndex.memoized``:
        ``functools.cached_property``) serves it to readers that run nothing
        of it, so its readers (resolved and by name, guarded by who can hand
        them an instance) and sites are followed as for a changed value. The
        isolation assumption holds for both: a store by a test's own code is
        seen by that test alone."""
        changed = self._change_by_id
        queue: deque[str] = deque()
        for s in sorted({*self.E, *self.guarded}):
            symbol = self.symbols.get(s)
            if (
                symbol is not None
                and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
                and s not in self._runs_e
            ):
                self._runs_e[s] = s
                queue.append(s)
        # (variable, writer, what it can run, the chain code calling the writer)
        found: list[tuple[str, str, str, str | None]] = []
        joined: list[str] = []  # the chain's functions found this round
        while queue:
            node = queue.popleft()
            symbol = self.symbols[node]
            if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                joined.append(node)
                for variable in sorted(self.writes.get(node, ())):
                    if variable not in self._followed and variable in self.symbols:
                        found.append((variable, node, self._runs_e[node], None))
            by_name = node in changed and self._runs_e[node] == node
            for caller in sorted(self._callers_of(symbol, by_name=by_name)):
                if caller not in self._runs_e and caller in self.symbols:
                    self._runs_e[caller] = self._runs_e[node]
                    queue.append(caller)
        for callee, caller, ran in self._handed_to(joined):
            for variable in sorted(self.writes.get(callee, ())):
                if variable not in self._followed and variable in self.symbols:
                    found.append((variable, callee, ran, caller))
        for variable, writer, ran, caller in found:
            if variable in self._followed:
                continue
            if writer in self.test_code.isolated and self._only_in_tests({writer}):
                # The isolation assumption: what a test's own code stores is
                # seen by that test alone, which runs the writer itself.
                continue
            self._followed.add(variable)
            why = self.E.get(ran) or self.guarded[ran][0][2]
            change = changed.get(why.changed_symbol or "")
            ran_by = (
                f"{writer} can run {ran} and"
                if caller is None
                else f"{caller} can run {ran} and calls {writer}, which"
            )
            label = (
                f"{ran_by} stores into {variable}, where a later test may "
                f"read what an earlier one stored ({why.detail})"
            )
            self._observe(variable, Rule.EXECUTED_READER, label, change)
            self._readers(
                variable,
                change,
                label,
                in_tests=True,
                content_only=True,
                exclude=self._lazy_writer(variable, writer),
            )
            self._sites(self.symbols[variable], change, label, in_tests=True)
        # Storing or memoised code anywhere in the chain, every round: what a
        # store derives from may have joined the chain only now.
        keepers = [
            (node, ran)
            for node, ran in sorted(self._runs_e.items())
            if (node in self.stores or node in self.memoized) and node not in self._stored_seen
        ]
        kept = False
        chain_names: set[str] | None = None
        for keeper, ran in keepers:
            if keeper in self.test_code.isolated and self._only_in_tests({keeper}):
                self._stored_seen.add(keeper)
                continue  # the isolation assumption, as above
            why = self.E.get(ran) or self.guarded[ran][0][2]
            change = changed.get(why.changed_symbol or "")
            if keeper in self.memoized:
                self._stored_seen.add(keeper)
                kept = True
                label = (
                    f"{keeper} can run {ran}, and its decorator may keep the result for "
                    f"later readers ({why.detail})"
                )
                self._observe(keeper, Rule.EXECUTED_READER, label, change)
                self._readers(keeper, change, label, in_tests=True, content_only=True)
                self._sites(self.symbols[keeper], change, label, in_tests=True)
            wheres: set[str] = set()
            for where, derived in sorted(self.stores.get(keeper, ())):
                if chain_names is None:
                    chain_names = {self.symbols[s].name for s in self._runs_e if s in self.symbols}
                if self._derives_from_chain(derived, chain_names):
                    wheres.add(where)
            if not wheres:
                continue  # the chain's result is not what it stores, as far as E goes
            self._stored_seen.add(keeper)
            kept = True
            label = (
                f"{keeper} can run {ran} and stores what that computes, where a later test "
                f"may read what an earlier one stored ({why.detail})"
            )
            self._observe(keeper, Rule.EXECUTED_READER, label, change)
            for reader in sorted(self._state_readers(keeper, wheres, change, label)):
                self._reader(reader, change, label, in_tests=True)
        return bool(found) or kept

    def _handed_to(self, joined: list[str]) -> list[tuple[str, str, str]]:
        """The code the chain's functions that joined it this round call,
        transitively, outside the chain: (callee, the chain code calling it,
        what that can run). It may store what it is handed, the chain's
        result (audit E36: ``_put(k, compute(k))``), or something a branch
        on that result decides, so every write it makes in place counts, as
        for the chain itself. Calls are the resolved references and, for a
        class, what creating or calling it runs (not ``__init_subclass__``
        or ``__set_name__``, which a class statement runs at import); a
        method of a value of unknown type is not taken for every method of
        its name, as for ``SourceIndex.stores`` (R22, docs/limitations.md). A
        module's top-level code runs at import, which the import rules
        follow, and a test's own code hands nothing on: what it has stored
        is seen by that test alone (the isolation assumption, as in
        ``_stored_results``)."""
        handed = self._handed
        found: list[tuple[str, str, str]] = []
        queue: deque[tuple[str, str, str]] = deque(
            (node, node, self._runs_e[node])
            for node in joined
            if not (node in self.test_code.isolated and self._only_in_tests({node}))
        )
        while queue:
            node, caller, ran = queue.popleft()
            callees: set[str] = set()
            for target in self.calls.get(node, ()):
                symbol = self.symbols.get(target)
                if symbol is None:
                    continue
                if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                    callees.add(target)
                elif symbol.kind == SymbolKind.CLASS:
                    callees.update(
                        c
                        for c in self._constructors(target)
                        if c.rsplit(".", 1)[-1] not in ("__init_subclass__", "__set_name__")
                    )
            for callee in sorted(callees):
                if callee in self._runs_e or callee in handed:
                    continue
                handed[callee] = (caller, ran)
                found.append((callee, caller, ran))
                queue.append((callee, caller, ran))
        return found

    def _derives_from_chain(self, derived: str, chain_names: set[str]) -> bool:
        """Whether a stored value (``SourceIndex.stores``) may come from code
        that can run a member of E: a symbol in the chain (a class through
        its constructors), a name some member of the chain bears, or
        anything (a call through a parameter, a local bound twice)."""
        if derived == DERIVED_ANY:
            return True
        if derived.startswith(DERIVED_NAME):
            return derived[len(DERIVED_NAME) :] in chain_names
        if derived in self._runs_e:
            return True
        symbol = self.symbols.get(derived)
        if symbol is not None and symbol.kind == SymbolKind.CLASS:
            return any(c in self._runs_e for c in self._constructors(derived))
        return False

    def _state_readers(
        self, keeper: str, wheres: set[str], change: SymbolChange | None, label: str
    ) -> set[str]:
        """The code that reads the state ``keeper`` stores into (``wheres``,
        as ``SourceIndex.stores`` spells them): the readers of an attribute
        by name (guarded, for a member of a test-code class, by who can
        hand them an instance), every member of the class's family for the
        receiver itself, nothing beyond the keeper for a closure, and the
        readers of each attribute a caller handed for a parameter container."""
        readers: set[str] = set()
        symbol = self.symbols[keeper]
        container = self.symbols.get(symbol.container or "")
        cls = container.id if container is not None and container.kind == SymbolKind.CLASS else None
        attrs: set[str] = set()
        for where in wheres:
            if where == STORE_SELF:
                if cls is not None:
                    for c in sorted(self._related(cls, BASES) | self._related(cls, SUBCLASSES)):
                        readers |= {
                            m
                            for m in self.members.get(c, ())
                            if self.symbols[m].kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
                        }
            elif where == STORE_PARAM:
                attrs |= self.handed_to.get(keeper, set())
            elif where != STORE_CLOSURE:
                attrs.add(where)
        for attr in sorted(attrs):
            readers |= self.attribute_readers.get(attr, set())
            matched = set(self.by_name.get(attr, ()))
            if cls is not None:
                matched = self._guard(matched, cls, change, f"{label} (attribute {attr})")
            readers |= matched
        readers.discard(keeper)
        return readers

    def _callers_of(self, symbol: Symbol, *, by_name: bool) -> set[str]:
        """The code that can run ``symbol`` (``_stored_results``), one step
        back: what references it (a call, or a read of the function as a
        value, which reaches the readers of a variable or class holding it),
        what the project declares, the decorator function wrapping it
        (``SourceIndex.decorations``), and for a special method (a constructor
        among them), the code naming its class or a subclass, which stands in
        for whoever holds an instance. With ``by_name`` (the changed function
        itself), also the calls by its name on objects of unknown type. A
        name match further back, or a lookup by a name nothing bounds, is not
        followed: on strata either ties every writer to every change
        (internal/evidence_design.md, the isolation assumption). A module's
        top-level code ends a chain: it runs at import, which
        ``_import_effect`` follows."""
        if symbol.kind == SymbolKind.MODULE:
            return set()
        if self._called_by is None:
            self._called_by = defaultdict(set)
            for source, targets in self.calls.items():
                for target in targets:
                    self._called_by[target].add(source)
            for decl in self.declarations:
                self._called_by[decl.target].add(decl.source)
            # A decorator function's wrapper may run what it decorates.
            for decorated, decorators in self._decorated_by.items():
                self._called_by[decorated] |= decorators
        out = set(self._called_by.get(symbol.id, ()))
        container = self.symbols.get(symbol.container or "")
        if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD) and _is_dunder(symbol.name):
            if container is not None and container.kind == SymbolKind.CLASS:
                for cls in self._related(container.id, SUBCLASSES):
                    out |= self._called_by.get(cls, set())
        elif symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD) and by_name:
            plain, held = self._matched(symbol, symbol.name)
            out |= plain | held
        out.discard(symbol.id)
        return out

    def _newly_run(self) -> None:
        """A module a changed symbol newly imports, or no longer imports
        (``planner._newly_run_modules``: an import added to a module outside
        its base import closure, or to a function body, and the closure of
        the imported module; R24), ran at C for no test, or will run for
        none: what its import-time code or its callees write into is a
        changed value for every reader (``_import_writes``), and a reader of
        the module or its members whose own imports never reach it
        (``pkg.sub.f()`` under ``import pkg``) now finds, or no longer finds,
        it."""
        newly_run = _newly_run_modules(self.c, self.other, self.changes)
        if not newly_run:
            return
        change_by_id = {c.id: c for c in self.changes}
        closures = {"c": _import_closures(self.c), "other": _import_closures(self.other)}
        readers: dict[str, list[str]] = defaultdict(list)
        for index in (self.c, self.other):
            for edge in index.edges:
                if edge.kind != EdgeKind.REFERENCES:
                    continue
                target = self.symbols.get(edge.target)
                module = (
                    edge.target
                    if edge.target in newly_run
                    else (target.module if target is not None else None)
                )
                if module in newly_run:
                    readers[module].append(edge.source)
        for module, step in sorted(newly_run.items()):
            change = change_by_id[step.target]
            how = "newly imported" if step.kind == StepKind.NEWLY_IMPORTED else "no longer imported"
            why = f"{module}: {how} by {change.id} ({'/'.join(change.changes)})"
            self._import_writes([module], change, why)
            for reader in sorted(set(readers.get(module, ()))):
                symbol = self.symbols.get(reader)
                if symbol is None:
                    continue
                reached = {
                    m
                    for label, index in (("c", self.c), ("other", self.other))
                    for m in closures[label].get(symbol.module, set())
                }
                if module in reached:
                    continue  # its own imports run the module
                detail = f"{reader} reads {module}, which only that import binds: {why}"
                if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                    self._observe(reader, Rule.EXECUTED_READER, detail, change)
                else:
                    self._escalate_module(symbol.module, detail)

    def _docstring_readers(self) -> None:
        """A docstring-only change runs no different code, but code reading
        docstrings (``C.__doc__``, ``inspect.getdoc(f)``, its module's
        ``__doc__``) sees it without running the documented code, so no
        record shows the read. As in static planning, such a reader notices
        the change of what it refers to (or of its own module): the tests
        that ran it, or, for code that runs at import, its module's."""
        documented = {
            c.id: c
            for c in self.changes
            if not c.carries_impact and ChangeKind.DOCSTRING_CHANGED in c.changes
        }
        if not documented:
            return
        for index in (self.c, self.other):
            for reader, referenced in sorted(docstring_readers(index).items()):
                if ANY_DOCSTRING in referenced:
                    # Read off a value its references do not bound (R7).
                    referenced = referenced | documented.keys()
                hits = sorted(referenced & documented.keys())
                if not hits:
                    continue
                change = documented[hits[0]]
                why = f"{reader} reads docstrings and refers to {hits[0]}, whose docstring changed"
                symbol = self.symbols[reader]
                if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                    self._observe(reader, Rule.EXECUTED_READER, why, change)
                else:
                    self._escalate_module(symbol.module, why)

    def _unrecorded_phases(self) -> None:
        """Outside every test, something ran that no record shows (a process
        nothing recorded, text code): any change may reach what that import
        built, or, outside every import (a hook, collection), any test."""
        for module in sorted(self.evidence.import_flagged):
            why = "a process no record follows, or code compiled from text,"
            if module == "" or module.startswith(UNINDEXED_MODULE):
                where = module[len(UNINDEXED_MODULE) :] if module else ""
                self._select_all(
                    Rule.SUBPROCESS,
                    f"{why} ran outside every test "
                    + (
                        f"while {where}, a file outside the source roots, was imported"
                        if where
                        else "and outside any import (a hook or collection)"
                    ),
                )
            else:
                self._escalate_module(module, f"{why} ran while {module} was imported")

    def _preloaded(self) -> None:
        """The modules imported before the recorder started
        (``Evidence.preloaded``: a ``.pth`` file, a ``-p`` plugin loaded
        before it when ``sitecustomize`` could not run): their import-time
        code ran where no record shows it, and a function of theirs may
        have run then too (a decorated function whose result is kept at
        import). Any change in one is planned from the code: the change
        escalated, and the module with it, as for a change that ran during
        an import. A changed file outside the source roots among them
        selects everything: nothing static says who observes what it did."""
        preloaded = self.evidence.preloaded
        if not preloaded:
            return
        for change in self.changes:
            module = change.symbol.module
            if not change.carries_impact or module not in preloaded:
                continue
            why = (
                f"{change.id} {'/'.join(change.changes)}: {module} was imported before the "
                "recorder started, so what its code did then no record shows"
            )
            self._escalate_change(change, why)
            self._escalate_module(module, why)
        for path, what in sorted(self.outside.items()):
            if UNINDEXED_MODULE + path in preloaded:
                self._select_all(
                    Rule.UNINDEXED_IMPORT,
                    f"{path} {what}: a file outside the source roots, imported before the "
                    "recorder started",
                )

    def _cython(self) -> set[str]:
        """Cython sources (roadmap items 7 and 8): the paths this rule
        handled.

        A body edit selects the tests that executed the function in a
        profiled build at C, and for a ``nogil``, ``cpdef`` or unprofiled
        (``profile(False)``, ``linetrace(False)``) function, which such a
        build does not always report, the tests that executed any Cython
        function naming it (``_cython_ran``). A name bound outside every
        function whose binding changed reaches the code that can see it and
        names it (``_cython_name``). Any other change (an added or deleted
        ``.pyx``, a statement that binds nothing by name, a file
        ``tokenize`` cannot read), or a module the store holds no Cython
        record of (built without ``profile=True``), selects all."""
        paths = {
            p
            for p in self.unanalysed
            if is_cython(p) and (p in self.c.cython or p in self.other.cython)
        }
        if not paths:
            return set()
        changes = cython_changes(
            {p: m for p, m in self.c.cython.items() if p in paths},
            {p: m for p, m in self.other.cython.items() if p in paths},
        )
        for path, why in changes.files:
            self._select_all(
                Rule.UNOBSERVED_FILE,
                f"{path} {why}: a Cython change outside function bodies is not attributed "
                "to any function",
            )
        recorded = {
            s.split("::", 1)[0]
            for s in self.evidence.symbols
            if "::" in s and is_cython(s.split("::", 1)[0])
        }
        for path, name in changes.functions:
            label = f"{symbol_id(path, name)} body_changed (Cython)"
            function = self.c.cython[path].by_name()[name]
            self._cython_ran(path, function, Rule.EXECUTED_CHANGED, label, label, recorded)
        for changed in changes.names:
            self._cython_name(changed, recorded)
        return paths

    def _cython_ran(
        self,
        path: str,
        function: CythonFunction,
        rule: Rule,
        detail: str,
        label: str,
        recorded: set[str],
    ) -> None:
        """The tests that executed ``function`` at C observe the change;
        when it is ``nogil``, ``cpdef`` or unprofiled (``profile(False)``,
        ``linetrace(False)``), which a profiled build does not always
        report, so do those that executed a Cython function naming it
        (``_cython_callers``)."""
        sid = symbol_id(path, function.name)
        if path not in recorded:
            self._select_all(
                Rule.UNOBSERVED_FILE,
                f"{label}: the evidence holds no Cython record from {path} (collected "
                "without a profile=True build of it?)",
            )
            return
        self._observe(sid, rule, detail, None)
        self._cython_import_effect(sid, label, recorded)
        if function.nogil or function.cpdef or function.unprofiled:
            kind = "nogil" if function.nogil else ("cpdef" if function.cpdef else "profile(False)")
            for caller in self._cython_callers(path, function.name):
                self._observe(
                    caller,
                    Rule.CYTHON_CALLER,
                    f"{caller} names {sid}, a {kind} function a profiled build does not "
                    f"always report; {label}",
                    None,
                )
                self._cython_import_effect(caller, label, recorded)

    def _cython_name(self, changed: CythonName, recorded: set[str], cause: str = "") -> None:
        """A name bound outside every function body changed (roadmap item
        8): the Cython functions that can see it and mention it, and for a
        name Python can see, the Python code reading it by that name and the
        lookup and reflection sites. A class attribute also reaches
        everything holding an instance."""
        qualified = f"{changed.scope}.{changed.name}" if changed.scope else changed.name
        label = f"{changed.path}::{qualified} {changed.change} (Cython, outside functions)"
        if cause:
            label = f"{label}: {cause}"
        seers = self._cython_seers(changed.path, changed.name, changed.visible)
        for path, function in self._cython_mentioning(changed.name):
            if seers is None or path in seers:
                self._cython_ran(
                    path,
                    function,
                    Rule.EXECUTED_READER,
                    f"{symbol_id(path, function.name)} names {changed.name}; {label}",
                    label,
                    recorded,
                )
        if changed.attribute:
            self._cython_instances(changed.scope.split(".")[-1], label, recorded)
        if changed.visible:
            readers = self.by_name.get(changed.name, set())
            if changed.scope:
                readers = readers | self.attribute_readers.get(changed.name, set())
            for reader in sorted(readers):
                self._reader(reader, None, label, read=changed.name)
            # A lookup by a name nothing bounds may find it, and read its
            # value as well as notice it come or go.
            for site, kind in self.sites:
                if kind != SiteKind.IMPORT and site in self.symbols:
                    self._observe(
                        site,
                        Rule.LOOKUP_SITE,
                        f"{site} looks names up by a name nothing bounds and may see "
                        f"{changed.path}; {label}",
                        None,
                    )

    def _cython_instances(self, cls: str, label: str, recorded: set[str]) -> None:
        """A class attribute declaration changes the layout and the generated
        pickling of every instance of the class and its subclasses: the
        tests that ran their methods or a function naming one of them (where
        instances come from), and the Python code naming one."""
        family, stack = {cls}, [cls]
        while stack:
            base = stack.pop()
            for module in (*self.c.cython.values(), *self.other.cython.values()):
                for statement in module.statements or ():
                    name = statement.names[0] if statement.names else ""
                    if statement.kind == CYTHON_CLASS and base in statement.bases:
                        if name not in family:
                            family.add(name)
                            stack.append(name)
        for path, module in self.c.cython.items():
            for function in module.functions:
                if function.scope.split(".")[-1] in family:
                    self._cython_ran(
                        path,
                        function,
                        Rule.EXECUTED_READER,
                        f"{symbol_id(path, function.name)} is a method of {cls} or a "
                        f"subclass; {label}",
                        label,
                        recorded,
                    )
        for name in sorted(family):
            for path, function in self._cython_mentioning(name):
                self._cython_ran(
                    path,
                    function,
                    Rule.EXECUTED_READER,
                    f"{symbol_id(path, function.name)} names {name}, whose instances "
                    f"changed; {label}",
                    label,
                    recorded,
                )
            for reader in sorted(self.by_name.get(name, ())):
                self._reader(reader, None, label, read=name)

    def _cython_mentioning(self, name: str) -> list[tuple[str, CythonFunction]]:
        """The Cython functions at C whose header or body mentions ``name``."""
        if self._cython_mentions is None:
            self._cython_mentions = defaultdict(list)
            for p, module in self.c.cython.items():
                for function in module.functions:
                    for mentioned in function.names:
                        self._cython_mentions[mentioned].append((p, function))
        return self._cython_mentions.get(name, [])

    def _cython_seers(self, path: str, name: str, visible: bool) -> set[str] | None:
        """The Cython files whose functions can see ``name`` bound in
        ``path``, or None for every file. A name Python can see may be
        imported anywhere, and a ``.pxi`` file is included anywhere; a
        ``.pyx`` file's C names stay in it (and in the ``.pxi`` files it may
        include) unless its ``.pxd`` declares them too (a C global the
        ``.pyx`` initialises, read by cimporters); a ``.pxd`` file's reach its
        ``.pyx`` and every file that cimports from it, transitively through
        other ``.pxd`` files."""
        if visible or path.endswith(".pxi"):
            return None
        modules = {**self.c.cython, **self.other.cython}
        seen = {path} | {p for p in modules if p.endswith(".pxi")}
        if path.endswith(".pyx"):
            twin = modules.get(path[: -len(".pyx")] + ".pxd")
            declared = twin is not None and (
                twin.statements is None
                or any(name in s.names for s in twin.statements)
                or any(f.simple_name == name for f in twin.functions)
            )
            if not declared:
                return seen
            path = path[: -len(".pyx")] + ".pxd"
            seen.add(path)
        seen.add(path[: -len(".pxd")] + ".pyx")
        frontier = [path]
        while frontier:
            stem = pxd_stem(frontier.pop())
            for p in sorted(modules):
                if p in seen:
                    continue
                statements = modules[p].statements
                if statements is None or any(
                    s.kind == CYTHON_IMPORT and not s.visible and names_module(s, stem)
                    for s in statements
                ):
                    seen.add(p)
                    if p.endswith(".pxd"):
                        frontier.append(p)
        return seen

    def _cython_callers(self, path: str, name: str) -> list[str]:
        """The Cython functions at C that name ``path::name``, and through
        any of them that is itself ``nogil``, ``cpdef`` or unprofiled,
        theirs."""
        found: set[str] = set()
        stack = [(path, name)]
        while stack:
            p, n = stack.pop()
            simple = n.split(".")[-1].split("#")[0]
            for caller_path, caller in self._cython_mentioning(simple):
                caller_id = symbol_id(caller_path, caller.name)
                if caller_id in found or (caller_path, caller.name) == (path, name):
                    continue
                found.add(caller_id)
                if caller.nogil or caller.cpdef or caller.unprofiled:
                    stack.append((caller_path, caller.name))
        return sorted(found)

    def _cython_import_effect(self, symbol_id_: str, label: str, recorded: set[str]) -> None:
        """Cython code that ran while a module was imported built its state;
        code that ran in a hook or during collection cannot be planned
        statically, so it selects all. Either may have written into state
        elsewhere that later tests read (``_cython_writes``).

        The Python side is ``_import_effect``; this one differs where a
        Cython function differs from an indexed symbol. What it can write is
        not known from edges, so ``_cython_writes`` treats every name it
        mentions as written (``_import_writes`` follows ``mutated_by``). And
        it is never a test's own collection code (``_collects_itself``) nor
        a node static planning can be seeded from, so having run in a hook
        or during collection always selects everything."""
        collected = self.evidence.collect_by.get(symbol_id_, frozenset())
        if self.evidence.import_by.get(symbol_id_) or collected:
            self._cython_writes(symbol_id_, label, recorded)
        for module in sorted(self.evidence.import_by.get(symbol_id_, ())):
            if module.startswith(UNINDEXED_MODULE):
                self._select_all(
                    Rule.UNINDEXED_IMPORT,
                    f"{label}: {symbol_id_} ran while a file outside the source roots was imported",
                )
            else:
                self._escalate_module(module, f"{symbol_id_} ran while {module} was imported")
        if symbol_id_ in self.evidence.hook_phase:
            self._select_all(
                Rule.UNOBSERVED_FILE,
                f"{label}: {symbol_id_} ran outside every test (a hook or collection)",
            )
        else:
            # Run while pytest collected a test module (the Python side:
            # ``_collected``): that module's tests.
            for module in sorted(collected):
                self._collected_tests(
                    module, None, f"{symbol_id_} ran while pytest collected {module} at C; {label}"
                )

    def _cython_writes(self, symbol_id_: str, label: str, recorded: set[str]) -> None:
        """A changed Cython function that ran while a module was imported
        may have written into state another module holds, which tests read
        without running it (the Cython side of ``_import_writes``). There is
        no index of what Cython code writes, so every name it mentions that
        is bound outside every function, in a Cython file or as a Python
        variable, is treated as a changed value; a Cython file whose
        statements could not be read selects all."""
        if symbol_id_ in self._ran_at_import:
            return
        self._ran_at_import.add(symbol_id_)
        path, name = symbol_id_.split("::", 1)
        module = self.c.cython.get(path)
        function = module.by_name().get(name) if module is not None else None
        modules = {**self.c.cython, **self.other.cython}
        if function is None or any(m.statements is None for m in modules.values()):
            self._select_all(
                Rule.UNOBSERVED_FILE,
                f"{label}: {symbol_id_} ran while a module was imported, and what it can "
                "write is not known",
            )
            return
        cause = f"{symbol_id_} names it and ran while a module was imported at C; {label}"
        bound: set[tuple[str, str, str, bool]] = set()
        for p, m in modules.items():
            for statement in m.statements or ():
                if statement.kind in (CYTHON_DECLARATION, CYTHON_ASSIGNMENT):
                    for n in statement.names:
                        if n in function.names:
                            bound.add((p, statement.scope, n, statement.visible))
        for p, scope, n, visible in sorted(bound):
            written = CythonName(p, scope, n, "changed", visible, bool(scope))
            self._cython_name(written, recorded, cause)
        if self._variables_by_name is None:
            self._variables_by_name = defaultdict(set)
            for symbol in self.symbols.values():
                if symbol.kind == SymbolKind.VARIABLE:
                    self._variables_by_name[symbol.name].add(symbol.id)
        for n in sorted(function.names):
            for variable in sorted(self._variables_by_name.get(n, ())):
                if variable in self._followed:
                    continue
                self._followed.add(variable)
                why = f"{variable} may be written by {cause}"
                self._observe(variable, Rule.EXECUTED_READER, why, None)
                self._readers(variable, None, why)
                self._sites(self.symbols[variable], None, why, at_import=True)

    def _change(self, change: SymbolChange) -> None:
        symbol = change.symbol
        kinds = set(change.changes)
        test = self.test_code.is_test_code(symbol.module)
        label = f"{change.id} {'/'.join(change.changes)}"
        if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD) and symbol.name.startswith(
            "pytest_"
        ):
            self._select_all(Rule.PYTEST_HOOK, f"{label}: a pytest hook can change any test")
            return
        if (
            symbol.kind == SymbolKind.VARIABLE
            and symbol.container == symbol.module
            and (
                (
                    symbol.module in self.test_code.conftests
                    and symbol.name in PYTEST_COLLECTION_NAMES
                )
                # In a test module too (or any module pytest imports, holding
                # targets or not): the plugins it names are registered for the
                # whole session once the module is imported.
                or symbol.name == "pytest_plugins"
            )
        ):
            self._select_all(Rule.PYTEST_HOOK, f"{label}: it decides what pytest loads")
            return
        body_only = symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD) and kinds <= {
            ChangeKind.BODY_CHANGED,
            ChangeKind.DEPENDENCIES_CHANGED,
            ChangeKind.DOCSTRING_CHANGED,
        }
        if test and not body_only and change.id not in self.test_code.entries:
            # A test's own change reaches the targets it is the entry of
            # (changed_target); anything else in test code reaches its scope.
            self._scope(symbol, change)
        if symbol.kind == SymbolKind.MODULE:
            if kinds & {ChangeKind.ADDED, ChangeKind.DELETED}:
                self._observe(change.id, Rule.EXECUTED_CHANGED, label, change)
                self._readers(change.id, change, label)
                if ChangeKind.DELETED in kinds:
                    self._importers(change.id, change, label)
                # What it imports runs at its import too.
                runs = sorted(self._forward([change.id], imports=True))
                self._sites(symbol, change, label, imports=True, at_import=True, runs=runs)
            else:
                self._escalate_change(change, f"{label}: module-level code runs at import")
            return
        if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
            self._observe(change.id, Rule.EXECUTED_CHANGED, label, change)
            self._import_effect(change.id, change, label)
            if kinds & {
                ChangeKind.ADDED,
                ChangeKind.DELETED,
                ChangeKind.DEFINITION_CHANGED,
                ChangeKind.ANNOTATIONS_CHANGED,
            }:
                self._readers(change.id, change, label)
                if ChangeKind.DELETED in kinds:
                    self._importers(change.id, change, label)
                self._sites(symbol, change, label)
            if _runs_at_import(change):
                # ``@register def two()``, ``def f(x: set_mode("fast"))``: the
                # ``def`` runs code at import that may change shared state no
                # test's record names. A test's own decorators reach other
                # tests only through what they write
                # (``@parametrize("x", [set_mode(1)])``).
                why = f"{label}: its definition runs code at import"
                self._import_writes([change.id], change, why)
                if change.id not in self.test_code.entries:
                    self._escalate_change(change, why)
            if _is_dunder(symbol.name) and not body_only and symbol.container:
                container = self.symbols.get(symbol.container)
                if container is not None and container.kind == SymbolKind.CLASS:
                    self._hierarchy(container.id, change, label)
                elif container is not None and container.kind == SymbolKind.MODULE:
                    # A module ``__getattr__``/``__dir__`` (PEP 562) serves every
                    # lookup the module does not answer, wherever it is made.
                    self._escalate_change(
                        change, f"{label}: a module's {symbol.name} serves its missing names"
                    )
            return
        if symbol.kind == SymbolKind.VARIABLE and symbol.name == "__all__":
            # What ``from m import *`` binds in every star importer: no record
            # names the variable, the import statement reads it.
            self._escalate_change(change, f"{label}: star imports of the module bind its names")
        if symbol.kind == SymbolKind.VARIABLE:
            self._observe(change.id, Rule.EXECUTED_CHANGED, label, change)
            self._readers(change.id, change, label)
            # A lookup by a name nothing bounds reads the value as well as
            # noticing the name come or go: a variable runs no code of its
            # own for the record to show. Its initialiser runs at import,
            # and may write somewhere unless it binds a literal on both sides
            # (an annotation evaluated at import is code too).
            runs_code = _runs_at_import(change)
            self._sites(symbol, change, label, at_import=runs_code)
            if ChangeKind.DELETED in kinds:
                self._importers(change.id, change, label)
            if runs_code:
                # ``X = set_mode("fast")``, ``X: set_mode("fast") = 1``: what
                # the statement's calls write is read elsewhere, by tests
                # that never run it.
                self._import_writes([change.id], change, f"{label}: its statement runs code")
            return
        if symbol.kind == SymbolKind.CLASS:
            self._observe(change.id, Rule.EXECUTED_CHANGED, label, change)
            if kinds & {ChangeKind.ADDED, ChangeKind.DELETED, ChangeKind.BODY_CHANGED}:
                # Its body runs at import, and what its calls write is read
                # elsewhere (``x = set_mode("fast")`` as a class attribute).
                self._import_writes([change.id], change, f"{label}: its body runs at import")
            if kinds & {ChangeKind.ADDED, ChangeKind.DELETED}:
                self._readers(change.id, change, label)
                self._sites(symbol, change, label, at_import=True)
                if ChangeKind.DELETED in kinds:
                    self._importers(change.id, change, label)
                if self._runs_on_creation(symbol.id):
                    # ``class B(Base)`` whose base registers subclasses, a
                    # decorated or metaclassed class: creating it runs code.
                    self._escalate_change(change, f"{label}: creating the class runs code")
                return
            before = self.c.class_attributes.get(symbol.id, {})
            after = self.other.class_attributes.get(symbol.id, {})
            if ChangeKind.DEPENDENCIES_CHANGED in kinds or before.get(CLASS_STATEMENT) != after.get(
                CLASS_STATEMENT
            ):
                # Bases, decorators, metaclass keywords (or what the body's
                # names resolve to): they run at import and may register the
                # class or rebuild it. A definition change that is only an
                # added or deleted member is that member's own change.
                self._escalate_change(change, f"{label}: the class statement runs at import")
            elif (
                change.base is not None
                and change.head is not None
                and reordered(change.base.statement_layout, change.head.statement_layout)
            ):
                # The body's statements run in another order (R6): what each
                # sees differs, and no attribute's own hash says so.
                self._escalate_change(change, f"{label}: its statements run in another order")
            if ChangeKind.BODY_CHANGED in kinds:
                self._class_body(symbol, change, label)
            # A definition change that is only an added or deleted member is
            # that member's own change: code notices a new or missing
            # attribute only by looking it up, by name (a reader), by a name
            # nothing bounds (a lookup site) or reflectively.
            return
        self._escalate_change(change, f"{label}: not a kind evidence can bound")

    def _runs_on_creation(self, class_id: str) -> bool:
        """Whether creating the class may run code that reads its attributes:
        it or an ancestor is open (decorators, keywords, a base outside the
        index) or an ancestor defines ``__init_subclass__``."""
        for index in (self.c, self.other):
            for cls in _closure([class_id], index.class_bases):
                if cls in index.open_classes:
                    return True
                if cls != class_id and f"{cls}.__init_subclass__" in index.symbols:
                    return True
        return False

    def _class_body(self, symbol: Symbol, change: SymbolChange, label: str) -> None:
        before = self.c.class_attributes.get(symbol.id, {})
        after = self.other.class_attributes.get(symbol.id, {})
        names = {
            n
            for n in before.keys() | after.keys()
            if before.get(n) != after.get(n) and n != CLASS_STATEMENT
        }
        if OPAQUE_ATTRIBUTE in names or any(_is_dunder(n) for n in names):
            self._escalate_change(
                change,
                f"{label}: its body runs code that binds no plain attribute, or a special one",
            )
            return
        if self._runs_on_creation(symbol.id):
            # A dataclass field default, an Enum member: consumed when the class
            # is created, and used through generated code that never names it.
            self._escalate_change(
                change, f"{label}: its creation (a decorator, metaclass or base) reads its body"
            )
            return
        for name in sorted(names):
            attribute = f"{label} (attribute {name})"
            matched = self.by_name.get(name, set())
            readers = self.attribute_readers.get(name, set()) | self._guard(
                matched, symbol.id, change, attribute
            )
            for reader in sorted(readers):
                self._reader(reader, change, attribute, read=name)
        self._sites(symbol, change, label, at_import=True)

    def _hierarchy(self, cls: str, change: SymbolChange, label: str) -> None:
        """Members of the class, its bases and its subclasses: code running on
        an instance of any of them may see the change."""
        related = self._related(cls, BASES) | self._related(cls, SUBCLASSES)
        for c in sorted(related):
            for member in self.members.get(c, ()):
                kind = self.symbols[member].kind
                if kind in (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS):
                    self._observe(
                        member,
                        Rule.EXECUTED_READER,
                        f"{member} belongs to the hierarchy of {cls}; {label}",
                        change,
                    )
            if c != cls and c in self.subclasses.get(cls, set()):
                self._readers(c, change, label)

    def _readers(
        self,
        target: str,
        change: SymbolChange | None,
        label: str,
        *,
        in_tests: bool = False,
        content_only: bool = False,
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        """``in_tests``: what changed is what code running inside the tests
        stored (``_stored_results``), which no import or hook that ran before
        them can have read: function readers are observed, not followed into
        what imports and hooks they ran in. ``content_only``: what ``target``
        holds changed in place (code writing into it ran differently), so
        code that only empties or adds to it (``X.clear()``) reads nothing
        that changed. Code rebinding it from another module (``mod.X = v``)
        reads nothing of it unless it was deleted. ``exclude``: readers that
        cannot see the change (``_lazy_writer``)."""
        readers = set(self.readers_of.get(target, ()))
        if content_only:
            readers -= self.write_sites.get(target, set())
        elif not (
            change is not None and target == change.id and ChangeKind.DELETED in change.changes
        ):
            readers -= self.rebind_sites.get(target, set())
        name = target.rsplit(".", 1)[-1]
        if not _is_dunder(name):
            symbol = self.symbols.get(target)
            matched, held = self._matched(symbol, name)
            matched -= readers | {target}
            own = change is not None and target == change.id
            container = self.symbols.get(symbol.container) if symbol and symbol.container else None
            if container is not None and container.kind == SymbolKind.CLASS:
                matched = self._guard(
                    matched, container.id, change, label if own else f"{label} via {target}"
                )
            if symbol is not None and held:
                matched |= self._module_guard(
                    held - readers - {target},
                    symbol.module,
                    change,
                    label if own else f"{label} via {target}",
                )
            readers |= matched
        readers.discard(target)
        readers -= exclude
        for reader in sorted(readers):
            own = change is not None and target == change.id
            self._reader(
                reader,
                change,
                label if own else f"{label} via {target}",
                in_tests=in_tests,
                read=target.rsplit(".", 1)[-1],
            )

    def _guard(
        self, matched: set[str], cls: str, change: SymbolChange | None, label: str
    ) -> set[str]:
        """Name-matched readers of a member of ``cls``: those that are
        functions or methods are guarded when only the code ``_holders``
        finds can hand them an instance; the rest are returned, to be read
        as now."""
        if not matched:
            return matched
        holders = self._holders(cls)
        if holders is None:
            return matched
        builders, members = holders
        rest: set[str] = set()
        for reader in sorted(matched):
            symbol = self.symbols.get(reader)
            if symbol is None or symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                rest.add(reader)
                continue
            why = Reason(
                Rule.EXECUTED_READER,
                f"{reader} reads {label} by name, on an object only code naming {cls} or a "
                "subclass can hand it",
                (),
                change.id if change is not None else None,
                change.changes if change is not None else (),
            )
            self.guarded[reader].append((builders, members, why))
        return rest

    def _holders(self, cls: str) -> tuple[frozenset[str], frozenset[str]] | None:
        """The code that can hand another function an instance of ``cls`` or
        of a subclass (or the class itself): the functions and methods that
        name one of those classes, the lookup and reflection sites that can
        see their namespaces (the builders), and their members (a running
        method holds ``self``). None unless holding one needs one of those
        to run in the same test (roadmap item 13): every class of the family
        and every ancestor is test code, none runs code when created, each
        builder is a function or method, and none of them ran during an
        import or outside every test."""
        if cls in self._holders_of:
            return self._holders_of[cls]
        self._holders_of[cls] = None
        family = self._related(cls, SUBCLASSES)
        ancestors = _closure(family, self.bases)
        if not all(self._in_test_code(c) for c in ancestors):
            return None
        if any(self._runs_on_creation(c) for c in family):
            return None
        builders: set[str] = set()
        members: set[str] = set()
        for c in family:
            for source in self.readers_of.get(c, set()) | self.by_name.get(
                c.rsplit(".", 1)[-1], set()
            ):
                if source not in family:  # a subclass naming its base
                    builders.add(source)
            builders.update(site for site, _ in self._seeing_sites(self.symbols[c]))
            for member in self.members.get(c, ()):
                if self.symbols[member].kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                    members.add(member)
        # Code that finds the class without naming it: through an ancestor's
        # ``__subclasses__()`` (or any class's: ``object.__subclasses__()``
        # walks every class), the collector, a frame's or function's globals.
        builders |= self.graph_readers
        # ``members`` are methods: only whether they ran outside tests matters.
        if not self._only_in_tests(builders | members):
            return None
        self._holders_of[cls] = (frozenset(builders), frozenset(members))
        return self._holders_of[cls]

    def _reader(
        self,
        reader: str,
        change: SymbolChange | None,
        label: str,
        *,
        in_tests: bool = False,
        read: str | None = None,
    ) -> None:
        """``read``: the name ``reader`` reads (a variable's, an attribute's),
        or None when it finds what changed by looking it up."""
        symbol = self.symbols.get(reader)
        if symbol is None:
            return
        if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
            self._observe(reader, Rule.EXECUTED_READER, f"{reader} reads {label}", change)
            if not in_tests:
                self._import_effect(reader, change, f"{label}, read by {reader}", read=read)
        elif symbol.kind == SymbolKind.VARIABLE:
            # Its initialiser captured the value: it changed too.
            if reader not in self._followed:
                self._followed.add(reader)
                self._observe(reader, Rule.EXECUTED_READER, f"{reader} reads {label}", change)
                self._readers(reader, change, label, in_tests=in_tests)
                self._sites(
                    symbol,
                    change,
                    f"{label}, captured by {reader}",
                    at_import=not in_tests,
                    in_tests=in_tests,
                )
        elif in_tests:
            # Module or class top-level code reads what a test stored only
            # when the module is imported inside a test, whose record holds it.
            self._observe(reader, Rule.EXECUTED_READER, f"{reader} reads {label}", change)
        else:
            # Module or class top-level code: import-time state.
            self._escalate_module(symbol.module, f"top-level code of {reader} reads {label}")

    def _importers(self, target: str, change: SymbolChange, label: str) -> None:
        """A deleted name or module: whoever imports it fails there."""
        for importer in sorted(self.importers_of.get(target, ())):
            symbol = self.symbols.get(importer)
            if symbol is None:
                continue
            if symbol.kind == SymbolKind.MODULE:
                self._escalate_module(importer, f"{importer} imports {label}")
            else:
                self._observe(importer, Rule.EXECUTED_READER, f"{importer} imports {label}", change)

    def _import_effect(
        self, symbol_id: str, change: SymbolChange | None, label: str, *, read: str | None = None
    ) -> None:
        """Code that ran at C while a module was imported built that module's
        import-time state; code that ran while pytest collected a test module
        decided that module's tests (``_collected``); code that ran outside
        every test, import and such collection ran in a hook. Each may also
        have written into another module's state, which every later test
        reads (``_import_writes``).

        A symbol whose only code that ran there was its annotations'
        (``__annotate__``, Python 3.14) ran none of its body there: a change
        to its body or docstring alone changes nothing that ran outside the
        tests, and as a reader (``read``: the name it reads, None for a
        lookup) it read the change there only if its annotations use that
        name, or call something that could look it up. Any other change of
        its own (its annotations, its definition) is taken as before."""
        ev = self.evidence
        if symbol_id in ev.annotations_only:
            if change is not None and symbol_id == change.id:
                if set(change.changes) <= {ChangeKind.BODY_CHANGED, ChangeKind.DOCSTRING_CHANGED}:
                    return
            elif not self._annotations_read(symbol_id, read):
                return
        hook = symbol_id in ev.hook_phase
        collected = ev.collect_by.get(symbol_id, frozenset())
        if ev.import_by.get(symbol_id) or hook or collected:
            self._import_writes(
                [symbol_id], change, f"{symbol_id} ran outside every test at C; {label}"
            )
        for module in sorted(self.evidence.import_by.get(symbol_id, ())):
            if module.startswith(UNINDEXED_MODULE):
                self._select_all(
                    Rule.UNINDEXED_IMPORT,
                    f"{label}: {symbol_id} ran while {module[len(UNINDEXED_MODULE) :]}, a file "
                    "outside the source roots, was imported",
                )
            else:
                self._escalate_module(
                    module, f"{symbol_id} ran while {module} was imported; {label}"
                )
        if not (hook or collected):
            return
        if self._collects_itself(symbol_id):
            reason = f"{symbol_id} ran outside every test (a hook or collection); {label}"
            if change is not None and symbol_id == change.id:
                self._escalate_change(change, reason)
            elif symbol_id not in self.seed_nodes:
                self.seed_nodes[symbol_id] = reason
        elif hook:
            # A hook (or code it reaches without a static path: a plugin
            # object's method, a callback) runs once per session, before or
            # between the tests, and what it leaves behind (an environment
            # variable, a mark on an item, another library's state) reaches
            # tests that run none of its code. Having run in a hook outweighs
            # having run while a test module was collected too.
            self._select_all(
                Rule.PYTEST_HOOK,
                f"{label}: {symbol_id} ran outside every test at C (in a pytest hook, or "
                "while pytest collected what is no test module of the index), and what it "
                "does there can reach any later test",
            )
        else:
            self._collected(symbol_id, change, label, collected)

    def _collected(
        self, symbol_id: str, change: SymbolChange | None, label: str, modules: Iterable[str]
    ) -> None:
        """``symbol_id`` (changed, or reading a change) ran at C while pytest
        collected the test modules ``modules`` (``Evidence.collect_by``), and
        in no hook: building their parameters' ids, an ``ids=`` callable,
        ``pytest_generate_tests`` (roadmap item 19, rule 3). What it can
        change there is those modules' tests (their parameters, ids and
        marks, whether they are collected), which are selected, and state
        written then: into variables, by what it can run (``_import_writes``,
        from ``_import_effect``) and by the code that ran it, with its result
        (``_stored_results``, which follows every member of E back to its
        callers); and onto an external module that a lookup may find
        (``_collection_sites``).

        Code that ran then and can run it (``_collection_reach``: the code
        between pytest and it, a ``pytest_generate_tests`` and its helpers)
        can pass its result on. It counts as a hook's after all (everything
        is selected) when that code, or what it can run, writes process-global
        state (``os.environ``, warning filters: indexer/process.py), which
        every later test sees, or can reach pytest's session-wide objects
        (``_session_code``), where it can leave state for the hooks acting on
        every test."""
        modules = sorted(modules)
        reach, hooky = self._collection_reach(symbol_id, modules)
        if hooky is not None:
            self._select_all(
                Rule.PYTEST_HOOK,
                f"{label}: {symbol_id} ran while pytest collected {modules[0]} at C, and {hooky}",
            )
            return
        self._collection_sites(symbol_id, reach, change, label)
        for module in modules:
            self._collected_tests(
                module,
                change,
                f"{symbol_id} ran while pytest collected {module} at C, which decides its "
                f"tests (their parameters, ids and marks); {label}",
            )

    def _collected_tests(self, module: str, change: SymbolChange | None, why: str) -> None:
        """Select the tests collected from ``module``'s file."""
        path = self.test_code.path_of_module.get(module)
        for test in self.test_code.tests_by_file.get(path, ()) if path is not None else ():
            self.direct.setdefault(
                test,
                Reason(
                    Rule.COLLECTION,
                    why,
                    (),
                    change.id if change is not None else None,
                    change.changes if change is not None else (),
                ),
            )

    def _collection_reach(
        self, symbol_id: str, modules: list[str]
    ) -> tuple[frozenset[str], str | None]:
        """What code that ran at C while pytest collected ``modules``, and
        can run ``symbol_id`` (or is it), can run in turn (``_forward``), and
        why that is a hook's doing (``_collected``), or None."""
        key = (symbol_id, tuple(modules))
        found = self._collection_reach_of.get(key)
        if found is not None:
            return found
        upstream = {symbol_id}
        for module in modules:
            for code in self._collected_code(module):
                if code not in upstream and symbol_id in self._forward([code]):
                    upstream.add(code)
        reach = frozenset().union(*(self._forward([code]) for code in sorted(upstream)))
        why: str | None = None
        writers = sorted(reach & self._process_writers)
        session = sorted(reach & self._session_code())
        if writers:
            why = (
                f"code run then can run {writers[0]}, which writes process-global state that "
                "every later test sees"
            )
        elif session:
            why = (
                f"code run then can run {session[0]}, which can reach pytest's session-wide "
                "objects (a hook handed the config, or code reading .config, .session, .stash "
                "or .pluginmanager) and leave state there for hooks acting on every test"
            )
        self._collection_reach_of[key] = (reach, why)
        return reach, why

    def _session_code(self) -> set[str]:
        """Code that can reach pytest's session-wide objects: a ``pytest_*``
        function or method (pytest hands hooks the config or the session)
        other than ``pytest_generate_tests`` (handed only a ``metafunc``,
        which leads there through the attributes below), and code reading
        one of ``SESSION_ATTRIBUTES`` off a value (``metafunc.config``,
        ``request.session``)."""
        if self._session_code_found is None:
            found = {
                sid
                for sid, symbol in self.symbols.items()
                if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
                and symbol.name.startswith("pytest_")
                and symbol.name != "pytest_generate_tests"
            }
            for index in (self.c, self.other):
                for ref in index.unresolved:
                    if ref.kind in ATTRIBUTE_KINDS and ref.name in SESSION_ATTRIBUTES:
                        found.add(ref.symbol)
            self._session_code_found = found
        return self._session_code_found

    def _collection_sites(
        self, symbol_id: str, reach: frozenset[str], change: SymbolChange | None, label: str
    ) -> None:
        """The lookups on an external module the project writes to that code
        run while a test module was collected (``reach``: what the code
        running ``symbol_id`` then can run) can make write there, for every
        later test (``import_sites``, as ``_seeing_sites`` takes them for
        code running at import), join E and are followed as readers."""
        opaque = bool(reach & self._opaque_callers)
        for (site, _kind), writers in self.import_sites.items():
            if site not in self.symbols or not (opaque or reach & writers):
                continue
            self._observe(
                site,
                Rule.LOOKUP_SITE,
                f"{site} looks names up on an external module that code run while pytest "
                f"collected a test module, with {symbol_id}, may write to; {label}",
                change,
            )
            if (site, False) not in self._sites_followed:
                self._sites_followed.add((site, False))
                self._pending_sites.append((site, change, f"{label}, seen by {site}", False))

    def _collected_code(self, module: str) -> list[str]:
        """The symbols that ran at C while pytest collected ``module``."""
        if self._collected_code_of is None:
            self._collected_code_of = defaultdict(list)
            for symbol, modules in sorted(self.evidence.collect_by.items()):
                for m in modules:
                    self._collected_code_of[m].append(symbol)
        return self._collected_code_of.get(module, [])

    def _annotations_read(self, symbol_id: str, read: str | None) -> bool:
        """Whether evaluating ``symbol_id``'s annotations at C can read
        ``read`` (a name; None: whatever a lookup finds)."""
        symbol = self.c.symbols.get(symbol_id) or self.symbols[symbol_id]
        names = symbol.annotation_names
        return "*" in names or (read is not None and read in names)

    def _collects_itself(self, symbol_id: str) -> bool:
        """Whether code of ``symbol_id`` that ran outside every test is code
        pytest runs to collect the tests using it: a test's or fixture's
        ``ids``/``params`` callables, which land in that test or fixture. A
        change there reaches those tests (``changed_target``, the fixture's
        scope) and what static planning reaches from it. Not a hook, nor a
        helper a hook calls."""
        symbol = self.symbols.get(symbol_id)
        if symbol is None or symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD):
            return False
        if symbol_id in self.test_code.entries:
            return True
        return (
            symbol_id in self.test_code.users
            and not symbol.name.startswith("pytest_")
            and self.test_code.is_test_code(symbol.module)
        )

    def _escalate_change(self, change: SymbolChange, why: str) -> None:
        """Plan the change statically (it runs at import). What it built is
        seen through the namespace holding it: a module-level change through
        the module, a decorated member through its class, so the lookup
        sites are those that can see that namespace."""
        self.seed_changes.add(change.id)
        symbol = change.symbol
        self._module_symbols(symbol.module, why)
        sources = (
            self._import_code(symbol.module) if symbol.kind == SymbolKind.MODULE else [change.id]
        )
        runs = sources
        before = change.base.imports if change.base is not None else None
        after = change.head.imports if change.head is not None else None
        if symbol.kind == SymbolKind.MODULE and before != after:
            # Its imports changed: what it imports may now run at its import.
            runs = sorted(self._forward([*sources, change.id], imports=True))
        self._sites(
            symbol, change, why, imports=symbol.kind == SymbolKind.MODULE, at_import=True, runs=runs
        )
        self._import_writes(sources, change, why)

    def _escalate_module(self, module: str, why: str) -> None:
        """Plan the module's import statically, as if its top-level code
        changed: what ran during it (or what it read) did."""
        if module not in self.seed_nodes:
            self.seed_nodes[module] = why
        self._module_symbols(module, why)
        module_symbol = self.symbols.get(module)
        code = self._import_code(module)
        if module_symbol is not None:
            self._sites(module_symbol, None, why, imports=True, at_import=True, runs=code)
        self._import_writes(code, None, why)

    def _import_code(self, module: str) -> list[str]:
        """The symbols of ``module`` whose code runs when it is imported: the
        module's own code, variable initialisers, class bodies and the
        headers of ``def`` statements that may run project code (a
        function's body is followed with it: references do not tell the two
        apart). A ``def`` whose header runs no project code (``quiet_header``:
        ``@pytest.fixture``, ``@pytest.mark.parametrize("x", CASES)``) is not
        followed: its body does not run at import. Empty once
        it has been followed."""
        if module in self._import_code_followed:
            return []
        self._import_code_followed.add(module)
        out = []
        for symbol in self._symbols_of(module):
            if symbol.kind in (SymbolKind.MODULE, SymbolKind.VARIABLE, SymbolKind.CLASS) or (
                symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
                and not symbol.inert_definition
                and not symbol.quiet_header
            ):
                out.append(symbol.id)
        return sorted(out)

    def _import_writes(
        self, sources: Iterable[str], change: SymbolChange | None, label: str
    ) -> None:
        """Code that runs at import (``sources``: a changed variable
        initialiser, class body or decorator, or code that ran during an
        import or a hook at C) can call code that writes into a variable
        elsewhere: ``X = set_mode("fast")`` writes ``MODE``, which every
        later test reads without running the writer. So each variable
        written in place (``mutated_by``) by the code the sources can call,
        transitively, is treated as a changed value. Calling a class runs
        its constructors. A write through an argument or a receiver
        (``set_mode(REG)``, ``registry.register(x)``) is a ``mutated_by``
        edge to the caller (diffcone.indexer.writes)."""
        stack = sorted((s for s in sources if s not in self._ran_at_import), reverse=True)
        start = set(stack)
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            symbol = self.symbols.get(current)
            if symbol is None:
                continue
            if symbol.kind == SymbolKind.CLASS and current not in start:
                stack.extend(sorted(self._constructors(current), reverse=True))
                continue
            if symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD) and current not in start:
                continue  # read, not run
            if current in self._ran_at_import:
                continue
            self._ran_at_import.add(current)
            for variable in sorted(self.writes.get(current, ())):
                written = self.symbols.get(variable)
                if written is None or variable in self._followed:
                    continue
                self._followed.add(variable)
                why = f"{label}; it can run {current}, which writes into {variable}"
                self._observe(variable, Rule.EXECUTED_READER, f"{variable}: {why}", change)
                self._readers(
                    variable,
                    change,
                    why,
                    content_only=True,
                    exclude=self._lazy_writer(variable, current),
                )
                self._sites(written, change, why, at_import=True)
            stack.extend(sorted(self.calls.get(current, ()), reverse=True))

    def _lazy_writer(self, variable: str, writer: str) -> frozenset[str]:
        """``writer`` when ``variable`` is a lazily initialised constant it
        fills (``SourceIndex.lazy_constants``) and it did not change: it
        computes the same value whoever runs it first and whenever, so its
        own reads of the variable cannot tell whether an earlier run (at
        import, in a hook, in an earlier test) filled it. Other code reading
        the variable can (it may read it before the writer ever ran) and is
        followed as before; a change to the writer itself is planned as any
        change."""
        if self.lazy_constants.get(variable) == writer and writer not in self._change_by_id:
            return frozenset({writer})
        return frozenset()

    def _constructors(self, cls: str) -> list[str]:
        """What calling or creating ``cls`` can run: the constructors,
        ``__call__``, ``__init_subclass__`` and ``__set_name__`` of it and
        its ancestors."""
        return [
            member
            for current in sorted(self._related(cls, BASES))
            for member in self.members.get(current, ())
            if member.rsplit(".", 1)[-1] in _CONSTRUCTORS
        ]

    def _module_symbols(self, module: str, why: str) -> None:
        """Every symbol of a module whose import-time state may differ: code
        running there can read that state without naming it."""
        if module in self.escalated_modules:
            return
        self.escalated_modules.add(module)
        for symbol in self._symbols_of(module):
            if symbol.kind in (
                SymbolKind.FUNCTION,
                SymbolKind.METHOD,
                SymbolKind.CLASS,
                SymbolKind.MODULE,
            ):
                self._observe(
                    symbol.id,
                    Rule.ESCALATED,
                    f"{symbol.id} is in {module}, whose import-time state may differ: {why}",
                    None,
                )

    def _sites(
        self,
        symbol: Symbol,
        change: SymbolChange | None,
        label: str,
        *,
        imports: bool = False,
        at_import: bool = False,
        runs: Iterable[str] | None = None,
        in_tests: bool = False,
    ) -> None:
        """The lookup and reflection sites that can see ``symbol``'s namespace
        join E, and are followed as readers (``_reader``, with ``in_tests``)."""
        handed: list[str] = []
        guarded: list[tuple[str, frozenset[str], str]] = []
        for site, detail in self._seeing_sites(
            symbol,
            imports=imports,
            at_import=at_import,
            runs=runs,
            handed=handed,
            guarded=guarded,
        ):
            self._observe(site, Rule.LOOKUP_SITE, f"{detail}; {label}", change)
            followed = (site, in_tests) in self._sites_followed or (
                site,
                False,
            ) in self._sites_followed
            if not followed:
                # A site is a reader: one that runs at import (module or class
                # top-level code, a variable's initialiser, a function an
                # import or a hook ran) built state no test's record shows it
                # reading. Followed from ``run`` (``_settle``).
                self._sites_followed.add((site, in_tests))
                self._pending_sites.append((site, change, f"{label}, seen by {site}", in_tests))
        for site, writers, detail in guarded:
            self.guarded[site].append(
                (
                    writers,
                    frozenset(),
                    Reason(
                        Rule.LOOKUP_SITE,
                        f"{detail}; {label}",
                        (),
                        change.id if change is not None else None,
                        change.changes if change is not None else (),
                    ),
                )
            )
        if handed and symbol.container:
            # Library code finds a test class's member only on an instance
            # (or the class) a test handed it: guarded like a name match.
            what = f"{label} (a lookup by a name nothing bounds)"
            for site in sorted(self._guard(set(handed), symbol.container, change, what)):
                self._observe(
                    site,
                    Rule.LOOKUP_SITE,
                    f"{site} looks names up by a name nothing bounds and may be handed an "
                    f"instance of {symbol.container}; {label}",
                    change,
                )

    def _seeing_sites(
        self,
        symbol: Symbol,
        *,
        imports: bool = False,
        at_import: bool = False,
        runs: Iterable[str] | None = None,
        handed: list[str] | None = None,
        guarded: list[tuple[str, frozenset[str], str]] | None = None,
    ) -> list[tuple[str, str]]:
        """Code that finds names by a name nothing bounds and can see the
        namespace of ``symbol``: a read off an object from anywhere, eval and
        reflection always; a read off a module global when the namespace's
        module is in the site's import closure; a module named at run time
        for a module-level namespace, when ``imports``.

        Test code is held by pytest, and by other code only in a few ways.
        A module-level name of a test module or conftest is seen by the sites
        whose import closure holds the module, and through the code that can
        obtain the module otherwise, which stands in for the lookup after it:
        a ``.module`` or ``sys.modules`` read, a reference to the module as a
        value, an import of a module named at run time (library code can
        import ``"tests.test_a"`` when a test names it), and
        code walking the object graph. When one of those ran outside every
        test, what it obtained may serve any later test, and every lookup on
        an object from elsewhere counts. A member of a class only the runner
        ever instantiates (``_runner_only``) is seen only by sites
        inside that class, its bases and its subclasses: no other code can
        hold one of its instances. A member of another test-code class is
        also seen by library lookups on an object from elsewhere, once a test
        hands it an instance: with ``handed``, those are put there for the
        caller to guard (``_guard``); without, they are returned. For a change
        to code that runs at import
        (``at_import``: an escalation, a variable, a class body), the lookups
        on an external module the project writes to count too, wherever they
        are: that code may now write there at import, for every later test.
        When only test code can run the writers, only a change to that code
        does (``import_sites``)."""
        out: list[tuple[str, str]] = []
        namespace = symbol.module
        test = self.test_code.is_test_code(namespace)
        family = self._runner_family(symbol.id)
        # A test module or conftest is held by pytest and by its importers.
        # Only code that imports it, or is handed it (below), can look a
        # module-level name up on it.
        module_name = test and (symbol.kind == SymbolKind.MODULE or symbol.container == namespace)
        # A module no code of the recording imported held no object a lookup
        # at C could find (audit E23: ``docs/conf.py``, seen by every frame
        # reader); at the head only an import added elsewhere can bring it in,
        # a change planned on its own.
        never_imported = self._never_imported(namespace)
        for site, kind in [] if never_imported else self.sites:
            site_symbol = self.symbols.get(site)
            if site_symbol is None:
                continue
            library = test and not self.test_code.is_test_code(site_symbol.module)
            if family is not None and not _inside(site, family, self.c, self.other):
                continue  # but see the handing-on sites below
            if kind == SiteKind.CLOSURE or module_name:
                if namespace not in self.reach.closure_of(site):
                    continue
            elif kind == SiteKind.IMPORT:
                if not imports or test:
                    continue
            elif library:
                # A lookup on an object from elsewhere, in library code, for
                # a member of a test-code class.
                if handed is not None:
                    handed.append(site)
                    continue
            out.append(
                (site, f"{site} looks names up by a name nothing bounds and can see {namespace}")
            )
        if module_name:
            listed = {site for site, _ in out}
            getters = (
                {site: "reads .module or sys.modules" for site in self.module_hands_on}
                | {site: "imports a module named at run time" for site in self.module_getters}
                | {site: "walks the object graph" for site in self.graph_readers}
            )
            for reader in self.readers_of.get(namespace, ()):
                kind_ = self.symbols[reader].kind if reader in self.symbols else None
                if kind_ in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                    getters.setdefault(reader, "uses the module as a value")
            for site, how in sorted(getters.items()):
                if site not in listed and site in self.symbols:
                    listed.add(site)
                    out.append(
                        (
                            site,
                            f"{site} {how} and may hand a test module on to a lookup that "
                            f"can see {namespace}",
                        )
                    )
            for site, writers in sorted(self.table_getters.items()):
                # It imports what a table names, no test module, unless code
                # that may change the table ran first: in the same test, as
                # none of it runs outside every test.
                if site in listed or site not in self.symbols:
                    continue
                listed.add(site)
                how = (
                    f"{site} imports a module a literal table names, which "
                    f"{sorted(writers)[0]} may change, and may hand a test module on to a "
                    f"lookup that can see {namespace}"
                )
                if guarded is not None:
                    guarded.append((site, writers, how))
                else:
                    out.append((site, how))
            outside = sorted(g for g in getters if self._ran_outside_tests(g))
            if outside:
                # What it obtained outside every test may serve any later one.
                for site, kind in self.sites:
                    if kind in (SiteKind.ELSEWHERE, SiteKind.ANY) and site not in listed:
                        if site in self.symbols:
                            listed.add(site)
                            out.append(
                                (
                                    site,
                                    f"{site} looks names up on an object from elsewhere, and "
                                    f"{outside[0]} may have obtained {namespace} outside every "
                                    "test",
                                )
                            )
        if family is not None:
            # ``request.instance``, ``item.instance`` and ``request.cls`` hand
            # a test object to other code, which may then look anything up on
            # it. That happens within the test that read it, so its record
            # holds the read: the reader stands in for every lookup after it.
            for site in sorted(self.hands_on):
                out.append(
                    (
                        site,
                        f"{site} reads .instance or .cls and may hand a test object on to a "
                        f"lookup that can see {namespace}",
                    )
                )
        if at_import:
            listed = {site for site, _ in out}
            # Only where that code (``runs``: the import-time code that
            # changed, by default the symbol) can run one of the site's
            # writers, as ``_import_writes`` follows it, or reaches code that
            # calls what no edge shows.
            reach = self._forward(runs if runs is not None else [symbol.id])
            opaque = bool(reach & self._opaque_callers)
            for (site, _kind), writers in self.import_sites.items():
                if not opaque and not reach & writers:
                    continue
                if site not in listed and site in self.symbols:
                    listed.add(site)
                    out.append(
                        (
                            site,
                            f"{site} looks names up on an external module that code running "
                            f"at import in {namespace} may write to",
                        )
                    )
        return out

    def _runner_family(self, symbol_id: str) -> set[str] | None:
        """The runner-only class holding ``symbol_id`` with its bases and
        subclasses, or None when no runner-only class holds it."""
        current: str | None = symbol_id
        while current:
            if current in self.runner_only:
                return set(self._related(current, BASES) | self._related(current, SUBCLASSES))
            symbol = self.symbols.get(current)
            current = symbol.container if symbol is not None else None
        return None

    def _file(self, path: str, what: str, *, names: bool, source: bool = False) -> None:
        """A changed file the index does not read, or the file of a changed
        module (``source``). Its content is observed by whoever opened or
        stat'ed it; that it exists (``names``: it was added or deleted) also
        by whoever listed a directory above it. Everything is selected for a
        build input, which for a module's file is a build script as static
        planning judges it (planner.is_build_script): an ordinary
        ``pkg/build.py`` is imported like any module."""
        if is_build_script(path, self.project_dirs) if source else build_input(path):
            self._select_all(
                Rule.UNOBSERVED_FILE,
                f"{path} {what}: compiled source, build, dependency or pytest configuration, "
                "read where no test's record sees it",
            )
            return
        # The content: opened or stat'ed, or an entry of a directory scanned
        # with ``os.scandir`` (the entries' metadata is read without an event).
        observed = [(path, self.evidence.import_paths), (_scanned(path), self.evidence.import_dirs)]
        if names:
            # A name seen: a directory above it listed, or the path itself
            # stat'ed (a source file's stat is recorded as a name seen).
            observed += [(d, self.evidence.import_dirs) for d in _ancestors(path)]
        for seen, where_seen in observed:
            for module in sorted(self.evidence.seen_by(where_seen, seen)):
                where = seen or "the checkout root"
                if module == "" or module.startswith(UNINDEXED_MODULE):
                    self._select_all(
                        Rule.UNOBSERVED_FILE,
                        f"{path} {what}, and project code touched {where} outside every test "
                        "and outside any import or test module it could be credited to (a hook)",
                    )
                elif module.startswith(COLLECTED):
                    # Read while pytest collected a test module: by code that
                    # ran then, which may now do otherwise.
                    collected = module[len(COLLECTED) :]
                    why = (
                        f"{path} {what}, and {where} was touched while pytest collected {collected}"
                    )
                    self._collected_tests(collected, None, why)
                    for symbol in self._collected_code(collected):
                        self._import_effect(symbol, None, why)
                else:
                    self._escalate_module(
                        module,
                        f"{path} {what}, and {where} was touched while {module} was imported",
                    )
        if not source and self._imported_in_a_test(path):
            # The first test to import it ran its code, and its record holds
            # the file; every later test may use the module, or what it
            # built, from ``sys.modules`` without touching the file, and
            # nothing static names what reads it (audit T2).
            self._select_all(
                Rule.UNINDEXED_IMPORT,
                f"{path} {what}: a module the index does not read whose code ran inside a "
                "test, so a later test may use what its import built without running it",
            )
        self.files[path] = (what, names)

    def _imported_in_a_test(self, path: str) -> bool:
        """Whether code of ``path``, a file the index does not read, ran
        inside a test window at C (``Evidence.test_window_code``); for a store
        that did not record that, whether a test touched the ``.py`` file."""
        ev = self.evidence
        if ev.test_window_code is not None:
            return ev.has(set(ev.test_window_code), path)
        if not path.endswith(".py"):
            return False
        folded = path.casefold() if ev.case_insensitive else path
        ids = {
            i
            for i, p in enumerate(ev.paths)
            if (p.casefold() if ev.case_insensitive else p) == folded
        }
        return any(ids & record.paths for record in ev.tests.values())


def _changed_outside_roots(
    repo: Path, commit: str, other: SourceIndex, source_roots: list[str] | None
) -> dict[str, str]:
    """Paths changed between the recorded commit and ``other`` that lie
    outside every source root (none when a root is the repository itself)."""
    dirs = [split_root(r)[0] for r in (source_roots or ["."])]
    if "" in dirs:
        return {}
    revision = other.snapshot.commit
    changed = changed_paths(repo, commit, revision, other.snapshot.kind)
    return {
        path: what
        for path, what in changed.items()
        if not any(path.startswith(d + "/") for d in dirs)
        and path not in OWN_FILES
        and not path.startswith(OWN_DIRS)
    }


def plan_with_evidence(
    base: SourceIndex,
    head: SourceIndex,
    evidence: Evidence,
    evidence_index: SourceIndex,
    manifest: Manifest | None,
    *,
    repo: str = "",
    source_roots: list[str] | None = None,
    discovered: list[DiscoveryResult] | None = None,
    declarations: list[Declaration] | None = None,
    base_target_ids: set[str] | None = None,
    lifecycle_changes: LifecycleChanges | None = None,
    lifecycle_since_recording: LifecycleChanges | None = None,
) -> Plan:
    """``lifecycle_changes``: the discovered targets' lifecycle dependencies
    that moved between base and head, for targets of other runners, which
    keep their static decision; ``lifecycle_since_recording``: those of the
    pytest targets that moved between the recording and the base or the
    head (planner._since_recording). A fixture, hook or plugin a test now
    runs, and did not at C, is in no record to meet a change, so the test is
    selected (``lifecycle_changed``)."""
    discovered = list(discovered or [])
    declared = list(declarations or [])
    targets = merge_targets(manifest, discovered)
    pytest_targets = [t for t in targets if t.runner == "pytest"]
    changes = classify(base, head)
    errors = sorted(base.errors + head.errors + evidence_index.errors)

    pairs = [("head", head)]
    if not (base.snapshot.committed and base.snapshot.commit == evidence.commit):
        pairs.append(("base", base))
    test_code = _TestCode(pytest_targets, (evidence_index, base, head))
    test_code.entry_modules({**evidence_index.symbols, **base.symbols, **head.symbols})
    test_code.isolated = isolated_test_code(targets, (evidence_index, base, head))

    fallbacks: list[Fallback] = []
    if errors:
        fallbacks.append(
            Fallback(
                Rule.ANALYSIS_ERROR,
                "all_targets",
                f"{len(errors)} analysis error(s); the dependency graph is incomplete, "
                "so every supplied target is selected",
            )
        )
    observed: list[_Observers] = []
    escalated: dict[str, list[Reason]] = defaultdict(list)
    target_fallbacks: dict[str, list[Reason]] = defaultdict(list)
    changed_ids: set[str] = {c.id for c in changes if c.carries_impact}
    docstring_ids: set[str] = {c.id for c in changes if ChangeKind.DOCSTRING_CHANGED in c.changes}
    for side, other in pairs:
        runner_only = _runner_only(evidence_index, other, pytest_targets)
        obs = _Observers(evidence_index, other, evidence, test_code, declared, runner_only)
        if repo:
            obs.outside = _changed_outside_roots(Path(repo), evidence.commit, other, source_roots)
        obs.run()
        observed.append(obs)
        fallbacks += obs.fallbacks
        changed_ids |= {c.id for c in obs.changes if c.carries_impact}
        docstring_ids |= {c.id for c in obs.changes if ChangeKind.DOCSTRING_CHANGED in c.changes}
        runner = _runner_dependency_fallbacks(targets, obs.changes, evidence_index, other)
        seeds = Seeds(frozenset(obs.seed_changes), dict(obs.seed_nodes))
        static = plan_from_indexes(
            evidence_index,
            other,
            manifest,
            repo=repo,
            source_roots=source_roots,
            discovered=discovered,
            declarations=declared,
            seeds=seeds,
        )
        for decision in static.decisions:
            for r in decision.reasons:
                if r.rule == Rule.ANALYSIS_ERROR:
                    continue  # reported once, above
                if r.rule in (Rule.ESCALATED,) or r.path or r.changed_symbol:
                    escalated[decision.target.node_id].append(
                        Reason(
                            Rule.ESCALATED,
                            f"static planning of C -> {side}: {r.detail}",
                            r.path,
                            r.changed_symbol,
                            r.changes,
                        )
                    )
                else:
                    target_fallbacks[decision.target.node_id].append(r)
        for fb in runner:
            target_fallbacks[fb.target or ""].append(Reason(fb.rule, fb.detail))

    static_other: dict[str, Decision] = {}
    if any(t.runner != "pytest" for t in targets):
        full = plan_from_indexes(
            base,
            head,
            manifest,
            repo=repo,
            source_roots=source_roots,
            discovered=discovered,
            declarations=declared,
            base_target_ids=base_target_ids,
            lifecycle_changes=lifecycle_changes,
        )
        static_other = {d.target.node_id: d for d in full.decisions if d.target.runner != "pytest"}

    # What each pair's E and guarded readers are in the evidence's symbol
    # table, to meet a record's indices without spelling its symbols out.
    index_of = {s: i for i, s in enumerate(evidence.symbols)}
    met = [
        (
            frozenset(index_of[s] for s in obs.E if s in index_of),
            frozenset(index_of[s] for s in obs.guarded if s in index_of),
        )
        for obs in observed
    ]
    decisions = [
        _evidence_decision(
            target,
            evidence,
            observed,
            met,
            fallbacks,
            escalated,
            target_fallbacks,
            changed_ids,
            docstring_ids,
            base_target_ids,
            static_other,
            lifecycle_since_recording or {},
        )
        for target in targets
    ]
    plan = Plan(
        repo=repo,
        source_roots=list(source_roots or []),
        changes=changes,
        decisions=decisions,
        fallbacks=_dedupe(fallbacks),
        unresolved=[],
        errors=errors,
        declarations=sorted(declared),
        base_index=base,
        head_index=head,
        discovery=discovered,
        targets=targets,
    )
    plan.evidence = _summary(evidence, pairs, observed, changes)
    return plan


def _dedupe(fallbacks: list[Fallback]) -> list[Fallback]:
    return list(dict.fromkeys(fallbacks))


def _evidence_decision(
    target: Target,
    evidence: Evidence,
    observed: list[_Observers],
    met: list[tuple[frozenset[int], frozenset[int]]],
    fallbacks: list[Fallback],
    escalated: dict[str, list[Reason]],
    target_fallbacks: dict[str, list[Reason]],
    changed_ids: set[str],
    docstring_ids: set[str],
    base_target_ids: set[str] | None,
    static_other: dict[str, Decision],
    lifecycle_since_recording: LifecycleChanges,
) -> Decision:
    if target.runner != "pytest":
        return static_other[target.node_id]
    reasons: list[Reason] = [Reason(fb.rule, fb.detail) for fb in _dedupe(fallbacks)]
    record = evidence.tests.get(target.runner_id)
    fixture_sources = any(obs.fixture_sources_changed for obs in observed)
    for r in dict.fromkeys(target_fallbacks.get(target.node_id, ())):
        if r.rule == Rule.LIFECYCLE_UNRESOLVED and record is not None and not fixture_sources:
            # A fixture discovery could not resolve still ran, so the record
            # holds it. Only one added or redefined where discovery cannot
            # see it could reach the test unrecorded, and a fixture needs
            # pytest: nothing that uses pytest defined a function differently.
            continue
        reasons.append(r)
    short = evidence.commit[:12]
    if record is None:
        reasons.append(
            Reason(
                Rule.NO_EVIDENCE,
                f"{target.runner_id} has no record in the evidence from {short} (new, "
                "deselected or not collected then)",
            )
        )
    else:
        if record.flags & FLAG_UNSTABLE:
            reasons.append(
                Reason(
                    Rule.UNSTABLE,
                    "its record differed between two collections in different orders",
                )
            )
        if record.flags & FLAG_FAILED:
            reasons.append(
                Reason(
                    Rule.NO_EVIDENCE,
                    f"it failed or errored when recorded at {short}: its record stops "
                    "where it failed",
                )
            )
        if record.flags & FLAG_SUBPROCESS:
            reasons.append(
                Reason(Rule.SUBPROCESS, "it started a subprocess, whose execution is not recorded")
            )
        if record.flags & FLAG_TEXT:
            reasons.append(
                Reason(
                    Rule.TEXT_CODE,
                    "it ran code compiled from text no file holds (a doctest, timeit), "
                    "which may use any name",
                )
            )
        files = any(obs.files for obs in observed)
        touched = evidence.touched(record) if files else set()
        listed = evidence.listed(record) if files else set()
        executed: set[str] | None = None  # spelled out only for a guarded reader
        for obs, (e_ids, guarded_ids) in zip(observed, met, strict=True):
            hits = record.symbols & e_ids
            if hits:
                first = min(evidence.symbols[i] for i in hits)
                why = obs.E[first]
                more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
                reasons.append(
                    Reason(
                        why.rule,
                        f"executed {first} in the evidence run at {short}{more}: {why.detail}",
                        (),
                        why.changed_symbol,
                        why.changes,
                    )
                )
            for reader in sorted(evidence.symbols[i] for i in record.symbols & guarded_ids):
                if executed is None:
                    executed = evidence.executed(record)
                guard = _guarded_hit(executed, obs.guarded[reader])
                if guard is not None:
                    holder, why = guard
                    reasons.append(
                        Reason(
                            why.rule,
                            f"executed {reader} and {holder} in the evidence run at {short}: "
                            f"{why.detail}",
                            (),
                            why.changed_symbol,
                            why.changes,
                        )
                    )
                    break
            for path, (what, names) in sorted(obs.files.items()):
                how = None
                if evidence.has(touched, path):
                    how = f"opened or stat'ed {path}"
                elif evidence.has(listed, _scanned(path)):
                    how = f"scanned {_scanned(path)[: -len(SCANNED)] or 'the checkout root'}"
                elif names:
                    dirs = [d for d in _ancestors(path) if evidence.has(listed, d)]
                    if dirs and dirs[0] == path:
                        how = f"stat'ed {path}"
                    elif dirs:
                        how = f"listed {dirs[0] or 'the checkout root'}"
                if how is not None:
                    reasons.append(
                        Reason(
                            Rule.TOUCHED_FILE,
                            f"{how} in the evidence run at {short}; {path} {what}",
                        )
                    )
                    break
    for obs in observed:
        if target.runner_id in obs.direct:
            reasons.append(obs.direct[target.runner_id])
    containers = _entry_chain(target.entry_symbol)
    hit = sorted(changed_ids & set(containers))
    if hit:
        reasons.append(
            Reason(
                Rule.CHANGED_TARGET,
                f"{hit[0]} changed: the target's entry or what contains it (a skipped test's "
                "record lacks its own entry)",
                (),
                hit[0],
            )
        )
    if base_target_ids is not None and target.runner_id not in base_target_ids:
        reasons.append(
            Reason(
                Rule.NEW_TARGET,
                f"{target.runner_id} is not in the base snapshot: a new target is selected "
                "whatever its entry symbol did",
            )
        )
    moved = lifecycle_since_recording.get((target.runner, target.runner_id))
    if moved is not None:
        reasons.append(
            lifecycle_reason(
                target.runner_id, *moved, f"since the recording at {evidence.commit[:12]}"
            )
        )
    if target.entry_symbol in docstring_ids:
        reasons.append(
            Reason(
                Rule.ENTRY_DOCSTRING,
                f"the docstring of the entry symbol {target.entry_symbol} changed",
            )
        )
    reasons += escalated.get(target.node_id, [])
    return _decision(target, list(dict.fromkeys(reasons)), {})


def _guarded_hit(
    executed: set[str], guards: list[tuple[frozenset[str], frozenset[str], Reason]]
) -> tuple[str, Reason] | None:
    """The first code able to hand a guarded reader its object that the test
    ran, preferring code that names the class to the class's own members."""
    for builders, members, why in guards:
        hit = sorted(executed & builders) or sorted(executed & members)
        if hit:
            return hit[0], why
    return None


def _entry_chain(entry: str) -> list[str]:
    parts = entry.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


def _summary(
    evidence: Evidence,
    pairs: list[tuple[str, SourceIndex]],
    observed: list[_Observers],
    changes: list[SymbolChange],
) -> dict:
    in_range = {c.id for c in changes}
    outside = sorted({c.id for obs in observed for c in obs.changes} - in_range)
    return {
        "store": str(evidence.location) if evidence.location else None,
        "commit": evidence.commit,
        "environment_hash": evidence.environment_hash,
        # ``plan`` reads files only: it cannot ask the project's interpreter
        # what is installed, so whether this environment is the recorded one
        # is unknown here. ``run`` checks it in the test process before any
        # test runs, and sets this when it matched.
        "environment_checked": False,
        "python": python_version(evidence.environment),
        "hash_seed": evidence.environment.get("variables", {}).get("PYTHONHASHSEED"),
        "variables": sorted(evidence.environment.get("variables", {})),
        "created": evidence.created,
        "tests": len(evidence.tests),
        "reverse_checked": evidence.reverse_checked,
        "planned": [f"{evidence.commit[:12]} -> {side}" for side, _ in pairs],
        "changes_outside_range": outside,
        "escalated_modules": sorted({m for obs in observed for m in obs.escalated_modules}),
        "preloaded_modules": sorted(evidence.preloaded),
    }
