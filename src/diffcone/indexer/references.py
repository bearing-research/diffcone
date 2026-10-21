"""The second pass's AST walker: what a function, class or module body
references, calls, writes and looks up dynamically.

A write (an item or attribute stored, augmented or deleted, a container
mutator, ``setattr``, a standard-library function changing its first
argument) is traced back to the shared state it changes, its roots: module
variables and classes, reached directly, through ``cls``/``type(self)``,
through items and method results of a root, through any local name bound
to one (``aliases.name_sources``), ``globals()``, or what an in-scope
function returns (``state_returns``/``result_writes``, closed after pass 2
by ``Indexer._result_writes``). Copies are no roots. Each root gets a
``mutated_by`` edge to the writer (``_rooted_write``), after pass 2 and
unless the root is a variable whose value cannot change in place
(``value_writes``, Indexer._value_writes); rebinding a variable is an edge of
its own (``_mutation_target``).

An instance an in-scope class makes (``Store()``, ``cls()``,
``cls.__new__(cls)``, ``self.__class__.__new__(self.__class__)``,
``super().__new__(cls)``, ``object.__new__(cls)``: ``_made_instance``) is a
root of its own (``FRESH``), never the class it was made from, and what it
holds is ``INSTANCE``. A write in place into either, or either handed to a
callee writing its parameter, is a write of the class's state when the
class holds any (``instance_writes``, decided after pass 2 by
Indexer._instance_writes as for a module variable holding an instance);
binding an attribute on the instance itself changes that object alone when
nothing but ``object.__new__`` made it (audit round 6, T22).

A class attribute (``Field.DAYS``, ``self._MONTHS`` when the class body
binds ``_MONTHS``) is a root of its own (``ATTR``): a write into it writes
the class's state unless the class body binds the attribute once to a value
that cannot change in place and nothing may bind it anew, on the class or
on any class of its MRO and subclasses (Indexer._class_attribute_writes; T24).
A method called on one, or on an item of one, by a name that does not
resolve is a write of it unless it is a literal table nothing changes
(writes.apply_writes).
What a builtin returns may be its argument (``min(m, self._N)``), so a
local augmented after it keeps the root; ``cast(T, x)`` is ``x``, not
``T``, and ``type(Store())`` is the class. The class of anything else
(``type(o)``, ``o.__class__`` of a parameter, of what a call returns) is
unknown (``ANY_CLASS``): a write onto it is a write of every in-scope class
that may own the attribute (writes.AnyClassOwners, after pass 2; T25).

What a constructor call of an in-scope class is handed (``Tokenizer(src,
rules=RULES)``) is held by the instance it makes (``HELD``): a change in
place of the instance, a method call on it, or a write into anything it
holds writes the argument, but binding or augmenting an attribute of the
instance itself (``tok.position += 1``, through any number of callees:
``param_flavors``) does not when that runs no code of the class and the
attribute only ever holds values that cannot change in place
(writes._HeldSafety; T23). The initialiser's own writes through ``self``
reach the arguments as a call handing them would."""

from __future__ import annotations

import ast
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING

from diffcone.indexer.aliases import VALUE, name_sources
from diffcone.indexer.facts import _AttrRef, _AttrWrite, _CallSite, _ParamDynamic, apply_fact
from diffcone.indexer.literals import _collect_store_names, _LocalBindings
from diffcone.indexer.patches import (
    DICT,
    MULTIPLE,
    OBJECT,
    SETATTR,
    callee_parts,
    patch_target_form,
    patched_names,
    suspends,
)
from diffcone.indexer.process import (
    LOGGER_GETTERS,
    LOGGER_MUTATORS,
    STATE_CALLS,
    STATE_MUTATORS,
    state_object,
)
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
from diffcone.indexer.scripts import (
    HOLE_NAME,
    code_edges,
    code_texts,
    format_attribute_names,
    observe_call_arguments,
    observe_command,
    observe_shell,
    observe_string,
    plugin_options,
    runner_list_known,
    script_value,
    test_file,
    unwrap_text,
    whole_module,
)
from diffcone.indexer.syntax import (
    DYNAMIC_CALLS,
    FUNC_NODES,
    REFLECTIVE_ATTRIBUTES,
    REFLECTIVE_BUILTINS,
    REFLECTIVE_CALLS,
    _flatten_chain,
    own_directory_join,
    reads_files,
)
from diffcone.indexer.uses import (
    CONTAINER_MUTATORS,
    FILE_LOADERS,
    PICKLER_DUMPS,
    UNPICKLER_CALLS,
    UNPICKLER_CLASSES,
)
from diffcone.indexer.writes import (
    ANY_CLASS_WHAT,
    ARGUMENT_MUTATORS,
    HELD_WHAT,
    _whole_param,
    observe_assign,
    observe_augassign,
    observe_call,
    observe_return,
)
from diffcone.model import (
    DISCARDED_IMPORT,
    GRAPH_HANDLE,
    GRAPH_INERT,
    GRAPH_MODULES,
    MUTATED_BY,
    REBINDS,
    RUNS_PROGRAM,
    RUNS_TEXT,
    WRITES,
    Edge,
    EdgeKind,
    ExternalReference,
    SymbolKind,
    UnresolvedKind,
    UnresolvedReference,
)
from diffcone.snapshot import module_name_for, split_root

if TYPE_CHECKING:
    from diffcone.indexer.resolver import Resolver

# Methods that change a builtin container the same way whatever it holds,
# and return nothing a statement uses (``pop`` only with a default).
WRITE_ONLY_METHODS = frozenset(
    {
        "clear",
        "update",
        "append",
        "extend",
        "add",
        "discard",
        "insert",
        "setdefault",
        "appendleft",
        "extendleft",
        "pop",
    }
)
# Detail of an item store (``X[k] = v``) until the tree is known: it reads
# nothing of a dict only (Indexer._write_only_references).
SETITEM = "writes[]"
# A store target that reads the old value too (augmented assignment).
_READ = "read"


# Calls that hand out every member of their argument, values included.
MEMBER_LISTINGS = frozenset({"inspect.getmembers", "inspect.getmembers_static"})
# Calls that import a module by name, and those that hand one back by name.
# ``pytest.importorskip`` imports it (a test skips when that fails);
# ``find_spec`` imports the parent packages and finds the module's file,
# which ``module_from_spec`` and ``exec_module`` then run (audit round 6, T9).
IMPORT_CALLS = frozenset(
    {
        "importlib.import_module",
        "importlib.__import__",
        "runpy.run_module",
        "pytest.importorskip",
        "importlib.util.find_spec",
    }
)
FIND_SPEC = "importlib.util.find_spec"
# What runs the module a spec names.
SPEC_LOADERS = frozenset({"module_from_spec", "exec_module", "load_module"})
# Detail of a ``find_spec`` of a name known at run time whose spec nothing
# around it loads; it does not say "import" (model.imports_anything).
FINDS_MODULE = "find_spec(<non-literal>): finds a module by a name known at run time"
# ``__import__``'s forms, which hand back the top-level package unless given a ``fromlist``.
DUNDER_IMPORTS = frozenset({"__import__", "importlib.__import__"})
MODULE_GETTERS = frozenset({"importlib.import_module", "sys.modules.get", "pytest.importorskip"})
# Calls that import the module a dotted name starts with and hand back what
# the rest of it names (``pkgutil.resolve_name("pkg.mod:attr")``).
NAME_RESOLVERS = frozenset({"pkgutil.resolve_name", "pydoc.locate"})
# Calls that list the modules on a path (``walk_packages`` imports the
# packages among them too).
MODULE_LISTINGS = frozenset({"pkgutil.iter_modules", "pkgutil.walk_packages"})
CASTS = frozenset({"typing.cast", "typing_extensions.cast"})
# Calls whose result calls what they are handed first (``partial(C)()`` makes
# a ``C``: ``_class_named``).
PARTIALS = frozenset({"functools.partial"})

# Decorators that hand back the function itself or a wrapper calling it
# afresh on every call, so what a call returns is no earlier call's result
# (Resolver._keeps_result; any other decorator may keep one: audit round 7,
# U7). By canonical name; builtins where the module does not rebind them.
FRESH_RESULT_DECORATORS = frozenset(
    {
        *(
            f"{prefix}{name}"
            for prefix in ("", "builtins.")
            for name in ("staticmethod", "classmethod", "property")
        ),
        "abc.abstractmethod",
        "abc.abstractclassmethod",
        "abc.abstractstaticmethod",
        "abc.abstractproperty",
        "contextlib.contextmanager",
        "contextlib.asynccontextmanager",
        "functools.wraps",
        "functools.singledispatch",
        "functools.singledispatchmethod",
        "functools.total_ordering",
        "pytest.fixture",
        "pytest.yield_fixture",
        "warnings.deprecated",
        "typing_extensions.deprecated",
    }
)

