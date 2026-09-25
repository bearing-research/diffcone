"""Evidence-mode planning: select the tests whose recorded run meets a change.

Evidence recorded at commit C says which symbols each test executed and
which repository paths it touched (evidence.py). A test that runs
identically at C and at a snapshot cannot differ there, so a plan base ->
head is the union of two plans from C: C -> base and C -> head (one when C
is the base). For each, every change is turned into E, the symbols whose
execution would *notice* it (docs/evidence_design.md, "What a change is
observed by"), and a test is selected when its record meets E:

* a function body: the function itself; if it ran during an import, the
  importing module's import-time state may differ, so that module is
  escalated (below). Code that ran outside every test and every import
  (hooks, collection) is escalated itself;
* a function's signature, defaults, decorators or annotations: also its
  readers (resolved references and name matches, one hop), the lookup and
  reflection sites that can see its namespace, and escalation when the
  ``def`` runs code at import;
* a variable: its readers; a reader that is itself a variable captured the
  value and is followed in turn; a module's or class's top-level reader
  escalates its module;
* a class body: the attributes whose statements changed, through every
  reader of those names and the lookup and reflection sites; an opaque
  body statement, a dunder attribute or a changed class statement (bases,
  decorators, keywords) escalates. A special method (``__eq__``) is used
  without being named, so a change to one reaches every member of the
  class's hierarchy;
* an added or deleted name: its readers, the unbounded lookup and
  reflection sites that can see the namespace, and for a deletion the
  modules importing it (their import now fails);
* module-level code: escalates the module;
* a non-Python file: the tests that touched it or a directory above it;
  everything when it is compiled source, build or pytest configuration, or
  was touched outside every test;
* test code other than a function body: every test in its scope as well
  (the module's tests, or every test under a conftest), since pytest
  reads marks, fixtures and parameters without a static reader.

"Escalated" means planned by the static planner from exactly those seeds,
without dynamic-reference pseudo-seeds: a test that executed a dynamic
site is in the record through the code it reached, so the lookup sites
that can see an escalated module join E instead. Every symbol of an
escalated module joins E too.

Targets of other runners, which have no evidence, keep their static
decision. A pytest target with no record (new, or never run) is selected.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

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
from diffcone.declarations import Declaration
from diffcone.discovery import DiscoveryResult
from diffcone.evidence import FLAG_SUBPROCESS, FLAG_UNSTABLE, UNINDEXED_MODULE, Evidence
from diffcone.manifest import Manifest, Target
from diffcone.model import (
    CLASS,
    CLASS_STATEMENT,
    DECLARED,
    FUNCTION,
    IMPORTS,
    IMPORTS_NAME,
    METHOD,
    MODULE,
    OPAQUE_ATTRIBUTE,
    REFERENCES,
    UNRESOLVED_DYNAMIC,
    VARIABLE,
    SourceIndex,
    Symbol,
)
from diffcone.planner import (
    RULE_ANALYSIS_ERROR,
    RULE_CHANGED_TARGET,
    RULE_ENTRY_DOCSTRING,
    RULE_ESCALATED,
    RULE_EXECUTED_CHANGED,
    RULE_EXECUTED_READER,
    RULE_LOOKUP_SITE,
    RULE_NEW_TARGET,
    RULE_NO_EVIDENCE,
    RULE_PYTEST_HOOK,
    RULE_SUBPROCESS,
    RULE_TEST_SCOPE,
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
    _is_dunder,
    _members_by_container,
    _runner_dependency_fallbacks,
    merge_targets,
    plan_from_indexes,
)

# Sources no Python-level event reports: compiled into extensions, or read
# by pytest or the build before the recorder starts.
COMPILED_SUFFIXES = (
    ".pyx",
    ".pxd",
    ".pxi",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hh",
    ".hpp",
    ".f",
    ".f90",
    ".rs",
    ".cu",
    ".in",
    ".tpl",
    ".so",
    ".pyd",
    ".dylib",
    ".dll",
)
CONFIG_FILES = frozenset(
    {
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        ".pytest.ini",
        "MANIFEST.in",
        "meson.build",
        "meson.options",
        "meson_options.txt",
        "CMakeLists.txt",
        "Makefile",
        "Pipfile",
        "pixi.toml",
        ".python-version",
        ".coveragerc",
    }
)
CONFIG_PREFIXES = ("requirements", "environment", "constraints")
# Module-level names in a conftest that pytest reads to decide what to load
# or collect at all.
PYTEST_COLLECTION_NAMES = frozenset({"pytest_plugins", "collect_ignore", "collect_ignore_glob"})

# UNRESOLVED_DYNAMIC details, by what the site can see.
SITE_CLOSURE = "closure"  # a name looked up on a module global: its import closure
SITE_ELSEWHERE = "elsewhere"  # on an object from anywhere
SITE_IMPORT = "import"  # a module named at run time
SITE_ANY = "any"  # eval/exec/run_path


def _site_kind(detail: str) -> str:
    if "import" in detail or detail.startswith("runpy.run_module"):
        return SITE_IMPORT
    if detail.startswith(("getattr(<non-literal>) on a receiver from elsewhere", "vars(")):
        return SITE_ELSEWHERE
    if detail.startswith(("getattr(<non-literal>)", "globals(")):
        return SITE_CLOSURE
    return SITE_ANY  # eval, exec, runpy.run_path, anything not recognised


def _unobserved_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.endswith(COMPILED_SUFFIXES)
        or name in CONFIG_FILES
        or name.endswith(".lock")
        or (name.startswith(CONFIG_PREFIXES) and name.endswith((".txt", ".yml", ".yaml")))
    )


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
        for target in targets:
            file = target.runner_id.split("::", 1)[0]
            self.tests_by_file[file].append(target.runner_id)
            self.entries.add(target.entry_symbol)
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

    def scope(self, module: str) -> list[str]:
        """The tests a change in ``module`` reaches without a static reader:
        the module's own tests, or every test under a conftest's directory."""
        path = self.path_of_module.get(module, "")
        if module in self.conftests:
            prefix = path[: -len("conftest.py")]
            return sorted(
                t for f, tests in self.tests_by_file.items() if f.startswith(prefix) for t in tests
            )
        return sorted(self.tests_by_file.get(path, ()))


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
    ) -> None:
        self.c, self.other = c_index, other
        self.evidence = evidence
        self.test_code = test_code
        self.symbols: dict[str, Symbol] = {**c_index.symbols, **other.symbols}
        self.changes = classify(c_index, other)
        self.readers_of: dict[str, set[str]] = defaultdict(set)
        self.attribute_readers: dict[str, set[str]] = defaultdict(set)  # "attribute:n" edges
        self.importers_of: dict[str, set[str]] = defaultdict(set)
        self.by_name: dict[str, set[str]] = defaultdict(set)
        self.sites: list[tuple[str, str]] = []  # (symbol, kind)
        for index in (c_index, other):
            for edge in index.edges:
                if edge.kind in (REFERENCES, DECLARED):
                    self.readers_of[edge.target].add(edge.source)
                    if edge.detail.startswith("attribute:"):
                        self.attribute_readers[edge.detail[len("attribute:") :]].add(edge.source)
                elif edge.kind in (IMPORTS, IMPORTS_NAME):
                    self.importers_of[edge.target].add(edge.source)
            for ref in index.unresolved:
                if ref.kind == UNRESOLVED_DYNAMIC:
                    self.sites.append((ref.symbol, _site_kind(ref.detail)))
                elif ref.name:
                    self.by_name[ref.name].add(ref.symbol)
            for symbol, _detail in index.reflection:
                self.sites.append((symbol, SITE_ANY))
        for decl in declarations:
            self.readers_of[decl.target].add(decl.source)
        self.sites = sorted(set(self.sites))
        self.members = _members_by_container(set(self.symbols))
        self.reach = _ImportReach(c_index, other)
        self.subclasses: dict[str, set[str]] = defaultdict(set)
        self.bases: dict[str, set[str]] = defaultdict(set)
        for index in (c_index, other):
            for edge in index.edges:
                source = self.symbols.get(edge.source)
                target = self.symbols.get(edge.target)
                if (
                    edge.kind == REFERENCES
                    and not edge.detail
                    and source is not None
                    and target is not None
                    and source.kind == CLASS
                    and target.kind == CLASS
                ):
                    # Over-approximates bases (any class the class statement
                    # names), which only widens E.
                    self.bases[edge.source].add(edge.target)
                    self.subclasses[edge.target].add(edge.source)

        self.E: dict[str, Reason] = {}
        self.direct: dict[str, Reason] = {}
        self.fallbacks: list[Fallback] = []
        self.files: dict[str, tuple[str, bool]] = {}  # changed path -> (what, names too)
        self.seed_changes: set[str] = set()
        self.seed_nodes: dict[str, str] = {}
        self.escalated_modules: set[str] = set()
        self._followed: set[str] = set()

    # -- recording --------------------------------------------------------------

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
        for test in self.test_code.scope(symbol.module):
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
        for change in self.changes:
            if change.carries_impact:
                self._change(change)
        for path in _changed_unanalysed_files(self.c, self.other):
            before, after = path in self.c.other_files, path in self.other.other_files
            what = "edited" if before and after else ("added" if after else "deleted")
            self._file(path, what, names=not (before and after))
        for change in self.changes:
            symbol = change.head or change.base
            if symbol.kind == MODULE and {ADDED, DELETED} & set(change.changes):  # type: ignore[union-attr]
                what = f"{change.id} {'/'.join(change.changes)}"
                self._file(symbol.path, what, names=True)  # type: ignore[union-attr]

    def _change(self, change: SymbolChange) -> None:
        symbol = change.head or change.base
        assert symbol is not None
        kinds = set(change.changes)
        test = self.test_code.is_test_code(symbol.module)
        label = f"{change.id} {'/'.join(change.changes)}"
        if symbol.kind in (FUNCTION, METHOD) and symbol.name.startswith("pytest_"):
            self._select_all(RULE_PYTEST_HOOK, f"{label}: a pytest hook can change any test")
            return
        if (
            symbol.kind == VARIABLE
            and symbol.module in self.test_code.conftests
            and symbol.container == symbol.module
            and symbol.name in PYTEST_COLLECTION_NAMES
        ):
            self._select_all(RULE_PYTEST_HOOK, f"{label}: it decides what pytest loads")
            return
        body_only = symbol.kind in (FUNCTION, METHOD) and kinds <= {
            BODY_CHANGED,
            DEPENDENCIES_CHANGED,
            DOCSTRING_CHANGED,
        }
        if test and not body_only:
            self._scope(symbol, change)
        if symbol.kind == MODULE:
            if kinds & {ADDED, DELETED}:
                self._observe(change.id, RULE_EXECUTED_CHANGED, label, change)
                self._readers(change.id, change, label)
                if DELETED in kinds:
                    self._importers(change.id, change, label)
                self._sites(symbol, change, label, imports=True)
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
            elif kinds & {DEFINITION_CHANGED, ANNOTATIONS_CHANGED}:
                self._readers(change.id, change, label)
                self._sites(symbol, change, label)
                runs_code = not all(
                    s.inert_definition for s in (change.base, change.head) if s is not None
                )
                if runs_code and change.id not in self.test_code.entries:
                    self._escalate_change(change, f"{label}: its definition runs code at import")
            if _is_dunder(symbol.name) and not body_only and symbol.container:
                container = self.symbols.get(symbol.container)
                if container is not None and container.kind == CLASS:
                    self._hierarchy(container.id, change, label)
            return
        if symbol.kind == VARIABLE:
            self._observe(change.id, RULE_EXECUTED_CHANGED, label, change)
            self._readers(change.id, change, label)
            if kinds & {ADDED, DELETED}:
                self._sites(symbol, change, label)
                if DELETED in kinds:
                    self._importers(change.id, change, label)
            return
        if symbol.kind == CLASS:
            self._observe(change.id, RULE_EXECUTED_CHANGED, label, change)
            if kinds & {ADDED, DELETED}:
                self._readers(change.id, change, label)
                self._sites(symbol, change, label)
                if DELETED in kinds:
                    self._importers(change.id, change, label)
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
        for name in sorted(names):
            readers = self.attribute_readers.get(name, set()) | self.by_name.get(name, set())
            for reader in sorted(readers):
                self._reader(reader, change, f"{label} (attribute {name})")
        self._sites(symbol, change, label)

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

    def _readers(self, target: str, change: SymbolChange, label: str) -> None:
        readers = set(self.readers_of.get(target, ()))
        name = target.rsplit(".", 1)[-1]
        if not _is_dunder(name):
            readers |= self.by_name.get(name, set())
        readers.discard(target)
        for reader in sorted(readers):
            self._reader(reader, change, label if target == change.id else f"{label} via {target}")

    def _reader(self, reader: str, change: SymbolChange, label: str) -> None:
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

    def _import_effect(self, symbol_id: str, change: SymbolChange, label: str) -> None:
        """Code that ran at C while a module was imported built that module's
        import-time state; code that ran outside every test and import ran in
        a hook or during collection."""
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
            if symbol_id == change.id:
                self._escalate_change(change, reason)
            elif symbol_id not in self.seed_nodes:
                self.seed_nodes[symbol_id] = reason

    def _escalate_change(self, change: SymbolChange, why: str) -> None:
        self.seed_changes.add(change.id)
        symbol = change.head or change.base
        assert symbol is not None
        self._escalate_module(symbol.module, why, seed=False)

    def _escalate_module(self, module: str, why: str, *, seed: bool = True) -> None:
        if seed and module not in self.seed_nodes:
            self.seed_nodes[module] = why
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
        module_symbol = self.symbols.get(module)
        if module_symbol is not None:
            self._sites(module_symbol, None, why, imports=True)

    def _sites(
        self, symbol: Symbol, change: SymbolChange | None, label: str, *, imports: bool = False
    ) -> None:
        """Code that finds names by a name nothing bounds and can see the
        namespace of ``symbol``: a read off an object from anywhere, eval and
        reflection always; a read off a module global when the namespace's
        module is in the site's import closure; a module named at run time
        for a module-level namespace, when ``imports``. For test code only
        sites in test code count: nothing else holds a test module or a test
        class the runner instantiates."""
        namespace = symbol.module
        test = self.test_code.is_test_code(namespace)
        for site, kind in self.sites:
            site_symbol = self.symbols.get(site)
            if site_symbol is None:
                continue
            if test and not self.test_code.is_test_code(site_symbol.module):
                continue
            if kind == SITE_CLOSURE:
                if namespace not in self.reach.closure_of(site):
                    continue
            elif kind == SITE_IMPORT:
                if not imports or test:
                    continue
            self._observe(
                site,
                RULE_LOOKUP_SITE,
                f"{site} looks names up by a name nothing bounds and can see {namespace}; {label}",
                change,
            )

    def _file(self, path: str, what: str, *, names: bool) -> None:
        """A changed file the index does not read. Its content is observed by
        whoever opened or stat'ed it; that it exists (``names``: it was added
        or deleted) also by whoever listed a directory above it."""
        if _unobserved_file(path):
            self._select_all(
                RULE_UNOBSERVED_FILE,
                f"{path} {what}: compiled source, build or pytest configuration, read where "
                "no test's record sees it",
            )
            return
        observed = [(path, self.evidence.import_paths)]
        if names:
            observed += [(d, self.evidence.import_dirs) for d in _ancestors(path)[1:]]
        for seen, where_seen in observed:
            for module in sorted(where_seen.get(seen, ())):
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
        obs = _Observers(evidence_index, other, evidence, test_code, declared)
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
    reasons += list(dict.fromkeys(target_fallbacks.get(target.node_id, ())))
    record = evidence.tests.get(target.runner_id)
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
            for path, (what, names) in sorted(obs.files.items()):
                how = None
                if path in touched:
                    how = f"opened or stat'ed {path}"
                elif names:
                    dirs = [d for d in _ancestors(path)[1:] if d in listed]
                    if dirs:
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
        "python": evidence.environment.get("python", "").split()[0],
        "hash_seed": evidence.environment.get("variables", {}).get("PYTHONHASHSEED"),
        "created": evidence.created,
        "tests": len(evidence.tests),
        "reverse_checked": evidence.reverse_checked,
        "planned": [f"{evidence.commit[:12]} -> {side}" for side, _ in pairs],
        "changes_outside_range": outside,
        "escalated_modules": sorted({m for obs in observed for m in obs.escalated_modules}),
    }
