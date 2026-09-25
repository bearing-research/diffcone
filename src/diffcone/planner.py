"""Impact planner.

Operates purely on the explicit data structures produced by the snapshot
reader, indexer and classifier. It knows nothing about pytest or ASV: targets
are opaque (runner, runner_id, entry symbol, lifecycle dependencies) records
supplied by the manifest.

Propagation rules (a dependency edge ``X -> Y`` carries impact from Y to X):

* ``references``/``entry``/``lifecycle``/``unresolved_name_match``: any impact
  on Y affects X (behaviour-level).
* ``defined_in``: X is affected only when its container Y is *structurally*
  affected (added, deleted, definition or dependencies changed, a class body
  change, or itself structurally invalidated by its own container). A plain
  body change of a module does not invalidate every member; members that use
  module state carry their own ``references`` edges.
* ``imports``: importing a module runs its import-time code, so any impact
  on the imported module reaches the importer (behaviour-level); deleting
  it invalidates the importer and every member of it (structural).
* ``imports_name``: only a deletion of the imported name propagates
  (structural); the accompanying ``imports`` edge to the module carries
  import-time impact.

A change that runs at import (a module body change, a variable, a class, a
function's decorators or defaults, an added or deleted definition) also
seeds its module, so every module that transitively imports it is reached.

Every selection is backed by a concrete edge path or an explicit fallback
rule. See docs/design.md.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TypeVar

from diffcone.cache import IndexCache
from diffcone.classify import (
    ADDED,
    DEFINITION_CHANGED,
    DELETED,
    DOCSTRING_CHANGED,
    SymbolChange,
    classify,
)
from diffcone.declarations import FILENAME as DECLARATION_FILE
from diffcone.declarations import Declaration
from diffcone.declarations import load as load_declarations
from diffcone.discovery import RUNNER_MODULES, DiscoveryOptions, DiscoveryResult, discover
from diffcone.evidence import Evidence, EvidenceError
from diffcone.indexer import build_index
from diffcone.manifest import Manifest, Target
from diffcone.model import (
    CLASS,
    DECLARED,
    DEFINED_IN,
    ENTRY,
    IMPORTS,
    IMPORTS_NAME,
    LIFECYCLE,
    METHOD,
    MODULE,
    REFERENCES,
    UNRESOLVED_DYNAMIC,
    UNRESOLVED_NAME_MATCH,
    VARIABLE,
    AnalysisError,
    Edge,
    SnapshotInfo,
    SourceIndex,
    UnresolvedReference,
)
from diffcone.snapshot import INDEX, WORKTREE, GitError, Snapshot, read_snapshot, resolve_commit

BEHAVIOR = 1
STRUCTURAL = 2

RULE_DEPENDENCY = "dependency"
RULE_UNRESOLVED_NAME_MATCH = "unresolved_name_match"
RULE_DYNAMIC_REFERENCE = "dynamic_reference"
RULE_ENTRY_UNRESOLVED = "entry_symbol_unresolved"
RULE_LIFECYCLE_UNRESOLVED = "lifecycle_dependency_unresolved"
RULE_ANALYSIS_ERROR = "analysis_error"
RULE_RUNNER_DEPENDENCY = "runner_dependency"
RULE_ENTRY_DOCSTRING = "entry_docstring_changed"
RULE_DECLARED_DEPENDENCY = "declared_dependency"
RULE_NEW_TARGET = "new_target"
RULE_UNANALYSED_FILE = "unanalysed_file_changed"
# Evidence mode (evidence_plan.py). ``escalated`` is a selection made by
# static planning of a change that evidence cannot bound.
RULE_ESCALATED = "escalated"
RULE_EXECUTED_CHANGED = "executed_changed"
RULE_EXECUTED_READER = "executed_reader"
RULE_LOOKUP_SITE = "lookup_site"
RULE_TOUCHED_FILE = "touched_file"
RULE_TEST_SCOPE = "test_scope"
RULE_CHANGED_TARGET = "changed_target"
RULE_NO_EVIDENCE = "no_evidence"
RULE_UNSTABLE = "unstable"
RULE_SUBPROCESS = "subprocess"
RULE_PYTEST_HOOK = "pytest_hook_changed"
RULE_UNOBSERVED_FILE = "unobserved_file_changed"
RULE_UNINDEXED_IMPORT = "unindexed_import"
# A declared endpoint that names a container stands for everything in it;
# beyond this many pairs the declaration is too coarse to be useful.
DECLARATION_FANOUT = 5000

# A lifecycle dependency ``dynamic:<module>`` says the target runs code with
# that module's globals (a doctest): it is affected by any impact-carrying
# change in the module's import closure, as a dynamic reference is.
DYNAMIC_DEPENDENCY = "dynamic:"

CONSERVATIVE_RULES = frozenset(
    {
        RULE_UNRESOLVED_NAME_MATCH,
        RULE_DYNAMIC_REFERENCE,
        RULE_ENTRY_UNRESOLVED,
        RULE_LIFECYCLE_UNRESOLVED,
        RULE_ANALYSIS_ERROR,
        RULE_RUNNER_DEPENDENCY,
        RULE_UNANALYSED_FILE,
        RULE_LOOKUP_SITE,
        RULE_NO_EVIDENCE,
        RULE_UNSTABLE,
        RULE_SUBPROCESS,
        RULE_PYTEST_HOOK,
        RULE_UNOBSERVED_FILE,
        RULE_UNINDEXED_IMPORT,
    }
)

# Files under the source roots that are diffcone's own, not the project's:
# the cache, and the declarations the planner already reads from both
# revisions.
OWN_FILES = ("diffcone.toml",)
OWN_DIRS = (".diffcone/",)
UNANALYSED_PATHS_SHOWN = 5


def _changed_unanalysed_files(base: SourceIndex, head: SourceIndex) -> list[str]:
    """Non-Python files under the source roots whose content differs between
    the snapshots (added, deleted or edited). The index reads none of them,
    so it cannot say who depends on one: a data file the code opens, a
    compiled extension's source, a configuration file."""
    paths = base.other_files.keys() | head.other_files.keys()
    return sorted(
        p
        for p in paths
        if base.other_files.get(p) != head.other_files.get(p)
        and p not in OWN_FILES
        and not p.startswith(OWN_DIRS)
    )