# What a write through a name bound to part of another object changes
# (``_state_roots``): calls whose result holds nothing of their arguments, and
# those making a new container of their elements (a shallow copy: writing
# into it changes nothing else, writing into an element does). Not ``abs``,
# ``round``, ``pow``, ``divmod`` or ``sum``: a special method may return the
# object itself (``__abs__``), ``sum([], start)`` returns ``start``; not
# ``copy.deepcopy``, which hands back a class, a function, an enum member or
# what ``__deepcopy__`` returns (``_copy_is_new``), nor ``pickle.loads``,
# whose ``__reduce__`` may name a module's own object (audit round 7, U7).
FRESH_CALLS = frozenset(
    {
        "all",
        "any",
        "ascii",
        "bin",
        "bool",
        "bytes",
        "callable",
        "chr",
        "float",
        "format",
        "hasattr",
        "hash",
        "hex",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "json.dumps",
        "len",
        "oct",
        "ord",
        "print",
        "repr",
        "str",
    }
)
# ``json.loads`` builds its result afresh unless a hook gives it something.
JSON_LOADS = frozenset({"json.loads", "json.load"})
JSON_HOOKS = frozenset(
    {"object_hook", "object_pairs_hook", "parse_float", "parse_int", "parse_constant", "cls"}
)
SHALLOW_CALLS = frozenset(
    {
        "collections.Counter",
        "collections.OrderedDict",
        "collections.deque",
        "copy.copy",
        "dict",
        "frozenset",
        "list",
        "set",
        "sorted",
        "tuple",
    }
)
# The special methods by which ``copy.copy``/``copy.deepcopy`` (and a
# ``.copy()`` of the class's own) let a class decide what a copy is: any of
# them in scope may hand back the object itself (``_copy_is_new``).
COPY_HOOKS = {
    "copy": ("copy", "__copy__"),
    "__copy__": ("copy", "__copy__"),
    "copy.copy": ("__copy__", "__reduce__", "__reduce_ex__"),
    "copy.deepcopy": ("__deepcopy__", "__reduce__", "__reduce_ex__"),
}
# This module's namespace as a value (``globals()``): an item of it is the
# module variable of that name (_state_roots).
_NAMESPACE = "<namespace>"
# A root that is what a call of an in-scope function returns: ``RESULT`` and
# the function's id (_state_roots, joined in Indexer._result_writes).
RESULT = "<result>:"
# A root that is an instance of an in-scope class the call makes (``Store()``,
# ``cls()``, ``cls.__new__(cls)``): ``FRESH``, how it was made (``call``, a
# ``__new__`` called on a class: ``new``, on ``super()``: ``super``, on
# ``object``: ``object``), ``:`` and the class id. A write in place into it
# may be one into state its class holds; binding an attribute on it is not,
# when nothing but ``object.__new__`` made it (``Indexer._instance_writes``).
# ``INSTANCE``, ``.`` and the attribute it was reached through (``s._items``)
# or nothing (an item, a method's result), ``:`` and the class id: what such
# an instance holds or is part of, where a write may be one into its class's
# state (audit round 6, T22). ``SHARED_ONLY`` in place of ``.``: through
# ``self`` in a method, an attribute a method of the class stores on
# ``self``, which is the instance's own object unless the method binds it to
# one that may be shared (audit round 7, U11). The class id may be a module
# variable's, for an instance made by calling what the variable holds
# (``Legacy = Conf; Legacy()``; U2).
FRESH = "<fresh>:"
INSTANCE = "<instance>:"
SHARED_ONLY = "-"
# Builtins whose result is of a builtin type nothing can change in place,
# whatever they are handed (``_immutable_typed``).
IMMUTABLE_TYPED_CALLS = frozenset(
    {
        "ascii",
        "bin",
        "bool",
        "bytes",
        "callable",
        "chr",
        "complex",
        "float",
        "format",
        "hasattr",
        "hash",
        "hex",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "len",
        "oct",
        "ord",
        "repr",
        "str",
    }
)
# What a class attribute holds (``Field.DAYS``, ``self._SECS_MAP`` when the
# class body binds it): ``ATTR``, the attribute, ``:`` and the class id. A
# write into it is a write of the class's state unless the attribute is
# bound once to a value that cannot change in place and nothing binds it
# anew (decided after pass 2: Indexer._class_attribute_writes; audit round 6,
# T24).
ATTR = "<attr>:"
# What a constructor call of an in-scope class was handed (an argument's
# root), as part of the fresh instance: ``HELD``, the class id, ``|`` and the
# argument's root. A change in place of the instance (its methods may change
# what it holds) or of any part of it may change the argument; binding an
# attribute of the instance does not, unless that runs code of the class
# (writes._HeldSafety; audit round 6, T23).
HELD = "<held>:"
# The class of an object whose type the analysis does not know (``type(o)``,
# ``o.__class__`` of a parameter, of something a call returned, of a module
# variable): ``ANY_CLASS`` and the attribute it is reached through
# (``type(o).items``) or nothing. A write into it may be one of any in-scope
# class that may own that attribute (decided after pass 2:
# writes.AnyClassOwners; audit round 6, T25).
ANY_CLASS = "<anyclass>:"
# Attributes every class has whose value ``type`` keeps of a type nothing can
# change in place (assigning another type raises): what a class of unknown
# type holds there is no state (T25).
_FIXED_CLASS_ATTRIBUTES = frozenset({"__name__", "__qualname__"})
# Attributes every class has that hold other classes: an item of one is a
# class, written as any (``Sub.__mro__[1].registry[k] = v``; audit round 7,
# U2).
CLASS_LISTS = frozenset({"__mro__", "__bases__", "__base__"})
# unittest.mock's mocks: each instance has a class of its own (``type(m).p =
# PropertyMock()`` changes that mock alone), so ``type(m)`` is no class of the
# project (T25).
MOCK_CALLS = frozenset(
    f"{module}.{name}"
    for module in ("unittest.mock", "mock")
    for name in (
        "Mock",
        "MagicMock",
        "NonCallableMock",
        "NonCallableMagicMock",
        "AsyncMock",
        "PropertyMock",
        "create_autospec",
    )
)
_NAMESPACE_GETTERS = frozenset({"get", "setdefault", "pop", "__getitem__"})
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


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
        # Attribute targets whose stored value cannot change in place (an
        # immutable of a builtin type: ``_immutable_typed``) -> True.
        self._attr_ok: dict[int, bool] = {}
        # The nodes being visited, outermost first (see visit).
        self._stack: list[ast.AST] = []
        # Nodes naming a variable only to change it (``X`` in ``X.clear()``
        # or ``X[k] = v``) -> the edge detail their reference gets.
        self._write_only: dict[int, str] = {}
        # Function body -> what ``_name_bindings`` found in it.
        self._binding_maps: dict[
            int, tuple[ast.AST, dict[str, list[ast.Name]], dict[int, ast.expr | None]]
        ] = {}
        # Name -> what this module's star imports may bind it to.
        self._star_names: dict[str, list[str]] = {}
        # Per function, lambda or comprehension: what its bindings give each
        # name, and the names it binds; per class, its ``class`` statement
        # (``_alias_roots``, ``_class_node``). The node is kept with each.
        self._source_maps: dict[int, tuple[ast.AST, dict[str, list[tuple[ast.expr, str]]]]] = {}
        self._bound_maps: dict[int, tuple[ast.AST, set[str]]] = {}
        self._class_nodes: dict[str, tuple[ast.Module, ast.ClassDef | None]] = {}

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
            local_alt_imports=outer.local_alt_imports,
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

    def _resolve(
        self, parts: list[str], kind: EdgeKind = EdgeKind.REFERENCES, mark: str = ""
    ) -> None:
        alias = self.indexer.module_alias(parts[0], self.scope) if len(parts) > 1 else None
        if alias is None:
            node, rest = self.indexer.resolve_chain_names(parts, self.scope)
            self._resolved(parts, node, rest, kind, mark)
            self.indexer.class_object_read(self.source, parts, self.scope)
            return
        # A name bound to a module (``m = ops``): ``m.value`` is a read of
        # each module's ``value``, as through the import. The name's own
        # binding (a module variable) is read too; when the name may hold
        # something else as well, the read off a value of unknown type
        # stays beside them.
        modules, partial = alias
        head = parts if partial else parts[:1]
        node, rest = self.indexer.resolve_chain_names(head, self.scope)
        self._resolved(head, node, rest, kind, mark if partial else "")
        for module in modules:
            node, rest = self.indexer.chain_names_from(ModuleNode(module), parts)
            self._resolved(parts, node, rest, kind, mark)

    def _resolved(
        self, parts: list[str], node: Node, rest: Sequence[str], kind: EdgeKind, mark: str
    ) -> None:
        """Record what ``parts`` resolved to (``node``, and the names read
        off a value of unknown type after it)."""
        chain = ".".join(parts)
        if not (mark and self._write_reference(node, rest, mark)):
            self.indexer._record(self.source, node, kind=kind, chain=chain)
        if isinstance(node, External) and not mark:
            # What of the library module the code reads (a store onto it,
            # ``json.x = v``, reads nothing of it), for a write onto it
            # elsewhere (Indexer._external_state_edges; U13).
            names = [
                n
                for n in self._canonical_names(parts)
                if n == node.module or n.startswith(node.module + ".")
            ]
            # The module itself read as a value (``=json``: what reads it may
            # read any attribute), or reached through a project module's
            # binding (``store.json.x``).
            for name in names or [node.module]:
                if name == node.module:
                    name = "=" + name
                self.indexer.out.external_reads.add((self.source, name))
        if isinstance(node, External) and node.module.split(".")[0] in GRAPH_MODULES:
            # ``gc.get_objects``: a member that hands out objects nothing
            # names (the bare module is its import, or a value the use scan
            # sees handed on).
            if any(
                name.split(".")[0] in GRAPH_MODULES
                and (member := name.split(".")[1:2])
                and member[0] not in GRAPH_INERT
                for name in self._canonical_names(parts)
            ):
                self.indexer.out.reflection.add((self.source, GRAPH_HANDLE))
        for name in rest:
            self.indexer.out.unresolved.add(
                UnresolvedReference(self.source, UnresolvedKind.ATTRIBUTE, name, chain)
            )

    def _dynamic(self, detail: str) -> None:
        self.indexer.out.unresolved.add(
            UnresolvedReference(self.source, UnresolvedKind.DYNAMIC, "", detail)
        )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._resolve([node.id], mark=self._write_only.get(id(node), ""))
            self._mark_escape(node, [node.id])
        elif not self.skip_defs and node.id not in self.scope.locals:
            self._global_rebinding(node.id)

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
        if (
            isinstance(node.ctx, ast.Load)
            and node.attr in CONTAINER_MUTATORS
            and node.attr != "__init__"
            and not (isinstance(call := self._parent(node), ast.Call) and call.func is node)
        ):
            # A bound mutator taken as a value (``_put = _CACHE.__setitem__``,
            # ``callbacks.append(_SEEN.add)``): whoever calls it writes.
            self._rooted_write(node.value, call=True, bound=True)
        if node.attr == "__dict__":
            # ``__dict__`` can read or write any attribute; through another
            # receiver (aliased, reassigned, ``|=``) the class is unknown.
            owner = self.scope.self_class if self._is_self(node.value) else ""
            self.indexer.out.attr_unbound.add((owner or "", "*"))
            self._generic_store(owner or "", node.value)
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
        if parts is not None and isinstance(node.ctx, ast.Load):
            # ``top = __import__("pkg.sub"); top.sub``: a chain off a local
            # bound once to a module obtained by a literal name.
            held = self._module_chain(node)
            if held is not None:
                parts = None
                self._module_members(node, held)
        if parts is not None:
            mark = self._write_only.get(id(node), "")
            if mark == _READ:
                mark = ""
            elif isinstance(node.ctx, ast.Store):
                mark = REBINDS
            self._resolve(parts, mark=mark)
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
        if isinstance(node.ctx, ast.Load) and (held := self._module_chain(node)) is not None:
            # ``import_module("pkg").sub``, ``__import__("pkg.sub").sub.f``:
            # an attribute chain off a module obtained by a literal name.
            self._module_members(node, held)
            self.generic_visit(node)
            return
        # ``Foo().run``, ``items[0].run``, ``make().run``: the base value is
        # unknown, but the attribute name still bounds what it may refer to.
        self.indexer.out.unresolved.add(
            UnresolvedReference(
                self.source, UnresolvedKind.ATTRIBUTE, node.attr, f"<expr>.{node.attr}"
            )
        )
        self.generic_visit(node)

    def _module_chain(self, node: ast.Attribute) -> list[str] | None:
        """The absolute dotted name an attribute chain stands for when its
        base is a module obtained by a literal name (``import_module("m")``,
        ``sys.modules["m"]``, ``__import__("m")``: the top package unless a
        ``fromlist`` is given) or a local bound once to one; None
        otherwise."""
        attrs: list[str] = []
        expr: ast.expr = node
        while isinstance(expr, ast.Attribute):
            attrs.insert(0, expr.attr)
            expr = expr.value
        if (
            isinstance(expr, ast.Name)
            and expr.id in self.scope.locals
            and expr.id not in self.scope.params
        ):
            source = self._local_source(expr.id)
            if source is None:
                return None
            expr = source
        module = self._obtained_module(expr)
        return None if module is None else [*module.split("."), *attrs]

    def _obtained_module(self, expr: ast.expr) -> str | None:
        """The module a call or subscript yields by a literal name."""
        module = self._literal_module(expr)
        if module is not None:
            return module
        if not (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "__import__"
            and not self._is_shadowed("__import__")
            and expr.args
        ):
            return None
        name = _string(expr.args[0])
        if name is None or not all(p.isidentifier() for p in name.split(".")):
            return None
        fromlist = expr.args[3] if len(expr.args) > 3 else None
        for k in expr.keywords:
            if k.arg == "fromlist":
                fromlist = k.value
        if fromlist is None or (isinstance(fromlist, (ast.List, ast.Tuple)) and not fromlist.elts):
            return name.split(".")[0]
        return name

    def _module_members(self, node: ast.Attribute, chain: list[str]) -> None:
        """A reference to what ``chain`` names, off a module obtained by a
        literal name (``_module_chain``), with the module itself referred to
        as a value (its members count once the module is handed on)."""
        target = self.indexer.resolve_dotted(chain)
        self.indexer._record(self.source, target, chain=".".join(chain))
        if node is not self._call_func:
            self._escape(target)
        for i in range(len(chain) - 1, 0, -1):
            prefix = chain[:i]
            found = self.indexer.resolve_dotted(prefix)
            if isinstance(found, ModuleNode):
                self.indexer._record(self.source, found, chain=".".join(prefix))
                break

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
            if chain is not None and self._canonical_names(chain) == {"sys.modules"}:
                key = expr.slice
        elif isinstance(expr, ast.Call) and expr.args:
            chain = _flatten_chain(expr.func)
            if chain is not None and self._only(chain, MODULE_GETTERS):
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

    # Project code run or read by naming it in a string (diffcone.indexer.scripts).
    def visit_Constant(self, node: ast.Constant) -> None:
        observe_string(self, node)
        observe_shell(self, node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        observe_shell(self, node)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Add):
            observe_shell(self, node)  # ``"X=1 " + sys.executable + " -m pkg.tool"``
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                self._hand_on(value)  # ``{**obj}`` calls ``obj.keys()``
        self.generic_visit(node)

    def visit_MatchClass(self, node: ast.MatchClass) -> None:
        """``case C(x=1)`` / ``case C(1)``: an ``isinstance`` check that
        then reads attributes of the subject by name: each keyword's, and
        for the positional patterns the names ``C.__match_args__`` lists (a
        literal tuple in ``C`` or an in-scope base; when it is not written
        out there, any attribute of ``C``). Resolved on the class when it
        is in scope, bounded by the name otherwise."""
        self._mark_type_node(node.cls)
        parts = _flatten_chain(node.cls)
        cls: str | None = None
        if parts is not None:
            target = self.indexer.resolve_chain(parts, self.scope)
            if (
                isinstance(target, Resolved)
                and not target.detail
                and target.symbol in self.indexer.class_scopes
            ):
                cls = target.symbol
        names = list(node.kwd_attrs)
        if node.patterns:
            positional = self._match_args(cls) if cls is not None else None
            if positional is not None:
                names += positional[: len(node.patterns)]
            elif cls is not None:
                for member in sorted(self.indexer._class_members({cls})):
                    if member != self.source:
                        self.indexer.out.edges.add(
                            Edge(self.source, member, EdgeKind.REFERENCES, "match pattern")
                        )
        shown = ".".join(parts) if parts is not None else "<expr>"
        for name in names:
            if cls is not None and parts is not None:
                self._resolve([*parts, name])
            else:
                self.indexer.out.unresolved.add(
                    UnresolvedReference(
                        self.source, UnresolvedKind.ATTRIBUTE, name, f"case {shown}({name}=...)"
                    )
                )
        self.generic_visit(node)

    def _match_args(self, cls: str) -> tuple[str, ...] | None:
        """``__match_args__`` as the first class in ``cls``'s MRO writes it
        out; None when none does before a class the index cannot see."""
        for cid in self.indexer._mro(cls):
            cscope = self.indexer.class_scopes.get(cid)
            if cscope is None:
                return None
            if cscope.match_args is not None:
                return cscope.match_args
            if "__match_args__" in cscope.bindings or cscope.opaque:
                return None
        return None

    def visit_List(self, node: ast.List) -> None:
        observe_command(self, node)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        observe_command(self, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return

    def _mutation_target(self, expr: ast.expr, whole: bool = False) -> None:
        """Record ``source`` as a writer when ``expr`` (an assignment target or
        a receiver of a mutating call) is a subscript/attribute of a variable
        symbol, or the variable itself under ``global``; or another module's
        variable assigned through the module (``settings.DEBUG = True`` in a
        conftest: whoever reads ``settings.DEBUG`` sees the writer). Items
        count on the way (``store._CACHE["k"].append(x)``, ``del
        store._CACHE["k"]``, ``store._CACHE["k"] += [x]``: W15). ``whole``:
        ``expr`` is itself bound or deleted (an assignment target), so a
        variable it names is rebound, which is an edge of its own; a change
        of what the variable holds is a value write (``_value_write``)."""
        inner = expr
        while isinstance(inner, (ast.Subscript, ast.Attribute)):
            chain = _flatten_chain(inner) if isinstance(inner, ast.Attribute) else None
            rebound = whole and inner is expr
            inner = inner.value
            if chain is not None and len(chain) > 1 and chain[0] not in self.scope.locals:
                target = self.indexer.resolve_chain(chain, self.scope)
                if isinstance(target, Resolved):
                    symbol = self.indexer.index.symbols.get(target.symbol)
                    if (
                        symbol is not None
                        and symbol.id != self.source
                        and symbol.module != self.scope.module.name
                        and (
                            (symbol.kind == SymbolKind.VARIABLE and not target.detail)
                            or (
                                symbol.kind == SymbolKind.MODULE
                                and target.detail.startswith("attribute:")
                            )
                        )
                    ):
                        if rebound:  # ``settings.DEBUG = True``
                            self.indexer.out.edges.add(
                                Edge(symbol.id, self.source, EdgeKind.REFERENCES, MUTATED_BY)
                            )
                        else:
                            self._value_write(symbol.id)
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
            if symbol is None or symbol.kind != SymbolKind.VARIABLE or symbol.id == self.source:
                return
            if self.skip_defs and symbol.module == self.scope.module.name:
                return  # a module's own top-level mutations are part of the variable's hash
            if whole and base is expr:  # ``global X; X = v``: rebound
                self.indexer.out.edges.add(
                    Edge(symbol.id, self.source, EdgeKind.REFERENCES, MUTATED_BY)
                )
            else:
                self._value_write(symbol.id)

    def _held_instance(
        self, target: ast.expr, value: ast.expr, annotation: ast.expr | None = None
    ) -> None:
        """Record what a binding of a module variable holds (``STORE =
        Store()``, ``global _S; _S = make()``, ``mod.X = Y``) where that may
        be an instance of an in-scope class, or the class: a write in place
        into the variable may be one into state the class holds
        (writes.variable_classes; audit round 6, T21)."""
        if isinstance(target, (ast.Tuple, ast.List)):
            pairs = (
                zip(target.elts, value.elts, strict=True)
                if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts)
                else ((elt, value) for elt in target.elts)
            )
            for elt, part in pairs:
                self._held_instance(elt, part)
            return
        if isinstance(target, ast.Starred):
            self._held_instance(target.value, value)
            return
        chain = _flatten_chain(target) if isinstance(target, (ast.Name, ast.Attribute)) else None
        if chain is None or self._is_local(chain[0]):
            return
        node = self.indexer.resolve_chain(chain, self.scope)
        if not isinstance(node, Resolved) or node.detail:
            return
        symbol = self.indexer.index.symbols.get(node.symbol)
        if symbol is None or symbol.kind != SymbolKind.VARIABLE:
            return
        origins = self._held_origins(value, 0)
        if annotation is not None:
            # ``STORE: Store = make()``: an instance of the class annotated.
            for sub in ast.walk(annotation):
                if isinstance(sub, (ast.Name, ast.Attribute)):
                    origins |= {
                        "class:" + o[len("type:") :]
                        for o in self._held_origins(sub, 0)
                        if o.startswith("type:")
                    }
        for origin in sorted(origins):
            self.indexer.out.instance_bindings.add((symbol.id, origin))

    def _held_origins(self, expr: ast.expr, depth: int) -> set[str]:
        """``_held_instance``'s origins of what ``expr`` evaluates to: an
        instance of an in-scope class called (``class:``; the id may be a
        module variable's, called as a class: ``Legacy()``), an in-scope
        class itself (``type:``: ``Alias = Conf``; audit round 7, U2), an
        in-scope function or method called (``call:``), a module variable
        (``var:``), through conditions, ``or``/``and``, ``await``, a
        container display's items, a local bound once and what any call but
        a fresh value's may give back of what it is handed (``dict(dev=
        Config)``, ``ident(Conf)``)."""
        if depth > 8:
            return set()
        if isinstance(expr, ast.IfExp):
            return self._held_origins(expr.body, depth + 1) | self._held_origins(
                expr.orelse, depth + 1
            )
        if isinstance(expr, ast.BoolOp):
            return {o for v in expr.values for o in self._held_origins(v, depth + 1)}
        if isinstance(expr, (ast.NamedExpr, ast.Await, ast.Starred)):
            return self._held_origins(expr.value, depth + 1)
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            # A container display: an instance in it is reached through it.
            items = expr.values if isinstance(expr, ast.Dict) else expr.elts
            return {o for v in items for o in self._held_origins(v, depth + 1)}
        if isinstance(expr, ast.Name) and self._is_local(expr.id):
            source = self._local_source(expr.id)
            return self._held_origins(source, depth + 1) if source is not None else set()
        call = isinstance(expr, ast.Call)
        handed: set[str] = set()
        if isinstance(expr, ast.Call):
            if (made := self._made_instance(expr)) is not None:
                return {f"class:{made[1]}"}  # ``Store.__new__(Store)`` too (T22)
            func = _flatten_chain(expr.func)
            fresh = func is not None and (
                (len(func) == 1 and func[0] in FRESH_CALLS and not self._is_shadowed(func[0]))
                or (len(func) > 1 and not self._is_local(func[0]) and self._only(func, FRESH_CALLS))
            )
            if not fresh:
                for arg in [*expr.args, *(k.value for k in expr.keywords)]:
                    handed |= self._held_origins(arg, depth + 1)
        parts = _flatten_chain(expr.func if isinstance(expr, ast.Call) else expr)
        if parts is None or self._is_local(parts[0]):
            return handed
        node = self.indexer.resolve_chain(parts, self.scope)
        if not isinstance(node, Resolved) or node.detail:
            return handed
        symbol = self.indexer.index.symbols.get(node.symbol)
        if symbol is None:
            return handed
        if not call and symbol.kind == SymbolKind.CLASS:
            return {f"type:{symbol.id}"}
        if call and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
            return {f"call:{symbol.id}"} | handed
        if not call and symbol.kind == SymbolKind.VARIABLE:
            return {f"var:{symbol.id}"}
        return handed

    def _value_write(self, state: str) -> None:
        """A change in place of what ``state`` (a module variable, class or
        function) holds, made by ``source``: a ``mutated_by`` edge after pass
        2 unless the variable cannot change in place (``value_writes``,
        Indexer._value_writes; audit round 6, T20)."""
        self.indexer.out.value_writes.add((state, self.source))

    def _rooted_write(
        self,
        obj: ast.expr,
        *,
        call: bool = False,
        bound: bool = False,
        attr: str = "",
        member: str = "",
    ) -> None:
        """A change in place of ``obj`` (an item or attribute stored into it,
        a mutator called on it): a
        ``mutated_by`` edge from each module variable, class or function
        whose state ``obj`` may be part of (``_state_roots``; audit round 6,
        T6 and T7), so that every reader of it reads this write. Through a
        name bound to a copy (``c = dict(_D)``, ``c = [*_L]``), changing the
        name's own object changes nothing else; changing one of its elements
        does. A method called on a class itself (``Base.__init__(self)``,
        ``call``) is no change of the class. ``bound``: a bound mutator taken
        as a value (``_put = _CACHE.__setitem__``), which writes when it is
        called, so a module-level one is no write the module's own hash
        covers: whoever calls the variable holding it writes. ``attr``: the
        attribute of ``obj`` itself bound or deleted (``new.x = v``; ``*``
        for a name only known at run time), which on a fresh instance
        changes that object alone (audit round 6, T22), and changes nothing
        a constructor was handed unless binding runs code of the class
        (``HELD``: ``held_binds``, decided after pass 2; T23). ``member``:
        the attribute of ``obj`` an augmented assignment changes
        (``type(o).n += 1``), which names what a class of unknown type
        (``ANY_CLASS``) may own (T25)."""
        if call and self._names_definition(obj):
            return
        roots = self._state_roots(obj, frozenset())
        own = isinstance(obj, ast.Name) and self._is_local(obj.id)
        # An item stored into, or a mutator called on, a variable's own
        # object changes that container, not a class it holds (U2).
        container = not attr and not member and self._direct(obj, 0)
        # The in-scope functions whose returned object ``obj`` itself is.
        immediate = self._immediate_results(obj, frozenset())
        out = self.indexer.out
        for root, copy in sorted(roots.items()):
            if root == _NAMESPACE or (copy and own):
                continue
            held = held_root(root)
            if held is not None:
                # What the constructor of a fresh instance was handed: changed
                # in place by a change in place of the instance (which may
                # run its methods), not by binding one of its attributes,
                # unless that runs code of the class (T23).
                cls, inner = held
                for fact in self._root_facts(inner, bound=bound, attr=""):
                    if attr:
                        out.held_binds.add((cls, attr, self.source, *fact))
                    else:
                        apply_fact(out, self.source, fact)
                continue
            direct = root.startswith(RESULT) and root[len(RESULT) :] in immediate
            facts = self._root_facts(
                root, bound=bound, attr=attr, member=member, container=container, direct=direct
            )
            for fact in facts:
                apply_fact(out, self.source, fact)

    def _direct(self, expr: ast.expr, depth: int) -> bool:
        """Whether ``expr`` is a module variable's (or class's) own object:
        its name, a chain to it, or a local every binding of which is one."""
        if isinstance(expr, ast.Attribute):
            chain = _flatten_chain(expr)
            if chain is None or self._is_local(chain[0]) or chain[0] == self.scope.self_name:
                return False
            node = self.indexer.resolve_chain(chain, self.scope)
            return isinstance(node, Resolved) and not node.detail
        if not isinstance(expr, ast.Name):
            return False
        if not self._is_local(expr.id):
            return True
        owner = self._binding_scope(expr.id)
        if owner is None or depth > 3:
            return False
        sources = self._sources_in(owner).get(expr.id, ())
        return bool(sources) and all(
            how == VALUE and self._direct(value, depth + 1) for value, how in sources
        )

    def _root_facts(
        self,
        root: str,
        *,
        bound: bool,
        attr: str,
        member: str = "",
        container: bool = False,
        direct: bool = False,
    ) -> list[tuple[str, str, str]]:
        """What a write into what ``root`` names records (``apply_fact``):
        ``value``, a change in place of a module variable, class or
        function (Indexer._value_writes); ``attribute``, of what a class
        attribute holds (Indexer._class_attribute_writes); ``instance``, of an
        instance of an in-scope class or part of one (Indexer._instance_writes);
        ``result``, of what a call of an in-scope function returns
        (Indexer._result_writes; ``direct_result`` when the write is into
        the returned object itself, ``direct``); ``own``, a class (or a
        variable that may hold one) written by its own module's top-level
        code, which is part of the module's hash but gives the class the
        attribute ``attr``, any for ``*`` or a change in place of the class
        itself (T24, U3); ``bind``, the same by any other code, besides its
        ``value`` edge (U3); ``typed``, a change of a variable that writes
        the class it may hold (anything but a ``container`` change of the
        variable's own object, U2); ``anyclass``, of a class of unknown type
        (``type(o).x = v``): the attribute it binds or is reached through
        (``*``: any), and the writer's module for code its hash covers,
        decided after pass 2 (writes.AnyClassOwners; T25)."""
        here = self.scope.module.name
        # A write in a lambda's body runs when the lambda is called, not
        # where it stands, as a bound mutator taken as a value does: at
        # module level, in a class body or in a parameter default it is no
        # write the module's own hash covers, and whoever holds the lambda
        # (the variable, the class, the function whose default it is)
        # writes (audit round 7, U19).
        bound = bound or any(isinstance(n, ast.Lambda) for n in self._stack)
        unhashed = not self.skip_defs or (bound and self.source != here)
        symbols = self.indexer.index.symbols
        reached = any_class_root(root)
        if reached is not None:
            name = reached or attr or member or "*"
            return [("anyclass", "*" if name == "__dict__" else name, "" if unhashed else here)]
        made = instance_root(root)
        if made is not None:
            # An instance of an in-scope class (or part of one): a write
            # of the class's state when the class holds any, decided
            # after pass 2 (Indexer._instance_writes).
            how, cls = made
            if root.startswith(FRESH):
                if not attr:
                    how = ""  # a write in place into the instance itself
                elif how == "object":
                    return []  # nothing but ``object.__new__`` made it
            symbol = symbols.get(cls)
            if symbol is None or (symbol.module == here and not unhashed):
                return []
            return [("instance", cls, how)]
        if root.startswith(RESULT):
            # What a call returns, known after pass 2 (a copy included:
            # what it returns may hold the state it copied, though a write
            # into the copy itself changes none: ``direct``).
            kind = "direct_result" if direct else "result"
            return [(kind, root[len(RESULT) :], here if self.skip_defs and not bound else "")]
        named = class_attribute_root(root)
        if named is not None:
            cls, name = named
            symbol = symbols.get(cls)
            if symbol is None or cls == self.source:
                return []
            if symbol.module == here and not unhashed:
                # Part of the module's hash, but what the attribute holds is
                # no longer what its class body bound (a literal table
                # changed: writes.apply_writes).
                return [("own", cls, name)]
            return [("attribute", cls, name)]
        symbol = symbols.get(root)
        if symbol is None or symbol.id == self.source:
            return []
        name = attr or member
        if symbol.module == here and not unhashed:
            # A module's own top-level code is part of its hash, but gives a
            # class it writes the attribute it binds (``_c = TD; _c.X = []``
            # through a variable holding the class: U2).
            if symbol.kind == SymbolKind.CLASS:
                return [("own", symbol.id, name or "*")]
            if symbol.kind == SymbolKind.VARIABLE and not container:
                return [("own", symbol.id, name or "*")]
            return []
        found = [("value", symbol.id, "")]
        if symbol.kind == SymbolKind.CLASS and name:
            found.append(("bind", symbol.id, name))
        elif symbol.kind == SymbolKind.VARIABLE and not container:
            found.append(("typed", symbol.id, ""))
            if name:
                found.append(("bind", symbol.id, name))
        return found

    def _handed_state(self, expr: ast.expr, receiver: bool = False) -> list[str]:
        """The module variables and classes whose state a call is handed in
        ``expr`` (an argument; ``receiver``: the object a method is called
        on) where writes.py's ``_handed`` names none: through a name the
        function binds to part of one (any binding), an item or a method's
        result, a class itself as an argument (``configure(Config)``). A
        copy is no state handed on; nor is a class a method is called on
        (``Base.method(self)``: a classmethod writing ``cls`` is a write of
        its own). As writes.py's passes give them (``_handed_root``):
        ``var:<id>`` for a module variable or class, ``attr:<attribute>:<class
        id>`` for what a class attribute holds (T24), ``instance:<how>:<class
        id>`` for an instance of an in-scope class or part of one (a write of
        the class's state when it holds any: audit round 6, T22),
        ``anyclass:<attribute>`` for the class of an object of unknown type
        (T25), each one held by a fresh instance as ``held:<class>|...``
        (T23)."""
        if receiver and self._names_definition(expr):
            return []
        found: list[str] = []
        for root, copy in sorted(self._state_roots(expr, frozenset()).items()):
            if copy or root == self.source:
                continue
            if receiver and any_class_root(root) == "":
                continue  # ``type(o).register(x)``: the method's ``cls`` writes are its own
            held = held_root(root)
            if held is not None:
                # Part of a fresh instance its constructor was handed: written
                # when the callee may change it (writes.apply_writes; T23).
                found += [f"{HELD_WHAT}{held[0]}|{w}" for w in self._handed_root(held[1])]
            else:
                found += self._handed_root(root)
        return list(dict.fromkeys(found))

    def _handed_root(self, root: str) -> list[str]:
        """``_handed_state`` of one root: ``var:<id>`` for a module variable
        or class, ``attr:<attribute>:<class id>`` for what a class attribute
        holds (T24), ``instance:<how>:<id>`` for an instance of an in-scope
        class or part of one (the id a module variable's for an instance of
        the class it holds: U2), ``anyclass:<attribute>`` for a class of
        unknown type."""
        symbols = self.indexer.index.symbols
        made = instance_root(root)
        if made is not None:
            if made[1] in symbols:
                how = made[0] if root.startswith(INSTANCE) else ""
                return [f"instance:{how}:{made[1]}"]
            return []
        named = class_attribute_root(root)
        if named is not None:
            return [f"attr:{named[1]}:{named[0]}"] if named[0] in symbols else []
        member = any_class_root(root)
        if member is not None:
            return [f"{ANY_CLASS_WHAT}{member}"]  # a class of unknown type (T25)
        symbol = symbols.get(root)
        if symbol is not None and symbol.kind in (SymbolKind.VARIABLE, SymbolKind.CLASS):
            return [f"var:{root}"]
        return []

    def _target_write(self, target: ast.expr, *, bind: bool = True) -> None:
        """``_rooted_write`` for each item or attribute an assignment or
        ``del`` target stores into (each element of an unpacking). ``bind``:
        an attribute target is bound or deleted, not changed in place as
        ``new.items += [x]`` may change it."""
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._target_write(elt, bind=bind)
        elif isinstance(target, ast.Starred):
            self._target_write(target.value, bind=bind)
        elif isinstance(target, ast.Subscript):
            self._rooted_write(target.value)
        elif isinstance(target, ast.Attribute):
            self._rooted_write(target.value, attr=target.attr if bind else "", member=target.attr)

    def _is_local(self, name: str) -> bool:
        """A name the function binds itself (no parameter default's alias,
        no import, not its ``self``/``cls``)."""
        scope = self.scope
        return (
            name in scope.locals
            and name not in scope.param_aliases
            and name not in scope.local_imports
            and name != scope.self_name
        )

    def _names_definition(self, expr: ast.expr) -> bool:
        """``expr`` names a class or function itself (``Config``, ``cls``,
        ``type(self)``), not state held on one."""
        scope = self.scope
        if isinstance(expr, ast.Name) and expr.id == scope.self_name:
            return scope.self_is_class
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "type"
            and len(expr.args) == 1
            and self._is_self(expr.args[0])
        ):
            return True
        parts = _flatten_chain(expr)
        if parts is None or self._is_local(parts[0]) or parts[0] == scope.self_name:
            return False
        node = self.indexer.resolve_chain(parts, scope)
        if not isinstance(node, Resolved) or node.detail:
            return False
        symbol = self.indexer.index.symbols.get(node.symbol)
        return symbol is not None and symbol.kind in (
            SymbolKind.CLASS,
            SymbolKind.FUNCTION,
            SymbolKind.METHOD,
        )

    def _names_function(self, expr: ast.expr) -> bool:
        """``expr`` names an in-scope function or method itself."""
        parts = _flatten_chain(expr)
        if parts is None or self._is_local(parts[0]) or parts[0] == self.scope.self_name:
            return False
        node = self.indexer.resolve_chain(parts, self.scope)
        if not isinstance(node, Resolved) or node.detail:
            return False
        symbol = self.indexer.index.symbols.get(node.symbol)
        return symbol is not None and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)

    def _module_named(self, expr: ast.expr) -> str | None:
        """The in-scope module ``expr`` is: a name bound to it, or one
        obtained by a literal name (``import_module("m")``,
        ``sys.modules["m"]``, a local bound once to one of those)."""
        found = self._obtained_module(expr)
        if found is None and isinstance(expr, ast.Name) and self._is_local(expr.id):
            source = self._local_source(expr.id)
            found = self._obtained_module(source) if source is not None else None
        if found is not None:
            return found if found in self.indexer.scopes else None
        chain = _flatten_chain(expr)
        if chain is None or self._is_local(chain[0]) or chain[0] == self.scope.self_name:
            return None
        node = self.indexer.resolve_chain(chain, self.scope)
        if isinstance(node, ModuleNode) and node.module in self.indexer.scopes:
            return node.module
        return None

    def _module_members_named(
        self, module: ast.expr, key: ast.expr | None
    ) -> dict[str, bool] | None:
        """The roots of the members of the in-scope module ``module`` names
        that ``key`` may name (``getattr(state, "Conf")``, ``vars(state)
        ["Conf"]``, ``state.__dict__["Conf"]``): each of its variables and
        classes when nothing bounds the key. None when ``module`` is no
        in-scope module (audit round 7, U2)."""
        name = self._module_named(module)
        if name is None:
            return None
        names = self.scope.string_candidates(key) if key is not None else None
        symbols = self.indexer.index.symbols
        if names is None:
            return {
                s.id: False
                for s in symbols.values()
                if s.module == name
                and s.container == name
                and s.kind in (SymbolKind.VARIABLE, SymbolKind.CLASS)
            }
        found: dict[str, bool] = {}
        for member in names:
            node = self.indexer.resolve_dotted([*name.split("."), member])
            found = _merged(found, self._symbol_roots(node) if node is not None else {})
        return found

    def _state_roots(self, expr: ast.AST, seen: frozenset) -> dict[str, bool]:
        """The module variables, classes and functions whose state ``expr``
        may be (part of): symbol id -> whether ``expr`` is only a new
        container of its elements (``dict(X)``, ``[*X]``, ``X.copy()``,
        ``X[1:]``). A name the function binds is what each of its bindings
        gives it (``_alias_roots``); an item, an attribute or a method's
        result (``X.get(k)``, ``X.setdefault(k, {})``) is part of what it is
        taken from; a call's result may be any of its arguments, its
        receiver included (unless it is a fresh value or a copy:
        FRESH_CALLS, SHALLOW_CALLS), but no class or function named as one;
        an instance an in-scope class makes is ``FRESH`` (``_made_instance``);
        ``type(self)``, ``self.__class__`` and a classmethod's ``cls`` are
        the class, and ``self.<attr>`` is the class when its body binds
        ``attr`` and the instance gets no object of its own
        (``_class_held``). ``globals()`` is this module's namespace, an item
        of it the module's name of that key, or each of them when no literal
        gives the key."""
        if isinstance(expr, (ast.NamedExpr, ast.Await, ast.Starred)):
            return self._state_roots(expr.value, seen)
        if isinstance(expr, ast.Name):
            return self._name_roots(expr.id, seen)
        if isinstance(expr, ast.Attribute):
            return self._attribute_roots(expr, seen)
        if isinstance(expr, ast.Subscript):
            namespace = self._namespace_of(expr.value)
            if namespace is not None:
                # ``vars(state)["Conf"]``, ``state.__dict__["Conf"]`` (U2).
                members = self._module_members_named(namespace, expr.slice)
                if members is not None:
                    return members
            inner = self._state_roots(expr.value, seen)
            if _NAMESPACE in inner:
                return self._namespace_item(expr.slice)
            return _part_of(inner, copy=isinstance(expr.slice, ast.Slice))
        if isinstance(expr, ast.Call):
            return self._call_roots(expr, seen)
        if isinstance(expr, ast.IfExp):
            return _merged(self._state_roots(expr.body, seen), self._state_roots(expr.orelse, seen))
        parts: list[ast.AST] = []
        if isinstance(expr, ast.BoolOp):
            return _merged(*(self._state_roots(v, seen) for v in expr.values))
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            parts = list(expr.elts)
        elif isinstance(expr, ast.Dict):
            parts = list(expr.values)
        elif isinstance(expr, ast.BinOp):
            parts = [expr.left, expr.right]
        elif isinstance(expr, _COMPREHENSIONS):
            parts = [g.iter for g in expr.generators]
        return _part_of(_merged(*(self._state_roots(p, seen) for p in parts)), copy=True)

    def _namespace_of(self, expr: ast.expr) -> ast.expr | None:
        """``x`` of ``vars(x)`` or ``x.__dict__``: whose namespace ``expr``
        is."""
        if isinstance(expr, ast.Attribute) and expr.attr == "__dict__":
            return expr.value
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "vars"
            and not self._is_shadowed("vars")
            and len(expr.args) == 1
            and not expr.keywords
        ):
            return expr.args[0]
        return None

    def _name_roots(self, name: str, seen: frozenset) -> dict[str, bool]:
        scope = self.scope
        if name == scope.self_name and scope.self_class is not None:
            return {scope.self_class: False} if scope.self_is_class else {}
        if self._is_local(name):
            return self._alias_roots(name, seen)
        return self._symbol_roots(self.indexer.resolve_chain([name], scope))

    def _symbol_roots(self, node: Node) -> dict[str, bool]:
        if not isinstance(node, Resolved) or node.detail:
            return {}
        symbol = self.indexer.index.symbols.get(node.symbol)
        if symbol is None or symbol.kind not in (
            SymbolKind.VARIABLE,
            SymbolKind.CLASS,
            SymbolKind.FUNCTION,
        ):
            return {}
        return {symbol.id: False}

    def _alias_roots(self, name: str, seen: frozenset) -> dict[str, bool]:
        """What every binding the function makes of ``name`` gives it
        (aliases.name_sources): ``c = _CACHE``, ``(c := _CACHE)``, ``for c
        in (_CACHE,)``, ``with nullcontext(_CACHE) as c``, ``d =
        _CACHE["a"]``, ``e = _CACHE.setdefault(k, {})``, and what its object
        is stored into (``_CACHE[k] = entry = {}``, ``_CACHE.append(buf)``);
        anywhere in the function, since a loop may run a write after a
        later binding."""
        owner = self._binding_scope(name)
        if owner is None or (id(owner), name) in seen:
            return {}
        seen = seen | {(id(owner), name)}
        found: dict[str, bool] = {}
        for value, how in self._sources_in(owner).get(name, ()):
            roots = self._state_roots(value, seen)
            if how != VALUE:  # an element, what entering gives, a holder
                roots = _part_of(roots, copy=False)
            found = _merged(found, roots)
        if isinstance(owner, (*FUNC_NODES, ast.Lambda)):
            # A parameter's default (``def fill(k, klass=Conf)``): what it
            # names, unless a call hands it something else (U2).
            for param, default in _defaults(owner.args):
                chain = _flatten_chain(default) if param == name else None
                if chain is not None and chain[0] not in _parameter_names(owner.args):
                    node = self.indexer.resolve_chain(chain, self.scope)
                    found = _merged(found, self._resolved_roots(node, chain[-1]) or {})
        return found

    def _sources_in(self, owner: ast.AST) -> dict[str, list[tuple[ast.expr, str]]]:
        kept = self._source_maps.get(id(owner))
        if kept is None or kept[0] is not owner:
            kept = self._source_maps[id(owner)] = (owner, name_sources(owner))
        return kept[1]

    def _binding_scope(self, name: str) -> ast.AST | None:
        """The function, lambda or comprehension whose own name ``name`` is
        here: the innermost one being visited that binds it, or the
        function this collector walks."""
        for node in reversed(self._stack):
            if isinstance(node, _COMPREHENSIONS):
                if any(name in _collect_store_names(g.target) for g in node.generators):
                    return node
            elif isinstance(node, (*FUNC_NODES, ast.Lambda)):
                bound = self._bound_maps.get(id(node))
                if bound is None or bound[0] is not node:
                    bound = self._bound_maps[id(node)] = (node, _LocalBindings().collect(node))
                if name in bound[1]:
                    return node
        root = self.scope
        while root.literal_parent is not None:
            root = root.literal_parent
        node = root.literal_node
        return node if isinstance(node, (*FUNC_NODES, ast.Lambda)) else None

    def _attribute_roots(self, expr: ast.Attribute, seen: frozenset) -> dict[str, bool]:
        scope = self.scope
        if expr.attr == "__class__" and self._is_self(expr.value) and not scope.self_is_class:
            return {scope.self_class: False} if scope.self_class else {}
        if expr.attr in CLASS_LISTS:
            return self._class_list_roots(expr, seen)
        chain = _flatten_chain(expr)
        if chain is not None and not self._is_local(chain[0]):
            if chain[0] == scope.self_name and not scope.self_is_class:
                if len(chain) == 2 and scope.self_class is not None:
                    return self._instance_roots(chain)
            else:
                found = self._resolved_roots(self.indexer.resolve_chain(chain, scope), chain[-1])
                if found is not None:
                    return found
        if (held := self._module_chain(expr)) is not None:
            # ``import_module("pkg.state").Conf``, ``sys.modules[...].X``:
            # the member of the module named by a literal (U2).
            found = self._resolved_roots(self.indexer.resolve_dotted(held), held[-1])
            if found is not None:
                return found
        inner = self._state_roots(expr.value, seen)
        if expr.attr == "__class__":
            return _class_of(inner, self._made_here(expr.value))
        return _part_of(inner, copy=False, attr=expr.attr)

    def _resolved_roots(self, node: Node, attr: str) -> dict[str, bool] | None:
        """The roots of what a chain ending in ``attr`` resolves to: a module
        variable or a class itself, or what a class attribute holds; None
        for anything else."""
        if not isinstance(node, Resolved):
            return None
        symbol = self.indexer.index.symbols.get(node.symbol)
        if symbol is not None and symbol.kind == SymbolKind.VARIABLE:
            if not node.detail:
                return {symbol.id: False}
        elif symbol is not None and symbol.kind == SymbolKind.CLASS:
            if node.detail == "":
                return {symbol.id: False}
            if node.detail == f"attribute:{attr}":
                # What a class attribute holds (T24): the class's state,
                # unless it cannot change in place.
                found = {_class_attribute(symbol.id, attr): False}
                for other, detail in node.overrides:
                    if detail == f"attribute:{attr}":
                        found[_class_attribute(other, attr)] = False
                return found
        return None

    def _class_list_roots(self, expr: ast.Attribute, seen: frozenset) -> dict[str, bool]:
        """``X.__mro__``, ``X.__bases__``, ``X.__base__``: the classes they
        hold (an item of them is one; U2). Of a class the analysis knows,
        each in-scope class of its MRO or of a subclass's (``type(self)``
        and ``cls`` may be one); of a variable that may hold a class, the
        variable (decided after pass 2); of a class of unknown type, a
        class of unknown type."""
        found: dict[str, bool] = {}
        for root in self._state_roots(expr.value, seen):
            if root == ANY_CLASS:
                found[ANY_CLASS] = False
            elif root in self.indexer.class_scopes:
                for sub in (root, *self.indexer._descendants.get(root, ())):
                    for cls in self.indexer._mro(sub):
                        found[cls] = False
            elif any_class_root(root) is None and instance_root(root) is None:
                found = _merged(found, _part_of({root: False}, copy=False))
        return found

    def _made_here(self, expr: ast.expr, depth: int = 0) -> bool:
        """Whether ``expr`` is certainly an object made where it is used: a
        construction of an in-scope class or a ``__new__`` called with one
        (``_made_instance``), a mock (``MOCK_CALLS``), a display or literal,
        or a local name every binding of which is one of those (no
        parameter). Its class is then known (T25)."""
        if isinstance(expr, ast.Call):
            made = self._made_instance(expr)
            if made is not None and made[1] in self.indexer.class_scopes:
                return True
            parts = _flatten_chain(expr.func)
            return (
                parts is not None and not self._is_local(parts[0]) and self._only(parts, MOCK_CALLS)
            )
        if isinstance(expr, (ast.Constant, ast.JoinedStr, ast.List, ast.Dict, ast.Set, ast.Tuple)):
            return True
        if isinstance(expr, _COMPREHENSIONS):
            return True
        if not isinstance(expr, ast.Name) or depth > 3 or expr.id in self.scope.params:
            return False
        if not self._is_local(expr.id):
            return False
        owner = self._binding_scope(expr.id)
        if owner is None:
            return False
        sources = self._sources_in(owner).get(expr.id, ())
        return bool(sources) and all(
            how == VALUE and self._made_here(value, depth + 1) for value, how in sources
        )

    def _instance_roots(self, chain: list[str]) -> dict[str, bool]:
        """``self.<attr>``: the class whose body binds ``attr`` (and each
        subclass binding it again), when the instance has no object of its
        own there; a class nested in the class (``self.Meta``); and
        otherwise what the instance holds under ``attr``, which is the
        instance's own object or not as decided after pass 2 (an attribute
        given to the class outside its body, or bound in a method to an
        object that may be shared: Indexer._instance_writes; audit round
        7, U3, U11)."""
        cls = self.scope.self_class
        assert cls is not None
        node = self.indexer.resolve_chain(chain, self.scope)
        attr = chain[1]
        # A method of the class storing ``self.<attr>`` gives the instance an
        # object of its own there, unless that object is shared (``-``);
        # otherwise the class may be given it elsewhere (``.``).
        owned = f"{INSTANCE}{SHARED_ONLY}{attr}:{cls}"
        if not isinstance(node, Resolved):
            if self._class_held(cls, attr):
                return {f"{INSTANCE}.{attr}:{cls}": False}
            return {owned: False}
        found: dict[str, bool] = {}
        symbols = self.indexer.index.symbols
        for cid, detail in [(node.symbol, node.detail), *node.overrides]:
            if detail == f"attribute:{attr}":
                held = self._class_held(cid, attr)
                found[_class_attribute(cid, attr) if held else owned] = False
            elif not detail and (s := symbols.get(cid)) is not None and s.kind == SymbolKind.CLASS:
                found[cid] = False  # a nested class (U11)
        return found

    def _class_held(self, cid: str, attr: str) -> bool:
        """Whether ``attr``, which class ``cid``'s body binds, is the class's
        own object for its instances: neither that class nor the class of
        ``self`` stores ``self.<attr>`` in a method, or binds it to a field
        each instance gets (``field(...)``, an annotation alone). A class of
        another module is not read here: its attribute counts as held."""
        for cls in dict.fromkeys((cid, self.scope.self_class)):
            node = self._class_node(cls) if cls is not None else None
            if node is not None and _instance_owned(node, attr):
                return False
        return True

    def _class_node(self, cid: str) -> ast.ClassDef | None:
        """The ``class`` statement of an in-scope class of this module."""
        module = self.scope.module
        symbol = self.indexer.index.symbols.get(cid)
        if module.tree is None or symbol is None or symbol.module != module.name:
            return None
        found = self._class_nodes.get(cid)
        if found is None or found[0] is not module.tree:
            path = cid[len(module.name) + 1 :].split(".")
            found = self._class_nodes[cid] = (module.tree, _find_class(module.tree.body, path))
        return found[1]

    def _class_named(self, expr: ast.expr, variables: bool = False, depth: int = 0) -> str | None:
        """The in-scope class ``expr`` names: a class (``Store``), a
        classmethod's ``cls``, ``type(self)`` or ``self.__class__`` (the
        method's class, or a subclass of it), a local every binding of which
        names the same one (``c = Store``), ``functools.partial`` of one;
        with ``variables``, a module variable too (``Legacy = Store``),
        whose class is known after pass 2 (audit round 7, U2)."""
        scope = self.scope
        if depth > 4:
            return None
        if isinstance(expr, ast.Call) and expr.args and not isinstance(expr.args[0], ast.Starred):
            func = _flatten_chain(expr.func)
            if func is not None and not self._is_local(func[0]) and self._only(func, PARTIALS):
                return self._class_named(expr.args[0], variables, depth + 1)
        if isinstance(expr, ast.Name) and self._is_local(expr.id) and expr.id != scope.self_name:
            owner = self._binding_scope(expr.id)
            sources = self._sources_in(owner).get(expr.id, ()) if owner is not None else ()
            named = {
                self._class_named(value, variables, depth + 1) if how == VALUE else None
                for value, how in sources
            }
            return named.pop() if len(named) == 1 else None
        if scope.self_class is not None:
            if isinstance(expr, ast.Name) and expr.id == scope.self_name:
                return scope.self_class if scope.self_is_class else None
            if not scope.self_is_class and (
                (
                    isinstance(expr, ast.Attribute)
                    and expr.attr == "__class__"
                    and self._is_self(expr.value)
                )
                or (
                    isinstance(expr, ast.Call)
                    and isinstance(expr.func, ast.Name)
                    and expr.func.id == "type"
                    and not self._is_shadowed("type")
                    and len(expr.args) == 1
                    and not expr.keywords
                    and self._is_self(expr.args[0])
                )
            ):
                return scope.self_class
        parts = _flatten_chain(expr)
        if parts is None or self._is_local(parts[0]) or parts[0] == scope.self_name:
            return None
        node = self.indexer.resolve_chain(parts, scope)
        if not isinstance(node, Resolved) or node.detail:
            return None
        symbol = self.indexer.index.symbols.get(node.symbol)
        if symbol is None:
            return None
        if symbol.kind == SymbolKind.CLASS:
            return symbol.id
        if variables and symbol.kind == SymbolKind.VARIABLE and symbol.name == parts[-1]:
            return symbol.id  # the variable itself, not a method of its value
        return None

    def _made_instance(self, node: ast.Call) -> tuple[str, str] | None:
        """(how, class id) when the call makes an instance of an in-scope
        class: a constructor call (``Store()``, ``cls()``, ``type(self)()``:
        ``call``) or ``__new__`` called with the class (``cls.__new__(cls)``,
        ``self.__class__.__new__(self.__class__)``: ``new``;
        ``super().__new__(cls)``: ``super``; ``object.__new__(cls)``:
        ``object``). Not a ``__new__`` of a class outside the instance's
        MRO, which may give anything back."""
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "__new__":
            if not node.args or isinstance(node.args[0], ast.Starred):
                return None
            cls = self._class_named(node.args[0])
            if cls is None:
                return None
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id == "object":
                return ("object", cls) if not self._is_shadowed("object") else None
            if (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Name)
                and receiver.func.id == "super"
                and not self._is_shadowed("super")
            ):
                return "super", cls
            on = self._class_named(receiver)
            if on is None:
                return None
            return ("new", cls) if on in self.indexer._mro(cls) else None
        cls = self._class_named(func, variables=True)
        return ("call", cls) if cls is not None else None

    def _new_results(self, cls: str) -> dict[str, bool]:
        """What an in-scope ``__new__`` making ``cls`` (or a subclass of it)
        returns, joined after pass 2: the object may be one it keeps."""
        hit = self.indexer.lookup_in_class(cls, "__new__", dispatch=True)
        if not isinstance(hit, Resolved):
            return {}
        found = {}
        for symbol_id, detail in [(hit.symbol, hit.detail), *hit.overrides]:
            symbol = self.indexer.index.symbols.get(symbol_id)
            if not detail and symbol is not None and symbol.kind == SymbolKind.METHOD:
                found[f"{RESULT}{symbol_id}"] = False
        return found

    def _instance_roots_of(self, node: ast.Call, seen: frozenset) -> dict[str, bool] | None:
        """``_call_roots`` of a call making an instance of an in-scope class
        (``_made_instance``): the instance (``FRESH``), what an in-scope
        ``__new__`` returns and, as for any call, what its other arguments
        give it. The class itself, named as the call or its first
        argument, is no root: binding an attribute on the new object is no
        change of the class (audit round 6, T22)."""
        made = self._made_instance(node)
        if made is None:
            return None
        how, cls = made
        rest = [*node.args, *(k.value for k in node.keywords)]
        if how != "call":
            rest = rest[1:]  # the class handed to ``__new__``
        found = _part_of(
            _merged(*(self._state_roots(a, seen) for a in rest if not self._names_definition(a))),
            copy=False,
        )
        if how == "call":
            # What the constructor was handed is held by the new instance,
            # not the instance itself (T23).
            found = {_held(cls, root): copy for root, copy in found.items()}
        if how != "object" and cls in self.indexer.class_scopes:
            found = _merged(found, self._new_results(cls))
        return _merged(found, {f"{FRESH}{how}:{cls}": False})

    def _call_roots(self, node: ast.Call, seen: frozenset) -> dict[str, bool]:
        made = self._instance_roots_of(node, seen)
        if made is None:
            return self._plain_call_roots(node, seen)
        made_by = self._made_instance(node)
        if made_by is not None and made_by[1] in self.indexer.class_scopes:
            return made
        # A module variable called (``get = lambda: _CACHE``, ``Legacy =
        # Store``): an instance of a class it may hold, decided after pass 2,
        # or whatever else calling it hands back (U2, U7).
        return _merged(made, self._plain_call_roots(node, seen))

    def _plain_call_roots(self, node: ast.Call, seen: frozenset) -> dict[str, bool]:
        func = node.func
        args = [*node.args, *(k.value for k in node.keywords)]
        parts = _flatten_chain(func)
        copy = False
        if isinstance(func, ast.Name) and not self._is_shadowed(func.id):
            if func.id == "globals" and not args:
                return {_NAMESPACE: False}
            if func.id == "type" and len(node.args) == 1 and not node.keywords:
                if self._is_self(node.args[0]) and not self.scope.self_is_class:
                    return {self.scope.self_class: False} if self.scope.self_class else {}
                return _class_of(
                    self._state_roots(node.args[0], seen), self._made_here(node.args[0])
                )
            if func.id in FRESH_CALLS:
                return {}
            if func.id == "getattr" and len(node.args) in (2, 3) and not node.keywords:
                members = self._module_members_named(node.args[0], node.args[1])
                if members is not None:
                    # ``getattr(state, "Conf", Default)``: the member, or the
                    # default (a class handed in is a root as any value: U2).
                    rest = node.args[2:]
                    return _merged(members, *(self._state_roots(a, seen) for a in rest))
            copy = func.id in SHALLOW_CALLS
        elif parts is not None and not self._is_local(parts[0]):
            if self._only(parts, FRESH_CALLS):
                return {}
            if self._only(parts, JSON_LOADS) and not any(
                k.arg in JSON_HOOKS for k in node.keywords
            ):
                return {}
            if len(node.args) == 2 and not node.keywords and self._only(parts, CASTS):
                return self._state_roots(node.args[1], seen)  # ``cast(T, x)`` is ``x``
            copy = self._only(parts, SHALLOW_CALLS)
            for hooked in ("copy.copy", "copy.deepcopy"):
                if self._only(parts, frozenset({hooked})):
                    copy = self._copy_is_new(hooked, node.args[0] if node.args else None)
        # What a call returns may be anything it is handed, a class too
        # (``HANDLERS.get(k, Default)``, ``ident(Conf)``: U2), but not a
        # function named as one (``sorted(xs, key=f)``).
        found = _merged(
            *(self._state_roots(a, seen) for a in args if not self._names_function(a)),
            {f"{RESULT}{callee}": False for callee in self._callees(parts)},
            self._callable_roots(func, parts, seen),
        )
        if isinstance(func, ast.Attribute) and not self._names_definition(func.value):
            receiver = self._state_roots(func.value, seen)
            if _NAMESPACE in receiver:
                return self._namespace_method(func.attr, node)
            if receiver:
                copy = func.attr in ("copy", "__copy__") and self._copy_is_new(
                    func.attr, func.value
                )
                # A method of a class of unknown type gives what a class
                # owning that method holds (T25).
                receiver = {
                    (ANY_CLASS + func.attr if r == ANY_CLASS else r): c for r, c in receiver.items()
                }
                found = _merged(found, receiver)
        return _part_of(found, copy=copy)

    def _copy_is_new(self, how: str, value: ast.expr | None) -> bool:
        """Whether a copy (``how``: ``copy.copy``, ``copy.deepcopy``, a
        ``.copy()``/``.__copy__()`` method) of ``value`` is certainly a new
        object: ``value`` is a display or a module variable bound to one (a
        builtin container, whose copy is new whatever classes define), or,
        for a shallow copy, no in-scope class defines a special method
        (``COPY_HOOKS``) that may hand back the object itself. A deep copy
        of an object of unknown type is not: it may be a class, a function
        or an enum member, which ``deepcopy`` hands back as they are (audit
        round 7, U7)."""
        if value is None:
            return False
        if isinstance(value, (ast.Dict, ast.List, ast.Set, *_COMPREHENSIONS)):
            return True
        parts = _flatten_chain(value)
        if parts is not None and not self._is_local(parts[0]):
            node = self.indexer.resolve_chain(parts, self.scope)
            if isinstance(node, Resolved) and not node.detail:
                symbol = self.indexer.index.symbols.get(node.symbol)
                scope = self.indexer.scopes.get(symbol.module) if symbol is not None else None
                if (
                    symbol is not None
                    and symbol.kind == SymbolKind.VARIABLE
                    and scope is not None
                    and symbol.name in scope.containers
                ):
                    return True
        if how == "copy.deepcopy":
            return False
        hooks = COPY_HOOKS[how]
        return not any(
            name in cscope.members or name in cscope.bindings
            for cscope in self.indexer.class_scopes.values()
            for name in hooks
        )

    def _callable_roots(
        self, func: ast.expr, parts: list[str] | None, seen: frozenset
    ) -> dict[str, bool]:
        """What calling a callable that is no ``def`` or class hands back may
        be part of: a module variable holding it (``get = lambda: _CACHE``,
        ``get = partial(_get, _CACHE)``, ``get = lru_cache(_get)``, a closure
        a factory made), also through a local name bound to one or to a
        function, an item of a table of callables (``OPS["k"]()``) or a
        call's result (``factory()()``): ``RESULT`` of each, joined after
        pass 2 (Indexer._result_writes: a variable gives itself and what it
        refers to; audit round 7, U7)."""
        roots: dict[str, bool] = {}
        local = isinstance(func, ast.Name) and self._is_local(func.id)
        if local or isinstance(func, (ast.Subscript, ast.Call)):
            roots = self._state_roots(func, seen)
        elif parts is not None and not self._is_local(parts[0]):
            node = self.indexer.resolve_chain(parts, self.scope)
            if isinstance(node, Resolved) and not node.detail:
                roots = {node.symbol: False}
        found: dict[str, bool] = {}
        symbols = self.indexer.index.symbols
        for root in roots:
            if root.startswith(RESULT):
                found[root] = False
                continue
            symbol = symbols.get(root)
            if symbol is None:
                continue
            # A function named directly is ``_callees``'s.
            if symbol.kind == SymbolKind.VARIABLE or (
                symbol.kind == SymbolKind.FUNCTION and (local or not isinstance(func, ast.Name))
            ):
                found[f"{RESULT}{symbol.id}"] = False
        return found

    def _immediate_results(self, obj: ast.expr, seen: frozenset) -> frozenset[str]:
        """The in-scope functions of which ``obj`` is surely the returned
        object itself, not something made of it or holding it: a call of
        one (through ``cast``, ``await``), or a local every binding of which
        is one (``env = settings()``). A write into ``obj`` then changes
        nothing a ``return`` of theirs only copied (``return
        dict(_cached())``: Indexer._result_writes; audit round 7, U7)."""
        if isinstance(obj, (ast.Await, ast.NamedExpr)):
            return self._immediate_results(obj.value, seen)
        if isinstance(obj, ast.Call):
            parts = _flatten_chain(obj.func)
            if (
                parts is not None
                and len(obj.args) == 2
                and not obj.keywords
                and not self._is_local(parts[0])
                and self._only(parts, CASTS)
            ):
                return self._immediate_results(obj.args[1], seen)
            return frozenset(self._callees(parts))
        if isinstance(obj, ast.Name) and self._is_local(obj.id):
            owner = self._binding_scope(obj.id)
            if owner is None or (id(owner), obj.id) in seen:
                return frozenset()
            seen = seen | {(id(owner), obj.id)}
            sources = self._sources_in(owner).get(obj.id, ())
            found: set[str] = set()
            for value, how in sources:
                got = self._immediate_results(value, seen) if how == VALUE else frozenset()
                if not got:
                    return frozenset()
                found |= got
            return frozenset(found)
        return frozenset()

    def _callees(self, parts: list[str] | None) -> list[str]:
        """The in-scope functions and methods a call of ``parts`` runs, when
        it resolves (an override of a ``self`` method included): what they
        return is joined after pass 2 (Indexer._result_writes)."""
        if parts is None or self._is_local(parts[0]):
            return []
        node = self.indexer.resolve_chain(parts, self.scope)
        if not isinstance(node, Resolved):
            return []
        found = []
        for symbol_id, detail in [(node.symbol, node.detail), *node.overrides]:
            symbol = self.indexer.index.symbols.get(symbol_id)
            if (
                not detail
                and symbol is not None
                and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
            ):
                found.append(symbol_id)
        return found

    def visit_Return(self, node: ast.Return) -> None:
        """What the function returns (``return _CACHE``, ``return
        cls._instance``): state a caller writing into the result changes."""
        if (
            node.value is not None
            and self.scope.literal_parent is None
            and isinstance(self.scope.literal_node, FUNC_NODES)
        ):
            for root, copy in self._state_roots(node.value, frozenset()).items():
                held = held_root(root)
                if held is not None:
                    root = held[1]  # what a caller writing into the instance may change
                if root != _NAMESPACE and root != f"{RESULT}{self.source}":
                    self.indexer.out.state_returns.add((self.source, root))
                    if not copy or held is not None:
                        self.indexer.out.whole_returns.add((self.source, root))
            observe_return(self, node.value)
        self.generic_visit(node)

    def _namespace_item(self, key: ast.expr | None) -> dict[str, bool]:
        """``globals()[key]``: this module's names ``key`` may be, or each
        of its variables when nothing bounds it (``key`` None: any)."""
        module = self.scope.module.name
        symbols = self.indexer.index.symbols
        names = self.scope.string_candidates(key) if key is not None else None
        if names is None:
            return {
                s.id: False
                for s in symbols.values()
                if s.module == module and s.container == module and s.kind == SymbolKind.VARIABLE
            }
        found: dict[str, bool] = {}
        for name in names:
            found |= self._symbol_roots(Resolved(f"{module}.{name}"))
        return found

    def _namespace_method(self, attr: str, node: ast.Call) -> dict[str, bool]:
        if attr in _NAMESPACE_GETTERS and node.args:
            return self._namespace_item(node.args[0])
        if attr in ("values", "items"):
            return self._namespace_item(None)
        return {}

    def _argument_mutator(self, parts: list[str] | None) -> bool:
        """A call of a standard-library function that changes its first
        argument in place (writes.ARGUMENT_MUTATORS, as writes.observe_call
        reads it)."""
        return parts is not None and any(
            canonical in ARGUMENT_MUTATORS
            and (len(parts) > 1 or canonical != parts[0] or not self._is_shadowed(parts[0]))
            for canonical in self._canonical_names(parts)
        )

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

    def _looked_up(self, expr: ast.expr) -> list[str]:
        """What a lookup on ``expr`` looks on, when that is something of an
        external module, by its whole dotted name (``getattr(os.path, n)``
        looks on ``os.path``, not on ``os``): each name the chain may stand
        for under that module; empty when it is no external module's."""
        module = self._external_module(expr)
        if module is None:
            return []
        chain = _flatten_chain(expr)
        names = (
            sorted(
                n for n in self._canonical_names(chain) if n == module or n.startswith(module + ".")
            )
            if chain is not None
            else []
        )
        return names or [module]

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
        owner = self._self_class(node.value) or ""
        self.indexer.out.attr_values.add((owner, node.attr, self._attr_ok.pop(id(node), False)))
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

    def _instance_binding(self, value: ast.expr) -> list | None:
        """What ``self.<attr> = value`` binds when ``value`` makes an object
        of a known kind (audit round 3, W21): ``["instance", class id]`` for
        a construction of an in-scope class, ``["external"]`` for a call
        into a module outside the source roots, ``["factory", function
        id]`` for a call of an in-scope function (third-party only when its
        every return is, ``writes.returns_third_party``)."""
        if not isinstance(value, ast.Call):
            return None
        parts = _flatten_chain(value.func)
        if parts is None or (
            parts[0] in self.scope.locals and parts[0] not in self.scope.local_imports
        ):
            return None  # a parameter or local may hold anything
        target = self.indexer.resolve_chain(parts, self.scope)
        if isinstance(target, External):
            return ["external"]
        if (
            not isinstance(target, Resolved)
            or target.detail
            or target.receiver
            or target.overrides
            or target.uncertain_attr
            or target.alternatives
            or target.also
        ):
            return None
        symbol = self.indexer.index.symbols.get(target.symbol)
        if symbol is None:
            return None
        if symbol.kind == SymbolKind.CLASS:
            return ["instance", symbol.id]
        if symbol.kind == SymbolKind.FUNCTION:
            return ["factory", symbol.id]
        return None

    def _stash_binding(self, target: ast.expr, value: ast.expr | None) -> None:
        if value is not None and isinstance(target, ast.Attribute) and self._is_self(target.value):
            if self.scope.method.rsplit(".", 1)[-1] == "__init__":
                binding = self._init_binding(value) or self._instance_binding(value)
            else:
                binding = self._instance_binding(value)
            self._bindings[id(target)] = binding

    def _bounded_hasattr(self, node: ast.Call) -> bool:
        """``hasattr(obj, name)`` with a name a literal bounds: a reference
        to each attribute it may be (resolved on a known object, name-bounded
        on any other), as ``getattr(obj, "x")`` is, and no reflection site.
        False (nothing recorded) when the name is unbounded."""
        if len(node.args) < 2 or node.keywords:
            return False
        names = self.scope.string_candidates(node.args[1])
        if names is None or not all(
            n.isidentifier() and not (n.startswith("__") and n.endswith("__")) for n in names
        ):
            return False  # a dunder is matched by no name: keep the reflection site
        base = _flatten_chain(node.args[0])
        for name in names:
            if base is None:
                self.indexer.out.unresolved.add(
                    UnresolvedReference(
                        self.source, UnresolvedKind.ATTRIBUTE, name, f"hasattr(..., {name!r})"
                    )
                )
            else:
                self._resolve(base + [name])
        return True

    def _reflective_write(
        self, receiver: ast.expr | None, name: ast.expr | None, *, undone: bool = False
    ) -> None:
        """``setattr(receiver, name, ...)`` and its relatives (``undone``: a
        store undone after the test, which writes no external module for
        later code; indexer/patches.py)."""
        if not undone and (module := self._written_module(receiver)) is not None:
            self.indexer.out.external_writes.add((module, self.source))
        names = self.scope.string_candidates(name) if name is not None else None
        owner = (self._self_class(receiver) if receiver is not None else None) or ""
        for attr in names if names is not None else ("*",):
            self.indexer.out.attr_unbound.add((owner, attr.rsplit(".", 1)[-1]))
            if attr == "*":
                self._generic_store(owner, receiver)
            else:
                self.indexer.out.attr_values.add((owner, attr.rsplit(".", 1)[-1], False))

    def _generic_store(self, owner: str, receiver: ast.expr | None) -> None:
        """A store under a name only known at run time (``setattr(r, name,
        v)``, ``r.__dict__``, ``vars(r)``) may bind any attribute of ``r`` to
        anything (``attr_values``: writes._HeldSafety, audit round 6, T23).
        Through a parameter of the function itself, an instance handed to it
        is one the write tracing follows (its kind is ``=*``); a parameter of
        a nested function is reached only by a call of it, which the write
        tracing does not follow for any write; and a module is no instance:
        none of these is recorded on a receiver of unknown type."""
        if not owner and receiver is not None:
            if _whole_param(self, receiver):
                return
            if isinstance(receiver, ast.Name) and self._nested_parameter(receiver.id):
                return
            if self._written_module(receiver) is not None or self._is_module(receiver):
                return
        self.indexer.out.attr_values.add((owner, "*", False))

    def _nested_parameter(self, name: str) -> bool:
        """``name`` is a parameter of a function nested in the symbol (the
        innermost scope binding it is that function's)."""
        scope: Scope | None = self.scope
        while scope is not None:
            if name in scope.literal_bound:
                node = scope.literal_node
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return False
                args = node.args
                named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
                named += [a for a in (args.vararg, args.kwarg) if a is not None]
                return any(a.arg == name for a in named)
            scope = scope.literal_parent
        return False

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

    def _stash_value(self, target: ast.expr, value: ast.expr | None) -> None:
        """Whether an attribute target is bound to a value that cannot
        change in place (``_attribute_write``; audit round 6, T23)."""
        if isinstance(target, ast.Attribute) and value is not None:
            self._attr_ok[id(target)] = self._immutable_typed(value, target)

    def _immutable_typed(self, expr: ast.expr, target: ast.Attribute | None = None) -> bool:
        """Whether ``expr`` is a value of a builtin type that cannot change
        in place, whatever the names it reads hold: a literal, an f-string,
        a call of a builtin giving one (``len(x)``, ``str(x)``), an operator
        on such values, or (``target``: ``self.<attr>``) the attribute being
        bound itself, as in ``self.n = self.n + 1``."""
        if isinstance(expr, (ast.Constant, ast.JoinedStr)):
            return True
        if isinstance(expr, ast.UnaryOp):
            return isinstance(expr.op, ast.Not) or self._immutable_typed(expr.operand, target)
        if isinstance(expr, ast.BinOp):
            return self._immutable_typed(expr.left, target) and self._immutable_typed(
                expr.right, target
            )
        if isinstance(expr, ast.BoolOp):
            return all(self._immutable_typed(v, target) for v in expr.values)
        if isinstance(expr, ast.Compare):
            if all(isinstance(op, (ast.In, ast.NotIn, ast.Is, ast.IsNot)) for op in expr.ops):
                return True
            return self._immutable_typed(expr.left, target) and all(
                self._immutable_typed(c, target) for c in expr.comparators
            )
        if isinstance(expr, ast.IfExp):
            return self._immutable_typed(expr.body, target) and self._immutable_typed(
                expr.orelse, target
            )
        if isinstance(expr, ast.Call):
            func = expr.func
            return (
                isinstance(func, ast.Name)
                and func.id in IMMUTABLE_TYPED_CALLS
                and not self._is_shadowed(func.id)
            )
        if isinstance(expr, ast.Attribute) and target is not None:
            return (
                expr.attr == target.attr
                and self._is_self(expr.value)
                and self._is_self(target.value)
            )
        return False

    def _attr_share(self, target: ast.expr, value: ast.expr) -> None:
        """``self.<attr> = value`` in a method: when ``value`` may be an
        object other code holds (a module variable, a class attribute, a
        parameter's mutable default, the Borg's ``self.__dict__ = shared``),
        a write into that attribute of an instance writes it, and the
        attribute is not the instance's own (``attr_shares``; audit round 7,
        U11). A fresh object (a display, a constructor call, a copy) and a
        plain parameter are the instance's own."""
        scope = self.scope
        cls = scope.self_class
        if (
            cls is None
            or scope.self_is_class
            or not isinstance(target, ast.Attribute)
            or not self._is_self(target.value)
        ):
            return
        facts: set[tuple[str, str, str]] = set()
        for root, copy in self._state_roots(value, frozenset()).items():
            if copy or root == _NAMESPACE or root.startswith(FRESH) or held_root(root):
                continue
            facts.update(self._root_facts(root, bound=False, attr=""))
        if self._mutable_default(value):
            facts.add(("value", self.source, ""))
        for kind, first, second in sorted(facts):
            self.indexer.out.attr_shares.add((cls, target.attr, kind, first, second))

    def _mutable_default(self, value: ast.expr) -> bool:
        """Whether ``value`` names a parameter of this function whose
        default is an object made once, when the function was defined
        (``def __init__(self, items=[])``), and kept by the function."""
        owner = self._binding_scope_node()
        if not isinstance(owner, (*FUNC_NODES, ast.Lambda)):
            return False
        names = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
        for name, default in _defaults(owner.args):
            if name in names and not isinstance(default, (ast.Constant, ast.Name, ast.Attribute)):
                return True
        return False

    def _binding_scope_node(self) -> ast.AST | None:
        """The function this statement is in (``_binding_scope`` of none)."""
        for node in reversed(self._stack):
            if isinstance(node, (*FUNC_NODES, ast.Lambda)):
                return node
        root = self.scope
        while root.literal_parent is not None:
            root = root.literal_parent
        return root.literal_node

    def _type_creation(self, node: ast.Call) -> None:
        """``type(name, bases, ns)``: making a class runs code of its
        in-scope bases and metaclass as a ``class`` statement does
        (``__init_subclass__``: Resolver.class_creation), and that code may
        change each base (a registry of subclasses): a write of each
        (audit round 7, U12)."""
        spec = node.args[1]
        elts = spec.elts if isinstance(spec, (ast.Tuple, ast.List)) else [spec]
        bases = [
            cid
            for elt in elts
            if not isinstance(elt, ast.Starred)
            and (cid := self._class_named(elt)) is not None
            and cid in self.indexer.class_scopes
        ]
        if not bases:
            return
        self.indexer.class_creation(self.source, bases, node.keywords, self.scope)
        for cid in bases:
            for fact in self._root_facts(cid, bound=False, attr=""):
                apply_fact(self.indexer.out, self.source, fact)

    def _library_class_method(self, func: ast.Attribute) -> None:
        """A method called on an in-scope class itself that no class of its
        MRO in scope defines, when the class has a base outside the source
        roots or a decorator or metaclass (``Base.register(Foo)`` on an
        ``abc`` class, ``Case.addClassCleanup(f)`` on a ``TestCase``): the
        library's method may keep state on the class (an ABC's registry,
        the cleanups) that other code reads, so it writes each in-scope
        class of the MRO (audit round 7, U12). Not a special method, nor
        ``mro``, which read the class. Whether a class of the MRO is plain
        is decided after pass 2 (``library_calls``: writes.apply_writes),
        since another module's decorators are no part of this one's
        environment fingerprint."""
        name = func.attr
        if name == "mro" or (name.startswith("__") and name.endswith("__")):
            return
        cls = self._class_named(func.value)
        scopes = self.indexer.class_scopes
        if cls is None or cls not in scopes:
            return
        if isinstance(self.indexer.lookup_in_class(cls, name), Resolved):
            return
        out = self.indexer.out
        for c in self.indexer._mro(cls):
            if c in scopes:
                for fact in self._root_facts(c, bound=False, attr=""):
                    out.library_calls.add((cls, self.source, *fact))

    def visit_Assign(self, node: ast.Assign) -> None:
        observe_assign(self, node.targets)
        if len(node.targets) == 1:
            self._stash_binding(node.targets[0], node.value)
        for target in node.targets:
            self._stash_value(target, node.value)
        undone = self.scope.module.undone()
        for target in node.targets:
            if isinstance(target, ast.Attribute) and not self._is_self(target.value):
                # ``handler.stream = W()``: stored on an object other code
                # holds, which may call any method of it by name.
                self._hand_on(node.value)
                if target.attr == "plugins" and isinstance(node.value, (ast.List, ast.Tuple)):
                    # ``pytester.plugins = ["pkg.plug"]``: loaded into the
                    # sessions it runs.
                    for elt in node.value.elts:
                        if (name := _string(elt)) is not None:
                            whole_module(
                                self.indexer, self.source, name, "loaded as a pytest plugin"
                            )
            if id(target) in undone:
                continue  # put back before the test ends (indexer/patches.py)
            self._process_store(target)
            if isinstance(target, ast.Subscript):
                self._mark_write_only(target.value, SETITEM)
                self._dict_write(target)
            bound = {id(t) for t in _bound_targets(target)}
            for sub in ast.walk(target):
                if isinstance(sub, (ast.Subscript, ast.Attribute)):
                    self._mutation_target(sub, id(sub) in bound)
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    self._mutation_target(sub, True)  # rebinding a ``global`` variable
            self._target_write(target)
            self._held_instance(target, node.value)
            self._attr_share(target, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        observe_augassign(self, node)
        # ``self.n += 1``: what an immutable ``n`` becomes is one again.
        self._stash_value(node.target, node.value)
        self._write_only[id(node.target)] = _READ  # ``mod.X += 1`` reads it
        self._process_store(node.target)
        self._mutation_target(node.target, True)
        if isinstance(node.target, ast.Subscript):
            self._dict_write(node.target)
        if isinstance(node.target, ast.Name):
            # ``c += [x]`` extends in place what ``c`` holds.
            self._rooted_write(node.target)
        else:
            self._target_write(node.target, bind=False)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            observe_assign(self, [node.target])
            self._process_store(node.target)
            self._mutation_target(node.target, True)
            self._target_write(node.target)
            self._stash_binding(node.target, node.value)
            self._stash_value(node.target, node.value)
            self._held_instance(node.target, node.value, node.annotation)
            self._attr_share(node.target, node.value)
        self.visit(node.target)
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        observe_assign(self, node.targets)
        for target in node.targets:
            self._process_store(target)
            for whole in _bound_targets(target):
                self._mutation_target(whole, True)
            self._target_write(target)
            if isinstance(target, ast.Subscript):
                self._dict_write(target)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        parts = _flatten_chain(node.func)
        observe_call(self, node, parts)
        self._process_call(node, parts)
        self._handed_instances(node, parts)
        self._format_fields(node)
        if parts is not None:
            observe_call_arguments(self, node, parts)
            self._runner_call(node, parts)
            self._pytester_call(node, parts)
            self._string_names(node, parts)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITE_ONLY_METHODS
            and (node.func.attr != "pop" or (len(node.args) == 2 and not node.keywords))
            and isinstance(self._parent(node), ast.Expr)
        ):
            # A statement whose value nobody uses: ``X.clear()``.
            self._write_only[id(node.func)] = WRITES
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in CONTAINER_MUTATORS
            and isinstance(node.func.value, (ast.Name, ast.Subscript, ast.Attribute, ast.Call))
        ):
            self._mutation_target(node.func.value)
            self._dict_write(node.func.value)
        if isinstance(node.func, ast.Attribute) and node.func.attr in CONTAINER_MUTATORS:
            self._rooted_write(node.func.value, call=True)
        if (
            self._argument_mutator(parts)
            and node.args
            and not isinstance(node.args[0], ast.Starred)
        ):
            # ``setattr(Config, "mode", v)``, ``heappush(queue, x)``.
            if id(node) not in self.scope.module.undone():
                binds = parts is not None and parts[-1] in (
                    "setattr",
                    "delattr",
                    "__setattr__",
                    "__delattr__",
                )
                names = (
                    self.scope.string_candidates(node.args[1])
                    if binds and len(node.args) > 1
                    else None
                )
                bound_name = names[0] if names is not None and len(names) == 1 else "*"
                self._rooted_write(node.args[0], attr=bound_name if binds else "")
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "__setattr__",
            "__delattr__",
        ):
            # ``object.__setattr__(self, n, v)`` / ``self.__setattr__(n, v)``.
            receiver = node.func.value if self._is_self(node.func.value) else None
            if receiver is None and node.args and self._is_self(node.args[0]):
                receiver = node.args[0]
            self._reflective_write(receiver, None)
        if isinstance(node.func, ast.Attribute):
            self._library_class_method(node.func)
        if self._unpickler_load(node):
            self._unpickle(node, "Unpickler.load")
        if parts is not None:
            name = ".".join(parts)
            builtin = len(parts) == 1 and not self._is_shadowed(name)
            if builtin and parts[0] == "hasattr" and self._bounded_hasattr(node):
                pass  # ``hasattr(obj, "x")`` notices only ``x``, as ``obj.x`` does
            elif (builtin and parts[0] in REFLECTIVE_BUILTINS) or (
                not builtin and self._canonical_names(parts) & REFLECTIVE_CALLS
            ):
                looked = self._looked_up(node.args[0]) if node.args else []
                for module in looked:
                    # ``dir(builtins)``: what an external module holds.
                    self.indexer.out.external_lookups.add(
                        (self.source, module, "reflection", f"{name}()")
                    )
                if not looked:
                    self.indexer.out.reflection.add((self.source, f"{name}()"))
                if (
                    node.args
                    and self._canonical_names(parts) & MEMBER_LISTINGS
                    and (self._is_module(node.args[0]) or self._is_class(node.args[0]))
                ):
                    # ``inspect.getmembers(mod)`` hands out every member's
                    # value, as ``vars(mod)`` does: any attribute is read.
                    self._member_read(node.args[0], None)
            if builtin and parts[0] in DYNAMIC_CALLS:
                code = node.args[0] if node.args else None
                if parts[0] in ("exec", "eval") and code is not None:
                    self._text_code(node, code, f"{name}()")
                else:
                    self._dynamic(f"{name}()")
            elif builtin and parts[0] == "__import__":
                self._import_module(node, "__import__")
            elif builtin and parts[0] == "getattr":
                self._getattr(node)
            else:
                # Each name the callee may be (an import in ``try`` and
                # another in ``except``, a star import): each one's call.
                for canonical in sorted(self._canonical_names(parts)):
                    if canonical in IMPORT_CALLS:
                        self._import_module(node, canonical)
                    elif canonical in NAME_RESOLVERS:
                        self._resolve_name(node, canonical)
                    elif canonical in MODULE_LISTINGS:
                        self._module_listing(node, canonical)
                    elif canonical == "runpy.run_path":
                        self._dynamic("runpy.run_path()")  # runs a file by path: anything
                    elif canonical in UNPICKLER_CALLS:
                        self._unpickle(node, canonical)
                    elif canonical in FILE_LOADERS:
                        self._file_loader(node, canonical, FILE_LOADERS[canonical])
            if builtin and parts[0] == "vars" and node.args:
                if (cls := self._self_class(node.args[0])) is not None:
                    self.indexer.out.attr_unbound.add((cls, "*"))
                self._generic_store(cls or "", node.args[0])
            if builtin and parts[0] == "type" and len(node.args) == 1:
                if (cls := self._self_class(node.args[0])) is not None:
                    self.indexer.escape_class_family(cls)
            if builtin and parts[0] == "type" and len(node.args) == 3:
                self._type_creation(node)
            if parts[-1] in ("setattr", "delattr") or patch_target_form(parts) in (
                OBJECT,
                MULTIPLE,
            ):
                self._setattr_call(node, parts)
            self._string_targets(node, parts)
            if builtin and parts[0] in ("isinstance", "issubclass") and len(node.args) == 2:
                self._mark_type_node(node.args[1])
            elif node.args and self._only(parts, CASTS):
                self._mark_type_node(node.args[0])
            self._record_call_site(node, parts)
        elif (
            isinstance(node.func, ast.Attribute)
            and self._is_zero_arg_super(node.func.value)
            and self.scope.self_class is not None
        ):
            target = self.indexer.lookup_super(self.scope.self_class, node.func.attr)
            self._add_call_site(node, target, receiver_bound=True)
        elif (form := patch_target_form(tail := callee_parts(node.func))) is not None:
            # A patcher on a value: ``pytest.MonkeyPatch().setattr(...)``.
            if form in (SETATTR, OBJECT, MULTIPLE):
                self._setattr_call(node, tail)
            self._string_targets(node, tail)
        self._call_func = node.func
        self.generic_visit(node)

    _call_func: ast.expr | None = None

    def _text_code(
        self, node: ast.Call, code: ast.expr, what: str, *, namespace: bool = True
    ) -> None:
        """Text run as code in this process (``exec``, ``eval``,
        ``timeit``, an interactive interpreter's ``runsource``). Written
        out (``scripts.code_texts``), it imports what it imports and reaches
        the names it reaches through those imports (``code_edges``), and
        is unbounded when it imports or runs by a name it computes; run in
        this module's namespace (``namespace``) it also reaches what the
        namespace names, a dynamic reference bounded by the module's import
        closure. A text a parameter gives is bounded by what the call sites
        pass; read from a file or built at run time otherwise, it can import
        anything."""
        holes: list[ast.expr] = []
        texts = code_texts(self, code, holes)
        own = namespace and self._own_namespace(node)
        module = self.scope.module.name if own else None
        if texts is not None:
            computed = (
                f"{RUNS_TEXT} with {what}, text that imports or runs by a name it computes: "
                "an import of anything"
            )
            for text in texts:
                if HOLE_NAME in text:
                    # One field a parameter fills (``exec(f"def fn():\n    {body}")``):
                    # the text each call site makes, read there.
                    base = [text, module] if module is not None else [text]
                    if not (
                        len(holes) == 1
                        and len(texts) == 1
                        and self._param_dynamic(holes[0], "template", base, computed)
                    ):
                        self._dynamic(computed)
                elif not code_edges(self.indexer, self.source, text, module):
                    self._dynamic(computed)
            return
        if own:
            self._dynamic(what)  # text from elsewhere may name anything the namespace does
        scope_node = self.scope.literal_node or self.scope.module.tree
        if reads_files(code) or (scope_node is not None and reads_files(scope_node)):
            # ``exec(open("plugin.py").read())``: code read at run time.
            self._dynamic(f"{what} of code read at run time: an import of anything")
            return
        detail = f"{RUNS_TEXT} with {what}: an import of anything"
        base = [module] if module is not None else []
        if not self._param_dynamic(unwrap_text(self, code), "code", base, detail):
            self._dynamic(detail)

    def _own_namespace(self, node: ast.Call) -> bool:
        """Whether ``exec``/``eval`` runs its text with this module's
        globals: no ``globals`` argument, ``globals()``, a ``__dict__``, or
        anything but a dict display (or a local bound once to an empty
        one), in which the text can name only what it imports."""
        namespace = node.args[1] if len(node.args) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == "globals":
                namespace = keyword.value
        if namespace is None or (isinstance(namespace, ast.Constant) and namespace.value is None):
            return True
        if isinstance(namespace, ast.Name) and namespace.id in self.scope.locals:
            namespace = self._local_source(namespace.id) or namespace
        if isinstance(namespace, ast.Dict):
            return any(key is None for key in namespace.keys)  # ``{**globals()}``
        if isinstance(namespace, ast.Call) and isinstance(namespace.func, ast.Name):
            return not (namespace.func.id == "dict" and not namespace.args)
        return True

    def _runner_call(self, node: ast.Call, parts: list[str]) -> None:
        """A test runner, or text as code, run in this process: ``pytest.main
        ([...])`` and ``unittest.main(module=...)`` run the tests of what
        they are given (``scripts.test_file``: a file or module whole, else
        unbounded); ``unittest``'s ``loadTestsFromName(s)`` likewise, its
        ``discover`` and ``doctest.testfile``/``DocFileSuite`` are
        unbounded; ``timeit`` and ``code.InteractiveInterpreter`` run
        text."""
        names = self._canonical_names(parts)
        last = parts[-1]
        unbounded = f"{RUNS_PROGRAM}: an import of anything"
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        if "pytest.main" in names:
            args = node.args[0] if node.args else keywords.get("args")
            for plugin in _elements(keywords.get("plugins")):
                if (name := _string(plugin)) is not None:
                    whole_module(self.indexer, self.source, name, "loaded as a pytest plugin")
            if not runner_list_known(self, args, "pytest"):
                self._dynamic(f"{unbounded} (pytest.main)")
            return
        if "unittest.main" in names:
            module = node.args[0] if node.args else keywords.get("module")
            if module is None:
                return  # this module's own tests, which are targets of their own
            if isinstance(module, ast.Constant) and module.value is None:
                self._dynamic(f"{unbounded} (unittest.main of what sys.argv names)")
            elif (name := _string(module)) is not None and not test_file(self, name, "unittest"):
                self._dynamic(f"{unbounded} (unittest.main)")
            return
        if last in (
            "loadTestsFromName",
            "loadTestsFromNames",
            "discover",
        ) and self._unittest_loader(
            node.func.value if isinstance(node.func, ast.Attribute) else None
        ):
            if last == "discover" or not node.args:
                self._dynamic(f"{unbounded} (unittest discovery)")
                return
            values = self.scope.string_candidates(node.args[0])
            if values is None or not all(test_file(self, v, "unittest") for v in values):
                self._dynamic(f"{unbounded} (unittest.{last})")
            return
        if names & {"doctest.testfile", "doctest.DocFileSuite", "doctest.DocFileTest"}:
            self._dynamic(f"{unbounded} (doctest examples read from a file)")
            return
        if names & TIMEIT_CALLS:
            # The setup and the statement run as one program, in a namespace
            # of timeit's own (``globals=`` hands this one's on: a value).
            setup = node.args[1] if len(node.args) > 1 else keywords.get("setup")
            stmt = node.args[0] if node.args else keywords.get("stmt")
            pieces: list[str] = []
            for text in (setup, stmt):
                if text is None or self._is_callable_value(text):
                    continue  # a function handed to timeit: it escapes as a value
                found = code_texts(self, text)
                if found is None or len(found) != 1 or HOLE_NAME in found[0]:
                    self._text_code(node, text, f"{last}()", namespace=False)
                    return
                pieces.append(found[0])
            if pieces and not code_edges(self.indexer, self.source, "\n".join(pieces)):
                self._dynamic(
                    f"{RUNS_TEXT} with {last}(), text that imports or runs by a name it "
                    "computes: an import of anything"
                )
            return
        if last in ("runsource", "push", "runcode") and node.args:
            receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
            if receiver is not None and self._interactive_interpreter(receiver):
                self._text_code(node, node.args[0], f"{last}()", namespace=False)
        if "code.interact" in names:
            self._dynamic(f"{unbounded} (code read from standard input)")

    def _unittest_loader(self, receiver: ast.expr | None) -> bool:
        """``unittest.defaultTestLoader``, ``unittest.TestLoader()`` or a
        local bound once to one."""
        if receiver is None:
            return False
        if isinstance(receiver, ast.Name) and receiver.id in self.scope.locals:
            receiver = self._local_source(receiver.id) or receiver
        expr = receiver.func if isinstance(receiver, ast.Call) else receiver
        chain = _flatten_chain(expr)
        return chain is not None and bool(
            self._canonical_names(chain) & {"unittest.defaultTestLoader", "unittest.TestLoader"}
        )

    def _interactive_interpreter(self, receiver: ast.expr) -> bool:
        if isinstance(receiver, ast.Name) and receiver.id in self.scope.locals:
            receiver = self._local_source(receiver.id) or receiver
        if not isinstance(receiver, ast.Call):
            return False
        chain = _flatten_chain(receiver.func)
        return chain is not None and bool(
            self._canonical_names(chain)
            & {"code.InteractiveInterpreter", "code.InteractiveConsole"}
        )

    def _is_callable_value(self, expr: ast.expr) -> bool:
        """A function, method, class or lambda rather than text."""
        if isinstance(expr, ast.Lambda):
            return True
        chain = _flatten_chain(expr)
        if chain is None:
            return False
        target = self.indexer.resolve_chain(chain, self.scope)
        if isinstance(target, Resolved) and not target.detail:
            symbol = self.indexer.index.symbols.get(target.symbol)
            return symbol is not None and symbol.kind in (
                SymbolKind.FUNCTION,
                SymbolKind.METHOD,
                SymbolKind.CLASS,
            )
        return isinstance(target, External)

    def _pytester_call(self, node: ast.Call, parts: list[str]) -> None:
        """pytest's ``pytester``/``testdir`` fixture runs text as a test
        session of its own, with the project's registered plugins loaded
        (discovery adds those to the test: pytest_static.py). What the text
        it writes imports and names counts here: ``makepyfile``,
        ``makeconftest``, ``getmodulecol``, ``getitems`` and ``runitem``
        texts as code (``code_edges``; a ``pytest_plugins`` in them loads
        those modules whole), ``maketxtfile`` as doctest examples, ``makeini``
        and ``makepyprojecttoml`` for ``-p NAME``, ``runpytest*``,
        ``inline_run``, ``parseconfig*`` and ``inline_genitems`` for ``-p
        NAME`` and ``plugins=``, ``copy_example`` as a file or directory of
        tests run whole, ``runpython_c`` as code. A text not written out is
        unbounded; a non-literal ``runpytest`` argument is not followed."""
        last = parts[-1]
        if last not in PYTESTER_METHODS or len(parts) < 2:
            return
        target = self.indexer.resolve_chain(parts, self.scope)
        if isinstance(target, (Resolved, External)):
            return  # a method of the project's own, or a library's
        unbounded = f"{RUNS_PROGRAM}: an import of anything (pytester.{last})"
        texts = [a for a in node.args if not isinstance(a, ast.Starred)]
        for keyword in node.keywords:
            if keyword.arg is not None:
                if keyword.arg != "plugins":
                    texts.append(keyword.value)
            elif isinstance(keyword.value, ast.Dict):
                texts += [v for v in keyword.value.values if v is not None]  # ``**{name: src}``
            else:
                self._dynamic(unbounded)  # ``**sources``: texts from elsewhere
        if last == "makefile":
            ext = _string(node.args[0]) if node.args else None
            texts = texts[1:]
            if ext is None:
                self._dynamic(unbounded)
                return
            last = {".py": "makepyfile", ".txt": "maketxtfile", ".rst": "maketxtfile"}.get(
                ext, "makeini"
            )
        if last in PYTESTER_CODE or last == "runpython_c":
            for text in texts:
                found = code_texts(self, text)
                if found is None:
                    self._dynamic(unbounded)
                    continue
                for code in found:
                    if last == "maketxtfile":
                        code = _doctest_source(code)
                    if not code_edges(self.indexer, self.source, code):
                        self._dynamic(unbounded)
            return
        if last in PYTESTER_CONFIG:
            for text in texts:
                found = code_texts(self, text)
                if found is None:
                    self._dynamic(unbounded)
                    continue
                for config in found:
                    plugin_options(self.indexer, self.source, config.split())
            return
        if last == "copy_example":
            for text in texts:
                found = self.scope.string_candidates(text)
                if found is None:
                    self._dynamic(unbounded)
                    continue
                for name in found:
                    if name.endswith((".py", ".pyw")):
                        test_file(self, name, "pytest")
                    else:
                        self.indexer.out.test_runs.add((self.source, name.strip("/"), "pytest"))
            return
        if last in PYTESTER_RUNS:
            words: list[str] = []
            for arg in node.args:
                if isinstance(arg, ast.Starred):
                    continue
                found = self.scope.string_candidates(arg)
                if found is not None and len(found) == 1:
                    words.append(found[0])
                elif not self._param_dynamic(
                    arg, "runner", None, f"{unbounded} (a non-literal argument)"
                ):
                    words.append("")
            plugin_options(self.indexer, self.source, words)
            for plugin in _elements(
                next((k.value for k in node.keywords if k.arg == "plugins"), None)
            ):
                if (name := _string(plugin)) is not None:
                    whole_module(self.indexer, self.source, name, "loaded as a pytest plugin")

    def _string_names(self, node: ast.Call, parts: list[str]) -> None:
        """Attribute and method names a library reads off an object from a
        string: ``operator.attrgetter("a.b")`` (each name of each
        argument), ``operator.methodcaller("m", ...)``: a reference to each
        name bounded like ``getattr(x, "a")`` on an unknown receiver; a name
        that is not written out is bounded by what the call sites pass for a
        parameter, else a dynamic reference."""
        names = self._canonical_names(parts)
        if not names & ATTRIBUTE_GETTERS:
            return
        args = node.args[:1] if "operator.methodcaller" in names else node.args
        for arg in args:
            if isinstance(arg, ast.Starred):
                self._dynamic(f"{parts[-1]}(*args): an attribute read by any name")
                continue
            self._named_attributes(arg, f"{parts[-1]}(...)")

    def _named_attributes(self, expr: ast.expr, detail: str) -> None:
        """A string naming attributes (dotted: each one), read off an
        object the analysis does not know."""
        found = self.scope.string_candidates(expr)
        if found is None:
            unbounded = f"{detail}: an attribute read by a name not written out"
            if not self._param_dynamic(expr, "getattr", None, unbounded):
                self._dynamic(unbounded)
            return
        for value in found:
            for name in value.split("."):
                if name.isidentifier():
                    self.indexer.out.unresolved.add(
                        UnresolvedReference(self.source, UnresolvedKind.ATTRIBUTE, name, detail)
                    )

    def _format_fields(self, node: ast.Call) -> None:
        """``"{0.name}".format(obj)``, ``"{x.name}".format(x=obj)``,
        ``format_map``, ``string.Formatter().format(fmt, ...)``: each
        attribute a field of the template reads, bounded by its name. A
        template not written out whose arguments are more than literals is
        bounded by what the call sites pass for a parameter (or what
        ``__init__`` binds an attribute to), else a dynamic reference."""
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("format", "format_map"):
            return
        template: ast.expr | None = func.value
        args = list(node.args)
        if isinstance(func.value, ast.Call) and (chain := _flatten_chain(func.value.func)):
            if self._only(chain, {"string.Formatter"}):
                template = args[0] if args else None
                args = args[1:]
            else:
                return  # ``make().format(...)``: not a string's
        if template is None:
            return
        values = [a for a in args if not isinstance(a, ast.Starred)]
        values += [k.value for k in node.keywords]
        if all(isinstance(v, (ast.Constant, ast.JoinedStr)) for v in values) and len(values) == len(
            args
        ) + len(node.keywords):
            return  # literals only: nothing whose attributes matter
        detail = f"str.{func.attr}(...)"
        # ``(self.error or DEFAULT).format(...)``: either may be the template.
        candidates = (
            list(template.values)
            if isinstance(template, ast.BoolOp)
            else [template.body, template.orelse]
            if isinstance(template, ast.IfExp)
            else [template]
        )
        for candidate in candidates:
            found = self.scope.string_candidates(candidate)
            if found is None:
                unbounded = f"{detail}: an attribute read by a name in a template not written out"
                if not self._param_dynamic(candidate, "format", None, unbounded):
                    if _flatten_chain(candidate) is None:
                        continue  # ``make().format(...)``: not known to be a string
                    self._dynamic(unbounded)
                continue
            for text in found:
                for name in format_attribute_names(text):
                    self.indexer.out.unresolved.add(
                        UnresolvedReference(self.source, UnresolvedKind.ATTRIBUTE, name, detail)
                    )

    def _handed_instances(self, node: ast.Call, parts: list[str] | None) -> None:
        """An instance of a known class (``C()``, the class itself, a local
        bound once to one, a factory's result) handed to a callee the
        analysis does not follow into: a library's, a builtin that reads
        more than dunders (``dict(x)``, ``vars(x)``, keywords such as
        ``print(file=w)``), a method on a receiver of unknown type
        (``items.append(C())``), a class with no in-scope constructor, or
        ``**x``. Whoever holds it may call any method of it by name, so the
        class escapes (``SourceIndex.escaped_classes``); a call site of an
        in-scope function records what it is handed already. Not the class
        handed to ``object.__new__`` alone (``cls.__new__(cls)`` where no
        class of the family defines ``__new__`` and every base is in scope),
        which reads no member by name (audit round 6, T22)."""
        if len(node.args) == 1 and not node.keywords:
            made = self._made_instance(node)
            if made is not None and made[0] != "call" and self._allocates_only(*made):
                return
        for keyword in node.keywords:
            if keyword.arg is None:
                self._hand_on(keyword.value)  # ``f(**obj)`` calls ``obj.keys()``
        values = [a for a in node.args if not isinstance(a, ast.Starred)]
        keywords = [k.value for k in node.keywords if k.arg is not None]
        if parts is not None:
            if len(parts) == 1 and not self._is_shadowed(parts[0]):
                if parts[0] not in ("dict", "vars"):
                    values = []  # a builtin reads dunders of its positional arguments
            elif self._canonical_names(parts) & TYPE_ONLY_CALLS:
                return  # ``cast(C, x)``, ``TypeVar("T", bound=C)``: the class as a type
            else:
                target = self.indexer.resolve_chain(parts, self.scope)
                if isinstance(target, Resolved) and not target.detail:
                    symbol = self.indexer.index.symbols.get(target.symbol)
                    if symbol is not None and symbol.kind in (
                        SymbolKind.FUNCTION,
                        SymbolKind.METHOD,
                    ):
                        return  # a call site: positional_classes and keyword_classes
                    if symbol is not None and symbol.kind == SymbolKind.CLASS:
                        init = self.indexer.lookup_in_class(
                            symbol.id, "__init__", dispatch=target.receiver
                        )
                        if isinstance(init, Resolved) and not init.detail:
                            return
        for value in [*values, *keywords]:
            self._hand_on(value)

    def _allocates_only(self, how: str, cls: str) -> bool:
        """Whether a ``__new__`` call making ``cls`` (``_made_instance``)
        runs ``object.__new__`` alone: called on ``object``, or no class in
        the MRO of ``cls`` or of a subclass of it defines ``__new__`` and
        all their bases are in scope."""
        if how == "object":
            return True
        indexer = self.indexer
        for c in (cls, *indexer._descendants.get(cls, ())):
            for m in indexer._mro(c):
                cscope = indexer.class_scopes[m]
                if not cscope.complete or "__new__" in cscope.members:
                    return False
                if "__new__" in cscope.bindings:
                    return False
        return True

    def _hand_on(self, expr: ast.expr) -> None:
        """``expr`` is given to code that may read any attribute of it by
        name: the class it is an instance of, when it says (``C()``, ``C``,
        a local bound once to one), or the factory whose result it is."""
        cls = self._argument_class(expr)
        if cls is None and isinstance(expr, ast.Name) and expr.id in self.scope.locals:
            source = self._local_source(expr.id)
            cls = self._argument_class(source) if source is not None else None
        if cls is not None:
            self.indexer.out.handed_instances.add(("class", cls))
            return
        factory = self._argument_source(expr)
        if factory is not None:
            self.indexer.out.handed_instances.add(("factory", factory))

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
        form = patch_target_form(parts)
        if form is None or not args:
            return
        first = args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            chain = first.value.split(".")
            if (len(chain) > 1 or form == MULTIPLE) and all(c.isidentifier() for c in chain):
                self._dotted_target(node, form, chain)
            return
        if form == MULTIPLE:
            receiver = _flatten_chain(first)
            if receiver is not None:
                for name in patched_names(node):
                    self._resolve([*receiver, name])
            return
        if len(args) > 1 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
            receiver = _flatten_chain(first)
            if receiver is not None and args[1].value.isidentifier():
                self._resolve([*receiver, args[1].value])

    def _dotted_target(self, node: ast.Call, form: str, chain: list[str]) -> None:
        """A patcher's target named by a dotted string: each name it patches
        (``patch.multiple``: the object and each attribute its keywords
        name; ``patch.dict`` of ``"pkg.mod.__dict__"``: the module), which
        must exist. A third-party one, unless the store is undone after the
        test (then only that test sees it, and it depends on what it stores;
        audit round 3, W26), puts something on whichever prefix is the
        module, and writes process-global state (indexer/process.py)."""
        if form == MULTIPLE:
            names = patched_names(node)
            if any(k.arg is None for k in node.keywords):
                names.append("*")  # ``**names``: any attribute
            written = [[*chain, name] for name in names]
            targets = [chain, *(w for w in written if w[-1] != "*")]
        elif form == DICT and chain[-1] == "__dict__":
            written, targets = [chain], [chain[:-1]]
        else:
            written, targets = [chain], [chain]
        third_party = False
        for target in targets:
            found = self.indexer.resolve_dotted(target)
            if found is None:
                third_party = True
                continue
            self.indexer._record(self.source, found, chain=".".join(target))
            # Patching imports the module the name is in (``mock`` and
            # ``monkeypatch`` resolve a dotted name by importing it).
            for i in range(len(target), 0, -1):
                if (module := ".".join(target[:i])) in self.indexer.scopes:
                    self.indexer._module_import_edge(self.source, module)
                    break
        if not third_party or self._undone(node):
            return
        for attribute in written:
            for i in range(1, len(attribute)):
                self.indexer.out.external_writes.add((".".join(attribute[:i]), self.source))
            what = ".".join(attribute)
            self._process_write(state_object(what) or what)

    def _setattr_call(self, node: ast.Call, parts: list[str]) -> None:
        """``setattr``/``delattr``, ``monkeypatch.setattr``, ``patch.object``
        and ``patch.multiple``: the receiver is the first argument and the
        name the second (``patch.multiple``: each keyword's), or a dotted
        string names both (``monkeypatch.setattr("pkg.mod.name", value)``,
        ``patch.multiple("pkg.mod", name=value)``)."""
        args = list(node.args)
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        names: list[ast.expr | None]
        multiple = patch_target_form(parts) == MULTIPLE
        if multiple:
            names = [ast.Constant(value=n) for n in patched_names(node)]
            if any(k.arg is None for k in node.keywords):
                names.append(None)  # ``**names``: any attribute
        else:
            names = [args[1] if len(args) > 1 else keywords.get("name", keywords.get("attribute"))]
        if len(parts) > 1 and args and isinstance(args[0], ast.Constant):
            # A dotted string: ``"pkg.mod.NAME"``, or ``"pkg.mod"`` and names.
            for name in names if multiple else [args[0]]:
                self._reflective_write(None, name)
            return
        receiver = args[0] if args else keywords.get("target")
        undone = len(parts) > 1 and self._undone(node)
        for name in names:
            self._reflective_write(receiver, name, undone=undone)

    def _undone(self, node: ast.Call) -> bool:
        """Whether what ``node`` stores is undone after the test that made
        it (indexer/patches.py)."""
        return id(node) in self.scope.module.undone()

    def _record_call_site(self, node: ast.Call, parts: list[str]) -> None:
        target = self.indexer.resolve_chain(parts, self.scope)
        if not (isinstance(target, Resolved) and not target.detail):
            return
        symbol = self.indexer.index.symbols.get(target.symbol)
        if symbol is not None and symbol.kind == SymbolKind.CLASS:
            # Constructing a class calls the ``__init__`` its MRO resolves to
            # (any subclass's, for ``cls(...)``), with ``self`` implicit.
            init = self.indexer.lookup_in_class(symbol.id, "__init__", dispatch=target.receiver)
            self._add_call_site(node, init, receiver_bound=True)
            return
        if symbol is None or symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD):
            return
        receiver_bound = False
        if symbol.kind == SymbolKind.METHOD and len(parts) > 1:
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
        if symbol is None or symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD):
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
        return node.symbol if symbol is not None and symbol.kind == SymbolKind.CLASS else None

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
        return (
            node.symbol
            if symbol is not None and symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
            else None
        )

    def _local_source(self, name: str) -> ast.expr | None:
        """What this scope assigns ``name``, when that is the only thing that
        binds it. A second assignment, a loop target, a ``with ... as``, a
        ``del`` or a parameter of the same name all say nothing: the object
        could be either."""
        body = self.scope.literal_node
        if body is None or name in self.scope.params:
            return None
        names, values = self._name_bindings(body)
        bindings = names.get(name, ())
        if len(bindings) != 1:
            return None
        return values.get(id(bindings[0]))

    def _name_bindings(
        self, body: ast.AST
    ) -> tuple[dict[str, list[ast.Name]], dict[int, ast.expr | None]]:
        """Every name ``body`` binds (nested scopes included) -> the nodes
        binding it, and each binding node that is an assignment's whole
        target -> the value assigned. Computed once per body: a function's
        call sites ask for many arguments."""
        found = self._binding_maps.get(id(body))
        if found is None or found[0] is not body:
            names: dict[str, list[ast.Name]] = {}
            values: dict[int, ast.expr | None] = {}
            for inner in ast.walk(body):
                if isinstance(inner, ast.Name) and not isinstance(inner.ctx, ast.Load):
                    names.setdefault(inner.id, []).append(inner)
                elif isinstance(inner, ast.Assign):
                    for target in inner.targets:
                        values.setdefault(id(target), inner.value)
                elif isinstance(inner, ast.AnnAssign):
                    values.setdefault(id(inner.target), inner.value)
            found = (body, names, values)
            self._binding_maps[id(body)] = found
        return found[1], found[2]

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
        alias = self.indexer.module_alias(parts[0], self.scope) if len(parts) > 1 else None
        for module in alias[0] if alias is not None else ():
            target = self.indexer.chain_from(ModuleNode(module), parts)
            self._escape(target, classes=not type_position)

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
            elif looked := self._looked_up(node.args[0]):
                # On an external module: bounded unless in-scope code stores
                # something there (decided over the whole tree).
                for module in looked:
                    self.indexer.out.external_lookups.add(
                        (self.source, module, "dynamic", "getattr(<non-literal>)")
                    )
                return
            else:
                detail = self._getattr_detail(node.args[0], base)
                if not self._param_dynamic(node.args[1], "getattr", base, detail):
                    # The name is not a parameter either: nothing bounds it.
                    self._dynamic(detail)
                return
        for name in names:
            if base is None:
                # Unknown receiver, known attribute name: bounded like ``obj.name``.
                self.indexer.out.unresolved.add(
                    UnresolvedReference(
                        self.source, UnresolvedKind.ATTRIBUTE, name, f"getattr(..., {name!r})"
                    )
                )
            else:
                # The attribute's value is used, so a function or class
                # found this way may be called from here with anything.
                self._resolve(base + [name])
                self._escape(self.indexer.resolve_chain(base + [name], self.scope))

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

    def _mark_write_only(self, expr: ast.expr, mark: str) -> None:
        """``expr`` (a name or a dotted chain) is named only to be changed."""
        if isinstance(expr, ast.Name) or (
            isinstance(expr, ast.Attribute) and _flatten_chain(expr) is not None
        ):
            self._write_only[id(expr)] = mark

    def _write_reference(self, node: Node, rest: Sequence[str], mark: str) -> bool:
        """Record a reference to a module variable that does not read its
        value (``mark``: WRITES, SETITEM or REBINDS); False when ``node`` is
        not one, to be recorded as an ordinary reference. Whether a
        container method or item store reads nothing is settled once the
        whole tree is known (Indexer._write_only_references)."""
        if not (
            isinstance(node, Resolved)
            and not node.detail
            and not node.overrides
            and not node.also
            and not node.alternatives
            and not node.uncertain_attr
            and node.symbol != self.source
        ):
            return False
        symbol = self.indexer.index.symbols.get(node.symbol)
        if symbol is None or symbol.kind != SymbolKind.VARIABLE:
            return False
        if mark == REBINDS:
            if rest or symbol.module == self.scope.module.name:
                return False
            self.indexer.out.rebound.add(symbol.id)
        elif len(rest) != (1 if mark == WRITES else 0):
            return False
        self.indexer.out.edges.add(Edge(self.source, symbol.id, EdgeKind.REFERENCES, mark))
        return True

    def _global_rebinding(self, name: str) -> None:
        """A function binding a module variable under ``global``."""
        node = self.indexer.resolve_chain([name], self.scope)
        if isinstance(node, Resolved) and not node.detail:
            symbol = self.indexer.index.symbols.get(node.symbol)
            if symbol is not None and symbol.kind == SymbolKind.VARIABLE:
                self.indexer.out.rebound.add(symbol.id)

    def _process_write(self, what: str) -> None:
        if not self._restored(what):
            self.indexer.out.process_writes.add((self.source, what))

    def _restored(self, what: str) -> bool:
        """Whether an enclosing ``with`` puts ``what`` back on exit:
        ``warnings.catch_warnings()`` for the warning filters (unless its
        body yields: it stays changed while suspended), and a
        ``mock.patch.dict(os.environ, ...)`` undone after the test
        (indexer/patches.py) for the dict it patches."""
        undone = self.scope.module.undone()
        for node in self._stack:
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            for item in node.items:
                call = item.context_expr
                if not isinstance(call, ast.Call):
                    continue
                parts = _flatten_chain(call.func)
                if parts is None:
                    continue
                if patch_target_form(parts) == DICT:
                    if (
                        call.args
                        and id(call) in undone
                        and self._state_dicts(call.args[0]) == {what}
                    ):
                        return True
                    continue
                names = self._canonical_names(parts)
                if (
                    names
                    and all(name.endswith("catch_warnings") for name in names)
                    and what.startswith("warnings.")
                    and not suspends(node.body)
                ):
                    return True
        return False

    def _state_dicts(self, target: ast.expr) -> set[str | None]:
        """The process-wide object (indexer/process.py) a ``patch.dict``
        target may be: a dotted string, or a name through the scope's
        imports, one for each name it may stand for; None for a name that
        is none (or a target that is not a name)."""
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            return {state_object(target.value)}
        chain = _flatten_chain(target)
        names = self._canonical_names(chain) if chain else frozenset()
        return {state_object(name) for name in names} if names else {None}

    def _patched_state(self, node: ast.Call) -> None:
        """A store into process-global state through a patcher or a
        ``MonkeyPatch`` method that is not undone after the test
        (indexer/patches.py): ``patch.dict(os.environ, ...).start()``,
        ``patch.object(sys, "argv", ...)``, ``MonkeyPatch().setenv(...)``,
        ``setitem(os.environ, ...)``, ``syspath_prepend``, ``chdir``. A
        dotted string target is a patch target like any other
        (``_dotted_target``)."""
        parts = callee_parts(node.func)
        if len(parts) < 2:
            return
        written = self._patched_objects(node, parts)
        if written and not self._undone(node):
            for what in written:
                self._process_write(what)

    def _patched_objects(self, node: ast.Call, parts: list[str]) -> list[str]:
        """The process-wide state a patcher or ``MonkeyPatch`` call (by its
        callee ``parts``) stores into (``_patched_state``)."""
        last = parts[-1]
        if last in ("setenv", "delenv"):
            return ["os.environ"]
        if last == "syspath_prepend":
            return ["sys.path"]
        if last == "chdir":
            return ["os.chdir"]
        form = patch_target_form(parts)
        first = node.args[0] if node.args else None
        if first is None or isinstance(first, ast.Constant):
            return []
        if last in ("setitem", "delitem") or form == DICT:
            return sorted(obj for obj in self._state_dicts(first) if obj is not None)
        if form not in (SETATTR, OBJECT, MULTIPLE):
            return []
        module = self._written_module(first)
        if module is None:
            return []
        if form == MULTIPLE:
            names: tuple[str, ...] | None = tuple(patched_names(node))
            if any(k.arg is None for k in node.keywords):
                names = None
        else:
            names = self.scope.string_candidates(node.args[1]) if len(node.args) > 1 else None
        return [
            state_object(what) or what
            for what in (f"{module}.{n}" for n in (names if names is not None else ("*",)))
        ]

    def _process_store(self, target: ast.expr) -> None:
        """A store, ``del`` or augmented assignment that writes process-global
        state outside the source roots (indexer/process.py)."""
        for sub in ast.walk(target):
            if not isinstance(sub, (ast.Subscript, ast.Attribute)) or isinstance(sub.ctx, ast.Load):
                continue
            expr: ast.expr = sub
            while isinstance(expr, ast.Subscript):
                expr = expr.value
            if isinstance(sub, ast.Attribute):
                module = self._written_module(sub.value)
                if module is not None:
                    self._process_write(f"{module}.{sub.attr}")
                if sub.attr in LOGGER_MUTATORS and self._is_logger(sub.value):
                    self._process_write(f"logging.getLogger().{sub.attr}")
            # A change in place of what a library module holds
            # (``json.registry["k"] = v``, ``json.conf.mode = v``; U13).
            held = sub.value if isinstance(sub, ast.Attribute) else expr
            for name in self._library_attributes(held):
                self._process_write(name)
            if (module := self._module_namespace(held)) is not None:
                self._process_write(f"setattr({module})")  # ``vars(json)["k"] = v``
            chain = _flatten_chain(expr)
            for name in sorted(self._canonical_names(chain) if chain else ()):
                obj = state_object(name)
                if obj is not None:
                    self._process_write(obj)

    def _is_logger(self, expr: ast.expr) -> bool:
        """``logging.getLogger(...)``."""
        if not isinstance(expr, ast.Call):
            return False
        parts = _flatten_chain(expr.func)
        return parts is not None and bool(self._canonical_names(parts) & LOGGER_GETTERS)

    def _process_call(self, node: ast.Call, parts: list[str] | None) -> None:
        """A call that writes process-global state outside the source roots
        (indexer/process.py)."""
        self._patched_state(node)
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in STATE_MUTATORS
            and (module := self._module_namespace(func.value)) is not None
        ):
            # ``json.__dict__.setdefault(k, v)``, ``vars(json).update(...)`` (U13).
            self._process_write(f"setattr({module})")
        if parts is None:
            if (
                isinstance(func, ast.Attribute)
                and func.attr in LOGGER_MUTATORS
                and self._is_logger(func.value)
            ):
                self._process_write(f"logging.getLogger().{func.attr}")
            return
        for canonical in sorted(self._canonical_names(parts) & STATE_CALLS):
            self._process_write(canonical)
        if len(parts) > 1 and parts[-1] in STATE_MUTATORS:
            for name in sorted(self._canonical_names(parts[:-1])):
                obj = state_object(name)
                if obj is not None:
                    self._process_write(obj)
            if isinstance(func, ast.Attribute):
                for name in self._library_attributes(func.value):
                    self._process_write(name)  # ``json.registry.append(x)`` (U13)
        if (
            len(parts) == 1
            and parts[0] in ("setattr", "delattr")
            and not self._is_shadowed(parts[0])
            and node.args
        ):
            module = self._written_module(node.args[0])
            if module is not None:
                self._process_write(f"{parts[0]}({module})")

    def _module_namespace(self, expr: ast.expr) -> str | None:
        """The external module (or ``*``) whose namespace ``expr`` is:
        ``json.__dict__``, ``vars(json)``; a change in place of it stores
        onto the module as ``setattr`` does (audit round 7, U13)."""
        if isinstance(expr, ast.Attribute) and expr.attr == "__dict__":
            return self._written_module(expr.value)
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "vars"
            and not self._is_shadowed("vars")
            and len(expr.args) == 1
        ):
            return self._written_module(expr.args[0])
        return None

    def _library_attributes(self, expr: ast.expr) -> list[str]:
        """The dotted names of what ``expr`` holds when it is an attribute
        of a module outside the source roots, not the module itself nor a
        process-wide object ``STATE_OBJECTS`` names (``json.registry``:
        ``json.registry``): a change in place of it is a write of process
        state (audit round 7, U13)."""
        module = self._external_module(expr)
        chain = _flatten_chain(expr)
        if module is None or chain is None or len(chain) < 2:
            return []
        return sorted(
            name
            for name in self._canonical_names(chain)
            if state_object(name) is None and name != module and not name.endswith(".__dict__")
        )

    def _canonical_names(self, parts: list[str]) -> frozenset[str]:
        """Every dotted name a chain may refer to through the scope's import
        bindings (``import_module`` from ``from importlib import
        import_module`` is ``importlib.import_module``), as
        definitions._canonical_names has them: each binding of a
        function-local import of its head (one in ``try`` and another in
        ``except``), or else each module-level one, and what each star
        import may bind the head to; the chain itself for a head no import
        binds (a builtin, a module-level definition). Empty for a local.

        A caller recognising a name to add what it implies asks whether any
        of them is that name; one recognising a name to leave something out
        (_only) asks whether all of them are."""
        head, rest = parts[0], parts[1:]
        local = self.scope.local_imports.get(head)
        if local is not None:
            bindings = [local, *self.scope.local_alt_imports.get(head, ())]
            return frozenset(".".join([b.dotted, *rest]) for b in bindings)
        if head in self.scope.locals:
            return frozenset()
        module = self.scope.module
        binding = module.imports.get(head)
        bases = (
            [binding.dotted, *(b.dotted for b in module.alt_imports.get(head, ()))]
            if binding is not None
            else [head]
        )
        if module.star_imports:
            if head not in self._star_names:
                self._star_names[head] = list(self.indexer._star_bases(module)(head))
            bases += self._star_names[head]
        return frozenset(".".join([base, *rest]) for base in bases)

    def _only(self, parts: list[str], names: Collection[str]) -> bool:
        """Whether every name the chain may refer to is among ``names``."""
        found = self._canonical_names(parts)
        return bool(found) and all(name in names for name in found)

    def _own_name(self) -> str | None:
        """What ``__name__`` is here: the module's name, unless the program
        rebinds it or the source roots give the module another name than
        Python does (a ``DIR=PREFIX`` root)."""
        if not self._named_as_python_does() or self._is_shadowed("__name__"):
            return None
        return self.scope.module.name

    def _named_as_python_does(self) -> bool:
        """Whether the indexed name of this module is the one Python gives
        it at run time: not under a ``DIR=PREFIX`` root, whose prefix is
        diffcone's own."""
        module = self.scope.module
        roots = [split_root(r)[0] for r in self.indexer.snapshot.source_roots]
        return module_name_for(module.path, roots) == module.name

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
        if not self._named_as_python_does():
            return None
        if isinstance(arg, ast.Name) and not self._is_shadowed(arg.id):
            if arg.id == "__name__":
                return module.name
            if arg.id == "__package__":
                return module.name if module.is_package else module.name.rpartition(".")[0]
        return None

    def _unpickler_load(self, node: ast.Call) -> bool:
        """``pickle.Unpickler(f).load()``."""
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "load"):
            return False
        inner = func.value
        if not isinstance(inner, ast.Call):
            return False
        parts = _flatten_chain(inner.func)
        return parts is not None and bool(self._canonical_names(parts) & UNPICKLER_CLASSES)

    def _unpickle(self, node: ast.Call, name: str) -> None:
        """An unpickler imports whatever module the data names and hands back
        any of its objects by reference (a function, a class, a table): an
        import by a name nothing bounds (audit round 3, W22). Two kinds of
        data say what they name. A round trip (``loads(dumps(x))``) loads
        what the code dumped, objects it holds already, and runs the
        pickling hooks of their classes, which may be any class's: it
        depends on every in-scope ``__reduce__``, ``__setstate__`` and the
        like. Bytes written in the code (``loads(b"...")``, a module-level
        constant) name their globals in their opcodes: an import of each
        module and a reference to each name, with the pickling hooks of a
        class among them."""
        data = node.args[0] if node.args and name != "Unpickler.load" else None
        if data is not None and self._dumped(data):
            for hook in self.indexer.pickling_hooks():
                if hook != self.source:
                    self.indexer.out.edges.add(
                        Edge(self.source, hook, EdgeKind.REFERENCES, "unpickled")
                    )
            return
        found = _pickled_globals(self._literal_bytes(data)) if data is not None else None
        if found is not None:
            for module, qualname in found:
                parts = [*module.split("."), *qualname.split(".")]
                if not all(p.isidentifier() for p in parts):
                    continue
                target = self.indexer.resolve_dotted(parts)
                if target is None:
                    continue  # a third-party name
                self.indexer._module_import_edge(self.source, module)
                self.indexer._record(self.source, target, chain=f"{module}.{qualname}")
                if isinstance(target, Resolved) and target.symbol in self.indexer.class_scopes:
                    for hook in sorted(PICKLING_HOOKS):
                        found_hook = self.indexer.lookup_in_class(target.symbol, hook)
                        if isinstance(found_hook, Resolved) and not found_hook.detail:
                            self.indexer._record(self.source, found_hook, chain=hook)
            return
        detail = f"{name}(): an import of what the data names"
        if isinstance(self._parent(node), ast.Expr):
            detail += DISCARDED_IMPORT
        self._dynamic(detail)

    def _dumped(self, data: ast.expr) -> bool:
        """Whether ``data`` is what a pickler's ``dumps`` returns (directly,
        or a local bound once to it), or a new, empty file this function
        ``dump``s into (``tempfile.TemporaryFile()``, ``io.BytesIO()``)."""
        if isinstance(data, ast.Name) and self._dumped_into(data.id):
            return True
        if isinstance(data, ast.Name) and data.id in self.scope.locals:
            source = self._local_source(data.id)
            if source is None:
                return False
            data = source
        if not isinstance(data, ast.Call):
            return False
        parts = _flatten_chain(data.func)
        return parts is not None and self._only(parts, PICKLER_DUMPS)

    def _dumped_into(self, name: str) -> bool:
        """Whether local ``name`` is bound once, to a new empty file, and this
        function hands it to a pickler's ``dump``."""
        body = self.scope.literal_node
        if body is None or name not in self.scope.locals or name in self.scope.params:
            return False
        bound: list[ast.expr | None] = []
        dumped = False
        for inner in ast.walk(body):
            if isinstance(inner, ast.Name) and inner.id == name:
                if not isinstance(inner.ctx, ast.Load):
                    bound.append(None)
            elif isinstance(inner, ast.withitem):
                target = inner.optional_vars
                if isinstance(target, ast.Name) and target.id == name:
                    bound.append(inner.context_expr)
            elif isinstance(inner, ast.Assign):
                if len(inner.targets) == 1 and isinstance(inner.targets[0], ast.Name):
                    if inner.targets[0].id == name:
                        bound.append(inner.value)
            elif isinstance(inner, ast.Call):
                parts = _flatten_chain(inner.func)
                if parts is not None and self._only(parts, PICKLER_DUMP):
                    file = inner.args[1] if len(inner.args) > 1 else None
                    for k in inner.keywords:
                        if k.arg == "file":
                            file = k.value
                    dumped |= isinstance(file, ast.Name) and file.id == name
        # Each binding is seen twice: as its Name node and as its statement.
        values = [b for b in bound if b is not None]
        if not dumped or len(values) != 1 or len(bound) != 2:
            return False
        value = values[0]
        if not isinstance(value, ast.Call):
            return False
        parts = _flatten_chain(value.func)
        if parts is None:
            return False
        if self._only(parts, {"io.BytesIO"}):
            return not value.args and not value.keywords
        return self._only(parts, FRESH_FILES)

    def _literal_bytes(self, data: ast.expr) -> bytes | None:
        """The bytes ``data`` is when the code writes them out: a literal, or
        a name this module binds once, at module level, to one."""
        if isinstance(data, ast.Constant) and isinstance(data.value, bytes):
            return data.value
        if not isinstance(data, ast.Name) or data.id in self.scope.locals:
            return None
        tree = self.scope.module.tree
        if tree is None:
            return None
        values: list[ast.expr] = []
        stores = 0
        for inner in ast.walk(tree):
            if isinstance(inner, ast.Name) and inner.id == data.id:
                stores += not isinstance(inner.ctx, ast.Load)
        for stmt in tree.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == data.id
            ):
                values.append(stmt.value)
        if stores != 1 or len(values) != 1:
            return None
        value = values[0]
        return (
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, bytes)
            else None
        )

    def _file_loader(self, node: ast.Call, name: str, slot: tuple[int, str]) -> None:
        """A module loaded from a file by path (``spec_from_file_location``,
        ``SourceFileLoader``, ``imp.load_source``): a literal path is the
        module (or the script) it names, matched as a run argument's script
        is (diffcone.indexer.scripts); so is a path joined to this module's
        own directory (``Path(__file__).parent / name``), with a name a
        parameter gives bounded by what the call sites pass; a path with a
        literal prefix is any in-scope file under it; any other path,
        anything (audit round 3, W22)."""
        index, keyword = slot
        location: ast.expr | None = node.args[index] if len(node.args) > index else None
        for k in node.keywords:
            if k.arg == keyword:
                location = k.value
        if location is None:
            return
        detail = f"{name}(<non-literal>): an import of anything"
        for _ in range(4):
            if isinstance(location, ast.Name) and location.id not in self.scope.params:
                source = self._local_source(location.id)
                if source is None:
                    break
                location = source
            else:
                break
        relative = _own_directory_relative(location)
        if relative is not None:
            location = relative
            if self._param_dynamic(location, "script", None, detail):
                return
        paths = self.scope.string_candidates(location)
        if paths is not None and all(p.rstrip().endswith((".py", ".pyw")) for p in paths):
            for path in paths:
                script_value(self.indexer, self.source, path, detail)
            return
        prefix = _string_prefix(location)
        files = self.indexer.snapshot.files
        if prefix and not prefix.startswith(("/", ".")):
            under = {p for p in files if p.startswith(prefix) or f"/{prefix}" in f"/{p}"}
            modules = {
                scope.path: module
                for module, scope in self.indexer.scopes.items()
                if scope.path in under
            }
            if all(p in modules or not p.endswith((".py", ".pyw")) for p in under):
                for path in sorted(modules):
                    self.indexer._module_import_edge(self.source, modules[path])
                # An excluded file under the prefix runs unread (audit round
                # 6, T2): an unknown dependency.
                for path in self.indexer.excluded_paths:
                    if path.startswith(prefix) or f"/{prefix}" in f"/{path}":
                        self.indexer.excluded_import(self.source, "", path)
                return
        self._dynamic(detail)

    def _import_module(self, node: ast.Call, name: str) -> None:
        if not node.args:
            return
        relative = name in ("importlib.import_module", "importlib.util.find_spec")
        package = self._import_package(node) if relative else None
        names = self.scope.string_candidates(node.args[0])
        if names is not None and package is not None:
            resolved = [_resolve_relative_name(n, package) for n in names]
            if all(r is not None for r in resolved):
                names = tuple(r for r in resolved if r is not None)
        if names is None:
            prefix = _string_prefix(node.args[0], self._own_name())
            if prefix is not None and prefix.startswith(".") and package is not None:
                prefix = _resolve_relative_name(prefix, package, prefix=True)
            if prefix is not None and not prefix.startswith("."):
                # ``import_module(f"attr.{name}")``: every in-scope module under
                # the prefix may be imported, and every excluded one (an
                # unknown dependency); nothing outside it can be.
                names = self.indexer.modules_with_prefix(prefix)
                excluded = sorted(m for m in self.indexer.excluded_modules if m.startswith(prefix))
                for module in excluded:
                    self.indexer._module_import_edge(self.source, module)
                if not names and excluded:
                    return
                if not names:
                    self.indexer.out.external.add(ExternalReference(self.source, prefix + "*"))
                    return
        if names is None or any(n.startswith(".") for n in names):
            detail = f"{name}(<non-literal>)"
            if name == FIND_SPEC and not self._loads_a_spec():
                # Looks a module up without running it (flask finds an app's
                # package directory): bounded by the import closure, as a
                # lookup by a name known at run time is. Loaded in the same
                # code (``module_from_spec``, ``exec_module``), it runs
                # anything (audit round 6, T9).
                detail = FINDS_MODULE
            elif isinstance(self._parent(node), ast.Expr):
                # ``__import__(name)`` as a statement: it runs the module's
                # import, but hands the module to nothing.
                detail += DISCARDED_IMPORT
            if names is not None or not self._param_dynamic(node.args[0], "import", None, detail):
                self._dynamic(detail)
                bound = self.scope.table_candidates(node.args[0]) if names is None else None
                if bound is not None and package is not None:
                    resolved = [_resolve_relative_name(n, package) for n in bound]
                    bound = None if None in resolved else tuple(r for r in resolved if r)
                if bound is not None and not any(n.startswith(".") or not n for n in bound):
                    # Bounded but for other code that may change the table.
                    self.indexer.out.table_imports.add((self.source, detail, tuple(sorted(bound))))
            return
        for module in names:
            self.indexer._module_import_edge(self.source, module)
        if isinstance(self._parent(node), ast.Expr):
            return  # the module is imported and dropped
        # The module is held: a reference to it as a value (handed on, its
        # members are reachable through it: SourceIndex.escaped_modules).
        for module in names:
            held = module
            if name in DUNDER_IMPORTS:
                fromlist = node.args[3] if len(node.args) > 3 else None
                for k in node.keywords:
                    if k.arg == "fromlist":
                        fromlist = k.value
                if fromlist is None or (
                    isinstance(fromlist, (ast.List, ast.Tuple)) and not fromlist.elts
                ):
                    held = module.split(".")[0]  # ``__import__("a.b")`` is ``a``
            target = self.indexer.resolve_dotted(held.split("."))
            if isinstance(target, ModuleNode):
                self.indexer._record(self.source, target, chain=held)

    def _loads_a_spec(self) -> bool:
        """Whether the code around a ``find_spec`` runs a module from a spec
        (``module_from_spec``, ``exec_module``, ``load_module``): the
        enclosing functions, or the module's top level."""
        scope: Scope | None = self.scope
        node: ast.AST | None = None
        while scope is not None:
            node = scope.literal_node or node  # the outermost function wins
            scope = scope.literal_parent
        node = node or self.scope.module.tree
        return node is not None and any(
            isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Attribute) and n.func.attr in SPEC_LOADERS)
                or (isinstance(n.func, ast.Name) and n.func.id in SPEC_LOADERS)
            )
            for n in ast.walk(node)
        )

    def _resolve_name(self, node: ast.Call, name: str) -> None:
        """``pkgutil.resolve_name("pkg.mod:attr")``, ``pydoc.locate("pkg.mod.attr")``:
        an import of the module the name starts with (the part before the
        colon; without one, its longest prefix that is a module, as both
        import it part by part) and a reference to what the rest names. A
        name only known at run time imports anything (audit round 6, T9)."""
        if not node.args:
            return
        names = self.scope.string_candidates(node.args[0])
        if names is None:
            self._dynamic(f"{name}(<non-literal>): an import of anything")
            return
        for text in names:
            module, colon, rest = text.partition(":")
            parts = module.split(".")
            attrs = rest.split(".") if rest else []
            if not all(p.isidentifier() for p in parts + attrs):
                self._dynamic(f"{name}({text!r}): an import of anything")
                continue
            if not colon:
                for i in range(len(parts), 0, -1):
                    prefix = ".".join(parts[:i])
                    if self.indexer._module_in_scope(prefix) or prefix in (
                        self.indexer.excluded_modules
                    ):
                        parts, attrs = parts[:i], parts[i:]
                        break
                else:
                    parts, attrs = parts[:1], parts[1:]
            self.indexer._module_import_edge(self.source, ".".join(parts))
            target = self.indexer.resolve_dotted(parts + attrs)
            self.indexer._record(self.source, target, chain=text)

    def _module_listing(self, node: ast.Call, name: str) -> None:
        """``pkgutil.iter_modules(pkg.__path__)``, ``walk_packages(...)``:
        what it finds changes with the modules a package holds, and
        ``walk_packages`` imports the packages among them. Over an in-scope
        package's ``__path__``: an import of each module under it; over any
        other path, ``walk_packages`` imports anything and ``iter_modules``
        is a lookup bounded by the import closure (audit round 6, T9)."""
        path = node.args[0] if node.args else None
        for k in node.keywords:
            if k.arg == "path":
                path = k.value
        chain = _flatten_chain(path) if isinstance(path, ast.Attribute) else None
        if chain is not None and chain[-1] == "__path__":
            package = self.indexer.resolve_chain(chain[:-1], self.scope)
            if isinstance(package, ModuleNode) and package.module in self.indexer.scopes:
                prefix = package.module + "."
                for module in self.indexer.modules_with_prefix(prefix):
                    self.indexer._module_import_edge(self.source, module)
                for module in sorted(self.indexer.excluded_modules):
                    if module.startswith(prefix):
                        self.indexer._module_import_edge(self.source, module)
                return
        if name == "pkgutil.walk_packages":
            self._dynamic(f"{name}(): imports the packages on a path, an import of anything")
        else:
            # Lists without running anything: bounded by the import closure,
            # as a lookup by a name known at run time is.
            self._dynamic(f"{name}(): lists the modules on a path known at run time")


