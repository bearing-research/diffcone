"""The second pass's AST walker: what a function, class or module body
references, calls, writes and looks up dynamically."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from diffcone.indexer.definitions import _flatten_chain
from diffcone.indexer.facts import _AttrRef, _AttrWrite, _CallSite, _ParamDynamic
from diffcone.indexer.literals import _collect_store_names, _LocalBindings
from diffcone.indexer.scopes import (
    External,
    ModuleNode,
    Node,
    Resolved,
    Scope,
    Unresolved,
    _resolve_relative_name,
    _string_prefix,
)
from diffcone.indexer.syntax import (
    DYNAMIC_CALLS,
    REFLECTIVE_ATTRIBUTES,
    REFLECTIVE_BUILTINS,
    REFLECTIVE_CALLS,
)
from diffcone.indexer.uses import CONTAINER_MUTATORS
from diffcone.model import (
    CLASS,
    FUNCTION,
    METHOD,
    MODULE,
    REFERENCES,
    UNRESOLVED_ATTRIBUTE,
    UNRESOLVED_DYNAMIC,
    VARIABLE,
    Edge,
    ExternalReference,
    UnresolvedReference,
)
from diffcone.snapshot import module_name_for, split_root

if TYPE_CHECKING:
    from diffcone.indexer.resolver import Resolver


# Calls that hand out every member of their argument, values included.
MEMBER_LISTINGS = frozenset({"inspect.getmembers", "inspect.getmembers_static"})


# Calls that read a file's text or bytes: code built from them is not the
# program's own.
FILE_READS = frozenset(
    {"open", "read", "read_text", "read_bytes", "get_data", "read_binary", "files", "readlines"}
)


def _reads_files(node: ast.AST) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in FILE_READS:
                return True
    return False


class _ReferenceCollector(ast.NodeVisitor):
    """Walk a symbol's code and record edges / unresolved references.

    Nested functions, lambdas and comprehensions push their own scope so a
    name bound there does not shadow the enclosing symbol's references. With
    ``skip_defs`` (module and class bodies) nested definitions are not
    entered at all: they are symbols resolved on their own.
    """

    def __init__(
        self, indexer: Resolver, source: str, scope: Scope, *, skip_defs: bool = False
    ) -> None:
        self.indexer = indexer
        self.source = source
        self.scope = scope
        self.skip_defs = skip_defs
        # Positions that name a class without constructing it (the second
        # argument of ``isinstance``/``issubclass``, the first of
        # ``typing.cast``). Annotations are not among them: frameworks build
        # instances from them (injector, FastAPI's ``Depends()``, pydantic).
        self._type_nodes: set[int] = set()
        # ``self.<attr> = value`` targets in ``__init__`` -> what they bind.
        self._bindings: dict[int, list | None] = {}
        # The nodes being visited, outermost first (see visit).
        self._stack: list[ast.AST] = []

    def visit(self, node: ast.AST) -> None:
        self._stack.append(node)
        try:
            super().visit(node)
        finally:
            self._stack.pop()

    def _parent(self, node: ast.AST, up: int = 1) -> ast.AST | None:
        """The node ``up`` levels above ``node`` while it is being visited."""
        if len(self._stack) > up and self._stack[-1] is node:
            return self._stack[-1 - up]
        return None

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
            # A class body's names are not visible in scopes nested in it.
            locals=(set() if outer.class_level else outer.locals) | bound,
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

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
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
                    self.visit(arg.annotation)
            if node.returns is not None:
                self.visit(node.returns)
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self.scope = outer

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return self.visit_FunctionDef(node)

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

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
    ) -> None:
        generators = node.generators
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
        self.indexer.class_object_read(self.source, parts, self.scope)
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

    def _self_class(self, node: ast.expr) -> str | None:
        """The class of the method this is in, when ``node`` is its ``self``
        or ``cls``."""
        if isinstance(node, ast.Name) and node.id == self.scope.self_name:
            return self.scope.self_class
        return None

    def _is_self(self, node: ast.expr) -> bool:
        return self._self_class(node) is not None

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if not isinstance(node.ctx, ast.Load):
            self._attribute_write(node)
        elif node.attr in REFLECTIVE_ATTRIBUTES:
            self.indexer.out.reflection.add((self.source, f".{node.attr}"))
        if node.attr == "__dict__":
            # ``__dict__`` can read or write any attribute; through another
            # receiver (aliased, reassigned, ``|=``) the class is unknown.
            owner = self.scope.self_class if self._is_self(node.value) else ""
            self.indexer.out.attr_unbound.add((owner or "", "*"))
            if isinstance(node.ctx, ast.Load):
                self._namespace_read(node)
        elif isinstance(node.value, ast.Attribute) and node.value.attr == "__dict__":
            # ``m.__dict__.get(k)``, ``m.__dict__.items()``: the chain is
            # resolved whole below, so the namespace read is seen here.
            inner = node.value
            call = self._parent(node)
            key = None
            if node.attr == "get" and isinstance(call, ast.Call) and call.func is node:
                key = call.args[0] if call.args else None
            if node.attr not in ("keys", "__contains__", "__len__"):
                self._member_read(inner.value, key)
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

    def _namespace_read(self, node: ast.Attribute) -> None:
        """``m.__dict__[k]``, ``m.__dict__.get(k)`` or any other read of a
        namespace: a module's or a class's (``C.__dict__``, ``cls.__dict__``),
        or an object's of any type (``obj.__dict__[k]``,
        ``self.__class__.__dict__[k]``): the attribute ``k`` names, read as
        ``getattr(x, k)`` is. Uses that see only the names (``k in
        x.__dict__``, iterating it, ``len``) read no attribute; anything else
        reads any attribute."""
        parent = self._parent(node)
        if self._keys_only(node, parent):
            return
        key: ast.expr | None = None
        if isinstance(parent, ast.Subscript) and parent.value is node:
            key = parent.slice
        elif isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = self._parent(node, 2)
            if isinstance(call, ast.Call) and call.func is parent and call.args:
                key = call.args[0]
        self._member_read(node.value, key)

    def _keys_only(self, node: ast.expr, parent: ast.AST | None) -> bool:
        """Whether ``node`` (a ``__dict__``) is used only for its names."""
        if isinstance(parent, ast.Compare):
            return node in parent.comparators and all(
                isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops
            )
        if isinstance(parent, (ast.For, ast.AsyncFor, ast.comprehension)):
            return parent.iter is node
        if isinstance(parent, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            # The iterators are visited from the comprehension itself.
            return any(gen.iter is node for gen in parent.generators)
        if isinstance(parent, ast.Call) and node in parent.args:
            chain = _flatten_chain(parent.func)
            return chain == ["len"]
        return False

    def _member_read(self, receiver: ast.expr, key: ast.expr | None) -> None:
        """A read of ``receiver``'s attribute by the name ``key`` (any name
        when None), handled as ``getattr(receiver, key)``."""
        name = key if key is not None else ast.Name(id="<any attribute>", ctx=ast.Load())
        func = ast.Name(id="getattr", ctx=ast.Load())
        self._getattr(ast.Call(func=func, args=[receiver, name], keywords=[]))

    def _is_module(self, expr: ast.expr) -> bool:
        """Whether ``expr`` names a module (in scope or not), through this
        scope's bindings or as ``sys.modules["m"]``/``import_module("m")``."""
        if self._literal_module(expr) is not None:
            return True
        chain = _flatten_chain(expr)
        if chain is None:
            return False
        if chain[0] in self.scope.locals and chain[0] not in self.scope.local_imports:
            return False
        return isinstance(self.indexer.resolve_chain(chain, self.scope), (ModuleNode, External))

    def _literal_module(self, expr: ast.expr) -> str | None:
        """The module ``sys.modules["m"]``, ``sys.modules.get("m")`` or
        ``import_module("m")`` names by a literal."""
        key: ast.expr | None = None
        if isinstance(expr, ast.Subscript):
            chain = _flatten_chain(expr.value)
            if chain is not None and self._canonical_name(chain) == "sys.modules":
                key = expr.slice
        elif isinstance(expr, ast.Call) and expr.args:
            chain = _flatten_chain(expr.func)
            if chain is not None:
                canonical = self._canonical_name(chain)
                if canonical in ("importlib.import_module", "sys.modules.get"):
                    key = expr.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            parts = key.value.split(".")
            if all(p.isidentifier() for p in parts):
                return key.value
        return None

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
    MUTATING_METHODS = CONTAINER_MUTATORS

    def _mutation_target(self, expr: ast.expr) -> None:
        """Record ``source`` as a writer when ``expr`` (an assignment target or
        a receiver of a mutating call) is a subscript/attribute of a variable
        symbol, or the variable itself under ``global``; or another module's
        variable assigned through the module (``settings.DEBUG = True`` in a
        conftest: whoever reads ``settings.DEBUG`` sees the writer)."""
        if isinstance(expr, ast.Attribute):
            chain = _flatten_chain(expr)
            if chain is not None and len(chain) > 1 and chain[0] not in self.scope.locals:
                target = self.indexer.resolve_chain(chain, self.scope)
                if isinstance(target, Resolved):
                    symbol = self.indexer.index.symbols.get(target.symbol)
                    if (
                        symbol is not None
                        and symbol.id != self.source
                        and symbol.module != self.scope.module.name
                        and (
                            (symbol.kind == VARIABLE and not target.detail)
                            or (symbol.kind == MODULE and target.detail.startswith("attribute:"))
                        )
                    ):
                        self.indexer.out.edges.add(
                            Edge(symbol.id, self.source, REFERENCES, "mutated_by")
                        )
                        return
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

    def _external_module(self, expr: ast.expr | None) -> str | None:
        """The external module ``expr`` names (``logging``, ``os.path``), if
        it names one through this scope's bindings (a function-local import
        included)."""
        chain = _flatten_chain(expr) if expr is not None else None
        if chain is None or (
            chain[0] in self.scope.locals and chain[0] not in self.scope.local_imports
        ):
            return None
        node = self.indexer.resolve_chain(chain, self.scope)
        return node.module if isinstance(node, External) else None

    def _written_module(self, expr: ast.expr | None) -> str | None:
        """The external module a write's receiver may be: one it names, or
        ``*`` (any) for a module found at run time (``sys.modules[name]``,
        ``import_module(name)``, ``__import__(name)``)."""
        if expr is None:
            return None
        module = self._external_module(expr)
        if module is not None:
            return module
        for node in ast.walk(expr):
            if isinstance(node, ast.Attribute) and node.attr == "modules":
                return "*"
            if isinstance(node, ast.Call):
                parts = _flatten_chain(node.func) or []
                if parts and parts[-1] in ("import_module", "__import__"):
                    return "*"
        return None

    def _attribute_write(self, node: ast.Attribute) -> None:
        """A store or delete of ``<receiver>.<attr>``: on ``self`` it is a
        write of that class's instance attribute (bound only when it is a
        plain ``__init__`` assignment); on any other receiver the type is
        unknown, so no class's ``attr`` can be bounded."""
        if (module := self._written_module(node.value)) is not None:
            self.indexer.out.external_writes.add((module, self.source))
        if (cls := self._self_class(node.value)) is not None:
            binding = self._bindings.pop(id(node), None)
            self.indexer.out.attr_writes.append(
                _AttrWrite(cls, node.attr, self.scope.method, binding)
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
        if (module := self._written_module(receiver)) is not None:
            self.indexer.out.external_writes.add((module, self.source))
        names = self.scope.string_candidates(name) if name is not None else None
        owner = (self._self_class(receiver) if receiver is not None else None) or ""
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
        self.visit(node.annotation)
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
            if (builtin and parts[0] in REFLECTIVE_BUILTINS) or (
                not builtin and self._canonical_name(parts) in REFLECTIVE_CALLS
            ):
                module = self._external_module(node.args[0]) if node.args else None
                if module is not None:
                    # ``dir(builtins)``: what an external module holds.
                    self.indexer.out.external_lookups.add(
                        (self.source, module, "reflection", f"{name}()")
                    )
                else:
                    self.indexer.out.reflection.add((self.source, f"{name}()"))
                if (
                    node.args
                    and self._canonical_name(parts) in MEMBER_LISTINGS
                    and (self._is_module(node.args[0]) or self._is_class(node.args[0]))
                ):
                    # ``inspect.getmembers(mod)`` hands out every member's
                    # value, as ``vars(mod)`` does: any attribute is read.
                    self._member_read(node.args[0], None)
            if builtin and parts[0] in DYNAMIC_CALLS:
                code = node.args[0] if node.args else None
                literal = isinstance(code, ast.Constant) and isinstance(code.value, str)
                scope_node = self.scope.literal_node or self.scope.module.tree
                if (
                    parts[0] in ("exec", "eval")
                    and code is not None
                    and not literal
                    and (
                        _reads_files(code) or (scope_node is not None and _reads_files(scope_node))
                    )
                ):
                    # Code read from a file at run time (``exec(open("plugin.py")
                    # .read())``) can import anything; code generated from the
                    # program's own templates reaches only what its module can.
                    self._dynamic(f"{name}() of code read at run time: an import of anything")
                else:
                    self._dynamic(f"{name}()")
            elif builtin and parts[0] == "__import__":
                self._import_module(node, "__import__")
            elif builtin and parts[0] == "getattr":
                self._getattr(node)
            elif (canonical := self._canonical_name(parts)) in (
                "importlib.import_module",
                "importlib.__import__",
                "runpy.run_module",
            ):
                self._import_module(node, canonical)
            elif canonical == "runpy.run_path":
                self._dynamic("runpy.run_path()")  # runs a file by path: anything
            if builtin and parts[0] == "vars" and node.args:
                if (cls := self._self_class(node.args[0])) is not None:
                    self.indexer.out.attr_unbound.add((cls, "*"))
            if builtin and parts[0] == "type" and len(node.args) == 1:
                if (cls := self._self_class(node.args[0])) is not None:
                    self.indexer.escape_class_family(cls)
            if parts[-1] in ("setattr", "delattr") or parts[-2:] == ["patch", "object"]:
                self._setattr_call(node, parts)
            self._string_targets(node, parts)
            if builtin and parts[0] in ("isinstance", "issubclass") and len(node.args) == 2:
                self._mark_type_node(node.args[1])
            elif node.args and self._canonical_name(parts) in (
                "typing.cast",
                "typing_extensions.cast",
            ):
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

    def _string_targets(self, node: ast.Call, parts: list[str]) -> None:
        """Names a call reaches only through a string: ``monkeypatch.setattr(
        "pkg.config.TIMEOUT", 0)``, ``mock.patch("pkg.api.fetch")``,
        ``patch.dict("pkg.config.D")`` and ``(obj, "NAME")`` pairs depend on
        that name existing (patching a missing one raises); a string
        ``skipif``/``xfail`` condition is code pytest evaluates."""
        args = node.args
        last = parts[-1]
        if last in ("skipif", "xfail") and args:
            condition = args[0]
            if isinstance(condition, ast.Constant) and isinstance(condition.value, str):
                try:
                    expr = ast.parse(condition.value, mode="eval").body
                except SyntaxError:
                    return
                self.visit(expr)
            return
        patching = (
            (last in ("setattr", "delattr") and len(parts) > 1)
            or last == "patch"
            or parts[-2:] in (["patch", "object"], ["patch", "dict"])
        )
        if not patching or not args:
            return
        first = args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            chain = first.value.split(".")
            if len(chain) > 1 and all(c.isidentifier() for c in chain):
                node_ = self.indexer.resolve_dotted(chain)
                if node_ is not None:
                    self.indexer._record(self.source, node_, chain=first.value)
                else:
                    # A third-party target: whichever prefix is the module,
                    # something may now be stored on it.
                    for i in range(1, len(chain)):
                        self.indexer.out.external_writes.add((".".join(chain[:i]), self.source))
            return
        if len(args) > 1 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
            receiver = _flatten_chain(first)
            if receiver is not None and args[1].value.isidentifier():
                self._resolve([*receiver, args[1].value])

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
            positional_classes=[self._argument_class(a) for a in node.args],
            keyword_classes={
                k.arg: self._argument_class(k.value) for k in node.keywords if k.arg is not None
            },
            positional_sources=[self._argument_source(a) for a in node.args],
            keyword_sources={
                k.arg: self._argument_source(k.value) for k in node.keywords if k.arg is not None
            },
            caller=self.source,
            positional_params=[self._argument_param(a) for a in node.args],
            keyword_params={
                k.arg: self._argument_param(k.value) for k in node.keywords if k.arg is not None
            },
        )
        self.indexer.out.call_sites[symbol.id].append(site)
        # A dispatched call may land on any override: they share the call site.
        for override_id, detail in target.overrides:
            if not detail:
                self.indexer.out.call_sites[override_id].append(site)

    def _argument_class(self, expr: ast.expr) -> str | None:
        """The class an argument is an instance of, when the argument says so:
        ``C()`` passes an instance of C, ``C`` passes the class itself. A name
        holding an instance, or anything a function returns, says nothing."""
        target = expr.func if isinstance(expr, ast.Call) else expr
        parts = _flatten_chain(target)
        if parts is None:
            return None
        node = self.indexer.resolve_chain(parts, self.scope)
        if not isinstance(node, Resolved) or node.detail:
            return None
        symbol = self.indexer.index.symbols.get(node.symbol)
        return node.symbol if symbol is not None and symbol.kind == CLASS else None

    def _argument_param(self, expr: ast.expr) -> str | None:
        """The enclosing function's parameter an argument is, when it is
        exactly that name and the body never rebinds it."""
        if not isinstance(expr, ast.Name) or isinstance(expr.ctx, ast.Store):
            return None
        if expr.id not in self.scope.params or expr.id in self.scope.rebound:
            return None
        return expr.id

    def _argument_source(self, expr: ast.expr) -> str | None:
        """The function an argument came out of: ``make()`` directly, or a
        name this scope assigned once from such a call (``obj = make()``).
        What that function returns is known only once every module has been
        indexed, so the join happens at the end (see Indexer.build)."""
        if isinstance(expr, ast.Name) and not isinstance(expr.ctx, ast.Store):
            expr = self._local_source(expr.id) or expr
        if not isinstance(expr, ast.Call):
            return None
        parts = _flatten_chain(expr.func)
        if parts is None:
            return None
        node = self.indexer.resolve_chain(parts, self.scope)
        if not isinstance(node, Resolved) or node.detail:
            return None
        symbol = self.indexer.index.symbols.get(node.symbol)
        return node.symbol if symbol is not None and symbol.kind in (FUNCTION, METHOD) else None

    def _local_source(self, name: str) -> ast.expr | None:
        """What this scope assigns ``name``, when that is the only thing that
        binds it. A second assignment, a loop target, a ``with ... as``, a
        ``del`` or a parameter of the same name all say nothing: the object
        could be either."""
        body = self.scope.literal_node
        if body is None or name in self.scope.params:
            return None
        bindings = [
            inner
            for inner in ast.walk(body)
            if isinstance(inner, ast.Name)
            and inner.id == name
            and not isinstance(inner.ctx, ast.Load)
        ]
        if len(bindings) != 1:
            return None
        for inner in ast.walk(body):
            if isinstance(inner, ast.Assign) and any(t is bindings[0] for t in inner.targets):
                return inner.value
            if isinstance(inner, ast.AnnAssign) and inner.target is bindings[0]:
                return inner.value
        return None

    def _mark_escape(self, node: ast.expr, parts: list[str]) -> None:
        """A function referenced other than as the callee of a call may be
        called from anywhere with anything; so may a class (constructed), unless
        it is only named by ``isinstance``/``issubclass``/``typing.cast``
        (annotations count: frameworks construct from them). ``self`` as a value is an
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
        type_position = id(node) in self._type_nodes
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
        if isinstance(expr, ast.Attribute) and (cls := self._self_class(expr.value)) is not None:
            self.indexer.out.param_dynamics.append(
                _ParamDynamic(self.source, expr.attr, kind, base, self.scope, detail, cls)
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

    def _getattr_detail(self, receiver: ast.expr, base: list[str] | None) -> str:
        """What bounds an unbounded ``getattr(x, <name>)``. The seeding
        module's import closure covers an object that is one of that
        module's globals: a module it can name holds attributes from its own
        closure, which is inside ours. A receiver that came from somewhere
        else -- a parameter, a call result, an instance whose attributes a
        caller may have set -- can be an object of any module; that read is
        bounded at the other end, by the classes handed to other code (see
        ``SourceIndex.escaped_classes``), and the detail says so."""
        node = self.indexer.resolve_chain(base, self.scope) if base else None
        if isinstance(node, (ModuleNode, External)):
            return "getattr(<non-literal>)"
        return "getattr(<non-literal>) on a receiver from elsewhere, bound to the classes handed on"

    def _getattr(self, node: ast.Call) -> None:
        if len(node.args) < 2:
            return
        if self._forwarded_name(node.args[1]):
            return
        names = self.scope.string_candidates(node.args[1])
        base = _flatten_chain(node.args[0])
        literal_module = self._literal_module(node.args[0]) if base is None else None
        if literal_module is not None and names is None and self._module_getattr(literal_module):
            return
        if names is None:
            prefix = _string_prefix(node.args[1])
            if prefix is not None:
                # ``getattr(obj, f"pytest_{name}")``: every in-scope attribute
                # name with that prefix is a candidate, nothing else.
                names = self.indexer.symbol_names_with_prefix(prefix)
            elif (module := self._external_module(node.args[0])) is not None:
                # On an external module: bounded unless in-scope code stores
                # something there (decided over the whole tree).
                self.indexer.out.external_lookups.add(
                    (self.source, module, "dynamic", "getattr(<non-literal>)")
                )
                return
            elif not self._param_dynamic(
                node.args[1], "getattr", base, self._getattr_detail(node.args[0], base)
            ):
                # The name is not a parameter either: nothing bounds it.
                self._dynamic(self._getattr_detail(node.args[0], base))
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

    def _is_class_object(self, expr: ast.expr) -> bool:
        """Whether ``expr`` is a class itself (a name or chain resolving to
        one, or ``cls`` in a classmethod), not an instance."""
        if isinstance(expr, ast.Name) and expr.id == self.scope.self_name:
            return self.scope.self_is_class and self.scope.self_class is not None
        return self._is_class(expr)

    def _is_class(self, expr: ast.expr) -> bool:
        chain = _flatten_chain(expr)
        if chain is None or chain[0] == self.scope.self_name:
            return False
        node = self.indexer.resolve_chain(chain, self.scope)
        return (
            isinstance(node, Resolved)
            and not node.detail
            and node.symbol in self.indexer.class_scopes
        )

    def _module_getattr(self, module: str) -> bool:
        """``getattr(sys.modules["m"], <non-literal>)`` (or
        ``import_module("m")``, ``sys.modules["m"].__dict__[n]``) on an
        in-scope module: any attribute of it, as on a module global, and
        the reader depends on it as on an import of it. False for a module
        outside the source roots, left to the general rule."""
        node = self.indexer.resolve_dotted(module.split("."))
        if not isinstance(node, ModuleNode):
            return False
        self.indexer._module_import_edge(self.source, node.module)
        self._dynamic("getattr(<non-literal>)")
        return True

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
