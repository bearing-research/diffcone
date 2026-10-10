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
  escalated (below). Code that ran outside every test and every import
  (hooks, collection) is escalated itself. Either may also have written
  into another module's state, which tests read without running it: the
  variables written in place by what it can call are changed values
  (``_import_writes``), and so are those of a changed variable
  initialiser, class body or ``def`` header that runs code;
* a function's signature, defaults, decorators or annotations: also its
  readers (resolved references and name matches, one hop), the lookup and
  reflection sites that can see its namespace, and escalation when the
  ``def`` runs code at import. A name match on a member of a class in test
  code, and a library lookup by a name nothing bounds, is guarded: it
  selects a test only if the test also ran code that can hand it an
  instance (``_Observers._holders``), which includes code finding classes
  without naming them (``__subclasses__``, ``gc``);
* a variable: its readers and the lookup and reflection sites that can see
  its namespace (a lookup by a name nothing bounds reads the value too); a
  reader that is itself a variable captured the value and is followed in
  turn; a module's or class's top-level reader escalates its module;
* a class body: the attributes whose statements changed, through every
  reader of those names and the lookup and reflection sites; an opaque
  body statement, a dunder attribute or a changed class statement (bases,
  decorators, keywords) escalates, and so does any attribute change of a
  class whose creation may read its body (``SourceIndex.open_classes``:
  decorators, keywords, a base the index cannot see, such as a dataclass
  field default or an ``Enum`` member; or an ancestor with
  ``__init_subclass__``). A special method (``__eq__``) is used without
  being named, so a change to one reaches every member of the class's
  hierarchy; a module's ``__getattr__``/``__dir__`` escalates its module;
* an added or deleted name: its readers, the unbounded lookup and
  reflection sites that can see the namespace, and for a deletion the
  modules importing it (their import now fails); an added or deleted
  definition that runs code at import (a decorated function, a class whose
  creation runs code) escalates too;
* module-level code: escalates the module, and so does a change to
  ``__all__`` (what star imports bind);
* a non-Python file, or any file outside the source roots (read from a
  git diff, since the index holds only the roots): the tests that touched
  it or a directory above it; everything when it is compiled source, build
  or pytest configuration, a ``conftest.py`` outside the roots, or was
  touched outside every test;
* test code other than a function body: the tests that list the symbol as
  a lifecycle dependency (a fixture) or are collected from its class or a
  subclass, and for a test module's variable (``pytestmark``) every test
  of the module, since pytest reads marks, fixtures and parameters without
  a static reader. ``pytest_plugins`` in any module selects everything:
  the plugins it names serve the whole session.

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
(``_Observers._written_in_tests``, ``import_sites``).

Targets of other runners, which have no evidence, keep their static
decision. A pytest target with no record (new, or never run) is selected.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from diffcone.classify import (
    ADDED,
    ANNOTATIONS_CHANGED,
    BODY_CHANGED,
    DEFINITION_CHANGED,
    DELETED,
    DEPENDENCIES_CHANGED,
    DOCSTRING_CHANGED,
    SymbolChange,
    classify,
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
    FLAG_SUBPROCESS,
    FLAG_TEXT,
    FLAG_UNSTABLE,
    UNINDEXED_MODULE,
    Evidence,
)
from diffcone.manifest import Manifest, Target
from diffcone.model import (
    CLASS,
    CLASS_STATEMENT,
    DECLARED,
    EXTERNAL_WRITTEN,
    FUNCTION,
    IMPORTS,
    IMPORTS_NAME,
    METHOD,
    MODULE,
    OPAQUE_ATTRIBUTE,
    REFERENCES,
    UNRESOLVED_ATTRIBUTE,
    UNRESOLVED_DYNAMIC,
    VARIABLE,
    SourceIndex,
    Symbol,
)
from diffcone.planner import (
    BUILD_SCRIPTS,
    OWN_DIRS,
    OWN_FILES,
    RULE_ANALYSIS_ERROR,
    RULE_CHANGED_TARGET,
    RULE_CYTHON_CALLER,
    RULE_ENTRY_DOCSTRING,
    RULE_ESCALATED,
    RULE_EXECUTED_CHANGED,
    RULE_EXECUTED_READER,
    RULE_LIFECYCLE_UNRESOLVED,
    RULE_LOOKUP_SITE,
    RULE_NEW_TARGET,
    RULE_NO_EVIDENCE,
    RULE_PYTEST_HOOK,
    RULE_SUBPROCESS,
    RULE_TEST_SCOPE,
    RULE_TEXT_CODE,
    RULE_TOUCHED_FILE,
    RULE_UNINDEXED_IMPORT,
    RULE_UNOBSERVED_FILE,
    RULE_UNSTABLE,
    Decision,
    Fallback,
    Plan,
    Reason,
    Seeds,
    _changed_unanalysed_files,
    _decision,
    _ImportReach,
    _inside,
    _is_dunder,
    _members_by_container,
    _runner_dependency_fallbacks,
    build_input,
    merge_targets,
    plan_from_indexes,
)
from diffcone.snapshot import changed_paths, split_root

# Module-level names in a conftest that pytest reads to decide what to load
# or collect at all.
PYTEST_COLLECTION_NAMES = frozenset({"pytest_plugins", "collect_ignore", "collect_ignore_glob"})

# Attributes and modules that hand out objects without naming them: a
# class's subclasses, a function's or frame's globals, a closure cell, every
# object the collector tracks.
GRAPH_ATTRIBUTES = frozenset(
    {"__subclasses__", "__globals__", "f_globals", "f_locals", "cell_contents"}
)
GRAPH_MODULES = frozenset({"gc"})

# What calling or creating a class runs.
_CONSTRUCTORS = frozenset(
    {"__new__", "__init__", "__post_init__", "__call__", "__init_subclass__", "__set_name__"}
)

