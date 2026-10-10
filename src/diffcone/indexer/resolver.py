"""Pass 2: the in-scope class model (bases, MRO, overrides) and the
resolution of names and attribute chains into edges."""

from __future__ import annotations

import ast
from collections import defaultdict

from diffcone.indexer.definitions import (
    _EXPLICIT_SPECIAL_METHODS,
    _STRUCTURAL_BASES,
    _class_attributes,
    _flatten_chain,
    _future_annotations,
    _has_decorator,
    _is_inert_decorator,
    _is_special_method,
    _is_staticmethod,
    _rebound_names,
)
from diffcone.indexer.facts import _FuncParams, _Output
from diffcone.indexer.literals import NESTED_SCOPES, _LocalBindings
from diffcone.indexer.references import _ReferenceCollector
from diffcone.indexer.scopes import (
    ClassScope,
    External,
    ImportBinding,
    Local,
    ModuleNode,
    ModuleScope,
    Node,
    Resolved,
    Scope,
    Unresolved,
    _absolute_module,
    relative_import_escapes,
)
from diffcone.indexer.symbols import FirstPass
from diffcone.indexer.syntax import (
    BUILTIN_NAMES,
    DEF_NODES,
    FUNC_NODES,
    _digest,
    iter_scope_statements,
    type_param_exprs,
)
from diffcone.indexer.writes import returns_third_party
from diffcone.model import (
    CLASS,
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
    Edge,
    ExternalReference,
    Symbol,
    UnresolvedReference,
)


def _with_alternatives(bindings: list[Node]) -> Node:
    """The node for a name bound several ways at module level: the first
    binding (a definition, else the last import), carrying the in-scope
    symbols and modules of the others as alternatives. When the first one
    names nothing in scope (an external fallback tried first) an in-scope
    alternative takes its place, so the reference is not merely external."""
    in_scope: list[str] = []
    for node in bindings:
        if isinstance(node, Resolved):
            in_scope.append(node.symbol)
        elif isinstance(node, ModuleNode):
            in_scope.append(node.module)
    primary = bindings[0]
    if not isinstance(primary, (Resolved, ModuleNode)):
        primary = next((b for b in bindings if isinstance(b, Resolved)), primary)
    if not isinstance(primary, Resolved):
        return primary
    others = tuple(dict.fromkeys(i for i in in_scope if i != primary.symbol))
    if not others:
        return primary
    return Resolved(
        primary.symbol,
        primary.detail,
        primary.uncertain_attr,
        primary.receiver,
        primary.overrides,
        primary.also,
        others,
    )


def _definition_scope(scope: ModuleScope, class_scope: ClassScope | None) -> Scope:
    """Where a ``def`` or ``class`` statement's header (decorators, defaults,
    annotations, type parameters) is evaluated: the module, or the body of
    the class it stands in."""
    return Scope(
        module=scope,
        locals=set(class_scope.bindings) if class_scope is not None else set(),
        class_members=dict(class_scope.members) if class_scope is not None else {},
        class_level=class_scope is not None,
        # A class-level name shadows the module's literal of that name.
        literal_bound=(
            frozenset(class_scope.bindings) | frozenset(class_scope.members)
            if class_scope is not None
            else frozenset()
        ),
    )


def _outermost_calls(expr: ast.expr) -> list[ast.Call]:
    """The calls in ``expr`` that no other call encloses (a lambda's body
    does not run)."""
    found: list[ast.Call] = []
    stack: list[ast.AST] = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            found.append(node)
        elif not isinstance(node, ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))
    return sorted(found, key=lambda c: (c.lineno, c.col_offset))