def _bound_targets(target: ast.expr) -> list[ast.expr]:
    """The expressions an assignment or ``del`` target binds or deletes
    itself: the target, or each element it unpacks."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return [t for elt in target.elts for t in _bound_targets(elt)]
    if isinstance(target, ast.Starred):
        return _bound_targets(target.value)
    return [target]


def _merged(*found: dict[str, bool]) -> dict[str, bool]:
    """State roots together: a copy of a root only where every one is."""
    out: dict[str, bool] = {}
    for roots in found:
        for root, copy in roots.items():
            out[root] = out.get(root, True) and copy
    return out


def _part_of(roots: dict[str, bool], *, copy: bool, attr: str = "") -> dict[str, bool]:
    """What is held in, or made of, the objects of ``roots``: an item or an
    attribute of them (no copy), or a new container of them (``copy``). What
    a fresh instance holds is no longer the new object itself (``attr``: the
    attribute it is reached through)."""
    parts = {_instance_part(root, attr): copy for root in roots if root != _NAMESPACE}
    parts.pop(_NAMESPACE, None)
    return parts


def _instance_part(root: str, attr: str = "") -> str:
    """``root``, or for a fresh instance (``FRESH``) what is part of it:
    reached through attribute ``attr``, or anyhow."""
    if root.startswith(FRESH):
        return f"{INSTANCE}{'.' + attr if attr else ''}:{root.rpartition(':')[2]}"
    if root == ANY_CLASS:
        if attr in _FIXED_CLASS_ATTRIBUTES:
            return _NAMESPACE  # nothing a write in place can change (dropped)
        return ANY_CLASS + attr  # what the class holds under ``attr`` (T25)
    held = held_root(root)
    if held is not None:
        return held[1]  # a part of the instance may be what it was handed
    return root


def _class_of(inner: dict[str, bool], made: bool) -> dict[str, bool]:
    """The roots of ``type(x)`` and ``x.__class__`` where ``inner`` are
    those of ``x`` and ``made`` says whether ``x`` is certainly an object a
    call or display just made (``_made_here``). The class of a fresh
    instance is its class (or a subclass, whose state the class's edges
    reach): ``type(new).x = v`` binds a class attribute; a display's or a
    mock's is no class of the project. Of anything else (a parameter, what
    a call returned, a module variable's value, a local that may be any of
    these), part of what it is stays a root, and the class is one the
    analysis does not know (``ANY_CLASS``; T25)."""
    classes = {r.rpartition(":")[2]: False for r in inner if r.startswith(FRESH)}
    rest = {r: c for r, c in inner.items() if not r.startswith(FRESH)}
    found = _merged(classes, _part_of(rest, copy=False))
    if not made:
        found[ANY_CLASS] = False
    return found


def any_class_root(root: str) -> str | None:
    """The attribute an ``ANY_CLASS`` root is reached through ("" for the
    class itself); None for any other root."""
    return root[len(ANY_CLASS) :] if root.startswith(ANY_CLASS) else None


def _held(cls: str, root: str) -> str:
    """``root`` as held by a fresh instance of ``cls`` (HELD)."""
    return f"{HELD}{cls}|{root}"


def held_root(root: str) -> tuple[str, str] | None:
    """(class id, the root it holds) of a ``HELD`` root; None for any other."""
    if root.startswith(HELD):
        cls, _, inner = root[len(HELD) :].partition("|")
        return cls, inner
    return None


def _class_attribute(cls: str, attr: str) -> str:
    """The root of what class ``cls``'s attribute ``attr`` holds (ATTR)."""
    return f"{ATTR}{attr}:{cls}"


