"""Pass 1: a module's statements and definitions become symbols, with
their hashes, line spans and import tables."""

from __future__ import annotations

import ast
from collections import defaultdict

from diffcone.indexer.definitions import (
    _annotated_args,
    _canonical_imports,
    _end_line,
    _flatten_chain,
    _future_annotations,
    _has_annotations,
    _import_layout,
    _inert_def,
    _is_inert_decorator,
    _is_literal,
    _is_special_method,
    _start_line,
    _variable_statements,
)
from diffcone.indexer.literals import (
    _collect_literal_bindings,
    _collect_store_names,
    _module_mutations,
)
from diffcone.indexer.scopes import (
    ClassScope,
    ImportBinding,
    ModuleScope,
    VariableStatement,
    _absolute_module,
)
from diffcone.indexer.state import IndexerState
from diffcone.indexer.syntax import (
    DEF_NODES,
    FUNC_NODES,
    _digest,
    _docstring_hash,
    _split_docstring,
    hash_nodes,
    hash_scope_body,
    iter_scope_statements,
)
from diffcone.model import (
    CLASS,
    DEFINED_IN,
    FUNCTION,
    METHOD,
    MODULE,
    REFERENCES,
    VARIABLE,
    Edge,
    Symbol,
)
from diffcone.snapshot import member_symbol_id