# UNRESOLVED_DYNAMIC details, by what the site can see.
SITE_CLOSURE = "closure"  # a name looked up on a module global: its import closure
SITE_ELSEWHERE = "elsewhere"  # on an object from anywhere
SITE_IMPORT = "import"  # a module named at run time
SITE_ANY = "any"  # eval/exec/run_path


def _site_kind(detail: str) -> str:
    if detail.endswith(EXTERNAL_WRITTEN):
        # On an external module: in-scope code anywhere may have stored there,
        # not only the site's own import closure.
        return SITE_ANY
    if "import" in detail or detail.startswith("runpy.run_module"):
        return SITE_IMPORT
    if detail.startswith(("getattr(<non-literal>) on a receiver from elsewhere", "vars(")):
        return SITE_ELSEWHERE
    if detail.startswith(("getattr(<non-literal>)", "globals(")):
        return SITE_CLOSURE
    return SITE_ANY  # eval, exec, runpy.run_path, anything not recognised


def _ancestors(path: str) -> list[str]:
    """The path and every directory above it, the checkout root as ""."""
    out = [path]
    while "/" in path:
        path = path.rsplit("/", 1)[0]
        out.append(path)
    out.append("")
    return out


class _TestCode:
    """Which modules are test code: those holding a pytest target (by file or
    entry symbol) and every conftest, and which tests each one scopes."""

    def __init__(self, targets: list[Target], indexes: Iterable[SourceIndex]) -> None:
        self.module_of_path: dict[str, str] = {}
        self.path_of_module: dict[str, str] = {}
        for index in indexes:
            for symbol in index.symbols.values():
                if symbol.kind == MODULE:
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


