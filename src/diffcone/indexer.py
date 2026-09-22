"""Source index and dependency resolver.

Parses every Python module of a snapshot, assigns stable symbol identities,
hashes bodies and definitions, and resolves the statically resolvable subset
of references into explicit dependency edges. Everything that cannot be
resolved is recorded as an :class:`UnresolvedReference`, never dropped.

Supported subset (see docs/design.md):

* module-level functions, classes, methods (and nested classes) as symbols;
* bare names and dotted attribute chains rooted at a module-level definition,
  an import alias, ``self``/``cls`` inside a method, or a star import;
* ``import``/``from ... import`` (absolute and relative) within source roots;
* ``importlib.import_module`` / ``getattr`` with literal arguments, with
  parameters whose call sites pass literals, and with instance attributes
  that ``__init__`` binds to such values (``getattr(x, self.name)``);
* attribute lookup on classes through their in-scope MRO (``self.m`` for an
  inherited ``m``, ``Sub.m``, ``super().m``).

Deliberately unsupported: type inference, dynamic dispatch on unknown
receivers, instance attributes written outside ``__init__`` or reflectively,
decorators that rewrite call targets.
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field

from diffcone.model import (
    CLASS,
    DEFINED_IN,
    FUNCTION,
    IMPORTS,
    IMPORTS_NAME,
    METHOD,
    MODULE,
    REFERENCES,
    UNRESOLVED_ATTRIBUTE,
    UNRESOLVED_DYNAMIC,
    UNRESOLVED_NAME,
    VARIABLE,
    AnalysisError,
    Edge,
    ExternalReference,
    SourceIndex,
    Symbol,
    UnresolvedReference,
)
from diffcone.snapshot import (
    Snapshot,
    child_modules,
    member_symbol_id,
    module_name_for,
    split_root,
)

BUILTIN_NAMES = frozenset(dir(builtins))
DYNAMIC_CALLS = frozenset({"eval", "exec", "__import__", "globals", "vars"})
DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


# --------------------------------------------------------------------------- hashing


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class _StripDefs(ast.NodeTransformer):
    """Remove nested definitions (and optionally imports) from a scope body.

    Definitions are hashed as their own symbols and imports as the module's
    definition hash, so leaving them here would double-count changes.
    """

    def __init__(self, strip_imports: bool) -> None:
        self.strip_imports = strip_imports

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.stmt | None:
        return None

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.stmt | None:
        return None

    def visit_Import(self, node: ast.Import) -> ast.stmt | None:
        return None if self.strip_imports else node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.stmt | None:
        return None if self.strip_imports else node


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def hash_nodes(nodes: list[ast.AST]) -> str:
    return _digest("\n".join(_dump(n) for n in nodes))


def _contains_definition_or_import(stmt: ast.stmt, strip_imports: bool) -> bool:
    for node in ast.walk(stmt):
        if isinstance(node, DEF_NODES):
            return True
        if strip_imports and isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
    return False


def _split_docstring(stmts: list[ast.stmt]) -> tuple[str, list[ast.stmt]]:
    """(docstring text, statements without it) for a module/class/function body."""
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        return stmts[0].value.value, stmts[1:]
    return "", stmts


def _docstring_hash(bodies: list[list[ast.stmt]]) -> str:
    docs = [_split_docstring(b)[0] for b in bodies]
    return _digest("\n".join(docs)) if any(docs) else ""


def hash_scope_body(stmts: list[ast.stmt], strip_imports: bool) -> str:
    """Hash a scope's statements with nested definitions (and optionally
    imports) removed. Only statements that actually contain one are copied
    and stripped; the rest are dumped as they are, which avoids a deep copy
    of every statement (the dominant cost of indexing)."""
    kept: list[ast.AST] = []
    for stmt in stmts:
        if isinstance(stmt, DEF_NODES) or (
            strip_imports and isinstance(stmt, (ast.Import, ast.ImportFrom))
        ):
            continue
        if _contains_definition_or_import(stmt, strip_imports):
            stripped = _StripDefs(strip_imports).visit(copy.deepcopy(stmt))
            if stripped is not None:
                kept.append(stripped)
        else:
            kept.append(stmt)
    return hash_nodes(kept)


def iter_scope_statements(body: list[ast.stmt]):
    """Yield statements of a scope, descending into compound statements but
    never into function or class definitions."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, DEF_NODES):
            continue
        for attr in ("body", "orelse", "finalbody"):
            child = getattr(stmt, attr, None)
            if isinstance(child, list):
                yield from iter_scope_statements(child)
        for handler in getattr(stmt, "handlers", []) or []:
            yield from iter_scope_statements(handler.body)
        for case in getattr(stmt, "cases", []) or []:
            yield from iter_scope_statements(case.body)


# --------------------------------------------------------------------------- scopes


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


@dataclass
class ModuleScope:
    name: str
    path: str
    is_package: bool
    # None for a module whose facts were served by the module cache; the
    # tree is parsed on demand only when the module must be re-resolved.
    tree: ast.Module | None
    imports: dict[str, ImportBinding] = field(default_factory=dict)
    star_imports: list[str] = field(default_factory=list)
    bindings: set[str] = field(default_factory=set)
    members: dict[str, str] = field(default_factory=dict)
    import_nodes: list[ast.stmt] = field(default_factory=list)
    # Simple top-level assignments that are symbols of their own: name -> id,
    # and the statement each one came from (excluded from the module body hash).
    variables: dict[str, str] = field(default_factory=dict)
    variable_stmts: dict[str, ast.stmt] = field(default_factory=dict)
    # NAME = "lit" / ("a", "b") at module level: string sets a name may hold.
    literal_names: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
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

    @property
    def literal_names(self) -> dict[str, tuple[str, ...] | None]:
        if self._literal_cache is None:
            names: dict[str, tuple[str, ...] | None] = {}
            if self.literal_parent is not None:
                parent = self.literal_parent.literal_names
                names = {k: v for k, v in parent.items() if k not in self.literal_bound}
            if self.literal_node is not None:
                names.update(
                    _collect_literal_bindings(self.literal_node, self.module.literal_names)
                )
            names.update(self.literal_extra)
            self._literal_cache = names
        return self._literal_cache

    def string_candidates(self, expr: ast.expr) -> tuple[str, ...] | None:
        """Every string ``expr`` may evaluate to, or None when unbounded."""
        return _string_candidates(expr, self.literal_names, self.module.literal_names)


# Resolution results ---------------------------------------------------------


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


def _literal_strings(expr: ast.expr) -> tuple[str, ...] | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return (expr.value,)
    if isinstance(expr, ast.JoinedStr) and all(isinstance(v, ast.Constant) for v in expr.values):
        return ("".join(str(v.value) for v in expr.values),)  # type: ignore[attr-defined]
    if isinstance(expr, ast.Dict):
        # Iterating a dict yields its keys; only all-literal string keys count.
        keys: list[str] = []
        for key in expr.keys:
            if key is None or not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            keys.append(key.value)
        return tuple(keys)
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        out: list[str] = []
        for elt in expr.elts:
            values = _literal_strings(elt)
            if values is None:
                return None
            out.extend(values)
        return tuple(out)
    return None


def _string_candidates(
    expr: ast.expr,
    local_literals: dict[str, tuple[str, ...] | None],
    module_literals: dict[str, tuple[str, ...] | None],
) -> tuple[str, ...] | None:
    direct = _literal_strings(expr)
    if direct is not None:
        return direct
    if isinstance(expr, ast.Name):
        if expr.id in local_literals:
            return local_literals[expr.id]
        return module_literals.get(expr.id)
    # ``D.keys()`` over a literal-keyed dict yields its keys. (``D.items()``
    # yields pairs and is handled only for ``for key, value in`` targets.)
    if _is_dict_method_call(expr, "keys"):
        return _string_candidates(expr.func.value, local_literals, module_literals)  # type: ignore[attr-defined]
    return None


def _is_dict_method_call(expr: ast.expr, method: str) -> bool:
    return (
        isinstance(expr, ast.Call)
        and not expr.args
        and not expr.keywords
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == method
    )


def _collect_literal_bindings(
    node: ast.AST, module_literals: dict[str, tuple[str, ...] | None]
) -> dict[str, tuple[str, ...] | None]:
    """Names bound in ``node``'s scope to string literals, tuples of them, or
    loop variables over such tuples. A name with any other binding maps to
    None (unbounded); nested scopes are not entered."""
    found: dict[str, tuple[str, ...] | None] = {}

    def bind(name: str, values: tuple[str, ...] | None) -> None:
        if name in found and found[name] is not None and values is not None:
            found[name] = tuple(dict.fromkeys(found[name] + values))
        else:
            found[name] = None if (name in found and found[name] is None) else values

    # Source order matters: ``names = {...}`` must be seen before the loop
    # that iterates it, so children are pushed reversed onto the LIFO stack.
    stack: list[ast.AST] = list(reversed(list(ast.iter_child_nodes(node))))
    while stack:
        n = stack.pop()
        if isinstance(n, NESTED_SCOPES):
            continue
        if isinstance(n, ast.Assign):
            values = _string_candidates(n.value, found, module_literals)
            for target in n.targets:
                if isinstance(target, ast.Name):
                    bind(target.id, values)
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            if isinstance(n.target, ast.Name):
                bind(n.target.id, _string_candidates(n.value, found, module_literals))
        elif isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.target, ast.Name):
            bind(n.target.id, _string_candidates(n.iter, found, module_literals))
        elif isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.target, ast.Tuple):
            # ``for key, value in D.items()``: the key is bounded, the value is not.
            elts = n.target.elts
            keys = None
            if _is_dict_method_call(n.iter, "items") and len(elts) == 2:
                keys = _string_candidates(n.iter.func.value, found, module_literals)  # type: ignore[attr-defined]
            for i, elt in enumerate(elts):
                if isinstance(elt, ast.Name):
                    bind(elt.id, keys if i == 0 else None)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            if n.id not in found:
                found[n.id] = None
        stack.extend(reversed(list(ast.iter_child_nodes(n))))
    return found


COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
NESTED_SCOPES = DEF_NODES + COMPREHENSIONS + (ast.Lambda,)


def _collect_store_names(stmt: ast.AST) -> set[str]:
    """Names bound by a statement in *its own* scope (nested scopes excluded)."""
    names: set[str] = set()
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        if isinstance(node, NESTED_SCOPES) and node is not stmt:
            if isinstance(node, DEF_NODES):
                names.add(node.name)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return names