def class_attribute_root(root: str) -> tuple[str, str] | None:
    """(class id, attribute) of an ``ATTR`` root; None for any other."""
    if root.startswith(ATTR):
        attr, _, cls = root[len(ATTR) :].partition(":")
        return cls, attr
    return None


def instance_root(root: str) -> tuple[str, str] | None:
    """(how, class id) of a ``FRESH`` root (how it was made), and of an
    ``INSTANCE`` one ("" or ``.`` and the attribute it was reached
    through); None for any other."""
    for prefix in (FRESH, INSTANCE):
        if root.startswith(prefix):
            how, _, cls = root[len(prefix) :].partition(":")
            return how, cls
    return None


# Calls in a class body giving each instance its own object (dataclasses,
# attrs, pydantic).
_FIELD_CALLS = frozenset({"field", "ib", "attrib", "Field", "PrivateAttr"})


def _instance_owned(node: ast.ClassDef, attr: str) -> bool:
    """Whether instances of the class get their own ``attr``: a method
    stores ``self.<attr>``, or the class body declares it only (``attr:
    T``) or as a per-instance field."""
    for stmt in node.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if not any(isinstance(t, ast.Name) and t.id == attr for t in targets):
                continue
            value = stmt.value
            if value is None:
                return True
            callee = _flatten_chain(value.func) if isinstance(value, ast.Call) else None
            if callee is not None and callee[-1] in _FIELD_CALLS:
                return True
        elif isinstance(stmt, FUNC_NODES):
            params = stmt.args.posonlyargs + stmt.args.args
            if not params:
                continue
            me = params[0].arg
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Assign):
                    targets = inner.targets
                elif isinstance(inner, ast.AnnAssign) and inner.value is not None:
                    targets = [inner.target]
                else:
                    continue
                for target in targets:
                    for t in ast.walk(target):
                        if (
                            isinstance(t, ast.Attribute)
                            and t.attr == attr
                            and isinstance(t.value, ast.Name)
                            and t.value.id == me
                            and isinstance(t.ctx, ast.Store)
                        ):
                            return True
    return False


