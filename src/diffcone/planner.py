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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

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
from diffcone.snapshot import read_snapshot

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
        for adj in (self.reverse, self.forward):
            for key in adj:
                adj[key].sort(key=lambda item: (item[0], item[1]))


T = TypeVar("T")


def _union(base_items: Iterable[T], head_items: Iterable[T]) -> dict[T, tuple[str, ...]]:
    """Merge two revisions' items, tagging each with the revisions it appears in."""
    revs: dict[T, list[str]] = defaultdict(list)
    for item in sorted(base_items):  # type: ignore[type-var]
        revs[item].append("base")
    for item in sorted(head_items):  # type: ignore[type-var]
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
    # known ``run`` (function, method or class) in either revision, so it gets
    # an edge to each of them and impact flows through the graph as usual;
    # matching only *changed* symbols would miss a ``run`` that is unchanged
    # but calls something that changed. Dynamic references are pseudo-seeds.
    symbols_by_name: dict[str, list[str]] = defaultdict(list)
    for symbol_id in sorted(known_symbols):
        symbol = head.symbols.get(symbol_id) or base.symbols[symbol_id]
        if symbol.kind != MODULE:
            symbols_by_name[symbol.name].append(symbol_id)
    pending_unresolved: list[tuple[UnresolvedReference, tuple[str, ...], tuple[str, ...]]] = []
    dynamic_symbols: dict[str, tuple[str, ...]] = {}
    for ref, revs in _union(base.unresolved, head.unresolved).items():
        if ref.kind == UNRESOLVED_DYNAMIC:
            dynamic_symbols.setdefault(ref.symbol, revs)
            pending_unresolved.append((ref, revs, ()))
            continue
        matches = tuple(s for s in symbols_by_name.get(ref.name, ()) if s != ref.symbol)
        for match in matches:
            graph.add(Edge(ref.symbol, match, UNRESOLVED_NAME_MATCH, ref.detail), revs)
        pending_unresolved.append((ref, revs, matches))

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
    for change in changes:
        mode[change.id] = STRUCTURAL if change.structural else BEHAVIOR
        via[change.id] = None
        queue.append(change.id)
    if changes:
        for symbol in sorted(dynamic_symbols):
            if symbol not in mode:
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

    unresolved_records = [
        UnresolvedRecord(
            ref.symbol,
            ref.kind,
            ref.name,
            ref.detail,
            revs,
            tuple(m for m in matches if m in mode),  # matches that actually carry impact
        )
        for ref, revs, matches in pending_unresolved
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


def _explain(
    node: str,
    via: dict[str, tuple[Edge, tuple[str, ...], str] | None],
    change_by_id: dict[str, SymbolChange],
    dynamic_symbols: dict[str, tuple[str, ...]],
) -> Reason:
    steps: list[Step] = []
    current = node
    rule = RULE_DEPENDENCY
    while True:
        link = via[current]
        if link is None:
            break
        edge, revs, nxt = link
        steps.append(Step(edge.source, edge.target, edge.kind, edge.detail, revs))
        if edge.kind == UNRESOLVED_NAME_MATCH:
            rule = RULE_UNRESOLVED_NAME_MATCH
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
        "its dependencies cannot be bounded statically",
        tuple(steps),
        None,
        (),
    )


def plan(
    repo: str | Path,
    base: str,
    head: str,
    manifest: Manifest | None = None,
    source_roots: list[str] | None = None,
    discover_runners: Iterable[str] = (),
    discovery_options: DiscoveryOptions | None = None,
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
    base_index = build_index(read_snapshot(repo_path, base, roots))
    runners = list(discover_runners)
    head_snapshot = read_snapshot(repo_path, head, roots, with_config=bool(runners))
    head_index = build_index(head_snapshot)
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
