"""Scopes the resolver walks (modules, classes, functions), import bindings,
the nodes a reference resolves to, and relative module names."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from diffcone.indexer.literals import (
    INDEXED,
    NESTED,
    _literal_strings,
    _string_candidates,
    literal_base,
    own_literal_bindings,
)
from diffcone.indexer.uses import UseRecord


@dataclass(frozen=True)
class ImportBinding:
    module: str
    attr: str | None


@dataclass
class ClassScope:
    id: str
    module: ModuleScope
    enclosing: ClassScope | None = None  # the class this one is nested in, if any
    members: dict[str, str] = field(default_factory=dict)  # name -> symbol id
    bindings: set[str] = field(default_factory=set)
    # Each base as a dotted name chain, or None for a non-name expression
    # (``Generic[T]``, ``namedtuple(...)``) that the collector visits instead.
    base_chains: list[list[str] | None] = field(default_factory=list)
    # Each base as written, for deciding whether it is external: the name
    # chain, or the subscripted name of ``Generic[T]``-style bases.
    base_names: list[list[str] | None] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)  # in-scope base class ids, in order
    complete: bool = True  # False when some base is external/dynamic/unresolved
    # No decorators and no class keywords (metaclass=...): nothing but the
    # class body and its bases decides how instances are built.
    plain: bool = True
    # Some base is external, dynamic or unresolved, other than a plain
    # ``object``: attributes may be written by code that is not visible.
    opaque: bool = False
    # Some base is outside the source roots and may call this class's
    # methods (not a builtin or a purely structural typing/abc base).
    external_base: bool = False
    bases_state: int = 0  # 0 pending, 1 resolving, 2 resolved
    mro: list[str] | None = None
    in_mro: bool = False  # cycle guard while linearising


# A top-level ``NAME = <expr>`` or ``NAME: T = <expr>`` that is a variable symbol.
VariableStatement = ast.Assign | ast.AnnAssign


@dataclass
class ModuleScope:
    name: str
    path: str
    is_package: bool
    # None for a module whose facts were served by the module cache; the
    # tree is parsed on demand only when the module must be re-resolved.
    tree: ast.Module | None
    imports: dict[str, ImportBinding] = field(default_factory=dict)
    # Other import bindings of a name ``imports`` holds the last one of
    # (``try: from a import f`` / ``except ImportError: from b import f``,
    # ``if``/``else`` imports): which one is live is not known statically,
    # so a reference to the name depends on every one.
    alt_imports: dict[str, list[ImportBinding]] = field(default_factory=dict)
    star_imports: list[str] = field(default_factory=list)
    bindings: set[str] = field(default_factory=set)
    members: dict[str, str] = field(default_factory=dict)
    import_nodes: list[ast.stmt] = field(default_factory=list)
    # Simple top-level assignments that are symbols of their own: name -> id,
    # and the statement each one came from (excluded from the module body hash).
    variables: dict[str, str] = field(default_factory=dict)
    variable_stmts: dict[str, VariableStatement] = field(default_factory=dict)
    # NAME = "lit" / ("a", "b") at module level: string sets a name may hold.
    literal_names: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
    # How this module uses what it can name in other modules (and through
    # star imports): what may change their literal tables, or write onto an
    # external module (diffcone.indexer.uses).
    uses: tuple[UseRecord, ...] = ()
    # Module-level names bound to a dict, list or set display (or a live view
    # of one): a use elsewhere that is not a read may change them in place.
    containers: frozenset[str] = frozenset()
    # Module-level literal name -> the names its values were taken from.
    literal_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Module cache bookkeeping: the content key of the file and the digest
    # of everything other modules' resolution may read from this one.
    cache_key: str | None = None
    env_digest: str = ""


@dataclass
class Scope:
    module: ModuleScope
    local_imports: dict[str, ImportBinding] = field(default_factory=dict)
    locals: set[str] = field(default_factory=set)
    self_name: str | None = None
    self_class: str | None = None
    # Function-level ``name = "lit"`` / ``for name in ("a", "b")`` bindings
    # (the string values a name may hold, or None when a binding is not
    # literal) are computed lazily: collecting them walks the whole function,
    # and only functions with a dynamic-name call ever ask. ``literal_node`` is
    # the scope's own body to collect from, ``literal_parent`` the enclosing
    # scope whose bindings are inherited minus ``literal_bound``, and
    # ``literal_extra`` explicit bindings (comprehension variables).
    literal_node: ast.AST | None = None
    literal_parent: Scope | None = None
    literal_bound: frozenset[str] = frozenset()
    literal_extra: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
    _literal_cache: dict[str, tuple[str, ...] | None] | None = field(default=None, repr=False)
    # The enclosing function's own parameters (only in that function's scope,
    # not in nested scopes): name -> positional index or None for keyword-only.
    params: dict[str, int | None] = field(default_factory=dict)
    # Parameters whose default is a module-level variable alias that variable:
    # reads and in-place mutations through the parameter belong to it.
    param_aliases: dict[str, str] = field(default_factory=dict)
    # Names the function body rebinds: a rebound parameter no longer holds
    # what the call sites passed. Only in the function's own scope.
    rebound: frozenset[str] = frozenset()
    # The bound method whose own scope this is (not a nested scope's).
    method: str = ""
    # ``self_name`` names the class (``cls`` of a classmethod), not an instance.
    self_is_class: bool = False
    # A class body's own scope: the members it has defined (``value =
    # property(_get)`` names the method) resolve before module names, and
    # scopes nested in it (lambdas, comprehensions, functions) do not see the
    # class's names at all, as Python's scoping rules say.
    class_members: dict[str, str] = field(default_factory=dict)
    class_level: bool = False

    @property
    def literal_names(self) -> dict[str, tuple[str, ...] | None]:
        if self._literal_cache is None:
            names: dict[str, tuple[str, ...] | None] = {}
            if self.literal_parent is not None:
                parent = self.literal_parent.literal_names
                names = {
                    k: v for k, v in parent.items() if literal_base(k) not in self.literal_bound
                }
            # A name this scope binds is not the module's literal of that
            # name, whatever it holds (``def f(NAMES)``, ``lambda NAMES:``,
            # ``for NAMES, _ in ...``): see own_literal_bindings.
            names.update(
                own_literal_bindings(
                    self.literal_node, self.literal_bound, self.module.literal_names
                )
            )
            names.update(self.literal_extra)
            names.update({k + INDEXED: None for k in self.literal_extra})
            names.update({k + NESTED: None for k in self.literal_extra})
            self._literal_cache = names
        return self._literal_cache

    def string_candidates(self, expr: ast.expr) -> tuple[str, ...] | None:
        """Every string ``expr`` may evaluate to, or None when unbounded."""
        return _string_candidates(expr, self.literal_names, self.module.literal_names)


@dataclass(frozen=True)
class Resolved:
    symbol: str
    detail: str = ""
    # Set when the hit was found *after* an external/unknown base in an MRO:
    # the real target may be an override we cannot see, so the name stays
    # bounded (an unresolved attribute record is kept alongside the edge).
    uncertain_attr: str = ""
    # The node is ``self``/``cls`` of the enclosing method: attribute lookups
    # on it dispatch at runtime, so in-scope overrides are recorded too.
    receiver: bool = False
    # Overrides to record alongside the resolved hit (see lookup_in_class):
    # (symbol id, detail) pairs; detail is "" for a method, "attribute:NAME"
    # for a class-attribute rebinding.
    overrides: tuple[tuple[str, str], ...] = ()
    # Modules the name may also denote: ``pkg.retry`` when ``pkg`` binds
    # ``retry`` and has a submodule ``retry`` (see member_symbol_id).
    also: tuple[str, ...] = ()
    # Other symbols or modules the name may be bound to (see
    # ModuleScope.alt_imports): each is a dependency as well.
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModuleNode:
    module: str


@dataclass(frozen=True)
class External:
    module: str


@dataclass(frozen=True)
class Unresolved:
    kind: str
    name: str


@dataclass(frozen=True)
class Local:
    """A function-local (or class-local) binding; its value is unknown."""


Node = Resolved | ModuleNode | External | Unresolved | Local | None


def resolve_relative_module(current: str, is_package: bool, module: str | None, level: int) -> str:
    """Absolute module named by ``from <'.' * level><module> import ...`` as
    written inside ``current`` (a package's ``__init__`` counts as the
    package itself)."""
    if level == 0:
        return module or ""
    parts = current.split(".") if current else []
    if not is_package:
        parts = parts[:-1]
    drop = level - 1
    if drop:
        parts = parts[: len(parts) - drop] if drop <= len(parts) else []
    base = ".".join(parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def relative_import_escapes(current: str, is_package: bool, level: int) -> bool:
    """Whether ``from <'.' * level>... import`` climbs above the top-level
    package of ``current``: at runtime that is an error, or the module has
    another name there than its source root gives it (``tests/__init__.py``
    exists but the root is ``tests``). Either way what it imports is unknown."""
    if level == 0:
        return False
    parts = current.split(".") if current else []
    if not is_package:
        parts = parts[:-1]
    return len(parts) < level


def _absolute_module(scope_module: ModuleScope, module: str | None, level: int) -> str:
    return resolve_relative_module(scope_module.name, scope_module.is_package, module, level)


def _string_prefix(expr: ast.expr) -> str | None:
    """The literal prefix of a string built at runtime: an f-string starting
    with text, ``"pkg." + name``, ``"pkg.%s" % name`` or ``"pkg.{}".format(name)``.
    None when the string does not start with a literal."""
    if isinstance(expr, ast.JoinedStr):
        if expr.values and isinstance(expr.values[0], ast.Constant):
            return str(expr.values[0].value) or None
        return None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _literal_strings(expr.left)
        if left is not None and len(left) == 1:
            return left[0] or None
        return _string_prefix(expr.left)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod):
        if isinstance(expr.left, ast.Constant) and isinstance(expr.left.value, str):
            return expr.left.value.split("%", 1)[0] or None
        return None
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "format"
        and isinstance(expr.func.value, ast.Constant)
        and isinstance(expr.func.value.value, str)
    ):
        return expr.func.value.value.split("{", 1)[0] or None
    return None


def _resolve_relative_name(name: str, package: str, *, prefix: bool = False) -> str | None:
    """``importlib.resolve_name``: ``..x`` against package ``a.b.c`` is
    ``a.b.x``. A prefix keeps its trailing text (``..t.`` -> ``a.b.t.``) and
    ``..`` alone becomes ``a.b.``. None when the dots go above the top."""
    if not name.startswith("."):
        return name
    level = len(name) - len(name.lstrip("."))
    bits = package.rsplit(".", level - 1)
    if not package or len(bits) < level:
        return None
    base, rest = bits[0], name[level:]
    if prefix or rest:
        return f"{base}.{rest}"
    return base
