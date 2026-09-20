"""Explicit data structures shared by the indexer, classifier and planner.

Symbol identity is the dotted qualified name (``pkg.mod``, ``pkg.mod.func``,
``pkg.mod.Class.method``). Source locations are metadata only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MODULE = "module"
CLASS = "class"
FUNCTION = "function"
METHOD = "method"
VARIABLE = "variable"  # a simple module-level assignment ``NAME = <expr>``

# Edge kinds. ``source`` depends on ``target``.
REFERENCES = "references"  # name/call/attribute reference resolved to a symbol
DEFINED_IN = "defined_in"  # symbol is defined inside the target container
IMPORTS = "imports"  # module-level import of a module (init-time dependency)
IMPORTS_NAME = "imports_name"  # module-level ``from m import name`` of a symbol
UNRESOLVED_NAME_MATCH = "unresolved_name_match"  # conservative edge synthesised by the planner
ENTRY = "entry"  # target -> its entry symbol
LIFECYCLE = "lifecycle"  # target -> declared setup/fixture dependency

# Unresolved reference kinds.
UNRESOLVED_NAME = "name"  # bare name that resolves to nothing known
UNRESOLVED_ATTRIBUTE = "attribute"  # ``<unknown>.name`` — bounded by the attribute name
UNRESOLVED_DYNAMIC = "dynamic"  # getattr/importlib/eval with non-literal arguments


@dataclass(frozen=True, order=True)
class Symbol:
    id: str
    kind: str
    module: str
    name: str
    path: str
    lineno: int
    body_hash: str
    definition_hash: str
    container: str | None
    # Source line spans of every definition of this symbol (metadata, not
    # identity); used by coverage-based validation to map executed lines back
    # to symbols.
    line_ranges: tuple[tuple[int, int], ...] = ()
    # Modules only: canonical import bindings ("import a as b", "from m import n"),
    # sorted. Lets the classifier tell additions from removals/redirections.
    imports: tuple[str, ...] = ()
    # Hash of the docstring alone; body_hash excludes it. A docstring-only edit
    # is reported as docstring_changed and carries no impact.
    docstring_hash: str = ""

    def covers_line(self, line: int) -> bool:
        return any(start <= line <= end for start, end in self.line_ranges)


@dataclass(frozen=True, order=True)
class Edge:
    source: str
    target: str
    kind: str
    detail: str = ""


@dataclass(frozen=True, order=True)
class UnresolvedReference:
    symbol: str
    kind: str
    name: str
    detail: str


@dataclass(frozen=True, order=True)
class ExternalReference:
    symbol: str
    module: str


@dataclass(frozen=True, order=True)
class AnalysisError:
    revision: str
    path: str
    message: str


KIND_COMMIT = "commit"
KIND_INDEX = "index"
KIND_WORKTREE = "worktree"


@dataclass(frozen=True)
class SnapshotInfo:
    """What a snapshot is: carried unchanged from the reader to the report."""

    revision: str
    commit: str
    kind: str = KIND_COMMIT
    description: str = ""

    @property
    def committed(self) -> bool:
        return self.kind == KIND_COMMIT

    @property
    def is_worktree(self) -> bool:
        return self.kind == KIND_WORKTREE


@dataclass
class SourceIndex:
    snapshot: SnapshotInfo
    modules: set[str] = field(default_factory=set)
    symbols: dict[str, Symbol] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)
    unresolved: set[UnresolvedReference] = field(default_factory=set)
    external: set[ExternalReference] = field(default_factory=set)
    errors: list[AnalysisError] = field(default_factory=list)
    # Modules that failed to parse; their symbols are unknown in this revision.
    failed_modules: set[str] = field(default_factory=set)

    @property
    def revision(self) -> str:
        return self.snapshot.revision

    @property
    def commit(self) -> str:
        return self.snapshot.commit
