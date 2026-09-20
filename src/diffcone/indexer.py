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

Deliberately unsupported: type inference, dynamic dispatch, inheritance
lookup, instance attributes, decorators that rewrite call targets.
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
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


def hash_scope_body(stmts: list[ast.stmt], strip_imports: bool) -> str:
    stripped = [_StripDefs(strip_imports).visit(copy.deepcopy(s)) for s in stmts]
    return hash_nodes([s for s in stripped if s is not None])


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
    members: dict[str, str] = field(default_factory=dict)  # name -> symbol id
    bindings: set[str] = field(default_factory=set)


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


@dataclass
class Scope:
    module: ModuleScope
    local_imports: dict[str, ImportBinding] = field(default_factory=dict)
    locals: set[str] = field(default_factory=set)
    self_name: str | None = None
    self_class: str | None = None


# Resolution results ---------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    symbol: str
    detail: str = ""


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


def _absolute_module(scope_module: ModuleScope, module: str | None, level: int) -> str:
    if level == 0:
        return module or ""
    parts = scope_module.name.split(".") if scope_module.name else []
    if not scope_module.is_package:
        parts = parts[:-1]
    drop = level - 1
    if drop:
        parts = parts[: len(parts) - drop] if drop <= len(parts) else []
    base = ".".join(parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


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


class Indexer:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.index = SourceIndex(revision=snapshot.revision, commit=snapshot.commit)
        self.scopes: dict[str, ModuleScope] = {}
        self.class_scopes: dict[str, ClassScope] = {}
        self._module_prefixes: set[str] = set()

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
        for module in sorted(self.scopes):
            self._resolve_module(self.scopes[module])
        return self.index

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
        self._add_symbol(
            Symbol(
                id=scope.name,
                kind=MODULE,
                module=scope.name,
                name=scope.name.rsplit(".", 1)[-1],
                path=scope.path,
                lineno=1,
                body_hash=hash_scope_body(scope.tree.body, strip_imports=True),
                definition_hash=hash_nodes(scope.import_nodes),
                container=None,
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
                        "\n".join(hash_scope_body(n.body, strip_imports=False) for n in nodes)
                    ),
                    definition_hash=definition_hash,
                    container=container_id,
                )
                if not self._add_symbol(symbol):
                    continue
                members[name] = symbol_id
                self.index.edges.add(Edge(symbol_id, container_id, DEFINED_IN))
                cscope = ClassScope(id=symbol_id)
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
                    body_hash=_digest("\n".join(hash_nodes(list(n.body)) for n in nodes)),
                    definition_hash=definition_hash,
                    container=container_id,
                )
                if not self._add_symbol(symbol):
                    continue
                members[name] = symbol_id
                self.index.edges.add(Edge(symbol_id, container_id, DEFINED_IN))

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
                for expr in list(stmt.bases) + list(stmt.keywords) + list(stmt.decorator_list):
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
        fscope = Scope(module=scope, locals=_LocalBindings().collect(node))
        if class_scope is not None and not _is_staticmethod(node):
            params = node.args.posonlyargs + node.args.args
            if params:
                fscope.self_name = params[0].arg
                fscope.self_class = class_scope.id
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
            return Resolved(scope.self_class)
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
                cscope = self.class_scopes[symbol.id]
                if attr in cscope.members:
                    return Resolved(cscope.members[attr])
                if attr in cscope.bindings:
                    return Resolved(symbol.id, detail=f"attribute:{attr}")
                return Unresolved(UNRESOLVED_ATTRIBUTE, attr)
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
        elif isinstance(node, ModuleNode):
            if node.module in self.scopes and node.module != source:
                self.index.edges.add(Edge(source, node.module, kind, "module"))
        elif isinstance(node, External):
            self.index.external.add(ExternalReference(source, node.module))
        elif isinstance(node, Unresolved):
            self.index.unresolved.add(UnresolvedReference(source, node.kind, node.name, chain))


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

    def _push(self, bound: set[str]) -> Scope:
        outer = self.scope
        self.scope = Scope(
            module=outer.module,
            local_imports=outer.local_imports,
            locals=outer.locals | bound,
            self_name=None if outer.self_name in bound else outer.self_name,
            self_class=None if outer.self_name in bound else outer.self_class,
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
        outer = self._push(_LocalBindings().collect(node))
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
        for gen in generators:
            bound |= _collect_store_names(gen.target)
        outer = self._push(bound)
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

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts = _flatten_chain(node)
        if parts is not None:
            self._resolve(parts)
            return
        # ``Foo().run``, ``items[0].run``, ``make().run``: the base value is
        # unknown, but the attribute name still bounds what it may refer to.
        self.indexer.index.unresolved.add(
            UnresolvedReference(self.source, UNRESOLVED_ATTRIBUTE, node.attr, f"<expr>.{node.attr}")
        )
        self.generic_visit(node)

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
        self.generic_visit(node)

    def _getattr(self, node: ast.Call) -> None:
        if len(node.args) < 2:
            return
        attr = node.args[1]
        if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
            self._dynamic("getattr(<non-literal>)")
            return
        base = _flatten_chain(node.args[0])
        if base is None:
            self.indexer.index.unresolved.add(
                UnresolvedReference(
                    self.source, UNRESOLVED_ATTRIBUTE, attr.value, f"getattr(..., {attr.value!r})"
                )
            )
            return
        self._resolve(base + [attr.value])

    def _import_module(self, node: ast.Call, name: str) -> None:
        if not node.args:
            return
        arg = node.args[0]
        literal = isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        if literal and not arg.value.startswith("."):
            self.indexer._module_import_edge(self.source, arg.value)
        else:
            self._dynamic(f"{name}(<non-literal>)")


def build_index(snapshot: Snapshot) -> SourceIndex:
    return Indexer(snapshot).build()
