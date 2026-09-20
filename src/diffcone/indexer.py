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
* ``importlib.import_module`` / ``getattr`` with literal arguments.

* attribute lookup on classes through their in-scope MRO (``self.m`` for an
  inherited ``m``, ``Sub.m``, ``super().m``).

Deliberately unsupported: type inference, dynamic dispatch on unknown
receivers, instance attributes, decorators that rewrite call targets.
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
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
    AnalysisError,
    Edge,
    ExternalReference,
    SourceIndex,
    Symbol,
    UnresolvedReference,
)
from diffcone.snapshot import Snapshot, module_name_for

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
    base_exprs: list[ast.expr] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)  # in-scope base class ids, in order
    complete: bool = True  # False when some base is external/dynamic/unresolved
    bases_state: int = 0  # 0 pending, 1 resolving, 2 resolved
    mro: list[str] | None = None
    in_mro: bool = False  # cycle guard while linearising


@dataclass
class ModuleScope:
    name: str
    path: str
    is_package: bool
    tree: ast.Module
    imports: dict[str, ImportBinding] = field(default_factory=dict)
    star_imports: list[str] = field(default_factory=list)
    bindings: set[str] = field(default_factory=set)
    members: dict[str, str] = field(default_factory=dict)
    import_nodes: list[ast.stmt] = field(default_factory=list)
    # NAME = "lit" / ("a", "b") at module level: string sets a name may hold.
    literal_names: dict[str, tuple[str, ...] | None] = field(default_factory=dict)


@dataclass
class Scope:
    module: ModuleScope
    local_imports: dict[str, ImportBinding] = field(default_factory=dict)
    locals: set[str] = field(default_factory=set)
    self_name: str | None = None
    self_class: str | None = None
    # Function-level ``name = "lit"`` / ``for name in ("a", "b")`` bindings:
    # the string values a name may hold, or None when any binding is not literal.
    literal_names: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
    # The enclosing function's own parameters (only in that function's scope,
    # not in nested scopes): name -> positional index or None for keyword-only.
    params: dict[str, int | None] = field(default_factory=dict)

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


def _literal_strings(expr: ast.expr) -> tuple[str, ...] | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return (expr.value,)
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
    param: str
    kind: str  # "getattr" | "import"
    base: list[str] | None  # receiver chain for getattr, when it is a name chain
    scope: Scope
    detail: str