def _runner_only(c_index: SourceIndex, other: SourceIndex, targets: list[Target]) -> set[str]:
    """Test classes (with the bases they inherit from) whose instances only
    pytest ever holds: planner._runner_only_classes, with two differences.
    A class that is only ever a base of test classes (pandas' ExtensionTests
    combines a dozen Base*Tests) counts too, since its instances are the
    test classes' instances. And a read of ``.instance`` does not turn the
    rule off: the tests that executed one are selected instead (see
    ``_Observers._sites``)."""
    symbols = {**c_index.symbols, **other.symbols}
    bases, _ = _class_graph((c_index, other))
    runner: set[str] = set()
    for target in targets:
        entry = symbols.get(target.entry_symbol)
        if entry is not None and entry.kind == METHOD and entry.container:
            runner.add(entry.container)
        for dep in target.lifecycle_dependencies:
            owner = symbols.get(dep)
            if owner is not None and owner.kind == CLASS:
                runner.add(dep)
    stack = list(runner)
    while stack:
        for base in bases.get(stack.pop(), ()):
            if base not in runner:
                runner.add(base)
                stack.append(base)
    if not runner:
        return set()
    held = (c_index.escaped_classes | other.escaped_classes) & runner
    for index in (c_index, other):
        for edge in index.edges:
            if (
                edge.kind == REFERENCES
                and edge.target in runner
                and not _inside(edge.source, runner, c_index, other)
            ):
                held.add(edge.target)
    stack = list(held)
    while stack:  # a held subclass holds its bases: its instances carry their methods
        for base in bases.get(stack.pop(), ()):
            if base in runner and base not in held:
                held.add(base)
                stack.append(base)
    return runner - held


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
        self.sites: list[tuple[str, str]] = []  # (symbol, kind)
        # Sites on an external module that only test-window code writes to
        # (``_written_in_tests``) -> the writers: seen only by a change to
        # code that runs at import (``import_sites``).
        bounded: dict[tuple[str, str], set[str]] = defaultdict(set)
        unbounded: set[tuple[str, str]] = set()  # the other external sites
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
        # Code a symbol's code can run (references, forward), and the
        # variables each function writes into in place (``mutated_by``).
        self.calls: dict[str, set[str]] = defaultdict(set)
        self.writes: dict[str, set[str]] = defaultdict(set)
        for index in (c_index, other):
            for edge in index.edges:
                if edge.kind in (REFERENCES, DECLARED):
                    self.readers_of[edge.target].add(edge.source)
                    if edge.detail.startswith("attribute:"):
                        self.attribute_readers[edge.detail[len("attribute:") :]].add(edge.source)
                    if edge.detail == "mutated_by":
                        self.writes[edge.target].add(edge.source)
                    elif edge.detail != "registers":
                        self.calls[edge.source].add(edge.target)
                elif edge.kind in (IMPORTS, IMPORTS_NAME):
                    self.importers_of[edge.target].add(edge.source)
            for ref in index.unresolved:
                if ref.kind == UNRESOLVED_DYNAMIC:
                    site = (ref.symbol, _site_kind(ref.detail))
                    if site[1] == SITE_IMPORT:
                        # Imports a module named at run time (or runs code
                        # read at run time): it can hold any module. Code
                        # generated in the program reaches what its module
                        # can, as static planning bounds it.
                        self.module_getters.add(ref.symbol)
                    writers = external.get((ref.symbol, ref.detail))
                    if writers and self._written_in_tests(writers):
                        bounded[site].update(writers)
                    else:
                        if writers:
                            unbounded.add(site)
                        self.sites.append(site)
                elif ref.name:
                    self.by_name[ref.name].add(ref.symbol)
                    if ref.kind == UNRESOLVED_ATTRIBUTE and ref.name in GRAPH_ATTRIBUTES:
                        self.graph_readers.add(ref.symbol)
            for x in index.external:
                symbol_ = index.symbols.get(x.symbol)
                if x.module.split(".")[0] in GRAPH_MODULES and (
                    symbol_ is None or symbol_.kind != MODULE  # the import statement
                ):
                    self.graph_readers.add(x.symbol)
            for symbol, detail in index.reflection:
                if detail[1:] in GRAPH_ATTRIBUTES:
                    # Hands objects out (``type.__subclasses__(c)``); looks no
                    # name up.
                    self.graph_readers.add(symbol)
                    continue
                writers = external.get((symbol, detail))
                if writers and self._written_in_tests(writers):
                    bounded[(symbol, SITE_ANY)].update(writers)
                else:
                    if writers:
                        unbounded.add((symbol, SITE_ANY))
                    self.sites.append((symbol, SITE_ANY))
        for decl in declarations:
            self.readers_of[decl.target].add(decl.source)
        self.hands_on = {
            ref.symbol
            for index in (c_index, other)
            for ref in index.unresolved
            if ref.kind == UNRESOLVED_ATTRIBUTE and ref.name in ("instance", "cls")
        }
        self.module_hands_on = {
            ref.symbol
            for index in (c_index, other)
            for ref in index.unresolved
            if ref.kind == UNRESOLVED_ATTRIBUTE and ref.name == "module"
        } | {
            symbol
            for index in (c_index, other)
            for symbol, detail in index.reflection
            if detail == ".modules"
        }
        self.module_hands_on = {s for s in self.module_hands_on if self._in_test_code(s)}
        self.sites = sorted(set(self.sites))
        self.members = _members_by_container(set(self.symbols))
        self.reach = _ImportReach(c_index, other)
        self.bases, self.subclasses = _class_graph((c_index, other))

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
            and c.symbol.kind in (FUNCTION, METHOD, CLASS)
            and c.symbol.module in pytest_modules
            and {ADDED, DELETED, DEFINITION_CHANGED} & set(c.changes)
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
        # Cython name -> the Cython functions at C that mention it (lazily).
        self._cython_mentions: dict[str, list[tuple[str, CythonFunction]]] | None = None
        # The lookups on an external module that a change to code running at
        # import may make write there, for every later test (``_seeing_sites``):
        # site -> None when any such change may (code outside the tests can
        # run a writer), else the test code that can run one (``_callers``).
        self.import_sites: dict[tuple[str, str], frozenset[str] | None] = {
            site: None for site in unbounded
        }
        for site, writers in sorted(bounded.items()):
            self.import_sites[site] = self._callers(writers)

    # -- recording --------------------------------------------------------------

    def _in_test_code(self, symbol_id: str) -> bool:
        symbol = self.symbols.get(symbol_id)
        return symbol is not None and self.test_code.is_test_code(symbol.module)

    def _written_in_tests(self, writers: set[str]) -> bool:
        """Whether a lookup on an external module written by ``writers`` can
        find the project's code only in a test that ran one of them (roadmap
        item 14): each is a function or method, and none ran at C during an
        import or outside every test. A write at import (module or class
        top-level code, a decorator) stays for every later test."""
        ev = self.evidence
        for writer in writers:
            symbol = self.symbols.get(writer)
            if symbol is None or symbol.kind not in (FUNCTION, METHOD):
                return False
            if writer in ev.import_phase or writer in ev.import_by or writer in ev.hook_phase:
                return False
        return True

    def _callers(self, writers: set[str]) -> frozenset[str] | None:
        """The code that can run one of ``writers``: each writer and every
        function that can call one (a reference, a name match, a lookup site
        that can see it), transitively, with the top-level code (of a module,
        a class, a variable) that does; top-level code ends a chain, since it
        runs at its module's import. None unless all of it is test code and
        none is used as a value (a callback, a registry entry): then library
        code running at import cannot run a writer, and test code can only
        through a symbol of the set."""
        escaped = self.c.escaped_values | self.other.escaped_values
        seen = set(writers)
        stack = list(writers)
        while stack:
            current = stack.pop()
            if not self._in_test_code(current) or current in escaped:
                return None
            symbol = self.symbols[current]
            if symbol.kind not in (FUNCTION, METHOD):
                continue
            callers = set(self.readers_of.get(current, ()))
            if not _is_dunder(symbol.name):
                callers |= self.by_name.get(symbol.name, set())
            callers.update(site for site, _ in self._seeing_sites(symbol))
            for caller in callers - seen:
                seen.add(caller)
                stack.append(caller)
        return frozenset(seen)

    def _observe(self, symbol: str, rule: str, detail: str, change: SymbolChange | None) -> None:
        if symbol not in self.E:
            self.E[symbol] = Reason(
                rule,
                detail,
                (),
                change.id if change is not None else None,
                change.changes if change is not None else (),
            )

    def _select_all(self, rule: str, detail: str) -> None:
        self.fallbacks.append(Fallback(rule, "all_targets", detail))

    def _scope(self, symbol: Symbol, change: SymbolChange) -> None:
        classes: set[str] = set()
        current: str | None = symbol.id if symbol.kind == CLASS else symbol.container
        while current:
            holder = self.symbols.get(current)
            if holder is None:
                break
            if holder.kind == CLASS:
                stack = [current]
                while stack:
                    cls = stack.pop()
                    if cls not in classes:
                        classes.add(cls)
                        stack.extend(self.subclasses.get(cls, ()))
            current = holder.container
        whole_module = (
            symbol.kind == VARIABLE
            and symbol.container == symbol.module
            and symbol.module not in self.test_code.conftests
        )
        for test in self.test_code.scope(symbol, classes, whole_module=whole_module):
            self.direct.setdefault(
                test,
                Reason(
                    RULE_TEST_SCOPE,
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
        if self.changes or _changed_unanalysed_files(self.c, self.other) or self.outside:
            self._unrecorded_phases()
        decorated = self.c.doc_decorated | self.other.doc_decorated
        for change in self.changes:
            if change.symbol.path in BUILD_SCRIPTS:
                self._file(change.symbol.path, "edited", names=False)
                continue
            if change.carries_impact:
                self._change(change)
            elif DOCSTRING_CHANGED in change.changes and change.id in decorated:
                # Its decorator reads the docstring when the module is
                # imported: no record shows that, the import-time state does.
                self._escalate_change(
                    change, f"{change.id} docstring_changed: its decorator reads it at import"
                )
        cython = self._cython()
        for path in _changed_unanalysed_files(self.c, self.other):
            if path in cython:
                continue
            before, after = path in self.c.other_files, path in self.other.other_files
            what = "edited" if before and after else ("added" if after else "deleted")
            self._file(path, what, names=not (before and after))
        for change in self.changes:
            symbol = change.symbol
            if symbol.kind == MODULE and {ADDED, DELETED} & set(change.changes):
                what = f"{change.id} {'/'.join(change.changes)}"
                self._file(symbol.path, what, names=True)
        # A source file is also data to whoever reads it as a file
        # (``exec(open("version.py").read())``, ``inspect.getsource``): its
        # edit reaches the tests that opened it.
        for path in sorted({c.symbol.path for c in self.changes} - set(self.files)):
            if path.endswith(".py") and path not in BUILD_SCRIPTS:
                self._file(path, "edited", names=False)
        for path, what in sorted(self.outside.items()):
            if path.rsplit("/", 1)[-1] == "conftest.py":
                # pytest loads it for tests that never ran a line of it (a new
                # autouse fixture, a hook): no record can vouch for them.
                self._select_all(
                    RULE_UNOBSERVED_FILE,
                    f"{path} {what}: a conftest.py outside the source roots, which the "
                    "index does not read",
                )
            else:
                self._file(path, what, names=what != "edited")

    def _unrecorded_phases(self) -> None:
        """Outside every test, something ran that no record shows (a process
        nothing recorded, text code): any change may reach what that import
        built, or, outside every import (a hook, collection), any test."""
        for module in sorted(self.evidence.import_flagged):
            why = "a process no record follows, or code compiled from text,"
            if module == "" or module.startswith(UNINDEXED_MODULE):
                where = module[len(UNINDEXED_MODULE) :] if module else ""
                self._select_all(
                    RULE_SUBPROCESS,
                    f"{why} ran outside every test "
                    + (
                        f"while {where}, a file outside the source roots, was imported"
                        if where
                        else "and outside any import (a hook or collection)"
                    ),
                )
            else:
                self._escalate_module(module, f"{why} ran while {module} was imported")

    def _cython(self) -> set[str]:
        """Cython sources (roadmap item 7): the paths this rule handled.

        A body edit selects the tests that executed the function in a
        profiled build at C, and for a ``nogil`` or ``cpdef`` function (which
        such a build does not always report) the tests that executed any
        Cython function naming it. Anything else, or a module the store holds
        no Cython record of (built without ``profile=True``), selects all."""
        paths = {
            p
            for p in _changed_unanalysed_files(self.c, self.other)
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
                RULE_UNOBSERVED_FILE,
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
            self._cython_ran(path, function, RULE_EXECUTED_CHANGED, label, label, recorded)
        for changed in changes.names:
            self._cython_name(changed, recorded)
        return paths

    def _cython_ran(
        self,
        path: str,
        function: CythonFunction,
        rule: str,
        detail: str,
        label: str,
        recorded: set[str],
    ) -> None:
        """The tests that executed ``function`` at C observe the change;
        when it is ``nogil`` or ``cpdef``, which a profiled build does not
        always report, so do those that executed a Cython function naming
        it."""
        sid = symbol_id(path, function.name)
        if path not in recorded:
            self._select_all(
                RULE_UNOBSERVED_FILE,
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
                    RULE_CYTHON_CALLER,
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
                    RULE_EXECUTED_READER,
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
                self._reader(reader, None, label)
            # A lookup by a name nothing bounds may find it, and read its
            # value as well as notice it come or go.
            library = {site for site, callers in self.import_sites.items() if callers is None}
            for site, kind in sorted(set(self.sites) | library):
                if kind != SITE_IMPORT and site in self.symbols:
                    self._observe(
                        site,
                        RULE_LOOKUP_SITE,
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
                        RULE_EXECUTED_READER,
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
                    RULE_EXECUTED_READER,
                    f"{symbol_id(path, function.name)} names {name}, whose instances "
                    f"changed; {label}",
                    label,
                    recorded,
                )
            for reader in sorted(self.by_name.get(name, ())):
                self._reader(reader, None, label)

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
        any of them that is itself ``nogil`` or ``cpdef``, theirs."""
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
        elsewhere that later tests read (``_cython_writes``)."""
        if self.evidence.import_by.get(symbol_id_):
            self._cython_writes(symbol_id_, label, recorded)
        for module in sorted(self.evidence.import_by.get(symbol_id_, ())):
            if module.startswith(UNINDEXED_MODULE):
                self._select_all(
                    RULE_UNINDEXED_IMPORT,
                    f"{label}: {symbol_id_} ran while a file outside the source roots was imported",
                )
            else:
                self._escalate_module(module, f"{symbol_id_} ran while {module} was imported")
        if symbol_id_ in self.evidence.hook_phase:
            self._select_all(
                RULE_UNOBSERVED_FILE,
                f"{label}: {symbol_id_} ran outside every test (a hook or collection)",
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
                RULE_UNOBSERVED_FILE,
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
                if symbol.kind == VARIABLE:
                    self._variables_by_name[symbol.name].add(symbol.id)
        for n in sorted(function.names):
            for variable in sorted(self._variables_by_name.get(n, ())):
                if variable in self._followed:
                    continue
                self._followed.add(variable)
                why = f"{variable} may be written by {cause}"
                self._observe(variable, RULE_EXECUTED_READER, why, None)
                self._readers(variable, None, why)
                self._sites(self.symbols[variable], None, why, at_import=True)

    def _change(self, change: SymbolChange) -> None:
        symbol = change.symbol
        kinds = set(change.changes)
        test = self.test_code.is_test_code(symbol.module)
        label = f"{change.id} {'/'.join(change.changes)}"
        if symbol.kind in (FUNCTION, METHOD) and symbol.name.startswith("pytest_"):
            self._select_all(RULE_PYTEST_HOOK, f"{label}: a pytest hook can change any test")
            return
        if (
            symbol.kind == VARIABLE
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
            self._select_all(RULE_PYTEST_HOOK, f"{label}: it decides what pytest loads")
            return
        body_only = symbol.kind in (FUNCTION, METHOD) and kinds <= {
            BODY_CHANGED,
            DEPENDENCIES_CHANGED,
            DOCSTRING_CHANGED,
        }
        if test and not body_only and change.id not in self.test_code.entries:
            # A test's own change reaches the targets it is the entry of
            # (changed_target); anything else in test code reaches its scope.
            self._scope(symbol, change)
        if symbol.kind == MODULE:
            if kinds & {ADDED, DELETED}:
                self._observe(change.id, RULE_EXECUTED_CHANGED, label, change)
                self._readers(change.id, change, label)
                if DELETED in kinds:
                    self._importers(change.id, change, label)
                self._sites(symbol, change, label, imports=True, at_import=True)
            else:
                self._escalate_change(change, f"{label}: module-level code runs at import")
            return
        if symbol.kind in (FUNCTION, METHOD):
            self._observe(change.id, RULE_EXECUTED_CHANGED, label, change)
            self._import_effect(change.id, change, label)
            if kinds & {ADDED, DELETED}:
                self._readers(change.id, change, label)
                if DELETED in kinds:
                    self._importers(change.id, change, label)
                self._sites(symbol, change, label)
                # Decorators and defaults are what can register a function;
                # its annotations cannot.
                inert = all(s.inert_header for s in (change.base, change.head) if s is not None)
                if not inert:
                    # ``@register def two()``: the decorator runs at import and
                    # may change shared state no test's record names. A
                    # test's own decorators reach other tests only through
                    # what they write (``@parametrize("x", [set_mode(1)])``).
                    why = f"{label}: its definition runs code at import"
                    self._import_writes([change.id], change, why)
                    if change.id not in self.test_code.entries:
                        self._escalate_change(change, why)
            elif kinds & {DEFINITION_CHANGED, ANNOTATIONS_CHANGED}:
                self._readers(change.id, change, label)
                self._sites(symbol, change, label)
                runs_code = not all(
                    s.inert_definition for s in (change.base, change.head) if s is not None
                )
                if runs_code:
                    why = f"{label}: its definition runs code at import"
                    self._import_writes([change.id], change, why)
                    if change.id not in self.test_code.entries:
                        self._escalate_change(change, why)
            if _is_dunder(symbol.name) and not body_only and symbol.container:
                container = self.symbols.get(symbol.container)
                if container is not None and container.kind == CLASS:
                    self._hierarchy(container.id, change, label)
                elif container is not None and container.kind == MODULE:
                    # A module ``__getattr__``/``__dir__`` (PEP 562) serves every
                    # lookup the module does not answer, wherever it is made.
                    self._escalate_change(
                        change, f"{label}: a module's {symbol.name} serves its missing names"
                    )
            return
        if symbol.kind == VARIABLE and symbol.name == "__all__":
            # What ``from m import *`` binds in every star importer: no record
            # names the variable, the import statement reads it.
            self._escalate_change(change, f"{label}: star imports of the module bind its names")
        if symbol.kind == VARIABLE:
            self._observe(change.id, RULE_EXECUTED_CHANGED, label, change)
            self._readers(change.id, change, label)
            # A lookup by a name nothing bounds reads the value as well as
            # noticing the name come or go: a variable runs no code of its
            # own for the record to show. Its initialiser runs at import.
            self._sites(symbol, change, label, at_import=True)
            if DELETED in kinds:
                self._importers(change.id, change, label)
            if not all(s.inert_definition for s in (change.base, change.head) if s is not None):
                # ``X = set_mode("fast")``: what the initialiser's calls write
                # is read elsewhere, by tests that never run it.
                self._import_writes([change.id], change, f"{label}: its initialiser runs code")
            return
        if symbol.kind == CLASS:
            self._observe(change.id, RULE_EXECUTED_CHANGED, label, change)
            if kinds & {ADDED, DELETED, BODY_CHANGED}:
                # Its body runs at import, and what its calls write is read
                # elsewhere (``x = set_mode("fast")`` as a class attribute).
                self._import_writes([change.id], change, f"{label}: its body runs at import")
            if kinds & {ADDED, DELETED}:
                self._readers(change.id, change, label)
                self._sites(symbol, change, label, at_import=True)
                if DELETED in kinds:
                    self._importers(change.id, change, label)
                if self._runs_on_creation(symbol.id):
                    # ``class B(Base)`` whose base registers subclasses, a
                    # decorated or metaclassed class: creating it runs code.
                    self._escalate_change(change, f"{label}: creating the class runs code")
                return
            before = self.c.class_attributes.get(symbol.id, {})
            after = self.other.class_attributes.get(symbol.id, {})
            if DEPENDENCIES_CHANGED in kinds or before.get(CLASS_STATEMENT) != after.get(
                CLASS_STATEMENT
            ):
                # Bases, decorators, metaclass keywords (or what the body's
                # names resolve to): they run at import and may register the
                # class or rebuild it. A definition change that is only an
                # added or deleted member is that member's own change.
                self._escalate_change(change, f"{label}: the class statement runs at import")
            if BODY_CHANGED in kinds:
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
            seen = {class_id}
            stack = [class_id]
            while stack:
                cls = stack.pop()
                if cls in index.open_classes:
                    return True
                if cls != class_id and f"{cls}.__init_subclass__" in index.symbols:
                    return True
                for base in index.class_bases.get(cls, ()):
                    if base not in seen:
                        seen.add(base)
                        stack.append(base)
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
                self._reader(reader, change, attribute)
        self._sites(symbol, change, label, at_import=True)

    def _hierarchy(self, cls: str, change: SymbolChange, label: str) -> None:
        """Members of the class, its bases and its subclasses: code running on
        an instance of any of them may see the change."""
        related = {cls}
        for graph in (self.bases, self.subclasses):
            stack = [cls]
            while stack:
                for nxt in graph.get(stack.pop(), ()):
                    if nxt not in related:
                        related.add(nxt)
                        stack.append(nxt)
        for c in sorted(related):
            for member in self.members.get(c, ()):
                kind = self.symbols[member].kind
                if kind in (FUNCTION, METHOD, CLASS):
                    self._observe(
                        member,
                        RULE_EXECUTED_READER,
                        f"{member} belongs to the hierarchy of {cls}; {label}",
                        change,
                    )
            if c != cls and c in self.subclasses.get(cls, set()):
                self._readers(c, change, label)

    def _readers(self, target: str, change: SymbolChange | None, label: str) -> None:
        readers = set(self.readers_of.get(target, ()))
        name = target.rsplit(".", 1)[-1]
        if not _is_dunder(name):
            matched = self.by_name.get(name, set()) - readers - {target}
            symbol = self.symbols.get(target)
            container = self.symbols.get(symbol.container) if symbol and symbol.container else None
            if container is not None and container.kind == CLASS:
                own = change is not None and target == change.id
                matched = self._guard(
                    matched, container.id, change, label if own else f"{label} via {target}"
                )
            readers |= matched
        readers.discard(target)
        for reader in sorted(readers):
            own = change is not None and target == change.id
            self._reader(reader, change, label if own else f"{label} via {target}")

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
            if symbol is None or symbol.kind not in (FUNCTION, METHOD):
                rest.add(reader)
                continue
            why = Reason(
                RULE_EXECUTED_READER,
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
        family = {cls}
        stack = [cls]
        while stack:
            for sub_ in self.subclasses.get(stack.pop(), ()):
                if sub_ not in family:
                    family.add(sub_)
                    stack.append(sub_)
        ancestors = set(family)
        stack = list(family)
        while stack:
            for base in self.bases.get(stack.pop(), ()):
                if base not in ancestors:
                    ancestors.add(base)
                    stack.append(base)
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
                if self.symbols[member].kind in (FUNCTION, METHOD):
                    members.add(member)
        # Code that finds the class without naming it: through an ancestor's
        # ``__subclasses__()`` (or any class's: ``object.__subclasses__()``
        # walks every class), the collector, a frame's or function's globals.
        builders |= self.graph_readers
        ev = self.evidence
        for builder in builders:
            symbol = self.symbols.get(builder)
            if symbol is None or symbol.kind not in (FUNCTION, METHOD):
                return None
        for holder in builders | members:
            if holder in ev.import_phase or holder in ev.import_by or holder in ev.hook_phase:
                return None
        self._holders_of[cls] = (frozenset(builders), frozenset(members))
        return self._holders_of[cls]

    def _reader(self, reader: str, change: SymbolChange | None, label: str) -> None:
        symbol = self.symbols.get(reader)
        if symbol is None:
            return
        if symbol.kind in (FUNCTION, METHOD):
            self._observe(reader, RULE_EXECUTED_READER, f"{reader} reads {label}", change)
            self._import_effect(reader, change, f"{label}, read by {reader}")
        elif symbol.kind == VARIABLE:
            # Its initialiser captured the value: it changed too.
            if reader not in self._followed:
                self._followed.add(reader)
                self._observe(reader, RULE_EXECUTED_READER, f"{reader} reads {label}", change)
                self._readers(reader, change, label)
                self._sites(symbol, change, f"{label}, captured by {reader}", at_import=True)
        else:
            # Module or class top-level code: import-time state.
            self._escalate_module(symbol.module, f"top-level code of {reader} reads {label}")

    def _importers(self, target: str, change: SymbolChange, label: str) -> None:
        """A deleted name or module: whoever imports it fails there."""
        for importer in sorted(self.importers_of.get(target, ())):
            symbol = self.symbols.get(importer)
            if symbol is None:
                continue
            if symbol.kind == MODULE:
                self._escalate_module(importer, f"{importer} imports {label}")
            else:
                self._observe(importer, RULE_EXECUTED_READER, f"{importer} imports {label}", change)

    def _import_effect(self, symbol_id: str, change: SymbolChange | None, label: str) -> None:
        """Code that ran at C while a module was imported built that module's
        import-time state; code that ran outside every test and import ran in
        a hook or during collection. Either may also have written into
        another module's state, which every later test reads
        (``_import_writes``)."""
        ev = self.evidence
        if ev.import_by.get(symbol_id) or symbol_id in ev.hook_phase:
            self._import_writes(
                [symbol_id], change, f"{symbol_id} ran outside every test at C; {label}"
            )
        for module in sorted(self.evidence.import_by.get(symbol_id, ())):
            if module.startswith(UNINDEXED_MODULE):
                self._select_all(
                    RULE_UNINDEXED_IMPORT,
                    f"{label}: {symbol_id} ran while {module[len(UNINDEXED_MODULE) :]}, a file "
                    "outside the source roots, was imported",
                )
            else:
                self._escalate_module(
                    module, f"{symbol_id} ran while {module} was imported; {label}"
                )
        if symbol_id in self.evidence.hook_phase:
            reason = f"{symbol_id} ran outside every test (a hook or collection); {label}"
            if change is not None and symbol_id == change.id:
                self._escalate_change(change, reason)
            elif symbol_id not in self.seed_nodes:
                self.seed_nodes[symbol_id] = reason

    def _escalate_change(self, change: SymbolChange, why: str) -> None:
        """Plan the change statically (it runs at import). What it built is
        seen through the namespace holding it: a module-level change through
        the module, a decorated member through its class, so the lookup
        sites are those that can see that namespace."""
        self.seed_changes.add(change.id)
        symbol = change.symbol
        self._module_symbols(symbol.module, why)
        self._sites(symbol, change, why, imports=symbol.kind == MODULE, at_import=True)
        sources = self._import_code(symbol.module) if symbol.kind == MODULE else [change.id]
        self._import_writes(sources, change, why)

    def _escalate_module(self, module: str, why: str) -> None:
        """Plan the module's import statically, as if its top-level code
        changed: what ran during it (or what it read) did."""
        if module not in self.seed_nodes:
            self.seed_nodes[module] = why
        self._module_symbols(module, why)
        module_symbol = self.symbols.get(module)
        if module_symbol is not None:
            self._sites(module_symbol, None, why, imports=True, at_import=True)
        self._import_writes(self._import_code(module), None, why)

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
        for symbol in self.symbols.values():
            if symbol.module != module:
                continue
            if symbol.kind in (MODULE, VARIABLE, CLASS) or (
                symbol.kind in (FUNCTION, METHOD)
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
        its constructors. A write through an argument or a receiver is not
        modelled (static planning does not model it either)."""
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
            if symbol.kind == CLASS and current not in start:
                stack.extend(sorted(self._constructors(current), reverse=True))
                continue
            if symbol.kind not in (FUNCTION, METHOD) and current not in start:
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
                self._observe(variable, RULE_EXECUTED_READER, f"{variable}: {why}", change)
                self._readers(variable, change, why)
                self._sites(written, change, why, at_import=True)
            stack.extend(sorted(self.calls.get(current, ()), reverse=True))

    def _constructors(self, cls: str) -> list[str]:
        """What calling or creating ``cls`` can run: the constructors,
        ``__call__``, ``__init_subclass__`` and ``__set_name__`` of it and
        its ancestors."""
        out: list[str] = []
        seen = {cls}
        stack = [cls]
        while stack:
            current = stack.pop()
            for member in self.members.get(current, ()):
                if member.rsplit(".", 1)[-1] in _CONSTRUCTORS:
                    out.append(member)
            for base in self.bases.get(current, ()):
                if base not in seen:
                    seen.add(base)
                    stack.append(base)
        return out

    def _module_symbols(self, module: str, why: str) -> None:
        """Every symbol of a module whose import-time state may differ: code
        running there can read that state without naming it."""
        if module in self.escalated_modules:
            return
        self.escalated_modules.add(module)
        for symbol in self.symbols.values():
            if symbol.module == module and symbol.kind in (FUNCTION, METHOD, CLASS, MODULE):
                self._observe(
                    symbol.id,
                    RULE_ESCALATED,
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
    ) -> None:
        handed: list[str] = []
        for site, detail in self._seeing_sites(
            symbol, imports=imports, at_import=at_import, handed=handed
        ):
            self._observe(site, RULE_LOOKUP_SITE, f"{detail}; {label}", change)
        if handed and symbol.container:
            # Library code finds a test class's member only on an instance
            # (or the class) a test handed it: guarded like a name match.
            what = f"{label} (a lookup by a name nothing bounds)"
            for site in sorted(self._guard(set(handed), symbol.container, change, what)):
                self._observe(
                    site,
                    RULE_LOOKUP_SITE,
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
        handed: list[str] | None = None,
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
        ever instantiates (planner._runner_only_classes) is seen only by sites
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
        module_name = test and (symbol.kind == MODULE or symbol.container == namespace)
        for site, kind in self.sites:
            site_symbol = self.symbols.get(site)
            if site_symbol is None:
                continue
            library = test and not self.test_code.is_test_code(site_symbol.module)
            if family is not None and not _inside(site, family, self.c, self.other):
                continue  # but see the handing-on sites below
            if kind == SITE_CLOSURE or module_name:
                if namespace not in self.reach.closure_of(site):
                    continue
            elif kind == SITE_IMPORT:
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
                if kind_ in (FUNCTION, METHOD):
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
            ev = self.evidence
            outside = sorted(
                g
                for g in getters
                if g in ev.import_phase or g in ev.import_by or g in ev.hook_phase
            )
            if outside:
                # What it obtained outside every test may serve any later one.
                for site, kind in self.sites:
                    if kind in (SITE_ELSEWHERE, SITE_ANY) and site not in listed:
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
            for (site, _kind), callers in self.import_sites.items():
                if callers is not None and symbol.id not in callers:
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
                family = {current}
                for graph in (self.bases, self.subclasses):
                    stack = [current]
                    while stack:
                        for nxt in graph.get(stack.pop(), ()):
                            if nxt not in family:
                                family.add(nxt)
                                stack.append(nxt)
                return family
            symbol = self.symbols.get(current)
            current = symbol.container if symbol is not None else None
        return None

    def _file(self, path: str, what: str, *, names: bool) -> None:
        """A changed file the index does not read. Its content is observed by
        whoever opened or stat'ed it; that it exists (``names``: it was added
        or deleted) also by whoever listed a directory above it."""
        if build_input(path):
            self._select_all(
                RULE_UNOBSERVED_FILE,
                f"{path} {what}: compiled source, build, dependency or pytest configuration, "
                "read where no test's record sees it",
            )
            return
        observed = [(path, self.evidence.import_paths)]
        if names:
            # A name seen: a directory above it listed, or the path itself
            # stat'ed (a source file's stat is recorded as a name seen).
            observed += [(d, self.evidence.import_dirs) for d in _ancestors(path)]
        for seen, where_seen in observed:
            for module in sorted(self.evidence.seen_by(where_seen, seen)):
                where = seen or "the checkout root"
                if module == "" or module.startswith(UNINDEXED_MODULE):
                    self._select_all(
                        RULE_UNOBSERVED_FILE,
                        f"{path} {what}, and project code touched {where} outside every test "
                        "and outside any import it could be credited to (a hook or collection)",
                    )
                else:
                    self._escalate_module(
                        module,
                        f"{path} {what}, and {where} was touched while {module} was imported",
                    )
        self.files[path] = (what, names)


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
) -> Plan:
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

    fallbacks: list[Fallback] = []
    if errors:
        fallbacks.append(
            Fallback(
                RULE_ANALYSIS_ERROR,
                "all_targets",
                f"{len(errors)} analysis error(s); the dependency graph is incomplete, "
                "so every supplied target is selected",
            )
        )
    observed: list[_Observers] = []
    escalated: dict[str, list[Reason]] = defaultdict(list)
    target_fallbacks: dict[str, list[Reason]] = defaultdict(list)
    changed_ids: set[str] = {c.id for c in changes if c.carries_impact}
    docstring_ids: set[str] = {c.id for c in changes if DOCSTRING_CHANGED in c.changes}
    for side, other in pairs:
        runner_only = _runner_only(evidence_index, other, pytest_targets)
        obs = _Observers(evidence_index, other, evidence, test_code, declared, runner_only)
        if repo:
            obs.outside = _changed_outside_roots(Path(repo), evidence.commit, other, source_roots)
        obs.run()
        observed.append(obs)
        fallbacks += obs.fallbacks
        changed_ids |= {c.id for c in obs.changes if c.carries_impact}
        docstring_ids |= {c.id for c in obs.changes if DOCSTRING_CHANGED in c.changes}
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
                if r.rule == RULE_ANALYSIS_ERROR:
                    continue  # reported once, above
                if r.rule in (RULE_ESCALATED,) or r.path or r.changed_symbol:
                    escalated[decision.target.node_id].append(
                        Reason(
                            RULE_ESCALATED,
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
        )
        static_other = {d.target.node_id: d for d in full.decisions if d.target.runner != "pytest"}

    decisions = [
        _evidence_decision(
            target,
            evidence,
            observed,
            fallbacks,
            escalated,
            target_fallbacks,
            changed_ids,
            docstring_ids,
            base_target_ids,
            static_other,
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
    fallbacks: list[Fallback],
    escalated: dict[str, list[Reason]],
    target_fallbacks: dict[str, list[Reason]],
    changed_ids: set[str],
    docstring_ids: set[str],
    base_target_ids: set[str] | None,
    static_other: dict[str, Decision],
) -> Decision:
    if target.runner != "pytest":
        return static_other[target.node_id]
    reasons: list[Reason] = [Reason(fb.rule, fb.detail) for fb in _dedupe(fallbacks)]
    record = evidence.tests.get(target.runner_id)
    fixture_sources = any(obs.fixture_sources_changed for obs in observed)
    for r in dict.fromkeys(target_fallbacks.get(target.node_id, ())):
        if r.rule == RULE_LIFECYCLE_UNRESOLVED and record is not None and not fixture_sources:
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
                RULE_NO_EVIDENCE,
                f"{target.runner_id} has no record in the evidence from {short} (new, "
                "deselected or not collected then)",
            )
        )
    else:
        if record.flags & FLAG_UNSTABLE:
            reasons.append(
                Reason(
                    RULE_UNSTABLE,
                    "its record differed between two collections in different orders",
                )
            )
        if record.flags & FLAG_SUBPROCESS:
            reasons.append(
                Reason(RULE_SUBPROCESS, "it started a subprocess, whose execution is not recorded")
            )
        if record.flags & FLAG_TEXT:
            reasons.append(
                Reason(
                    RULE_TEXT_CODE,
                    "it ran code compiled from text no file holds (a doctest, timeit), "
                    "which may use any name",
                )
            )
        executed = evidence.executed(record)
        touched = evidence.touched(record)
        listed = evidence.listed(record)
        for obs in observed:
            hits = sorted(executed & obs.E.keys())
            if hits:
                why = obs.E[hits[0]]
                more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
                reasons.append(
                    Reason(
                        why.rule,
                        f"executed {hits[0]} in the evidence run at {short}{more}: {why.detail}",
                        (),
                        why.changed_symbol,
                        why.changes,
                    )
                )
            for reader in sorted(executed & obs.guarded.keys()):
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
                elif names:
                    dirs = [d for d in _ancestors(path) if evidence.has(listed, d)]
                    if dirs and dirs[0] == path:
                        how = f"stat'ed {path}"
                    elif dirs:
                        how = f"listed {dirs[0] or 'the checkout root'}"
                if how is not None:
                    reasons.append(
                        Reason(
                            RULE_TOUCHED_FILE,
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
                RULE_CHANGED_TARGET,
                f"{hit[0]} changed: the target's entry or what contains it (a skipped test's "
                "record lacks its own entry)",
                (),
                hit[0],
            )
        )
    if base_target_ids is not None and target.runner_id not in base_target_ids:
        reasons.append(
            Reason(
                RULE_NEW_TARGET,
                f"{target.runner_id} is not in the base snapshot: a new target is selected "
                "whatever its entry symbol did",
            )
        )
    if target.entry_symbol in docstring_ids:
        reasons.append(
            Reason(
                RULE_ENTRY_DOCSTRING,
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
        "python": evidence.environment.get("python", "").split()[0],
        "hash_seed": evidence.environment.get("variables", {}).get("PYTHONHASHSEED"),
        "variables": sorted(evidence.environment.get("variables", {})),
        "created": evidence.created,
        "tests": len(evidence.tests),
        "reverse_checked": evidence.reverse_checked,
        "planned": [f"{evidence.commit[:12]} -> {side}" for side, _ in pairs],
        "changes_outside_range": outside,
        "escalated_modules": sorted({m for obs in observed for m in obs.escalated_modules}),
    }
