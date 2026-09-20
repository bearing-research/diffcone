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
* ``imports``/``imports_name``: module-level imports break only when the
  imported module or name is deleted; then the importing module and every
  member of it are invalidated.

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
from diffcone.classify import DELETED, SymbolChange, classify
from diffcone.discovery import DiscoveryOptions, DiscoveryResult, discover
from diffcone.indexer import build_index
from diffcone.manifest import Manifest, Target
from diffcone.model import (
    DEFINED_IN,
    ENTRY,
    IMPORTS,
    IMPORTS_NAME,
    LIFECYCLE,
    MODULE,
    UNRESOLVED_DYNAMIC,
    UNRESOLVED_NAME_MATCH,
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

CONSERVATIVE_RULES = frozenset(
    {
        RULE_UNRESOLVED_NAME_MATCH,
        RULE_DYNAMIC_REFERENCE,
        RULE_ENTRY_UNRESOLVED,
        RULE_LIFECYCLE_UNRESOLVED,
        RULE_ANALYSIS_ERROR,
    }
)


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
        return None
    return BEHAVIOR


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


def plan_from_indexes(
    base: SourceIndex,
    head: SourceIndex,
    manifest: Manifest | None,
    *,
    repo: str = "",
    source_roots: list[str] | None = None,
    discovered: list[DiscoveryResult] | None = None,
) -> Plan:
    discovered = list(discovered or [])
    targets = merge_targets(manifest, discovered)
    changes = classify(base, head)
    change_by_id = {c.id: c for c in changes}
    known_symbols = set(base.symbols) | set(head.symbols)
    fallbacks: list[Fallback] = []
    errors = sorted(base.errors + head.errors)

    graph = _Graph()
    for edge, revs in _union(base.edges, head.edges).items():
        graph.add(edge, revs)

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
    for symbol_id in sorted(known_symbols):
        symbol = head.symbols.get(symbol_id) or base.symbols[symbol_id]
        if symbol.kind != MODULE and not _is_dunder(symbol.name):
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
            if dep in known_symbols:
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

    # Backward reachability from every changed symbol.
    mode: dict[str, int] = {}
    via: dict[str, tuple[Edge, tuple[str, ...], str] | None] = {}
    queue: deque[str] = deque()
    impacting = [c for c in changes if c.carries_impact]
    for change in impacting:
        mode[change.id] = STRUCTURAL if change.structural else BEHAVIOR
        via[change.id] = None
        queue.append(change.id)
    # A dynamic reference (eval/exec/getattr with an unbounded name) can reach
    # whatever its module's globals can reach: the module itself and every
    # module it imports, transitively. A dynamic *import* can reach anything.
    if impacting:
        changed_modules = {(c.head or c.base).module for c in impacting}  # type: ignore[union-attr]
        reach = _ImportReach(base, head)
        for symbol in sorted(dynamic_symbols):
            if symbol in mode:
                continue
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
            reasons.append(_explain(target.node_id, via, change_by_id, dynamic_symbols))
        for fb in target_fallbacks.get(target.node_id, ()):
            reasons.append(Reason(fb.rule, fb.detail))
        for fb in global_fallbacks:
            reasons.append(Reason(fb.rule, fb.detail))
        affected = sorted(
            dep for dep in (target.entry_symbol, *target.lifecycle_dependencies) if dep in mode
        )
        selected = bool(reasons)
        decisions.append(
            Decision(
                target=target,
                selected=selected,
                reasons=reasons,
                affected_dependencies=affected,
                unselected_reason=None
                if selected
                else "no dependency path from this target to a changed symbol in either revision",
            )
        )

    return Plan(
        repo=repo,
        source_roots=list(source_roots or []),
        changes=changes,
        decisions=decisions,
        fallbacks=fallbacks,
        unresolved=sorted(unresolved_records, key=lambda r: (r.symbol, r.kind, r.name, r.detail)),
        errors=errors,
        base_index=base,
        head_index=head,
        discovery=discovered,
        targets=targets,
    )


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
        if edge.kind == UNRESOLVED_NAME_MATCH:
            rule = RULE_UNRESOLVED_NAME_MATCH
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
    # Pseudo-seed: a symbol with a dynamic reference.
    revs = dynamic_symbols.get(current, ())
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
) -> Plan:
    """Analyse two committed revisions and produce a selection plan.

    Targets come from the manifest, from static discovery of the head
    snapshot for each runner in ``discover_runners``, or both. ``base`` and
    ``head`` are git revisions, ``INDEX`` or ``WORKTREE``; the plan records
    which kind each one was.
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
    discovered = [
        discover(runner, head_snapshot, head_index, discovery_options) for runner in runners
    ]
    return plan_from_indexes(
        base_index,
        head_index,
        manifest,
        repo=str(repo_path),
        source_roots=roots,
        discovered=discovered,
    )