@dataclass(frozen=True)
class Seeds:
    """Where a restricted search starts (evidence mode's escalation): the
    changes to plan from, plus other nodes with the reason each one is a
    starting point (a module whose import ran changed code). A restricted
    search adds no dynamic-reference pseudo-seeds, no unanalysed-file
    fallback and no target-level rules (new target, entry docstring,
    ``dynamic:`` dependencies): the caller decides those."""

    changes: frozenset[str] = frozenset()
    nodes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    source: str
    target: str
    kind: str
    detail: str
    revisions: tuple[str, ...]


@dataclass(frozen=True)
class Reason:
    rule: str
    detail: str
    path: tuple[Step, ...] = ()
    changed_symbol: str | None = None
    changes: tuple[str, ...] = ()

    @property
    def conservative(self) -> bool:
        return self.rule in CONSERVATIVE_RULES


@dataclass
class Decision:
    target: Target
    selected: bool
    reasons: list[Reason]
    affected_dependencies: list[str]
    unselected_reason: str | None = None


@dataclass(frozen=True)
class Fallback:
    rule: str
    scope: str  # "all_targets" | "target"
    detail: str
    target: str | None = None


@dataclass(frozen=True)
class UnresolvedRecord:
    symbol: str
    kind: str
    name: str
    detail: str
    revisions: tuple[str, ...]
    matched_affected_symbols: tuple[str, ...]


@dataclass
class Plan:
    repo: str
    source_roots: list[str]
    changes: list[SymbolChange]
    decisions: list[Decision]
    fallbacks: list[Fallback]
    unresolved: list[UnresolvedRecord]
    errors: list[AnalysisError]
    base_index: SourceIndex = field(repr=False, default=None)  # type: ignore[assignment]
    head_index: SourceIndex = field(repr=False, default=None)  # type: ignore[assignment]
    discovery: list[DiscoveryResult] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    declarations: list[Declaration] = field(default_factory=list)
    # Evidence mode: which store planned the pytest targets, and from where
    # (evidence_plan._summary); None for a static plan.
    evidence: dict | None = None

    @property
    def selected(self) -> list[Decision]:
        return [d for d in self.decisions if d.selected]

    @property
    def unselected(self) -> list[Decision]:
        return [d for d in self.decisions if not d.selected]

    @property
    def degraded(self) -> bool:
        return bool(self.errors)

    @property
    def incomplete_discovery(self) -> list:
        """Discovery notes saying a runner may collect tests that are not
        targets. A degraded plan runs too much; this runs too little, so the
        two are reported (and exited) separately."""
        return [n for d in self.discovery for n in d.incomplete]

    @property
    def base(self) -> SnapshotInfo:
        return self.base_index.snapshot

    @property
    def head(self) -> SnapshotInfo:
        return self.head_index.snapshot

    @property
    def uncommitted_analyzed(self) -> bool:
        """True when either snapshot is the index or the working tree."""
        return not (self.base.committed and self.head.committed)

    @property
    def working_tree_analyzed(self) -> bool:
        return self.base.is_worktree or self.head.is_worktree

    @property
    def scope_statement(self) -> str:
        """One sentence saying exactly what was analysed."""
        if not self.uncommitted_analyzed:
            return "two committed snapshots; the working tree was not analyzed"
        sides = [
            name for name, info in (("base", self.base), ("head", self.head)) if not info.committed
        ]
        return f"UNCOMMITTED state was analyzed as {' and '.join(sides)}; see base/head"


# --------------------------------------------------------------------------- graph


