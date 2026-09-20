"""Change classifier: compares two source indexes symbol by symbol."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from diffcone.model import CLASS, DEFINED_IN, MODULE, SourceIndex, Symbol

ADDED = "added"
DELETED = "deleted"
BODY_CHANGED = "body_changed"
DEFINITION_CHANGED = "definition_changed"
IMPORTS_ADDED = "imports_added"  # modules: new import bindings only
DEPENDENCIES_CHANGED = "dependencies_changed"  # an edge was removed or redirected
DEPENDENCIES_ADDED = "dependencies_added"  # edges were only added

# Changes that invalidate everything defined inside the symbol (and, for
# deletions, everything that imports it), not just direct references.
# Pure additions (a new import binding, a new dependency edge) cannot break
# an existing member: a member whose own resolution changed because of the
# addition carries its own dependencies_changed.
STRUCTURAL = frozenset({ADDED, DELETED, DEFINITION_CHANGED, DEPENDENCIES_CHANGED})


@dataclass(frozen=True, order=True)
class SymbolChange:
    id: str
    kind: str
    changes: tuple[str, ...]
    base: Symbol | None
    head: Symbol | None

    @property
    def structural(self) -> bool:
        """Whether the change invalidates every member of the symbol.

        Class bodies count: class-level attributes (ASV ``params``, pytest
        marks, registries) shape how every method runs even when no method
        references them textually. Module bodies do not; members that use
        module state carry their own edges (see docs/design.md).
        """
        if STRUCTURAL & set(self.changes):
            return True
        return self.kind == CLASS and BODY_CHANGED in self.changes


def _dependency_signatures(index: SourceIndex) -> dict[str, frozenset[tuple[str, str, str]]]:
    grouped: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for e in index.edges:
        if e.kind != DEFINED_IN:
            grouped[e.source].add((e.kind, e.target, e.detail))
    return {source: frozenset(sig) for source, sig in grouped.items()}


def classify(base: SourceIndex, head: SourceIndex) -> list[SymbolChange]:
    """Return every symbol that differs between the two revisions.

    Symbols whose module failed to parse in one revision are skipped: their
    status is unknown, and the planner handles that through the analysis
    error fallback instead of inventing additions or deletions.
    """
    changes: list[SymbolChange] = []
    base_deps = _dependency_signatures(base)
    head_deps = _dependency_signatures(head)
    empty: frozenset[tuple[str, str, str]] = frozenset()
    for symbol_id in sorted(set(base.symbols) | set(head.symbols)):
        b = base.symbols.get(symbol_id)
        h = head.symbols.get(symbol_id)
        module = (b or h).module  # type: ignore[union-attr]
        if b is None and module in base.failed_modules:
            continue
        if h is None and module in head.failed_modules:
            continue
        if b is None:
            changes.append(SymbolChange(symbol_id, h.kind, (ADDED,), None, h))  # type: ignore[union-attr]
            continue
        if h is None:
            changes.append(SymbolChange(symbol_id, b.kind, (DELETED,), b, None))
            continue
        kinds: list[str] = []
        if b.body_hash != h.body_hash:
            kinds.append(BODY_CHANGED)
        if b.kind != h.kind:
            kinds.append(DEFINITION_CHANGED)
        elif b.definition_hash != h.definition_hash:
            if b.kind == MODULE and set(b.imports) <= set(h.imports):
                kinds.append(IMPORTS_ADDED)
            else:
                kinds.append(DEFINITION_CHANGED)
        before = base_deps.get(symbol_id, empty)
        after = head_deps.get(symbol_id, empty)
        if before != after:
            kinds.append(DEPENDENCIES_ADDED if before <= after else DEPENDENCIES_CHANGED)
        if kinds:
            changes.append(SymbolChange(symbol_id, h.kind, tuple(kinds), b, h))
    return changes