def _doc_reads(body: list[ast.stmt]):
    """``__doc__`` names and attributes, and ``getdoc(...)`` calls, in a
    scope's own statements (not in nested definitions, which are symbols
    of their own)."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, DEF_NODES):
            # Decorators, defaults and bases run in this scope; the body not.
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend([*node.bases, *node.keywords])
            else:
                stack.extend([*node.args.defaults, *(d for d in node.args.kw_defaults if d)])
            continue
        if isinstance(node, ast.Name) and node.id == "__doc__":
            yield node
        elif isinstance(node, ast.Attribute) and node.attr == "__doc__":
            yield node
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "getdoc":
                yield node
        stack.extend(ast.iter_child_nodes(node))


def _reads_docstrings(body: list[ast.stmt]) -> bool:
    return next(_doc_reads(body), None) is not None


def _names_module_doc(body: list[ast.stmt]) -> bool:
    return any(isinstance(n, ast.Name) for n in _doc_reads(body))


class FirstPass(IndexerState):
    """Pass 1: modules and their definitions into symbols."""

    def _module_statements(
        self, scope: ModuleScope, *, register_imports: bool
    ) -> tuple[list[ast.stmt], list[ast.stmt], dict[str, VariableStatement]]:
        """(all scope statements, body without docstring, variable statements)
        of a parsed module; records the import statements and, unless the
        import table was served by the cache, fills it."""
        assert scope.tree is not None
        stmts = list(iter_scope_statements(scope.tree.body))
        scope.import_nodes = [s for s in stmts if isinstance(s, (ast.Import, ast.ImportFrom))]
        if register_imports:
            for node in scope.import_nodes:
                self._register_imports(
                    scope, node, scope.imports, scope.star_imports, scope.alt_imports
                )
        body = _split_docstring(scope.tree.body)[1]
        return stmts, body, _variable_statements(scope, body)

    def _index_module(self, scope: ModuleScope) -> None:
        assert scope.tree is not None
        stmts, body, variable_stmts = self._module_statements(scope, register_imports=True)
        for stmt in stmts:
            if not isinstance(stmt, DEF_NODES + (ast.Import, ast.ImportFrom)):
                scope.bindings |= _collect_store_names(stmt)
        scope.literal_names = _collect_literal_bindings(scope.tree, {})
        scope.mutations = frozenset(_module_mutations(scope.tree))
        imports = tuple(sorted(_canonical_imports(scope)))
        layout = _import_layout(scope)
        # A variable statement is its own symbol, so it leaves the module's
        # body hash; one rebinding a def or class name (``helper = 3`` after
        # ``def helper``) is not a variable symbol and stays in it.
        defined = {s.name for s in stmts if isinstance(s, DEF_NODES)}
        own_symbol = {id(stmt) for name, stmt in variable_stmts.items() if name not in defined}
        module_body_hash = hash_scope_body(
            [s for s in body if id(s) not in own_symbol], strip_imports=True
        )
        module_doc_hash = _docstring_hash([scope.tree.body])
        module_reads_doc = _reads_docstrings(scope.tree.body)
        if module_reads_doc and _names_module_doc(scope.tree.body):
            # ``ArgumentParser(description=__doc__)``: import-time code reads
            # the module's own docstring.
            module_body_hash = _digest(module_body_hash + "|doc:" + module_doc_hash)
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
                definition_hash=_digest("\n".join(imports) + "|layout|" + "\n".join(layout)),
                container=None,
                line_ranges=((1, _end_line(scope.tree)),),
                imports=imports,
                import_layout=layout,
                reads_docstrings=module_reads_doc,
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
                continue  # also a def/class: kept in the module body hash above
            symbol_id = self._member_id(scope.name, name)
            value = stmt.value
            assert value is not None  # _variable_statements keeps assignments with a value
            symbol = Symbol(
                id=symbol_id,
                kind=VARIABLE,
                module=scope.name,
                name=name,
                path=scope.path,
                lineno=stmt.lineno,
                body_hash=hash_nodes([value, *mutators.get(name, [])]),
                # An annotation is evaluated at import unless deferred: a
                # change to it is a definition change (see classify).
                annotation_hash=(
                    _digest(hash_nodes([stmt.annotation]))
                    if isinstance(stmt, ast.AnnAssign)
                    else ""
                ),
                deferred_annotations=_future_annotations(scope),
                definition_hash="",
                container=scope.name,
                line_ranges=tuple(
                    (s.lineno, _end_line(s)) for s in (stmt, *mutators.get(name, []))
                ),
                # Binding a literal runs no code when the module is imported;
                # only readers of the value can observe the change. ``__all__``
                # is not inert: it decides what ``from m import *`` binds.
                inert_definition=(
                    name != "__all__" and not mutators.get(name) and _is_literal(value)
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
        alternatives: dict[str, list[ImportBinding]] | None = None,
    ) -> None:
        def bind(name: str, binding: ImportBinding) -> None:
            previous = table.get(name)
            if alternatives is not None and previous is not None and previous != binding:
                if previous not in alternatives.setdefault(name, []):
                    alternatives[name].append(previous)
            table[name] = binding

        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bind(alias.asname, ImportBinding(alias.name, None))
                else:
                    bind(alias.name.split(".")[0], ImportBinding(alias.name.split(".")[0], None))
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(scope, node.module, node.level)
            for alias in node.names:
                if alias.name == "*":
                    stars.append(base)
                else:
                    bind(alias.asname or alias.name, ImportBinding(base, alias.name))

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
                if any(n.decorator_list or n.keywords for n in nodes):
                    # A decorator or metaclass receives the class with its
                    # docstring, and may run it (a ``@doc``-style formatter).
                    definition_hash = _digest(definition_hash + "|doc:" + doc_hash)
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
                    reads_docstrings=any(_reads_docstrings(n.body) for n in nodes),
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
                        definition_parts.append(n.args)
                        definition_parts += list(n.decorator_list)
                        if n.returns is not None:
                            annotation_parts.append(n.returns)
                    # The definition hash excludes annotations: detach them
                    # while hashing (no copy of the tree) and restore them.
                    detached = [a for n in nodes for a in _annotated_args(n.args)]
                    annotations = [a.annotation for a in detached]
                    for a in detached:
                        a.annotation = None
                    try:
                        definition = hash_nodes(definition_parts)
                    finally:
                        for a, annotation in zip(detached, annotations, strict=True):
                            a.annotation = annotation
                    annotation_parts = [
                        *(a for a in annotations if a is not None),
                        *annotation_parts,
                    ]
                    return (
                        _digest(hash_nodes(annotation_parts)),
                        _digest(
                            "\n".join(hash_nodes(list(_split_docstring(n.body)[1])) for n in nodes)
                        ),
                        _digest(definition + "|" + ",".join(type(n).__name__ for n in nodes)),
                        _docstring_hash([n.body for n in nodes]),
                    )

                annotation_hash, body_hash, definition_hash, doc_hash = function_hashes()
                if not all(_is_inert_decorator(d, scope) for n in nodes for d in n.decorator_list):
                    # The decorator receives the function with its docstring,
                    # and may run it (pandas' ``@doc`` formats it at import).
                    definition_hash = _digest(definition_hash + "|doc:" + doc_hash)
                deferred = (
                    _future_annotations(scope)
                    and all(_is_inert_decorator(d, scope) for n in nodes for d in n.decorator_list)
                    and (class_scope is None or class_scope.plain)
                )
                inert = all(_inert_def(n, scope) for n in nodes) and (
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
                    reads_docstrings=any(_reads_docstrings(n.body) for n in nodes),
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