@dataclass
class _Graph:
    """Union of both revisions' edges, indexed for backward traversal."""

    reverse: dict[str, list[tuple[str, Edge, tuple[str, ...]]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    forward: dict[str, list[tuple[str, Edge, tuple[str, ...]]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def add(self, edge: Edge, revisions: tuple[str, ...]) -> None:
        self.reverse[edge.target].append((edge.source, edge, revisions))
        self.forward[edge.source].append((edge.target, edge, revisions))

    def freeze(self) -> None:
        # A plain tuple key: dataclass ordering on Edge is far slower.
        for adj in (self.reverse, self.forward):
            for key in adj:
                adj[key].sort(
                    key=lambda item: (item[0], item[1].kind, item[1].detail, item[1].target)
                )


T = TypeVar("T")


def _union(base_items: Iterable[T], head_items: Iterable[T]) -> dict[T, tuple[str, ...]]:
    """Merge two revisions' items, tagging each with the revisions it appears in.

    Insertion order is not significant: adjacency lists are sorted in
    ``_Graph.freeze`` and records are sorted before reporting, so the items
    are not sorted here (comparing tens of thousands of dataclasses was a
    third of planning time)."""
    revs: dict[T, list[str]] = defaultdict(list)
    for item in base_items:
        revs[item].append("base")
    for item in head_items:
        revs[item].append("head")
    return {item: tuple(r) for item, r in revs.items()}


def _propagate(edge: Edge, target_mode: int, target_change: SymbolChange | None) -> int | None:
    if edge.kind == DEFINED_IN:
        return STRUCTURAL if target_mode == STRUCTURAL else None
    if edge.kind in (IMPORTS, IMPORTS_NAME):
        if target_change is not None and DELETED in target_change.changes:
            return STRUCTURAL
        # Importing a module runs its import-time code.
        return BEHAVIOR if edge.kind == IMPORTS else None
    return BEHAVIOR


def _runs_at_import(change: SymbolChange) -> bool:
    """Module bodies, variable initialisers and class bodies run at import;
    so does a ``def`` statement's decorators, defaults and (when evaluated
    eagerly) annotations. A function body does not (what import-time code
    calls is reached through its edges), and an inert ``def`` at both
    revisions only binds a name (removing or rebinding it reaches its users
    through deletion and resolution edges)."""
    symbol = change.head or change.base
    if symbol is None:
        return False
    if symbol.kind in (MODULE, CLASS):
        return True
    if symbol.kind == VARIABLE:
        # Binding a literal runs nothing; a computed value does.
        return not all(s.inert_definition for s in (change.base, change.head) if s is not None)
    if not {ADDED, DELETED, DEFINITION_CHANGED} & set(change.changes):
        return False
    return not all(s.inert_definition for s in (change.base, change.head) if s is not None)


# --------------------------------------------------------------------------- planning


def merge_targets(manifest: Manifest | None, discovered: list[DiscoveryResult]) -> list[Target]:
    """Manifest targets win over discovered ones with the same runner and id."""
    merged: dict[tuple[str, str], Target] = {}
    for result in discovered:
        for target in result.targets:
            merged.setdefault((target.runner, target.runner_id), target)
    if manifest is not None:
        for target in manifest.targets:
            merged[(target.runner, target.runner_id)] = target
    return sorted(merged.values())


def _members_by_container(symbols: set[str]) -> dict[str, list[str]]:
    """Symbols inside each module or class, by qualified name. Identity is the
    dotted path, so a container's members are the symbols under its prefix."""
    members: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols:
        prefix = symbol
        while "." in prefix:
            prefix = prefix.rsplit(".", 1)[0]
            if prefix in symbols:
                members[prefix].append(symbol)
    return members


def _runner_only_classes(
    base: SourceIndex,
    head: SourceIndex,
    discovered: list[DiscoveryResult],
    edges: dict[Edge, tuple[str, ...]],
) -> set[str]:
    """Test classes whose instances only the test runner ever holds.

    A call ``obj.m()`` reaches ``C.m`` only if ``obj`` is an instance of C or
    of a subclass. pytest instantiates a test class to run its tests; if the
    analysed code never constructs it, never refers to it as a value and
    never hands an instance on, those are the only instances, and they never
    leave the class's own methods. Nothing outside it can be holding one, so
    no name-matched call from outside can land on its members. (Calls on
    ``self`` inside it are resolved through the MRO and are not name matches.)

    pandas is why this matters: its library code says ``x.dtype``,
    ``x.index``, ``x.copy`` thousands of times, and every one of those matched
    a fixture or helper of the same name on some test class -- one of which
    held a dynamic reference that every test reached through ``DataFrame``.

    A class counts as runner-instantiated when it owns the entry of a pytest
    target, or is the collecting class a target lists among its lifecycle
    dependencies. It is *held* -- and keeps its members as candidates -- when
    an instance or the class is passed on (``escaped_classes``), when a
    symbol outside every runner class refers to it, or when a held class
    inherits from it. A read of an attribute named ``instance`` anywhere is
    pytest's ``request.instance``, the one channel that hands a test instance
    to other code, and turns the rule off."""
    symbols = {**base.symbols, **head.symbols}
    runner: set[str] = set()
    for result in discovered:
        if result.runner != "pytest":
            continue
        for target in result.targets:
            entry = symbols.get(target.entry_symbol)
            if entry is not None and entry.kind == METHOD and entry.container:
                runner.add(entry.container)
            for dep in target.lifecycle_dependencies:
                owner = symbols.get(dep)
                if owner is not None and owner.kind == CLASS:
                    runner.add(dep)
    if not runner:
        return set()
    for index in (base, head):
        if any(u.kind != UNRESOLVED_DYNAMIC and u.name == "instance" for u in index.unresolved):
            return set()

    def within_runner(symbol_id: str) -> bool:
        return _inside(symbol_id, runner, base, head)

    held = (base.escaped_classes | head.escaped_classes) & runner
    for edge in edges:
        if edge.kind == REFERENCES and edge.target in runner and not within_runner(edge.source):
            held.add(edge.target)
    # A held subclass holds its bases too: its instances carry their methods.
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if (
                edge.kind == REFERENCES
                and edge.source in held
                and edge.target in runner
                and edge.target not in held
            ):
                held.add(edge.target)
                changed = True
    return runner - held


def _inside(symbol_id: str, classes: set[str], base: SourceIndex, head: SourceIndex) -> bool:
    """Whether ``symbol_id`` is one of ``classes`` or sits inside one."""
    current: str | None = symbol_id
    while current:
        if current in classes:
            return True
        symbol = head.symbols.get(current) or base.symbols.get(current)
        current = symbol.container if symbol is not None else None
    return False


def plan_from_indexes(
    base: SourceIndex,
    head: SourceIndex,
    manifest: Manifest | None,
    *,
    repo: str = "",
    source_roots: list[str] | None = None,
    discovered: list[DiscoveryResult] | None = None,
    declarations: list[Declaration] | None = None,
    base_target_ids: set[str] | None = None,
    seeds: Seeds | None = None,
) -> Plan:
    discovered = list(discovered or [])
    discovered_ids = {t.runner_id for result in discovered for t in result.targets}
    declared = list(declarations or [])
    targets = merge_targets(manifest, discovered)
    changes = classify(base, head)
    change_by_id = {c.id: c for c in changes}
    known_symbols = set(base.symbols) | set(head.symbols)
    fallbacks: list[Fallback] = []
    errors = sorted(base.errors + head.errors)

    graph = _Graph()
    union = _union(base.edges, head.edges)
    for edge, revs in union.items():
        graph.add(edge, revs)

    # An instance handed to someone else can have any attribute read off it by
    # a name nothing resolves (``invoke(obj, name)``), and no static rule can
    # say which. So referring to such a class depends on its members, not only
    # on its structure.
    class_members = _members_by_container(set(base.symbols) | set(head.symbols))
    for cls in sorted(base.escaped_classes | head.escaped_classes):
        for member in class_members.get(cls, ()):
            graph.add(
                Edge(cls, member, REFERENCES, "attribute of a class passed to other code"),
                ("base", "head"),
            )

    # Dependencies the project declares (diffcone.toml): the analysis cannot
    # see them, and they only add edges, so they widen selection and never
    # narrow it. An endpoint that is in neither revision is an analysis
    # error: a declaration that silently does nothing is worth failing on.
    # An endpoint that names a module or a class means everything in it, so
    # it expands to that container's members -- a change to one of them is
    # what the declaration is about, and the container node alone would
    # never see it.
    members = _members_by_container(known_symbols)
    for decl in declared:
        missing = [e for e in (decl.source, decl.target) if e not in known_symbols]
        if missing:
            errors.append(
                AnalysisError(
                    revision=head.snapshot.revision,
                    path=DECLARATION_FILE,
                    message=(
                        f"declared edge {decl.source!r} -> {decl.target!r} names "
                        f"{' and '.join(repr(m) for m in missing)}, which is in neither revision"
                    ),
                )
            )
            continue
        from_side = [decl.source, *members.get(decl.source, ())]
        to_side = [decl.target, *members.get(decl.target, ())]
        if len(from_side) * len(to_side) > DECLARATION_FANOUT:
            errors.append(
                AnalysisError(
                    revision=head.snapshot.revision,
                    path=DECLARATION_FILE,
                    message=(
                        f"declared edge {decl.source!r} -> {decl.target!r} joins "
                        f"{len(from_side)} and {len(to_side)} symbols, more than "
                        f"{DECLARATION_FANOUT} pairs; declare the symbols that depend "
                        "on each other instead"
                    ),
                )
            )
            continue
        for source in from_side:
            for target in to_side:
                if source != target:
                    graph.add(Edge(source, target, DECLARED, decl.detail), ("declared",))

    # Conservative edges from unresolved references: ``obj.run()`` may be any
    # known ``run`` (function, method or class) in either revision, and
    # impact flows through the graph as usual (matching only *changed*
    # symbols would miss a ``run`` that is unchanged but calls something that
    # changed). Each name gets one pseudo-node ``name:<n>`` so the edge count
    # is linear in references plus symbols. Dunder names (``__init__``,
    # ``__eq__``) are excluded: they exist on nearly every class and bound
    # nothing; constructors are reached through explicit class references.
    # Dynamic references are pseudo-seeds.
    symbols_by_name: dict[str, list[str]] = defaultdict(list)
    runner_only = _runner_only_classes(base, head, discovered, union)
    for symbol_id in sorted(known_symbols):
        symbol = head.symbols.get(symbol_id) or base.symbols[symbol_id]
        if symbol.kind != MODULE and not _is_dunder(symbol.name):
            if _inside(symbol_id, runner_only, base, head):
                continue  # only the test runner can hold an instance: see below
            symbols_by_name[symbol.name].append(symbol_id)
    for name, symbols in symbols_by_name.items():
        for symbol_id in symbols:
            graph.add(Edge(_name_node(name), symbol_id, UNRESOLVED_NAME_MATCH), ("both",))
    pending_unresolved: list[tuple[UnresolvedReference, tuple[str, ...]]] = []
    dynamic_symbols: dict[str, tuple[str, ...]] = {}
    unbounded_dynamic: set[str] = set()  # dynamic *imports*: reach anything
    for ref, revs in _union(base.unresolved, head.unresolved).items():
        if ref.kind == UNRESOLVED_DYNAMIC:
            dynamic_symbols.setdefault(ref.symbol, revs)
            if "import" in ref.detail:
                unbounded_dynamic.add(ref.symbol)
        elif ref.name in symbols_by_name and not _is_dunder(ref.name):
            graph.add(
                Edge(ref.symbol, _name_node(ref.name), UNRESOLVED_NAME_MATCH, ref.detail), revs
            )
        pending_unresolved.append((ref, revs))

    # Targets join the graph as nodes with explicit dependency edges.
    dynamic_deps: dict[str, list[str]] = defaultdict(list)
    for target in targets:
        if target.entry_symbol in known_symbols:
            graph.add(Edge(target.node_id, target.entry_symbol, ENTRY), ("manifest",))
        else:
            fallbacks.append(
                Fallback(
                    RULE_ENTRY_UNRESOLVED,
                    "target",
                    f"entry symbol {target.entry_symbol!r} was not found in either revision",
                    target=target.node_id,
                )
            )
        for dep in target.lifecycle_dependencies:
            module = dep[len(DYNAMIC_DEPENDENCY) :] if dep.startswith(DYNAMIC_DEPENDENCY) else None
            if module is not None and module in known_symbols:
                dynamic_deps[target.node_id].append(module)
            elif dep in known_symbols:
                graph.add(Edge(target.node_id, dep, LIFECYCLE), ("manifest",))
            else:
                fallbacks.append(
                    Fallback(
                        RULE_LIFECYCLE_UNRESOLVED,
                        "target",
                        f"lifecycle dependency {dep!r} was not found in either revision",
                        target=target.node_id,
                    )
                )
    graph.freeze()
    seeded = changes if seeds is None else [c for c in changes if c.id in seeds.changes]
    fallbacks += _runner_dependency_fallbacks(targets, seeded, base, head)
    unanalysed = _changed_unanalysed_files(base, head) if seeds is None else []
    if unanalysed:
        shown = ", ".join(unanalysed[:UNANALYSED_PATHS_SHOWN])
        more = len(unanalysed) - UNANALYSED_PATHS_SHOWN
        fallbacks.append(
            Fallback(
                RULE_UNANALYSED_FILE,
                "all_targets",
                f"{len(unanalysed)} file(s) the analysis does not read changed under the source "
                f"roots ({shown}{f', and {more} more' if more > 0 else ''}); code or tests may "
                "read them, so every supplied target is selected",
            )
        )

    # Backward reachability from every changed symbol.
    mode: dict[str, int] = {}
    via: dict[str, tuple[Edge, tuple[str, ...], str] | None] = {}
    queue: deque[str] = deque()
    impacting = [c for c in seeded if c.carries_impact]
    for change in impacting:
        mode[change.id] = STRUCTURAL if change.structural else BEHAVIOR
        via[change.id] = None
        queue.append(change.id)
    # A change that runs when its module is imported affects the module's
    # import, hence (through ``imports`` edges) every importer.
    for change in impacting:
        symbol = change.head or change.base
        assert symbol is not None
        if symbol.module not in mode and _runs_at_import(change):
            mode[symbol.module] = BEHAVIOR
            via[symbol.module] = None
            queue.append(symbol.module)
    seed_reasons = dict(seeds.nodes) if seeds is not None else {}
    for node in sorted(seed_reasons):
        if node not in mode:
            mode[node] = BEHAVIOR
            via[node] = None
            queue.append(node)
    # A dynamic reference (eval/exec/getattr with an unbounded name) can reach
    # whatever its module's globals can reach: the module itself and every
    # module it imports, transitively. A dynamic *import* can reach anything.
    # That bound holds only while the object read is one of those globals.
    # ``def invoke(obj, name): getattr(obj, name)`` reads an object a caller
    # supplied, which can belong to any module; the index binds that read at
    # the other end instead (a class whose instances are handed around gains
    # an edge to each of its members), and the closure check stays as well.
    changed_modules = {(c.head or c.base).module for c in impacting}  # type: ignore[union-attr]
    reach = _ImportReach(base, head)

    if impacting and seeds is None:
        for symbol in sorted(dynamic_symbols):
            if symbol in mode:
                continue
            # A symbol can hold several dynamic references; the widest one
            # decides, so an import that names anything is not narrowed by a
            # getattr beside it.
            if symbol in unbounded_dynamic or reach.closure_of(symbol) & changed_modules:
                mode[symbol] = BEHAVIOR
                via[symbol] = None
                queue.append(symbol)
    while queue:
        node = queue.popleft()
        node_mode = mode[node]
        for source, edge, revs in graph.reverse.get(node, ()):
            new_mode = _propagate(edge, node_mode, change_by_id.get(node))
            if new_mode is None or new_mode <= mode.get(source, 0):
                continue
            mode[source] = new_mode
            via[source] = (edge, revs, node)
            queue.append(source)

    affected_by_name = {
        name: tuple(s for s in symbols if s in mode) for name, symbols in symbols_by_name.items()
    }
    unresolved_records = [
        UnresolvedRecord(
            ref.symbol,
            ref.kind,
            ref.name,
            ref.detail,
            revs,
            tuple(s for s in affected_by_name.get(ref.name, ()) if s != ref.symbol)
            if ref.kind != UNRESOLVED_DYNAMIC
            else (),
        )
        for ref, revs in pending_unresolved
    ]

    if errors:
        fallbacks.append(
            Fallback(
                RULE_ANALYSIS_ERROR,
                "all_targets",
                f"{len(errors)} analysis error(s); the dependency graph is incomplete, "
                "so every supplied target is selected",
            )
        )
    target_fallbacks: dict[str, list[Fallback]] = defaultdict(list)
    global_fallbacks: list[Fallback] = []
    for fb in fallbacks:
        if fb.scope == "target" and fb.target:
            target_fallbacks[fb.target].append(fb)
        else:
            global_fallbacks.append(fb)

    decisions: list[Decision] = []
    for target in targets:
        reasons: list[Reason] = []
        if target.node_id in mode:
            reasons.append(
                _explain(
                    target.node_id,
                    via,
                    change_by_id,
                    dynamic_symbols,
                    unbounded_dynamic,
                    seed_reasons,
                )
            )
        for fb in target_fallbacks.get(target.node_id, ()):
            reasons.append(Reason(fb.rule, fb.detail))
        for fb in global_fallbacks:
            reasons.append(Reason(fb.rule, fb.detail))
        if seeds is not None:
            decisions.append(_decision(target, reasons, mode))
            continue
        for module in dynamic_deps.get(target.node_id, ()):
            if reach.closure_of(module) & changed_modules:
                reasons.append(
                    Reason(
                        RULE_DYNAMIC_REFERENCE,
                        f"the target runs code with the globals of {module}, and a change lies "
                        "in that module's import closure",
                    )
                )
        # A discovered target the base snapshot did not have is new, whatever
        # its entry symbol did: ``from support import test_shared as
        # test_new`` adds a test whose entry is untouched. Only discovery can
        # answer this, and only because it runs at the base as well; a
        # manifest names targets without saying when they appeared.
        if (
            base_target_ids is not None
            and target.runner_id in discovered_ids
            and target.runner_id not in base_target_ids
        ):
            reasons.append(
                Reason(
                    RULE_NEW_TARGET,
                    f"{target.runner_id} is not in the base snapshot: a new target is selected "
                    "whatever its entry symbol did",
                )
            )
        entry_change = change_by_id.get(target.entry_symbol)
        if entry_change is not None and DOCSTRING_CHANGED in entry_change.changes:
            # For a doctest the docstring is the test; for anything else
            # re-running a target whose own docstring changed is cheap.
            reasons.append(
                Reason(
                    RULE_ENTRY_DOCSTRING,
                    f"the docstring of the entry symbol {target.entry_symbol} changed",
                )
            )
        decisions.append(_decision(target, reasons, mode))

    return Plan(
        repo=repo,
        source_roots=list(source_roots or []),
        changes=changes,
        decisions=decisions,
        fallbacks=fallbacks,
        unresolved=sorted(unresolved_records, key=lambda r: (r.symbol, r.kind, r.name, r.detail)),
        errors=errors,
        declarations=sorted(declared),
        base_index=base,
        head_index=head,
        discovery=discovered,
        targets=targets,
    )


def _decision(target: Target, reasons: list[Reason], mode: dict[str, int]) -> Decision:
    affected = sorted(
        dep for dep in (target.entry_symbol, *target.lifecycle_dependencies) if dep in mode
    )
    selected = bool(reasons)
    return Decision(
        target=target,
        selected=selected,
        reasons=reasons,
        affected_dependencies=affected,
        unselected_reason=None
        if selected
        else "no dependency path from this target to a changed symbol in either revision",
    )


def _runner_dependency_fallbacks(
    targets: list[Target], changes: list[SymbolChange], base: SourceIndex, head: SourceIndex
) -> list[Fallback]:
    """A runner whose own process imports modules that are in the source
    roots (pytest runs pluggy's hooks for every test) runs project code for
    every target: a change there selects all of that runner's targets. The
    modules are discovery's RUNNER_MODULES, those under them, and their
    import closure."""
    impacting = [c for c in changes if c.carries_impact]
    if not impacting:
        return []
    modules = base.modules | head.modules
    reach = _ImportReach(base, head)
    fallbacks: list[Fallback] = []
    for runner in sorted({t.runner for t in targets}):
        entries = RUNNER_MODULES.get(runner, ())
        roots = {m for m in modules if any(m == e or m.startswith(e + ".") for e in entries)}
        if not roots:
            continue
        runner_modules: set[str] = set()
        for module in roots:
            runner_modules |= reach.closure_of(module)
        hit = sorted(
            c.id
            for c in impacting
            if (c.head or c.base).module in runner_modules  # type: ignore[union-attr]
        )
        if not hit:
            continue
        shown = ", ".join(hit[:3]) + (f" and {len(hit) - 3} more" if len(hit) > 3 else "")
        detail = (
            f"{shown} changed in code the {runner} runner itself imports and runs for every "
            "target, so every target of that runner is selected"
        )
        for target in targets:
            if target.runner == runner:
                fallbacks.append(
                    Fallback(RULE_RUNNER_DEPENDENCY, "target", detail, target=target.node_id)
                )
    return fallbacks


class _ImportReach:
    """Transitive import closure of modules, over both revisions."""

    def __init__(self, base: SourceIndex, head: SourceIndex) -> None:
        self.module_of: dict[str, str] = {}
        self.imports_of: dict[str, set[str]] = defaultdict(set)
        for index in (base, head):
            for symbol in index.symbols.values():
                self.module_of[symbol.id] = symbol.module
        for index in (base, head):
            for edge in index.edges:
                if edge.kind == IMPORTS and edge.target in self.module_of:
                    source_module = self.module_of.get(edge.source)
                    if source_module is not None:
                        self.imports_of[source_module].add(edge.target)
        self._closures: dict[str, set[str]] = {}

    def closure_of(self, symbol_id: str) -> set[str]:
        module = self.module_of.get(symbol_id)
        if module is None:
            return set()
        if module not in self._closures:
            seen = {module}
            stack = [module]
            while stack:
                for target in self.imports_of.get(stack.pop(), ()):
                    if target not in seen:
                        seen.add(target)
                        stack.append(target)
            self._closures[module] = seen
        return self._closures[module]


def _name_node(name: str) -> str:
    return f"name:{name}"


def _is_name_node(node_id: str) -> bool:
    return node_id.startswith("name:")


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _explain(
    node: str,
    via: dict[str, tuple[Edge, tuple[str, ...], str] | None],
    change_by_id: dict[str, SymbolChange],
    dynamic_symbols: dict[str, tuple[str, ...]],
    unbounded_dynamic: set[str],
    seed_reasons: dict[str, str] | None = None,
) -> Reason:
    steps: list[Step] = []
    current = node
    rule = RULE_DEPENDENCY
    pending: tuple[str, str, tuple[str, ...]] | None = None  # (source, detail, revs)
    while True:
        link = via[current]
        if link is None:
            break
        edge, revs, nxt = link
        if edge.kind == DECLARED and rule == RULE_DEPENDENCY:
            # The path only holds because the project said so; say which rule
            # carried it rather than calling it an ordinary dependency. A name
            # match anywhere on the path outranks it: that one is our guess,
            # this one is the project's statement.
            rule = RULE_DECLARED_DEPENDENCY
        if edge.kind == UNRESOLVED_NAME_MATCH:
            rule = RULE_UNRESOLVED_NAME_MATCH  # outranks a declared edge
            if _is_name_node(edge.target):
                pending = (edge.source, edge.detail, revs)  # collapse the pseudo-node
                current = nxt
                continue
            if pending is not None:
                source, detail, revs = pending
                steps.append(Step(source, edge.target, UNRESOLVED_NAME_MATCH, detail, revs))
                pending = None
                current = nxt
                continue
        steps.append(Step(edge.source, edge.target, edge.kind, edge.detail, revs))
        current = nxt
    change = change_by_id.get(current)
    if change is not None:
        detail = f"{current} {'/'.join(change.changes)}"
        return Reason(rule, detail, tuple(steps), current, change.changes)
    if seed_reasons and current in seed_reasons:
        return Reason(RULE_ESCALATED, seed_reasons[current], tuple(steps))
    # Pseudo-seed: a symbol with a dynamic reference.
    revs = dynamic_symbols.get(current, ())
    if current in unbounded_dynamic:
        return Reason(
            RULE_DYNAMIC_REFERENCE,
            f"{current} imports a module named at runtime ({', '.join(revs)}); any module in "
            "scope may be behind it, so its dependencies cannot be bounded statically",
            tuple(steps),
        )
    return Reason(
        RULE_DYNAMIC_REFERENCE,
        f"{current} uses a dynamic import/attribute access ({', '.join(revs)}); "
        "a change is reachable from its module's imports, so its dependencies cannot be "
        "bounded statically",
        tuple(steps),
        None,
        (),
    )


def _index_snapshot(
    repo_path: Path,
    revision: str,
    roots: list[str],
    *,
    with_config: bool,
    cache: IndexCache | None,
) -> tuple[SourceIndex, Snapshot | None]:
    """Index a snapshot, serving committed snapshots from the cache. Returns
    the snapshot too when it had to be read (discovery needs its files)."""
    if cache is not None and revision not in (WORKTREE, INDEX) and not with_config:
        try:
            commit = resolve_commit(repo_path, revision)
        except GitError:
            commit = None
        if commit is not None:
            cached = cache.load(commit, roots)
            if cached is not None:
                cached = replace(cached, snapshot=replace(cached.snapshot, revision=revision))
                return cached, None
    snapshot = read_snapshot(repo_path, revision, roots, with_config=with_config)
    index = build_index(snapshot, module_cache=cache.modules if cache is not None else None)
    if cache is not None and snapshot.info.committed:
        cache.store(index, roots)
    return index, snapshot


def plan(
    repo: str | Path,
    base: str,
    head: str,
    manifest: Manifest | None = None,
    source_roots: list[str] | None = None,
    discover_runners: Iterable[str] = (),
    discovery_options: DiscoveryOptions | None = None,
    cache: IndexCache | None = None,
    evidence: Evidence | None = None,
) -> Plan:
    """Analyse two committed revisions and produce a selection plan.

    Targets come from the manifest, from static discovery of the head
    snapshot for each runner in ``discover_runners``, or both. ``base`` and
    ``head`` are git revisions, ``INDEX`` or ``WORKTREE``; the plan records
    which kind each one was. With ``evidence`` (recorded by ``diffcone
    collect``) pytest targets are selected on what each test executed
    (evidence_plan.py); the evidence's source roots must match.
    """
    repo_path = Path(repo)
    manifest_roots = manifest.source_roots if manifest is not None else None
    roots = list(source_roots or manifest_roots or ["."])
    runners = list(discover_runners)
    base_index, _ = _index_snapshot(repo_path, base, roots, with_config=False, cache=cache)
    # The head snapshot's files are needed for discovery, so it is only served
    # from cache when nothing is discovered.
    head_index, head_snapshot = _index_snapshot(
        repo_path, head, roots, with_config=bool(runners), cache=cache
    )
    if runners and head_snapshot is None:  # pragma: no cover - guarded above
        raise GitError("discovery needs the head snapshot's files")
    # Both revisions, as every other edge is: a commit that deletes a
    # declaration while changing what it pointed at must still select.
    # Discovery at the base too, so a target the base did not have is known
    # to be new. Only the snapshot is read again (0.1 s on the largest
    # repositories); the base index still comes from the cache.
    base_target_ids: set[str] | None = None
    if runners:
        base_snapshot = read_snapshot(repo_path, base, roots, with_config=True)
        base_target_ids = {
            target.runner_id
            for runner in runners
            for target in discover(runner, base_snapshot, base_index, discovery_options).targets
        }
    declared: list[Declaration] = []
    for revision, index in ((base, base_index), (head, head_index)):
        found, problems = load_declarations(repo_path, revision)
        declared += found
        for problem in problems:
            index.errors.append(
                AnalysisError(revision=revision, path=DECLARATION_FILE, message=problem)
            )
    declared = sorted(set(declared))
    discovered = [
        discover(runner, head_snapshot, head_index, discovery_options) for runner in runners
    ]
    if evidence is not None:
        from diffcone.evidence_plan import plan_with_evidence

        if sorted(evidence.source_roots) != sorted(roots):
            raise EvidenceError(
                f"the evidence was recorded with source roots {evidence.source_roots}, the plan "
                f"uses {roots}: symbols would not line up"
            )
        evidence_index, _ = _index_snapshot(
            repo_path, evidence.commit, roots, with_config=False, cache=cache
        )
        return plan_with_evidence(
            base_index,
            head_index,
            evidence,
            evidence_index,
            manifest,
            repo=str(repo_path),
            source_roots=roots,
            discovered=discovered,
            declarations=declared,
            base_target_ids=base_target_ids,
        )
    return plan_from_indexes(
        base_index,
        head_index,
        manifest,
        repo=str(repo_path),
        source_roots=roots,
        discovered=discovered,
        declarations=declared,
        base_target_ids=base_target_ids,
    )