class _LocalBindings(ast.NodeVisitor):
    """Collect the names a function, lambda or class body binds in its own
    scope. Nested functions, lambdas and comprehensions get their own scope;
    only their names (for defs) are bound here. ``global`` names are excluded.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.globals: set[str] = set()

    def collect(self, node: ast.AST) -> set[str]:
        if isinstance(node, FUNC_NODES + (ast.Lambda,)):
            self.visit(node.args)
        body = getattr(node, "body", [])
        for stmt in body if isinstance(body, list) else [body]:
            self.visit(stmt)
        return self.names - self.globals

    def visit_arg(self, node: ast.arg) -> None:
        self.names.add(node.arg)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def _visit_comprehension(self, node: ast.AST) -> None:
        return

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.names.add(node.name)

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)


# --------------------------------------------------------------------------- indexer


@dataclass
class _FuncParams:
    positional: list[str]  # including self/cls for bound methods
    bound: bool
    defaults: dict[str, tuple[str, ...] | None]
    has_varargs: bool


@dataclass
class _CallSite:
    positional: list[tuple[str, ...] | None]  # candidates per positional argument
    keywords: dict[str, tuple[str, ...] | None]
    unbounded: bool  # *args / **kwargs at the call site
    receiver_bound: bool  # ``obj.m(...)`` / ``self.m(...)``: self is implicit

    def value_for(self, param: str, info: _FuncParams) -> tuple[str, ...] | None:
        if self.unbounded:
            return None
        if param in self.keywords:
            return self.keywords[param]
        if param in info.positional:
            index = info.positional.index(param)
            if info.bound and self.receiver_bound:
                index -= 1
            if 0 <= index < len(self.positional):
                return self.positional[index]
        return info.defaults.get(param)


@dataclass
class _ParamDynamic:
    function: str
    param: str  # a parameter name, or an instance attribute name with self_class
    kind: str  # "getattr" | "import"
    base: list[str] | None  # receiver chain for getattr, when it is a name chain
    scope: Scope
    detail: str
    self_class: str = ""  # set when the name is ``self.<param>`` of this class


@dataclass
class _AttrWrite:
    """A write of ``self.<attr>`` in a method of ``cls``. ``binding`` is what
    an ``__init__`` assignment binds (``["param", name]``, ``["strings",
    [...]]``, ``["symbol", id]``); None for any other write."""

    cls: str
    attr: str
    method: str
    binding: list | None


@dataclass
class _AttrRef:
    """``self.<attr>[.rest]`` read in ``source`` where no class in the MRO
    defines ``attr``: resolved to the bound symbols when there are some."""

    source: str
    cls: str
    attr: str
    rest: list[str]
    chain: str


@dataclass
class _Output:
    """Everything a resolution pass writes. Pass 2 runs per module against a
    fresh instance so a module's contribution can be cached and merged; the
    rest of the indexer writes to the global one backed by the index."""

    edges: set[Edge] = field(default_factory=set)
    unresolved: set[UnresolvedReference] = field(default_factory=set)
    external: set[ExternalReference] = field(default_factory=set)
    call_sites: dict[str, list[_CallSite]] = field(default_factory=lambda: defaultdict(list))
    escapes: set[str] = field(default_factory=set)
    func_params: dict[str, _FuncParams] = field(default_factory=dict)
    param_dynamics: list[_ParamDynamic] = field(default_factory=list)
    attr_writes: list[_AttrWrite] = field(default_factory=list)
    # (class id, attribute) pairs whose value cannot be bounded; the class is
    # "" for a write through a receiver of unknown type, the attribute "*"
    # for every attribute.
    attr_unbound: set[tuple[str, str]] = field(default_factory=set)
    attr_refs: list[_AttrRef] = field(default_factory=list)

    def merge(self, other: _Output) -> None:
        self.edges |= other.edges
        self.unresolved |= other.unresolved
        self.external |= other.external
        for function, sites in other.call_sites.items():
            self.call_sites[function].extend(sites)
        self.escapes |= other.escapes
        self.func_params.update(other.func_params)
        self.param_dynamics.extend(other.param_dynamics)
        self.attr_writes.extend(other.attr_writes)
        self.attr_unbound |= other.attr_unbound
        self.attr_refs.extend(other.attr_refs)


def _tuples(value: list | None) -> tuple[str, ...] | None:
    return None if value is None else tuple(value)


def _scope_to_dict(scope: Scope) -> dict:
    """The part of a function scope that deferred parameter-dynamic expansion
    resolves names against (module, imports, locals, self, aliases)."""
    return {
        "module": scope.module.name,
        "local_imports": {k: [b.module, b.attr] for k, b in scope.local_imports.items()},
        "locals": sorted(scope.locals),
        "self_name": scope.self_name,
        "self_class": scope.self_class,
        "param_aliases": scope.param_aliases,
    }


def _scope_from_dict(data: dict, scopes: dict[str, ModuleScope]) -> Scope:
    return Scope(
        module=scopes[data["module"]],
        local_imports={k: ImportBinding(m, a) for k, (m, a) in data["local_imports"].items()},
        locals=set(data["locals"]),
        self_name=data["self_name"],
        self_class=data["self_class"],
        param_aliases=dict(data["param_aliases"]),
    )


def _output_to_dict(out: _Output) -> dict:
    return {
        "edges": [[e.source, e.target, e.kind, e.detail] for e in sorted(out.edges)],
        "unresolved": [[u.symbol, u.kind, u.name, u.detail] for u in sorted(out.unresolved)],
        "external": [[x.symbol, x.module] for x in sorted(out.external)],
        "call_sites": {
            f: [[s.positional, s.keywords, s.unbounded, s.receiver_bound] for s in sites]
            for f, sites in out.call_sites.items()
        },
        "escapes": sorted(out.escapes),
        "func_params": {
            f: [p.positional, p.bound, p.defaults, p.has_varargs]
            for f, p in out.func_params.items()
        },
        "param_dynamics": [
            [
                pd.function,
                pd.param,
                pd.kind,
                pd.base,
                _scope_to_dict(pd.scope),
                pd.detail,
                pd.self_class,
            ]
            for pd in out.param_dynamics
        ],
        "attr_writes": [[w.cls, w.attr, w.method, w.binding] for w in out.attr_writes],
        "attr_unbound": sorted(out.attr_unbound),
        "attr_refs": [[r.source, r.cls, r.attr, r.rest, r.chain] for r in out.attr_refs],
    }


def _output_from_dict(data: dict, scopes: dict[str, ModuleScope]) -> _Output:
    out = _Output()
    out.edges = {Edge(*e) for e in data["edges"]}
    out.unresolved = {UnresolvedReference(*u) for u in data["unresolved"]}
    out.external = {ExternalReference(*x) for x in data["external"]}
    for f, sites in data["call_sites"].items():
        out.call_sites[f] = [
            _CallSite(
                positional=[_tuples(v) for v in positional],
                keywords={k: _tuples(v) for k, v in keywords.items()},
                unbounded=unbounded,
                receiver_bound=receiver_bound,
            )
            for positional, keywords, unbounded, receiver_bound in sites
        ]
    out.escapes = set(data["escapes"])
    out.func_params = {
        f: _FuncParams(
            positional=list(positional),
            bound=bound,
            defaults={k: _tuples(v) for k, v in defaults.items()},
            has_varargs=has_varargs,
        )
        for f, (positional, bound, defaults, has_varargs) in data["func_params"].items()
    }
    out.param_dynamics = [
        _ParamDynamic(
            function, param, kind, base, _scope_from_dict(scope, scopes), detail, self_class
        )
        for function, param, kind, base, scope, detail, self_class in data["param_dynamics"]
    ]
    out.attr_writes = [_AttrWrite(*w) for w in data["attr_writes"]]
    out.attr_unbound = {(c, a) for c, a in data["attr_unbound"]}
    out.attr_refs = [_AttrRef(*r) for r in data["attr_refs"]]
    return out


def _facts_to_dict(
    scope: ModuleScope, symbols: list[Symbol], classes: list[ClassScope], edges: set[Edge]
) -> dict:
    """A module's first-pass output: a pure function of its file (given its
    module name), so it is cached by content. ``env`` digests the part other
    modules' resolution can observe (names, kinds, class members); it feeds
    the environment fingerprint that keys second-pass outputs."""
    record = {
        "imports": {k: [b.module, b.attr] for k, b in scope.imports.items()},
        "star_imports": list(scope.star_imports),
        "bindings": sorted(scope.bindings),
        "members": dict(scope.members),
        "variables": dict(scope.variables),
        "literal_names": {
            k: (list(v) if v is not None else None) for k, v in scope.literal_names.items()
        },
        "symbols": [dict(vars(s)) for s in symbols],  # flat and frozen: no deep copy needed
        "classes": [
            {
                "id": c.id,
                "enclosing": c.enclosing.id if c.enclosing is not None else None,
                "members": dict(c.members),
                "bindings": sorted(c.bindings),
                "base_chains": c.base_chains,
                "base_names": c.base_names,
                "plain": c.plain,
            }
            for c in classes
        ],
        "edges": [[e.source, e.target, e.kind, e.detail] for e in sorted(edges)],
    }
    env = [
        scope.name,
        record["imports"],
        record["star_imports"],
        record["bindings"],
        record["members"],
        record["variables"],
        [[s.id, s.kind, s.module] for s in symbols],
        [[c["id"], c["enclosing"], c["members"], c["bindings"]] for c in record["classes"]],
    ]
    record["env"] = _digest(json.dumps(env, sort_keys=True))
    return record


class Indexer:
    def __init__(self, snapshot: Snapshot, module_cache=None) -> None:
        self.snapshot = snapshot
        self.index = SourceIndex(snapshot=snapshot.info)
        self.index.errors.extend(snapshot.errors)
        # Optional per-module cache of first-pass facts and second-pass
        # outputs (diffcone.cache.ModuleCache). Applies to every snapshot kind.
        self.module_cache = module_cache
        self.scopes: dict[str, ModuleScope] = {}
        self.class_scopes: dict[str, ClassScope] = {}
        self._module_prefixes: set[str] = set()
        self._bases_final = False
        # Where writes go: the global output is backed by the index; pass 2
        # swaps in a per-module output so it can be cached (see _Output).
        self._global = _Output(
            edges=self.index.edges, unresolved=self.index.unresolved, external=self.index.external
        )
        self.out = self._global
        # Symbols and class scopes added by the module being indexed, and
        # whether one of its symbols collided with an earlier module's.
        self._added_symbols: list[Symbol] = []
        self._added_classes: list[ClassScope] = []
        self._collided = False
        # Transitive in-scope descendants per class, built once bases are final.
        self._descendants: dict[str, tuple[str, ...]] = {}
        # Classes with an unresolved ``super().<name>``, per name (final pass).
        self._super_misses: dict[str, set[str]] = defaultdict(set)

    # -- pass 1 ---------------------------------------------------------------

    def build(self) -> SourceIndex:
        cache = self.module_cache
        facts: dict[str, dict | None] = {}
        candidates = [
            (path, module)
            for path in sorted(self.snapshot.files)
            if (module := module_name_for(path, self.snapshot.source_roots)) is not None
        ]
        # Submodule names per package: a top-level binding of that name gets
        # a distinct identity (member_symbol_id).
        self._children = child_modules(self.snapshot)
        keys: dict[str, str] = {}  # path -> cache key; every record is loaded in one query
        if cache is not None:
            keys = {p: cache.key(m, p, self.snapshot.files[p]) for p, m in candidates}
        loaded = cache.load_facts(list(keys.values())) if cache is not None else {}
        for path, module in candidates:
            if module in self.scopes:
                other = self.scopes[module].path
                self._error(path, f"module {module!r} is also defined by {other}")
                continue
            record = loaded.get(keys[path]) if path in keys else None
            if record is None:
                tree = self._parse(path, module)
                if tree is None:
                    continue
            else:
                tree = None
            scope = ModuleScope(
                name=module, path=path, is_package=path.endswith("__init__.py"), tree=tree
            )
            scope.cache_key = keys.get(path)
            self.scopes[module] = scope
            self.index.modules.add(module)
            facts[module] = record
        new_facts: dict[str, dict] = {}
        new_resolved: dict[str, dict] = {}
        for module in self.index.modules:
            parts = module.split(".")
            for i in range(1, len(parts) + 1):
                self._module_prefixes.add(".".join(parts[:i]))
        for module in sorted(self.scopes):
            scope = self.scopes[module]
            record = facts[module]
            if record is not None:
                # A record whose symbols collide with an earlier module's (an
                # error either way) is not applied, so the errors come out
                # exactly as without a cache; a malformed record is a miss.
                try:
                    applied = self._apply_facts(scope, record)
                except (KeyError, TypeError, ValueError, AttributeError):
                    applied = False
                if applied:
                    continue
                scope.tree = self._parse(scope.path, module)
                if scope.tree is None:  # pragma: no cover - same content parsed before
                    del self.scopes[module]
                    self.index.modules.discard(module)
                    continue
            self._added_symbols, self._added_classes, self._collided = [], [], False
            self.out = _Output()
            try:
                self._index_module(scope)
            finally:
                captured, self.out = self.out, self._global
            self._global.merge(captured)
            if cache is not None:
                record = _facts_to_dict(
                    scope, self._added_symbols, self._added_classes, captured.edges
                )
                scope.env_digest = record["env"]
                if not self._collided and scope.cache_key is not None:
                    new_facts[scope.cache_key] = record
        for class_id in sorted(self.class_scopes):
            self._ensure_bases(class_id)
        self._bases_final = True  # MROs may be memoised from here on
        self._build_descendants()
        self._external_callers()
        fingerprint = self._environment_fingerprint() if cache is not None else ""
        loaded = cache.load_resolved(list(keys.values()), fingerprint) if cache else {}
        for module in sorted(self.scopes):
            scope = self.scopes[module]
            key = scope.cache_key
            out: _Output | None = None
            if key is not None and key in loaded:
                try:
                    out = _output_from_dict(loaded[key], self.scopes)
                except (KeyError, TypeError, ValueError, AttributeError):
                    out = None  # malformed record: resolve as on a miss
            if out is None:
                if scope.tree is None:
                    self._load_tree(scope)
                self.out = _Output()
                try:
                    self._resolve_module(scope)
                finally:
                    out, self.out = self.out, self._global
                if key is not None:
                    new_resolved[key] = _output_to_dict(out)
            self._global.merge(out)
        self._resolve_param_dynamics()
        if cache is not None and (new_facts or new_resolved):
            cache.store(new_facts, new_resolved, fingerprint, list(keys.values()))
        return self.index

    def _parse(self, path: str, module: str) -> ast.Module | None:
        try:
            source = self.snapshot.files[path].decode("utf-8")
            return ast.parse(source, filename=path)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            self._error(path, f"cannot parse: {exc}")
            self.index.failed_modules.add(module)
            return None

    def _load_tree(self, scope: ModuleScope) -> None:
        """Parse a module whose facts came from the cache but which must be
        resolved again (its file or the environment changed)."""
        tree = self._parse(scope.path, scope.name)
        if tree is None:  # pragma: no cover - the same content parsed before
            tree = ast.Module(body=[], type_ignores=[])
        scope.tree = tree
        _, _, variable_stmts = self._module_statements(scope, register_imports=False)
        scope.variable_stmts = {n: s for n, s in variable_stmts.items() if n in scope.variables}

    def _apply_facts(self, scope: ModuleScope, record: dict) -> bool:
        """Install a cached facts record. Everything is built before anything
        is stored, so a malformed record (which raises) or one whose symbols
        collide with an earlier module's (returns False) leaves no trace."""
        imports = {k: ImportBinding(m, a) for k, (m, a) in record["imports"].items()}
        star_imports = list(record["star_imports"])
        bindings = set(record["bindings"])
        members = dict(record["members"])
        variables = dict(record["variables"])
        literal_names = {k: _tuples(v) for k, v in record["literal_names"].items()}
        env_digest = str(record["env"])
        symbols: list[Symbol] = []
        for data in record["symbols"]:
            data = dict(data)
            data["line_ranges"] = tuple(tuple(r) for r in data["line_ranges"])
            data["imports"] = tuple(data["imports"])
            symbols.append(Symbol(**data))
        classes: dict[str, ClassScope] = {}
        for data in record["classes"]:
            enclosing = classes[data["enclosing"]] if data["enclosing"] else None
            classes[data["id"]] = ClassScope(
                id=str(data["id"]),
                module=scope,
                enclosing=enclosing,
                members=dict(data["members"]),
                bindings=set(data["bindings"]),
                base_chains=[list(c) if c is not None else None for c in data["base_chains"]],
                base_names=[list(c) if c is not None else None for c in data["base_names"]],
                plain=bool(data["plain"]),
            )
        edges = {Edge(*e) for e in record["edges"]}
        if any(s.id in self.index.symbols for s in symbols):
            return False
        # Top-level identities depend on which submodules exist: a record
        # made before a shadowed submodule was added (or removed) is stale.
        for name, symbol_id in [*members.items(), *variables.items()]:
            if symbol_id != self._member_id(scope.name, name):
                return False
        scope.imports, scope.star_imports, scope.bindings = imports, star_imports, bindings
        scope.members, scope.variables, scope.literal_names = members, variables, literal_names
        scope.env_digest = env_digest
        for symbol in symbols:
            self._add_symbol(symbol)
        self.class_scopes.update(classes)
        self.out.edges |= edges
        return True

    def _environment_fingerprint(self) -> str:
        """Digest of everything a module's resolution reads from other
        modules: their observable facts plus every class's resolved bases,
        and the root specs (whether ``__name__`` is a module's runtime name)."""
        material = [
            sorted(self.snapshot.source_roots),
            [[m, self.scopes[m].env_digest] for m in sorted(self.scopes)],
            [[c, cs.bases, cs.complete] for c, cs in sorted(self.class_scopes.items())],
        ]
        return _digest(json.dumps(material))

    def _resolve_param_dynamics(self) -> None:
        """Expand ``getattr(x, p)`` / ``import_module(p)`` where ``p`` is a
        parameter or an instance attribute, using the literal strings every
        resolved call site passes (for an attribute: what ``__init__`` binds
        it to, see _attribute_writes). A function that escapes (used as a
        value, or whose name occurs as an unresolved reference so callers may
        be unknown) or has an unbounded call site stays dynamic.

        Expanding a ``getattr`` can itself make a function escape (its value
        is used), which can unbound another expansion, so the candidates are
        recomputed until the escape set is stable."""
        unresolved_names = {
            u.name for u in self.index.unresolved if u.name and not u.detail.startswith("super().")
        }
        # ``super().m`` that did not resolve in class K can still only reach
        # an ``m`` after K in the MRO of K or of a subclass of K.
        self._super_misses.clear()
        for u in self.index.unresolved:
            if u.name and u.detail.startswith("super()."):
                source = self.index.symbols.get(u.symbol)
                while source is not None and source.kind != CLASS:
                    source = self.index.symbols.get(source.container or "")
                if source is None:
                    unresolved_names.add(u.name)
                else:
                    self._super_misses[u.name].add(source.id)
        writes: dict[tuple[str, str], list[_AttrWrite]] = defaultdict(list)
        for w in self.out.attr_writes:
            writes[(w.cls, w.attr)].append(w)
        while True:
            escapes = set(self.out.escapes)
            planned: list[tuple[_ParamDynamic, list[str] | None]] = []
            for pd in self.out.param_dynamics:
                if pd.self_class:
                    values = self._attribute_strings(
                        pd.self_class, pd.param, writes, unresolved_names
                    )
                else:
                    values = self._param_values(pd.function, pd.param, unresolved_names)
                planned.append((pd, values))
            for pd, values in planned:
                if values is not None and pd.kind == "getattr" and pd.base is not None:
                    for name in dict.fromkeys(values):
                        node = self.resolve_chain(pd.base + [name], pd.scope)
                        if isinstance(node, Resolved) and not node.detail:
                            self.escape(node)
            if self.out.escapes == escapes:
                break
        for pd, values in planned:
            if values is None:
                self.out.unresolved.add(
                    UnresolvedReference(pd.function, UNRESOLVED_DYNAMIC, "", pd.detail)
                )
                continue
            for name in dict.fromkeys(values):
                if pd.kind == "import":
                    if name.startswith("."):
                        self.out.unresolved.add(
                            UnresolvedReference(pd.function, UNRESOLVED_DYNAMIC, "", pd.detail)
                        )
                    else:
                        self._module_import_edge(pd.function, name)
                elif pd.base is None:
                    self.out.unresolved.add(
                        UnresolvedReference(
                            pd.function, UNRESOLVED_ATTRIBUTE, name, f"getattr(..., {name!r})"
                        )
                    )
                else:
                    node = self.resolve_chain(pd.base + [name], pd.scope)
                    self._record(pd.function, node, chain=".".join(pd.base + [name]))
        for ref in self.out.attr_refs:
            bound = self._attribute_writes(ref.cls, ref.attr, writes)
            if bound is None or any(w.binding[0] != "symbol" for w in bound):  # type: ignore[index]
                continue
            for w in bound:
                node: Node = Resolved(w.binding[1])  # type: ignore[index]
                for attr in ref.rest:
                    node = self._step(node, attr)
                if isinstance(node, Resolved) and not ref.rest and node.symbol != ref.source:
                    self.out.edges.add(
                        Edge(ref.source, node.symbol, REFERENCES, f"self.{ref.attr}")
                    )
                else:
                    self._record(ref.source, node, chain=ref.chain)

    def _param_values(
        self, function: str, param: str, unresolved_names: set[str]
    ) -> list[str] | None:
        """The literal strings every call site of ``function`` passes for
        ``param``, or None when some caller may be unseen or unbounded."""
        info = self.out.func_params.get(function)
        symbol = self.index.symbols.get(function)
        sites = self.out.call_sites.get(function, [])
        if (
            info is None
            or symbol is None
            or function in self.out.escapes
            or symbol.name in unresolved_names
            or self._super_may_reach(symbol)
            or not sites
            or self._constructor_escapes(symbol, unresolved_names)
        ):
            return None
        values: list[str] = []
        for site in sites:
            found = site.value_for(param, info)
            if found is None:
                return None
            values.extend(found)
        return values

    def lookup_super(self, class_id: str, attr: str) -> Node:
        """``super().<attr>`` in ``class_id``: the next definition after it in
        its MRO, plus (as overrides) what follows it in the MRO of each
        in-scope subclass, where a mixin may come first."""
        hit = self.lookup_in_class(class_id, attr, skip_self=True)
        base = (hit.symbol, hit.detail) if isinstance(hit, Resolved) else None
        extra: list[tuple[str, str]] = []
        for sub in self._descendants.get(class_id, ()):
            mro = self._mro(sub)
            if class_id not in mro:
                continue
            for cid in mro[mro.index(class_id) + 1 :]:
                cscope = self.class_scopes[cid]
                if attr in cscope.members:
                    pair = (cscope.members[attr], "")
                elif attr in cscope.bindings:
                    pair = (cid, f"attribute:{attr}")
                else:
                    continue
                if pair != base and pair not in extra:
                    extra.append(pair)
                break
        if not extra or not isinstance(hit, Resolved):
            return hit
        return Resolved(hit.symbol, hit.detail, hit.uncertain_attr, overrides=tuple(extra))

    def _super_may_reach(self, symbol: Symbol) -> bool:
        """An unresolved ``super().<name>`` in class K may call ``symbol``
        when its class follows K in some in-scope MRO."""
        owner = symbol.container
        for k in self._super_misses.get(symbol.name, ()):
            for cid in (k, *self._descendants.get(k, ())):
                mro = self._mro(cid)
                if k in mro and owner in mro[mro.index(k) + 1 :]:
                    return True
        return False

    def _constructor_escapes(self, symbol: Symbol, unresolved_names: set[str]) -> bool:
        """An ``__init__`` also runs whenever a class that inherits it is
        constructed: that happens unseen when such a class escapes or its name
        occurs as an unresolved reference."""
        if symbol.kind != METHOD or symbol.name != "__init__":
            return False
        owner = symbol.container
        if owner not in self.class_scopes:
            return True
        for cid in (owner, *self._descendants.get(owner, ())):
            init = self.lookup_in_class(cid, "__init__")
            if not (isinstance(init, Resolved) and init.symbol == symbol.id):
                continue
            if cid in self.out.escapes or self.index.symbols[cid].name in unresolved_names:
                return True
        return False

    def _attribute_writes(
        self, class_id: str, attr: str, writes: dict[tuple[str, str], list[_AttrWrite]]
    ) -> list[_AttrWrite] | None:
        """What ``self.<attr>`` may hold in a method of ``class_id``: its
        bound writes, or None when it cannot be bounded. The
        instance may belong to any in-scope subclass, so every class in the
        MRO of the class or of a subclass counts; each must be plain and
        fully in scope, none may define the attribute at class level or
        customise attribute access, and every write must be a bounded
        ``__init__`` assignment."""
        unbound = self.out.attr_unbound
        if ("", "*") in unbound or ("", attr) in unbound:
            return None
        family: set[str] = set()
        for cid in (class_id, *self._descendants.get(class_id, ())):
            family.update(self._mro(cid))
        bound: list[_AttrWrite] = []
        for cid in sorted(family):
            cscope = self.class_scopes[cid]
            if not cscope.plain or cscope.opaque or (cid, "*") in unbound or (cid, attr) in unbound:
                return None
            if attr in cscope.members or attr in cscope.bindings:
                return None
            if _ATTRIBUTE_HOOKS & cscope.members.keys():
                return None
            for w in writes.get((cid, attr), ()):
                if w.binding is None:
                    return None
                bound.append(w)
        return bound or None

    def _attribute_strings(
        self,
        class_id: str,
        attr: str,
        writes: dict[tuple[str, str], list[_AttrWrite]],
        unresolved_names: set[str],
    ) -> list[str] | None:
        bound = self._attribute_writes(class_id, attr, writes)
        if bound is None:
            return None
        values: list[str] = []
        for w in bound:
            kind, value = w.binding  # type: ignore[misc]
            if kind == "strings":
                values.extend(value)
            elif kind == "param":
                found = self._param_values(w.method, value, unresolved_names)
                if found is None:
                    return None
                values.extend(found)
            else:
                return None
        return values

    def _error(self, path: str, message: str) -> None:
        self.index.errors.append(
            AnalysisError(revision=self.snapshot.revision, path=path, message=message)
        )

    def _add_symbol(self, symbol: Symbol) -> bool:
        existing = self.index.symbols.get(symbol.id)
        if existing is not None:
            self._error(
                symbol.path,
                f"symbol identity {symbol.id!r} collides with {existing.kind} in {existing.path}",
            )
            self._collided = True
            return False
        self.index.symbols[symbol.id] = symbol
        self._added_symbols.append(symbol)
        return True

    def _module_statements(
        self, scope: ModuleScope, *, register_imports: bool
    ) -> tuple[list[ast.stmt], list[ast.stmt], dict[str, ast.stmt]]:
        """(all scope statements, body without docstring, variable statements)
        of a parsed module; records the import statements and, unless the
        import table was served by the cache, fills it."""
        assert scope.tree is not None
        stmts = list(iter_scope_statements(scope.tree.body))
        scope.import_nodes = [s for s in stmts if isinstance(s, (ast.Import, ast.ImportFrom))]
        if register_imports:
            for node in scope.import_nodes:
                self._register_imports(scope, node, scope.imports, scope.star_imports)
        body = _split_docstring(scope.tree.body)[1]
        return stmts, body, _variable_statements(scope, body)

    def _index_module(self, scope: ModuleScope) -> None:
        assert scope.tree is not None
        stmts, body, variable_stmts = self._module_statements(scope, register_imports=True)
        for stmt in stmts:
            if not isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
                scope.bindings |= _collect_store_names(stmt)
        scope.literal_names = _collect_literal_bindings(scope.tree, {})
        imports = tuple(sorted(_canonical_imports(scope)))
        module_body_hash = hash_scope_body(
            [s for s in body if s not in variable_stmts.values()], strip_imports=True
        )
        module_doc_hash = _docstring_hash([scope.tree.body])
        self._add_symbol(
            Symbol(
                id=scope.name,
                kind=MODULE,
                module=scope.name,
                name=scope.name.rsplit(".", 1)[-1],
                path=scope.path,
                lineno=1,
                body_hash=module_body_hash,
                docstring_hash=module_doc_hash,
                definition_hash=_digest("\n".join(imports)),
                container=None,
                line_ranges=((1, _end_line(scope.tree)),),
                imports=imports,
            )
        )
        self._index_definitions(scope, scope.tree.body, scope.name, scope.members, None)
        # Module-level statements that mention a variable may mutate it in place
        # (``REGISTRY[k] = v``, ``NAMES.append(x)``, ``CONFIG.update(...)``), so
        # they are part of that variable's body, not only of the module's.
        mutators: dict[str, list[ast.stmt]] = defaultdict(list)
        variable_ids = {id(s) for s in variable_stmts.values()}
        for stmt in body:
            if isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
                continue
            if id(stmt) in variable_ids:
                continue
            mentioned = {
                n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and n.id in variable_stmts
            }
            for name in mentioned:
                mutators[name].append(stmt)
        for name, stmt in variable_stmts.items():
            if name in scope.members:
                continue  # also a def/class: Python's last binding wins; stay conservative
            symbol_id = self._member_id(scope.name, name)
            value = stmt.value  # type: ignore[attr-defined]
            symbol = Symbol(
                id=symbol_id,
                kind=VARIABLE,
                module=scope.name,
                name=name,
                path=scope.path,
                lineno=stmt.lineno,
                body_hash=hash_nodes([value, *mutators.get(name, [])]),
                definition_hash="",
                container=scope.name,
                line_ranges=tuple(
                    (s.lineno, _end_line(s)) for s in (stmt, *mutators.get(name, []))
                ),
            )
            if self._add_symbol(symbol):
                scope.variables[name] = symbol_id
                scope.variable_stmts[name] = stmt
                self.out.edges.add(Edge(symbol_id, scope.name, DEFINED_IN))

    def _register_imports(
        self,
        scope: ModuleScope,
        node: ast.stmt,
        table: dict[str, ImportBinding],
        stars: list[str],
    ) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = ImportBinding(alias.name, None)
                else:
                    table[alias.name.split(".")[0]] = ImportBinding(alias.name.split(".")[0], None)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(scope, node.module, node.level)
            for alias in node.names:
                if alias.name == "*":
                    stars.append(base)
                else:
                    table[alias.asname or alias.name] = ImportBinding(base, alias.name)

    def _index_definitions(
        self,
        scope: ModuleScope,
        body: list[ast.stmt],
        container_id: str,
        members: dict[str, str],
        class_scope: ClassScope | None,
    ) -> None:
        funcs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        classes: dict[str, list[ast.ClassDef]] = {}
        order: list[str] = []
        for stmt in iter_scope_statements(body):
            if isinstance(stmt, FUNC_NODES):
                funcs.setdefault(stmt.name, []).append(stmt)
            elif isinstance(stmt, ast.ClassDef):
                classes.setdefault(stmt.name, []).append(stmt)
            else:
                continue
            if stmt.name not in order:
                order.append(stmt.name)
        for name in order:
            if class_scope is None and container_id == scope.name:
                symbol_id = self._member_id(scope.name, name)
            else:
                symbol_id = f"{container_id}.{name}"
            if name in classes:
                nodes = classes[name]
                first = nodes[0]
                member_names = sorted(
                    {
                        s.name
                        for n in nodes
                        for s in iter_scope_statements(n.body)
                        if isinstance(s, DEF_NODES)
                    }
                )

                def class_hashes(nodes=nodes, member_names=member_names):
                    definition_parts: list[ast.AST] = []
                    for n in nodes:
                        definition_parts += (
                            list(n.bases) + list(n.keywords) + list(n.decorator_list)
                        )
                    return (
                        _digest(
                            "\n".join(
                                hash_scope_body(_split_docstring(n.body)[1], strip_imports=False)
                                for n in nodes
                            )
                        ),
                        _digest(hash_nodes(definition_parts) + "|" + ",".join(member_names)),
                        _docstring_hash([n.body for n in nodes]),
                    )

                body_hash, definition_hash, doc_hash = class_hashes()
                symbol = Symbol(
                    id=symbol_id,
                    kind=CLASS,
                    module=scope.name,
                    name=name,
                    path=scope.path,
                    lineno=first.lineno,
                    body_hash=body_hash,
                    definition_hash=definition_hash,
                    container=container_id,
                    line_ranges=tuple((_start_line(n), _end_line(n)) for n in nodes),
                    docstring_hash=doc_hash,
                )
                if not self._add_symbol(symbol):
                    continue
                members[name] = symbol_id
                self.out.edges.add(Edge(symbol_id, container_id, DEFINED_IN))
                cscope = ClassScope(id=symbol_id, module=scope, enclosing=class_scope)
                cscope.plain = len(nodes) == 1 and not (first.decorator_list or first.keywords)
                for n in nodes:
                    cscope.base_chains.extend(_flatten_chain(b) for b in n.bases)
                    cscope.base_names.extend(
                        _flatten_chain(b.value if isinstance(b, ast.Subscript) else b)
                        for b in n.bases
                    )
                self.class_scopes[symbol_id] = cscope
                self._added_classes.append(cscope)
                for n in nodes:
                    for stmt in iter_scope_statements(n.body):
                        if not isinstance(stmt, DEF_NODES):
                            cscope.bindings |= _collect_store_names(stmt)
                # Every definition of the class (``if``/``else`` variants) is
                # one symbol, so its members are indexed together: a method
                # defined in several of them is one symbol too.
                bodies = [stmt for n in nodes for stmt in n.body]
                self._index_definitions(scope, bodies, symbol_id, cscope.members, cscope)
                # Special methods run implicitly on instances (``==``, ``len()``,
                # calling one, ``with``): whatever references the class may
                # trigger them, so the class depends on them.
                for member, member_id in sorted(cscope.members.items()):
                    member_symbol = self.index.symbols.get(member_id)
                    if (
                        _is_special_method(member)
                        and member_symbol is not None
                        and member_symbol.kind == METHOD
                    ):
                        self.out.edges.add(Edge(symbol_id, member_id, REFERENCES, "special_method"))
            else:
                nodes = funcs[name]
                first = nodes[0]

                def function_hashes(nodes=nodes):
                    definition_parts: list[ast.AST] = []
                    annotation_parts: list[ast.AST] = []
                    for n in nodes:
                        args, annotations = _split_annotations(n.args)
                        definition_parts.append(args)
                        definition_parts += list(n.decorator_list)
                        annotation_parts += annotations
                        if n.returns is not None:
                            annotation_parts.append(n.returns)
                    return (
                        _digest(hash_nodes(annotation_parts)),
                        _digest(
                            "\n".join(hash_nodes(list(_split_docstring(n.body)[1])) for n in nodes)
                        ),
                        _digest(
                            hash_nodes(definition_parts)
                            + "|"
                            + ",".join(type(n).__name__ for n in nodes)
                        ),
                        _docstring_hash([n.body for n in nodes]),
                    )

                annotation_hash, body_hash, definition_hash, doc_hash = function_hashes()
                deferred = (
                    _future_annotations(scope)
                    and all(_is_inert_decorator(d) for n in nodes for d in n.decorator_list)
                    and (class_scope is None or class_scope.plain)
                )
                inert = all(_inert_def(n) for n in nodes) and (
                    class_scope is None or class_scope.plain
                )
                inert = inert and (deferred or not any(_has_annotations(n) for n in nodes))
                symbol = Symbol(
                    id=symbol_id,
                    kind=METHOD if class_scope is not None else FUNCTION,
                    module=scope.name,
                    name=name,
                    path=scope.path,
                    lineno=first.lineno,
                    body_hash=body_hash,
                    docstring_hash=doc_hash,
                    definition_hash=definition_hash,
                    container=container_id,
                    line_ranges=tuple((_start_line(n), _end_line(n)) for n in nodes),
                    annotation_hash=annotation_hash,
                    deferred_annotations=deferred,
                    inert_definition=inert,
                )
                if not self._add_symbol(symbol):
                    continue
                members[name] = symbol_id
                self.out.edges.add(Edge(symbol_id, container_id, DEFINED_IN))

    def _member_id(self, module: str, name: str) -> str:
        return member_symbol_id(module, name, self._children.get(module, frozenset()))

    def modules_with_prefix(self, prefix: str) -> tuple[str, ...]:
        return tuple(sorted(m for m in self.scopes if m.startswith(prefix)))

    def symbol_names_with_prefix(self, prefix: str) -> tuple[str, ...]:
        names = {s.name for s in self.index.symbols.values() if s.name.startswith(prefix)}
        return tuple(sorted(names))

    # -- inheritance ----------------------------------------------------------

    def _ensure_bases(self, class_id: str) -> None:
        """Resolve a class's bases on demand (a dotted base such as
        ``Zed.Inner`` may need another class's MRO first)."""
        cscope = self.class_scopes[class_id]
        if cscope.bases_state:
            return
        cscope.bases_state = 1
        for parts in cscope.base_chains:
            node = self._resolve_base_expr(parts, cscope)
            self._record(cscope.id, node, chain=".".join(parts) if parts else "<expr>")
            if (
                isinstance(node, Resolved)
                and not node.detail
                and not node.uncertain_attr
                and node.symbol in self.class_scopes
                and node.symbol != cscope.id
            ):
                cscope.bases.append(node.symbol)
            else:
                cscope.complete = False  # external, dynamic (``Generic[T]``) or unknown
                if not (parts == ["object"] and node is None):
                    cscope.opaque = True
        for name in cscope.base_names:
            if self._calls_back(name, cscope):
                cscope.external_base = True
        cscope.bases_state = 2

    def _calls_back(self, name: list[str] | None, cscope: ClassScope) -> bool:
        """Whether a base (as written) is code outside the source roots that
        may call the subclass's methods: not an in-scope class (its own
        status is inherited through the MRO), not a builtin, not a purely
        structural typing/abc base. An unknown expression counts."""
        if name is None:
            return True
        node = self._resolve_base_expr(name, cscope)
        if isinstance(node, Resolved) and not node.detail and node.symbol in self.class_scopes:
            return False
        head = name[0]
        binding = cscope.module.imports.get(head)
        if binding is None:
            if node is None and head in BUILTIN_NAMES and len(name) == 1:
                return False
            canonical = ".".join(name)
        else:
            base = binding.module if binding.attr is None else f"{binding.module}.{binding.attr}"
            canonical = ".".join([base, *name[1:]])
        return canonical not in _STRUCTURAL_BASES

    def _resolve_base_expr(self, parts: list[str] | None, cscope: ClassScope) -> Node:
        """A base name is looked up in the enclosing class body (for nested
        classes) and then in the module, as Python does when the class
        statement executes."""
        if parts is None:
            return None  # ``Generic[T]``, ``namedtuple(...)``: the collector visits it
        enclosing = cscope.enclosing
        if enclosing is not None and parts[0] in enclosing.members:
            node: Node = Resolved(enclosing.members[parts[0]])
            for attr in parts[1:]:
                node = self._step(node, attr)
            return node
        if enclosing is not None and parts[0] in enclosing.bindings:
            return Resolved(enclosing.id, detail=f"attribute:{parts[0]}")
        return self.resolve_chain(parts, Scope(module=cscope.module))

    def _mro(self, class_id: str) -> list[str]:
        """Linearisation over in-scope classes: the class, then its bases
        depth-first left to right keeping the last occurrence of a repeated
        base (C3 for ordinary hierarchies; documented as an approximation).
        Memoised only once every class's bases are resolved."""
        cscope = self.class_scopes[class_id]
        if cscope.mro is not None:
            return cscope.mro
        self._ensure_bases(class_id)
        if cscope.in_mro:
            return [class_id]  # inheritance cycle: stop here
        cscope.in_mro = True
        try:
            order: list[str] = []
            for base in cscope.bases:
                order.extend(self._mro(base))
        finally:
            cscope.in_mro = False
        seen: set[str] = {class_id}
        tail: list[str] = []
        for cid in reversed(order):
            if cid not in seen:
                seen.add(cid)
                tail.append(cid)
        result = [class_id, *reversed(tail)]
        if self._bases_final:
            cscope.mro = result
        return result

    def lookup_in_class(
        self, class_id: str, attr: str, *, skip_self: bool = False, dispatch: bool = False
    ) -> Node:
        """Resolve ``attr`` on a class through its in-scope MRO.

        A hit found after a class whose bases are not all known is marked
        uncertain: an override in the unknown part of the hierarchy could
        win, so the edge is recorded together with a name-bounded unresolved
        reference. Not found anywhere yields the unresolved reference alone.

        With ``dispatch`` (a lookup on ``self``/``cls``) the receiver may be
        an instance of any in-scope subclass, so every subclass that defines
        ``attr`` itself is returned as an override to record as well.
        """
        uncertain = False
        for cid in self._mro(class_id)[1 if skip_self else 0 :]:
            cscope = self.class_scopes[cid]
            hit: Resolved | None = None
            if attr in cscope.members:
                hit = Resolved(cscope.members[attr], uncertain_attr=attr if uncertain else "")
            elif attr in cscope.bindings:
                hit = Resolved(
                    cid, detail=f"attribute:{attr}", uncertain_attr=attr if uncertain else ""
                )
            if hit is not None:
                if dispatch:
                    hit = Resolved(
                        hit.symbol,
                        hit.detail,
                        hit.uncertain_attr,
                        overrides=self._overrides_of(class_id, attr, (hit.symbol, hit.detail)),
                    )
                return hit
            if not cscope.complete:
                uncertain = True
        return Unresolved(UNRESOLVED_ATTRIBUTE, attr)

    def _external_callers(self) -> None:
        """A class whose MRO has a base outside the source roots (a
        transport, a handler, a visitor) may have any method called by that
        code, which nothing in the source roots shows: the class depends on
        every method it defines, like its special methods. The generic
        bases of ``Base[T]`` are not in the MRO; their status counts too."""
        for class_id in sorted(self.class_scopes):
            cscope = self.class_scopes[class_id]
            related = set(self._mro(class_id))
            for chain, name in zip(cscope.base_chains, cscope.base_names, strict=True):
                if chain is None and name is not None:  # ``Base[T]``
                    node = self._resolve_base_expr(name, cscope)
                    if isinstance(node, Resolved) and node.symbol in self.class_scopes:
                        related |= set(self._mro(node.symbol))
            if not any(self.class_scopes[c].external_base for c in related):
                continue
            for member, member_id in sorted(cscope.members.items()):
                symbol = self.index.symbols.get(member_id)
                if symbol is None or symbol.kind != METHOD or _is_special_method(member):
                    continue
                if member in _EXPLICIT_SPECIAL_METHODS:
                    continue
                self.out.edges.add(Edge(class_id, member_id, REFERENCES, "external_base"))

    def _build_descendants(self) -> None:
        subclasses: dict[str, set[str]] = defaultdict(set)
        for cscope in self.class_scopes.values():
            for base in cscope.bases:
                subclasses[base].add(cscope.id)
        for class_id in self.class_scopes:
            seen = {class_id}
            order: list[str] = []
            stack = [class_id]
            while stack:
                for sub in sorted(subclasses.get(stack.pop(), ())):
                    if sub not in seen:
                        seen.add(sub)
                        order.append(sub)
                        stack.append(sub)
            self._descendants[class_id] = tuple(order)

    def _overrides_of(
        self, class_id: str, attr: str, base_hit: tuple[str, str]
    ) -> tuple[tuple[str, str], ...]:
        """What ``attr`` resolves to on each in-scope descendant of
        ``class_id`` when that differs from the base hit: a method defined by
        the descendant, one it inherits from a mixin outside the base's
        hierarchy, or a class-attribute rebinding."""
        assert self._bases_final, "overrides need every class's bases resolved"
        found: list[tuple[str, str]] = []
        for sub in self._descendants.get(class_id, ()):
            hit = self.lookup_in_class(sub, attr)
            if isinstance(hit, Resolved):
                pair = (hit.symbol, hit.detail)
                if pair != base_hit and pair not in found:
                    found.append(pair)
        return tuple(found)

    # -- pass 2 ---------------------------------------------------------------

    def _module_in_scope(self, name: str) -> bool:
        return name in self._module_prefixes

    def _resolve_module(self, scope: ModuleScope) -> None:
        module_scope = Scope(module=scope)
        # Module-level imports -> init-time edges.
        for node in scope.import_nodes:
            self._import_edges(scope.name, scope, node)
        top_level = [
            s
            for s in scope.tree.body
            if not isinstance(s, DEF_NODES + (ast.Import, ast.ImportFrom))
        ]
        collector = _ReferenceCollector(self, scope.name, module_scope, skip_defs=True)
        variable_ids = {
            id(stmt): symbol
            for name, stmt in scope.variable_stmts.items()
            for symbol in [scope.variables[name]]
        }
        for stmt in top_level:
            owner = variable_ids.get(id(stmt))
            if owner is not None:
                # The right-hand side's references belong to the variable symbol.
                _ReferenceCollector(self, owner, module_scope, skip_defs=True).visit(stmt)
            else:
                collector.visit(stmt)
        self._resolve_definitions(scope, scope.tree.body, scope.members, None)

    def _resolve_definitions(
        self,
        scope: ModuleScope,
        body: list[ast.stmt],
        members: dict[str, str],
        class_scope: ClassScope | None,
    ) -> None:
        for stmt in iter_scope_statements(body):
            if isinstance(stmt, ast.ClassDef):
                symbol_id = members.get(stmt.name)
                if symbol_id is None or symbol_id not in self.class_scopes:
                    continue
                cscope = self.class_scopes[symbol_id]
                class_level = Scope(module=scope, locals=set(cscope.bindings))
                collector = _ReferenceCollector(self, symbol_id, class_level, skip_defs=True)
                # The class statement runs when its container runs: a
                # top-level class is created when the module is imported.
                for creator in (symbol_id, scope.name) if class_scope is None else (symbol_id,):
                    self.class_creation(creator, cscope.bases, stmt.keywords, Scope(module=scope))
                # Name-chain bases were resolved (and recorded) by _ensure_bases;
                # only dynamic base expressions still need their references collected.
                dynamic_bases = [b for b in stmt.bases if _flatten_chain(b) is None]
                for expr in dynamic_bases + list(stmt.keywords) + list(stmt.decorator_list):
                    collector.visit(expr)
                for inner in stmt.body:
                    if not isinstance(inner, DEF_NODES):
                        collector.visit(inner)
                self._resolve_definitions(scope, stmt.body, cscope.members, cscope)
            elif isinstance(stmt, FUNC_NODES):
                symbol_id = members.get(stmt.name)
                if symbol_id is None:
                    continue
                self._resolve_function(scope, stmt, symbol_id, class_scope)

    def _resolve_function(
        self,
        scope: ModuleScope,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        symbol_id: str,
        class_scope: ClassScope | None,
    ) -> None:
        fscope = Scope(module=scope, locals=_LocalBindings().collect(node), literal_node=node)
        bound_method = class_scope is not None and not _is_staticmethod(node)
        if bound_method:
            params = node.args.posonlyargs + node.args.args
            if params:
                fscope.self_name = params[0].arg
                fscope.self_class = class_scope.id
                fscope.method = symbol_id
                fscope.self_is_class = _has_decorator(node, "classmethod")
        fscope.rebound = frozenset(_rebound_names(node))
        positional = [a.arg for a in node.args.posonlyargs + node.args.args]
        fscope.params = {name: i for i, name in enumerate(positional)}
        fscope.params.update({a.arg: None for a in node.args.kwonlyargs})
        defaults: dict[str, tuple[str, ...] | None] = {}
        n_defaults = len(node.args.defaults)
        for name, default in zip(
            positional[len(positional) - n_defaults :], node.args.defaults, strict=True
        ):
            defaults[name] = fscope.string_candidates(default)
        for a, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            if default is not None:
                defaults[a.arg] = fscope.string_candidates(default)
        self.out.func_params[symbol_id] = _FuncParams(
            positional=positional,
            bound=bound_method,
            defaults=defaults,
            has_varargs=node.args.vararg is not None or node.args.kwarg is not None,
        )
        # Function-local imports are visible to the whole body.
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Import, ast.ImportFrom)):
                stars: list[str] = []
                self._register_imports(scope, inner, fscope.local_imports, stars)
                self._import_edges(symbol_id, scope, inner, local=True)
        # Decorators, defaults and annotations evaluate where the ``def``
        # statement runs, so the function's own parameters must not shadow
        # them (``def f(info=info)`` refers to the module-level ``info``).
        outer_scope = Scope(
            module=scope,
            locals=set(class_scope.bindings) if class_scope is not None else set(),
        )
        outer = _ReferenceCollector(self, symbol_id, outer_scope, skip_defs=True)
        for dec in node.decorator_list:
            outer.visit(dec)
        all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for arg in all_args + [a for a in (node.args.vararg, node.args.kwarg) if a]:
            if arg.annotation is not None:
                outer.visit_type(arg.annotation)
        if node.returns is not None:
            outer.visit_type(node.returns)
        for name, default in zip(
            positional[len(positional) - n_defaults :] + [a.arg for a in node.args.kwonlyargs],
            list(node.args.defaults) + list(node.args.kw_defaults),
            strict=True,
        ):
            if default is None:
                continue
            outer.visit(default)
            target = (
                self.resolve_chain(parts, outer_scope)
                if (parts := _flatten_chain(default))
                else None
            )
            if isinstance(target, Resolved) and not target.detail:
                aliased = self.index.symbols.get(target.symbol)
                if aliased is not None and aliased.kind == VARIABLE:
                    fscope.param_aliases[name] = aliased.id
        collector = _ReferenceCollector(self, symbol_id, fscope)
        for stmt in node.body:
            collector.visit(stmt)

    def _import_edges(
        self, source: str, scope: ModuleScope, node: ast.stmt, local: bool = False
    ) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._module_import_edge(source, alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(scope, node.module, node.level)
            self._module_import_edge(source, base)
            if not self._module_in_scope(base):
                return
            for alias in node.names:
                if alias.name == "*":
                    continue
                target = self._step(ModuleNode(base), alias.name)
                if isinstance(target, ModuleNode):
                    self._module_import_edge(source, target.module)
                    continue
                self._record(
                    source,
                    target,
                    kind=IMPORTS_NAME if not local else REFERENCES,
                    chain=f"from {base} import {alias.name}",
                )

    def _module_import_edge(self, source: str, module: str) -> None:
        if not self._module_in_scope(module):
            if self._module_in_scope(module.split(".")[0]):
                self.out.unresolved.add(
                    UnresolvedReference(
                        source, UNRESOLVED_ATTRIBUTE, module.rsplit(".", 1)[-1], f"import {module}"
                    )
                )
            else:
                self.out.external.add(ExternalReference(source, module))
            return
        parts = module.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if prefix in self.scopes and prefix != source:
                self.out.edges.add(Edge(source, prefix, IMPORTS))

    # -- resolution --------------------------------------------------------------

    def _lookup_base(self, name: str, scope: Scope) -> Node:
        if scope.self_name is not None and name == scope.self_name and scope.self_class:
            return Resolved(scope.self_class, receiver=True)
        if name in scope.local_imports:
            return self._import_binding_node(scope.local_imports[name])
        if name in scope.locals:
            if name in scope.param_aliases:
                return Resolved(scope.param_aliases[name])
            return Local()
        found = self._lookup_in_module(scope.module, name, set())
        if found is not None:
            return found
        if name in BUILTIN_NAMES or (name.startswith("__") and name.endswith("__")):
            return None
        return Unresolved(UNRESOLVED_NAME, name)

    def _lookup_in_module(self, target: ModuleScope, name: str, seen: set[str]) -> Node:
        """Resolve ``name`` as seen from inside ``target``'s global namespace.

        Order: own definitions, import aliases, module-level variables,
        submodules, then star imports. Every analysed star-imported module is
        consulted before an external star import is blamed, so an in-scope
        symbol is never misattributed to a third-party package.
        """
        if name in target.members:
            return Resolved(target.members[name])
        if name in target.imports:
            return self._import_binding_node(target.imports[name])
        if name in target.variables:
            return Resolved(target.variables[name])
        if name in target.bindings:
            return Resolved(target.name, detail=f"attribute:{name}")
        if self._module_in_scope(f"{target.name}.{name}"):
            return ModuleNode(f"{target.name}.{name}")
        external: str | None = None
        for star in target.star_imports:
            if star in seen:
                continue
            seen.add(star)
            star_scope = self.scopes.get(star)
            if star_scope is None:
                if external is None and not self._module_in_scope(star):
                    external = star
                continue
            found = self._lookup_in_module(star_scope, name, seen)
            if isinstance(found, External):
                external = external or found.module
            elif found is not None:
                return found
        return External(external) if external is not None else None

    def _import_binding_node(self, binding: ImportBinding) -> Node:
        if not self._module_in_scope(binding.module):
            if self._module_in_scope(binding.module.split(".")[0]):
                # ``pkg.missing`` inside an analysed package: not an external
                # dependency, but nothing we can see either (deleted module,
                # compiled extension, generated code).
                name = binding.attr or binding.module.rsplit(".", 1)[-1]
                return Unresolved(UNRESOLVED_ATTRIBUTE, name)
            return External(binding.module)
        node: Node = ModuleNode(binding.module)
        if binding.attr is not None:
            node = self._step(node, binding.attr)
        return node

    def _step(self, node: Node, attr: str) -> Node:
        """Resolve one attribute access on a resolved node."""
        if isinstance(node, ModuleNode):
            sub = f"{node.module}.{attr}"
            target = self.scopes.get(node.module)
            if self._module_in_scope(sub):
                # A package binding of the same name wins at runtime once the
                # package has run (it binds after importing the submodule);
                # either may be meant, so the attribute denotes both.
                shadow = None
                if target is not None:
                    shadow = target.members.get(attr) or target.variables.get(attr)
                if shadow is not None and sub in self.scopes:
                    return Resolved(shadow, also=(sub,))
                return ModuleNode(sub)
            if target is None:
                return Unresolved(UNRESOLVED_ATTRIBUTE, attr)
            found = self._lookup_in_module(target, attr, set())
            return found if found is not None else Unresolved(UNRESOLVED_ATTRIBUTE, attr)
        if isinstance(node, External):
            return node
        if isinstance(node, Resolved):
            if node.detail:
                return node  # attribute of an opaque module/class attribute: stop here
            symbol = self.index.symbols.get(node.symbol)
            if symbol is None:
                return node
            if symbol.kind == CLASS:
                return self.lookup_in_class(symbol.id, attr, dispatch=node.receiver)
            if symbol.kind == MODULE:
                return self._step(ModuleNode(symbol.id), attr)
            return node  # attribute on a function object
        if isinstance(node, Unresolved):
            return Unresolved(UNRESOLVED_ATTRIBUTE, attr)
        return None

    def resolve_chain(self, parts: list[str], scope: Scope) -> Node:
        node = self._lookup_base(parts[0], scope)
        if isinstance(node, Local):
            # ``obj.method`` on a local: the method name bounds what it may be.
            return Unresolved(UNRESOLVED_ATTRIBUTE, parts[1]) if len(parts) > 1 else None
        for attr in parts[1:]:
            if node is None:
                return None
            if isinstance(node, Resolved) and node.detail:
                return node
            node = self._step(node, attr)
        return node

    def resolve_chain_names(self, parts: list[str], scope: Scope) -> tuple[Node, tuple[str, ...]]:
        """Like resolve_chain, plus the attribute names after the point where
        resolution stopped (an unknown or opaque value: a local, a failed
        step, a variable, a function, a class-level binding). Each is looked
        up on a value of unknown type, so each is a name-bounded reference."""
        node = self._lookup_base(parts[0], scope)
        if isinstance(node, Local):
            if len(parts) < 2:
                return None, ()
            return Unresolved(UNRESOLVED_ATTRIBUTE, parts[1]), tuple(parts[2:])
        for i, attr in enumerate(parts[1:], 1):
            if node is None or isinstance(node, External):
                return node, ()
            if isinstance(node, Resolved):
                symbol = self.index.symbols.get(node.symbol)
                opaque = node.detail or (symbol is not None and symbol.kind not in (CLASS, MODULE))
                if opaque:
                    return node, tuple(parts[i:])
            node = self._step(node, attr)
            if isinstance(node, Unresolved):
                return node, tuple(parts[i + 1 :])
        return node, ()

    def class_creation(
        self, source: str, bases: list[str], keywords: list[ast.keyword], scope: Scope
    ) -> None:
        """Creating a class runs code of its bases and metaclass: the
        ``__init_subclass__`` that the new class's MRO finds after itself
        (the union of what each in-scope base finds covers it), and an
        in-scope metaclass's ``__new__`` and ``__init__``."""
        hooks: list[tuple[str, str]] = []
        for base in bases:
            hooks.append((base, "__init_subclass__"))
        for kw in keywords:
            parts = _flatten_chain(kw.value) if kw.arg == "metaclass" else None
            if parts is None:
                continue
            meta = self.resolve_chain(parts, scope)
            if isinstance(meta, Resolved) and not meta.detail and meta.symbol in self.class_scopes:
                hooks += [(meta.symbol, "__new__"), (meta.symbol, "__init__")]
        for class_id, name in hooks:
            hit = self.lookup_in_class(class_id, name)
            if isinstance(hit, Resolved) and not hit.detail and hit.symbol != source:
                self.out.edges.add(Edge(source, hit.symbol, REFERENCES, name))

    def escape(self, target: Resolved, *, classes: bool = True) -> None:
        """Record that ``target`` is used as a value (see _mark_escape)."""
        symbol = self.index.symbols.get(target.symbol)
        if symbol is None:
            return
        if symbol.kind in (FUNCTION, METHOD) or (classes and symbol.kind == CLASS):
            self.out.escapes.add(symbol.id)
            for override_id, detail in target.overrides:
                if not detail:
                    self.out.escapes.add(override_id)

    def escape_class_family(self, class_id: str) -> None:
        """The class of an instance of ``class_id``: any in-scope subclass."""
        self.out.escapes.add(class_id)
        self.out.escapes.update(self._descendants.get(class_id, ()))

    def _record(self, source: str, node: Node, kind: str = REFERENCES, chain: str = "") -> None:
        if node is None:
            return
        if isinstance(node, Resolved):
            if node.symbol != source:
                self.out.edges.add(Edge(source, node.symbol, kind, node.detail))
            if node.uncertain_attr:
                self.out.unresolved.add(
                    UnresolvedReference(source, UNRESOLVED_ATTRIBUTE, node.uncertain_attr, chain)
                )
            for symbol_id, detail in node.overrides:
                if symbol_id != source:
                    label = f"override:{detail}" if detail else "override"
                    self.out.edges.add(Edge(source, symbol_id, kind, label))
            for module in node.also:
                if module != source:
                    self.out.edges.add(Edge(source, module, kind, "module"))
            if kind == REFERENCES and not node.detail and node.symbol in self.class_scopes:
                # Using a class (``Foo(...)``, subclassing) runs its constructor.
                init = self.lookup_in_class(node.symbol, "__init__")
                if isinstance(init, Resolved) and init.symbol != source:
                    self.out.edges.add(Edge(source, init.symbol, REFERENCES, "constructor"))
        elif isinstance(node, ModuleNode):
            if node.module in self.scopes and node.module != source:
                self.out.edges.add(Edge(source, node.module, kind, "module"))
        elif isinstance(node, External):
            self.out.external.add(ExternalReference(source, node.module))
        elif isinstance(node, Unresolved):
            self.out.unresolved.add(UnresolvedReference(source, node.kind, node.name, chain))


def _canonical_imports(scope: ModuleScope) -> set[str]:
    """One string per imported binding, independent of statement grouping or
    order: adding a name to ``from m import (a, b)`` adds one entry."""
    out: set[str] = set()
    for node in scope.import_nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(scope, node.module, node.level)
            for alias in node.names:
                entry = f"from {base} import {alias.name}"
                out.add(entry + (f" as {alias.asname}" if alias.asname else ""))
    return out


def _start_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    """First line of a definition including its decorators, which belong to
    the definition (they are part of its definition hash)."""
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def _variable_statements(scope: ModuleScope, body: list[ast.stmt]) -> dict[str, ast.stmt]:
    """Top-level ``NAME = <expr>`` / ``NAME: T = <expr>`` statements whose name
    is bound exactly once in the module: candidates for variable symbols.
    Names bound any other way too (in a loop, by unpacking, inside a block)
    stay on the module symbol."""
    simple: dict[str, list[ast.stmt]] = defaultdict(list)
    for stmt in body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target = stmt.target
        else:
            continue
        if isinstance(target, ast.Name):
            simple[target.id].append(stmt)
    simple_stmts = {id(s) for stmts in simple.values() for s in stmts}
    other_bound: set[str] = set()
    for stmt in iter_scope_statements(scope.tree.body):
        if isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
            continue
        if id(stmt) in simple_stmts:
            continue
        other_bound |= _collect_store_names(stmt)
    return {
        name: stmts[0]
        for name, stmts in simple.items()
        if len(stmts) == 1 and name not in other_bound and name not in scope.imports
    }


def _end_line(node: ast.AST) -> int:
    """Last source line of a definition; a Module has no position of its own."""
    if isinstance(node, ast.Module):
        return node.body[-1].end_lineno or 1 if node.body else 1
    return node.end_lineno or node.lineno  # type: ignore[attr-defined]


def _has_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == name:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == name:
            return True
    return False


# Bases outside the source roots that only add structure and never call
# a subclass's methods.
_STRUCTURAL_BASES = frozenset(
    {
        "abc.ABC",
        "typing.Generic",
        "typing.Protocol",
        "typing.NamedTuple",
        "typing.TypedDict",
        "typing_extensions.Generic",
        "typing_extensions.Protocol",
        "typing_extensions.NamedTuple",
        "typing_extensions.TypedDict",
    }
)

# Reached explicitly: construction and class creation record their own edges.
_EXPLICIT_SPECIAL_METHODS = frozenset({"__init__", "__new__", "__init_subclass__"})


def _is_special_method(name: str) -> bool:
    return (
        len(name) > 4
        and name.startswith("__")
        and name.endswith("__")
        and name not in _EXPLICIT_SPECIAL_METHODS
    )


def _split_annotations(args: ast.arguments) -> tuple[ast.arguments, list[ast.AST]]:
    """The parameters without their annotations, and the annotations."""
    stripped = copy.deepcopy(args)
    annotations: list[ast.AST] = []
    params = [*stripped.posonlyargs, *stripped.args, *stripped.kwonlyargs]
    for arg in [*params, stripped.vararg, stripped.kwarg]:
        if arg is not None and arg.annotation is not None:
            annotations.append(arg.annotation)
            arg.annotation = None
    return stripped, annotations


# Decorators that do nothing with the function's annotations.
_INERT_DECORATORS = frozenset({"overload", "override", "final"})


def _is_inert_decorator(node: ast.expr) -> bool:
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)
    return name in _INERT_DECORATORS