class Resolver(FirstPass):
    """Pass 2: the class model, and references into edges."""

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

    def _returned_class(self, node: ast.AST, scope: Scope) -> tuple[str, ...] | None:
        """The classes ``node`` returns, when every ``return`` in it yields
        one: ``def make(): return Provider()`` yields that class, and a
        factory that picks between two yields both. One return that says
        something else, or none at all, says nothing -- a guess here would
        bind a class that never reaches the caller."""
        found: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, NESTED_SCOPES) and inner is not node:
                continue
            if not isinstance(inner, ast.Return) or inner.value is None:
                continue
            cls = self._expression_class(inner.value, scope)
            if cls is None:
                return None
            found.add(cls)
        return tuple(sorted(found)) or None

    def _expression_class(self, expr: ast.expr, scope: Scope) -> str | None:
        """The class an expression is an instance of, when it says so: ``C()``
        constructs one, ``C`` is the class itself."""
        target = expr.func if isinstance(expr, ast.Call) else expr
        parts = _flatten_chain(target)
        if parts is None:
            return None
        node = self.resolve_chain(parts, scope)
        if not isinstance(node, Resolved) or node.detail:
            return None
        symbol = self.index.symbols.get(node.symbol)
        return node.symbol if symbol is not None and symbol.kind == CLASS else None

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

    def _module_in_scope(self, name: str) -> bool:
        return name in self._module_prefixes

    def _resolve_module(self, scope: ModuleScope) -> None:
        assert scope.tree is not None  # parsed before a module is resolved
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
        # ``if __name__ == "__main__":`` runs when the module is the program,
        # not when it is imported: what only that block does is recorded as
        # such (SourceIndex.main_guarded), though it stays the module's code.
        main_out = _Output()
        for stmt in top_level:
            owner = variable_ids.get(id(stmt))
            if owner is not None:
                # The right-hand side's references belong to the variable symbol;
                # the calls in it run when the module is imported, so (as a
                # class body's) they are the module's too: a change to a
                # function ``X = set_mode("slow")`` calls reaches its importers.
                # (``ib = attrib`` runs nothing: the alias's users depend on it.)
                _ReferenceCollector(self, owner, module_scope, skip_defs=True).visit(stmt)
                value = getattr(stmt, "value", None)
                if isinstance(value, ast.expr):
                    for call in _outermost_calls(value):
                        collector.visit(call)
            elif _is_main_guard(stmt, scope):
                assert isinstance(stmt, ast.If)
                collector.visit(stmt.test)
                outer, self.out = self.out, main_out
                try:
                    for inner in stmt.body:
                        collector.visit(inner)
                finally:
                    self.out = outer
                for inner in stmt.orelse:
                    collector.visit(inner)
            else:
                collector.visit(stmt)
        self._main_guarded(scope.name, main_out)
        self._resolve_definitions(scope, scope.tree.body, scope.members, None)

    def _main_guarded(self, module: str, main_out: _Output) -> None:
        """Merge what a module's ``__main__`` block recorded, noting what the
        module's own code does only there: references, names and dynamic
        lookups (``main_guarded``); its writes of process state are dropped,
        since they never run at import."""
        own_edges = {(e.target, e.kind) for e in self.out.edges if e.source == module}
        own_names = {
            (u.kind, u.name or u.detail) for u in self.out.unresolved if u.symbol == module
        }
        own_targets = {target for target, _ in own_edges}
        for e in main_out.edges:
            if e.source == module and e.target not in own_targets:
                self.out.main_guarded.add((module, "edge", e.target))
        for u in main_out.unresolved:
            key = (u.kind, u.name or u.detail)
            if u.symbol == module and key not in own_names:
                self.out.main_guarded.add((module, *key))
        main_out.process_writes = {w for w in main_out.process_writes if w[0] != module}
        self.out.merge(main_out)

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
                class_level = Scope(
                    module=scope,
                    locals=set(cscope.bindings),
                    class_members=dict(cscope.members),
                    class_level=True,
                )
                # The class statement (bases, decorators, body) runs when the
                # module is imported, nested classes included: its references
                # are the class's and, as import-time code, the module's.
                collectors = [
                    _ReferenceCollector(self, source, class_level, skip_defs=True)
                    for source in (symbol_id, scope.name)
                ]
                for creator in (symbol_id, scope.name):
                    self.class_creation(creator, cscope.bases, stmt.keywords, Scope(module=scope))
                # Name-chain bases were resolved (and recorded) by _ensure_bases;
                # only dynamic base expressions still need their references collected.
                dynamic_bases = [b for b in stmt.bases if _flatten_chain(b) is None]
                for collector in collectors:
                    for expr in dynamic_bases + list(stmt.keywords) + list(stmt.decorator_list):
                        collector.visit(expr)
                    for inner in stmt.body:
                        if not isinstance(inner, DEF_NODES):
                            collector.visit(inner)
                # Type parameters' bounds and defaults are evaluated lazily,
                # where the class statement stands: the class's, not import-time.
                if bounds := type_param_exprs(stmt):
                    params = _ReferenceCollector(
                        self, symbol_id, _definition_scope(scope, class_scope), skip_defs=True
                    )
                    for expr in bounds:
                        params.visit(expr)
                # An import in the class body runs when the class is created,
                # at import: the class and the module depend on the imported
                # module's import-time code, as for a module-level import.
                for inner in iter_scope_statements(stmt.body):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        for source in (symbol_id, scope.name):
                            self._import_edges(source, scope, inner, local=True)
                attributes = _class_attributes(stmt)
                previous = self.out.class_attributes.get(symbol_id)
                if previous is not None:  # a conditional second definition
                    for name in previous.keys() | attributes.keys():
                        attributes[name] = _digest(
                            previous.get(name, "") + "|" + attributes.get(name, "")
                        )
                self.out.class_attributes[symbol_id] = attributes
                creators = list(stmt.decorator_list) + [k.value for k in stmt.keywords]
                if creators and self._decorators_may_read_docs(
                    creators, scope, Scope(module=scope)
                ):
                    self.out.doc_decorated.add(symbol_id)
                self.out.class_bases[symbol_id] = tuple(sorted(set(cscope.bases)))
                # Open: decorators or keywords, or a base outside the index
                # that may consume the body (``Enum``, a framework model); not
                # ``object``, ``Exception`` or a typing/abc base.
                if not cscope.plain or cscope.external_base:
                    self.out.open_classes.add(symbol_id)
                self._resolve_definitions(scope, stmt.body, cscope.members, cscope)
            elif isinstance(stmt, FUNC_NODES):
                symbol_id = members.get(stmt.name)
                if symbol_id is None:
                    continue
                self._resolve_function(scope, stmt, symbol_id, class_scope)

    def _record_decorations(
        self, symbol_id: str, decorators: list[ast.expr], module: ModuleScope, where: Scope
    ) -> None:
        """For each decorator not known inert: the in-scope function it
        resolves to and the in-scope object it is an attribute of
        (``show`` in ``@show.register(int)``, ``app`` in ``@app.command``).
        A decorator may keep the function it decorates there."""
        for dec in decorators:
            if _is_inert_decorator(dec, module):
                continue
            target = dec.func if isinstance(dec, ast.Call) else dec
            parts = _flatten_chain(target)
            if parts is None:
                continue
            decorator = self.resolve_chain(parts, where)
            decorator_id = decorator.symbol if isinstance(decorator, Resolved) else ""
            receiver_id = ""
            if len(parts) > 1:
                receiver = self.resolve_chain(parts[:-1], where)
                if isinstance(receiver, Resolved) and receiver.symbol != symbol_id:
                    receiver_id = receiver.symbol
            if decorator_id or receiver_id:
                self.out.decorations.add((symbol_id, decorator_id, receiver_id))

    def _decorators_may_read_docs(
        self, decorators: list[ast.expr], module: ModuleScope, where: Scope
    ) -> bool:
        """Whether any decorator (or metaclass) may read the docstring of what
        it decorates: one that resolves to nothing visible (external,
        unresolved, a value), or in-scope code that reads ``__doc__`` (the
        decorator itself, a factory whose wrapper does, a class's
        ``__init__``/``__new__``/``__call__``). Known inert ones never do."""
        for dec in decorators:
            if _is_inert_decorator(dec, module):
                continue
            target = dec.func if isinstance(dec, ast.Call) else dec
            parts = _flatten_chain(target)
            if parts is None:
                return True
            node = self.resolve_chain(parts, where)
            if not isinstance(node, Resolved) or node.detail:
                return True
            symbol = self.index.symbols.get(node.symbol)
            if symbol is None or symbol.kind not in (FUNCTION, METHOD, CLASS):
                return True
            if symbol.kind == CLASS:
                hooks = [
                    self.index.symbols.get(f"{symbol.id}.{m}")
                    for m in ("__init__", "__new__", "__call__")
                ]
                if any(h is not None and h.reads_docstrings for h in hooks):
                    return True
            elif symbol.reads_docstrings:
                return True
        return False

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
        # Defaults are evaluated where the ``def`` statement runs: in
        # ``def f(NAMES=NAMES)`` the default is the module's ``NAMES``.
        header = _definition_scope(scope, class_scope)
        defaults: dict[str, tuple[str, ...] | None] = {}
        n_defaults = len(node.args.defaults)
        for name, default in zip(
            positional[len(positional) - n_defaults :], node.args.defaults, strict=True
        ):
            defaults[name] = header.string_candidates(default)
        for a, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            if default is not None:
                defaults[a.arg] = header.string_candidates(default)
        self.out.func_params[symbol_id] = _FuncParams(
            positional=positional,
            bound=bound_method,
            defaults=defaults,
            has_varargs=node.args.vararg is not None or node.args.kwarg is not None,
        )
        returned = self._returned_class(node, fscope)
        if returned is not None:
            self.out.returns[symbol_id] = returned
        elif returns_third_party(self, node, fscope):
            self.out.external_returns.add(symbol_id)
        # Function-local imports are visible to the whole body.
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Import, ast.ImportFrom)):
                stars: list[str] = []
                self._register_imports(scope, inner, fscope.local_imports, stars)
                self._import_edges(symbol_id, scope, inner, local=True)
        # Decorators, defaults and annotations evaluate where the ``def``
        # statement runs, so the function's own parameters must not shadow
        # them (``def f(info=info)`` refers to the module-level ``info``).
        outer_scope = _definition_scope(scope, class_scope)
        outer = _ReferenceCollector(self, symbol_id, outer_scope, skip_defs=True)
        # Type parameters' bounds and defaults are evaluated lazily (when
        # ``__bound__`` is read): the function's references, not import-time.
        for expr in type_param_exprs(node):
            outer.visit(expr)
        # Decorators, defaults and eagerly evaluated annotations run when the
        # ``def`` does, i.e. when the module is imported (a decorator such as
        # ``@app.get("/")`` calls into a framework then): the module depends on
        # what they reference too.
        at_import = _ReferenceCollector(self, scope.name, outer_scope, skip_defs=True)
        for expr in (
            node.decorator_list
            + [d for d in node.args.defaults]
            + [d for d in node.args.kw_defaults if d is not None]
        ):
            at_import.visit(expr)
        if not _future_annotations(scope):
            for arg in ast.walk(node.args):
                if isinstance(arg, ast.arg) and arg.annotation is not None:
                    at_import.visit(arg.annotation)
            if node.returns is not None:
                at_import.visit(node.returns)
        for dec in node.decorator_list:
            outer.visit(dec)
        if self._decorators_may_read_docs(node.decorator_list, scope, outer_scope):
            self.out.doc_decorated.add(symbol_id)
        self._record_decorations(symbol_id, node.decorator_list, scope, outer_scope)
        all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for arg in all_args + [a for a in (node.args.vararg, node.args.kwarg) if a]:
            if arg.annotation is not None:
                outer.visit(arg.annotation)
        if node.returns is not None:
            outer.visit(node.returns)
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
            if relative_import_escapes(scope.name, scope.is_package, node.level):
                written = "." * node.level + (node.module or "")
                self.out.unresolved.add(
                    UnresolvedReference(
                        source,
                        UNRESOLVED_DYNAMIC,
                        written,
                        f"import from {written}: above the top-level package of {scope.name} "
                        "(check the source roots)",
                    )
                )
                return
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
                hidden = self._misrooted(module)
                if hidden is not None:
                    # ``from calc.ops import add`` while the roots name the
                    # package ``src.calc``: not a third-party import, an
                    # in-scope one discovery cannot follow. Unbounded.
                    self.out.unresolved.add(
                        UnresolvedReference(
                            source,
                            UNRESOLVED_DYNAMIC,
                            module,
                            f"import {module}: the analysed package is named {hidden}; the "
                            "source roots name it differently from the import (add a source "
                            "root for the directory holding it, e.g. --source-root src)",
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

    def resolve_dotted(self, chain: list[str]) -> Node:
        """An absolute dotted name as a string names it (``"pkg.mod.NAME"``):
        the longest in-scope module prefix, then attribute steps. None when
        no prefix is in scope (a third-party target)."""
        for i in range(len(chain), 0, -1):
            module = ".".join(chain[:i])
            if self._module_in_scope(module):
                node: Node = ModuleNode(module)
                for attr in chain[i:]:
                    if node is None:
                        return None
                    node = self._step(node, attr)
                return node
        return None

    def _misrooted(self, module: str) -> str | None:
        """The analysed package an absolute import most likely means when the
        source roots name it with a prefix: ``calc`` for ``src.calc`` when
        ``src`` is a plain directory, not a package (a src layout analysed
        with the root ``.``)."""
        if self._misrooted_names is None:
            names: dict[str, str] = {}
            for m in sorted(self.scopes):
                parts = m.split(".")
                for i in range(1, len(parts)):
                    if ".".join(parts[:i]) in self.scopes:
                        break
                    candidate = ".".join(parts[: i + 1])
                    if candidate in self.scopes:
                        names.setdefault(parts[i], candidate)
                        break
            self._misrooted_names = names
        return self._misrooted_names.get(module.split(".")[0])

    def _lookup_base(self, name: str, scope: Scope) -> Node:
        if scope.self_name is not None and name == scope.self_name and scope.self_class:
            return Resolved(scope.self_class, receiver=True)
        if name in scope.local_imports:
            return self._import_binding_node(scope.local_imports[name])
        if name in scope.class_members:
            # The member when it is defined above the use; the module's name
            # when it is defined below (order is not tracked): both.
            module_binding = self._lookup_in_module(scope.module, name, set())
            return _with_alternatives(
                [Resolved(scope.class_members[name])]
                + ([module_binding] if module_binding is not None else [])
            )
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
        # Star imports bind too, and the last binding wins at runtime, which
        # statement order alone does not settle (``from a import f`` then
        # ``from b import *``; Django-style settings star-importing a base
        # and then a local override): every in-scope candidate counts.
        stars, external = self._star_hits(target, name, seen) if target.star_imports else ([], None)
        if name in target.members or name in target.imports:
            # Every binding of the name: a definition beside an import of the
            # same name (a fallback), and imports one of which overwrites
            # another in a try/except or if/else.
            bindings: list[Node] = []
            if name in target.members:
                bindings.append(Resolved(target.members[name]))
            if name in target.imports:
                bindings.append(self._import_binding_node(target.imports[name]))
            for binding in target.alt_imports.get(name, ()):
                bindings.append(self._import_binding_node(binding))
            return _with_alternatives(bindings + stars)
        if name in target.variables:
            return _with_alternatives([Resolved(target.variables[name]), *stars])
        if name in target.bindings:
            return _with_alternatives([Resolved(target.name, detail=f"attribute:{name}"), *stars])
        if self._module_in_scope(f"{target.name}.{name}"):
            return ModuleNode(f"{target.name}.{name}")
        if stars:
            return _with_alternatives(stars)
        return External(external) if external is not None else None

    def _star_hits(
        self, target: ModuleScope, name: str, seen: set[str]
    ) -> tuple[list[Node], str | None]:
        """What each of ``target``'s star imports binds ``name`` to (in-scope
        hits, in order), and the first out-of-scope star import that might."""
        hits: list[Node] = []
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
                hits.append(found)
        return hits, external

    def _import_binding_node(self, binding: ImportBinding) -> Node:
        if not binding.module:
            # Bound by a relative import above the top-level package (see
            # _import_edges, which records the import as unbounded).
            return Unresolved(UNRESOLVED_ATTRIBUTE, binding.attr or "")
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
            # ``pkg/__init__.py: from pkg import ext`` with no ``ext`` module
            # (a compiled extension) resolves through itself: stop there.
            key = (binding.module, binding.attr)
            if key in self._resolving_bindings:
                return Unresolved(UNRESOLVED_ATTRIBUTE, binding.attr)
            self._resolving_bindings.add(key)
            try:
                node = self._step(node, binding.attr)
            finally:
                self._resolving_bindings.discard(key)
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
                shadow: Resolved | None = None
                if target is not None:
                    symbol_id = target.members.get(attr) or target.variables.get(attr)
                    if symbol_id is not None:
                        shadow = Resolved(symbol_id)
                    elif attr in target.imports:
                        # ``from .main import main``: the re-exported name.
                        bound = self._import_binding_node(target.imports[attr])
                        if isinstance(bound, Resolved) and bound.symbol != sub:
                            shadow = Resolved(bound.symbol, bound.detail)
                    elif attr in target.bindings:
                        shadow = Resolved(target.name, detail=f"attribute:{attr}")
                if shadow is not None and sub in self.scopes:
                    return Resolved(shadow.symbol, shadow.detail, also=(sub,))
                return ModuleNode(sub)
            if target is None:
                return Unresolved(UNRESOLVED_ATTRIBUTE, attr)
            found = self._lookup_in_module(target, attr, set())
            if found is None or isinstance(found, Unresolved):
                lazy = target.members.get("__getattr__")
                if lazy is not None:
                    # PEP 562: a module ``__getattr__`` serves names the module
                    # does not bind (lazy loading): the reference depends on it,
                    # and stays bounded by the name for what it hands back.
                    return Resolved(lazy, uncertain_attr=attr)
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

    def class_object_read(self, source: str, parts: list[str], scope: Scope) -> None:
        """An attribute chain that reads, off a resolved class, an attribute
        the class does not define (``C.__type_params__``, ``C.__doc__``,
        ``C.__mro__``, ``C.registered`` set by a decorator): what it finds
        lives on the class object, so the reader depends on the class as a
        reader of the name ``C`` does, and whatever reaches the class
        (lazily evaluated type parameters and annotations, a docstring,
        what its decorators, metaclass and bases left on it) reaches the
        reader. The name-bounded reference resolve_chain_names reports stays
        beside the edge (a metaclass may define the attribute).

        On ``self``/``cls`` only dunder names count -- anything else is an
        instance attribute, bounded by the class's writes (attr_refs) -- and
        the class may be any in-scope subclass. ``__class__`` is left to
        ``escape_class_family`` and ``__dict__`` to the member read.

        ``__mro__``, ``__bases__`` and ``__base__`` hand out the class's
        ancestors, and ``__subclasses__`` its descendants: classes nothing
        names, which the reader can construct, call or read any attribute
        of. It refers to each, and to every member each holds."""
        if len(parts) < 2:
            return
        receiver = parts[0] == scope.self_name and scope.self_class is not None
        node = self._lookup_base(parts[0], scope)
        if receiver and parts[1] == "__class__" and len(parts) > 2:
            node, parts = Resolved(scope.self_class or ""), [parts[0], *parts[2:]]
        for i, attr in enumerate(parts[1:], 1):
            if not (
                isinstance(node, Resolved) and not node.detail and node.symbol in self.class_scopes
            ):
                return
            found = self._step(node, attr)
            if not isinstance(found, Unresolved):
                node = found
                continue
            on_self = receiver and i == 1
            dunder = attr.startswith("__") and attr.endswith("__")
            if on_self and (not dunder or attr in ("__class__", "__dict__")):
                return
            family = [node.symbol]
            if on_self:
                family += self._descendants.get(node.symbol, ())
            for cls in family:
                if cls != source:
                    self.out.edges.add(Edge(source, cls, REFERENCES, "class object"))
            handed: set[str] = set()
            if attr in ("__mro__", "__bases__", "__base__"):
                handed = {a for cls in family for a in self._mro(cls)[1:]}
            elif attr == "__subclasses__":
                handed = {d for cls in family for d in self._descendants.get(cls, ())}
            for cls in sorted(handed):
                self._record(source, Resolved(cls), chain=".".join(parts[: i + 1]))
                self.escape(Resolved(cls))
                for holder in self._mro(cls):
                    for member in sorted(self.class_scopes[holder].members.values()):
                        if member != source:
                            self.out.edges.add(Edge(source, member, REFERENCES, f"via {attr}"))
            return

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
            for other in node.alternatives:
                if other != source:
                    self.out.edges.add(Edge(source, other, kind, "alternative binding"))
            if kind == REFERENCES and not node.detail and node.symbol in self.class_scopes:
                # Using a class (``Foo(...)``, subclassing) runs its constructor.
                for hook in ("__init__", "__new__"):
                    found = self.lookup_in_class(node.symbol, hook)
                    if isinstance(found, Resolved) and found.symbol != source:
                        self.out.edges.add(Edge(source, found.symbol, REFERENCES, "constructor"))
        elif isinstance(node, ModuleNode):
            if node.module in self.scopes and node.module != source:
                self.out.edges.add(Edge(source, node.module, kind, "module"))
        elif isinstance(node, External):
            self.out.external.add(ExternalReference(source, node.module))
        elif isinstance(node, Unresolved):
            self.out.unresolved.add(UnresolvedReference(source, node.kind, node.name, chain))


def _is_main_guard(stmt: ast.stmt, scope: ModuleScope) -> bool:
    """``if __name__ == "__main__":`` (either way round), with ``__name__``
    not rebound by the module."""
    if not isinstance(stmt, ast.If) or "__name__" in scope.bindings:
        return False
    test = stmt.test
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
    ):
        return False
    sides = (test.left, test.comparators[0])
    return any(isinstance(a, ast.Name) and a.id == "__name__" for a in sides) and any(
        isinstance(b, ast.Constant) and b.value == "__main__" for b in sides
    )
