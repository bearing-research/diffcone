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


@dataclass
class SourceIndex:
    revision: str
    commit: str
    kind: str = "commit"  # "commit" | "index" | "worktree"
    description: str = ""
    modules: set[str] = field(default_factory=set)
    symbols: dict[str, Symbol] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)
    unresolved: set[UnresolvedReference] = field(default_factory=set)
    external: set[ExternalReference] = field(default_factory=set)
    errors: list[AnalysisError] = field(default_factory=list)
    # Modules that failed to parse; their symbols are unknown in this revision.
    failed_modules: set[str] = field(default_factory=set)