def _is_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_literal(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(e) for e in node.elts)
    return False


def _inert_def(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether executing the ``def`` runs no code beyond binding the name:
    inert decorators and literal defaults (annotations are checked apart)."""
    defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
    return all(_is_inert_decorator(d) for d in node.decorator_list) and all(
        _is_literal(d) for d in defaults
    )


def _has_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return node.returns is not None or any(
        isinstance(a, ast.arg) and a.annotation is not None for a in ast.walk(node.args)
    )


def _future_annotations(scope: ModuleScope) -> bool:
    binding = scope.imports.get("annotations")
    return binding is not None and binding.module == "__future__"


def _is_staticmethod(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return _has_decorator(node, "staticmethod")


# Defining one of these changes what ``self.<attr>`` reads or writes.
_ATTRIBUTE_HOOKS = frozenset({"__setattr__", "__delattr__", "__getattr__", "__getattribute__"})


def _rebound_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every name the function's body (nested scopes included, which only
    over-approximates) binds, deletes or imports."""
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and not isinstance(inner.ctx, ast.Load):
            names.add(inner.id)
        elif isinstance(inner, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and inner.name:
            names.add(inner.name)
        elif isinstance(inner, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in inner.names}
    return names


def _flatten_chain(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


class _ReferenceCollector(ast.NodeVisitor):
    """Walk a symbol's code and record edges / unresolved references.

    Nested functions, lambdas and comprehensions push their own scope so a
    name bound there does not shadow the enclosing symbol's references. With
    ``skip_defs`` (module and class bodies) nested definitions are not
    entered at all: they are symbols resolved on their own.
    """

    def __init__(
        self, indexer: Indexer, source: str, scope: Scope, *, skip_defs: bool = False
    ) -> None:
        self.indexer = indexer
        self.source = source
        self.scope = scope
        self.skip_defs = skip_defs
        # Type positions (annotations, ``isinstance``'s second argument,
        # ``cast``'s first): a class named there is not used as a value.
        self._type_depth = 0
        self._type_nodes: set[int] = set()
        # ``self.<attr> = value`` targets in ``__init__`` -> what they bind.
        self._bindings: dict[int, list | None] = {}

    def visit_type(self, node: ast.AST) -> None:
        self._type_depth += 1
        try:
            self.visit(node)
        finally:
            self._type_depth -= 1

    def _push(
        self,
        bound: set[str],
        literals: dict[str, tuple[str, ...] | None] | None = None,
        node: ast.AST | None = None,
    ) -> Scope:
        outer = self.scope
        self.scope = Scope(
            module=outer.module,
            local_imports=outer.local_imports,
            locals=outer.locals | bound,
            self_name=None if outer.self_name in bound else outer.self_name,
            self_class=None if outer.self_name in bound else outer.self_class,
            self_is_class=outer.self_is_class,
            literal_node=node,
            literal_parent=outer,
            literal_bound=frozenset(bound),
            literal_extra=dict(literals or {}),
            params={},  # a nested scope's names are not the enclosing function's parameters
            param_aliases={k: v for k, v in outer.param_aliases.items() if k not in bound},
        )
        return outer

    def _is_shadowed(self, name: str) -> bool:
        """True when ``name`` is bound by the program rather than a builtin."""
        scope = self.scope
        if name in scope.locals or name in scope.local_imports:
            return True
        module = scope.module
        return name in module.members or name in module.imports or name in module.bindings

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self.skip_defs:
            return
        for dec in node.decorator_list:
            self.visit(dec)
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            self.visit(default)
        outer = self._push(_LocalBindings().collect(node), node=node)
        try:
            for arg in ast.walk(node.args):
                if isinstance(arg, ast.arg) and arg.annotation is not None:
                    self.visit_type(arg.annotation)
            if node.returns is not None:
                self.visit_type(node.returns)
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self.scope = outer

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self.skip_defs:
            return
        for expr in list(node.bases) + list(node.keywords) + list(node.decorator_list):
            self.visit(expr)
        bases: list[str] = []
        for expr in node.bases:
            parts = _flatten_chain(expr)
            target = self.indexer.resolve_chain(parts, self.scope) if parts else None
            if isinstance(target, Resolved) and not target.detail:
                if target.symbol in self.indexer.class_scopes:
                    bases.append(target.symbol)
        self.indexer.class_creation(self.source, bases, node.keywords, self.scope)
        outer = self._push(_LocalBindings().collect(node))
        try:
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self.scope = outer

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            self.visit(default)
        outer = self._push(_LocalBindings().collect(node))
        try:
            self.visit(node.body)
        finally:
            self.scope = outer

    def _visit_comprehension(self, node: ast.AST) -> None:
        generators = node.generators  # type: ignore[attr-defined]
        bound: set[str] = set()
        literals: dict[str, tuple[str, ...] | None] = {}
        for gen in generators:
            bound |= _collect_store_names(gen.target)
            if isinstance(gen.target, ast.Name):
                literals[gen.target.id] = self.scope.string_candidates(gen.iter)
        outer = self._push(bound, literals)
        try:
            for gen in generators:
                self.visit(gen.iter)
                for cond in gen.ifs:
                    self.visit(cond)
            for field_name in ("elt", "key", "value"):
                child = getattr(node, field_name, None)
                if child is not None:
                    self.visit(child)
        finally:
            self.scope = outer

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    def _resolve(self, parts: list[str], kind: str = REFERENCES) -> None:
        node, rest = self.indexer.resolve_chain_names(parts, self.scope)
        chain = ".".join(parts)
        self.indexer._record(self.source, node, kind=kind, chain=chain)
        for name in rest:
            self.indexer.out.unresolved.add(
                UnresolvedReference(self.source, UNRESOLVED_ATTRIBUTE, name, chain)
            )

    def _dynamic(self, detail: str) -> None:
        self.indexer.out.unresolved.add(
            UnresolvedReference(self.source, UNRESOLVED_DYNAMIC, "", detail)
        )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._resolve([node.id])
            self._mark_escape(node, [node.id])

    def _is_self(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Name)
            and node.id == self.scope.self_name
            and self.scope.self_class is not None
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if not isinstance(node.ctx, ast.Load):
            self._attribute_write(node)
        if node.attr == "__dict__" and self._is_self(node.value):
            # ``self.__dict__`` can read or write any attribute.
            self.indexer.out.attr_unbound.add((self.scope.self_class, "*"))
        parts = _flatten_chain(node)
        if parts is not None:
            self._resolve(parts)
            self._mark_escape(node, parts)
            if (
                isinstance(node.ctx, ast.Load)
                and len(parts) >= 2
                and parts[0] == self.scope.self_name
                and self.scope.self_class is not None
                and isinstance(
                    self.indexer.lookup_in_class(self.scope.self_class, parts[1]), Unresolved
                )
            ):
                self.indexer.out.attr_refs.append(
                    _AttrRef(
                        self.source, self.scope.self_class, parts[1], parts[2:], ".".join(parts)
                    )
                )
            return
        if self._is_zero_arg_super(node.value) and self.scope.self_class is not None:
            # ``super().m``: next definition of ``m`` in the enclosing class's MRO.
            target = self.indexer.lookup_super(self.scope.self_class, node.attr)
            self.indexer._record(self.source, target, chain=f"super().{node.attr}")
            if node is not self._call_func:
                self._escape(target)
            return
        # ``Foo().run``, ``items[0].run``, ``make().run``: the base value is
        # unknown, but the attribute name still bounds what it may refer to.
        self.indexer.out.unresolved.add(
            UnresolvedReference(self.source, UNRESOLVED_ATTRIBUTE, node.attr, f"<expr>.{node.attr}")
        )
        self.generic_visit(node)

    def _is_zero_arg_super(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
            and not self._is_shadowed("super")
        )

    def visit_Import(self, node: ast.Import) -> None:
        return  # handled by Indexer._import_edges

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return

    # Methods that mutate a container in place: a call of one on a variable
    # makes the caller a writer of that variable.
    MUTATING_METHODS = frozenset(
        {
            "append",
            "extend",
            "insert",
            "pop",
            "popitem",
            "remove",
            "clear",
            "update",
            "setdefault",
            "add",
            "discard",
            "sort",
            "reverse",
            "__setitem__",
            "__delitem__",
        }
    )

    def _mutation_target(self, expr: ast.expr) -> None:
        """Record ``source`` as a writer when ``expr`` (an assignment target or
        a receiver of a mutating call) is a subscript/attribute of a variable
        symbol, or the variable itself under ``global``."""
        base = expr
        while isinstance(base, (ast.Subscript, ast.Attribute)):
            base = base.value
        if not isinstance(base, ast.Name):
            return
        if base.id in self.scope.locals and base.id not in self.scope.param_aliases:
            return
        if base is expr and base.id in self.scope.locals:
            return  # rebinding a parameter is not a mutation of its default
        node = self.indexer.resolve_chain([base.id], self.scope)
        if isinstance(node, Resolved) and not node.detail:
            symbol = self.indexer.index.symbols.get(node.symbol)
            if symbol is None or symbol.kind != VARIABLE or symbol.id == self.source:
                return
            if self.skip_defs and symbol.module == self.scope.module.name:
                return  # a module's own top-level mutations are part of the variable's hash
            self.indexer.out.edges.add(Edge(symbol.id, self.source, REFERENCES, "mutated_by"))

    def _attribute_write(self, node: ast.Attribute) -> None:
        """A store or delete of ``<receiver>.<attr>``: on ``self`` it is a
        write of that class's instance attribute (bound only when it is a
        plain ``__init__`` assignment); on any other receiver the type is
        unknown, so no class's ``attr`` can be bounded."""
        if self._is_self(node.value):
            binding = self._bindings.pop(id(node), None)
            self.indexer.out.attr_writes.append(
                _AttrWrite(self.scope.self_class, node.attr, self.scope.method, binding)
            )
        else:
            self.indexer.out.attr_unbound.add(("", node.attr))

    def _init_binding(self, value: ast.expr) -> list | None:
        """What ``self.<attr> = value`` in ``__init__`` binds, if bounded."""
        if isinstance(value, ast.Name) and value.id in self.scope.params:
            return None if value.id in self.scope.rebound else ["param", value.id]
        strings = self.scope.string_candidates(value)
        if strings is not None:
            return ["strings", list(strings)]
        parts = _flatten_chain(value)
        if parts is None:
            return None
        target = self.indexer.resolve_chain(parts, self.scope)
        if (
            isinstance(target, Resolved)
            and not (target.detail or target.receiver or target.overrides)
            and not target.uncertain_attr
        ):
            return ["symbol", target.symbol]
        return None

    def _stash_binding(self, target: ast.expr, value: ast.expr | None) -> None:
        if (
            value is not None
            and isinstance(target, ast.Attribute)
            and self._is_self(target.value)
            and self.scope.method.rsplit(".", 1)[-1] == "__init__"
        ):
            self._bindings[id(target)] = self._init_binding(value)

    def _reflective_write(self, receiver: ast.expr | None, name: ast.expr | None) -> None:
        """``setattr(receiver, name, ...)`` and its relatives."""
        names = self.scope.string_candidates(name) if name is not None else None
        owner = self.scope.self_class if receiver is not None and self._is_self(receiver) else ""
        for attr in names if names is not None else ("*",):
            self.indexer.out.attr_unbound.add((owner, attr.rsplit(".", 1)[-1]))

    def _dict_write(self, expr: ast.expr) -> None:
        """``x.__dict__[k] = v``, ``vars(x).update(...)``: any attribute."""
        while isinstance(expr, ast.Subscript):
            expr = expr.value
        receiver: ast.expr | None = None
        if isinstance(expr, ast.Attribute) and expr.attr == "__dict__":
            receiver = expr.value
        elif (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "vars"
            and expr.args
        ):
            receiver = expr.args[0]
        if receiver is not None:
            self._reflective_write(receiver, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1:
            self._stash_binding(node.targets[0], node.value)
        for target in node.targets:
            if isinstance(target, ast.Subscript):
                self._dict_write(target)
            for sub in ast.walk(target):
                if isinstance(sub, (ast.Subscript, ast.Attribute)):
                    self._mutation_target(sub)
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    self._mutation_target(sub)  # rebinding a ``global`` variable
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._mutation_target(node.target)
        if isinstance(node.target, ast.Subscript):
            self._dict_write(node.target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._mutation_target(node.target)
            self._stash_binding(node.target, node.value)
        self.visit(node.target)
        self.visit_type(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._mutation_target(target)
            if isinstance(target, ast.Subscript):
                self._dict_write(target)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        parts = _flatten_chain(node.func)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in self.MUTATING_METHODS
            and isinstance(node.func.value, (ast.Name, ast.Subscript, ast.Attribute, ast.Call))
        ):
            self._mutation_target(node.func.value)
            self._dict_write(node.func.value)
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "__setattr__",
            "__delattr__",
        ):
            # ``object.__setattr__(self, n, v)`` / ``self.__setattr__(n, v)``.
            receiver = node.func.value if self._is_self(node.func.value) else None
            if receiver is None and node.args and self._is_self(node.args[0]):
                receiver = node.args[0]
            self._reflective_write(receiver, None)
        if parts is not None:
            name = ".".join(parts)
            builtin = len(parts) == 1 and not self._is_shadowed(name)
            if builtin and parts[0] in DYNAMIC_CALLS:
                self._dynamic(f"{name}()")
            elif builtin and parts[0] == "getattr":
                self._getattr(node)
            elif (canonical := self._canonical_name(parts)) in (
                "importlib.import_module",
                "importlib.__import__",
            ):
                self._import_module(node, canonical)
            if builtin and parts[0] == "vars" and node.args and self._is_self(node.args[0]):
                self.indexer.out.attr_unbound.add((self.scope.self_class, "*"))
            if builtin and parts[0] == "type" and len(node.args) == 1:
                if self._is_self(node.args[0]):
                    self.indexer.escape_class_family(self.scope.self_class)
            if parts[-1] in ("setattr", "delattr") or parts[-2:] == ["patch", "object"]:
                self._setattr_call(node, parts)
            if builtin and parts[0] in ("isinstance", "issubclass") and len(node.args) == 2:
                self._mark_type_node(node.args[1])
            elif parts[-1] == "cast" and node.args:
                self._mark_type_node(node.args[0])
            self._record_call_site(node, parts)
        elif (
            isinstance(node.func, ast.Attribute)
            and self._is_zero_arg_super(node.func.value)
            and self.scope.self_class is not None
        ):
            target = self.indexer.lookup_super(self.scope.self_class, node.func.attr)
            self._add_call_site(node, target, receiver_bound=True)
        self._call_func = node.func
        self.generic_visit(node)

    _call_func: ast.expr | None = None

    def _mark_type_node(self, node: ast.expr) -> None:
        self._type_nodes.add(id(node))
        if isinstance(node, ast.Tuple):
            self._type_nodes |= {id(e) for e in node.elts}

    def _setattr_call(self, node: ast.Call, parts: list[str]) -> None:
        """``setattr``/``delattr``, ``monkeypatch.setattr`` and
        ``patch.object``: the receiver is the first argument and the name the
        second (``monkeypatch.setattr("pkg.mod.name", value)`` names it in a
        dotted string instead)."""
        args = list(node.args)
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        if len(parts) > 1 and args and isinstance(args[0], ast.Constant):
            self._reflective_write(None, args[0])
            return
        receiver = args[0] if args else keywords.get("target")
        name = args[1] if len(args) > 1 else keywords.get("name", keywords.get("attribute"))
        self._reflective_write(receiver, name)

    def _record_call_site(self, node: ast.Call, parts: list[str]) -> None:
        target = self.indexer.resolve_chain(parts, self.scope)
        if not (isinstance(target, Resolved) and not target.detail):
            return
        symbol = self.indexer.index.symbols.get(target.symbol)
        if symbol is not None and symbol.kind == CLASS:
            # Constructing a class calls the ``__init__`` its MRO resolves to
            # (any subclass's, for ``cls(...)``), with ``self`` implicit.
            init = self.indexer.lookup_in_class(symbol.id, "__init__", dispatch=target.receiver)
            self._add_call_site(node, init, receiver_bound=True)
            return
        if symbol is None or symbol.kind not in (FUNCTION, METHOD):
            return
        receiver_bound = False
        if symbol.kind == METHOD and len(parts) > 1:
            base = self.indexer._lookup_base(parts[0], self.scope)
            base_is_class = (
                isinstance(base, Resolved)
                and not base.detail
                and base.symbol in self.indexer.class_scopes
                and len(parts) == 2
                and parts[0] != self.scope.self_name  # self/cls also resolve to the class
            )
            receiver_bound = not base_is_class
        self._add_call_site(node, target, receiver_bound=receiver_bound)

    def _add_call_site(self, node: ast.Call, target: Node, *, receiver_bound: bool) -> None:
        if not (isinstance(target, Resolved) and not target.detail):
            return
        symbol = self.indexer.index.symbols.get(target.symbol)
        if symbol is None or symbol.kind not in (FUNCTION, METHOD):
            return
        unbounded = any(isinstance(a, ast.Starred) for a in node.args) or any(
            k.arg is None for k in node.keywords
        )
        site = _CallSite(
            positional=[self.scope.string_candidates(a) for a in node.args],
            keywords={
                k.arg: self.scope.string_candidates(k.value)
                for k in node.keywords
                if k.arg is not None
            },
            unbounded=unbounded,
            receiver_bound=receiver_bound,
        )
        self.indexer.out.call_sites[symbol.id].append(site)
        # A dispatched call may land on any override: they share the call site.
        for override_id, detail in target.overrides:
            if not detail:
                self.indexer.out.call_sites[override_id].append(site)

    def _mark_escape(self, node: ast.expr, parts: list[str]) -> None:
        """A function referenced other than as the callee of a call may be
        called from anywhere with anything; so may a class (constructed), unless
        the reference is in a type position. ``self`` as a value is an
        instance, not its class; ``cls``, ``type(self)`` and
        ``self.__class__`` are the class of any in-scope subclass."""
        if parts[0] == self.scope.self_name and self.scope.self_class is not None:
            if parts == [parts[0], "__class__"] or (
                len(parts) == 1 and self.scope.self_is_class and node is not self._call_func
            ):
                self.indexer.escape_class_family(self.scope.self_class)
                return
            if len(parts) == 1:
                return
        if node is self._call_func:
            return
        type_position = bool(self._type_depth) or id(node) in self._type_nodes
        self._escape(self.indexer.resolve_chain(parts, self.scope), classes=not type_position)

    def _escape(self, target: Node, *, classes: bool = True) -> None:
        if isinstance(target, Resolved) and not target.detail:
            self.indexer.escape(target, classes=classes)

    def _param_dynamic(
        self, expr: ast.expr, kind: str, base: list[str] | None, detail: str
    ) -> bool:
        """Defer a dynamic use whose name is one of the enclosing function's
        parameters (not rebound in its body) or an instance attribute
        ``self.<name>``; returns False when that does not apply."""
        if isinstance(expr, ast.Attribute) and self._is_self(expr.value):
            self.indexer.out.param_dynamics.append(
                _ParamDynamic(
                    self.source, expr.attr, kind, base, self.scope, detail, self.scope.self_class
                )
            )
            return True
        if not (
            isinstance(expr, ast.Name)
            and expr.id in self.scope.params
            and expr.id not in self.scope.rebound
        ):
            return False
        self.indexer.out.param_dynamics.append(
            _ParamDynamic(self.source, expr.id, kind, base, self.scope, detail)
        )
        return True

    def _forwarded_name(self, expr: ast.expr) -> bool:
        """``getattr(x, name)`` inside ``__getattr__``/``__getattribute__``
        with ``name`` the method's own name parameter: the method runs only
        for an access ``obj.<name>``, and every access site records ``<name>``
        itself (resolved, or as a name-bounded reference that reaches
        whatever this lookup can), so it is not a dynamic reference."""
        method = self.scope.method.rsplit(".", 1)[-1]
        if method not in ("__getattr__", "__getattribute__") or not self.scope.self_class:
            return False
        params = [p for p, i in self.scope.params.items() if i == 1]
        return (
            isinstance(expr, ast.Name)
            and bool(params)
            and expr.id == params[0]
            and expr.id not in self.scope.rebound
        )

    def _getattr(self, node: ast.Call) -> None:
        if len(node.args) < 2:
            return
        if self._forwarded_name(node.args[1]):
            return
        names = self.scope.string_candidates(node.args[1])
        base = _flatten_chain(node.args[0])
        if names is None:
            prefix = _string_prefix(node.args[1])
            if prefix is not None:
                # ``getattr(obj, f"pytest_{name}")``: every in-scope attribute
                # name with that prefix is a candidate, nothing else.
                names = self.indexer.symbol_names_with_prefix(prefix)
            elif not self._param_dynamic(node.args[1], "getattr", base, "getattr(<non-literal>)"):
                self._dynamic("getattr(<non-literal>)")
                return
            else:
                return
        for name in names:
            if base is None:
                # Unknown receiver, known attribute name: bounded like ``obj.name``.
                self.indexer.out.unresolved.add(
                    UnresolvedReference(
                        self.source, UNRESOLVED_ATTRIBUTE, name, f"getattr(..., {name!r})"
                    )
                )
            else:
                # The attribute's value is used, so a function or class
                # found this way may be called from here with anything.
                self._resolve(base + [name])
                self._escape(self.indexer.resolve_chain(base + [name], self.scope))

    def _canonical_name(self, parts: list[str]) -> str | None:
        """The dotted name a callee chain refers to through the scope's import
        bindings (``import_module`` from ``from importlib import
        import_module`` is ``importlib.import_module``); None for a local."""
        head = parts[0]
        binding = self.scope.local_imports.get(head)
        if binding is None:
            if head in self.scope.locals:
                return None
            binding = self.scope.module.imports.get(head)
        if binding is None:
            return ".".join(parts)
        base = binding.module if binding.attr is None else f"{binding.module}.{binding.attr}"
        return ".".join([base, *parts[1:]])

    def _import_package(self, node: ast.Call) -> str | None:
        """``import_module``'s ``package`` argument when it is known: a
        literal, ``__name__`` (this module) or ``__package__``."""
        arg = node.args[1] if len(node.args) > 1 else None
        for k in node.keywords:
            if k.arg == "package":
                arg = k.value
        if arg is None:
            return None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        module = self.scope.module
        # Under a ``DIR=PREFIX`` root the indexed name is diffcone's own, not
        # the one Python gives the module at runtime.
        roots = [split_root(r)[0] for r in self.indexer.snapshot.source_roots]
        if module_name_for(module.path, roots) != module.name:
            return None
        if isinstance(arg, ast.Name) and not self._is_shadowed(arg.id):
            if arg.id == "__name__":
                return module.name
            if arg.id == "__package__":
                return module.name if module.is_package else module.name.rpartition(".")[0]
        return None

    def _import_module(self, node: ast.Call, name: str) -> None:
        if not node.args:
            return
        package = self._import_package(node) if name == "importlib.import_module" else None
        names = self.scope.string_candidates(node.args[0])
        if names is not None and package is not None:
            resolved = [_resolve_relative_name(n, package) for n in names]
            if all(r is not None for r in resolved):
                names = tuple(r for r in resolved if r is not None)
        if names is None:
            prefix = _string_prefix(node.args[0])
            if prefix is not None and prefix.startswith(".") and package is not None:
                prefix = _resolve_relative_name(prefix, package, prefix=True)
            if prefix is not None and not prefix.startswith("."):
                # ``import_module(f"attr.{name}")``: every in-scope module under
                # the prefix may be imported; nothing outside it can be.
                names = self.indexer.modules_with_prefix(prefix)
                if not names:
                    self.indexer.out.external.add(ExternalReference(self.source, prefix + "*"))
                    return
        if names is None or any(n.startswith(".") for n in names):
            if names is not None or not self._param_dynamic(
                node.args[0], "import", None, f"{name}(<non-literal>)"
            ):
                self._dynamic(f"{name}(<non-literal>)")
            return
        for module in names:
            self.indexer._module_import_edge(self.source, module)


def build_index(snapshot: Snapshot, module_cache=None) -> SourceIndex:
    return Indexer(snapshot, module_cache=module_cache).build()