class Indexer:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.index = SourceIndex(snapshot=snapshot.info)
        self.index.errors.extend(snapshot.errors)
        self.scopes: dict[str, ModuleScope] = {}
        self.class_scopes: dict[str, ClassScope] = {}
        self._module_prefixes: set[str] = set()
        self._bases_final = False
        # Interprocedural literal propagation for ``getattr(x, param)``:
        # per function, what its resolved call sites pass; whether it is used
        # other than by a direct call; and the pending parameter-driven uses.
        self.call_sites: dict[str, list[_CallSite]] = defaultdict(list)
        self.escapes: set[str] = set()
        self.func_params: dict[str, _FuncParams] = {}
        self.param_dynamics: list[_ParamDynamic] = []
        # Transitive in-scope descendants per class, built once bases are final.
        self._descendants: dict[str, tuple[str, ...]] = {}

    # -- pass 1 ---------------------------------------------------------------

    def build(self) -> SourceIndex:
        for path in sorted(self.snapshot.files):
            module = module_name_for(path, self.snapshot.source_roots)
            if module is None:
                continue
            if module in self.scopes:
                other = self.scopes[module].path
                self._error(path, f"module {module!r} is also defined by {other}")
                continue
            try:
                source = self.snapshot.files[path].decode("utf-8")
                tree = ast.parse(source, filename=path)
            except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                self._error(path, f"cannot parse: {exc}")
                self.index.failed_modules.add(module)
                continue
            scope = ModuleScope(
                name=module, path=path, is_package=path.endswith("__init__.py"), tree=tree
            )
            self.scopes[module] = scope
            self.index.modules.add(module)
        for module in self.index.modules:
            parts = module.split(".")
            for i in range(1, len(parts) + 1):
                self._module_prefixes.add(".".join(parts[:i]))
        for module in sorted(self.scopes):
            self._index_module(self.scopes[module])
        for class_id in sorted(self.class_scopes):
            self._ensure_bases(class_id)
        self._bases_final = True  # MROs may be memoised from here on
        self._build_descendants()
        for module in sorted(self.scopes):
            self._resolve_module(self.scopes[module])
        self._resolve_param_dynamics()
        return self.index

    def _resolve_param_dynamics(self) -> None:
        """Expand ``getattr(x, p)`` / ``import_module(p)`` where ``p`` is a
        parameter, using the literal strings every resolved call site passes.
        A function that escapes (used as a value, or whose name occurs as an
        unresolved reference so callers may be unknown) or has an unbounded
        call site stays dynamic."""
        unresolved_names = {u.name for u in self.index.unresolved if u.name}
        for pd in self.param_dynamics:
            info = self.func_params.get(pd.function)
            symbol = self.index.symbols.get(pd.function)
            candidates: list[str] | None = []
            sites = self.call_sites.get(pd.function, [])
            if (
                info is None
                or symbol is None
                or pd.function in self.escapes
                or symbol.name in unresolved_names
                or not sites
            ):
                candidates = None
            else:
                for site in sites:
                    values = site.value_for(pd.param, info)
                    if values is None:
                        candidates = None
                        break
                    candidates.extend(values)
            if candidates is None:
                self.index.unresolved.add(
                    UnresolvedReference(pd.function, UNRESOLVED_DYNAMIC, "", pd.detail)
                )
                continue
            for name in dict.fromkeys(candidates):
                if pd.kind == "import":
                    if name.startswith("."):
                        self.index.unresolved.add(
                            UnresolvedReference(pd.function, UNRESOLVED_DYNAMIC, "", pd.detail)
                        )
                    else:
                        self._module_import_edge(pd.function, name)
                elif pd.base is None:
                    self.index.unresolved.add(
                        UnresolvedReference(
                            pd.function, UNRESOLVED_ATTRIBUTE, name, f"getattr(..., {name!r})"
                        )
                    )
                else:
                    node = self.resolve_chain(pd.base + [name], pd.scope)
                    self._record(pd.function, node, chain=".".join(pd.base + [name]))

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
            return False
        self.index.symbols[symbol.id] = symbol
        return True

    def _index_module(self, scope: ModuleScope) -> None:
        stmts = list(iter_scope_statements(scope.tree.body))
        scope.import_nodes = [s for s in stmts if isinstance(s, (ast.Import, ast.ImportFrom))]
        for node in scope.import_nodes:
            self._register_imports(scope, node, scope.imports, scope.star_imports)
        for stmt in stmts:
            if not isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
                scope.bindings |= _collect_store_names(stmt)
        scope.literal_names = _collect_literal_bindings(scope.tree, {})
        imports = tuple(sorted(_canonical_imports(scope)))
        self._add_symbol(
            Symbol(
                id=scope.name,
                kind=MODULE,
                module=scope.name,
                name=scope.name.rsplit(".", 1)[-1],
                path=scope.path,
                lineno=1,
                body_hash=hash_scope_body(_split_docstring(scope.tree.body)[1], strip_imports=True),
                docstring_hash=_docstring_hash([scope.tree.body]),
                definition_hash=_digest("\n".join(imports)),
                container=None,
                line_ranges=((1, _end_line(scope.tree)),),
                imports=imports,
            )
        )
        self._index_definitions(scope, scope.tree.body, scope.name, scope.members, None)

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
                definition_parts: list[ast.AST] = []
                for n in nodes:
                    definition_parts += list(n.bases) + list(n.keywords) + list(n.decorator_list)
                definition_hash = _digest(
                    hash_nodes(definition_parts) + "|" + ",".join(member_names)
                )
                symbol = Symbol(
                    id=symbol_id,
                    kind=CLASS,
                    module=scope.name,
                    name=name,
                    path=scope.path,
                    lineno=first.lineno,
                    body_hash=_digest(
                        "\n".join(
                            hash_scope_body(_split_docstring(n.body)[1], strip_imports=False)
                            for n in nodes
                        )
                    ),
                    definition_hash=definition_hash,
                    container=container_id,
                    line_ranges=tuple((_start_line(n), _end_line(n)) for n in nodes),
                    docstring_hash=_docstring_hash([n.body for n in nodes]),
                )
                if not self._add_symbol(symbol):
                    continue
                members[name] = symbol_id
                self.index.edges.add(Edge(symbol_id, container_id, DEFINED_IN))
                cscope = ClassScope(id=symbol_id, module=scope, enclosing=class_scope)
                for n in nodes:
                    cscope.base_exprs.extend(n.bases)
                self.class_scopes[symbol_id] = cscope
                for n in nodes:
                    for stmt in iter_scope_statements(n.body):
                        if not isinstance(stmt, DEF_NODES):
                            cscope.bindings |= _collect_store_names(stmt)
                    self._index_definitions(scope, n.body, symbol_id, cscope.members, cscope)
            else:
                nodes = funcs[name]
                first = nodes[0]
                definition_parts = []
                for n in nodes:
                    definition_parts.append(n.args)
                    definition_parts += list(n.decorator_list)
                    if n.returns is not None:
                        definition_parts.append(n.returns)
                definition_hash = _digest(
                    hash_nodes(definition_parts) + "|" + ",".join(type(n).__name__ for n in nodes)
                )
                symbol = Symbol(
                    id=symbol_id,
                    kind=METHOD if class_scope is not None else FUNCTION,
                    module=scope.name,
                    name=name,
                    path=scope.path,
                    lineno=first.lineno,
                    body_hash=_digest(
                        "\n".join(hash_nodes(list(_split_docstring(n.body)[1])) for n in nodes)
                    ),
                    docstring_hash=_docstring_hash([n.body for n in nodes]),
                    definition_hash=definition_hash,
                    container=container_id,
                    line_ranges=tuple((_start_line(n), _end_line(n)) for n in nodes),
                )
                if not self._add_symbol(symbol):
                    continue
                members[name] = symbol_id
                self.index.edges.add(Edge(symbol_id, container_id, DEFINED_IN))

    # -- inheritance ----------------------------------------------------------

    def _ensure_bases(self, class_id: str) -> None:
        """Resolve a class's bases on demand (a dotted base such as
        ``Zed.Inner`` may need another class's MRO first)."""
        cscope = self.class_scopes[class_id]
        if cscope.bases_state:
            return
        cscope.bases_state = 1
        for expr in cscope.base_exprs:
            node = self._resolve_base_expr(expr, cscope)
            self._record(cscope.id, node, chain=_chain_text(expr))
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
        cscope.bases_state = 2

    def _resolve_base_expr(self, expr: ast.expr, cscope: ClassScope) -> Node:
        """A base name is looked up in the enclosing class body (for nested
        classes) and then in the module, as Python does when the class
        statement executes."""
        parts = _flatten_chain(expr)
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
        for stmt in top_level:
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
        fscope = Scope(
            module=scope,
            locals=_LocalBindings().collect(node),
            literal_names=_collect_literal_bindings(node, scope.literal_names),
        )
        bound_method = class_scope is not None and not _is_staticmethod(node)
        if bound_method:
            params = node.args.posonlyargs + node.args.args
            if params:
                fscope.self_name = params[0].arg
                fscope.self_class = class_scope.id
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
        self.func_params[symbol_id] = _FuncParams(
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
        collector = _ReferenceCollector(self, symbol_id, fscope)
        collector.visit(node.args)
        for dec in node.decorator_list:
            collector.visit(dec)
        if node.returns is not None:
            collector.visit(node.returns)
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
                self.index.unresolved.add(
                    UnresolvedReference(
                        source, UNRESOLVED_ATTRIBUTE, module.rsplit(".", 1)[-1], f"import {module}"
                    )
                )
            else:
                self.index.external.add(ExternalReference(source, module))
            return
        parts = module.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if prefix in self.scopes and prefix != source:
                self.index.edges.add(Edge(source, prefix, IMPORTS))

    # -- resolution --------------------------------------------------------------

    def _lookup_base(self, name: str, scope: Scope) -> Node:
        if scope.self_name is not None and name == scope.self_name and scope.self_class:
            return Resolved(scope.self_class, receiver=True)
        if name in scope.local_imports:
            return self._import_binding_node(scope.local_imports[name])
        if name in scope.locals:
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
            if self._module_in_scope(sub):
                return ModuleNode(sub)
            target = self.scopes.get(node.module)
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

    def _record(self, source: str, node: Node, kind: str = REFERENCES, chain: str = "") -> None:
        if node is None:
            return
        if isinstance(node, Resolved):
            if node.symbol != source:
                self.index.edges.add(Edge(source, node.symbol, kind, node.detail))
            if node.uncertain_attr:
                self.index.unresolved.add(
                    UnresolvedReference(source, UNRESOLVED_ATTRIBUTE, node.uncertain_attr, chain)
                )
            for symbol_id, detail in node.overrides:
                if symbol_id != source:
                    label = f"override:{detail}" if detail else "override"
                    self.index.edges.add(Edge(source, symbol_id, kind, label))
            if kind == REFERENCES and not node.detail and node.symbol in self.class_scopes:
                # Using a class (``Foo(...)``, subclassing) runs its constructor.
                init = self.lookup_in_class(node.symbol, "__init__")
                if isinstance(init, Resolved) and init.symbol != source:
                    self.index.edges.add(Edge(source, init.symbol, REFERENCES, "constructor"))
        elif isinstance(node, ModuleNode):
            if node.module in self.scopes and node.module != source:
                self.index.edges.add(Edge(source, node.module, kind, "module"))
        elif isinstance(node, External):
            self.index.external.add(ExternalReference(source, node.module))
        elif isinstance(node, Unresolved):
            self.index.unresolved.add(UnresolvedReference(source, node.kind, node.name, chain))


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


def _end_line(node: ast.AST) -> int:
    """Last source line of a definition; a Module has no position of its own."""
    if isinstance(node, ast.Module):
        return node.body[-1].end_lineno or 1 if node.body else 1
    return node.end_lineno or node.lineno  # type: ignore[attr-defined]


def _chain_text(expr: ast.expr) -> str:
    parts = _flatten_chain(expr)
    return ".".join(parts) if parts else "<expr>"


def _is_staticmethod(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "staticmethod":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "staticmethod":
            return True
    return False


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

    def _push(
        self, bound: set[str], literals: dict[str, tuple[str, ...] | None] | None = None
    ) -> Scope:
        outer = self.scope
        literal_names = {k: v for k, v in outer.literal_names.items() if k not in bound}
        literal_names.update(literals or {})
        self.scope = Scope(
            module=outer.module,
            local_imports=outer.local_imports,
            locals=outer.locals | bound,
            self_name=None if outer.self_name in bound else outer.self_name,
            self_class=None if outer.self_name in bound else outer.self_class,
            literal_names=literal_names,
            params={},  # a nested scope's names are not the enclosing function's parameters
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
        outer = self._push(
            _LocalBindings().collect(node),
            _collect_literal_bindings(node, self.scope.module.literal_names),
        )
        try:
            for arg in ast.walk(node.args):
                if isinstance(arg, ast.arg) and arg.annotation is not None:
                    self.visit(arg.annotation)
            if node.returns is not None:
                self.visit(node.returns)
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
        node = self.indexer.resolve_chain(parts, self.scope)
        self.indexer._record(self.source, node, kind=kind, chain=".".join(parts))

    def _dynamic(self, detail: str) -> None:
        self.indexer.index.unresolved.add(
            UnresolvedReference(self.source, UNRESOLVED_DYNAMIC, "", detail)
        )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._resolve([node.id])
            self._mark_escape(node, [node.id])

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts = _flatten_chain(node)
        if parts is not None:
            self._resolve(parts)
            self._mark_escape(node, parts)
            return
        if self._is_zero_arg_super(node.value) and self.scope.self_class is not None:
            # ``super().m``: next definition of ``m`` in the enclosing class's MRO.
            target = self.indexer.lookup_in_class(self.scope.self_class, node.attr, skip_self=True)
            self.indexer._record(self.source, target, chain=f"super().{node.attr}")
            return
        # ``Foo().run``, ``items[0].run``, ``make().run``: the base value is
        # unknown, but the attribute name still bounds what it may refer to.
        self.indexer.index.unresolved.add(
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

    def visit_Call(self, node: ast.Call) -> None:
        parts = _flatten_chain(node.func)
        if parts is not None:
            name = ".".join(parts)
            if len(parts) == 1 and parts[0] in DYNAMIC_CALLS and not self._is_shadowed(name):
                self._dynamic(f"{name}()")
            elif len(parts) == 1 and parts[0] == "getattr" and not self._is_shadowed(name):
                self._getattr(node)
            elif name in ("importlib.import_module", "importlib.__import__"):
                self._import_module(node, name)
            self._record_call_site(node, parts)
        self._call_func = node.func
        self.generic_visit(node)

    _call_func: ast.expr | None = None

    def _record_call_site(self, node: ast.Call, parts: list[str]) -> None:
        target = self.indexer.resolve_chain(parts, self.scope)
        if not (isinstance(target, Resolved) and not target.detail):
            return
        symbol = self.indexer.index.symbols.get(target.symbol)
        if symbol is None or symbol.kind not in (FUNCTION, METHOD):
            return
        unbounded = any(isinstance(a, ast.Starred) for a in node.args) or any(
            k.arg is None for k in node.keywords
        )
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
        self.indexer.call_sites[symbol.id].append(site)
        # A dispatched call may land on any override: they share the call site.
        for override_id, detail in target.overrides:
            if not detail:
                self.indexer.call_sites[override_id].append(site)

    def _mark_escape(self, node: ast.expr, parts: list[str]) -> None:
        """A function referenced other than as the callee of a call may be
        called from anywhere with anything."""
        if node is self._call_func:
            return
        target = self.indexer.resolve_chain(parts, self.scope)
        if isinstance(target, Resolved) and not target.detail:
            symbol = self.indexer.index.symbols.get(target.symbol)
            if symbol is not None and symbol.kind in (FUNCTION, METHOD):
                self.indexer.escapes.add(symbol.id)
                for override_id, detail in target.overrides:
                    if not detail:
                        self.indexer.escapes.add(override_id)

    def _param_dynamic(
        self, expr: ast.expr, kind: str, base: list[str] | None, detail: str
    ) -> bool:
        """Defer a dynamic use whose name is one of the enclosing function's
        parameters; returns False when that does not apply."""
        if not (isinstance(expr, ast.Name) and expr.id in self.scope.params):
            return False
        self.indexer.param_dynamics.append(
            _ParamDynamic(self.source, expr.id, kind, base, self.scope, detail)
        )
        return True

    def _getattr(self, node: ast.Call) -> None:
        if len(node.args) < 2:
            return
        names = self.scope.string_candidates(node.args[1])
        base = _flatten_chain(node.args[0])
        if names is None:
            if not self._param_dynamic(node.args[1], "getattr", base, "getattr(<non-literal>)"):
                self._dynamic("getattr(<non-literal>)")
            return
        for name in names:
            if base is None:
                # Unknown receiver, known attribute name: bounded like ``obj.name``.
                self.indexer.index.unresolved.add(
                    UnresolvedReference(
                        self.source, UNRESOLVED_ATTRIBUTE, name, f"getattr(..., {name!r})"
                    )
                )
            else:
                self._resolve(base + [name])

    def _import_module(self, node: ast.Call, name: str) -> None:
        if not node.args:
            return
        names = self.scope.string_candidates(node.args[0])
        if names is None or any(n.startswith(".") for n in names):
            if names is not None or not self._param_dynamic(
                node.args[0], "import", None, f"{name}(<non-literal>)"
            ):
                self._dynamic(f"{name}(<non-literal>)")
            return
        for module in names:
            self.indexer._module_import_edge(self.source, module)


def build_index(snapshot: Snapshot) -> SourceIndex:
    return Indexer(snapshot).build()
