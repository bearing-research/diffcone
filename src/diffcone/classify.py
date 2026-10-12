"""Change classifier: compares two source indexes symbol by symbol."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum

from diffcone.indexer.definitions import INERT_DEF
from diffcone.model import (
    CLASS,
    MODULE,
    EdgeKind,
    SourceIndex,
    Symbol,
)


class ChangeKind(StrEnum):
    """How a symbol differs between the snapshots (``SymbolChange.changes``,
    the report's ``changes``; docs/concepts.md says what each means)."""

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
# addition carries its own dependencies_added (a name it used now
# resolves) or dependencies_changed (a binding it used moved).
STRUCTURAL = frozenset(
    {
        ChangeKind.ADDED,
        ChangeKind.DELETED,
        ChangeKind.DEFINITION_CHANGED,
        ChangeKind.DEPENDENCIES_CHANGED,
    }
)

# Changes that are reported but carry no impact of their own: nothing an
# existing dependent can observe differs (a member whose resolution moved
# because of the addition carries its own dependencies_added or
# dependencies_changed).
NON_IMPACT = frozenset({ChangeKind.IMPORTS_ADDED, ChangeKind.DOCSTRING_CHANGED})


@dataclass(frozen=True, order=True)
class SymbolChange:
    id: str
    kind: str
    changes: tuple[ChangeKind, ...]
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
        return self.kind == CLASS and ChangeKind.BODY_CHANGED in self.changes


# Dependency-signature kinds for references outside the edge graph.
UNRESOLVED_SIG = "unresolved"
EXTERNAL_SIG = "external"


def _dependency_signatures(index: SourceIndex) -> dict[str, frozenset[tuple[str, str, str]]]:
    """Each symbol's outgoing edges, and its unresolved and external
    references: a name that starts resolving to a third-party module (the
    ``from json import dumps`` that fixes a NameError) changes what the
    symbol does as much as a new edge does."""
    grouped: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for e in index.edges:
        if e.kind != EdgeKind.DEFINED_IN:
            grouped[e.source].add((e.kind, e.target, e.detail))
    for u in index.unresolved:
        grouped[u.symbol].add((UNRESOLVED_SIG, f"{u.kind}:{u.name}", ""))
    for x in index.external:
        grouped[x.symbol].add((EXTERNAL_SIG, x.module, ""))
    return {source: frozenset(sig) for source, sig in grouped.items()}


def _is_subsequence(before: tuple[str, ...], after: tuple[str, ...]) -> bool:
    """Whether ``after`` is ``before`` with entries inserted: every import
    kept its block and its order relative to the others."""
    remaining = iter(after)
    return all(entry in remaining for entry in before)


def reordered(before: tuple[str, ...], after: tuple[str, ...]) -> bool:
    """Whether statements both layouts have (``Symbol.statement_layout``)
    stand in another order: a reorder of a module's or class's body, a
    definition change (audit round 5, R6). The statements of ``before`` that
    an order-preserving matching with ``after`` cannot pair (a longest
    common subsequence) are deletions, and the unpaired ones of ``after``
    insertions, carried by their own symbols (or by the body hash, which
    keeps the order of plain statements among themselves): a reorder is
    when fewer can be paired than the two have in common. Adjacent inert
    ``def`` statements (``INERT_DEF``) bind a name each and run nothing:
    swapping them is no reorder."""
    if before == after:
        return False
    a = _inert_runs_sorted(before)
    b = _inert_runs_sorted(after)
    # The common prefix and suffix pair up as they stand.
    start = 0
    while start < len(a) and start < len(b) and a[start] == b[start]:
        start += 1
    end = 0
    while end < len(a) - start and end < len(b) - start and a[-1 - end] == b[-1 - end]:
        end += 1
    a = a[start : len(a) - end]
    b = b[start : len(b) - end]
    common = sum((Counter(a) & Counter(b)).values())
    return common > 0 and _lcs_length(a, b) < common


def _lcs_length(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0]
        for j, y in enumerate(b, 1):
            current.append(previous[j - 1] + 1 if x == y else max(previous[j], current[j - 1]))
        previous = current
    return previous[-1]


def _inert_runs_sorted(entries: tuple[str, ...]) -> list[str]:
    """``entries`` with each run of consecutive inert defs sorted."""
    out: list[str] = []
    run: list[str] = []
    for entry in entries:
        if entry.startswith(INERT_DEF):
            run.append(entry)
            continue
        out += sorted(run)
        run = []
        out.append(entry)
    return out + sorted(run)


def _runtime_imports(symbol: Symbol, modules: set[str]) -> set[str]:
    """The in-scope modules a module's own top-level code imports when it
    runs, from its import layout: not imports in function bodies (not in the
    layout) or under ``if TYPE_CHECKING:``, which never run."""
    found: set[str] = set()
    for entry in symbol.import_layout:
        context, _, statement = entry.rpartition("|")
        if "TYPE_CHECKING" in context:
            continue
        words = statement.split()
        if words[:1] == ["import"] and len(words) >= 2:
            names = [words[1]]
        elif words[:1] == ["from"] and len(words) >= 4:
            names = [words[1], f"{words[1]}.{words[3]}"]
        else:
            continue
        for name in names:
            # ``import a.b.c`` runs ``a`` and ``a.b`` too.
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                if ".".join(parts[:i]) in modules:
                    found.add(".".join(parts[:i]))
    return found


def _import_closures(index: SourceIndex) -> dict[str, set[str]]:
    """Each module's transitive closure of the modules importing it runs
    (itself included)."""
    modules = {s.id for s in index.symbols.values() if s.kind == MODULE}
    imports = {m: _runtime_imports(index.symbols[m], modules) for m in modules}
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

    return {module: closure(module) for module in modules}


def _module_additions_matter(
    module: str,
    added: frozenset[tuple[str, str, str]],
    base_closures: dict[str, set[str]],
    base_modules: set[str],
    b: Symbol,
    h: Symbol,
) -> bool:
    """Whether a module's additions change what importing it does: its
    import-time code references something new, or an import that now runs
    at import reaches an in-scope module importing it did not run before (a
    registration import such as ``import pkg.json_handler``, one moved out of
    a function or out of ``if TYPE_CHECKING:``). A module new in head is
    added, and reached through its own change. ``base_modules``: the
    base's modules."""
    closure = base_closures.get(module, {module})
    for kind, _target, _detail in added:
        # A new third-party import is not import-time code of the project's
        # (installed code is assumed unchanged); anything else is.
        if kind not in (EdgeKind.IMPORTS, EdgeKind.IMPORTS_NAME, EXTERNAL_SIG):
            return True
    newly_run = _runtime_imports(h, base_modules) - _runtime_imports(b, base_modules)
    return bool(newly_run - closure)


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
    base_modules = {s.id for s in base.symbols.values() if s.kind == MODULE}
    empty: frozenset[tuple[str, str, str]] = frozenset()
    for symbol_id in sorted(set(base.symbols) | set(head.symbols)):
        b = base.symbols.get(symbol_id)
        h = head.symbols.get(symbol_id)
        if b is None:
            assert h is not None  # the id came from one of the two
            if h.module not in base.failed_modules:
                changes.append(SymbolChange(symbol_id, h.kind, (ChangeKind.ADDED,), None, h))
            continue
        if h is None:
            if b.module in head.failed_modules:
                continue
            changes.append(SymbolChange(symbol_id, b.kind, (ChangeKind.DELETED,), b, None))
            continue
        kinds: list[ChangeKind] = []
        if b.body_hash != h.body_hash:
            kinds.append(ChangeKind.BODY_CHANGED)
        elif b.docstring_hash != h.docstring_hash:
            kinds.append(ChangeKind.DOCSTRING_CHANGED)
        if b.kind != h.kind:
            kinds.append(ChangeKind.DEFINITION_CHANGED)
        elif b.definition_hash != h.definition_hash:
            future = {i for i in set(b.imports) ^ set(h.imports) if i.startswith("from __future__")}
            if (
                b.kind == MODULE
                and set(b.imports) <= set(h.imports)
                and _is_subsequence(b.import_layout, h.import_layout)
                and not future
            ):
                kinds.append(ChangeKind.IMPORTS_ADDED)
            else:
                kinds.append(ChangeKind.DEFINITION_CHANGED)
        if (
            b.kind in (MODULE, CLASS)
            and ChangeKind.DEFINITION_CHANGED not in kinds
            and reordered(b.statement_layout, h.statement_layout)
        ):
            # The statements both have run in another order: what each
            # sees when it runs differs (R6).
            kinds.append(ChangeKind.DEFINITION_CHANGED)
        if b.annotation_hash != h.annotation_hash and ChangeKind.DEFINITION_CHANGED not in kinds:
            deferred = b.deferred_annotations and h.deferred_annotations
            kinds.append(
                ChangeKind.ANNOTATIONS_CHANGED if deferred else ChangeKind.DEFINITION_CHANGED
            )
        before = base_deps.get(symbol_id, empty)
        after = head_deps.get(symbol_id, empty)
        if ChangeKind.BODY_CHANGED in kinds or ChangeKind.DEFINITION_CHANGED in kinds:
            # The edit itself explains new unresolved and external references;
            # they matter on their own only when a binding moved under them.
            before = frozenset(d for d in before if d[0] not in (UNRESOLVED_SIG, EXTERNAL_SIG))
            after = frozenset(d for d in after if d[0] not in (UNRESOLVED_SIG, EXTERNAL_SIG))
        # A name that stopped being unresolved now resolves: an addition, not
        # a redirect (which is a resolved dependency lost or moved).
        lost = {d for d in before - after if d[0] != UNRESOLVED_SIG}
        resolved_now = any(d[0] == UNRESOLVED_SIG for d in before - after)
        if before != after:
            if lost:
                kinds.append(ChangeKind.DEPENDENCIES_CHANGED)
            elif (
                h.kind != MODULE
                or resolved_now
                or _module_additions_matter(
                    symbol_id, after - before, base_closures, base_modules, b, h
                )
            ):
                kinds.append(ChangeKind.DEPENDENCIES_ADDED)
            elif ChangeKind.IMPORTS_ADDED not in kinds:
                kinds.append(ChangeKind.IMPORTS_ADDED)
        elif (
            h.kind == MODULE
            and ChangeKind.IMPORTS_ADDED in kinds
            and _module_additions_matter(symbol_id, frozenset(), base_closures, base_modules, b, h)
        ):
            # No new edge (a TYPE_CHECKING copy already had it), but the
            # import now runs.
            kinds.append(ChangeKind.DEPENDENCIES_ADDED)
        if kinds:
            changes.append(SymbolChange(symbol_id, h.kind, tuple(kinds), b, h))
    return changes
