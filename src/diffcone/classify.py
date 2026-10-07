"""Change classifier: compares two source indexes symbol by symbol."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from diffcone.model import CLASS, DEFINED_IN, IMPORTS, IMPORTS_NAME, MODULE, SourceIndex, Symbol

ADDED = "added"
DELETED = "deleted"
BODY_CHANGED = "body_changed"
DEFINITION_CHANGED = "definition_changed"
IMPORTS_ADDED = "imports_added"  # modules: new import bindings only
DEPENDENCIES_CHANGED = "dependencies_changed"  # an edge was removed or redirected
# Edges were only added. A name the symbol uses now resolves to something
# (a missing import added, a fallback import that now succeeds): its own
# behaviour changed. For a module, only when its import-time code gained a
# reference, or an added import brings in-scope modules into its import
# closure whose import-time code did not run before; new name bindings alone
# are ``imports_added``, their users carrying their own added edges.
DEPENDENCIES_ADDED = "dependencies_added"
DOCSTRING_CHANGED = "docstring_changed"  # only the docstring differs
# Only a function's annotations differ and they are never evaluated at import
# (see Symbol.deferred_annotations): behaviour-level, not structural, not
# import-time (introspection such as typer's happens when called).
ANNOTATIONS_CHANGED = "annotations_changed"

# Changes that invalidate everything defined inside the symbol (and, for
# deletions, everything that imports it), not just direct references.
# Pure additions (a new import binding, a new dependency edge) cannot break
# an existing member: a member whose own resolution changed because of the
# addition carries its own dependencies_changed.
STRUCTURAL = frozenset({ADDED, DELETED, DEFINITION_CHANGED, DEPENDENCIES_CHANGED})

# Changes that are reported but carry no impact of their own: nothing an
# existing dependent can observe differs (a member whose resolution moved
# because of the addition carries its own dependencies_added or
# dependencies_changed).
NON_IMPACT = frozenset({IMPORTS_ADDED, DOCSTRING_CHANGED})


@dataclass(frozen=True, order=True)
class SymbolChange:
    id: str
    kind: str
    changes: tuple[str, ...]
    base: Symbol | None
    head: Symbol | None

    @property
    def symbol(self) -> Symbol:
        """The symbol as it is in head, or as it was in base when deleted:
        a change always has one side."""
        symbol = self.head or self.base
        assert symbol is not None, f"{self.id} has neither side"
        return symbol

    @property
    def carries_impact(self) -> bool:
        return not set(self.changes) <= NON_IMPACT

    @property
    def structural(self) -> bool:
        """Whether the change invalidates every member of the symbol.

        Class bodies count: class-level attributes (ASV ``params``, pytest
        marks, registries) shape how every method runs even when no method
        references them textually. Module bodies do not; members that use
        module state carry their own edges (see internal/design.md).
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


def _is_subsequence(before: tuple[str, ...], after: tuple[str, ...]) -> bool:
    """Whether ``after`` is ``before`` with entries inserted: every import
    kept its block and its order relative to the others."""
    remaining = iter(after)
    return all(entry in remaining for entry in before)


def _import_closures(index: SourceIndex) -> dict[str, set[str]]:
    """Each module's transitive import closure (itself included)."""
    imports: dict[str, set[str]] = defaultdict(set)
    for e in index.edges:
        if e.kind == IMPORTS and e.target in index.symbols:
            source = index.symbols.get(e.source)
            if source is not None:
                imports[source.module].add(e.target)
    closures: dict[str, set[str]] = {}

    def closure(module: str) -> set[str]:
        if module not in closures:
            seen = {module}
            stack = [module]
            while stack:
                for target in imports.get(stack.pop(), ()):
                    if target not in seen:
                        seen.add(target)
                        stack.append(target)
            closures[module] = seen
        return closures[module]

    return {module: closure(module) for module in list(imports)}


def _module_additions_matter(
    module: str,
    added: frozenset[tuple[str, str, str]],
    base_closures: dict[str, set[str]],
    base: SourceIndex,
) -> bool:
    """Whether edges added to a module change what importing it does: its
    import-time code references something new, or an added import reaches
    an in-scope module that importing it did not run before (a registration
    import such as ``import pkg.json_handler``). A module new in head is
    added, and reached through its own change."""
    closure = base_closures.get(module, {module})
    for kind, target, _detail in added:
        if kind not in (IMPORTS, IMPORTS_NAME):
            return True
        if kind == IMPORTS and target in base.symbols and target not in closure:
            return True
    return False


def classify(base: SourceIndex, head: SourceIndex) -> list[SymbolChange]:
    """Return every symbol that differs between the two revisions.

    Symbols whose module failed to parse in one revision are skipped: their
    status is unknown, and the planner handles that through the analysis
    error fallback instead of inventing additions or deletions.
    """
    changes: list[SymbolChange] = []
    base_deps = _dependency_signatures(base)
    head_deps = _dependency_signatures(head)
    base_closures = _import_closures(base)
    empty: frozenset[tuple[str, str, str]] = frozenset()
    for symbol_id in sorted(set(base.symbols) | set(head.symbols)):
        b = base.symbols.get(symbol_id)
        h = head.symbols.get(symbol_id)
        if b is None:
            assert h is not None  # the id came from one of the two
            if h.module not in base.failed_modules:
                changes.append(SymbolChange(symbol_id, h.kind, (ADDED,), None, h))
            continue
        if h is None:
            if b.module in head.failed_modules:
                continue
            changes.append(SymbolChange(symbol_id, b.kind, (DELETED,), b, None))
            continue
        kinds: list[str] = []
        if b.body_hash != h.body_hash:
            kinds.append(BODY_CHANGED)
        elif b.docstring_hash != h.docstring_hash:
            kinds.append(DOCSTRING_CHANGED)
        if b.kind != h.kind:
            kinds.append(DEFINITION_CHANGED)
        elif b.definition_hash != h.definition_hash:
            if (
                b.kind == MODULE
                and set(b.imports) <= set(h.imports)
                and _is_subsequence(b.import_layout, h.import_layout)
            ):
                kinds.append(IMPORTS_ADDED)
            else:
                kinds.append(DEFINITION_CHANGED)
        if b.annotation_hash != h.annotation_hash and DEFINITION_CHANGED not in kinds:
            deferred = b.deferred_annotations and h.deferred_annotations
            kinds.append(ANNOTATIONS_CHANGED if deferred else DEFINITION_CHANGED)
        before = base_deps.get(symbol_id, empty)
        after = head_deps.get(symbol_id, empty)
        if before != after:
            if not before <= after:
                kinds.append(DEPENDENCIES_CHANGED)
            elif h.kind != MODULE or _module_additions_matter(
                symbol_id, after - before, base_closures, base
            ):
                kinds.append(DEPENDENCIES_ADDED)
            elif IMPORTS_ADDED not in kinds:
                kinds.append(IMPORTS_ADDED)
        if kinds:
            changes.append(SymbolChange(symbol_id, h.kind, tuple(kinds), b, h))
    return changes