def _find_class(body: list[ast.stmt], path: list[str]) -> ast.ClassDef | None:
    """The ``class`` statement ``path`` names (classes nested in classes),
    in ``body`` or a block of it (``if``, ``try``)."""
    stack = list(body)
    while stack:
        stmt = stack.pop(0)
        if isinstance(stmt, ast.ClassDef):
            if stmt.name == path[0]:
                return stmt if len(path) == 1 else _find_class(stmt.body, path[1:])
        elif not isinstance(stmt, FUNC_NODES):
            for field in ("body", "orelse", "finalbody", "handlers"):
                block = getattr(stmt, field, None)
                if isinstance(block, list):
                    stack.extend(n for n in block if isinstance(n, (ast.stmt, ast.ExceptHandler)))
    return None


def _defaults(args: ast.arguments) -> list[tuple[str, ast.expr]]:
    """Each parameter that has a default, with it."""
    positional = [a.arg for a in args.posonlyargs + args.args]
    found = list(
        zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True)
    )
    found += [(a.arg, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d]
    return found


def _parameter_names(args: ast.arguments) -> set[str]:
    every = args.posonlyargs + args.args + args.kwonlyargs
    return {a.arg for a in every + [a for a in (args.vararg, args.kwarg) if a]}


def _elements(expr: ast.expr | None) -> list[ast.expr]:
    """The elements of a list or tuple display; nothing for anything else."""
    return list(expr.elts) if isinstance(expr, (ast.List, ast.Tuple)) else []


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _doctest_source(text: str) -> str:
    """The code of the ``>>>`` examples in a doctest text, as one
    program."""
    import doctest

    try:
        examples = doctest.DocTestParser().get_examples(text)
    except ValueError:
        return "import __diffcone_unreadable_doctest__"
    return "".join(e.source for e in examples)


# ``operator`` functions that read attributes or methods named by their
# string arguments.
ATTRIBUTE_GETTERS = frozenset({"operator.attrgetter", "operator.methodcaller"})
# Calls that take a class as a type, not as a value handed on.
TYPE_ONLY_CALLS = CASTS | frozenset(
    {"typing.TypeVar", "typing.NewType", "typing.Optional", "typing.Union", "typing.Type"}
)
# Calls that run text as code with a namespace of their own.
TIMEIT_CALLS = frozenset({"timeit.timeit", "timeit.repeat", "timeit.Timer"})
# pytester methods, by what their text is (``_pytester_call``).
PYTESTER_CODE = frozenset(
    {"makepyfile", "makeconftest", "getmodulecol", "getitems", "runitem", "maketxtfile"}
)
PYTESTER_CONFIG = frozenset({"makeini", "makepyprojecttoml"})
PYTESTER_RUNS = frozenset(
    {
        "runpytest",
        "runpytest_inprocess",
        "runpytest_subprocess",
        "inline_run",
        "parseconfig",
        "parseconfigure",
        "inline_genitems",
    }
)
PYTESTER_METHODS = (
    PYTESTER_CODE
    | PYTESTER_CONFIG
    | PYTESTER_RUNS
    | {
        "makefile",
        "copy_example",
        "runpython_c",
    }
)


def _own_directory_relative(path: ast.expr) -> ast.expr | None:
    """``name`` of ``Path(__file__).parent / name`` or
    ``os.path.join(os.path.dirname(__file__), name)`` (``own_directory_join``):
    the part of a path relative to the module's own directory. A function
    is matched by its last name and any ``__file__`` counts: what the part
    names is matched by path suffix under every root, wherever the
    directory is (``script_value``)."""
    return own_directory_join(
        path,
        lambda func, names: (_flatten_chain(func) or [""])[-1] in names,
        lambda node: isinstance(node, ast.Name) and node.id == "__file__",
    )


# Methods pickling runs on an object without naming them.
PICKLING_HOOKS = frozenset(
    {
        "__reduce__",
        "__reduce_ex__",
        "__getstate__",
        "__setstate__",
        "__getnewargs__",
        "__getnewargs_ex__",
        "__new__",
        "__getattr__",
        "__getattribute__",
    }
)
PICKLER_DUMP = frozenset(f"{m}.dump" for m in ("pickle", "_pickle", "cloudpickle", "dill"))
# Calls that make a new, empty file (``io.BytesIO`` with no initial bytes).
FRESH_FILES = frozenset(
    {"tempfile.TemporaryFile", "tempfile.NamedTemporaryFile", "tempfile.SpooledTemporaryFile"}
)


def _pickled_globals(data: bytes | None) -> list[tuple[str, str]] | None:
    """The (module, name) pairs a pickle's opcodes import, or None when the
    data cannot be read or names something its opcodes do not spell out
    (an extension code, a name built on the stack from anything but
    strings)."""
    if data is None:
        return None
    import pickletools

    found: list[tuple[str, str]] = []
    strings: list[str | None] = []
    memo: dict[int, str | None] = {}
    try:
        for op, arg, _ in pickletools.genops(data):
            if op.name in ("GLOBAL", "INST"):
                module, _, name = str(arg).partition(" ")
                found.append((module, name))
            elif op.name == "STACK_GLOBAL":
                if len(strings) < 2 or strings[-1] is None or strings[-2] is None:
                    return None
                found.append((str(strings[-2]), str(strings[-1])))
            elif op.name.startswith("EXT"):
                return None
            elif op.name in ("SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE"):
                strings.append(str(arg))
                continue
            elif op.name in ("PROTO", "FRAME"):
                continue
            elif op.name == "MEMOIZE":
                memo[len(memo)] = strings[-1] if strings else None
                continue
            elif op.name in ("PUT", "BINPUT", "LONG_BINPUT"):
                memo[int(str(arg))] = strings[-1] if strings else None
                continue
            elif op.name in ("GET", "BINGET", "LONG_BINGET"):
                strings.append(memo.get(int(str(arg))))
                continue
            strings.append(None)
    except Exception:  # any malformed data is unbounded
        return None
    return found
