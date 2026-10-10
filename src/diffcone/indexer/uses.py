"""How code uses the objects it can name, for the narrowings that bound a
lookup by what a literal table or an external module holds.

Such a narrowing holds only while nothing can change the table or the
module. This module decides, from the syntax alone (pass 1, cacheable per
module), which uses are reads the analysis recognises; every other use
counts as a possible change:

* a literal dict, list or set stays bounded only while every use of it reads
  it: an item read (``T[k]``, ``T.get(k)``), a non-mutating method, iteration,
  a comparison, a test, an argument of a builtin that only reads
  (``len(T)``, ``sorted(T)``). Bound to another name, passed to a function,
  returned, used as a default, put in a container, an item or attribute
  assigned, or a mutating method (``T.update``) reached: it may change.
* a module object (named through an import, ``sys.modules[...]``,
  ``import_module(...)`` or ``__import__``) may be read from (``m.x``,
  ``getattr(m, n)``, ``m.f()``); bound to a name, passed (``setattr(m, ...)``
  and ``vars(m)`` included), returned, or reached through ``__dict__``,
  ``__setattr__`` or ``__delattr__``, it may be written to. An attribute
  stored on it (``m.T = v``, ``setattr(m, "T", v)``, ``m.__dict__["T"] =
  v``, a dotted string a ``patch``/``monkeypatch`` call names) rebinds that
  attribute.
* a module named at run time by a name nothing bounds is any module (``*``);
  by a name starting with literal text (``f"plugins.{name}"``, ``"cell_" +
  key``, ``f"{__name__}.{name}"``), any module with that prefix
  (``plugins.*``). A function-local name bound only to modules and only
  read is followed instead of counted as an escape (``mod =
  import_module(name)`` then ``getattr(mod, attr)``); returned or passed
  on, it escapes. So is a module-level name bound once to one, and what a
  private function used here only by calls returns (``_ser =
  _load("serializer.py")``): a HELD record hands it on only if other code
  can reach the holder. A parameter of such a function every call here
  passes a literal for is bounded by those literals (a LOADER record undoes
  that if other code may call it; a CALL record is a private function
  called through a module or an import).
* a module made here (``types.ModuleType(name)``, ``FRESH``) is no module
  other code holds, and nothing done to it is recorded, unless it is
  installed in ``sys.modules`` under a name: then it is the modules that
  name may be. A ``module_from_spec`` copy is the module its spec's file is
  (``find_spec("m")``, or a literal file next to this module's own), any
  module otherwise; a loader's ``exec_module`` runs its own code in it.
* anything installed in ``sys.modules`` is what an import of that name
  gets (INSTALL, or SWAP for one test: ``monkeypatch.setitem``,
  ``patch.dict``); a handle installed under a literal name is that module
  for whatever names it (AS).
* a function's ``__globals__``, a frame's ``f_globals``/``f_locals`` are a
  module's namespace (that of the function's module, or any module's); an
  unpickler's result, ``pkgutil.resolve_name``/``pydoc.locate`` by a name
  nothing bounds, and an element of a ``gc`` object list (``OBJECTS``) are
  an object of any module, a table included.
* every handle is recorded as well, however it is used (``HANDLE``), with
  every string naming a module (``"pkg.mod"``, ``"pkg.mod:attr"``; not a
  patch target, a logger's name, a comparison or a docstring), which code
  the analysis cannot see may import and hand back: a value of unknown
  type may be such a module (audit round 3, W20). A patch undone after the
  test (``monkeypatch.setattr``, ``mock.patch``, ``patch.object``) is
  marked beside its STORE (``SCOPED``): it puts nothing on a module for
  later code (W24).
* ``globals()``, ``vars()``/``locals()`` without arguments, and ``exec``/
  ``eval`` of code that is not a literal reach this module's namespace, and
  code ``exec`` runs can reach what the module's globals can name; given a
  namespace of its own (``exec(code, m.__dict__)``), it reaches that one,
  which counts where it stands. A literal code string is read as the code
  it is.

Names resolve in the scope they stand in: an import binds a name in its own
scope, a parameter or local shadows a module-level import, and a builtin is
a name nothing in scope binds.

The records name what they reach by absolute dotted name, as far as the
module's own imports say; the indexer resolves them over every module
between the passes (``Indexer._apply_uses``: also what a handed-on module's
imports reach) and after pass 2 for external modules
(``Indexer._external_lookups``).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field

# Every method of the builtin list, dict and set that changes the container
# in place (and the dunders behind ``+=``, ``|=`` and item assignment). The
# literal tables the analysis bounds are displays of exactly these types, so
# any other attribute of one is a read.
CONTAINER_MUTATORS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "sort",
        "reverse",
        "popitem",
        "update",
        "setdefault",
        "add",
        "discard",
        "intersection_update",
        "difference_update",
        "symmetric_difference_update",
        "__setitem__",
        "__delitem__",
        "__iadd__",
        "__imul__",
        "__ior__",
        "__iand__",
        "__isub__",
        "__ixor__",
        "__init__",
    }
)

# Attributes that write an object (or hand out its namespace, or a module's)
# whatever it is.
_WRITING_ATTRIBUTES = frozenset(
    {"__setattr__", "__delattr__", "__dict__", "__globals__", "f_globals", "f_locals"}
)

# Builtins that only read their positional arguments (or iterate them): a
# table or module passed to one is not changed by it.
SAFE_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "ascii",
        "bool",
        "callable",
        "dict",
        "dir",
        "enumerate",
        "filter",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "print",
        "repr",
        "reversed",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "type",
        "zip",
    }
)

# Calls whose result is a module named by their first argument.
_IMPORT_CALLS = frozenset(
    {"importlib.import_module", "importlib.__import__", "pytest.importorskip"}
)

# Calls whose result is a new, empty module object, no module any code
# imported. (``importlib.util.module_from_spec`` is not one: once its loader
# runs, the module is a copy of whatever module the spec names.)
_FRESH_MODULES = frozenset({"types.ModuleType"})

# Calls whose result is a copy of the module a spec names: its code is that
# module's, so what is written to it is what that module's code then reads.
_MODULE_COPIES = frozenset({"importlib.util.module_from_spec"})
_FIND_SPEC = frozenset({"importlib.util.find_spec"})
_SPEC_FROM_FILE = frozenset({"importlib.util.spec_from_file_location"})

# Calls that return an object by a run-time name, imported and read off its
# module: an unpickler's ``find_class`` (any module's attribute the data
# names, a table included), ``pkgutil.resolve_name`` and ``pydoc.locate``.
_UNPICKLERS = frozenset(
    {
        f"{m}.{f}"
        for m in ("pickle", "_pickle", "cloudpickle", "dill", "joblib")
        for f in ("load", "loads")
    }
)
_UNPICKLER_CLASSES = frozenset({"pickle.Unpickler", "_pickle.Unpickler", "dill.Unpickler"})
UNPICKLER_CALLS, UNPICKLER_CLASSES = _UNPICKLERS, _UNPICKLER_CLASSES
_PICKLER_DUMPS = frozenset(f"{m}.dumps" for m in ("pickle", "_pickle", "cloudpickle", "dill"))
# Calls that load a module from a file by path: the path's (argument
# position, keyword).
FILE_LOADERS = {
    "importlib.util.spec_from_file_location": (1, "location"),
    "importlib.machinery.SourceFileLoader": (1, "path"),
    "importlib.machinery.SourcelessFileLoader": (1, "path"),
    "imp.load_source": (1, "pathname"),
}
_RESOLVERS = frozenset({"pkgutil.resolve_name", "pydoc.locate"})

# ``gc`` functions and attributes that hand out a list of objects nothing
# names (modules, their namespaces and tables among them).
_OBJECT_LISTS = frozenset({"gc.get_objects", "gc.get_referrers", "gc.get_referents"})
_OBJECT_LIST_ATTRIBUTES = frozenset({"gc.garbage"})

# Builtins that only look at a list of objects, never hand an element on.
_LIST_READERS = frozenset(
    {"len", "bool", "id", "hash", "repr", "str", "print", "isinstance", "type", "format", "ascii"}
)

# Attributes that hand out a module's namespace: a function's globals (the
# module it is defined in), a frame's globals, and a frame's locals (which at
# module level are its globals).
GLOBALS_ATTRIBUTES = frozenset({"__globals__", "f_globals", "f_locals"})

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

READ, USE, MUT, DYN = "read", "use", "mut", "dyn"
STORE = "store"
# A call of a private function through a module or an import (``mod._f()``),
# and a private function a bound was taken from because every call of it in
# its module passes a literal (``_call_site_strings``): the bound holds only
# while no other code calls or holds it.
CALL, LOADER = "call", "loader"
# A handle kept where only this module's code reads it: a module-level name
# bound once to it, or what a private function returns to its callers here
# (``sources[0]`` names the holder). It is handed on only if other code can
# reach the holder (``Indexer._apply_uses``).
HELD = "held"
# A handle installed in ``sys.modules`` under a name a literal gives: what
# names that module is the handle's module (``sources[0]`` is ``>`` and it).
AS = "as"
# An object installed in ``sys.modules`` under the name ``target`` (INSTALL:
# for good; SWAP: for a test, ``monkeypatch.setitem``, ``patch.dict``).
INSTALL, SWAP = "install", "swap"
# A module object obtained here other than by an import statement (a handle:
# ``import_module("m")``, ``sys.modules[...]``, a ``module_from_spec`` copy,
# an unpickled object, a ``gc`` list's element), however it is then used: a
# value of unknown type may be that module (``Indexer._attribute_modules``,
# audit round 3, W20).
HANDLE = "handle"
# A STORE that lasts one test (``monkeypatch.setattr``, ``mock.patch``,
# ``patch.object``): beside the STORE record, for what may put objects on a
# module for later code (``SourceIndex.module_writers``, W24).
SCOPED = "scoped"
ANY = "*"
# A module created here (``types.ModuleType(name)``): no module any other
# code holds, so nothing done to it is recorded, unless it is installed in
# ``sys.modules`` (then it is the modules its name there may be).
FRESH = "+"
# A list of objects from anywhere (``gc.get_objects()``): each element is an
# object of any module, a module or a table among them (``ANY``); the list
# itself is only read by ``len`` and by a loop whose variable is followed.
OBJECTS = "[*]"


@dataclass(frozen=True)
class UseRecord:
    """``kind`` of ``target`` seen in this module: ``use`` (the object may be
    changed: handed on, or its namespace reached), ``mut`` (a container
    mutator reached on it, or an item assigned), ``store`` (the attribute
    ``target`` names is rebound), ``dyn`` (an attribute read by a run-time
    name off it is handed on); ``install``/``swap``/``as`` (installed in
    ``sys.modules``), ``held``/``loader``/``call`` (what only this module
    holds, and the bounds that hold while no other code reaches it: see the
    constants); ``handle`` (a module object obtained here) and ``scoped`` (a
    store undone after the test). ``target`` is an absolute dotted name, ``*``
    (any module) or ``*.NAME``, ``PREFIX*`` or ``PREFIX*.NAME`` (any module
    whose name starts with PREFIX), or ``@NAME`` (a name this module may have
    from a star import). ``sources`` are this module's literal names a
    bounded target was computed from: if one of them is not the literal it
    was, the target is any module (``=NAME`` is a HELD record's holder,
    ``>MODULE`` an AS record's module). ``writer`` is the def path the use
    is in."""

    kind: str
    target: str
    sources: tuple[str, ...] = ()
    writer: tuple[str, ...] = ()

    def to_list(self) -> list:
        return [self.kind, self.target, list(self.sources), list(self.writer)]

    @staticmethod
    def from_list(data: list) -> UseRecord:
        kind, target, sources, writer = data
        return UseRecord(str(kind), str(target), tuple(sources), tuple(writer))


@dataclass
class _ScopeNames:
    """What one scope binds: the names an import binds there (to the
    absolute dotted names), the names something else binds (a parameter, an
    assignment, a def), and the names it declares ``global``/``nonlocal``."""

    imports: dict[str, set[str]] = field(default_factory=dict)
    other: set[str] = field(default_factory=set)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)


@dataclass
class Uses:
    """What a scan found: bare names used other than as reads (a container
    bound to one is not the literal it was), records for what other modules
    hold, and whether this module's own namespace may change wholesale."""

    names: set[str] = field(default_factory=set)
    records: set[UseRecord] = field(default_factory=set)
    namespace: bool = False


def _parents(nodes: list[ast.AST]) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in nodes:
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _bound_names(nodes: list[ast.AST]) -> set[str]:
    """Every name the code binds anywhere (a builtin of that name is shadowed
    somewhere, so it is not taken for the builtin)."""
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
    return names


def classify(
    node: ast.AST, parents: dict[int, ast.AST], builtin: Callable[[ast.AST], str | None]
) -> str:
    """How the value of ``node`` is used where it stands: ``read``, ``mut``
    (an item assigned or deleted, a container mutator reached) or ``use``
    (anything else: it may be changed by whoever gets it)."""
    while True:
        p = parents.get(id(node))
        if p is None or isinstance(p, ast.Expr):
            return READ
        if isinstance(p, ast.BoolOp) or (isinstance(p, ast.IfExp) and node is not p.test):
            node = p  # the operand is the expression's value
            continue
        if isinstance(p, ast.Subscript):
            if node is p.value and not isinstance(p.ctx, ast.Load):
                return MUT
            return READ
        if isinstance(p, ast.Attribute):
            if not isinstance(p.ctx, ast.Load):
                return USE
            if p.attr in CONTAINER_MUTATORS:
                return MUT
            if p.attr in ("__setattr__", "__delattr__"):
                return USE
            if p.attr == "__dict__":
                return READ if classify(p, parents, builtin) == READ else USE
            return READ
        if isinstance(p, ast.Call):
            if node is p.func:
                return READ
            if any(a is node for a in p.args) and builtin(p.func) in SAFE_BUILTINS:
                return READ
            return USE
        if isinstance(p, ast.Starred):
            # ``f(*T)`` hands on the elements, ``[*T]`` copies them.
            outer = parents.get(id(p))
            return READ if isinstance(outer, (ast.Call, ast.List, ast.Tuple, ast.Set)) else USE
        if isinstance(p, ast.Dict):
            # ``{**T}`` copies it; a value stored in a display is handed on.
            for key, value in zip(p.keys, p.values, strict=True):
                if value is node:
                    return READ if key is None else USE
            return READ  # a key: hashed
        if isinstance(p, (ast.Compare, ast.UnaryOp, ast.BinOp, ast.FormattedValue, ast.Await)):
            return READ
        if isinstance(p, (ast.For, ast.AsyncFor, ast.comprehension)):
            return READ if node is p.iter or node in getattr(p, "ifs", ()) else USE
        if isinstance(p, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            return READ
        if isinstance(p, ast.withitem):
            return READ if node is p.context_expr else USE
        if isinstance(p, ast.ClassDef) and any(b is node for b in p.bases):
            return READ
        if isinstance(p, ast.Slice):
            return READ
        if isinstance(p, ast.AugAssign) and node is p.target:
            return MUT
        return USE


def _chain(expr: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return parts[::-1]


def _constant(expr: ast.AST | None) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def _patch_like(parts: list[str]) -> bool:
    """A call naming what it patches by a dotted string first argument."""
    last = parts[-1]
    return (
        (last in ("setattr", "delattr") and len(parts) > 1)
        or last == "patch"
        or parts[-2:] in (["patch", "object"], ["patch", "dict"], ["patch", "multiple"])
    )


def _setattr_like(parts: list[str], builtin: str | None) -> bool:
    """A call storing on its first argument the attribute its second names."""
    return (
        builtin in ("setattr", "delattr")
        or (parts[-1] in ("setattr", "delattr") and len(parts) > 1)
        or parts[-2:] == ["patch", "object"]
    )


# Evaluates a string expression in its scope (the innermost function, lambda
# or class around it, or the module), with the names bound between the two
# (comprehension variables, an enclosing function's locals) unbounded: the
# strings it may be (None when unbounded) and the module-level literal names
# that told (literals.make_evaluator).
Evaluate = Callable[
    [ast.expr, ast.AST, frozenset[str]], tuple[tuple[str, ...] | None, tuple[str, ...]]
]


def _no_evaluate(
    expr: ast.expr, scope: ast.AST, shadowed: frozenset[str]
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    return _constant_tuple(expr), ()


def _constant_tuple(expr: ast.expr) -> tuple[str, ...] | None:
    value = _constant(expr)
    return (value,) if value is not None else None


class _Scanner:
    def __init__(
        self,
        root: ast.AST,
        *,
        module: str,
        is_package: bool,
        evaluate: Evaluate,
        records: bool,
        imports: dict[str, set[str]] | None = None,
        bound: set[str] | None = None,
        prefix: tuple[str, ...] = (),
        outer: _ScopeNames | None = None,
    ) -> None:
        self.root = root
        self.module = module
        self.is_package = is_package
        self.evaluate = evaluate
        self.want_records = records
        # Every node, once: the walks below read this list.
        self.nodes: list[ast.AST] = list(ast.walk(root))
        self.parents = _parents(self.nodes)
        self.bound = bound if bound is not None else _bound_names(self.nodes)
        # Every import anywhere (what code ``exec`` runs here may name).
        self.imports = imports if imports is not None else self._import_map(self.nodes)
        # What each scope binds, for resolving a name where it stands: an
        # import in one function does not bind the name in another, and a
        # parameter or local of the same name shadows a module-level import.
        # ``outer`` is the module's top level, for code ``exec`` runs there.
        self.scope_names: dict[int, _ScopeNames] = self._scope_names(root, self.nodes)
        self._chains: dict[int, list[ast.AST]] = {}
        self.outer = outer
        self.stars = any(
            isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
            for n in self.nodes
        )
        self.prefix = prefix
        self.out = Uses()
        self.aliases: dict[tuple[int, str], tuple[set[str], tuple[str, ...]]] = {}
        self.aliased_values: set[int] = set()
        # Private functions whose returned handles are followed to their
        # calls here (``_follow_returns``), and those returns.
        self.returns: dict[str, tuple[set[str], tuple[str, ...]]] = {}
        self.followed_returns: set[int] = set()
        # What only this module holds (HELD records), and the LOADER bounds:
        # if this module's namespace is handed out, both are handed on.
        self.held: list[tuple[set[str], tuple[str, ...], tuple[str, ...]]] = []
        self.loaders: list[tuple[str, ...]] = []

    # ------------------------------------------------------------ names

    def _import_map(self, nodes: list[ast.AST]) -> dict[str, set[str]]:
        """Name -> the absolute dotted names an import binds it to, from every
        import anywhere in the code (a function-local one included)."""
        found: dict[str, set[str]] = {}
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, target in self._import_bindings(node):
                    found.setdefault(name, set()).add(target)
        return found

    def _import_bindings(self, node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
        """(name, absolute dotted name) for each name an import binds."""
        from diffcone.indexer.scopes import resolve_relative_module

        found: list[tuple[str, str]] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found.append((alias.asname, alias.name))
                else:
                    head = alias.name.split(".")[0]
                    found.append((head, head))
            return found
        base = resolve_relative_module(self.module, self.is_package, node.module, node.level)
        for alias in node.names:
            if alias.name != "*":
                target = f"{base}.{alias.name}" if base else alias.name
                found.append((alias.asname or alias.name, target))
        return found

    def _scope_names(self, root: ast.AST, nodes: list[ast.AST]) -> dict[int, _ScopeNames]:
        """What each scope (the root, every function, lambda and class) binds
        in its own body. A nested def's header (decorators, defaults, bases)
        is the enclosing scope's code; a comprehension's variables count as
        bound in the scope around it (a name bound both ways is read both
        ways, which only adds records)."""
        found: dict[int, _ScopeNames] = {}
        scopes = [root, *(n for n in nodes if isinstance(n, _SCOPES) and n is not root)]
        for scope in scopes:
            names = found.setdefault(id(scope), _ScopeNames())
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                names.other |= {a.arg for a in ast.walk(scope.args) if isinstance(a, ast.arg)}
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body: list[ast.AST] = list(scope.body)
            elif isinstance(scope, ast.Lambda):
                body = [scope.body]
            else:
                body = list(ast.iter_child_nodes(scope))
            stack: list[ast.AST] = list(body)
            nodes: list[ast.AST] = []
            while stack:
                n = stack.pop()
                nodes.append(n)
                if isinstance(n, _SCOPES):
                    # Its header runs here; its body is a scope of its own.
                    if not isinstance(n, ast.Lambda):
                        nodes.append(ast.Name(id=n.name, ctx=ast.Store()))
                        stack.extend(n.decorator_list)
                    if isinstance(n, ast.ClassDef):
                        stack.extend(n.bases)
                        stack.extend(k.value for k in n.keywords)
                    else:
                        stack.extend(d for d in n.args.defaults)
                        stack.extend(d for d in n.args.kw_defaults if d is not None)
                    continue
                if isinstance(n, ast.comprehension):
                    # Its variables are its own (a walrus in it binds here).
                    stack.extend([n.iter, *n.ifs])
                    continue
                stack.extend(ast.iter_child_nodes(n))
            for n in nodes:
                if isinstance(n, ast.Global):
                    names.globals.update(n.names)
                elif isinstance(n, ast.Nonlocal):
                    names.nonlocals.update(n.names)
            top = found.setdefault(id(root), _ScopeNames())
            for n in nodes:
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    for name, target in self._import_bindings(n):
                        into = top if name in names.globals else names
                        into.imports.setdefault(name, set()).add(target)
                    continue
                bound: str | None = None
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                    bound = n.id
                elif isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
                    bound = n.name
                elif isinstance(n, ast.MatchMapping):
                    bound = n.rest
                if bound is not None:
                    (top if bound in names.globals else names).other.add(bound)
        return found

    def _scope_chain(self, node: ast.AST) -> list[ast.AST]:
        """The scopes ``node`` resolves names in, innermost first, the root
        last: a class body only for code directly in it, and a def's header
        in the scope around the def."""
        p = self.parents.get(id(node))
        path: list[ast.AST] = [node]
        while p is not None:
            if isinstance(p, _SCOPES) and p is not self.root and not self._in_header(p, path):
                return self._body_chain(p)
            path.append(p)
            p = self.parents.get(id(p))
        return [self.root]

    def _body_chain(self, scope: ast.AST) -> list[ast.AST]:
        """The scope chain of code directly in ``scope``'s body: it, then the
        scopes around its definition, class bodies left out (a class body is
        seen only by code directly in it)."""
        found = self._chains.get(id(scope))
        if found is None:
            around = self._scope_chain(scope)
            found = [
                scope,
                *(s for s in around if s is self.root or not isinstance(s, ast.ClassDef)),
            ]
            self._chains[id(scope)] = found
        return found

    @staticmethod
    def _in_header(scope: ast.AST, path: list[ast.AST]) -> bool:
        """Whether the path up to ``scope`` (``path[-1]`` is its child) runs
        in the scope around it: decorators, defaults, bases and keywords."""
        child = path[-1]
        if child in getattr(scope, "decorator_list", ()):
            return True
        if isinstance(scope, ast.ClassDef):
            return child in scope.bases or any(child is k for k in scope.keywords)
        if isinstance(child, ast.arguments) and len(path) > 1:
            inner = path[-2]
            return inner in child.defaults or any(inner is d for d in child.kw_defaults)
        return False

    def _binding(self, name: str, node: ast.AST) -> _ScopeNames | None:
        """What the scope binding ``name`` where ``node`` stands binds, or
        None when nothing in scope does (a builtin, or a star import's)."""
        chain = self._scope_chain(node)
        i = 0
        while i < len(chain):
            names = self.scope_names.get(id(chain[i]), _ScopeNames())
            if name in names.globals and chain[i] is not self.root:
                i = len(chain) - 1
                continue
            if name in names.nonlocals and chain[i] is not self.root:
                i += 1
                continue
            if name in names.imports or name in names.other:
                return names
            i += 1
        outer = self.outer
        if outer is not None and (name in outer.imports or name in outer.other):
            return outer
        return None

    def _imported(self, name: str, node: ast.AST) -> set[str]:
        """The dotted names an import may bind ``name`` to where ``node``
        stands (empty when no import binds it there)."""
        names = self._binding(name, node)
        return set(names.imports.get(name, ())) if names is not None else set()

    def _unbound(self, name: str, node: ast.AST) -> bool:
        """Whether nothing in scope binds ``name`` where ``node`` stands."""
        return self._binding(name, node) is None

    def _bare(self, name: str, node: ast.AST) -> bool:
        """Whether ``name`` where ``node`` stands may be something no import
        binds: a local, a parameter, a def, a builtin, a star import's."""
        names = self._binding(name, node)
        return names is None or name in names.other

    def _builtin(self, func: ast.AST) -> str | None:
        """The builtin a callee names: a name nothing in scope binds there."""
        if isinstance(func, ast.Name) and self._unbound(func.id, func):
            return func.id
        return None

    def _canonical(self, expr: ast.AST) -> set[str]:
        """The dotted names a callee or value chain may be: through the
        imports in scope where it stands, or a builtin's own name."""
        parts = _chain(expr)
        if parts is None:
            return set()
        head, rest = parts[0], parts[1:]
        imported = self._imported(head, expr)
        if imported:
            return {".".join([t, *rest]) for t in imported}
        if self._unbound(head, expr):
            return {".".join(parts)}
        return set()

    def _scope_of(self, node: ast.AST) -> ast.AST:
        p = self.parents.get(id(node))
        while p is not None and not isinstance(p, _SCOPES):
            p = self.parents.get(id(p))
        return p if p is not None else self.root

    def _evaluate(self, expr: ast.expr) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        from diffcone.indexer.literals import _collect_store_names, _LocalBindings

        scope = self._scope_of(expr)
        shadowed: set[str] = set()
        p = self.parents.get(id(expr))
        while p is not None and p is not scope:
            if isinstance(p, ast.comprehension):
                shadowed |= _collect_store_names(p.target)
            p = self.parents.get(id(p))
        outer = self.parents.get(id(scope)) if scope is not self.root else None
        while outer is not None:
            if isinstance(outer, _SCOPES):
                shadowed |= _LocalBindings().collect(outer)
            elif isinstance(outer, ast.comprehension):
                shadowed |= _collect_store_names(outer.target)
            outer = self.parents.get(id(outer))
        return self.evaluate(expr, scope, frozenset(shadowed))

    def _writer(self, node: ast.AST) -> tuple[str, ...]:
        path: list[str] = []
        p = self.parents.get(id(node))
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                path.append(p.name)
            p = self.parents.get(id(p))
        return self.prefix + tuple(reversed(path))

    # ------------------------------------------------------------ module handles

    def _own_name(self) -> str | None:
        return self.module if "__name__" not in self.bound else None

    def _module_name(
        self, expr: ast.expr | None, package: ast.expr | None = None
    ) -> tuple[set[str], tuple[str, ...]]:
        """The modules a run-time name may be: the literals it evaluates to,
        this module for ``__name__``, every module whose name starts with a
        literal prefix (``f"plugins.{name}"``, ``"cell_" + key``,
        ``f"{__name__}.{name}"``: the pattern ``PREFIX*``), or any module.
        ``package`` is ``import_module``'s, for a relative prefix."""
        if expr is None:
            return {ANY}, ()
        if isinstance(expr, ast.Name) and expr.id == "__name__" and "__name__" not in self.bound:
            return {self.module}, ()
        names, sources = self._evaluate(expr)
        if names is None and isinstance(expr, ast.JoinedStr):
            names, sources = self._joined(expr)
        if names is not None and not any(n.startswith(".") or not n for n in names):
            return set(names), sources
        if names is None and isinstance(expr, ast.Name):
            called = self._call_site_strings(expr)
            if called is not None and not any(n.startswith(".") or not n for n in called):
                return called, ()
        if names is None:
            from diffcone.indexer.scopes import _resolve_relative_name, _string_prefix

            prefix = _string_prefix(expr, self._own_name())
            if prefix is not None and prefix.startswith("."):
                own = self._package_name(package)
                prefix = _resolve_relative_name(prefix, own, prefix=True) if own else None
            if prefix:
                return {prefix + ANY}, ()
        return {ANY}, ()

    def _joined(self, expr: ast.JoinedStr) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        """The strings an f-string may be when each part is bounded
        (``f"{name}.{sub}"`` with both bound to literals), up to a few."""
        texts: list[str] = [""]
        sources: tuple[str, ...] = ()
        for value in expr.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts: tuple[str, ...] | None = (value.value,)
            elif (
                isinstance(value, ast.FormattedValue)
                and value.conversion in (-1, ord("s"))
                and value.format_spec is None
            ):
                parts, more = self._evaluate(value.value)
                sources += more
            else:
                parts = None
            if parts is None or len(texts) * len(parts) > 64:
                return None, ()
            texts = [t + p for t in texts for p in parts]
        return tuple(texts), sources

    def _package_name(self, package: ast.expr | None) -> str | None:
        """``import_module``'s ``package`` when it is this module's
        ``__name__`` or ``__package__``."""
        if isinstance(package, ast.Name) and package.id in ("__name__", "__package__"):
            if package.id in self.bound:
                return None
            if package.id == "__package__" and not self.is_package:
                return self.module.rpartition(".")[0] or None
            return self.module
        return None

    def _handle(self, node: ast.AST) -> tuple[set[str], tuple[str, ...]] | None:
        """The modules an expression yields when it is ``sys.modules[...]``,
        ``sys.modules.get(...)``, ``import_module(...)`` or ``__import__``; the
        module a ``module_from_spec`` copy is; the object an unpickler,
        ``pkgutil.resolve_name`` or ``pydoc.locate`` returns (an attribute of
        the module a run-time name gives: of any module, unless a literal
        names it); a list of objects from anywhere (``gc.get_objects()``:
        OBJECTS)."""
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if "sys.modules" in self._canonical(node.value) and not isinstance(
                node.slice, ast.Slice
            ):
                return self._module_name(node.slice)
            return None
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if self._canonical(node) & _OBJECT_LIST_ATTRIBUTES:
                return {OBJECTS}, ()
            return None
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        first = node.args[0] if node.args else None
        if (
            isinstance(func, ast.Name)
            and func.id in self.returns
            and self._binding(func.id, func) is self.scope_names.get(id(self.root))
        ):
            return self.returns[func.id]
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("get", "pop", "setdefault")
            and "sys.modules" in self._canonical(func.value)
        ):
            return self._module_name(first)
        names = self._canonical(func)
        if names & _IMPORT_CALLS:
            package = node.args[1] if len(node.args) > 1 else None
            for k in node.keywords:
                if k.arg == "package":
                    package = k.value
            literal = _constant(first)
            if literal is not None and literal.startswith(".") and package is not None:
                if isinstance(package, ast.Name) and package.id in ("__name__", "__package__"):
                    from diffcone.indexer.scopes import _resolve_relative_name

                    own = self.module
                    if package.id == "__package__" and not self.is_package:
                        own = own.rpartition(".")[0]
                    resolved = _resolve_relative_name(literal, own)
                    return ({resolved}, ()) if resolved else ({ANY}, ())
                return {ANY}, ()
            return self._module_name(first, package)
        if "__import__" in names:
            found, sources = self._module_name(first)
            fromlist = node.args[3] if len(node.args) > 3 else None
            for k in node.keywords:
                if k.arg == "fromlist":
                    fromlist = k.value
            empty = isinstance(fromlist, (ast.List, ast.Tuple)) and not fromlist.elts
            if fromlist is None or empty:
                found = {n if n == ANY else n.split(".")[0] for n in found}
            return found, sources
        if names & {"inspect.getmodule", "runpy.run_path", "runpy.run_module"}:
            return {ANY}, ()
        if names & _FRESH_MODULES:
            return {FRESH}, ()
        if names & _MODULE_COPIES:
            return self._spec_module(first)
        if names & _UNPICKLERS or (
            isinstance(func, ast.Attribute)
            and func.attr == "load"
            and isinstance(func.value, ast.Call)
            and self._canonical(func.value.func) & _UNPICKLER_CLASSES
        ):
            if (
                isinstance(first, ast.Call)
                and self._canonical(first.func) & _PICKLER_DUMPS
                and names & _UNPICKLERS
            ):
                return None  # a round trip: a copy of what the code holds
            return {ANY}, ()
        if names & _RESOLVERS:
            found, sources = self._module_name(first)
            if any(n.endswith(ANY) for n in found):
                return {ANY}, ()
            return {n.replace(":", ".") for n in found}, sources
        if names & _OBJECT_LISTS:
            return {OBJECTS}, ()
        return None

    def _single_value(self, name: ast.Name) -> ast.expr | None:
        """The value of the one binding of a function-local name (None when
        it has none, several, or is bound otherwise, a parameter included)."""
        scope = self._scope_of(name)
        if scope is self.root:
            return None
        values = [
            n.value
            for n in ast.walk(scope)
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
            and n.value is not None
            and any(
                isinstance(t, ast.Name) and t.id == name.id
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
            )
        ]
        stored = [
            n
            for n in ast.walk(scope)
            if (isinstance(n, ast.Name) and n.id == name.id and not isinstance(n.ctx, ast.Load))
            or (isinstance(n, ast.arg) and n.arg == name.id)
        ]
        return values[0] if len(values) == 1 and len(stored) == 1 else None

    def _spec_module(self, spec: ast.expr | None) -> tuple[set[str], tuple[str, ...]]:
        """The module a spec names, for ``module_from_spec(spec)``: the name
        ``importlib.util.find_spec`` is given, or the module of the file
        ``spec_from_file_location`` is given when a path relative to this
        module's own file tells it (``_file_modules``); any module
        otherwise."""
        if isinstance(spec, ast.Name):
            spec = self._single_value(spec)
        if not isinstance(spec, ast.Call):
            return {ANY}, ()
        names = self._canonical(spec.func)
        if names & _FIND_SPEC and len(spec.args) == 1 and not spec.keywords:
            return self._module_name(spec.args[0])
        if names & _SPEC_FROM_FILE:
            location = spec.args[1] if len(spec.args) > 1 else None
            for k in spec.keywords:
                if k.arg == "location":
                    location = k.value
            found = self._file_modules(location) if location is not None else None
            if found is not None:
                return found, ()
        return {ANY}, ()

    def _file_modules(self, path: ast.expr) -> set[str] | None:
        """The modules a file path may name, when it is this module's own
        directory joined with relative names that stay inside it
        (``Path(__file__).parent / "x.py"``, ``os.path.join(os.path.dirname(
        __file__), name)``); None otherwise. A name may be a parameter of the
        private module-level function the path is built in, when every use
        of that function in this module is a call passing a literal for it
        (``_call_site_strings``)."""
        if isinstance(path, ast.Name):
            value = self._single_value(path)
            return self._file_modules(value) if value is not None else None
        base: ast.expr | None = None
        rest: ast.expr | None = None
        if isinstance(path, ast.BinOp) and isinstance(path.op, ast.Div):
            base, rest = path.left, path.right
        elif (
            isinstance(path, ast.Call)
            and self._canonical(path.func) & {"os.path.join", "posixpath.join"}
            and len(path.args) == 2
            and not path.keywords
        ):
            base, rest = path.args
        if base is None or rest is None or not self._own_directory(base):
            return None
        names = self._strings(rest)
        if names is None:
            return None
        package = self.module if self.is_package else self.module.rpartition(".")[0]
        found: set[str] = set()
        for name in names:
            parts = name.split("/")
            if (
                not package
                or any(not p.isidentifier() for p in parts[:-1])
                or not parts[-1].endswith(".py")
                or not parts[-1][:-3].isidentifier()
            ):
                return None
            stem = parts[-1][:-3]
            found.add(".".join([package, *parts[:-1], *([] if stem == "__init__" else [stem])]))
        return found

    def _own_directory(self, expr: ast.expr) -> bool:
        """Whether ``expr`` is this module's own directory:
        ``Path(__file__).parent`` (``.resolve()``/``.absolute()`` on the
        way), ``os.path.dirname(__file__)`` (of ``abspath``/``realpath``), or
        a local name bound once to one."""
        if isinstance(expr, ast.Name):
            value = self._single_value(expr)
            return value is not None and self._own_directory(value)
        paths = {"pathlib.Path", "pathlib.PurePath"}

        def strip(node: ast.expr) -> ast.expr:
            while True:
                if (
                    isinstance(node, ast.Call)
                    and not node.args
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("resolve", "absolute")
                ):
                    node = node.func.value
                elif (
                    isinstance(node, ast.Call)
                    and len(node.args) == 1
                    and self._canonical(node.func) & {"os.path.abspath", "os.path.realpath", *paths}
                ):
                    node = node.args[0]
                else:
                    return node

        def own_file(node: ast.expr) -> bool:
            node = strip(node)
            return (
                isinstance(node, ast.Name)
                and node.id == "__file__"
                and self._unbound("__file__", node)
            )

        if isinstance(expr, ast.Attribute) and expr.attr == "parent":
            return own_file(expr.value)
        if (
            isinstance(expr, ast.Call)
            and len(expr.args) == 1
            and self._canonical(expr.func) & {"os.path.dirname"}
        ):
            return own_file(expr.args[0])
        return False

    def _strings(self, expr: ast.expr) -> set[str] | None:
        """The strings an expression may be: what the evaluator bounds, or,
        for a parameter of a private module-level function, what every call
        of it in this module passes (``_call_site_strings``)."""
        names, sources = self._evaluate(expr)
        if names is not None and not sources:
            return set(names)
        if isinstance(expr, ast.Name):
            return self._call_site_strings(expr)
        return None

    def _call_site_strings(self, name: ast.Name) -> set[str] | None:
        """The literals passed for the parameter ``name`` by every use of the
        function it belongs to, when that function is private, defined at
        module level by ``def`` alone, and used in this module by calls
        only. A LOADER record names the function: other code calling it
        (or holding it) makes the bound any module (``Indexer._apply_uses``)."""
        scope = self._scope_of(name)
        if (
            not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
            or self._scope_of(scope) is not self.root
            or not _private(scope.name)
            or self._binding(name.id, name) is not self.scope_names.get(id(scope))
            or not self._only_def(scope.name)
        ):
            return None
        args = scope.args
        positional = [a.arg for a in [*args.posonlyargs, *args.args]]
        if name.id not in positional and name.id not in [a.arg for a in args.kwonlyargs]:
            return None
        index = positional.index(name.id) if name.id in positional else None
        defaults: dict[str, ast.expr] = {}
        if args.defaults:
            defaults.update(zip(positional[-len(args.defaults) :], args.defaults, strict=True))
        for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if d is not None:
                defaults[a.arg] = d
        top = self.scope_names.get(id(self.root))
        found: set[str] = set()
        for use in self.nodes:
            if not (isinstance(use, ast.Name) and use.id == scope.name):
                continue
            if self._binding(use.id, use) is not top:
                continue  # another binding of the same spelling
            call = self.parents.get(id(use))
            if not isinstance(call, ast.Call) or call.func is not use:
                return None
            if any(isinstance(a, ast.Starred) for a in call.args) or any(
                k.arg is None for k in call.keywords
            ):
                return None
            value: ast.expr | None = None
            if index is not None and index < len(call.args):
                value = call.args[index]
            for k in call.keywords:
                if k.arg == name.id:
                    value = k.value
            literal = _constant(value if value is not None else defaults.get(name.id))
            if literal is None:
                return None
            found.add(literal)
        if not found:
            return None
        self._record(LOADER, f"{self.module}.{scope.name}", (), self._writer(name))
        self.loaders.append(self._writer(name))
        return found

    def _only_def(self, name: str) -> bool:
        """Whether module-level ``name`` is bound by one ``def`` and nothing
        else anywhere in the module (no ``global`` rebinding it)."""
        defs = [
            n
            for n in self.nodes
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name
            and self._scope_of(n) is self.root
        ]
        others = [
            n
            for n in self.nodes
            if (isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load))
            or (isinstance(n, ast.ClassDef) and n.name == name)
            or (isinstance(n, ast.alias) and (n.asname or n.name.split(".")[0]) == name)
            or (isinstance(n, ast.Global) and name in n.names)
        ]
        return len(defs) == 1 and not others

    def _installs(self, node: ast.AST) -> list[tuple[ast.expr | None, ast.expr | None, str]]:
        """(key, value, kind) for each entry ``node`` installs in
        ``sys.modules`` (``sys.modules[k] = v``, ``setdefault``,
        ``__setitem__``, ``update``, ``|=``: INSTALL, for good;
        ``monkeypatch.setitem(sys.modules, k, v)``, ``mock.patch.dict(
        sys.modules, {k: v})``: SWAP, for a test): a key None is a name
        nothing bounds, a value None one nothing tells."""
        found: list[tuple[ast.expr | None, ast.expr | None]] = []
        kind = INSTALL
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Subscript)
                    and self._is_sys_modules(t.value)
                    and not isinstance(t.slice, ast.Slice)
                ):
                    found.append((t.slice, node.value))
        elif isinstance(node, ast.AugAssign) and self._is_sys_modules(node.target):
            found += self._entries(node.value)
        elif isinstance(node, ast.Call):
            func, args = node.func, node.args
            parts = _chain(func) or [""]
            if isinstance(func, ast.Attribute) and self._is_sys_modules(func.value):
                if func.attr in ("setdefault", "__setitem__") and len(args) > 1:
                    found.append((args[0], args[1]))
                elif func.attr == "update":
                    for arg in args:
                        found += self._entries(arg)
                    found += [
                        (ast.Constant(k.arg) if k.arg else None, k.value if k.arg else None)
                        for k in node.keywords
                    ]
            elif parts[-1] == "setitem" and len(args) > 2 and self._is_sys_modules(args[0]):
                found.append((args[1], args[2]))
                kind = SWAP
            elif (
                parts[-1] == "dict"
                and args
                and (self._is_sys_modules(args[0]) or _constant(args[0]) == "sys.modules")
            ):
                for arg in args[1:2]:
                    found += self._entries(arg)
                found += [
                    (ast.Constant(k.arg) if k.arg else None, k.value if k.arg else None)
                    for k in node.keywords
                    if k.arg != "clear"
                ]
                kind = SWAP
        return [(key, value, kind) for key, value in found]

    @staticmethod
    def _entries(mapping: ast.expr) -> list[tuple[ast.expr | None, ast.expr | None]]:
        if isinstance(mapping, ast.Dict):
            return [
                (k, v) if k is not None else (None, None)
                for k, v in zip(mapping.keys, mapping.values, strict=True)
            ]
        return [(None, None)]

    def _install(self, node: ast.AST) -> None:
        """Whatever is installed in ``sys.modules`` under a name is what a
        later import of that name gets (None, which blocks the import, is
        nothing): an INSTALL or SWAP record for each module the name may be
        (``Indexer._apply_uses`` says what that does)."""
        writer: tuple[str, ...] | None = None
        for key, value, kind in self._installs(node):
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            found, sources = self._module_name(key) if key is not None else ({ANY}, ())
            writer = writer if writer is not None else self._writer(node)
            for target in sorted(found):
                if target != FRESH:
                    self._record(kind, target, sources, writer)

    def _install_keys(self, node: ast.AST) -> list[ast.expr] | None:
        """The names ``node``'s value is installed under in ``sys.modules``
        (``sys.modules[k] = node``, ``sys.modules.setdefault(k, node)``,
        ``monkeypatch.setitem(sys.modules, k, node)``,
        ``sys.modules.update({k: node})``, ``mock.patch.dict(sys.modules,
        {k: node})``), or None when it is not installed there."""
        p = self.parents.get(id(node))
        keys: list[ast.expr] = []
        if isinstance(p, ast.Assign) and p.value is node:
            keys = [
                t.slice
                for t in p.targets
                if isinstance(t, ast.Subscript) and self._is_sys_modules(t.value)
            ]
        elif isinstance(p, ast.Call) and any(a is node for a in p.args):
            func = p.func
            at = next(i for i, a in enumerate(p.args) if a is node)
            if (
                at == 1
                and isinstance(func, ast.Attribute)
                and func.attr in ("setdefault", "__setitem__")
                and self._is_sys_modules(func.value)
            ):
                keys = [p.args[0]]
            elif (
                at == 2
                and (_chain(func) or [""])[-1] == "setitem"
                and self._is_sys_modules(p.args[0])
            ):
                keys = [p.args[1]]
        elif isinstance(p, ast.Dict):
            q = self.parents.get(id(p))
            installs = isinstance(q, ast.Call) and (
                (
                    isinstance(q.func, ast.Attribute)
                    and q.func.attr == "update"
                    and self._is_sys_modules(q.func.value)
                    and p in q.args
                )
                or (
                    (_chain(q.func) or [""])[-1] == "dict"
                    and len(q.args) > 1
                    and self._is_sys_modules(q.args[0])
                    and q.args[1] is p
                )
            )
            if installs:
                keys = [k for k, v in zip(p.keys, p.values, strict=True) if v is node and k]
        return keys or None

    def _installed_as(self, node: ast.AST, targets: set[str], sources: tuple[str, ...]) -> bool:
        """A handle installed in ``sys.modules`` under names literals give
        (``sys.modules["_nb_ser"] = copy``): whoever imports such a name gets
        the handle's module, which an AS record says, so the install hands
        nothing on itself. Under a name nothing bounds, any import may get
        it: False, and it counts as handed on where it stands."""
        keys = self._install_keys(node)
        if keys is None or any(t == FRESH for t in targets):
            return False
        names: set[str] = set()
        more: tuple[str, ...] = ()
        for key in keys:
            found, extra = self._module_name(key)
            if any(n.endswith(ANY) for n in found):
                return False
            names |= found
            more += extra
        writer = self._writer(node)
        for name in sorted(names):
            for target in sorted(targets):
                self._record(AS, name, (">" + target, *sources, *more), writer)
        return True

    def _is_sys_modules(self, expr: ast.expr) -> bool:
        return "sys.modules" in self._canonical(expr)

    # ------------------------------------------------------------ the walk

    def scan(self) -> Uses:
        self._find_aliases()
        self._follow_returns()
        self._module_aliases()
        for node in self.nodes:
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    self._name(node)
                continue
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                # ``T += [...]`` extends a list (or set, or dict) in place.
                self._emit_root(node.target, MUT, ())
                continue
            if isinstance(node, ast.Call):
                self._call(node)
            if not self.want_records:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self._named_module(node)
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.Call)):
                self._install(node)
            if (
                isinstance(node, ast.Attribute)
                and node.attr in GLOBALS_ATTRIBUTES
                and isinstance(node.ctx, ast.Load)
            ):
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if not isinstance(root, ast.Name) and self._handle(root) is None:
                    # Off a call's or an item's value (``sys._getframe(1)
                    # .f_globals``): nothing follows that object up to here.
                    self._globals(node, node.value, None, (), (), node.attr)
            if (handle := self._handle(node)) is not None:
                targets, sources = handle
                for target in sorted(targets):
                    if target != FRESH:
                        found = ANY if target == OBJECTS else target
                        self._record(HANDLE, found, sources, self._writer(node))
                if OBJECTS in targets:
                    self._objects(node)
                else:
                    self._climb(node, targets, sources, ())
        if self.out.namespace and self.want_records:
            # This module's namespace is handed out: what it holds is too, and
            # its functions may be called with anything.
            for targets, sources, writer in self.held:
                for target in sorted(targets):
                    if target != FRESH:
                        self._record(USE, target, sources, writer)
            for writer in self.loaders:
                self._record(USE, ANY, (), writer)
        return self.out

    def _named_module(self, node: ast.Constant) -> None:
        """A string naming a module (``"pkg.mod"``, ``"pkg.mod:attr"``): code
        the analysis cannot see may import the module by it and hand it back
        (``import_string``, a framework's settings, an entry point), so it is
        a handle (``HANDLE``; the indexer keeps the names of in-scope modules
        only). Not one a comparison tests, a docstring, a patch target, or a
        logger's name."""
        text = node.value
        if not isinstance(text, str) or "." not in text and ":" not in text:
            return
        name = text.split(":", 1)[0]
        parts = name.split(".")
        if len(parts) < 2 and ":" not in text or not all(p.isidentifier() for p in parts):
            return
        p = self.parents.get(id(node))
        if p is None or isinstance(p, (ast.Expr, ast.Compare)):
            return
        if isinstance(p, ast.Call) and p.args and p.args[0] is node:
            callee = _chain(p.func) or []
            if callee and (_patch_like(callee) or callee[-1] in ("getLogger", "get_logger")):
                return
        self._record(HANDLE, name, (), self._writer(node))

    def _objects(self, node: ast.AST) -> None:
        """A list of objects from anywhere (``gc.get_objects()``): ``len()``
        or a test reads it, a loop whose variable is followed hands each
        element to that name, an item read is an object from anywhere;
        anything else hands every element on."""
        p = self.parents.get(id(node))
        if p is None or isinstance(p, (ast.Expr, ast.Compare)):
            return
        if isinstance(p, (ast.If, ast.While, ast.Assert, ast.IfExp)) and node is p.test:
            return
        if (
            isinstance(p, ast.Call)
            and any(a is node for a in p.args)
            and self._builtin(p.func) in _LIST_READERS
        ):
            return
        if (
            isinstance(p, (ast.For, ast.AsyncFor))
            and p.iter is node
            and id(node) in self.aliased_values
        ):
            return
        if (
            isinstance(p, ast.Subscript)
            and p.value is node
            and isinstance(p.ctx, ast.Load)
            and not isinstance(p.slice, ast.Slice)
        ):
            self._climb(p, {ANY}, (), ())
            return
        self._record(USE, ANY, (), self._writer(node))

    def _alias(self, node: ast.Name) -> tuple[set[str], tuple[str, ...]] | None:
        """What a followed name holds where ``node`` stands: a local of its
        function, or a module-level name (``_module_aliases``)."""
        alias = self.aliases.get((id(self._scope_of(node)), node.id))
        if alias is None and (id(self.root), node.id) in self.aliases:
            if self._binding(node.id, node) is self.scope_names.get(id(self.root)):
                alias = self.aliases[(id(self.root), node.id)]
        return alias

    def _name(self, node: ast.Name) -> None:
        alias = self._alias(node)
        if alias is not None:
            targets, sources = alias
            self._climb(node, targets, sources, ())
            return
        names = self._binding(node.id, node)
        imported = names.imports.get(node.id) if names is not None else None
        if imported and self.want_records:
            self._climb(node, set(imported), (), ())
        if names is None or node.id in names.other:
            # Not (only) an import here: this module's own name, a local, a
            # builtin (a name bound both ways is followed both ways).
            self._climb(node, None, (), ())

    def _call(self, node: ast.Call) -> None:
        builtin = self._builtin(node.func)
        if builtin in ("globals", "vars", "locals") and not node.args:
            # ``vars()``/``locals()`` in a function or class body is a copy of
            # its own locals; at module level, and ``globals()`` anywhere, it
            # is the module's namespace.
            if builtin == "globals" or self._scope_of(node) is self.root:
                self._namespace(node, None, (), ())
            return
        if builtin in ("exec", "eval"):
            self._exec(node, builtin)
            return
        if not self.want_records:
            return
        parts = _chain(node.func)
        if parts and _patch_like(parts) and node.args:
            target = _constant(node.args[0])
            if target is not None and all(p.isidentifier() for p in target.split(".")):
                writer = self._writer(node)
                if parts[-2:] == ["patch", "multiple"]:
                    for k in node.keywords:
                        if k.arg is not None:
                            self._record(STORE, f"{target}.{k.arg}", (), writer)
                            self._record(SCOPED, f"{target}.{k.arg}", (), writer)
                else:
                    self._record(STORE, target, (), writer)
                    self._record(SCOPED, target, (), writer)
        canonical = self._canonical(node.func)
        if canonical & {"runpy.run_path"}:
            self._record(USE, ANY, (), self._writer(node))

    def _visible(self, node: ast.AST) -> _ScopeNames:
        """Every binding of the scopes ``node`` resolves names in, merged (the
        names code ``exec`` runs there may see, locals or globals)."""
        merged = _ScopeNames()
        for scope in self._scope_chain(node):
            names = self.scope_names.get(id(scope), _ScopeNames())
            for name, targets in names.imports.items():
                merged.imports.setdefault(name, set()).update(targets)
            merged.other |= names.other
        if self.outer is not None:
            for name, targets in self.outer.imports.items():
                merged.imports.setdefault(name, set()).update(targets)
            merged.other |= self.outer.other
        return merged

    def _exec(self, node: ast.Call, builtin: str) -> None:
        code = _constant(node.args[0]) if node.args else None
        if code is not None:
            try:
                tree = ast.parse(code, mode="exec" if builtin == "exec" else "eval")
            except SyntaxError:
                tree = None
            if tree is not None:
                inner = _Scanner(
                    tree,
                    module=self.module,
                    is_package=self.is_package,
                    evaluate=_no_evaluate,
                    records=self.want_records,
                    imports={**self.imports, **self._import_map(list(ast.walk(tree)))},
                    bound=self.bound | _bound_names(list(ast.walk(tree))),
                    prefix=self._writer(node),
                    outer=self._visible(node),
                ).scan()
                self.out.names |= inner.names
                self.out.records |= inner.records
                self.out.namespace |= inner.namespace
                return
        # Code built at run time: it runs in this module's namespace, and can
        # reach whatever its globals name; read from a file, anything. Given
        # a namespace of its own (``exec(code, m.__dict__)``, ``exec(code,
        # {})``), it runs there instead: that argument is handed on where it
        # stands (a module's ``__dict__`` is that module written to,
        # ``globals()`` this one's namespace), and this module's namespace is
        # not reached.
        from diffcone.indexer.references import _reads_files

        reads_files = bool(node.args) and _reads_files(node.args[0])
        namespace = node.args[1] if len(node.args) > 1 else None
        for k in node.keywords:
            if k.arg == "globals":
                namespace = k.value
        own = namespace is None or (isinstance(namespace, ast.Constant) and namespace.value is None)
        if own:
            self.out.namespace = True
        if not self.want_records:
            return
        writer = self._writer(node)
        if reads_files:
            self._record(USE, ANY, (), writer)
            return
        if not own:
            return
        for targets in self.imports.values():
            for target in targets:
                self._record(USE, target, (), writer)

    def _find_aliases(self) -> None:
        """Function-local names bound once to a module and used nowhere but
        in their own function: their uses are the module's."""
        if not self.want_records:
            return
        for scope in self.nodes:
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stores: dict[str, list[ast.AST]] = {}
            declared: set[str] = set()
            params = {a.arg for a in ast.walk(scope.args) if isinstance(a, ast.arg)}
            nested_loads: set[str] = set()
            stack = list(ast.iter_child_nodes(scope))
            while stack:
                n = stack.pop()
                if isinstance(n, _SCOPES):
                    nested_loads |= {m.id for m in ast.walk(n) if isinstance(m, ast.Name)}
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        stores.setdefault(n.name, []).append(n)
                    continue
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                    stores.setdefault(n.id, []).append(n)
                elif isinstance(n, (ast.Global, ast.Nonlocal)):
                    declared.update(n.names)
                elif isinstance(n, ast.alias):
                    stores.setdefault(n.asname or n.name.split(".")[0], []).append(n)
                elif isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name:
                    stores.setdefault(n.name, []).append(n)
                elif isinstance(n, ast.MatchMapping) and n.rest:
                    stores.setdefault(n.rest, []).append(n)
                stack.extend(ast.iter_child_nodes(n))
            for name, nodes in stores.items():
                if name in declared or name in params or name in nested_loads:
                    continue
                # Every binding of the name assigns it a module (``mod =
                # sys.modules.get(n)`` here, ``mod = sys.modules[n]`` there).
                targets: set[str] = set()
                sources: tuple[str, ...] = ()
                values: list[int] = []
                for target in nodes:
                    assign = self.parents.get(id(target))
                    if (
                        isinstance(assign, (ast.For, ast.AsyncFor))
                        and assign.target is target
                        and (found := self._handle(assign.iter)) is not None
                        and OBJECTS in found[0]
                    ):
                        # ``for obj in gc.get_objects()``: each an object from
                        # anywhere, followed through the name.
                        targets.add(ANY)
                        values.append(id(assign.iter))
                        continue
                    if not (
                        isinstance(assign, ast.Assign)
                        and len(assign.targets) == 1
                        and assign.targets[0] is target
                    ):
                        break
                    root = self._root_of(assign.value)
                    if root is None:
                        break
                    targets |= root[0]
                    sources += root[1]
                    values.append(id(assign.value))
                else:
                    if FRESH in targets:
                        # A module made here is the modules its name in
                        # ``sys.modules`` may be, wherever it is installed.
                        for load in ast.walk(scope):
                            if (
                                isinstance(load, ast.Name)
                                and load.id == name
                                and isinstance(load.ctx, ast.Load)
                                and (keys := self._install_keys(load)) is not None
                            ):
                                for key in keys:
                                    found, more = self._module_name(key)
                                    targets |= found
                                    sources += more
                    self.aliases[(id(scope), name)] = (targets, sources)
                    self.aliased_values.update(values)

    def _only_called(self, name: str) -> bool:
        """Whether every use of module-level ``name`` in this module is a
        direct call of it."""
        top = self.scope_names.get(id(self.root))
        for use in self.nodes:
            if isinstance(use, ast.Name) and use.id == name:
                if self._binding(name, use) is not top:
                    continue
                call = self.parents.get(id(use))
                if not isinstance(call, ast.Call) or call.func is not use:
                    return False
        return True

    def _follow_returns(self) -> None:
        """A private function defined at module level by ``def`` alone, used
        here only by calls, that returns a handle: its calls are that handle
        (``mod = _load("x.py")``), and its returns hand nothing on, unless
        other code can call it (a HELD record)."""
        if not self.want_records:
            return
        for fn in self.nodes:
            if (
                not isinstance(fn, ast.FunctionDef)
                or not _private(fn.name)
                or self._scope_of(fn) is not self.root
            ):
                continue
            inner = _own_nodes(fn)
            if not any(isinstance(n, ast.Return) and n.value is not None for n in inner):
                continue
            if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in inner):
                continue
            targets: set[str] = set()
            sources: tuple[str, ...] = ()
            returns: list[int] = []
            for ret in inner:
                if isinstance(ret, ast.Return) and ret.value is not None:
                    root = self._root_of(ret.value)
                    if root is not None:
                        targets |= root[0]
                        sources += root[1]
                        returns.append(id(ret))
            if targets and self._only_def(fn.name) and self._only_called(fn.name):
                self.returns[fn.name] = (targets, sources)
                self.followed_returns.update(returns)
                self._hold(targets, sources, fn.name, (fn.name,))

    def _module_aliases(self) -> None:
        """A module-level name bound once, to a handle, and bound nowhere
        else: its uses here are the handle's, and binding it hands nothing
        on, unless other code can reach the name (a HELD record)."""
        if not self.want_records or not isinstance(self.root, ast.Module):
            return
        top = self.scope_names.get(id(self.root))
        declared = {name for n in self.nodes if isinstance(n, ast.Global) for name in n.names}
        stores: dict[str, list[ast.AST]] = {}
        for n in self.nodes:
            if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                if self._binding(n.id, n) is top:
                    stores.setdefault(n.id, []).append(n)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stores.setdefault(n.name, []).append(n)
            elif isinstance(n, ast.alias):
                stores.setdefault(n.asname or n.name.split(".")[0], []).append(n)
        for name, nodes in sorted(stores.items()):
            if len(nodes) != 1 or name in declared or top is None or name not in top.other:
                continue
            assign = self.parents.get(id(nodes[0]))
            if not (
                isinstance(assign, ast.Assign)
                and len(assign.targets) == 1
                and assign.targets[0] is nodes[0]
                and self._scope_of(assign) is self.root
            ):
                continue
            root = self._root_of(assign.value)
            if root is None:
                continue
            self.aliases[(id(self.root), name)] = root
            self.aliased_values.add(id(assign.value))
            self._hold(root[0], root[1], name, ())

    def _hold(
        self, targets: set[str], sources: tuple[str, ...], holder: str, writer: tuple[str, ...]
    ) -> None:
        writer = self.prefix + writer
        self.held.append((targets, sources, writer))
        for target in sorted(targets):
            if target != FRESH:
                self._record(HELD, target, ("=" + holder, *sources), writer)

    def _root_of(self, expr: ast.expr) -> tuple[set[str], tuple[str, ...]] | None:
        """The modules (or what they hold) an expression names, when it is a
        name chain from an import, a module handle, or an attribute chain off
        one, with nothing in it that writes."""
        attrs: list[str] = []
        while isinstance(expr, ast.Attribute):
            if expr.attr in CONTAINER_MUTATORS or expr.attr in _WRITING_ATTRIBUTES:
                return None
            attrs.append(expr.attr)
            expr = expr.value
        attrs.reverse()
        if isinstance(expr, ast.Name):
            alias = self._alias(expr)
            names = self._binding(expr.id, expr)
            if alias is not None:
                base, sources = alias
            elif names is not None and expr.id in names.imports and expr.id not in names.other:
                base, sources = set(names.imports[expr.id]), ()
            else:
                return None
        else:
            handle = self._handle(expr)
            if handle is None or OBJECTS in handle[0]:
                return None  # a list of objects is not one of them
            base, sources = handle
        return {_join(t, attrs) for t in base}, sources

    def _climb(
        self,
        node: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
    ) -> None:
        """Follow an object up its attribute chain to where it is used."""
        while True:
            p = self.parents.get(id(node))
            if isinstance(p, ast.Attribute) and p.value is node:
                attr = p.attr
                if targets is not None and self._is_modules_table(targets, chain):
                    # An item read (``get``, ``pop``, ``setdefault``) is a
                    # handle, scanned on its own, as an install (``update``,
                    # ``__setitem__``) is; removing entries hands nothing out.
                    if attr not in (
                        "get",
                        "pop",
                        "setdefault",
                        "keys",
                        "__contains__",
                        "update",
                        "__setitem__",
                        "__delitem__",
                        "clear",
                    ):
                        self._emit(p, targets, sources, chain, USE, any_module=True)
                    return
                if not isinstance(p.ctx, ast.Load):
                    self._emit(p, targets, sources, (*chain, attr), STORE)
                    return
                if attr in CONTAINER_MUTATORS:
                    self._emit(node, targets, sources, chain, MUT)
                    return
                if attr in ("__setattr__", "__delattr__"):
                    self._emit(node, targets, sources, chain, USE)
                    return
                if attr == "__dict__":
                    if targets is not None:
                        self._namespace(p, targets, sources, chain)
                    return
                if attr in GLOBALS_ATTRIBUTES:
                    self._globals(p, node, targets, sources, chain, attr)
                    return
                node, chain = p, (*chain, attr)
                continue
            if targets is not None and self._is_modules_table(targets, chain):
                # ``sys.modules`` itself: an item read is a handle (scanned on
                # its own), membership and iteration see names, and installing
                # or removing an entry (``monkeypatch.setitem(sys.modules,
                # "torch", fake)``) hands nothing out; anything else hands out
                # every module.
                installs = (
                    isinstance(p, ast.Call)
                    and bool(p.args)
                    and p.args[0] is node
                    and (_chain(p.func) or [""])[-1] in ("setitem", "delitem", "dict")
                )
                if not installs and not isinstance(
                    p, (ast.Subscript, ast.Compare, ast.For, ast.comprehension)
                ):
                    self._emit(node, targets, sources, chain, USE, any_module=True)
                return
            if targets is not None and self._is_import_function(targets, chain):
                if not (isinstance(p, ast.Call) and p.func is node):
                    self._emit(node, targets, sources, chain, USE, any_module=True)
                return
            if isinstance(p, ast.Call) and p.args and p.args[0] is node:
                builtin = self._builtin(p.func)
                parts = _chain(p.func) or []
                if builtin == "getattr":
                    names, more = self._evaluate(p.args[1]) if len(p.args) > 1 else (None, ())
                    if names is None:
                        if classify(p, self.parents, self._builtin) != READ:
                            self._emit(node, targets, sources, chain, DYN)
                        return
                    for name in names:
                        if name in GLOBALS_ATTRIBUTES:
                            self._globals(p, node, targets, sources + more, chain, name)
                        elif name in CONTAINER_MUTATORS or name in _WRITING_ATTRIBUTES:
                            self._emit(node, targets, sources, chain, USE)
                        else:
                            self._climb(p, targets, sources + more, (*chain, name))
                    return
                if parts and _setattr_like(parts, builtin):
                    name_arg = p.args[1] if len(p.args) > 1 else None
                    for k in p.keywords:
                        if k.arg in ("name", "attribute"):
                            name_arg = k.value
                    names, more = self._evaluate(name_arg) if name_arg is not None else (None, ())
                    if names is None:
                        self._emit(node, targets, sources, chain, USE)
                    else:
                        for name in names:
                            self._emit(node, targets, sources + more, (*chain, name), STORE)
                            if builtin is None:
                                # ``monkeypatch.setattr``, ``patch.object``:
                                # undone after the test.
                                self._emit(node, targets, sources, (*chain, name), SCOPED)
                    return
                if builtin == "vars":
                    if targets is not None:
                        self._namespace(p, targets, sources, chain)
                    return
            if isinstance(p, ast.Call) and p.func is node and targets is not None:
                # A private function called through a module or an import:
                # a bound taken from its calls (``_call_site_strings``) must
                # know of it.
                for target in sorted(targets):
                    called = _join(target, chain)
                    if ANY not in called and FRESH not in called:
                        if _private(called.rsplit(".", 1)[-1]):
                            self._record(CALL, called, (), self._writer(node))
                return
            if (
                isinstance(p, ast.Call)
                and p.args
                and p.args[0] is node
                and isinstance(p.func, ast.Attribute)
                and p.func.attr == "exec_module"
            ):
                # A loader runs the module's own code in it: a module of the
                # analysis runs code the analysis read; a file nothing bounds
                # runs code read at run time, which may reach anything (as
                # ``runpy.run_path`` does).
                if targets is None or any(t.endswith(ANY) for t in targets):
                    self._record(USE, ANY, (), self._writer(node))
                return
            if id(node) in self.aliased_values:
                return  # followed through the name it is bound to
            if targets is not None and self._installed_as(node, targets, sources):
                return
            if isinstance(p, ast.Return) and id(p) in self.followed_returns:
                return  # followed to the calls of its function
            kind = classify(node, self.parents, self._builtin)
            if kind != READ:
                self._emit(node, targets, sources, chain, kind)
            return

    def _globals(
        self,
        space: ast.AST,
        holder: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
        attr: str,
    ) -> None:
        """A module's namespace handed out at ``space``: ``f.__globals__`` is
        that of the module ``f`` is defined in (resolved between the passes:
        ``Indexer._apply_uses``), this module's for a function defined here;
        a frame's ``f_globals`` (and ``f_locals``, its globals at module
        level), or ``__globals__`` of an object nothing tells, any module's."""
        if attr == "__globals__" and targets is not None and not any(ANY in t for t in targets):
            self._namespace(space, targets, sources, (*chain, attr))
            return
        if (
            attr == "__globals__"
            and targets is None
            and not chain
            and isinstance(holder, ast.Name)
            and self._own_function(holder)
        ):
            self._namespace(space, None, (), ())
            return
        self._namespace(space, {ANY}, (), ())

    def _own_function(self, name: ast.Name) -> bool:
        """Whether ``name`` is a function this module defines (its globals
        are this module's), bound at module level by ``def`` alone."""
        if self._binding(name.id, name) is not self.scope_names.get(id(self.root)):
            return False
        defs = [
            n
            for n in self.nodes
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name.id
            and self._scope_of(n) is self.root
        ]
        stores = [
            n
            for n in self.nodes
            if (isinstance(n, ast.Name) and n.id == name.id and not isinstance(n.ctx, ast.Load))
            or (isinstance(n, ast.ClassDef) and n.name == name.id)
            or (isinstance(n, ast.alias) and (n.asname or n.name.split(".")[0]) == name.id)
        ]
        return bool(defs) and not stores

    def _namespace(
        self,
        node: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
    ) -> None:
        """A module's namespace dict (``m.__dict__``, ``vars(m)``; this
        module's own for ``globals()`` and a bare ``vars()``, with ``targets``
        None): an item read by a known name is that attribute, read where it
        stands; by an unknown name it is any of them; anything else may
        change the namespace."""
        p = self.parents.get(id(node))
        item: ast.AST | None = None
        key: ast.expr | None = None
        if isinstance(p, ast.Subscript) and p.value is node:
            if not isinstance(p.ctx, ast.Load):
                names, more = self._evaluate(p.slice) if targets is not None else (None, ())
                if names is None:
                    self._changes(node, targets, sources, chain, MUT)
                else:
                    # ``m.__dict__["T"] = v`` rebinds ``m.T``, as ``m.T = v`` does.
                    for name in names:
                        self._emit(p, targets, sources + more, (*chain, name), STORE)
                return
            item, key = p, p.slice
        elif isinstance(p, ast.Attribute) and p.value is node:
            call = self.parents.get(id(p))
            if p.attr in ("keys", "__contains__", "__len__", "__iter__"):
                return
            if (
                p.attr in ("get", "__getitem__")
                and isinstance(call, ast.Call)
                and call.func is p
                and call.args
            ):
                item, key = call, call.args[0]
            elif p.attr in ("values", "items", "copy"):
                self._changes(node, targets, sources, chain, DYN)  # hands out every value
                return
            else:
                kind = MUT if p.attr in CONTAINER_MUTATORS else USE
                self._changes(node, targets, sources, chain, kind)
                return
        if item is None or key is None:
            if classify(node, self.parents, self._builtin) != READ:
                self._changes(node, targets, sources, chain, USE)
            return
        names, more = self._evaluate(key)
        if names is None:
            if classify(item, self.parents, self._builtin) != READ:
                self._changes(node, targets, sources, chain, DYN)
            return
        for name in names:
            if targets is None:
                if classify(item, self.parents, self._builtin) != READ:
                    self.out.names.add(name)
                imported = self.scope_names[id(self.root)].imports.get(name)
                if self.want_records and imported:
                    # ``globals()[name]``: the module-level binding.
                    self._climb(item, set(imported), more, ())
            else:
                self._climb(item, targets, sources + more, (*chain, name))

    def _changes(
        self,
        node: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
        kind: str,
    ) -> None:
        """A change to a namespace (``kind`` MUT: an item stored or a mutator
        reached), or its values handed out (USE, DYN)."""
        if targets is not None:
            # A module's namespace changing is the module written to.
            self._emit(node, targets, sources, chain, USE if kind == MUT else kind)
            return
        self.out.namespace = True
        if kind != MUT and self.want_records:
            # Whoever holds this module's globals holds what it imported.
            writer = self._writer(node)
            for imported in self.imports.values():
                for target in imported:
                    self._record(USE, target, (), writer)

    @staticmethod
    def _is_modules_table(targets: set[str], chain: tuple[str, ...]) -> bool:
        return any(_join(t, chain) == "sys.modules" for t in targets)

    @staticmethod
    def _is_import_function(targets: set[str], chain: tuple[str, ...]) -> bool:
        return any(_join(t, chain) in _IMPORT_CALLS for t in targets)

    def _emit_root(self, node: ast.AST, kind: str, chain: tuple[str, ...]) -> None:
        if isinstance(node, ast.Name):
            names = self._binding(node.id, node)
            imported = names.imports.get(node.id) if names is not None else None
            if names is not None and imported:
                if self.want_records:
                    self._emit(node, set(imported), (), chain, kind)
                if node.id not in names.other:
                    return
        self._emit(node, None, (), chain, kind)

    def _emit(
        self,
        node: ast.AST,
        targets: set[str] | None,
        sources: tuple[str, ...],
        chain: tuple[str, ...],
        kind: str,
        *,
        any_module: bool = False,
    ) -> None:
        if targets is None:
            # A bare name: this module's own (or a star import's).
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if not isinstance(root, ast.Name):
                return
            if not chain and kind in (USE, MUT, DYN):
                self.out.names.add(root.id)
            if self.want_records and self.stars and root.id not in self.bound:
                self._record(kind, _join("@" + root.id, chain), (), self._writer(node))
            return
        if not self.want_records:
            return
        writer = self._writer(node)
        if any_module:
            self._record(USE, ANY, (), writer)
            return
        for target in sorted(targets):
            if target != FRESH:
                self._record(kind, _join(target, chain), sources, writer)

    def _record(
        self, kind: str, target: str, sources: tuple[str, ...], writer: tuple[str, ...]
    ) -> None:
        if kind == STORE and target == "sys.modules":
            kind, target = USE, ANY  # a table of modules of its own: any import
        if target == ANY and kind != HELD:
            sources = ()  # already any module
        self.out.records.add(UseRecord(kind, target, tuple(sorted(set(sources))), writer))


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """The nodes of a scope's body, not those of the scopes nested in it."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        found.append(n)
        if not isinstance(n, _SCOPES):
            stack.extend(ast.iter_child_nodes(n))
    return found


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _join(target: str, chain: tuple[str, ...] | list[str]) -> str:
    return ".".join([target, *chain]) if chain else target


def scan_names(node: ast.AST) -> Uses:
    """Names ``node`` (a module, a function, any scope) uses other than as
    reads, nested scopes included, and whether it reaches its namespace."""
    return _Scanner(node, module="", is_package=False, evaluate=_no_evaluate, records=False).scan()


def scan_module(tree: ast.Module, module: str, is_package: bool, evaluate: Evaluate) -> Uses:
    """A module's records (see UseRecord), with the names of scan_names."""
    return _Scanner(
        tree, module=module, is_package=is_package, evaluate=evaluate, records=True
    ).scan()
