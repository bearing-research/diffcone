"""Explicit data structures shared by the indexer, classifier and planner.

Symbol identity is the dotted qualified name (``pkg.mod``, ``pkg.mod.func``,
``pkg.mod.Class.method``). Source locations are metadata only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from diffcone.cython import CythonModule

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
DECLARED = "declared"  # dependency stated in diffcone.toml, not found by analysis
ENTRY = "entry"  # target -> its entry symbol
LIFECYCLE = "lifecycle"  # target -> declared setup/fixture dependency

# Unresolved reference kinds.
UNRESOLVED_NAME = "name"  # bare name that resolves to nothing known
UNRESOLVED_ATTRIBUTE = "attribute"  # ``<unknown>.name`` — bounded by the attribute name
UNRESOLVED_DYNAMIC = "dynamic"  # getattr/importlib/eval with non-literal arguments
OPAQUE_ATTRIBUTE = "*"  # SourceIndex.class_attributes: class-body code binding nothing by name
# Ends the detail of a lookup on an external module that in-scope code writes
# to (SourceIndex.external_sites). No "import" in it: details are read for that.
EXTERNAL_WRITTEN = " on an external module in-scope code writes to"
CLASS_STATEMENT = "(statement)"  # SourceIndex.class_attributes: bases, keywords, decorators


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
    # Modules only: each import binding with the block it sits in, in source
    # order. Moving an import (under ``if TYPE_CHECKING:``, into a ``try``)
    # or reordering imports changes what runs at import, though the set of
    # bindings is the same: only pure insertions are ``imports_added``.
    import_layout: tuple[str, ...] = ()
    # Hash of the docstring alone; body_hash excludes it. A docstring-only edit
    # is reported as docstring_changed and carries no impact, except where
    # code runs it: under a non-inert decorator (or a class decorator or
    # metaclass) the docstring is part of the definition hash, and a module
    # whose own code names ``__doc__`` has it in its body hash.
    docstring_hash: str = ""
    # Functions: hash of the parameter and return annotations, which
    # definition_hash excludes, and whether they are never evaluated at
    # import (``from __future__ import annotations``, no decorator that could
    # read them, a plain class): an annotation-only change then does not run
    # at import (annotations_changed).
    annotation_hash: str = ""
    deferred_annotations: bool = False
    # Functions: the ``def`` statement runs no code at import beyond binding
    # the name (inert decorators, literal defaults, deferred or no
    # annotations, a plain class for methods).
    inert_definition: bool = False
    # Functions: the decorators and defaults alone run nothing (annotations
    # aside): adding such a function registers nothing anywhere.
    inert_header: bool = False
    # Functions: executing the ``def`` runs no code of the project (quiet
    # decorators such as ``pytest.fixture``, arguments, defaults and
    # annotations that call nothing): the body is not code that runs at
    # import, so neither is anything it calls.
    quiet_header: bool = False
    # The symbol's own code reads a docstring (``obj.__doc__``, ``__doc__``,
    # ``getdoc(obj)``): a docstring change of what it references, or of its
    # module, reaches it.
    reads_docstrings: bool = False


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
    # Classes whose instances are passed to someone else, who may then read
    # any attribute off them by a name nothing resolves.
    escaped_classes: set[str] = field(default_factory=set)
    # Functions, methods and classes used as a value somewhere (passed,
    # stored, returned): code the analysis cannot see may call them. Static
    # planning uses this through the indexer; evidence mode asks whether only
    # test code can run an external module's writers (roadmap item 14).
    escaped_values: set[str] = field(default_factory=set)
    # The non-Python files under the source roots: path -> git blob id. The
    # index reads none of them, so the planner compares them whole.
    other_files: dict[str, str] = field(default_factory=dict)
    # Cython sources among them (.pyx, .pxd, .pxi) at function level: path ->
    # module (diffcone.cython). Static planning does not use these; evidence
    # mode diffs them to find the Cython functions a change touched.
    cython: dict[str, CythonModule] = field(default_factory=dict)
    # (symbol, detail): code that observes names or signatures reflectively
    # without naming them (``dir``, ``hasattr``, ``inspect.signature``, a
    # ``__dict__`` read). Static planning does not use these; evidence mode
    # counts them as sites that notice an added, deleted or redefined name.
    reflection: set[tuple[str, str]] = field(default_factory=set)
    # (symbol, detail) of a lookup or reflection site on an external module
    # that in-scope code writes to (its detail ends with EXTERNAL_WRITTEN)
    # -> the symbols writing there. Static planning does not use these;
    # evidence mode bounds such a site by its writers (roadmap item 14).
    external_sites: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    # Class -> {attribute bound in the class body: hash of its statements}.
    # Class attributes are not symbols; evidence mode compares these to find
    # which attribute names a class-body change touched. ``OPAQUE_ATTRIBUTE``
    # hashes every other statement of the body (a loop, a call, a ``del``),
    # ``CLASS_STATEMENT`` the bases, keywords and decorators.
    class_attributes: dict[str, dict[str, str]] = field(default_factory=dict)
    # Class -> the in-scope classes its statement names as bases (resolved).
    # Evidence mode walks class hierarchies with it; static planning does not
    # use it.
    class_bases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Classes whose creation may run code that reads their attributes:
    # decorators, keywords (a metaclass), or a base the index cannot see
    # (``Enum``, a dataclass-like framework base). Evidence mode escalates an
    # attribute change of one rather than trusting readers of the name.
    open_classes: set[str] = field(default_factory=set)
    # Functions and classes whose decorators (or metaclass) may read their
    # docstring: one the analysis cannot see, or in-scope code that reads
    # ``__doc__`` (pandas' ``@doc``). A docstring-only change of one runs at
    # import; of any other symbol it runs nothing.
    doc_decorated: set[str] = field(default_factory=set)

    @property
    def revision(self) -> str:
        return self.snapshot.revision

    @property
    def commit(self) -> str:
        return self.snapshot.commit
